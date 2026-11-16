import asyncio
import json
import time
from pathlib import Path

import httpx2
import pytest

from stackcheck_mcp.cache import LAYOUT_VERSION, Cache, Fetcher, entry_filename
from stackcheck_mcp.http import FetchError, NotFoundError
from tests.conftest import mock_http


def test_entry_filename_is_safe_and_case_unique() -> None:
    assert entry_filename("@types/react") == "@types%2Freact.json"
    assert entry_filename("org.springframework.boot:spring-boot") == (
        "org.springframework.boot%3Aspring-boot.json"
    )
    upper, lower = entry_filename("JSONStream"), entry_filename("jsonstream")
    assert upper.lower() != lower.lower()
    assert len(entry_filename("x" * 400)) == 64 + len(".json")
    for bad in ("../etc/passwd", "a\\b", "c:d", "e\x00f"):
        assert not set(entry_filename(bad)) & set("/\\:\x00")


def test_roundtrip_and_ttl(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=60)
    cache.put("npm", "react", {"latest": "19.3.1"})
    entry = cache.get("npm", "react")
    assert entry is not None and entry.data == {"latest": "19.3.1"}
    assert cache.is_fresh(entry)
    assert not Cache(tmp_path, ttl=60).is_fresh(type(entry)(entry.data, time.time() - 61))
    assert list((tmp_path / LAYOUT_VERSION / "npm").glob(".tmp-*")) == []


def test_ttl_zero_disables_cache(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=0)
    cache.put("npm", "react", {"x": 1})
    assert cache.get("npm", "react") is None
    assert not (tmp_path / LAYOUT_VERSION).exists()


def test_corrupt_or_foreign_entries_are_misses(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=60)
    cache.put("npm", "react", {"x": 1})
    path = tmp_path / LAYOUT_VERSION / "npm" / "react.json"
    path.write_text("{not json", encoding="utf-8")
    assert cache.get("npm", "react") is None
    path.write_text(
        json.dumps({"key": "other", "fetched_at": "2026-01-01T00:00:00+00:00", "data": 1}),
        encoding="utf-8",
    )
    assert cache.get("npm", "react") is None


def test_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STACKCHECK_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("STACKCHECK_CACHE_TTL", "120")
    cache = Cache.from_env()
    assert cache.ttl == 120 and cache.root == tmp_path / LAYOUT_VERSION
    monkeypatch.setenv("STACKCHECK_CACHE_TTL", "soon")
    assert Cache.from_env().ttl == 21600


