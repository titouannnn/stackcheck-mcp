"""``check_stack`` / ``get_runtime_info`` orchestration: concurrency and output assembly.

The decision rules live in the ecosystem modules; this module only fans out, waits for
the targets the rules need, and assembles the compact answer.
"""

import asyncio
import contextlib
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

from packaging.utils import canonicalize_name
from packaging.version import Version

from stackcheck_mcp import maven, npm, pypi, validation
from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.http import FetchError, HttpClient, NotFoundError
from stackcheck_mcp.models import (
    EcosystemResult,
    Explanation,
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


_EXTRA = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


class InputError(ValueError):
    """The request as a whole is unusable (reported as a tool error)."""


async def _swallow(coro: Coroutine[Any, Any, Any]) -> None:
    # The real check reports failures; this only warms the per-call memo.
    with contextlib.suppress(FetchError, ValueError):
        await coro


def _prefetch(
    fetcher: Fetcher, npm_specs: list[str], pypi_specs: list[str], maven_specs: list[str]
) -> list[Any]:
    """Start registry fetches while targets resolve; the checks reuse them via the memo."""
    jobs: list[Any] = []
    for raw in maven_specs:
        try:
            spec = maven.parse_spec(raw)
        except ValueError:
            continue
        jobs.append(_swallow(maven.fetch_metadata(fetcher, spec.group, spec.artifact)))
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


async def _validate(
    settings: Settings, results: dict[str, EcosystemResult], python: str | None
) -> dict[str, validation.Outcome]:
    """Real-resolver checks of the picks; ``skipped`` unless enabled at startup."""
    with_picks = [eco for eco in ("npm", "pypi") if eco in results and results[eco].picks]
    if not settings.resolver_validation:
        return {eco: validation.Outcome("skipped") for eco in with_picks}
    jobs = []
    for eco in with_picks:
        result = results[eco]
        if eco == "npm":
            jobs.append(validation.validate_npm(result.pins, settings.resolver_timeout))
        else:
            jobs.append(
                validation.validate_pypi(
                    result.pins, result.specs, python, settings.resolver_timeout
                )
            )
    return dict(zip(with_picks, await asyncio.gather(*jobs), strict=True))


async def check_stack(
    app: App,
    *,
    npm_specs: list[str] | None = None,
    pypi_specs: list[str] | None = None,
    maven_specs: list[str] | None = None,
    maven_bom: str | None = None,
    runtimes: list[str] | None = None,
    targets: Targets | None = None,
) -> StackResult:
    npm_specs = [s for s in npm_specs or [] if s.strip()]
    pypi_specs = [s for s in pypi_specs or [] if s.strip()]
    maven_specs = [s for s in maven_specs or [] if s.strip()]
    maven_bom = maven_bom.strip() if maven_bom and maven_bom.strip() else None
    runtimes = [s for s in runtimes or [] if s.strip()]
    if not (npm_specs or pypi_specs or maven_specs or maven_bom or runtimes):
        raise InputError("pass at least one of npm, pypi, maven, maven_bom or runtimes")

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
    if maven_specs or maven_bom:
        wanted.setdefault("java", None)

    targets_task = asyncio.ensure_future(resolve_targets(fetcher, wanted, today))
    runtimes_task = asyncio.ensure_future(runtime_infos(fetcher, runtimes, today))
    prefetch = asyncio.gather(*_prefetch(fetcher, npm_specs, pypi_specs, maven_specs))
    resolved, target_errors = await targets_task
    if target_errors:
        errors.setdefault("targets", {}).update(target_errors)

    results: dict[str, EcosystemResult] = {}

    async def run_npm() -> None:
        results["npm"] = await npm.check_npm(fetcher, npm_specs, resolved.get("node"))

    async def run_pypi() -> None:
        results["pypi"] = await pypi.check_pypi(fetcher, pypi_specs, resolved.get("python"))

    async def run_maven() -> None:
        results["maven"] = await maven.check_maven(
            fetcher, maven_specs, maven_bom, resolved.get("java")
        )

    jobs = ([run_npm()] if npm_specs else []) + ([run_pypi()] if pypi_specs else [])
    jobs += [run_maven()] if maven_specs or maven_bom else []
    await asyncio.gather(*jobs)
    await prefetch
    outcomes = await _validate(app.settings, results, resolved.get("python"))
    runtime_data, runtime_errors = await runtimes_task

    out: dict[str, Any] = {}
    if resolved:
        out["targets"] = resolved
    issues: list[Issue] = []
    statuses: dict[str, ValidationStatus] = {}
    for ecosystem in ("npm", "pypi", "maven"):
        result = results.get(ecosystem)
        if result is None:
            continue
        if result.picks:
            out[ecosystem] = result.picks
        issues += result.issues
        if result.errors:
            errors[ecosystem] = result.errors
    for ecosystem, outcome in outcomes.items():
        statuses[ecosystem] = outcome.status
        issues += outcome.issues
    if runtime_data:
        out["runtimes"] = runtime_data
    if runtime_errors:
        errors["runtimes"] = runtime_errors
    if issues:
        out["issues"] = issues
    if errors:
        out["errors"] = errors
    if statuses:
        out["validation"] = statuses
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


async def _explain_npm(fetcher: Fetcher, package: str, version: str, out: Explanation) -> None:
    spec = npm.parse_spec(package)
    out["package"] = spec.name
    doc = await npm.fetch_doc(fetcher, spec.name)
    chosen = version or spec.range or ""
    if not chosen or chosen not in doc["versions"]:
        chosen = npm.pick(doc, chosen or None) or ""
    if chosen not in doc["versions"]:
        out["error"] = "version not found (only stable versions are indexed)"
        return
    out["version"] = chosen
    info = npm.version_info(doc, chosen)
    if "peer" in info:
        out["peerDependencies"] = info["peer"]
    if "optional" in info:
        out["optionalPeers"] = info["optional"]
    if "engines" in info:
        out["engines"] = info["engines"]
    if "deprecated" in info:
        out["deprecated"] = info["deprecated"]


async def _explain_pypi(fetcher: Fetcher, package: str, version: str, out: Explanation) -> None:
    spec = pypi.parse_spec(package)
    out["package"] = spec.name
    index = await pypi.fetch_index(fetcher, spec.name)
    chosen = str(Version(version)) if version else pypi.select(index, spec)[0]
    if chosen is None or chosen not in index["versions"]:
        out["error"] = "version not found (only stable versions are indexed)"
        return
    out["version"] = chosen
    meta = await pypi.fetch_meta(fetcher, spec.name, chosen)
    # Extra-guarded lines only for the extras asked for (``fastapi[standard]``): compact.
    requires, extras = [], set()
    for line in meta["requires_dist"]:
        guards = {canonicalize_name(e) for e in _EXTRA.findall(line)}
        extras |= guards
        if not guards or guards & spec.extras:
            requires.append(line)
    if requires:
        out["requires_dist"] = requires
    if extras:
        out["extras"] = sorted(extras)
    requires_python = meta.get("requires_python") or pypi.version_info(index, chosen).get("rp")
    if requires_python:
        out["requires_python"] = requires_python
    if yanked := pypi.version_info(index, chosen).get("yanked"):
        out["yanked"] = yanked


async def _explain_maven(fetcher: Fetcher, package: str, version: str, out: Explanation) -> None:
    spec = maven.parse_spec(package)
    out["package"] = spec.ga
    chosen = version or (spec.range if spec.range and "[" not in spec.range else "")
    if not chosen or "(" in chosen or "[" in chosen:
        meta = await maven.fetch_metadata(fetcher, spec.group, spec.artifact)
        chosen = maven.pick(meta, spec) or ""
    if not chosen:
        out["error"] = "no stable version found"
        return
    out["version"] = chosen
    coords = f"{spec.ga}:{chosen}"
    pom = await maven.fetch_pom(fetcher, coords)
    model = await maven.load_effective(fetcher, coords)
    if "parent" in pom:
        out["parent"] = pom["parent"]
    deps = [maven.interpolate(d, model.props) for d in pom.get("deps", [])]
    if deps:
        out["dependencies"] = deps


async def explain(app: App, ecosystem: str, package: str, version: str = "") -> Explanation:
    """Declarations of one exact version (latest stable when ``version`` is empty)."""
    if ecosystem not in ("npm", "pypi", "maven"):
        raise InputError("ecosystem must be npm, pypi or maven")
    fetcher = app.fetcher()
    out = cast(Explanation, {"ecosystem": ecosystem, "package": package.strip()})
    handlers = {"npm": _explain_npm, "pypi": _explain_pypi, "maven": _explain_maven}
    try:
        await handlers[ecosystem](fetcher, package, version.strip(), out)
    except NotFoundError:
        out["error"] = "not found"
    except (FetchError, ValueError) as exc:
        out["error"] = str(exc)
    if fetcher.stale:
        out["stale"] = True
    return out
