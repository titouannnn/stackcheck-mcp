"""PyPI: Simple API index → reduce → pick latest stable → python/wheels/requires checks.

Reduced index (stable versions only; identical per-version entries stored once)::

    {"name": "pydantic-core", "status": "active",
     "versions": {"2.50.1": 0, ...},
     "meta": [{"rp": ">=3.10", "cp": [10, 11, 12, 13, 14, 15], "abi3": 9,
               "pure": true, "sdist": true, "yanked": "reason"}]}

Wheels are read for the platform every check assumes (markers, ``uv --python-platform
linux``): CPython on glibc Linux x86_64, i.e. ``any`` and ``manylinux*_x86_64`` tags.
``cp`` lists CPython 3 minors with a version-specific (non free-threaded) wheel, ``abi3``
the lowest minor of a stable-ABI wheel, ``pure`` any ``py3-none-*`` wheel (including
platform-tagged ones such as ruff's). Releases without any file are dropped (nothing to
install). Per-version dependency metadata
(``requires_dist``/``requires_python``) comes from ``/pypi/<name>/<version>/json`` and
is cached forever: published files never change.
"""

import asyncio
import contextlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict, cast

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from stackcheck_mcp.cache import Fetcher
from stackcheck_mcp.http import FetchError, NotFoundError
from stackcheck_mcp.models import EcosystemResult, Issue

PYPI = "https://pypi.org"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
ALTERNATIVE_SCAN = 40  # versions of the constraining package inspected for `alternative`
_CANONICAL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CP = re.compile(r"^cp3(\d+)$")
_MANYLINUX = re.compile(r"^manylinux(?:1|2010|2014|_\d+_\d+)_x86_64$")
_SDIST_EXTENSIONS = (".tar.gz", ".zip", ".tar.bz2", ".tar.xz", ".tgz", ".tar")


class PypiVersion(TypedDict):
    rp: NotRequired[str]
    yanked: NotRequired[str]
    pure: NotRequired[bool]
    cp: NotRequired[list[int]]
    abi3: NotRequired[int]
    sdist: NotRequired[bool]


class PypiIndex(TypedDict):
    name: str
    status: NotRequired[str]
    versions: dict[str, int]
    meta: list[PypiVersion]


class PypiMeta(TypedDict):
    requires_dist: list[str]
    requires_python: NotRequired[str]


@dataclass(frozen=True)
class PypiSpec:
    name: str
    extras: frozenset[str]
    specifier: SpecifierSet
    raw: str

    def pin(self, version: str) -> str:
        extras = f"[{','.join(sorted(self.extras))}]" if self.extras else ""
        return f"{self.name}{extras}=={version}"


def version_info(index: PypiIndex, version: str) -> PypiVersion:
    return index["meta"][index["versions"][version]]


# --------------------------------------------------------------------------- parse


def validate_name(name: str) -> str:
    canonical = canonicalize_name(name)
    if not _CANONICAL.match(canonical) or len(canonical) > 200:
        raise ValueError("invalid PyPI project name")
    return canonical


def parse_spec(raw: str) -> PypiSpec:
    try:
        req = Requirement(raw.strip())
    except InvalidRequirement as exc:
        raise ValueError(f"invalid requirement: {exc}") from None
    if req.url:
        raise ValueError("URL requirements are not supported")
    if req.marker is not None:
        raise ValueError("environment markers are not supported; use targets.python")
    name = validate_name(req.name)
    extras = frozenset(canonicalize_name(e) for e in req.extras)
    normalized = Requirement(str(req))
    normalized.name = name
    normalized.extras = set(extras)
    return PypiSpec(name, extras, req.specifier, str(normalized))


# --------------------------------------------------------------------------- reduce


def _stable(version: str) -> str | None:
    """Normalized form of a stable version, else ``None``."""
    try:
        parsed = Version(version)
    except InvalidVersion:
        return None
    if parsed.is_prerelease or parsed.is_devrelease:
        return None
    return str(parsed)


