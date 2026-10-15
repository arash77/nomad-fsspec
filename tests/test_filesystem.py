import io
import time

import fsspec.callbacks
import pytest
import requests
import responses
from responses import matchers, registries

from nomad_fsspec import NomadError, NomadFileSystem, filesystem

BASE = "https://nomad.example.org/oasis"
API = f"{BASE}/api/v1"


@pytest.fixture(autouse=True)
def list_folder_by_folder(monkeypatch):
    # these tests are about NOMAD's own folder listing; test_zip.py covers listing from the ZIP
    monkeypatch.setattr(NomadFileSystem, "_zip_tree", lambda self, upload_id: None)


def make_fs() -> NomadFileSystem:
    return NomadFileSystem(base_url=BASE, skip_instance_cache=True)


def aggregation_body(quantity, query, page_size, after=None):
    pagination = {"page_size": page_size}
    if after is not None:
        pagination["page_after_value"] = after
    terms = {"quantity": quantity, "pagination": pagination}
    return {
        "owner": "public",
        "query": query,
        "pagination": {"page_size": 0},
        "aggregations": {"buckets": {"terms": terms}},
    }


def aggregation(*values, after=None):
    data = [{"value": value, "count": 1} for value in values]
    terms = {"data": data, "pagination": {"next_page_after_value": after}}
    return {"aggregations": {"buckets": {"terms": terms}}}


@responses.activate
def test_the_top_level_holds_public_datasets_and_my_uploads():
    entries = make_fs().ls("nomad://")
    assert [(e["name"], e["display_name"], e["type"]) for e in entries] == [
        ("/datasets", "Public datasets", "directory"),
        ("/uploads", "My uploads", "directory"),
    ]
    assert len(responses.calls) == 0


@responses.activate
def test_public_datasets_are_listed_by_name_and_cached():
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
    entries = fs.ls("nomad://datasets")
    assert [(e["name"], e["display_name"], e["type"]) for e in entries] == [
        ("/datasets/ds1", "demo data", "directory"),
        ("/datasets/ds2", "ds2", "directory"),
    ]
    assert fs.ls("/datasets/", detail=False) == ["/datasets/ds1", "/datasets/ds2"]
    assert len(responses.calls) == 2


@responses.activate
def test_instances_share_the_public_datasets_unless_listings_are_not_kept():
    responses.post(f"{API}/entries/query", json=aggregation("ds1"))
    responses.get(
        f"{API}/datasets/", json={"pagination": {"total": 1}, "data": [{"dataset_id": "ds1"}]}
    )
    for _ in range(2):
        assert make_fs().ls("/datasets", detail=False) == ["/datasets/ds1"]
    assert len(responses.calls) == 2
    uncached = NomadFileSystem(base_url=BASE, use_listings_cache=False, skip_instance_cache=True)
    uncached.ls("/datasets")
    make_fs().invalidate_cache()
    make_fs().ls("/datasets")
    assert len(responses.calls) == 6


@responses.activate
def test_instances_share_the_uploads_of_a_dataset_whatever_the_token():
    responses.post(f"{API}/entries/query", json=aggregation("up1"))
    for token in (None, "my-token"):
        fs = NomadFileSystem(base_url=BASE, token=token, skip_instance_cache=True)
        assert fs.ls("/datasets/ds1", detail=False) == ["/datasets/ds1/up1"]
    assert len(responses.calls) == 1


@responses.activate
def test_instances_share_only_what_they_learn_about_public_uploads():
    public = {"upload_name": "a.zip", "published": True, "with_embargo": False, "entries": 3}
    responses.get(f"{API}/uploads/up1", json={"data": public})
    draft = {**public, "upload_name": "draft", "published": False}
    responses.get(f"{API}/uploads/up2", json={"data": draft})
    embargoed = {**public, "upload_name": "embargoed", "with_embargo": True}
    responses.get(f"{API}/uploads/up3", json={"data": embargoed})
    mine = NomadFileSystem(base_url=BASE, token="my-token", skip_instance_cache=True)
    names = {"up1": "a.zip", "up2": "draft", "up3": "embargoed"}
    assert mine.upload_names(["up1", "up2", "up3"]) == names
    other = make_fs()
    assert other.upload_names(["up1"]) == {"up1": "a.zip"}
    assert other._upload_facts("up1") == (True, 3)
    assert len(responses.calls) == 3
    other.upload_names(["up2", "up3"])
    assert len(responses.calls) == 5


