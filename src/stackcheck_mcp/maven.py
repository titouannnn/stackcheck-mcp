"""Maven Central: latest stable from ``maven-metadata.xml``, BOM alignment, Spring Boot Java.

Reduced metadata (``maven`` namespace, key ``group:artifact``): ``{"versions": [...]}``,
stable versions only, in Maven order. Reduced POM (``maven-pom`` namespace, key
``group:artifact:version``, immutable)::

    {"g": "...", "a": "...", "v": "...", "parent": "g:a:v", "props": {...},
     "managed": {"g:a": "raw version"}, "imports": ["g:a:raw version"],
     "deps": ["g:a:raw version (scope)"]}

Versions in a POM stay raw (``${...}``) because properties can come from a parent.
The Spring Boot Java baseline is the ``java.version`` property of
``spring-boot-starter-parent`` for that version (no hardcoded values).
"""

import asyncio
import re
import xml.etree.ElementTree as ET  # Maven Central XML; Expat >= 2.4 blocks entity bombs
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict, cast

from stackcheck_mcp.cache import Fetcher
from stackcheck_mcp.http import FetchError, NotFoundError
from stackcheck_mcp.maven_version import (
    Restriction,
    compare,
    in_range,
    is_stable,
    parse_range,
    sort_versions,
)
from stackcheck_mcp.models import EcosystemResult, Issue

CENTRAL = "https://repo1.maven.org/maven2"
BOM_DEPTH = 5
SPRING_BOOT_GROUP = "org.springframework.boot"
STARTER_PARENT = "spring-boot-starter-parent"
JAVA_SCAN = 40  # Spring Boot minor lines inspected, newest first, for a Java `alternative`
_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
# Early Apache artifacts were published under build dates (commons-collections 20040616,
# commons-lang 20030203.000129). Maven orders them above every real version.
_DATE_VERSION = re.compile(r"^(?:19|20)\d{6}(?:[.-]\d+)*$")


class MavenMeta(TypedDict):
    versions: list[str]


class Pom(TypedDict):
    g: str
    a: str
    v: str
    parent: NotRequired[str]
    props: dict[str, str]
    managed: dict[str, str]
    imports: list[str]
    deps: NotRequired[list[str]]


@dataclass(frozen=True)
class MavenSpec:
    group: str
    artifact: str
    range: str | None = None
    restrictions: tuple[Restriction, ...] = ()

    @property
    def ga(self) -> str:
        return f"{self.group}:{self.artifact}"


# --------------------------------------------------------------------------- parse


def parse_spec(raw: str) -> MavenSpec:
    """``groupId:artifactId[:version-or-range]``."""
    group, sep, rest = raw.strip().partition(":")
    artifact, _, rng = rest.partition(":")
    if not sep or not _ID.match(group) or not _ID.match(artifact) or ".." in group + artifact:
        raise ValueError("expected groupId:artifactId[:version or range]")
    rng = rng.strip()
    restrictions = tuple(parse_range(rng)) if rng else ()
    return MavenSpec(group, artifact, rng or None, restrictions)


def _path(group: str, artifact: str) -> str:
    return f"{CENTRAL}/{group.replace('.', '/')}/{artifact}"


# --------------------------------------------------------------------------- reduce


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _children(element: ET.Element | None) -> list[ET.Element]:
    return list(element) if element is not None else []


def _text(element: ET.Element | None, name: str) -> str | None:
    found = _child(element, name)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)  # noqa: S314 - no external entities are resolved
    except ET.ParseError as exc:
        raise FetchError(f"invalid XML: {exc}") from exc


def reduce_metadata(text: str) -> MavenMeta:
    versioning = _child(_xml(text), "versioning")
    versions_el = _child(versioning, "versions")
    versions = [v.text.strip() for v in _children(versions_el) if v.text and v.text.strip()]
    return {"versions": sort_versions([v for v in dict.fromkeys(versions) if is_stable(v)])}


