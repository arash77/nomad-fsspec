import errno
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import IO, Any

from fsspec import AbstractFileSystem
from fsspec.callbacks import DEFAULT_CALLBACK
from fsspec.dircache import DirCache
from fsspec.utils import isfilelike

from .client import DEFAULT_BASE_URL, NomadClient, RangeUnavailable
from .errors import NomadError, NomadTimeout
from .zipindex import Tree, ZipIndexError, children, read_tree, size_of

# NOMAD reads a whole directory for every page, so ask for all of it at once
PAGE_SIZE = 10000
AGGREGATION_PAGE_SIZE = 2500
# uploads named at once; one request each takes about 0.2 s
NAMES_WORKERS = 8
# upload names kept before they are dropped and asked for again
MAX_NAMES = 10000
CHUNK_SIZE = 2**20
DATASETS = "datasets"
UPLOADS = "uploads"
# smaller uploads list quickly folder by folder, and every ZIP read waits its turn: NOMAD allows
# one every 5 seconds per client
ZIP_MIN_ENTRIES = 300
# how long NOMAD may take to list a folder of a smaller published upload before its ZIP is read;
# NOMAD can take 20 seconds the first time it reads an upload, the ZIP a second or two
QUICK_LISTING_SECONDS = 5
# memory for the file lists kept from ZIPs, across all uploads; the largest public upload on
# nomad-lab.eu (394,495 files) takes about 10 MB
MAX_ZIP_BYTES = 64 << 20
# the largest public upload on nomad-lab.eu has a central directory of 34 MB
MAX_ZIP_DIRECTORY = 128 << 20
# how long the shared list of public datasets is kept when listings do not expire
SHARED_DATASETS_SECONDS = 300
# how long an upload NOMAD failed to describe is listed folder by folder before it is asked again
ASK_AGAIN_SECONDS = 60

# published uploads never change and their ZIPs are read without a token, so instances share them
_zip_trees: OrderedDict[tuple[str, str], tuple[Tree, int]] = OrderedDict()
# the public datasets are asked for as owner "public" whatever the token, so instances share them
_shared_datasets: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_shared_lock = threading.Lock()