@responses.activate(registry=registries.OrderedRegistry)
def test_a_dataset_lists_its_uploads_by_id_across_pages(monkeypatch):
    monkeypatch.setattr(filesystem, "AGGREGATION_PAGE_SIZE", 2)
    query = {"datasets.dataset_id": "ds1"}
    responses.post(
        f"{API}/entries/query",
        match=[matchers.json_params_matcher(aggregation_body("upload_id", query, 2))],
        json=aggregation("up3", "up1", after="up1"),
    )
    responses.post(
        f"{API}/entries/query",
        match=[matchers.json_params_matcher(aggregation_body("upload_id", query, 2, after="up1"))],
        json=aggregation("up2", after="up2"),
    )
    entries = make_fs().ls("/datasets/ds1")
    assert [(e["name"], e["display_name"]) for e in entries] == [
        ("/datasets/ds1/up1", "up1"),
        ("/datasets/ds1/up2", "up2"),
        ("/datasets/ds1/up3", "up3"),
    ]


@responses.activate
def test_upload_names_are_asked_for_one_by_one_and_kept(monkeypatch):
    responses.get(f"{API}/uploads/up1", json={"data": {"upload_name": "a.zip"}})
    responses.get(f"{API}/uploads/up2", json={"data": {}})
    responses.get(f"{API}/uploads/up3", status=500, json={"detail": "boom"})
    fs = make_fs()
    assert fs.upload_names(["up1", "up2", "up3"]) == {"up1": "a.zip"}

    def asked(upload):
        return sum(call.request.url == f"{API}/uploads/{upload}" for call in responses.calls)

    # an upload without a name is kept as such; one that failed is asked about again
    assert fs.upload_names(["up3", "up2", "up1"]) == {"up1": "a.zip"}
    assert (asked("up1"), asked("up2")) == (1, 1) and asked("up3") > 1
    # past the limit, the kept names are dropped and asked for again
    monkeypatch.setattr(filesystem, "MAX_NAMES", 1)
    assert fs.upload_names(["up1"]) == {"up1": "a.zip"}
    assert asked("up1") == 2


@responses.activate(registry=registries.OrderedRegistry)
def test_search_matches_names_before_asking_for_uploads():
    dataset = {"datasets.dataset_id": "ds1"}
    responses.post(
        f"{API}/entries/query",
        match=[matchers.json_params_matcher(aggregation_body("upload_name", dataset, 2500))],
        json=aggregation("Alexandria upload", "other"),
    )
    responses.post(
        f"{API}/entries/query",
        match=[
            matchers.json_params_matcher(
                aggregation_body(
                    "upload_id", {**dataset, "upload_name:any": ["Alexandria upload"]}, 2500
                )
            )
        ],
        json=aggregation("up1", "up2"),
    )
    fs = make_fs()
    assert fs.search_uploads("ds1", "ALEXANDRIA") == ["up1", "up2"]
    assert fs.upload_names(["up1"]) == {"up1": "Alexandria upload"}
    assert len(responses.calls) == 2


@responses.activate
def test_a_token_lists_the_uploads_of_its_user_and_reads_them():
    auth = matchers.header_matcher({"Authorization": "Bearer my-token"})
    responses.get(
        f"{API}/uploads",
        match=[auth, page(10000, 0)],
        json={
            "pagination": {"total": 2},
            "data": [
                {"upload_id": "up2", "upload_name": "beta"},
                {"upload_id": "up1", "upload_name": "Alpha"},
            ],
        },
    )
    responses.get(
        f"{API}/uploads/up1/rawdir/",
        match=[auth],
        json=rawdir({"name": "a.txt", "size": 3, "is_file": True}),
    )
    responses.get(f"{API}/uploads/up1/raw/a.txt", match=[auth], body=b"abc")
    fs = NomadFileSystem(base_url=BASE, token="my-token\n", skip_instance_cache=True)
    assert [(e["name"], e["display_name"]) for e in fs.ls("/uploads")] == [
        ("/uploads/up1", "Alpha"),
        ("/uploads/up2", "beta"),
    ]
    assert fs.ls("/uploads/up1", detail=False) == ["/uploads/up1/a.txt"]
    assert fs.cat_file("/uploads/up1/a.txt") == b"abc"
    with pytest.raises(IsADirectoryError):
        fs.cat_file("/uploads/up1")


@responses.activate
def test_without_a_token_my_uploads_are_refused_but_a_published_upload_reads():
    responses.get(f"{API}/uploads/up1/raw/a.txt", body=b"abc")
    fs = make_fs()
    with pytest.raises(PermissionError, match="token"):
        fs.ls("/uploads")
    assert fs.cat_file("/uploads/up1/a.txt") == b"abc"
    assert len(responses.calls) == 1