def reduce_pom(text: str) -> Pom:
    root = _xml(text)
    parent_el = _child(root, "parent")
    parent = None
    if parent_el is not None:
        coords = [_text(parent_el, k) for k in ("groupId", "artifactId", "version")]
        if all(coords):
            parent = ":".join(cast(list[str], coords))
    props_el = _child(root, "properties")
    props = {
        _local(p.tag): (p.text or "").strip() for p in _children(props_el) if isinstance(p.tag, str)
    }
    managed: dict[str, str] = {}
    imports: list[str] = []
    deps = _child(_child(root, "dependencyManagement"), "dependencies")
    for dep in _children(deps):
        group, artifact = _text(dep, "groupId"), _text(dep, "artifactId")
        version = _text(dep, "version")
        if not (group and artifact and version):
            continue
        if _text(dep, "scope") == "import" and (_text(dep, "type") or "jar") == "pom":
            imports.append(f"{group}:{artifact}:{version}")
        else:
            managed.setdefault(f"{group}:{artifact}", version)
    deps_list = []
    for dep in _children(_child(root, "dependencies")):
        group, artifact = _text(dep, "groupId"), _text(dep, "artifactId")
        if not (group and artifact):
            continue
        scope = _text(dep, "scope") or "compile"
        if scope == "test":
            continue
        entry = f"{group}:{artifact}:{_text(dep, 'version') or '(managed)'}"
        deps_list.append(entry if scope == "compile" else f"{entry} ({scope})")
    group = _text(root, "groupId") or (parent.split(":")[0] if parent else "")
    version = _text(root, "version") or (parent.split(":")[2] if parent else "")
    pom: Pom = {
        "g": group,
        "a": _text(root, "artifactId") or "",
        "v": version,
        "props": props,
        "managed": managed,
        "imports": imports,
    }
    if parent:
        pom["parent"] = parent
    if deps_list:
        pom["deps"] = deps_list
    return pom


# --------------------------------------------------------------------------- decide


def interpolate(value: str, props: dict[str, str]) -> str:
    for _ in range(10):
        replaced = _PLACEHOLDER.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if replaced == value:
            break
        value = replaced
    return value


def is_date_version(version: str) -> bool:
    return _DATE_VERSION.match(version) is not None


def pick(meta: MavenMeta, spec: MavenSpec | None = None) -> str | None:
    """Highest stable version in the user's range.

    Build-date versions are skipped unless the artifact has nothing else or the user's
    range names one (``20040616``, ``[20040616,)``).
    """
    versions = meta["versions"]
    restrictions = list(spec.restrictions) if spec is not None else []
    bounds = [b for r in restrictions for b in (r.lower, r.upper) if b]
    if any(not is_date_version(v) for v in versions) and not any(map(is_date_version, bounds)):
        versions = [v for v in versions if not is_date_version(v)]
    if restrictions:
        versions = [v for v in versions if in_range(v, restrictions)]
    return versions[-1] if versions else None


@dataclass
class Effective:
    """A POM with its parent chain applied: merged properties, managed versions, imports."""

    props: dict[str, str]
    managed: dict[str, str] = field(default_factory=dict)
    imports: list[str] = field(default_factory=list)


def effective_model(chain: list[Pom]) -> Effective:
    """``chain`` = [pom, parent, grandparent, ...]; the child wins on every key."""
    props: dict[str, str] = {}
    for pom in reversed(chain):
        props.update(pom["props"])
    pom = chain[0]
    builtins = {
        "project.version": pom["v"],
        "pom.version": pom["v"],
        "version": pom["v"],
        "project.groupId": pom["g"],
        "pom.groupId": pom["g"],
        "groupId": pom["g"],
        "project.artifactId": pom["a"],
    }
    if "parent" in pom:
        parent_group, _, parent_version = pom["parent"].split(":")
        builtins |= {
            "project.parent.version": parent_version,
            "parent.version": parent_version,
            "project.parent.groupId": parent_group,
        }
    props |= builtins
    model = Effective(props)
    for item in chain:
        for ga, raw in item["managed"].items():
            model.managed.setdefault(ga, interpolate(raw, props))
        model.imports += [interpolate(raw, props) for raw in item["imports"]]
    return model