def _file_version(filename: str) -> tuple[str, str, str] | None:
    """``(normalized name, normalized version, "wheel" | "sdist")`` from a distribution filename.

    Legacy sdist names are ambiguous: ``selenium-2.0-dev-9429.tar.gz`` parses as project
    ``selenium-2.0-dev``, version ``9429``. Callers must check the name.
    """
    try:
        if filename.endswith(".whl"):
            name, version, *_ = parse_wheel_filename(filename)
            return name, str(version), "wheel"
        if filename.endswith((".tar.gz", ".zip")):
            name, version = parse_sdist_filename(filename)
            return name, str(version), "sdist"
        for extension in _SDIST_EXTENSIONS:
            if filename.endswith(extension):
                stem, _, raw = filename[: -len(extension)].rpartition("-")
                return canonicalize_name(stem), str(Version(raw)), "sdist"
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
        return None
    return None


def linux_x86_64(platform: str) -> bool:
    """Platforms a CPython on glibc Linux x86_64 installs (the checks' target platform)."""
    return platform == "any" or _MANYLINUX.match(platform) is not None


def _wheel_tags(filename: str) -> tuple[bool, set[int], int | None]:
    """(pure, CPython minors, lowest abi3 minor) for one wheel, Linux x86_64 tags only."""
    pure, minors, abi3 = False, set[int](), None
    try:
        tags = parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return pure, minors, abi3
    for tag in tags:
        if not linux_x86_64(tag.platform):
            continue
        if tag.interpreter.startswith("py3") and tag.abi == "none":
            pure = True
        elif match := _CP.match(tag.interpreter):
            minor = int(match.group(1))
            if tag.abi == "abi3":
                abi3 = minor if abi3 is None else min(abi3, minor)
            elif tag.abi in (f"cp3{minor}", f"cp3{minor}m", f"cp3{minor}d", "none"):
                minors.add(minor)
    return pure, minors, abi3


@dataclass
class _Files:
    rp: str | None = None
    total: int = 0
    yanked: int = 0
    yank_reason: str | None = None
    pure: bool = False
    sdist: bool = False
    abi3: int | None = None
    minors: set[int] = field(default_factory=set)

    def add(self, filename: str, kind: str, rp: Any, yanked: Any) -> None:
        self.total += 1
        if yanked:
            self.yanked += 1
            if isinstance(yanked, str) and yanked and not self.yank_reason:
                self.yank_reason = yanked
        elif isinstance(rp, str) and rp.strip() and self.rp is None:
            self.rp = rp.strip()
        if kind == "sdist":
            self.sdist = True
            return
        pure, minors, abi3 = _wheel_tags(filename)
        self.pure = self.pure or pure
        self.minors |= minors
        if abi3 is not None:
            self.abi3 = abi3 if self.abi3 is None else min(self.abi3, abi3)

    def reduce(self) -> PypiVersion:
        info: PypiVersion = {}
        if self.rp:
            info["rp"] = self.rp
        if self.total and self.yanked == self.total:
            info["yanked"] = self.yank_reason or "yanked"
        if self.pure:
            info["pure"] = True
        if self.minors:
            info["cp"] = sorted(self.minors)
        if self.abi3 is not None:
            info["abi3"] = self.abi3
        if self.sdist:
            info["sdist"] = True
        return info


def _build_index(name: str, status: str | None, files: dict[str, _Files]) -> PypiIndex:
    # A release without any installable file (all deleted, or eggs only) is not a candidate.
    ordered = sorted((v for v in files if files[v].total), key=Version)
    table: list[PypiVersion] = []
    seen: dict[str, int] = {}
    versions: dict[str, int] = {}
    for version in ordered:
        info = files[version].reduce()
        key = json.dumps(info, sort_keys=True)
        if key not in seen:
            seen[key] = len(table)
            table.append(info)
        versions[version] = seen[key]
    index: PypiIndex = {"name": name, "versions": versions, "meta": table}
    if status:
        index["status"] = status
    return index


def reduce_simple(raw: Any) -> PypiIndex:
    """Reduce a PEP 691 JSON Simple API project page."""
    try:
        name = canonicalize_name(str(raw["name"]))
        files: dict[str, _Files] = {}
        listed = raw.get("versions") or []
        for version in listed:
            if (stable := _stable(str(version))) is not None:
                files.setdefault(stable, _Files())
        for item in raw["files"]:
            parsed = _file_version(str(item["filename"]))
            if parsed is None or parsed[0] != name:
                continue
            _, version, kind = parsed
            # The project's version list (API >= 1.1) is authoritative over filenames.
            if version not in files and (listed or _stable(version) is None):
                continue
            files.setdefault(version, _Files()).add(
                item["filename"], kind, item.get("requires-python"), item.get("yanked")
            )
        project_status = raw.get("project-status") or {}
        status = project_status.get("status")
        if not isinstance(status, str) or status == "active":
            status = None
        elif reason := project_status.get("reason"):
            status = f"{status}: {reason}"
    except (KeyError, TypeError, AttributeError) as exc:
        raise FetchError(f"unexpected PyPI Simple API response: {exc}") from exc
    return _build_index(name, status, files)