@responses.activate
def test_a_dataset_without_public_uploads_or_another_top_folder_is_not_found():
    responses.post(f"{API}/entries/query", json=aggregation())
    for path in ("/datasets/no-such-dataset", "/elsewhere", "/elsewhere/up1/a.txt"):
        with pytest.raises(FileNotFoundError):
            make_fs().ls(path)


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
    entries = fs.ls("/datasets/ds1/up1/data")
    assert [(e["name"], e["type"], e["size"]) for e in entries] == [
        ("/datasets/ds1/up1/data/a.txt", "file", 5),
        ("/datasets/ds1/up1/data/sub", "directory", 0),
        ("/datasets/ds1/up1/data/z.txt", "file", 0),
    ]
    assert fs.info("/datasets/ds1/up1/data/a.txt")["size"] == 5
    assert len(responses.calls) == 2


@responses.activate
def test_an_empty_page_ends_the_listing(monkeypatch):
    monkeypatch.setattr(filesystem, "PAGE_SIZE", 1)
    url = f"{API}/uploads/up1/rawdir/"
    responses.get(url, match=[page(1, 0)], json=rawdir({"name": "a", "is_file": True}, total=5))
    responses.get(url, match=[page(1, 1)], json=rawdir(total=5))
    assert [e["name"] for e in make_fs().ls("/datasets/ds1/up1")] == ["/datasets/ds1/up1/a"]


@responses.activate
def test_listing_a_file_returns_the_file():
    responses.get(
        f"{API}/uploads/up1/rawdir/a.txt/", json={"file_metadata": {"name": "", "size": 7}}
    )
    assert make_fs().ls("/datasets/ds1/up1/a.txt") == [
        {"name": "/datasets/ds1/up1/a.txt", "size": 7, "type": "file"}
    ]


@responses.activate
def test_dot_segments_are_refused():
    with pytest.raises(ValueError):
        make_fs().ls("/datasets/ds1/up1/../up2")


@responses.activate
def test_a_failing_folder_is_not_hidden_by_walk():
    responses.get(f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "sub", "is_file": False}))
    responses.get(f"{API}/uploads/up1/rawdir/sub/", status=500, json={"detail": "boom"})
    with pytest.raises(NomadError, match="boom"):
        list(make_fs().walk("/datasets/ds1/up1"))


@responses.activate
def test_a_folder_lists_its_contents_after_its_parent_was_listed():
    responses.get(f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "sub", "is_file": False}))
    responses.get(f"{API}/uploads/up1/rawdir/sub/", json=rawdir({"name": "b.txt", "is_file": True}))
    fs = make_fs()
    fs.ls("/datasets/ds1/up1")
    assert fs.ls("/datasets/ds1/up1/sub", detail=False) == ["/datasets/ds1/up1/sub/b.txt"]


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
    make_fs().get_file("/datasets/ds1/up1/data/run #1 100%.csv", str(target))
    assert target.read_bytes() == b"a,b\n1,2\n"
    assert len(responses.calls) == 1


@responses.activate
def test_get_file_of_a_dataset_or_an_upload_makes_a_local_folder(tmp_path):
    make_fs().get_file("/datasets/ds1/up1", str(tmp_path / "x"))
    assert (tmp_path / "x").is_dir()
    assert len(responses.calls) == 0


@responses.activate
def test_get_file_of_a_folder_onto_a_local_file_is_refused(tmp_path):
    target = tmp_path / "x"
    target.write_bytes(b"")
    with pytest.raises(IsADirectoryError, match="/datasets/ds1/up1"):
        make_fs().get_file("/datasets/ds1/up1", str(target))
    assert len(responses.calls) == 0


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
    assert fs.cat_file("/datasets/ds1/up1/a.bin", start=2, end=5) == b"cde"
    assert fs.cat_file("/datasets/ds1/up1/a.bin", end=2) == b"ab"
    assert fs.cat_file("/datasets/ds1/up1/a.bin", start=3, end=3) == b""
    assert fs.cat_file("/datasets/ds1/up1/a.bin", start=-2) == b"ij"
    with fs.open("/datasets/ds1/up1/a.bin", "rb") as handle:
        assert handle.read() == b"abcdefghij"


@responses.activate
def test_writing_is_refused():
    with pytest.raises(PermissionError):
        make_fs().open("/datasets/ds1/up1/new.txt", "wb")


@responses.activate(registry=registries.OrderedRegistry)
def test_a_full_aggregation_page_without_a_cursor_ends_the_listing(monkeypatch):
    monkeypatch.setattr(filesystem, "AGGREGATION_PAGE_SIZE", 2)
    responses.post(f"{API}/entries/query", json=aggregation("up1", "up2"))
    assert make_fs().ls("/datasets/ds1", detail=False) == ["/datasets/ds1/up1", "/datasets/ds1/up2"]


@responses.activate
def test_doubled_slashes_name_the_same_entry():
    responses.get(
        f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "f.txt", "size": 3, "is_file": True})
    )
    info = make_fs().info("/datasets/ds1/up1//f.txt")
    assert (info["name"], info["type"], info["size"]) == ("/datasets/ds1/up1/f.txt", "file", 3)