# --------------------------------------------------------------------------- fetch


async def fetch_metadata(fetcher: Fetcher, group: str, artifact: str) -> MavenMeta:
    async def load() -> MavenMeta:
        return reduce_metadata(
            await fetcher.http.get_text(f"{_path(group, artifact)}/maven-metadata.xml")
        )

    return cast(MavenMeta, await fetcher.cached("maven", f"{group}:{artifact}", load))


async def fetch_pom(fetcher: Fetcher, coords: str) -> Pom:
    group, artifact, version = coords.split(":")
    if not (_ID.match(group) and _ID.match(artifact) and _ID.match(version)):
        raise FetchError(f"unresolvable coordinates {coords}")

    async def load() -> Pom:
        url = f"{_path(group, artifact)}/{version}/{artifact}-{version}.pom"
        return reduce_pom(await fetcher.http.get_text(url))

    return cast(Pom, await fetcher.cached("maven-pom", coords, load, immutable=True))


async def load_effective(fetcher: Fetcher, coords: str) -> Effective:
    chain = [await fetch_pom(fetcher, coords)]
    while "parent" in chain[-1] and len(chain) < 10:
        try:
            chain.append(await fetch_pom(fetcher, chain[-1]["parent"]))
        except FetchError:
            break
    return effective_model(chain)


