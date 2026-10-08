import pytest
import requests

from nomad_fsspec import NomadFileSystem

pytestmark = pytest.mark.live

DEMO_DATASET = "/wWgAnNZNQxOHLf97H62dgw"
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


def test_root_lists_public_datasets_by_name(fs):
    root = fs.ls("/")
    assert len(root) > 1000
    assert any(entry["display_name"] == "demo example data" for entry in root)


def test_demo_file_downloads_and_reads_in_parts(fs, tmp_path):
    uploads = fs.ls(DEMO_DATASET)
    assert [upload["display_name"] for upload in uploads] == ["nomad-demo-data.zip"]
    assert fs.info(DEMO_FILE)["size"] == 424235
    target = tmp_path / "vasprun.xml"
    fs.get_file(DEMO_FILE, str(target))
    data = target.read_bytes()
    assert len(data) == 424235
    assert fs.cat_file(DEMO_FILE, start=100, end=150) == data[100:150]
