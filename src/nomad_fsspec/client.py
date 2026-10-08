from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import NomadError

DEFAULT_BASE_URL = "https://nomad-lab.eu/prod/v1"
API_PATH = "/api/v1"


class NomadClient:
    """A small client for NOMAD's REST API."""

    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, timeout: tuple[float, float] = (10, 120)
    ) -> None:
        base = base_url.strip().rstrip("/")
        if base.endswith(API_PATH):
            base = base[: -len(API_PATH)]
        self.api_url = base + API_PATH
        self.host = urlparse(base).netloc or base
        self.timeout = timeout
        # NOMAD refuses bursts with 429 or 503, usually with Retry-After
        retry = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 503],
            allowed_methods={"GET", "POST"},
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def url(self, parts: list[str]) -> str:
        return "/".join([self.api_url, *(quote(part, safe="") for part in parts)])

    def get_json(self, parts: list[str], params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.request("GET", parts, params=params) as response:
            return self._json(response)

    def post_json(self, parts: list[str], body: dict[str, Any]) -> dict[str, Any]:
        with self.request("POST", parts, json=body) as response:
            return self._json(response)

    @contextmanager
    def request(self, method: str, parts: list[str], **kwargs: Any) -> Iterator[requests.Response]:
        label = "/" + "/".join(parts)
        try:
            with self.session.request(
                method, self.url(parts), timeout=self.timeout, **kwargs
            ) as response:
                self._raise_for_status(response, label)
                yield response
        except requests.RequestException as e:
            raise NomadError(f"Could not reach NOMAD at {self.host}: {e}") from e

    def _raise_for_status(self, response: requests.Response, label: str) -> None:
        if response.ok:
            return
        detail = _detail(response)
        if response.status_code == 404:
            raise FileNotFoundError(f"{label}: {detail}")
        if response.status_code in (401, 403):
            raise PermissionError(f"NOMAD at {self.host} refused access to {label}: {detail}")
        raise NomadError(
            f"NOMAD at {self.host} answered HTTP {response.status_code} for {label}: {detail}"
        )

    def _json(self, response: requests.Response) -> dict[str, Any]:
        message = f"{self.host} did not answer like a NOMAD API; check the base URL"
        try:
            body = response.json()
        except ValueError as e:
            raise NomadError(message) from e
        if not isinstance(body, dict):
            raise NomadError(message)
        return body


def _detail(response: requests.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str):
        return detail
    return response.reason or f"HTTP {response.status_code}"
