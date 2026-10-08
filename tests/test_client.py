import pytest
import requests
import responses
from responses import registries

from nomad_fsspec.client import NomadClient
from nomad_fsspec.errors import NomadError

API = "https://nomad.example.org/oasis/api/v1"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://nomad.example.org/oasis",
        "https://nomad.example.org/oasis/",
        "https://nomad.example.org/oasis/api/v1",
        " https://nomad.example.org/oasis/api/v1/ ",
    ],
)
def test_base_url_spellings_reach_one_api_root(base_url):
    client = NomadClient(base_url)
    assert client.api_url == API
    assert client.host == "nomad.example.org"


def test_path_segments_are_encoded_one_at_a_time():
    url = NomadClient(API).url(["uploads", "u1", "raw", "run #1 100%.csv", ""])
    assert url == f"{API}/uploads/u1/raw/run%20%231%20100%25.csv/"


@responses.activate
def test_errors_are_mapped():
    client = NomadClient(API)
    responses.get(f"{API}/a", status=404, json={"detail": "The specified upload_id was not found."})
    responses.get(f"{API}/b", status=403, json={"detail": "Forbidden"})
    responses.get(f"{API}/c", status=500, body="<html>proxy error</html>")
    responses.get(f"{API}/d", body="<html>the NOMAD GUI</html>")
    responses.get(f"{API}/e", body=requests.ConnectionError("refused"))
    with pytest.raises(FileNotFoundError, match="upload_id was not found"):
        client.get_json(["a"])
    with pytest.raises(PermissionError):
        client.get_json(["b"])
    with pytest.raises(NomadError, match="HTTP 500"):
        client.get_json(["c"])
    with pytest.raises(NomadError, match="did not answer like a NOMAD API"):
        client.get_json(["d"])
    with pytest.raises(NomadError, match="Could not reach NOMAD"):
        client.get_json(["e"])
    assert not issubclass(NomadError, OSError)


@responses.activate(registry=registries.OrderedRegistry)
def test_rate_limited_and_unavailable_answers_are_retried():
    responses.get(f"{API}/x", status=503)
    responses.get(f"{API}/x", status=429, headers={"Retry-After": "0"})
    responses.get(f"{API}/x", json={"ok": True})
    assert NomadClient(API).get_json(["x"]) == {"ok": True}