def reduce_json_project(raw: Any) -> PypiIndex:
    """Reduce the legacy ``/pypi/<name>/json`` document (fallback)."""
    try:
        name = canonicalize_name(str(raw["info"]["name"]))
        files: dict[str, _Files] = {}
        for version, release_files in raw["releases"].items():
            stable = _stable(str(version))
            if stable is None:
                continue
            bucket = files.setdefault(stable, _Files())
            for item in release_files:
                kind = "wheel" if item.get("packagetype") == "bdist_wheel" else "sdist"
                if kind == "sdist" and item.get("packagetype") != "sdist":
                    continue
                yanked = (item.get("yanked_reason") or True) if item.get("yanked") else False
                bucket.add(item["filename"], kind, item.get("requires_python"), yanked)
    except (KeyError, TypeError, AttributeError) as exc:
        raise FetchError(f"unexpected PyPI JSON response: {exc}") from exc
    return _build_index(name, None, files)


def reduce_meta(raw: Any) -> PypiMeta:
    try:
        info = raw["info"]
        meta: PypiMeta = {"requires_dist": [str(r) for r in info.get("requires_dist") or []]}
        if isinstance(info.get("requires_python"), str) and info["requires_python"].strip():
            meta["requires_python"] = info["requires_python"].strip()
    except (KeyError, TypeError, AttributeError) as exc:
        raise FetchError(f"unexpected PyPI JSON response: {exc}") from exc
    return meta


# --------------------------------------------------------------------------- decide


def marker_env(python: str) -> dict[str, str]:
    """Marker environment for a CPython target on Linux x86_64."""
    major_minor = ".".join(python.split(".")[:2])
    return {
        "python_version": major_minor,
        "python_full_version": python,
        "implementation_name": "cpython",
        "implementation_version": python,
        "platform_python_implementation": "CPython",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "",
        "platform_version": "",
    }


def marker_applies(marker: Marker | None, env: dict[str, str], extras: frozenset[str]) -> bool:
    """A marker applies if it holds without extras or with any requested extra."""
    if marker is None:
        return True
    return any(marker.evaluate({**env, "extra": extra}) for extra in ["", *sorted(extras)])


def _specifier(text: str | None) -> SpecifierSet | None:
    if not text:
        return None
    try:
        return SpecifierSet(text)
    except InvalidSpecifier:
        return None


def python_ok(requires_python: str | None, python: str) -> bool | None:
    """``None`` when unknown/unparsable, else whether ``python`` is accepted."""
    spec = _specifier(requires_python)
    return None if spec is None else spec.contains(python, prereleases=True)


def wheel_ok(info: PypiVersion, minor: int) -> bool:
    if info.get("pure") or minor in info.get("cp", []):
        return True
    abi3 = info.get("abi3")
    return abi3 is not None and abi3 <= minor


def candidates(index: PypiIndex, specifier: SpecifierSet) -> list[str]:
    """Stable versions inside ``specifier``, ascending."""
    return [v for v in index["versions"] if specifier.contains(v, prereleases=True)]


def select(index: PypiIndex, spec: PypiSpec) -> tuple[str | None, list[Issue]]:
    """Highest stable version in the user's specifier, preferring non-yanked ones.

    A yanked latest is never hidden: it yields a ``yanked`` issue even when skipped.
    """
    matching = candidates(index, spec.specifier)
    if not matching:
        return None, []
    usable = [v for v in matching if "yanked" not in version_info(index, v)]
    chosen = usable[-1] if usable else matching[-1]
    issues: list[Issue] = []
    latest = matching[-1]
    if "yanked" in version_info(index, latest):
        issue: Issue = {
            "kind": "yanked",
            "ecosystem": "pypi",
            "package": f"{spec.name}=={latest}",
            "severity": "warn",
            "message": version_info(index, latest)["yanked"][:300],
        }
        if chosen != latest:
            issue["suggestion"] = {spec.name: chosen}
        issues.append(issue)
    return chosen, issues


