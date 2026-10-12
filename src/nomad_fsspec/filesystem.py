import errno
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import IO, Any

from fsspec import AbstractFileSystem
from fsspec.callbacks import DEFAULT_CALLBACK
from fsspec.utils import isfilelike

from .client import DEFAULT_BASE_URL, NomadClient

# NOMAD reads a whole directory for every page, so ask for all of it at once
PAGE_SIZE = 10000
AGGREGATION_PAGE_SIZE = 2500
# NOMAD takes about 0.7 s to name 25 uploads but 7 s to name 100
NAMES_BATCH = 25
CHUNK_SIZE = 2**20
DATASETS = "datasets"
UPLOADS = "uploads"


class NomadFileSystem(AbstractFileSystem):
    """Read-only access to the raw files of a NOMAD deployment.

    The top level has two folders. ``/datasets`` lists the public datasets, with paths
    ``/datasets/<dataset_id>/<upload_id>/<path inside the upload>``. Uploads inside a dataset are
    listed by id; ``upload_names`` looks up their names and ``search_uploads`` finds them by name.

    ``/uploads`` lists the uploads a token's user can read, published or not, with paths
    ``/uploads/<upload_id>/<path inside the upload>``. Listing it needs a token; reading a
    published upload through it does not.

    Datasets and uploads listed with their names carry them in ``display_name``.
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
        self.client = NomadClient(base_url, timeout, token)
        self._has_token = bool(token)
        self._upload_names: dict[str, str | None] = {}

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
            self.dircache[key] = entries
        return entries if detail else [entry["name"] for entry in entries]

    def invalidate_cache(self, path: str | None = None) -> None:
        if path is None:
            self.dircache.clear()
            self._upload_names.clear()
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
        """Look up the names of public uploads, which a dataset's listing leaves out.

        Names are kept, so only uploads not asked about before cost a request.
        """
        wanted = list(dict.fromkeys(upload_ids))
        missing = [upload_id for upload_id in wanted if upload_id not in self._upload_names]
        for start in range(0, len(missing), NAMES_BATCH):
            batch = missing[start : start + NAMES_BATCH]
            names: dict[str, str | None] = dict.fromkeys(batch)
            for bucket in self._aggregate("upload_id", {"upload_id:any": batch}, ["upload_name"]):
                names[bucket["value"]] = (bucket.get("entries") or [{}])[0].get("upload_name")
            self._upload_names.update(names)
        return {upload_id: name for upload_id in wanted if (name := self._upload_names[upload_id])}

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
        public = {bucket["value"] for bucket in self._aggregate("datasets.dataset_id", {})}
        params = {"order_by": "dataset_name", "order": "asc"}
        return _by_name(
            [
                _directory(f"/{DATASETS}/{dataset['dataset_id']}", dataset.get("dataset_name"))
                for dataset in self._pages(["datasets", ""], params)
                if dataset["dataset_id"] in public
            ]
        )

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
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {"page_size": PAGE_SIZE, "page_offset": offset}
            body = self.client.get_json(["uploads", upload_id, "rawdir", *inner, ""], params)
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

    def _aggregate(
        self, quantity: str, query: dict[str, Any], include: list[str] | None = None
    ) -> list[dict[str, Any]]:
        buckets: list[dict[str, Any]] = []
        after = None
        while True:
            pagination: dict[str, Any] = {"page_size": AGGREGATION_PAGE_SIZE}
            if after is not None:
                pagination["page_after_value"] = after
            terms: dict[str, Any] = {"quantity": quantity, "pagination": pagination}
            if include:
                terms["entries"] = {"size": 1, "required": {"include": include}}
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


def _split(path: str) -> list[str]:
    parts = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid NOMAD path: {path}")
    return parts


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
