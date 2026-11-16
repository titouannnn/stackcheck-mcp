"""npm: fetch abbreviated metadata → reduce → pick latest stable → peer/engines checks.

Reduced document (stable versions only; identical per-version metadata is stored once
in ``meta`` and referenced by index, which keeps ``vite``/``vitest`` at a few KB)::

    {"name": "typescript-eslint", "latest": "8.72.1",
     "versions": {"8.72.0": 0, "8.72.1": 0},
     "meta": [{"peer": {"typescript": ">=4.8.4 <6.1.0", ...}, "optional": ["..."],
               "engines": {"node": "..."}, "deprecated": "..."}]}
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict, cast
from urllib.parse import quote

from stackcheck_mcp.cache import Fetcher
from stackcheck_mcp.http import FetchError, NotFoundError
from stackcheck_mcp.models import EcosystemResult, Issue
from stackcheck_mcp.semver_utils import (
    highest_satisfying,
    is_evaluable,
    is_stable,
    satisfies,
    sort_versions,
    version_key,
)

REGISTRY = "https://registry.npmjs.org"
ABBREVIATED = "application/vnd.npm.install-v1+json"
_NAME = re.compile(r"^(?:@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*$", re.IGNORECASE)
_DIST_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


class NpmVersion(TypedDict):
    peer: NotRequired[dict[str, str]]
    optional: NotRequired[list[str]]
    engines: NotRequired[dict[str, str]]
    deprecated: NotRequired[str]


class NpmDoc(TypedDict):
    name: str
    latest: NotRequired[str]
    versions: dict[str, int]
    meta: list[NpmVersion]


def version_info(doc: NpmDoc, version: str) -> NpmVersion:
    return doc["meta"][doc["versions"][version]]


@dataclass(frozen=True)
class NpmSpec:
    name: str
    range: str | None = None


# --------------------------------------------------------------------------- parse


def validate_name(name: str) -> str:
    if not name or len(name) > 214 or not _NAME.match(name):
        raise ValueError("invalid npm package name")
    return name


def parse_spec(spec: str) -> NpmSpec:
    """``name`` or ``name@range``; scoped names split on the last ``@`` past index 0."""
    text = spec.strip()
    at = text.rfind("@")
    name, rng = (text[:at], text[at + 1 :].strip()) if at > 0 else (text, "")
    validate_name(name)
    if rng == "latest":  # what agents write most; the latest stable is the default anyway
        rng = ""
    if rng and not is_evaluable(rng):
        if _DIST_TAG.match(rng):
            raise ValueError(
                f"dist-tag {rng!r} not supported (only 'latest': other tags usually point "
                "to pre-releases); use a semver range"
            )
        raise ValueError(f"invalid version range {rng!r}")
    return NpmSpec(name, rng or None)


def package_url(name: str) -> str:
    return f"{REGISTRY}/{quote(validate_name(name), safe='@')}"


# --------------------------------------------------------------------------- reduce


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def reduce_doc(raw: Any) -> NpmDoc:
    if isinstance(raw, dict) and "versions" not in raw and "dist-tags" not in raw:
        # npm keeps only the name and a timestamp of an unpublished package.
        raise NotFoundError("unpublished")
    try:
        versions_raw = raw["versions"]
        tags = raw.get("dist-tags") or {}
        if not isinstance(versions_raw, dict):
            raise TypeError("versions is not an object")
    except (KeyError, TypeError, AttributeError) as exc:
        raise FetchError(f"unexpected npm response: {exc}") from exc
    versions: dict[str, int] = {}
    table: list[NpmVersion] = []
    index: dict[str, int] = {}
    for version in sort_versions([v for v in versions_raw if is_stable(v)]):
        meta = versions_raw[version] if isinstance(versions_raw[version], dict) else {}
        info: NpmVersion = {}
        if peer := _str_map(meta.get("peerDependencies")):
            info["peer"] = peer
        peer_meta = meta.get("peerDependenciesMeta")
        if isinstance(peer_meta, dict):
            optional = sorted(
                k for k, v in peer_meta.items() if isinstance(v, dict) and v.get("optional")
            )
            if optional:
                info["optional"] = optional
        if engines := _str_map(meta.get("engines")):
            info["engines"] = engines
        deprecated = meta.get("deprecated")
        if deprecated:
            info["deprecated"] = deprecated if isinstance(deprecated, str) else "deprecated"
        key = json.dumps(info, sort_keys=True)
        if key not in index:
            index[key] = len(table)
            table.append(info)
        versions[version] = index[key]
    doc: NpmDoc = {"name": str(raw.get("name", "")), "versions": versions, "meta": table}
    if isinstance(tags.get("latest"), str):
        doc["latest"] = tags["latest"]
    return doc


# --------------------------------------------------------------------------- decide


def candidates(doc: NpmDoc) -> list[str]:
    """Stable versions, ascending, capped by ``dist-tags.latest`` when it is stable."""
    versions = list(doc["versions"])
    latest = doc.get("latest")
    if latest and is_stable(latest):
        cap = version_key(latest)
        versions = [v for v in versions if version_key(v) <= cap]
    return sort_versions(versions)


def pick(doc: NpmDoc, user_range: str | None) -> str | None:
    return highest_satisfying(candidates(doc), [user_range] if user_range else [])


@dataclass(frozen=True)
class Pick:
    name: str
    version: str
    range: str | None
    doc: NpmDoc

    @property
    def info(self) -> NpmVersion:
        return version_info(self.doc, self.version)

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


def _peer_constraints(picks: dict[str, Pick], target: str) -> list[tuple[Pick, str]]:
    """Evaluable peer ranges that other requested packages put on ``target``.

    Optional peers count too: the peer is part of the request, so the range applies.
    """
    found = []
    for source in picks.values():
        if source.name == target:
            continue
        spec = source.info.get("peer", {}).get(target)
        if spec is not None and is_evaluable(spec):
            found.append((source, spec))
    return found


def check_peers(picks: dict[str, Pick]) -> list[Issue]:
    issues: list[Issue] = []
    for constrained in picks.values():
        constraints = _peer_constraints(picks, constrained.name)
        failing = [
            (src, spec) for src, spec in constraints if not satisfies(constrained.version, spec)
        ]
        if not failing:
            continue
        own = [constrained.range] if constrained.range else []
        suggestion = highest_satisfying(
            candidates(constrained.doc), own + [spec for _, spec in constraints]
        )
        for source, spec in failing:
            issue: Issue = {
                "kind": "peer",
                "ecosystem": "npm",
                "package": constrained.label,
                "required_by": source.label,
                "range": spec,
            }
            if suggestion:
                issue["suggestion"] = {constrained.name: suggestion}
            elif alternative := _peer_alternative(source, constrained):
                issue["alternative"] = {source.name: alternative}
            issues.append(issue)
    return issues


def _peer_alternative(source: Pick, constrained: Pick) -> str | None:
    """Highest version of ``source`` whose declared peer range accepts ``constrained``."""
    for version in reversed(candidates(source.doc)):
        if source.range and not satisfies(version, source.range):
            continue
        spec = version_info(source.doc, version).get("peer", {}).get(constrained.name)
        if spec is not None and satisfies(constrained.version, spec):
            return version
    return None


def check_engines(picks: dict[str, Pick], node: str) -> list[Issue]:
    issues: list[Issue] = []
    for item in picks.values():
        spec = item.info.get("engines", {}).get("node")
        if spec is None or satisfies(node, spec) is not False:
            continue
        issue: Issue = {
            "kind": "engines",
            "ecosystem": "npm",
            "package": item.label,
            "range": spec,
            "target": f"node {node}",
        }
        for version in reversed(candidates(item.doc)):
            if item.range and not satisfies(version, item.range):
                continue
            declared = version_info(item.doc, version).get("engines", {}).get("node")
            if declared is None or satisfies(node, declared) is not False:
                issue["suggestion"] = {item.name: version}
                break
        issues.append(issue)
    return issues


def check_deprecated(picks: dict[str, Pick]) -> list[Issue]:
    issues: list[Issue] = []
    for item in picks.values():
        message = item.info.get("deprecated")
        if not message:
            continue
        issue: Issue = {
            "kind": "deprecated",
            "ecosystem": "npm",
            "package": item.label,
            "severity": "warn",
            "message": message[:300],
        }
        for version in reversed(candidates(item.doc)):
            in_range = not item.range or satisfies(version, item.range)
            if in_range and not version_info(item.doc, version).get("deprecated"):
                issue["suggestion"] = {item.name: version}
                break
        issues.append(issue)
    return issues


def decide(
    specs: list[NpmSpec], docs: dict[str, NpmDoc], node: str | None
) -> tuple[dict[str, Pick], list[Issue], dict[str, str]]:
    """Pure core: picks, issues and per-package errors from reduced documents."""
    picks: dict[str, Pick] = {}
    issues: list[Issue] = []
    errors: dict[str, str] = {}
    for spec in specs:
        doc = docs[spec.name]
        if not candidates(doc):
            errors[spec.name] = "no stable version published"
            continue
        version = pick(doc, spec.range)
        if version is None:
            issues.append(
                {
                    "kind": "unsatisfiable",
                    "ecosystem": "npm",
                    "package": spec.name,
                    "range": spec.range or "",
                    "message": f"no stable version matches; latest is {candidates(doc)[-1]}",
                }
            )
            continue
        picks[spec.name] = Pick(spec.name, version, spec.range, doc)
    issues += check_peers(picks)
    if node:
        issues += check_engines(picks, node)
    issues += check_deprecated(picks)
    return picks, issues, errors


# --------------------------------------------------------------------------- fetch


async def fetch_doc(fetcher: Fetcher, name: str) -> NpmDoc:
    async def load() -> NpmDoc:
        raw = await fetcher.http.get_json(package_url(name), headers={"Accept": ABBREVIATED})
        return reduce_doc(raw)

    return cast(NpmDoc, await fetcher.cached("npm", name, load))


def parse_specs(raw_specs: list[str], errors: dict[str, str]) -> list[NpmSpec]:
    specs: dict[str, NpmSpec] = {}
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


async def check_npm(fetcher: Fetcher, raw_specs: list[str], node: str | None) -> EcosystemResult:
    result = EcosystemResult()
    specs = parse_specs(raw_specs, result.errors)
    docs: dict[str, NpmDoc] = {}

    async def load(spec: NpmSpec) -> None:
        try:
            docs[spec.name] = await fetch_doc(fetcher, spec.name)
        except FetchError as exc:  # "not found", "unpublished", "HTTP 503"...
            result.errors[spec.name] = str(exc)

    await asyncio.gather(*(load(spec) for spec in specs))
    found = [spec for spec in specs if spec.name in docs]
    picks, issues, errors = decide(found, docs, node)
    result.errors.update(errors)
    result.picks = {name: item.version for name, item in picks.items()}
    result.issues = issues
    result.pins = [item.label for item in picks.values()]
    result.specs = [f"{s.name}@{s.range}" if s.range else s.name for s in found]
    return result