@dataclass(frozen=True)
class Pick:
    spec: PypiSpec
    version: str
    index: PypiIndex

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def info(self) -> PypiVersion:
        return version_info(self.index, self.version)

    @property
    def label(self) -> str:
        return f"{self.name}=={self.version}"


def requirements_on(
    meta: PypiMeta, target: str, env: dict[str, str], extras: frozenset[str]
) -> list[SpecifierSet]:
    """Specifiers that ``meta`` (with ``extras``) applies to project ``target``."""
    found = []
    for line in meta["requires_dist"]:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if canonicalize_name(req.name) == target and marker_applies(req.marker, env, extras):
            found.append(req.specifier)
    return found


@dataclass
class AlternativeRequest:
    """A ``requires`` issue without suggestion: look for an older constraining version."""

    issue: Issue
    source: Pick
    constrained: Pick


def check_python(picks: dict[str, Pick], python: str) -> tuple[list[Issue], set[str]]:
    issues: list[Issue] = []
    failed: set[str] = set()
    for item in picks.values():
        requires_python = item.info.get("rp")
        if python_ok(requires_python, python) is not False:
            continue
        failed.add(item.name)
        issue: Issue = {
            "kind": "python",
            "ecosystem": "pypi",
            "package": item.label,
            "range": requires_python or "",
            "target": f"python {python}",
        }
        for version in reversed(candidates(item.index, item.spec.specifier)):
            info = version_info(item.index, version)
            if "yanked" not in info and python_ok(info.get("rp"), python) is not False:
                issue["suggestion"] = {item.name: version}
                break
        issues.append(issue)
    return issues, failed


def check_wheels(picks: dict[str, Pick], python: str, skip: set[str]) -> list[Issue]:
    minor = int(python.split(".")[1])
    issues: list[Issue] = []
    for item in picks.values():
        info = item.info
        has_files = info.get("pure") or info.get("cp") or "abi3" in info or info.get("sdist")
        if item.name in skip or not has_files or wheel_ok(info, minor):
            continue
        issue: Issue = {
            "kind": "wheels",
            "ecosystem": "pypi",
            "package": item.label,
            "target": f"cpython 3.{minor}",
        }
        if info.get("sdist"):
            issue["severity"] = "warn"
            issue["message"] = "no wheel for this Python; installers build from the sdist"
        else:
            issue["message"] = "no wheel or sdist for this Python"
        issues.append(issue)
    return issues


def check_requires(
    picks: dict[str, Pick], metas: dict[str, PypiMeta], env: dict[str, str]
) -> tuple[list[Issue], list[AlternativeRequest]]:
    issues: list[Issue] = []
    pending: list[AlternativeRequest] = []
    for constrained in picks.values():
        constraints = [
            (source, spec)
            for source in picks.values()
            if source.name != constrained.name and source.name in metas
            for spec in requirements_on(
                metas[source.name], constrained.name, env, source.spec.extras
            )
        ]
        failing = [
            (s, spec)
            for s, spec in constraints
            if not spec.contains(constrained.version, prereleases=True)
        ]
        if not failing:
            continue
        suggestion = None
        for version in reversed(candidates(constrained.index, constrained.spec.specifier)):
            if "yanked" in version_info(constrained.index, version):
                continue
            if all(spec.contains(version, prereleases=True) for _, spec in constraints):
                suggestion = version
                break
        for source, spec in failing:
            issue: Issue = {
                "kind": "requires",
                "ecosystem": "pypi",
                "package": constrained.label,
                "required_by": source.label,
                "range": str(spec),
            }
            if suggestion:
                issue["suggestion"] = {constrained.name: suggestion}
            else:
                pending.append(AlternativeRequest(issue, source, constrained))
            issues.append(issue)
    return issues, pending


def check_status(picks: dict[str, Pick]) -> list[Issue]:
    return [
        {
            "kind": "deprecated",
            "ecosystem": "pypi",
            "package": item.label,
            "severity": "warn",
            "message": f"project status: {item.index['status']}"[:300],
        }
        for item in picks.values()
        if item.index.get("status")
    ]


# --------------------------------------------------------------------------- fetch


async def fetch_index(fetcher: Fetcher, name: str) -> PypiIndex:
    async def load() -> PypiIndex:
        try:
            raw = await fetcher.http.get_json(
                f"{PYPI}/simple/{name}/", headers={"Accept": SIMPLE_JSON}
            )
            return reduce_simple(raw)
        except NotFoundError:
            raise
        except FetchError:
            return reduce_json_project(await fetcher.http.get_json(f"{PYPI}/pypi/{name}/json"))

    return cast(PypiIndex, await fetcher.cached("pypi", name, load))


