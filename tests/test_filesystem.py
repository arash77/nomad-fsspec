import pytest
import responses
from responses import matchers, registries

from nomad_fsspec import NomadError, NomadFileSystem, filesystem

BASE = "https://nomad.example.org/oasis"
API = f"{BASE}/api/v1"


def make_fs() -> NomadFileSystem:
    return NomadFileSystem(base_url=BASE, skip_instance_cache=True)


def aggregation_body(quantity, query, page_size, after=None, include=None):
    pagination = {"page_size": page_size}
    if after is not None:
        pagination["page_after_value"] = after
    terms = {"quantity": quantity, "pagination": pagination}
    if include:
        terms["entries"] = {"size": 1, "required": {"include": include}}
    return {
        "owner": "public",
        "query": query,
        "pagination": {"page_size": 0},
        "aggregations": {"buckets": {"terms": terms}},
    }


def aggregation(*values, names=None, after=None):
    data = []
    for value in values:
        bucket = {"value": value, "count": 1}
        if names and value in names:
            bucket["entries"] = [{"upload_name": names[value]}]
        data.append(bucket)
    terms = {"data": data, "pagination": {"next_page_after_value": after}}
    return {"aggregations": {"buckets": {"terms": terms}}}


@responses.activate
def test_root_lists_public_datasets_by_name_and_caches():
    # strict matchers matter: NOMAD ignores a parameter it does not know and answers with everything
    responses.post(
        f"{API}/entries/query",
        match=[matchers.json_params_matcher(aggregation_body("datasets.dataset_id", {}, 2500))],
        json=aggregation("ds1", "ds2"),
    )
    responses.get(
        f"{API}/datasets/",
        match=[
            matchers.query_param_matcher(
                {
                    "page_size": "10000",
                    "page_offset": "0",
                    "order_by": "dataset_name",
                    "order": "asc",
                }
            )
        ],
        json={
            "pagination": {"total": 3},
            "data": [
                {"dataset_id": "ds1", "dataset_name": "demo data"},
                {"dataset_id": "ds2"},
                {"dataset_id": "ds3", "dataset_name": "no public entries"},
            ],
        },
    )
    fs = make_fs()
    entries = fs.ls("nomad://")
    assert [(e["name"], e["display_name"], e["type"]) for e in entries] == [
        ("/ds1", "demo data", "directory"),
        ("/ds2", "ds2", "directory"),
    ]
    assert fs.ls("/", detail=False) == ["/ds1", "/ds2"]
    assert len(responses.calls) == 2


@responses.activate(registry=registries.OrderedRegistry)
def test_a_dataset_lists_its_uploads_across_pages(monkeypatch):
    monkeypatch.setattr(filesystem, "AGGREGATION_PAGE_SIZE", 2)
    query = {"datasets.dataset_id": "ds1"}
    include = ["upload_name"]
    responses.post(
        f"{API}/entries/query",
        match=[
            matchers.json_params_matcher(aggregation_body("upload_id", query, 2, include=include))
        ],
        json=aggregation("up1", "up2", names={"up1": "b.zip", "up2": "a.zip"}, after="up2"),
    )
    responses.post(
        f"{API}/entries/query",
        match=[
            matchers.json_params_matcher(
                aggregation_body("upload_id", query, 2, after="up2", include=include)
            )
        ],
        json=aggregation("up3", after="up3"),
    )
    entries = make_fs().ls("/ds1")
    assert [(e["name"], e["display_name"]) for e in entries] == [
        ("/ds1/up2", "a.zip"),
        ("/ds1/up1", "b.zip"),
        ("/ds1/up3", "up3"),
    ]


@responses.activate
def test_a_dataset_without_public_uploads_is_not_found():
    responses.post(f"{API}/entries/query", json=aggregation())
    with pytest.raises(FileNotFoundError):
        make_fs().ls("/no-such-dataset")


def test_nomad_error_is_exported():
    assert issubclass(NomadError, Exception)


def rawdir(*content, total=None):
    return {
        "directory_metadata": {"content": list(content)},
        "pagination": {"total": len(content) if total is None else total},
    }


def page(size, offset):
    return matchers.query_param_matcher({"page_size": str(size), "page_offset": str(offset)})


