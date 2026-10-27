"""Runtimes and LTS data from endoflife.date (API v1).

Reduced product document::

    {"name": "nodejs", "releases": [{"cycle": "24", "released": "2025-05-06",
      "latest": "24.22.1", "lts": true, "lts_from": "2025-10-28",
      "eol": "2028-04-30", "maintained": true}, ...]}

LTS status is recomputed against ``today`` rather than trusted from ``isLts``, because
cached data outlives the day it was generated (Node 26 turns LTS on 2026-10-28).
Java targets use ``eclipse-temurin``: vendor-neutral OpenJDK builds with LTS flags.
"""

import asyncio
import re
from datetime import UTC, date, datetime
from typing import Any, NotRequired, TypedDict, cast

from stackcheck_mcp.cache import Fetcher
from stackcheck_mcp.http import FetchError, NotFoundError
from stackcheck_mcp.models import NextLts, RuntimeInfo

EOL_API = "https://endoflife.date/api/v1"
TARGET_SLUGS = {"node": "nodejs", "python": "python", "java": "eclipse-temurin"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")


class Release(TypedDict):
    cycle: str
    released: NotRequired[str]
    latest: str
    lts: NotRequired[bool]
    lts_from: NotRequired[str]
    eol: NotRequired[str]
    maintained: NotRequired[bool]


class Product(TypedDict):
    name: str
    releases: list[Release]


class ProductRef(TypedDict):
    name: str
    aliases: list[str]


def utc_today() -> date:
    return datetime.now(UTC).date()


def _date(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- reduce


def reduce_product(raw: Any) -> Product:
    try:
        result = raw["result"]
        releases: list[Release] = []
        for item in result["releases"]:
            cycle = str(item["name"])
            latest = item.get("latest") or {}
            release: Release = {"cycle": cycle, "latest": str(latest.get("name") or cycle)}
            if _date(item.get("releaseDate")):
                release["released"] = item["releaseDate"][:10]
            if item.get("isLts"):
                release["lts"] = True
            if _date(item.get("ltsFrom")):
                release["lts_from"] = item["ltsFrom"][:10]
            if _date(item.get("eolFrom")):
                release["eol"] = item["eolFrom"][:10]
            if item.get("isMaintained"):
                release["maintained"] = True
            releases.append(release)
        return {"name": str(result["name"]), "releases": releases}
    except (KeyError, TypeError, AttributeError) as exc:
        raise FetchError(f"unexpected endoflife.date response: {exc}") from exc


def reduce_product_list(raw: Any) -> list[ProductRef]:
    try:
        return [
            {"name": str(item["name"]), "aliases": [str(a) for a in item.get("aliases") or []]}
            for item in raw["result"]
        ]
    except (KeyError, TypeError) as exc:
        raise FetchError(f"unexpected endoflife.date product list: {exc}") from exc


# --------------------------------------------------------------------------- decide


def sorted_releases(product: Product, today: date) -> list[Release]:
    """Released cycles, newest first (by release date; the API order is not trusted)."""
    released = [
        r for r in product["releases"] if (d := _date(r.get("released"))) is None or d <= today
    ]
    return sorted(released, key=lambda r: r.get("released", ""), reverse=True)


def lts_active(release: Release, today: date) -> bool:
    lts_from = _date(release.get("lts_from"))
    if lts_from is not None:
        return lts_from <= today
    return bool(release.get("lts"))


def active_lts(product: Product, today: date) -> Release | None:
    return next((r for r in sorted_releases(product, today) if lts_active(r, today)), None)


def has_lts(product: Product) -> bool:
    return any(r.get("lts") or r.get("lts_from") for r in product["releases"])


def next_lts(product: Product, today: date) -> Release | None:
    upcoming = [
        (d, r)
        for r in product["releases"]
        if (d := _date(r.get("lts_from"))) is not None and d > today
    ]
    return min(upcoming, key=lambda pair: pair[0])[1] if upcoming else None


def latest_maintained(product: Product, today: date) -> Release | None:
    return next((r for r in sorted_releases(product, today) if r.get("maintained")), None)


def recommended(product: Product, today: date) -> Release | None:
    """The cycle a new project should use: active LTS, else newest maintained, else newest."""
    releases = sorted_releases(product, today)
    if has_lts(product) and (lts := active_lts(product, today)) is not None:
        return lts
    return latest_maintained(product, today) or (releases[0] if releases else None)


def runtime_info(product: Product, today: date) -> RuntimeInfo:
    info: RuntimeInfo = {}
    releases = sorted_releases(product, today)
    if releases:
        info["latest"] = releases[0]["latest"]
    if has_lts(product) and (lts := active_lts(product, today)) is not None:
        info["lts"] = lts["latest"]
    if (upcoming := next_lts(product, today)) is not None:
        info["next_lts"] = cast(
            NextLts, {"version": upcoming["cycle"], "from": upcoming.get("lts_from", "")}
        )
    maintained = [r["cycle"] for r in releases if r.get("maintained")][:5]
    if maintained:
        info["maintained"] = maintained
    rec = recommended(product, today)
    if rec is not None and "eol" in rec:
        info["eol"] = rec["eol"]
    return info


def default_target(kind: str, product: Product, today: date) -> Release | None:
    """Node and Java: active LTS. Python: newest maintained released cycle."""
    if kind == "python":
        return latest_maintained(product, today)
    return active_lts(product, today) or latest_maintained(product, today)


def resolve_target(kind: str, product: Product, spec: str | None, today: date) -> str:
    """Full version for a target: default cycle, a cycle name (``"24"``, ``"3.14"``), a
    cycle prefix (``"3"`` → newest 3.x) or an exact version (``"24.1.0"``, kept as is)."""
    if spec is None or not spec.strip():
        release = default_target(kind, product, today)
        if release is None:
            raise ValueError(f"no default {kind} release found")
        return release["latest"]
    spec = spec.strip().removeprefix("v")
    releases = sorted_releases(product, today)
    for release in releases:
        if release["cycle"] == spec:
            return release["latest"]
    for release in releases:
        if release["cycle"].startswith(spec + "."):
            return release["latest"]
    for release in releases:
        if spec.startswith(release["cycle"] + ".") and re.fullmatch(r"[0-9][0-9A-Za-z.+_-]*", spec):
            return spec
    known = ", ".join(r["cycle"] for r in releases[:6])
    raise ValueError(f"unknown {kind} version {spec!r} (known cycles: {known})")


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def suggest_slugs(query: str, products: list[ProductRef], limit: int = 3) -> list[str]:
    """Closest product slugs: substring matches first, then edit distance."""
    scored: dict[str, tuple[int, int]] = {}
    for product in products:
        for candidate in [product["name"], *product["aliases"]]:
            substring = 0 if query in candidate or candidate in query else 1
            score = (substring, levenshtein(query, candidate))
            if product["name"] not in scored or score < scored[product["name"]]:
                scored[product["name"]] = score
    ranked = sorted(scored.items(), key=lambda item: (item[1], item[0]))
    return [name for name, (substring, dist) in ranked if substring == 0 or dist <= 4][:limit]


# --------------------------------------------------------------------------- fetch


def normalize_slug(slug: str) -> str:
    value = slug.strip().lower()
    if not _SLUG.match(value):
        raise ValueError("invalid product slug")
    return value


async def fetch_product(fetcher: Fetcher, slug: str) -> Product:
    async def load() -> Product:
        return reduce_product(await fetcher.http.get_json(f"{EOL_API}/products/{slug}"))

    return cast(Product, await fetcher.cached("eol", slug, load))


async def fetch_product_list(fetcher: Fetcher) -> list[ProductRef]:
    async def load() -> list[ProductRef]:
        return reduce_product_list(await fetcher.http.get_json(f"{EOL_API}/products"))

    return cast(list[ProductRef], await fetcher.cached("eol", "_products", load))


async def _unknown_product_message(fetcher: Fetcher, slug: str) -> str:
    try:
        suggestions = suggest_slugs(slug, await fetch_product_list(fetcher))
    except FetchError:
        suggestions = []
    if suggestions:
        return f"unknown product; did you mean: {', '.join(suggestions)}"
    return "unknown product"


async def lookup_product(fetcher: Fetcher, slug: str) -> Product:
    """Fetch a product, turning a 404 into a ``ValueError`` with slug suggestions."""
    try:
        return await fetch_product(fetcher, normalize_slug(slug))
    except NotFoundError:
        raise ValueError(await _unknown_product_message(fetcher, slug.strip().lower())) from None


async def runtime_infos(
    fetcher: Fetcher, slugs: list[str], today: date
) -> tuple[dict[str, RuntimeInfo], dict[str, str]]:
    infos: dict[str, RuntimeInfo] = {}
    errors: dict[str, str] = {}

    async def one(slug: str) -> None:
        try:
            infos[slug] = runtime_info(await lookup_product(fetcher, slug), today)
        except (ValueError, FetchError) as exc:
            errors[slug] = str(exc)

    unique = list(dict.fromkeys(s.strip() for s in slugs if s.strip()))
    await asyncio.gather(*(one(slug) for slug in unique))
    return {slug: infos[slug] for slug in unique if slug in infos}, errors


async def resolve_targets(
    fetcher: Fetcher, wanted: dict[str, str | None], today: date
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve ``{"node": "24" | None, ...}`` to full versions; errors keyed by target."""
    resolved: dict[str, str] = {}
    errors: dict[str, str] = {}

    async def one(kind: str, spec: str | None) -> None:
        try:
            product = await lookup_product(fetcher, TARGET_SLUGS[kind])
            resolved[kind] = resolve_target(kind, product, spec, today)
        except (ValueError, FetchError) as exc:
            errors[kind] = str(exc)

    await asyncio.gather(*(one(kind, spec) for kind, spec in wanted.items()))
    return {k: resolved[k] for k in TARGET_SLUGS if k in resolved}, errors