async def fetch_meta(fetcher: Fetcher, name: str, version: str) -> PypiMeta:
    async def load() -> PypiMeta:
        return reduce_meta(await fetcher.http.get_json(f"{PYPI}/pypi/{name}/{version}/json"))

    return cast(
        PypiMeta, await fetcher.cached("pypi-meta", f"{name}=={version}", load, immutable=True)
    )


async def _older_version(
    fetcher: Fetcher, source: Pick, accept: Callable[[PypiMeta], bool]
) -> str | None:
    """Highest other stable, non-yanked version of ``source`` whose metadata ``accept``s."""
    versions = [
        v
        for v in reversed(candidates(source.index, source.spec.specifier))
        if v != source.version and "yanked" not in version_info(source.index, v)
    ][:ALTERNATIVE_SCAN]
    for start in range(0, len(versions), 8):
        batch = versions[start : start + 8]
        metas = await asyncio.gather(
            *(fetch_meta(fetcher, source.name, v) for v in batch), return_exceptions=True
        )
        for version, meta in zip(batch, metas, strict=True):
            if not isinstance(meta, BaseException) and accept(meta):
                return version
    return None


async def find_alternative(
    fetcher: Fetcher, request: AlternativeRequest, env: dict[str, str]
) -> str | None:
    """Highest stable version of the constraining package that accepts the constrained pick."""
    source, constrained = request.source, request.constrained

    def accept(meta: PypiMeta) -> bool:
        specs = requirements_on(meta, constrained.name, env, source.spec.extras)
        return bool(specs) and all(s.contains(constrained.version, prereleases=True) for s in specs)

    return await _older_version(fetcher, source, accept)


# --------------------------------------------------------------------------- shared deps

# Only these operators can make two requirements incompatible: lower bounds alone always
# meet at the newest release, so they never cost an extra index fetch.
_BOUNDING = frozenset({"<", "<=", "==", "~=", "==="})


def shared_requirements(
    picks: dict[str, Pick], metas: dict[str, PypiMeta], env: dict[str, str]
) -> dict[str, list[tuple[Pick, SpecifierSet]]]:
    """Requirements on a project nobody requested, declared by two or more picks.

    The installer keeps one version of it, so the specifiers must intersect. Lines of one
    pick that apply together (several markers) are merged into one specifier.
    """
    found: dict[str, dict[str, tuple[Pick, SpecifierSet]]] = {}
    for source in picks.values():
        meta = metas.get(source.name)
        if meta is None:
            continue
        for line in meta["requires_dist"]:
            try:
                req = Requirement(line)
            except InvalidRequirement:
                continue
            name = canonicalize_name(req.name)
            if name in picks or not marker_applies(req.marker, env, source.spec.extras):
                continue
            by_source = found.setdefault(name, {})
            previous = by_source.get(source.name, (source, SpecifierSet()))[1]
            by_source[source.name] = (source, previous & req.specifier)
    return {
        name: list(by_source.values())
        for name, by_source in sorted(found.items())
        if len(by_source) > 1
        and any(s.operator in _BOUNDING for _, spec in by_source.values() for s in spec)
    }


def satisfiable(index: PypiIndex, specs: list[SpecifierSet]) -> bool:
    """Some stable, non-yanked version is inside every specifier."""
    return any(
        "yanked" not in version_info(index, version)
        and all(spec.contains(version, prereleases=True) for spec in specs)
        for version in index["versions"]
    )


def check_shared(
    shared: dict[str, list[tuple[Pick, SpecifierSet]]], indexes: dict[str, PypiIndex]
) -> list[tuple[Issue, str]]:
    """``shared`` issues (with the shared project's name) when no version fits everyone."""
    issues: list[tuple[Issue, str]] = []
    for name, constraints in shared.items():
        index = indexes.get(name)
        if index is None or satisfiable(index, [spec for _, spec in constraints]):
            continue  # unknown is never a conflict
        issue: Issue = {
            "kind": "shared",
            "ecosystem": "pypi",
            "package": name,
            "required_by": ", ".join(source.label for source, _ in constraints),
            "message": f"no stable {name} satisfies "
            + " and ".join(f"{source.label} ({spec or 'any'})" for source, spec in constraints),
        }
        issues.append((issue, name))
    return issues