@responses.activate
def test_upload_directory_pages_until_the_total(monkeypatch):
    monkeypatch.setattr(filesystem, "PAGE_SIZE", 2)
    url = f"{API}/uploads/up1/rawdir/data/"
    first = rawdir(
        {"name": "a.txt", "size": 5, "is_file": True},
        {"name": "sub", "size": 9, "is_file": False},
        total=4,
    )
    second = rawdir({"name": "", "is_file": False}, {"name": "z.txt", "is_file": True}, total=4)
    responses.get(url, match=[page(2, 0)], json=first)
    responses.get(url, match=[page(2, 2)], json=second)
    fs = make_fs()
    entries = fs.ls("/ds1/up1/data")
    assert [(e["name"], e["type"], e["size"]) for e in entries] == [
        ("/ds1/up1/data/a.txt", "file", 5),
        ("/ds1/up1/data/sub", "directory", 9),
        ("/ds1/up1/data/z.txt", "file", 0),
    ]
    assert fs.info("/ds1/up1/data/a.txt")["size"] == 5
    assert len(responses.calls) == 2


@responses.activate
def test_an_empty_page_ends_the_listing(monkeypatch):
    monkeypatch.setattr(filesystem, "PAGE_SIZE", 1)
    url = f"{API}/uploads/up1/rawdir/"
    responses.get(url, match=[page(1, 0)], json=rawdir({"name": "a", "is_file": True}, total=5))
    responses.get(url, match=[page(1, 1)], json=rawdir(total=5))
    assert [e["name"] for e in make_fs().ls("/ds1/up1")] == ["/ds1/up1/a"]


@responses.activate
def test_listing_a_file_returns_the_file():
    responses.get(
        f"{API}/uploads/up1/rawdir/a.txt/", json={"file_metadata": {"name": "", "size": 7}}
    )
    assert make_fs().ls("/ds1/up1/a.txt") == [{"name": "/ds1/up1/a.txt", "size": 7, "type": "file"}]


@responses.activate
def test_dot_segments_are_refused():
    with pytest.raises(ValueError):
        make_fs().ls("/ds1/up1/../up2")


@responses.activate
def test_a_failing_folder_is_not_hidden_by_walk():
    responses.get(f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "sub", "is_file": False}))
    responses.get(f"{API}/uploads/up1/rawdir/sub/", status=500, json={"detail": "boom"})
    with pytest.raises(NomadError, match="boom"):
        list(make_fs().walk("/ds1/up1"))


@responses.activate
def test_a_folder_lists_its_contents_after_its_parent_was_listed():
    responses.get(f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "sub", "is_file": False}))
    responses.get(f"{API}/uploads/up1/rawdir/sub/", json=rawdir({"name": "b.txt", "is_file": True}))
    fs = make_fs()
    fs.ls("/ds1/up1")
    assert fs.ls("/ds1/up1/sub", detail=False) == ["/ds1/up1/sub/b.txt"]


def raw_params(**extra):
    return matchers.query_param_matcher({"ignore_mime_type": "true", **extra})


@responses.activate
def test_get_file_streams_one_request(tmp_path):
    responses.get(
        f"{API}/uploads/up1/raw/data/run%20%231%20100%25.csv",
        match=[raw_params()],
        body=b"a,b\n1,2\n",
    )
    target = tmp_path / "out.csv"
    make_fs().get_file("/ds1/up1/data/run #1 100%.csv", str(target))
    assert target.read_bytes() == b"a,b\n1,2\n"
    assert len(responses.calls) == 1


@responses.activate
def test_get_file_refuses_a_dataset_or_an_upload(tmp_path):
    with pytest.raises(IsADirectoryError):
        make_fs().get_file("/ds1/up1", str(tmp_path / "x"))


@responses.activate
def test_partial_reads_use_offset_and_length():
    url = f"{API}/uploads/up1/raw/a.bin"
    responses.get(
        f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "a.bin", "size": 10, "is_file": True})
    )
    responses.get(url, match=[raw_params(offset="2", length="3")], body=b"cde")
    responses.get(url, match=[raw_params(length="2")], body=b"ab")
    responses.get(url, match=[raw_params(offset="8")], body=b"ij")
    responses.get(url, match=[raw_params(length="10")], body=b"abcdefghij")
    fs = make_fs()
    assert fs.cat_file("/ds1/up1/a.bin", start=2, end=5) == b"cde"
    assert fs.cat_file("/ds1/up1/a.bin", end=2) == b"ab"
    assert fs.cat_file("/ds1/up1/a.bin", start=3, end=3) == b""
    assert fs.cat_file("/ds1/up1/a.bin", start=-2) == b"ij"
    with fs.open("/ds1/up1/a.bin", "rb") as handle:
        assert handle.read() == b"abcdefghij"


@responses.activate
def test_writing_is_refused():
    with pytest.raises(PermissionError):
        make_fs().open("/ds1/up1/new.txt", "wb")
