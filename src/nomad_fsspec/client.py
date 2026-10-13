from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader
from urllib3.util.retry import Retry

from .errors import NomadError

DEFAULT_BASE_URL = "https://nomad-lab.eu/prod/v1"
API_PATH = "/api/v1"
MAX_RETRY_AFTER = 10


class RangeUnavailable(Exception):
    """The byte range could not be read; ``lasting`` when asking again would not help."""

    def __init__(self, message: str, lasting: bool) -> None:
        super().__init__(message)
        self.lasting = lasting


class _Retry(Retry):
    """Honour NOMAD's Retry-After, but never wait long or fail on an odd value."""

    def get_retry_after(self, response: Any) -> float | None:
        try:
            seconds = super().get_retry_after(response)
        except InvalidHeader:
            return None
        return None if seconds is None else min(seconds, MAX_RETRY_AFTER)


class NomadClient:
    """A small client for NOMAD's REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: tuple[float, float] = (10, 120),
        token: str | None = None,
    ) -> None:
        base = base_url.strip().rstrip("/")
        if base.endswith(API_PATH):
            base = base[: -len(API_PATH)]
        self.api_url = base + API_PATH
        parsed = urlparse(base)
        self.host = (parsed.hostname or base) + (f":{parsed.port}" if parsed.port else "")
        self.timeout = timeout
        # NOMAD refuses bursts with 429 or 503; a slow answer is not asked for again
        retry = _Retry(
            total=4,
            connect=1,
            read=0,
            status=3,
            backoff_factor=0.5,
            status_forcelist=[429, 503],
            allowed_methods={"GET", "POST"},
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def url(self, parts: list[str]) -> str:
        return "/".join([self.api_url, *(quote(part, safe="") for part in parts)])

    def get_json(self, parts: list[str], params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.request("GET", parts, params=params) as response:
            return self._json(response)

    def post_json(self, parts: list[str], body: dict[str, Any]) -> dict[str, Any]:
        with self.request("POST", parts, json=body) as response:
            return self._json(response)

    def read_range(self, parts: list[str], byte_range: str) -> tuple[bytes, int]:
        """Read an HTTP byte range without the token; return the bytes and the full size.

        Without the token the answer is the same for everyone, so callers may share it.
        """
        try:
            with self.session.get(
                self.url(parts),
                # None drops the session's Authorization header for this request
                headers={"Range": byte_range, "Authorization": None},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            ) as response:
                total = response.headers.get("Content-Range", "").rpartition("/")[2]
                if response.status_code == 206 and total.isdigit():
                    return response.content, int(total)
                # anything else is left unread: a server that ignores the range sends everything
                busy = response.status_code == 429 or response.status_code >= 500
                raise RangeUnavailable(f"HTTP {response.status_code}", lasting=not busy)
        except requests.RequestException as e:
            raise RangeUnavailable(str(e), lasting=False) from e

    @contextmanager
    def request(self, method: str, parts: list[str], **kwargs: Any) -> Iterator[requests.Response]:
        label = "/" + "/".join(parts)
        try:
            # NOMAD's API does not redirect, and a redirect could lead to an address nobody checked
            with self.session.request(
                method, self.url(parts), timeout=self.timeout, allow_redirects=False, **kwargs
            ) as response:
                self._raise_for_status(response, label)
                yield response
        except requests.RequestException as e:
            raise NomadError(f"Request to NOMAD at {self.host} failed: {e}") from e

    def _raise_for_status(self, response: requests.Response, label: str) -> None:
        if response.is_redirect:
            location = response.headers.get("Location")
            raise NomadError(
                f"NOMAD at {self.host} redirected {label} to {location}; "
                "use the address it redirects to as the base URL"
            )
        if response.ok:
            return
        detail = _detail(response)
        if response.status_code == 404:
            raise FileNotFoundError(f"{label}: {detail}")
        if response.status_code in (401, 403):
            raise PermissionError(f"NOMAD at {self.host} refused access to {label}: {detail}")
        if response.status_code == 400 and "is a directory" in detail.lower():
            raise IsADirectoryError(label)
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
        # callers add their own full stop
        return detail.rstrip(".")
    return response.reason or f"HTTP {response.status_code}"