async def find_shared_alternative(
    fetcher: Fetcher,
    constraints: list[tuple[Pick, SpecifierSet]],
    index: PypiIndex,
    env: dict[str, str],
) -> dict[str, str] | None:
    """Highest version of one constraining pick that lets the shared project resolve."""
    name = index["name"]
    for source, _ in constraints:
        others = [spec for other, spec in constraints if other is not source]

        def accept(
            meta: PypiMeta, source: Pick = source, others: list[SpecifierSet] = others
        ) -> bool:
            own = requirements_on(meta, name, env, source.spec.extras)
            return satisfiable(index, others + own)

        if version := await _older_version(fetcher, source, accept):
            return {source.name: version}
    return None


def parse_specs(raw_specs: list[str], errors: dict[str, str]) -> list[PypiSpec]:
    specs: dict[str, PypiSpec] = {}
    for raw in raw_specs:
        try:
            spec = parse_spec(raw)
        except ValueError as exc:
            errors[raw.strip() or "<empty>"] = str(exc)
            continue
        if spec.name in specs:
            errors[raw.strip()] = "duplicate package ignored"
            continue
        specs[spec.name] = spec
    return list(specs.values())


async def check_pypi(fetcher: Fetcher, raw_specs: list[str], python: str | None) -> EcosystemResult:
    result = EcosystemResult()
    specs = parse_specs(raw_specs, result.errors)
    indexes: dict[str, PypiIndex] = {}

    async def load_index(spec: PypiSpec) -> None:
        try:
            indexes[spec.name] = await fetch_index(fetcher, spec.name)
        except NotFoundError:
            result.errors[spec.name] = "not found"
        except FetchError as exc:
            result.errors[spec.name] = str(exc)

    await asyncio.gather(*(load_index(spec) for spec in specs))

    picks: dict[str, Pick] = {}
    for spec in specs:
        if spec.name not in indexes:
            continue
        index = indexes[spec.name]
        if not index["versions"]:
            result.errors[spec.name] = "no stable version published"
            continue
        version, issues = select(index, spec)
        result.issues += issues
        if version is None:
            latest = next(reversed(index["versions"]))
            result.issues.append(
                {
                    "kind": "unsatisfiable",
                    "ecosystem": "pypi",
                    "package": spec.name,
                    "range": str(spec.specifier),
                    "message": f"no stable version matches; latest is {latest}",
                }
            )
            continue
        picks[spec.name] = Pick(spec, version, index)

    metas: dict[str, PypiMeta] = {}

    async def load_meta(item: Pick) -> None:
        try:
            metas[item.name] = await fetch_meta(fetcher, item.name, item.version)
        except FetchError as exc:
            result.errors[item.name] = f"dependency metadata unavailable: {exc}"

    await asyncio.gather(*(load_meta(item) for item in picks.values()))

    env_python = python or ".".join(str(p) for p in sys.version_info[:3])
    env = marker_env(env_python)
    requires, pending = check_requires(picks, metas, env)
    result.issues += requires
    if python:
        python_issues, failed = check_python(picks, python)
        result.issues += python_issues + check_wheels(picks, python, failed)
    result.issues += check_status(picks)

    shared = shared_requirements(picks, metas, env)
    shared_indexes: dict[str, PypiIndex] = {}

    async def load_shared(name: str) -> None:
        with contextlib.suppress(FetchError, ValueError):
            shared_indexes[name] = await fetch_index(fetcher, validate_name(name))

    await asyncio.gather(*(load_shared(name) for name in shared))
    shared_issues = check_shared(shared, shared_indexes)
    result.issues += [issue for issue, _ in shared_issues]

    alternatives = await asyncio.gather(*(find_alternative(fetcher, r, env) for r in pending))
    for request, alternative in zip(pending, alternatives, strict=True):
        if alternative:
            request.issue["alternative"] = {request.source.name: alternative}
    shared_alternatives = await asyncio.gather(
        *(
            find_shared_alternative(fetcher, shared[name], shared_indexes[name], env)
            for _, name in shared_issues
        )
    )
    for (issue, _), found in zip(shared_issues, shared_alternatives, strict=True):
        if found:
            issue["alternative"] = found

    result.picks = {name: item.version for name, item in picks.items()}
    result.pins = [item.spec.pin(item.version) for item in picks.values()]
    result.specs = [spec.raw for spec in specs if spec.name in picks]
    return result
