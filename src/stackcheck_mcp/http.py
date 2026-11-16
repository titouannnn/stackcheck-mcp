"""Shared async HTTP client: bounded concurrency, retries, proxy/CA settings from the env.

``httpx2`` is the HTTP stack the MCP SDK itself depends on (same API as ``httpx``).
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2

from stackcheck_mcp import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"stackcheck-mcp/{__version__}"
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 10.0  # a longer wait than this would outlast the tool call: give up


def retry_after(value: str | None) -> float | None:
    """Seconds asked by a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


async def _sleep(seconds: float) -> None:  # patched by tests
    await asyncio.sleep(seconds)


class FetchError(Exception):
    """The resource could not be fetched (network failure, server error, bad payload)."""


class NotFoundError(FetchError):
    """The registry answered 404/410: the package, version or product does not exist."""


class HttpClient:
    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        concurrency: int = 8,
        attempts: int = 3,
        backoff: float = 0.3,
        timeout: float = 20.0,
    ) -> None:
        self._client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            trust_env=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._attempts = attempts
        self._backoff = backoff

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        last_error = "unknown error"
        asked = 0.0  # Retry-After of the previous answer
        for attempt in range(self._attempts):
            if attempt:
                await _sleep(max(self._backoff * 2 ** (attempt - 1), asked))
            try:
                async with self._semaphore:
                    response = await self._client.get(url, headers=headers)
            except httpx2.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.debug("GET %s failed (%s), attempt %d", url, last_error, attempt + 1)
                continue
            if response.status_code in (404, 410):
                raise NotFoundError("not found")
            if response.status_code in TRANSIENT_STATUS:
                last_error = f"HTTP {response.status_code}"
                log.debug("GET %s -> %s, attempt %d", url, last_error, attempt + 1)
                asked = retry_after(response.headers.get("retry-after")) or 0.0
                if asked > MAX_RETRY_AFTER:
                    raise FetchError(f"{last_error} (retry after {asked:.0f}s)")
                continue
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code}")
            return response.content
        raise FetchError(last_error)

    async def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        body = await self.get_bytes(url, headers)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise FetchError("invalid JSON response") from exc

    async def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        body = await self.get_bytes(url, headers)
        return body.decode("utf-8", errors="replace")