@responses.activate
def test_refresh_and_invalidate_cache_fetch_again():
    responses.get(f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "a", "is_file": True}))
    fs = make_fs()
    fs.ls("/datasets/ds1/up1")
    fs.ls("/datasets/ds1/up1", refresh=True)
    fs.invalidate_cache("/datasets/ds1/up1")
    fs.ls("/datasets/ds1/up1")
    fs.invalidate_cache()
    fs.ls("/datasets/ds1/up1")
    assert len(responses.calls) == 4


@responses.activate
def test_listings_past_max_paths_or_expired_are_dropped(monkeypatch):
    listing = rawdir({"name": "a", "is_file": True})
    for upload in ("up1", "up2", "up3"):
        responses.get(f"{API}/uploads/{upload}/rawdir/", json=listing)
    fs = NomadFileSystem(
        base_url=BASE, max_paths=2, listings_expiry_time=60, skip_instance_cache=True
    )
    for upload in ("up1", "up2", "up3"):
        fs.ls(f"/datasets/ds1/{upload}")
    assert list(fs.dircache) == ["/datasets/ds1/up2", "/datasets/ds1/up3"]
    later = time.monotonic() + 61
    monkeypatch.setattr(time, "monotonic", lambda: later)
    fs.ls("/datasets/ds1/up1")
    assert list(fs.dircache) == ["/datasets/ds1/up1"]


@responses.activate
def test_opening_a_folder_is_refused():
    responses.get(f"{API}/uploads/up1/rawdir/", json=rawdir({"name": "sub", "is_file": False}))
    fs = make_fs()
    for path in ("/datasets/ds1", "/datasets/ds1/up1/sub"):
        with pytest.raises(IsADirectoryError):
            fs.open(path).read()


@responses.activate
def test_get_copies_a_folder_tree(tmp_path):
    responses.post(f"{API}/entries/query", json=aggregation("up1"))
    responses.get(
        f"{API}/uploads/up1/rawdir/",
        json=rawdir(
            {"name": "a.txt", "size": 1, "is_file": True}, {"name": "sub", "is_file": False}
        ),
    )
    responses.get(
        f"{API}/uploads/up1/rawdir/sub/", json=rawdir({"name": "b.txt", "size": 1, "is_file": True})
    )
    responses.get(f"{API}/uploads/up1/raw/a.txt", body=b"a")
    responses.get(f"{API}/uploads/up1/raw/sub/b.txt", body=b"b")
    make_fs().get("/datasets/ds1/up1", str(tmp_path / "out"), recursive=True)
    copied = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert copied == ["out/a.txt", "out/sub/b.txt"]


@responses.activate
def test_get_file_writes_to_a_file_object_and_reports_progress():
    responses.get(f"{API}/uploads/up1/raw/a.txt", body=b"abc")
    target = io.BytesIO()
    callback = fsspec.callbacks.Callback()
    make_fs().get_file("/datasets/ds1/up1/a.txt", target, callback=callback)
    assert (target.getvalue(), callback.value) == (b"abc", 3)


@responses.activate
def test_a_failed_download_leaves_no_partial_file(tmp_path, monkeypatch):
    responses.get(f"{API}/uploads/up1/raw/a.txt", body=b"abc")

    def cut(chunks, target, callback):
        target.write(b"a")
        raise requests.exceptions.ChunkedEncodingError("cut")

    monkeypatch.setattr(filesystem, "_copy", cut)
    target = tmp_path / "a.txt"
    with pytest.raises(NomadError):
        make_fs().get_file("/datasets/ds1/up1/a.txt", str(target))
    assert not target.exists()


@responses.activate
def test_a_missing_path_is_reported_by_its_own_name(tmp_path):
    missing = {"status": 404, "json": {"detail": "Not found. Invalid path?"}}
    responses.get(f"{API}/uploads/up1/rawdir/gone/", **missing)
    responses.get(f"{API}/uploads/up1/raw/gone.txt", **missing)
    fs = make_fs()
    for call, path in (
        (fs.ls, "/datasets/ds1/up1/gone"),
        (fs.cat_file, "/datasets/ds1/up1/gone.txt"),
    ):
        with pytest.raises(FileNotFoundError) as error:
            call(path)
        assert path in str(error.value) and "rawdir" not in str(error.value)


@responses.activate
def test_datasets_are_sorted_by_name_ignoring_case():
    responses.post(f"{API}/entries/query", json=aggregation("ds1", "ds2", "ds3"))
    names = [("ds1", "Beta"), ("ds2", "Zeta"), ("ds3", "alpha")]
    data = [{"dataset_id": i, "dataset_name": name} for i, name in names]
    responses.get(f"{API}/datasets/", json={"pagination": {"total": 3}, "data": data})
    assert [e["display_name"] for e in make_fs().ls("/datasets")] == ["alpha", "Beta", "Zeta"]