async def managed_versions(
    fetcher: Fetcher, bom: str, wanted: set[str], depth: int = 0, seen: frozenset[str] = frozenset()
) -> dict[str, str]:
    """BOM-managed versions of ``wanted`` (``g:a``): own entries first, then imports in
    declaration order (first declaration wins), recursively up to ``BOM_DEPTH``."""
    model = await load_effective(fetcher, bom)
    found = {ga: model.managed[ga] for ga in wanted if ga in model.managed}
    remaining = wanted - set(found)
    imports = [i for i in model.imports if i not in seen and "${" not in i]
    if not remaining or depth >= BOM_DEPTH or not imports:
        return found
    seen = seen | {bom, *imports}
    results = await asyncio.gather(
        *(managed_versions(fetcher, i, remaining, depth + 1, seen) for i in imports),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            continue
        for ga, version in result.items():
            if ga in remaining:
                found[ga] = version
                remaining.discard(ga)
    return found


async def java_baseline(fetcher: Fetcher, boot_version: str) -> int | None:
    """Spring Boot's Java baseline: ``java.version`` of its ``spring-boot-starter-parent``."""
    coords = f"{SPRING_BOOT_GROUP}:{STARTER_PARENT}:{boot_version}"
    try:
        pom = await fetch_pom(fetcher, coords)
        props = pom["props"]
        if "${" in props.get("java.version", "${"):
            # Only walk the (large) parent chain when the property is not set locally.
            props = (await load_effective(fetcher, coords)).props
    except FetchError:
        return None
    return java_major(interpolate(props.get("java.version", ""), props))


def java_major(version: str) -> int | None:
    """``17`` for "17", "17.0.2+8" or "21-ea"; ``8`` for the legacy "1.8" form."""
    match = re.match(r"^(?:1\.(?=\d))?(\d+)", version.strip())
    return int(match.group(1)) if match else None


async def check_java(fetcher: Fetcher, boot_versions: dict[str, str], java: str) -> list[Issue]:
    """``boot_versions``: label → Spring Boot version (artifacts of the group, or the BOM)."""
    target = java_major(java)
    if target is None:
        return []
    issues: list[Issue] = []
    alternative: str | None = None
    alternative_done = False
    for label, version in boot_versions.items():
        baseline = await java_baseline(fetcher, version)
        if baseline is None or baseline <= target:
            continue
        issue: Issue = {
            "kind": "java",
            "ecosystem": "maven",
            "package": label,
            "range": f">={baseline}",
            "target": f"java {java}",
        }
        if not alternative_done:
            alternative = await _boot_for_java(fetcher, target)
            alternative_done = True
        if alternative:
            issue["alternative"] = {label.rsplit(":", 1)[0]: alternative}
        issues.append(issue)
    return issues


async def _boot_for_java(fetcher: Fetcher, target: int) -> str | None:
    try:
        meta = await fetch_metadata(fetcher, SPRING_BOOT_GROUP, STARTER_PARENT)
    except FetchError:
        return None
    latest_per_line: dict[str, str] = {}
    for version in meta["versions"]:
        latest_per_line[".".join(version.split(".")[:2])] = version
    candidates = list(reversed(latest_per_line.values()))[:JAVA_SCAN]
    for start in range(0, len(candidates), 8):
        batch = candidates[start : start + 8]
        baselines = await asyncio.gather(*(java_baseline(fetcher, v) for v in batch))
        for version, baseline in zip(batch, baselines, strict=True):
            if baseline is not None and baseline <= target:
                return version
    return None


def parse_specs(raw_specs: list[str], errors: dict[str, str]) -> list[MavenSpec]:
    specs: dict[str, MavenSpec] = {}
    for raw in raw_specs:
        try:
            spec = parse_spec(raw)
        except ValueError as exc:
            errors[raw.strip() or "<empty>"] = str(exc)
            continue
        if spec.ga in specs:
            errors[raw.strip()] = "duplicate artifact ignored"
            continue
        specs[spec.ga] = spec
    return list(specs.values())


async def _pick(fetcher: Fetcher, spec: MavenSpec, result: EcosystemResult) -> str | None:
    try:
        meta = await fetch_metadata(fetcher, spec.group, spec.artifact)
    except NotFoundError:
        result.errors[spec.ga] = "not found"
        return None
    except FetchError as exc:
        result.errors[spec.ga] = str(exc)
        return None
    if not meta["versions"]:
        result.errors[spec.ga] = "no stable version published"
        return None
    version = pick(meta, spec)
    if version is None:
        result.issues.append(
            {
                "kind": "unsatisfiable",
                "ecosystem": "maven",
                "package": spec.ga,
                "range": spec.range or "",
                "message": f"no stable version matches; latest is {pick(meta)}",
            }
        )
    return version


async def check_maven(
    fetcher: Fetcher, raw_specs: list[str], raw_bom: str | None, java: str | None
) -> EcosystemResult:
    result = EcosystemResult()
    specs = parse_specs(raw_specs, result.errors)
    bom: MavenSpec | None = None
    if raw_bom and raw_bom.strip():
        try:
            bom = parse_spec(raw_bom)
        except ValueError as exc:
            result.errors[raw_bom.strip()] = str(exc)
    everything = specs + ([bom] if bom else [])
    versions = await asyncio.gather(*(_pick(fetcher, s, result) for s in everything))
    picks = {s.ga: v for s, v in zip(everything, versions, strict=True) if v is not None}
    requested = {s.ga for s in specs}

    if bom is not None and bom.ga in picks:
        bom_label = f"{bom.ga}:{picks[bom.ga]}"
        wanted = {ga for ga in requested if ga in picks}
        try:
            managed = await managed_versions(fetcher, bom_label, wanted)
        except FetchError as exc:
            result.errors[bom.ga] = f"BOM unavailable: {exc}"
            managed = {}
        for ga in [s.ga for s in specs if s.ga in managed]:
            version = managed[ga]
            if "${" in version or compare(version, picks[ga]) == 0:
                continue
            result.issues.append(
                {
                    "kind": "bom",
                    "ecosystem": "maven",
                    "package": f"{ga}:{picks[ga]}",
                    "required_by": bom_label,
                    "suggestion": {ga: version},
                    "severity": "warn",
                }
            )

    if java:
        boot = {
            f"{ga}:{version}": version
            for ga, version in picks.items()
            if ga.startswith(f"{SPRING_BOOT_GROUP}:")
        }
        unique: dict[str, str] = {}
        for label, version in boot.items():
            unique.setdefault(version, label)
        result.issues += await check_java(fetcher, {lbl: v for v, lbl in unique.items()}, java)

    order = [s.ga for s in everything]
    result.picks = {ga: picks[ga] for ga in order if ga in picks}
    return result
