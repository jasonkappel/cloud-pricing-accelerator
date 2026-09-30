from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .core import HarvestError


ALLOWED_HOSTS = frozenset(
    {
        "prices.azure.com",
        "pricing.us-east-1.amazonaws.com",
    }
)


class SafeHttpClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        retries: int = 3,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds, connect=15.0),
            headers={"User-Agent": "cloud-pricing-accelerator-harvester/1.0"},
        )
        self._retries = retries

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def validate_url(url: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username
            or parsed.password
        ):
            raise HarvestError(f"URL is outside the approved price-feed allowlist: {url}")

    def get_json(self, url: str) -> dict:
        self.validate_url(url)
        response = self._request("GET", url)
        try:
            payload = json.loads(
                response.content,
                parse_float=Decimal,
                parse_int=int,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise HarvestError(f"Price feed returned invalid JSON: {url}") from exc
        if not isinstance(payload, dict):
            raise HarvestError(f"Price feed root must be an object: {url}")
        return payload

    def stream_to_file(self, url: str, destination: Path) -> int:
        self.validate_url(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(self._retries + 1):
            try:
                with self._client.stream("GET", url) as response:
                    if self._should_retry(response) and attempt < self._retries:
                        self._sleep_before_retry(response, attempt)
                        continue
                    self._validate_response(response, url)
                    size = 0
                    with destination.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                            size += len(chunk)
                    return size
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                destination.unlink(missing_ok=True)
                if attempt == self._retries:
                    raise HarvestError(f"Price feed download failed: {url}") from exc
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def _request(self, method: str, url: str) -> httpx.Response:
        for attempt in range(self._retries + 1):
            try:
                response = self._client.request(method, url)
                if self._should_retry(response) and attempt < self._retries:
                    self._sleep_before_retry(response, attempt)
                    continue
                self._validate_response(response, url)
                return response
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == self._retries:
                    raise HarvestError(f"Price feed request failed: {url}") from exc
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _validate_response(response: httpx.Response, url: str) -> None:
        if 300 <= response.status_code < 400:
            raise HarvestError(f"Price feed redirects are not allowed: {url}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HarvestError(
                f"Price feed returned HTTP {response.status_code}: {url}"
            ) from exc

    @staticmethod
    def _should_retry(response: httpx.Response) -> bool:
        return response.status_code == 429 or 500 <= response.status_code < 600

    @staticmethod
    def _sleep_before_retry(response: httpx.Response, attempt: int) -> None:
        retry_after = response.headers.get("Retry-After")
        try:
            delay = min(float(retry_after), 60.0) if retry_after else float(2**attempt)
        except ValueError:
            delay = float(2**attempt)
        time.sleep(max(delay, 0.0))
