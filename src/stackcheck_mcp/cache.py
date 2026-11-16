"""On-disk cache of *reduced* registry documents, and the per-call ``Fetcher``.

One JSON file per entry: ``<dir>/<LAYOUT_VERSION>/<namespace>/<quoted key>.json`` holding
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
import re
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
# How long a "not found" answer is reused: short, so a fresh publish shows up soon.
NOT_FOUND_TTL = 900.0
_MISSING = "$missing"  # data of a remembered 404 (no reduced document has this key)
# Bump whenever a reduced document's shape changes, so old entries are never misread.
LAYOUT_VERSION = "v2"
# Entries unused for this long are deleted (per-version documents never expire otherwise).
DEFAULT_MAX_AGE_DAYS = 60.0
DAY = 86400.0
NAMESPACES = ("npm", "pypi", "pypi-meta", "maven", "maven-pom", "eol")
_LAYOUT = re.compile(r"^v\d+$")


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
    def __init__(
        self,
        directory: Path | None,
        ttl: float = DEFAULT_TTL,
        max_age: float = DEFAULT_MAX_AGE_DAYS * DAY,
    ) -> None:
        self.ttl = ttl
        self.max_age = max_age  # seconds; 0 never prunes
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
        raw_age = os.environ.get("STACKCHECK_CACHE_MAX_AGE", "")
        try:
            days = float(raw_age) if raw_age.strip() else DEFAULT_MAX_AGE_DAYS
        except ValueError:
            log.warning("ignoring invalid STACKCHECK_CACHE_MAX_AGE=%r", raw_age)
            days = DEFAULT_MAX_AGE_DAYS
        return cls(Path(directory), ttl, days * DAY)

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

    def is_fresh(self, entry: CacheEntry, ttl: float | None = None) -> bool:
        return time.time() - entry.fetched_at < (self.ttl if ttl is None else ttl)

    def touch(self, namespace: str, key: str) -> None:
        """Mark an entry as used (at most once a day), so pruning keeps it."""
        if not self.enabled:
            return
        path = self._path(namespace, key)
        with contextlib.suppress(OSError):
            if time.time() - path.stat().st_mtime > DAY:
                os.utime(path)

    def prune(self) -> int:
        """Delete entries unused for ``max_age`` and those of older layouts; at most daily.

        Only ``*.json`` files inside the known namespace folders are ever deleted, so a
        ``STACKCHECK_CACHE_DIR`` shared with other data is safe. Returns the count.
        """
        if not self.enabled or self.max_age <= 0:
            return 0
        assert self.root is not None
        marker = self.root / ".pruned"
        now = time.time()
        try:
            if now - marker.stat().st_mtime < DAY:
                return 0
        except FileNotFoundError:
            pass
        except OSError:
            return 0
        removed = 0
        for layout in self.root.parent.iterdir() if self.root.parent.is_dir() else []:
            if not _LAYOUT.match(layout.name) or not layout.is_dir():
                continue
            current = layout.name == LAYOUT_VERSION
            for namespace in NAMESPACES:
                folder = layout / namespace
                if not folder.is_dir():
                    continue
                for path in folder.iterdir():
                    if path.suffix != ".json":
                        continue
                    with contextlib.suppress(OSError):
                        age = now - path.stat().st_mtime
                        temporary = path.name.startswith(".tmp-") and age > 3600
                        if not current or age > self.max_age or temporary:
                            path.unlink()
                            removed += 1
                with contextlib.suppress(OSError):
                    folder.rmdir()  # only when empty
            if not current:
                with contextlib.suppress(OSError):
                    layout.rmdir()
        with contextlib.suppress(OSError):
            self.root.mkdir(parents=True, exist_ok=True)
            marker.touch()
        if removed:
            log.info("pruned %d cache entries", removed)
        return removed

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
        if entry is not None and _missing(entry.data) is not None:
            if self.cache.is_fresh(entry, min(self.cache.ttl, NOT_FOUND_TTL)):
                raise NotFoundError(_missing(entry.data))
            entry = None  # an expired "not found" is never served as stale data
        if entry is not None and (immutable or self.cache.is_fresh(entry)):
            self.cache.touch(namespace, key)
            return entry.data
        try:
            data = await load()
        except NotFoundError as exc:
            self.cache.put(namespace, key, {_MISSING: str(exc)})
            raise
        except FetchError as exc:
            if entry is None:
                raise
            log.warning("serving stale %s/%s: %s", namespace, key, exc)
            self.stale = True
            return entry.data
        self.cache.put(namespace, key, data)
        return data


def _missing(data: Any) -> str | None:
    """The message of a remembered 404, if ``data`` is one."""
    if isinstance(data, dict) and isinstance(data.get(_MISSING), str):
        return str(data[_MISSING])
    return None
