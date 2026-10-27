"""npm version helpers on top of ``node-semver`` (import name ``nodesemver``).

``nodesemver.satisfies`` returns ``False`` for ranges it cannot parse (``workspace:*``,
``npm:foo@1``, ``latest``, …), which would look like a conflict. Ranges are therefore
compiled first: anything containing ``:`` or rejected by ``make_range`` is *unevaluable*
and must never produce an issue.
"""

import re
from functools import lru_cache
from typing import Any

import nodesemver

STABLE = re.compile(r"^\d+\.\d+\.\d+$")


def is_stable(version: str) -> bool:
    return STABLE.match(version) is not None


def version_key(version: str) -> tuple[int, ...]:
    """Ordering key for stable versions: numeric, never lexicographic."""
    return tuple(int(part) for part in version.split("."))


def sort_versions(versions: list[str]) -> list[str]:
    return sorted(versions, key=version_key)


@lru_cache(maxsize=4096)
def compile_range(spec: str) -> Any | None:
    """A ``nodesemver.Range``, or ``None`` when the range is unevaluable."""
    text = spec.strip()
    if ":" in text:
        return None
    try:
        return nodesemver.make_range(text, True)
    except (ValueError, TypeError, AttributeError, IndexError):
        return None


def is_evaluable(spec: str) -> bool:
    return compile_range(spec) is not None


def satisfies(version: str, spec: str) -> bool | None:
    """``None`` when ``spec`` is unevaluable, else whether ``version`` is in range."""
    compiled = compile_range(spec)
    if compiled is None:
        return None
    try:
        return bool(compiled.test(version))
    except (ValueError, TypeError, AttributeError):
        return None


def highest_satisfying(versions: list[str], specs: list[str]) -> str | None:
    """Highest of ``versions`` (stable, any order) inside every evaluable spec."""
    for version in sorted(versions, key=version_key, reverse=True):
        if all(satisfies(version, spec) is not False for spec in specs):
            return version
    return None
