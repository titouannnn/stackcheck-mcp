"""Shared offline test helpers.

``fetcher`` serves the recorded fixture cache through the real ``Fetcher``/``Cache`` code
path; its HTTP transport fails every request, so a missing fixture shows up as an error
instead of a silent network call.
"""

import shutil
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path

import httpx2
import pytest

from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.http import HttpClient

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"
TODAY = date(2026, 10, 27)  # fixtures were recorded on this date
FOREVER = 10.0**10


class OfflineTransport(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(str(request.url))
        raise httpx2.ConnectError("network disabled in offline tests", request=request)


@pytest.fixture
def offline_transport() -> OfflineTransport:
    return OfflineTransport()


@pytest.fixture
def fixture_cache(tmp_path: Path) -> Cache:
    directory = tmp_path / "cache"
    shutil.copytree(FIXTURE_CACHE, directory)
    return Cache(directory, ttl=FOREVER)


@pytest.fixture
async def fetcher(
    fixture_cache: Cache, offline_transport: OfflineTransport
) -> AsyncIterator[Fetcher]:
    http = HttpClient(transport=offline_transport, attempts=1)
    yield Fetcher(http, fixture_cache)
    await http.aclose()


def mock_http(
    handler: Callable[[httpx2.Request], httpx2.Response], attempts: int = 3
) -> HttpClient:
    return HttpClient(transport=httpx2.MockTransport(handler), backoff=0, attempts=attempts)