async def test_stale_entry_served_on_network_failure(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=60)
    cache.put("npm", "react", {"v": "old"})
    path = tmp_path / LAYOUT_VERSION / "npm" / "react.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["fetched_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(raw), encoding="utf-8")

    calls = 0

    def failing(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(503)

    http = mock_http(failing)
    fetcher = Fetcher(http, cache)

    async def load() -> object:
        return await http.get_json("https://registry.example/react")

    assert await fetcher.cached("npm", "react", load) == {"v": "old"}
    assert fetcher.stale is True
    assert calls == 3  # retried transient errors before falling back
    await http.aclose()


async def test_fresh_fetch_replaces_expired_entry(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=0.000001)
    cache.put("npm", "react", {"v": "old"})
    await asyncio.sleep(0.01)
    http = mock_http(lambda request: httpx2.Response(200, json={"v": "new"}))
    fetcher = Fetcher(http, cache)

    async def load() -> object:
        return await http.get_json("https://registry.example/react")

    assert await fetcher.cached("npm", "react", load) == {"v": "new"}
    assert fetcher.stale is False
    entry = cache.get("npm", "react")
    assert entry is not None and entry.data == {"v": "new"}
    await http.aclose()


async def test_immutable_entries_never_expire(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=0.000001)
    cache.put("pypi-meta", "x==1", {"v": 1})
    await asyncio.sleep(0.01)
    fetcher = Fetcher(mock_http(lambda r: httpx2.Response(500)), cache)

    async def load() -> object:
        raise AssertionError("must not fetch")

    assert await fetcher.cached("pypi-meta", "x==1", load, immutable=True) == {"v": 1}


async def test_not_found_is_not_masked_by_stale(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=0.000001)
    cache.put("npm", "gone", {"v": 1})
    await asyncio.sleep(0.01)
    http = mock_http(lambda r: httpx2.Response(404))
    fetcher = Fetcher(http, cache)

    async def load() -> object:
        return await http.get_json("https://registry.example/gone")

    with pytest.raises(NotFoundError):
        await fetcher.cached("npm", "gone", load)
    await http.aclose()


async def test_not_found_is_remembered_briefly(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=3600)
    calls = 0

    async def load() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NotFoundError("unpublished")
        return {"v": 2}

    for _ in range(2):  # a typo in a stack is not looked up again on every call
        with pytest.raises(NotFoundError, match="unpublished"):
            await Fetcher(mock_http(lambda r: httpx2.Response(500)), cache).cached(
                "npm", "new-pkg", load, immutable=True
            )
    assert calls == 1
    # Past NOT_FOUND_TTL (15 min), even for immutable data: a fresh publish shows up.
    path = cache._path("npm", "new-pkg")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["fetched_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(entry), encoding="utf-8")
    fetcher = Fetcher(mock_http(lambda r: httpx2.Response(500)), cache)
    assert await fetcher.cached("npm", "new-pkg", load, immutable=True) == {"v": 2}
    assert calls == 2 and not fetcher.stale


async def test_expired_not_found_is_not_served_as_stale(tmp_path: Path) -> None:
    cache = Cache(tmp_path, ttl=3600)
    cache.put("npm", "x", {"$missing": "not found"})
    path = cache._path("npm", "x")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["fetched_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(entry), encoding="utf-8")

    async def load() -> object:
        raise FetchError("HTTP 503")

    with pytest.raises(FetchError, match="HTTP 503"):
        await Fetcher(mock_http(lambda r: httpx2.Response(500)), cache).cached("npm", "x", load)


async def test_concurrent_identical_loads_are_deduplicated(tmp_path: Path) -> None:

    calls = 0

    async def load() -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"n": calls}

    fetcher = Fetcher(mock_http(lambda r: httpx2.Response(500)), Cache(tmp_path, ttl=60))
    results = await asyncio.gather(*(fetcher.cached("ns", "k", load) for _ in range(5)))
    assert calls == 1 and all(r == {"n": 1} for r in results)


async def test_http_errors() -> None:
    http = mock_http(lambda r: httpx2.Response(403), attempts=3)
    with pytest.raises(FetchError, match="HTTP 403"):
        await http.get_json("https://registry.example/x")
    http = mock_http(lambda r: httpx2.Response(200, content=b"<html>"))
    with pytest.raises(FetchError, match="invalid JSON"):
        await http.get_json("https://registry.example/x")
    seen: list[str] = []

    def check_headers(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["user-agent"])
        return httpx2.Response(200, json={})

    await mock_http(check_headers).get_json("https://registry.example/x")
    assert seen[0].startswith("stackcheck-mcp/")


async def test_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    from stackcheck_mcp import http as http_module

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(http_module, "_sleep", fake_sleep)
    answers = iter(
        [
            httpx2.Response(429, headers={"Retry-After": "3"}),
            httpx2.Response(503),
            httpx2.Response(200, json={"ok": True}),
        ]
    )
    http = mock_http(lambda r: next(answers), attempts=3)
    assert await http.get_json("https://registry.example/x") == {"ok": True}
    assert slept == [3.0, 0.0]  # Retry-After, then the (zero) test backoff
    # A registry asking for longer than a tool call can wait: fail fast, say why.
    http = mock_http(lambda r: httpx2.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(FetchError, match=r"HTTP 429 \(retry after 3600s\)"):
        await http.get_json("https://registry.example/x")
    assert slept == [3.0, 0.0]


def test_retry_after_parsing() -> None:
    from stackcheck_mcp.http import retry_after

    assert retry_after(None) is None and retry_after("soon") is None
    assert retry_after(" 7 ") == 7.0
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past
    assert (retry_after("Fri, 01 Jan 2100 00:00:00 GMT") or 0) > 50


async def test_large_bodies_are_parsed_off_the_event_loop() -> None:
    import threading

    from stackcheck_mcp.http import THREAD_THRESHOLD

    threads: list[str] = []

    def reduce(data: object) -> int:
        threads.append(threading.current_thread().name)
        return len(data)  # type: ignore[arg-type]

    big = {"versions": {str(i): "x" * 100 for i in range(THREAD_THRESHOLD // 100)}}
    http = mock_http(lambda r: httpx2.Response(200, json=big if "big" in r.url.path else {}))
    assert await http.get_json("https://registry.example/big", None, reduce) == 1
    assert await http.get_json("https://registry.example/small", None, reduce) == 0
    main = threading.current_thread().name
    assert threads[0] != main and threads[1] == main
    with pytest.raises(FetchError, match="invalid JSON"):
        bad = mock_http(lambda r: httpx2.Response(200, content=b"{" * (THREAD_THRESHOLD + 1)))
        await bad.get_json("https://registry.example/x", None, reduce)