class NomadFileSystem(AbstractFileSystem):
    """Read-only access to the raw files of a NOMAD deployment.

    The top level has two folders. ``/datasets`` lists the public datasets, with paths
    ``/datasets/<dataset_id>/<upload_id>/<path inside the upload>``. Uploads inside a dataset are
    listed by id; ``upload_names`` looks up their names and ``search_uploads`` finds them by name.

    ``/uploads`` lists the uploads a token's user can read, published or not, with paths
    ``/uploads/<upload_id>/<path inside the upload>``. Listing it needs a token; reading a
    published upload through it does not.

    Datasets and uploads listed with their names carry them in ``display_name``.

    A published upload is listed from the central directory of its ZIP, read with one or two
    byte ranges and kept: NOMAD's own folder listing adds up every size below a folder and can
    take a minute on large uploads. Other uploads are listed folder by folder.
    """

    protocol = "nomad"
    root_marker = "/"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: tuple[float, float] = (10, 120),
        token: str | None = None,
        **storage_options: Any,
    ) -> None:
        super().__init__(**storage_options)
        self.dircache = _Listings(**storage_options)
        token = (token or "").strip() or None
        self.client = NomadClient(base_url, timeout, token)
        self._has_token = bool(token)
        self._upload_names: dict[str, str | None] = {}
        self._no_zip: set[str] = set()
        self._small: set[str] = set()
        self._unanswered: dict[str, float] = {}

    @classmethod
    def _strip_protocol(cls, path: Any) -> Any:
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]
        parts = super()._strip_protocol(path).split("/")
        return "/" + "/".join(part for part in parts if part)

    def ls(self, path: str, detail: bool = True, refresh: bool = False, **kwargs: Any) -> list[Any]:
        path = self._strip_protocol(path)
        # not _ls_from_cache: it answers a folder with its own entry once the parent is cached
        key = path.rstrip("/")
        entries = None if refresh else self.dircache.get(key)
        if entries is None:
            with _named(path):
                entries = self._fetch(path)
            located = self._locate(path)
            # a folder of a kept ZIP is listed from it again rather than kept a second time
            if located is None or self._kept_tree(located[0]) is None:
                self.dircache[key] = entries
        return entries if detail else [entry["name"] for entry in entries]

    def invalidate_cache(self, path: str | None = None) -> None:
        if path is None:
            self.dircache.clear()
            self._upload_names.clear()
            self._no_zip.clear()
            self._small.clear()
            self._unanswered.clear()
            with _shared_lock:
                for key in [key for key in _zip_trees if key[0] == self.client.api_url]:
                    del _zip_trees[key]
                _shared_datasets.pop(self.client.api_url, None)
        else:
            self.dircache.pop(self._strip_protocol(path).rstrip("/"), None)

    def get_file(
        self,
        rpath: str,
        lpath: Any = None,
        callback: Any = DEFAULT_CALLBACK,
        outfile: IO[bytes] | None = None,
        **kwargs: Any,
    ) -> None:
        rpath = self._strip_protocol(rpath)
        located = self._locate(rpath)
        if isfilelike(lpath):
            outfile = lpath
        elif located is None or not located[1] or self._cached_type(rpath) == "directory":
            # only the cached listing is asked, so a plain download stays one request
            if os.path.isfile(lpath):
                raise IsADirectoryError(errno.EISDIR, "Is a directory", rpath)
            os.makedirs(lpath, exist_ok=True)
            return
        raw = self._raw(*self._file_parts(rpath))
        params = {"ignore_mime_type": "true"}
        callback = callback or DEFAULT_CALLBACK
        created = False
        try:
            with (
                _named(rpath),
                self.client.request("GET", raw, params=params, stream=True) as response,
            ):
                chunks = response.iter_content(CHUNK_SIZE)
                if outfile is not None:
                    _copy(chunks, outfile, callback)
                else:
                    os.makedirs(os.path.dirname(os.path.abspath(lpath)), exist_ok=True)
                    with open(lpath, "wb") as target:
                        created = True
                        _copy(chunks, target, callback)
        except BaseException:
            if created:
                os.remove(lpath)
            raise

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        raw = self._raw(*self._file_parts(path))
        start = start or 0
        if start < 0 or (end is not None and end < 0):
            size = int(self.info(path)["size"])
            start = max(size + start, 0) if start < 0 else start
            end = size + end if end is not None and end < 0 else end
        if end is not None and end <= start:
            return b""
        # NOMAD ignores HTTP Range headers; it takes offset and length instead
        params: dict[str, Any] = {"ignore_mime_type": "true"}
        if start:
            params["offset"] = start
        if end is not None:
            params["length"] = end - start
        with _named(path), self.client.request("GET", raw, params=params) as response:
            return response.content

    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> Any:
        if mode != "rb":
            raise PermissionError(f"NOMAD is read-only; cannot open {path} with mode {mode!r}")
        self._file_parts(path)
        info = self.info(path)
        if info["type"] == "directory":
            raise IsADirectoryError(path)
        return super()._open(path, mode=mode, size=info["size"], **kwargs)

    def upload_names(self, upload_ids: Iterable[str]) -> dict[str, str]:
        """Look up the names of uploads, which a dataset's listing leaves out.

        Each upload is one request, several at a time. Names are kept, so only uploads not asked
        about before cost a request; an upload that could not be asked about is left out.
        """
        wanted = list(dict.fromkeys(upload_ids))
        if len(self._upload_names) > MAX_NAMES:
            self._upload_names.clear()
        missing = [upload_id for upload_id in wanted if upload_id not in self._upload_names]
        if missing:
            with ThreadPoolExecutor(min(NAMES_WORKERS, len(missing))) as pool:
                for upload_id, (known, name) in zip(
                    missing, pool.map(self._upload_name, missing), strict=True
                ):
                    if known:
                        self._upload_names[upload_id] = name
        return {
            upload_id: name for upload_id in wanted if (name := self._upload_names.get(upload_id))
        }

    def _upload_name(self, upload_id: str) -> tuple[bool, str | None]:
        try:
            upload = self.client.get_json(["uploads", upload_id]).get("data") or {}
        except (NomadError, OSError):
            return False, None
        return True, upload.get("upload_name")

    def search_uploads(self, dataset_id: str, text: str) -> list[str]:
        """Find the public uploads of a dataset whose name contains ``text``, ignoring case.

        Returns their ids, for ``upload_names`` to name. Names are matched in NOMAD first, so
        this takes two requests however large the dataset is. Uploads without a name never match.
        """
        needle = text.casefold()
        query: dict[str, Any] = {"datasets.dataset_id": dataset_id}
        names = [
            bucket["value"]
            for bucket in self._aggregate("upload_name", query)
            if needle in str(bucket["value"]).casefold()
        ]
        if not names:
            return []
        found = self._aggregate("upload_id", {**query, "upload_name:any": names})
        upload_ids = [bucket["value"] for bucket in found]
        if len(names) == 1:
            self._upload_names.update(dict.fromkeys(upload_ids, names[0]))
        return upload_ids

    def _cached_type(self, path: str) -> str | None:
        located = self._locate(path)
        if located is not None:
            tree = self._kept_tree(located[0])
            if tree is not None and "/".join(located[1]) in tree:
                return "directory"
        listing = self.dircache.get(self._parent(path).rstrip("/")) or []
        return next((entry["type"] for entry in listing if entry["name"] == path), None)

    def _locate(self, path: str) -> tuple[str, list[str]] | None:
        """The upload id of a path and the parts inside the upload, or None above an upload."""
        parts = _split(self._strip_protocol(path))
        if parts and parts[0] not in (DATASETS, UPLOADS):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        # path segments up to and including the upload id
        depth = 3 if parts[:1] == [DATASETS] else 2
        return (parts[depth - 1], parts[depth:]) if len(parts) >= depth else None

    def _file_parts(self, path: str) -> tuple[str, list[str]]:
        located = self._locate(path)
        if located is None or not located[1]:
            raise IsADirectoryError(path)
        return located

    def _raw(self, upload_id: str, inner: list[str]) -> list[str]:
        return ["uploads", upload_id, "raw", *inner]

    def _fetch(self, path: str) -> list[dict[str, Any]]:
        parts = _split(path)
        if not parts:
            return [
                _directory(f"/{DATASETS}", "Public datasets"),
                _directory(f"/{UPLOADS}", "My uploads"),
            ]
        located = self._locate(path)
        if located is not None:
            return self._list_upload_directory(path, *located)
        if parts == [DATASETS]:
            return self._list_datasets()
        if parts == [UPLOADS]:
            return self._list_own_uploads()
        return self._list_dataset_uploads(parts[1])

    def _list_datasets(self) -> list[dict[str, Any]]:
        # each thread has its own instance, so its own listings; this one costs a second or two
        share = self.dircache.use_listings_cache
        lifetime = self.dircache.listings_expiry_time or SHARED_DATASETS_SECONDS
        if share:
            with _shared_lock:
                kept = _shared_datasets.get(self.client.api_url)
            if kept is not None and time.monotonic() - kept[0] < lifetime:
                return list(kept[1])
        public = {bucket["value"] for bucket in self._aggregate("datasets.dataset_id", {})}
        params = {"order_by": "dataset_name", "order": "asc"}
        datasets = _by_name(
            [
                _directory(f"/{DATASETS}/{dataset['dataset_id']}", dataset.get("dataset_name"))
                for dataset in self._pages(["datasets", ""], params)
                if dataset["dataset_id"] in public
            ]
        )
        if share:
            with _shared_lock:
                _shared_datasets[self.client.api_url] = (time.monotonic(), datasets)
        return list(datasets)

    def _list_own_uploads(self) -> list[dict[str, Any]]:
        if not self._has_token:
            raise PermissionError("NOMAD lists your own uploads only with a token")
        return _by_name(
            [
                _directory(f"/{UPLOADS}/{upload['upload_id']}", upload.get("upload_name"))
                for upload in self._pages(["uploads"], {})
            ]
        )

    def _list_dataset_uploads(self, dataset_id: str) -> list[dict[str, Any]]:
        # ids only: naming all uploads of a large dataset at once makes NOMAD time out
        buckets = self._aggregate("upload_id", {"datasets.dataset_id": dataset_id})
        if not buckets:
            raise FileNotFoundError(f"/{DATASETS}/{dataset_id}")
        return sorted(
            (_directory(f"/{DATASETS}/{dataset_id}/{bucket['value']}", None) for bucket in buckets),
            key=lambda entry: entry["name"],
        )

    def _pages(self, parts: list[str], params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            page_params = {**params, "page_size": PAGE_SIZE, "page_offset": offset}
            body = self.client.get_json(parts, page_params)
            page = body.get("data") or []
            yield from page
            offset += len(page)
            if not page or offset >= body["pagination"]["total"]:
                return

    def _list_upload_directory(
        self, path: str, upload_id: str, inner: list[str]
    ) -> list[dict[str, Any]]:
        tree = self._zip_tree(upload_id)
        if tree is None and upload_id in self._small:
            try:
                return self._list_by_folder(path, upload_id, inner, quick=True)
            except NomadTimeout:
                tree = self._zip_tree(upload_id, small_too=True)
        if tree is not None:
            return _from_tree(tree, path, inner)
        return self._list_by_folder(path, upload_id, inner)

    def _list_by_folder(
        self, path: str, upload_id: str, inner: list[str], quick: bool = False
    ) -> list[dict[str, Any]]:
        timeout = (self.client.timeout[0], QUICK_LISTING_SECONDS) if quick else None
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {"page_size": PAGE_SIZE, "page_offset": offset}
            parts = ["uploads", upload_id, "rawdir", *inner, ""]
            body = self.client.get_json(parts, params, timeout)
            if "directory_metadata" not in body:
                size = (body.get("file_metadata") or {}).get("size")
                return [{"name": path, "size": int(size or 0), "type": "file"}]
            content = body["directory_metadata"]["content"]
            entries.extend(
                {
                    "name": f"{path}/{item['name']}",
                    # NOMAD gives folders the size of everything inside; fsspec expects 0
                    "size": int(item.get("size") or 0) if item.get("is_file") else 0,
                    "type": "file" if item.get("is_file") else "directory",
                }
                for item in content
                if item.get("name")
            )
            offset += len(content)
            # an empty page or the total ends the listing; NOMAD answers 400 past the last page
            if not content or offset >= body["pagination"]["total"]:
                return entries

    def _zip_tree(self, upload_id: str, small_too: bool = False) -> Tree | None:
        """The files of a published upload from its ZIP, or None to list it by folder.

        A smaller upload is read from its ZIP only with ``small_too``.
        """
        kept = self._kept_tree(upload_id)
        if kept is not None:
            return kept
        if upload_id in self._no_zip:
            return None
        if upload_id not in self._small:
            if time.monotonic() < self._unanswered.get(upload_id, 0):
                return None
            facts = self._upload_facts(upload_id)
            if facts is None:
                self._unanswered[upload_id] = time.monotonic() + ASK_AGAIN_SECONDS
                return None
            self._unanswered.pop(upload_id, None)
            public, entries = facts
            if not public:
                self._no_zip.add(upload_id)
                return None
            if entries <= ZIP_MIN_ENTRIES:
                self._small.add(upload_id)
        if upload_id in self._small and not small_too:
            return None
        parts = ["uploads", upload_id, "raw"]
        try:
            tree = read_tree(
                lambda byte_range: self.client.read_range(parts, byte_range), MAX_ZIP_DIRECTORY
            )
        except RangeUnavailable as e:
            # a busy NOMAD is asked again next time; a missing ZIP is not
            if e.lasting:
                self._no_zip.add(upload_id)
            return None
        except ZipIndexError:
            self._no_zip.add(upload_id)
            return None
        size = size_of(tree)
        if size <= MAX_ZIP_BYTES:
            with _shared_lock:
                _zip_trees[(self.client.api_url, upload_id)] = (tree, size)
                in_use = sum(kept_size for _, kept_size in _zip_trees.values())
                while in_use > MAX_ZIP_BYTES:
                    _, (_, dropped) = _zip_trees.popitem(last=False)
                    in_use -= dropped
        return tree

    def _kept_tree(self, upload_id: str) -> Tree | None:
        key = (self.client.api_url, upload_id)
        with _shared_lock:
            kept = _zip_trees.get(key)
            if kept is None:
                return None
            _zip_trees.move_to_end(key)
            return kept[0]

    def _upload_facts(self, upload_id: str) -> tuple[bool, int] | None:
        """Whether anyone may read the upload's ZIP, and its number of entries.

        None when NOMAD did not answer, so that the upload is asked about again later.
        """
        # only a published upload without an embargo has a ZIP that anyone may read
        try:
            upload = self.client.get_json(["uploads", upload_id]).get("data") or {}
        except (FileNotFoundError, PermissionError):
            return False, 0
        except (NomadError, OSError):
            return None
        public = bool(upload.get("published")) and not upload.get("with_embargo")
        return public, int(upload.get("entries") or 0)

    def _aggregate(self, quantity: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        buckets: list[dict[str, Any]] = []
        after = None
        while True:
            pagination: dict[str, Any] = {"page_size": AGGREGATION_PAGE_SIZE}
            if after is not None:
                pagination["page_after_value"] = after
            terms: dict[str, Any] = {"quantity": quantity, "pagination": pagination}
            body = self.client.post_json(
                ["entries", "query"],
                {
                    "owner": "public",
                    "query": query,
                    "pagination": {"page_size": 0},
                    "aggregations": {"buckets": {"terms": terms}},
                },
            )
            result = body["aggregations"]["buckets"]["terms"]
            buckets.extend(result["data"])
            # NOMAD sets a cursor even on the last page, so a short page is the end
            if len(result["data"]) < AGGREGATION_PAGE_SIZE:
                return buckets
            after = (result.get("pagination") or {}).get("next_page_after_value")
            if after is None:
                return buckets


class _Listings(DirCache):
    """fsspec's listing cache, with ``max_paths`` and ``listings_expiry_time`` freeing memory.

    fsspec's own drops a listing past either only when that listing is asked for again, so an
    instance that lists many folders keeps them all.
    """

    def __init__(
        self,
        use_listings_cache: bool = True,
        listings_expiry_time: float | None = None,
        max_paths: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(use_listings_cache, listings_expiry_time)
        self.max_paths = max_paths
        # oldest first, by when each listing was set
        self._cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()

    def __getitem__(self, item: str) -> list[dict[str, Any]]:
        stored, listing = self._cache[item]
        if self._expired(stored):
            del self._cache[item]
            raise KeyError(item)
        return listing

    def __setitem__(self, key: str, value: list[dict[str, Any]]) -> None:
        if not self.use_listings_cache:
            return
        self._cache.pop(key, None)
        self._cache[key] = (time.monotonic(), value)
        while self._cache and (
            (self.max_paths and len(self._cache) > self.max_paths)
            or self._expired(next(iter(self._cache.values()))[0])
        ):
            self._cache.popitem(last=False)

    def _expired(self, stored: float) -> bool:
        expiry = self.listings_expiry_time
        return expiry is not None and time.monotonic() - stored > expiry

    def __reduce__(self) -> Any:
        return type(self), (self.use_listings_cache, self.listings_expiry_time, self.max_paths)


def _split(path: str) -> list[str]:
    parts = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid NOMAD path: {path}")
    return parts


def _from_tree(tree: Tree, path: str, inner: list[str]) -> list[dict[str, Any]]:
    folder = "/".join(inner)
    listed = children(tree, folder)
    if listed is None:
        parent, _, name = folder.rpartition("/")
        size = dict(children(tree, parent) or []).get(name)
        if size is None:
            raise FileNotFoundError(path)
        return [{"name": path, "size": size, "type": "file"}]
    return [
        {
            "name": f"{path}/{name}",
            "size": size or 0,
            "type": "directory" if size is None else "file",
        }
        for name, size in listed
    ]


def _directory(path: str, display_name: str | None) -> dict[str, Any]:
    name = display_name or path.rsplit("/", 1)[-1]
    return {"name": path, "size": 0, "type": "directory", "display_name": name}


def _by_name(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: (entry["display_name"].casefold(), entry["name"]))


def _copy(chunks: Any, target: IO[bytes], callback: Any) -> None:
    for chunk in chunks:
        target.write(chunk)
        callback.relative_update(len(chunk))


@contextmanager
def _named(path: str) -> Iterator[None]:
    """Report a missing path or a folder by its own name, not by NOMAD's API route."""
    try:
        yield
    except FileNotFoundError as e:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", path) from e
    except IsADirectoryError as e:
        raise IsADirectoryError(errno.EISDIR, "Is a directory", path) from e
