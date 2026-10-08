from typing import Any

from fsspec import AbstractFileSystem

from .client import DEFAULT_BASE_URL, NomadClient

# NOMAD reads a whole directory for every page, so ask for all of it at once
PAGE_SIZE = 10000
AGGREGATION_PAGE_SIZE = 2500
CHUNK_SIZE = 2**20


class NomadFileSystem(AbstractFileSystem):
    """Read-only access to the public datasets of a NOMAD deployment.

    Paths are ``/<dataset_id>/<upload_id>/<path inside the upload>``. Datasets and uploads carry
    their names in ``display_name``.
    """

    protocol = "nomad"
    root_marker = "/"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: tuple[float, float] = (10, 120),
        **storage_options: Any,
    ) -> None:
        super().__init__(**storage_options)
        self.client = NomadClient(base_url, timeout)

    @classmethod
    def _strip_protocol(cls, path: Any) -> Any:
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]
        return "/" + super()._strip_protocol(path).strip("/")

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list[Any]:
        path = self._strip_protocol(path)
        entries = self._ls_from_cache(path)
        if entries is None:
            entries = self._fetch(path)
            self.dircache[path.rstrip("/")] = entries
        return entries if detail else [entry["name"] for entry in entries]

    def _fetch(self, path: str) -> list[dict[str, Any]]:
        parts = _split(path)
        if not parts:
            return self._list_datasets()
        if len(parts) == 1:
            return self._list_uploads(parts[0])
        raise FileNotFoundError(path)

    def _list_datasets(self) -> list[dict[str, Any]]:
        public = {bucket["value"] for bucket in self._aggregate("datasets.dataset_id", {})}
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "page_size": PAGE_SIZE,
                "page_offset": offset,
                "order_by": "dataset_name",
                "order": "asc",
            }
            body = self.client.get_json(["datasets", ""], params)
            page = body["data"]
            entries.extend(
                _directory(f"/{dataset['dataset_id']}", dataset.get("dataset_name"))
                for dataset in page
                if dataset["dataset_id"] in public
            )
            offset += len(page)
            if not page or offset >= body["pagination"]["total"]:
                return entries

    def _list_uploads(self, dataset_id: str) -> list[dict[str, Any]]:
        query = {"datasets.dataset_id": dataset_id}
        buckets = self._aggregate("upload_id", query, include=["upload_name"])
        if not buckets:
            raise FileNotFoundError(f"/{dataset_id}")
        uploads = [
            _directory(
                f"/{dataset_id}/{bucket['value']}",
                (bucket.get("entries") or [{}])[0].get("upload_name"),
            )
            for bucket in buckets
        ]
        return sorted(
            uploads, key=lambda upload: (upload["display_name"].casefold(), upload["name"])
        )

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
            after = result["pagination"]["next_page_after_value"]


def _split(path: str) -> list[str]:
    parts = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid NOMAD path: {path}")
    return parts


def _directory(path: str, display_name: str | None) -> dict[str, Any]:
    name = display_name or path.rsplit("/", 1)[-1]
    return {"name": path, "size": 0, "type": "directory", "display_name": name}
