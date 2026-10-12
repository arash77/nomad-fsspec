import pytest
import requests

from nomad_fsspec import NomadFileSystem

pytestmark = pytest.mark.live

DEMO_DATASET = "/datasets/wWgAnNZNQxOHLf97H62dgw"
DEMO_FILE = (
    f"{DEMO_DATASET}/Xa2LB__ESiKu-odRBPJhjA/nomad-demo-data/BrK_svSe/TBCC006.ABC/vasprun.xml.relax2"
)


@pytest.fixture(scope="module")
def fs():
    try:
        requests.get("https://nomad-lab.eu/prod/v1/api/v1/info", timeout=20).raise_for_status()
    except requests.RequestException as e:
        pytest.skip(f"nomad-lab.eu is not reachable: {e}")
    return NomadFileSystem(skip_instance_cache=True)


def test_public_datasets_are_listed_by_name(fs):
    datasets = fs.ls("/datasets")
    assert len(datasets) > 1000
    assert any(entry["display_name"] == "demo example data" for entry in datasets)


def test_demo_file_downloads_and_reads_in_parts(fs, tmp_path):
    uploads = fs.ls(DEMO_DATASET, detail=False)
    assert fs.upload_names(path.rsplit("/", 1)[-1] for path in uploads) == {
        "Xa2LB__ESiKu-odRBPJhjA": "nomad-demo-data.zip"
    }
    assert fs.search_uploads(DEMO_DATASET.rsplit("/", 1)[-1], "DEMO-data") == [
        "Xa2LB__ESiKu-odRBPJhjA"
    ]
    assert fs.info(DEMO_FILE)["size"] == 424235
    target = tmp_path / "vasprun.xml"
    fs.get_file(DEMO_FILE, str(target))
    data = target.read_bytes()
    assert len(data) == 424235
    assert fs.cat_file(DEMO_FILE, start=100, end=150) == data[100:150]


def test_my_uploads_reads_a_published_upload_and_refuses_a_bad_token(fs):
    as_upload = DEMO_FILE.replace(DEMO_DATASET, "/uploads")
    assert fs.cat_file(as_upload, start=100, end=150) == fs.cat_file(DEMO_FILE, start=100, end=150)
    bad = NomadFileSystem(token="not-a-token", skip_instance_cache=True)
    with pytest.raises(PermissionError, match="token is invalid"):
        bad.ls("/uploads")
