import pytest
import requests
import responses
from responses import registries
from urllib3.response import HTTPResponse

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
    with pytest.raises(NomadError, match="Request to NOMAD at nomad.example.org failed"):
        client.get_json(["e"])
    assert not issubclass(NomadError, OSError)


@responses.activate(registry=registries.OrderedRegistry)
def test_rate_limited_and_unavailable_answers_are_retried():
    responses.get(f"{API}/x", status=503)
    responses.get(f"{API}/x", status=429, headers={"Retry-After": "0"})
    responses.get(f"{API}/x", json={"ok": True})
    assert NomadClient(API).get_json(["x"]) == {"ok": True}


def test_retry_waits_are_capped_and_odd_retry_after_values_are_tolerated():
    retry = NomadClient(API).session.get_adapter(API).max_retries
    assert retry.get_retry_after(HTTPResponse(headers={"Retry-After": "3600"})) == 10
    assert retry.get_retry_after(HTTPResponse(headers={"Retry-After": "1.5"})) is None


def test_a_slow_answer_is_not_requested_again():
    retry = NomadClient(API).session.get_adapter(API).max_retries
    assert retry.read == 0


def test_credentials_in_the_base_url_stay_out_of_messages():
    assert NomadClient("https://alice:s3cret@oasis.example.org:8443/nomad").host == (
        "oasis.example.org:8443"
    )


@responses.activate
def test_downloading_a_folder_is_an_is_a_directory_error():
    detail = "Path is a directory, `compress` must be set to true"
    responses.get(f"{API}/uploads/u1/raw/data", status=400, json={"detail": detail})
    with pytest.raises(IsADirectoryError):
        NomadClient(API).get_json(["uploads", "u1", "raw", "data"])
