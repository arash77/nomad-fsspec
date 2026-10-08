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
