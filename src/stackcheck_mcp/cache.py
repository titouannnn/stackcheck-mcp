"""On-disk cache of *reduced* registry documents, and the per-call ``Fetcher``.

One JSON file per entry: ``<dir>/v1/<namespace>/<quoted key>.json`` holding
``{"key", "fetched_at", "data"}``. Writes are atomic (temp file + ``os.replace``) so
several server processes can share a directory. Per-version documents never change
upstream and are flagged ``immutable``: they are reused whatever their age.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import platformdirs

from stackcheck_mcp.http import FetchError, HttpClient, NotFoundError

log = logging.getLogger(__name__)

DEFAULT_TTL = 21600.0
# Bump whenever a reduced document's shape changes, so old entries are never misread.
LAYOUT_VERSION = "v1"


@dataclass(frozen=True)
class CacheEntry:
    data: Any
    fetched_at: float


def entry_filename(key: str) -> str:
    """File name for a key: readable, safe on every OS, unique on case-insensitive disks."""
    name = quote(key, safe="@._-+=~")
    digest = hashlib.sha256(key.encode()).hexdigest()
    if len(name) > 150:
        return f"{digest}.json"
    if key != key.lower():
        return f"{name}.{digest[:8]}.json"
    return f"{name}.json"


class Cache:
    def __init__(self, directory: Path | None, ttl: float = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self.enabled = directory is not None and ttl > 0
        self.root = directory / LAYOUT_VERSION if directory is not None else None

    @classmethod
    def from_env(cls) -> "Cache":
        directory = os.environ.get("STACKCHECK_CACHE_DIR") or platformdirs.user_cache_dir(
            "stackcheck-mcp", appauthor=False
        )
        raw_ttl = os.environ.get("STACKCHECK_CACHE_TTL", "")
        try:
            ttl = float(raw_ttl) if raw_ttl.strip() else DEFAULT_TTL
        except ValueError:
            log.warning("ignoring invalid STACKCHECK_CACHE_TTL=%r", raw_ttl)
            ttl = DEFAULT_TTL
        return cls(Path(directory), ttl)

    def _path(self, namespace: str, key: str) -> Path:
        assert self.root is not None
        return self.root / namespace / entry_filename(key)

    def get(self, namespace: str, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        try:
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            if raw.get("key") != key:
                return None
            fetched_at = datetime.fromisoformat(raw["fetched_at"]).timestamp()
            return CacheEntry(raw["data"], fetched_at)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.debug("ignoring unreadable cache entry %s: %s", path, exc)
            return None

    def is_fresh(self, entry: CacheEntry) -> bool:
        return time.time() - entry.fetched_at < self.ttl

    def put(self, namespace: str, key: str, data: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        payload = {"key": key, "fetched_at": fetched_at, "data": data}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
            _replace(tmp, path)
        except OSError as exc:
            log.warning("could not write cache entry %s: %s", path, exc)


def _replace(tmp: str, path: Path) -> None:
    # On Windows the target can be briefly locked by a concurrent reader.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
    with contextlib.suppress(OSError):
        os.unlink(tmp)


class Fetcher:
    """Shared HTTP client + cache, scoped to one tool call.

    Deduplicates identical loads within the call and remembers whether any answer came
    from a stale cache entry (``stale``) so the tool output can say so.
    """

    def __init__(self, http: HttpClient, cache: Cache) -> None:
        self.http = http
        self.cache = cache
        self.stale = False
        self._tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}

    async def cached(
        self,
        namespace: str,
        key: str,
        load: Callable[[], Awaitable[Any]],
        *,
        immutable: bool = False,
    ) -> Any:
        task = self._tasks.get((namespace, key))
        if task is None:
            task = asyncio.ensure_future(self._cached(namespace, key, load, immutable))
            self._tasks[(namespace, key)] = task
        return await asyncio.shield(task)

    async def _cached(
        self,
        namespace: str,
        key: str,
        load: Callable[[], Awaitable[Any]],
        immutable: bool,
    ) -> Any:
        entry = self.cache.get(namespace, key)
        if entry is not None and (immutable or self.cache.is_fresh(entry)):
            return entry.data
        try:
            data = await load()
        except NotFoundError:
            raise
        except FetchError as exc:
            if entry is None:
                raise
            log.warning("serving stale %s/%s: %s", namespace, key, exc)
            self.stale = True
            return entry.data
        self.cache.put(namespace, key, data)
        return data
