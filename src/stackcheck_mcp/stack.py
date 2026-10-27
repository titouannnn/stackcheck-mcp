"""``check_stack`` / ``get_runtime_info`` orchestration: concurrency and output assembly.

The decision rules live in the ecosystem modules; this module only fans out, waits for
the targets the rules need, and assembles the compact answer.
"""

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

from stackcheck_mcp import npm, pypi
from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.http import FetchError, HttpClient
from stackcheck_mcp.models import (
    EcosystemResult,
    Issue,
    RuntimeResult,
    StackResult,
    Targets,
    ValidationStatus,
)
from stackcheck_mcp.runtimes import TARGET_SLUGS, resolve_targets, runtime_infos, utc_today


@dataclass
class Settings:
    resolver_validation: bool = False
    resolver_timeout: float = 120.0
    today: Callable[[], date] = utc_today


@dataclass
class App:
    """Process-wide state shared by all tool calls."""

    settings: Settings = field(default_factory=Settings)
    cache: Cache = field(default_factory=Cache.from_env)
    http_factory: Callable[[], HttpClient] = HttpClient
    _http: HttpClient | None = None

    @property
    def http(self) -> HttpClient:
        # Created lazily so the client binds to the server's running event loop.
        if self._http is None:
            self._http = self.http_factory()
        return self._http

    def fetcher(self) -> Fetcher:
        return Fetcher(self.http, self.cache)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


class InputError(ValueError):
    """The request as a whole is unusable (reported as a tool error)."""


async def _swallow(coro: Coroutine[Any, Any, Any]) -> None:
    # The real check reports failures; this only warms the per-call memo.
    with contextlib.suppress(FetchError, ValueError):
        await coro


def _prefetch(fetcher: Fetcher, npm_specs: list[str], pypi_specs: list[str]) -> list[Any]:
    """Start registry fetches while targets resolve; the checks reuse them via the memo."""
    jobs: list[Any] = []
    for raw in npm_specs:
        try:
            jobs.append(_swallow(npm.fetch_doc(fetcher, npm.parse_spec(raw).name)))
        except ValueError:
            continue
    for raw in pypi_specs:
        try:
            jobs.append(_swallow(pypi.fetch_index(fetcher, pypi.parse_spec(raw).name)))
        except ValueError:
            continue
    return jobs


def _validation(settings: Settings, ecosystem: str) -> ValidationStatus:
    return "skipped"


async def check_stack(
    app: App,
    *,
    npm_specs: list[str] | None = None,
    pypi_specs: list[str] | None = None,
    runtimes: list[str] | None = None,
    targets: Targets | None = None,
) -> StackResult:
    npm_specs = [s for s in npm_specs or [] if s.strip()]
    pypi_specs = [s for s in pypi_specs or [] if s.strip()]
    runtimes = [s for s in runtimes or [] if s.strip()]
    if not (npm_specs or pypi_specs or runtimes):
        raise InputError("pass at least one non-empty list: npm, pypi or runtimes")

    today = app.settings.today()
    fetcher = app.fetcher()
    given: dict[str, str] = {k: v for k, v in (targets or {}).items() if isinstance(v, str)}
    errors: dict[str, dict[str, str]] = {}
    unknown = sorted(set(given) - set(TARGET_SLUGS))
    if unknown:
        errors["targets"] = {k: "unknown target (use node, python, java)" for k in unknown}
    wanted: dict[str, str | None] = {k: given[k] for k in TARGET_SLUGS if k in given}
    if npm_specs:
        wanted.setdefault("node", None)
    if pypi_specs:
        wanted.setdefault("python", None)

    targets_task = asyncio.ensure_future(resolve_targets(fetcher, wanted, today))
    runtimes_task = asyncio.ensure_future(runtime_infos(fetcher, runtimes, today))
    prefetch = asyncio.gather(*_prefetch(fetcher, npm_specs, pypi_specs))
    resolved, target_errors = await targets_task
    if target_errors:
        errors.setdefault("targets", {}).update(target_errors)

    results: dict[str, EcosystemResult] = {}

    async def run_npm() -> None:
        results["npm"] = await npm.check_npm(fetcher, npm_specs, resolved.get("node"))

    async def run_pypi() -> None:
        results["pypi"] = await pypi.check_pypi(fetcher, pypi_specs, resolved.get("python"))

    jobs = ([run_npm()] if npm_specs else []) + ([run_pypi()] if pypi_specs else [])
    await asyncio.gather(*jobs)
    await prefetch
    runtime_data, runtime_errors = await runtimes_task

    out: dict[str, Any] = {}
    if resolved:
        out["targets"] = resolved
    issues: list[Issue] = []
    validation: dict[str, ValidationStatus] = {}
    for ecosystem in ("npm", "pypi"):
        result = results.get(ecosystem)
        if result is None:
            continue
        if result.picks:
            out[ecosystem] = result.picks
            validation[ecosystem] = _validation(app.settings, ecosystem)
        issues += result.issues
        if result.errors:
            errors[ecosystem] = result.errors
    if runtime_data:
        out["runtimes"] = runtime_data
    if runtime_errors:
        errors["runtimes"] = runtime_errors
    if issues:
        out["issues"] = issues
    if errors:
        out["errors"] = errors
    if validation:
        out["validation"] = validation
    if fetcher.stale:
        out["stale"] = True
    return cast(StackResult, out)


async def get_runtime_info(app: App, products: list[str]) -> RuntimeResult:
    products = [p for p in products if p.strip()]
    if not products:
        raise InputError("pass at least one endoflife.date product slug")
    fetcher = app.fetcher()
    infos, errors = await runtime_infos(fetcher, products, app.settings.today())
    out: RuntimeResult = {}
    if infos:
        out["runtimes"] = infos
    if errors:
        out["errors"] = errors
    if fetcher.stale:
        out["stale"] = True
    return out
