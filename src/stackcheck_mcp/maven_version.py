"""Maven version ordering, ranges and the stability rule.

Ordering follows Maven's ``ComparableVersion`` as described in the POM reference
("Version Order Specification", https://maven.apache.org/pom.html): versions are split
into numeric and qualifier tokens; ``-`` and digit/letter transitions open a nested list;
trailing "null" tokens (``0``, ``""``, ``final``, ``ga``, ``release``) are trimmed;
qualifiers order ``alpha < beta < milestone < rc = cr < snapshot < "" < sp`` and then
unknown qualifiers lexically; ``a1``/``b1``/``m1`` are short for alpha/beta/milestone.
"""

import re
from dataclasses import dataclass
from functools import cmp_to_key, lru_cache
from typing import Union

QUALIFIERS = ["alpha", "beta", "milestone", "rc", "snapshot", "", "sp"]
ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
RELEASE_INDEX = str(QUALIFIERS.index(""))
PRERELEASE_WORDS = frozenset(
    {"alpha", "beta", "rc", "cr", "milestone", "snapshot", "preview", "ea"}
)


class _List(list["Item"]):
    """A nested token list (opened by ``-`` or a digit/letter transition)."""


Item = Union[int, str, _List]  # noqa: UP007 - recursive alias


def _qualifier(token: str, followed_by_digit: bool) -> str:
    if followed_by_digit and len(token) == 1:
        token = {"a": "alpha", "b": "beta", "m": "milestone"}.get(token, token)
    return ALIASES.get(token, token)


def _comparable(qualifier: str) -> str:
    if qualifier in QUALIFIERS:
        return str(QUALIFIERS.index(qualifier))
    return f"{len(QUALIFIERS)}-{qualifier}"


def _is_null(item: Item) -> bool:
    if isinstance(item, _List):
        return len(item) == 0
    return item == 0 or item == ""


def _normalize(items: _List) -> None:
    for i in range(len(items) - 1, -1, -1):
        if _is_null(items[i]):
            del items[i]
        elif not isinstance(items[i], _List):
            break


def _parse_item(digit: bool, text: str) -> Item:
    return int(text) if digit else _qualifier(text, followed_by_digit=False)


@lru_cache(maxsize=8192)
def parse(version: str) -> _List:
    text = version.lower()
    root = current = _List()
    stack = [root]
    digit = False
    start = 0

    def open_list() -> None:
        nonlocal current
        new = _List()
        current.append(new)
        current = new
        stack.append(new)

    for i, char in enumerate(text):
        if char == ".":
            current.append(0 if i == start else _parse_item(digit, text[start:i]))
            start = i + 1
        elif char in "-_":
            current.append(0 if i == start else _parse_item(digit, text[start:i]))
            start = i + 1
            open_list()
        elif char.isdigit() and char.isascii():
            if not digit and i > start:
                current.append(_qualifier(text[start:i], followed_by_digit=True))
                start = i
                open_list()
            digit = True
        else:
            if digit and i > start:
                current.append(int(text[start:i]))
                start = i
                open_list()
            digit = False
    if len(text) > start:
        if not digit and current:
            open_list()  # a trailing ".qualifier" sorts like "-qualifier"
        current.append(_parse_item(digit, text[start:]))
    while stack:
        _normalize(stack.pop())
    return root


def _compare_null(item: Item) -> int:
    """Compare ``item`` with an absent (padded) item."""
    if isinstance(item, int):
        return 0 if item == 0 else 1
    if isinstance(item, str):
        return (_comparable(item) > RELEASE_INDEX) - (_comparable(item) < RELEASE_INDEX)
    return 0 if not item else _compare_null(item[0])


def _compare(left: Item | None, right: Item | None) -> int:
    if left is None:
        return 0 if right is None else -_compare_null(right)
    if right is None:
        return _compare_null(left)
    if isinstance(left, int):
        if isinstance(right, int):
            return (left > right) - (left < right)
        return 1
    if isinstance(left, str):
        if isinstance(right, int | _List):
            return -1
        a, b = _comparable(left), _comparable(right)
        return (a > b) - (a < b)
    if isinstance(right, int):
        return -1
    if isinstance(right, str):
        return 1
    for i in range(max(len(left), len(right))):
        result = _compare(left[i] if i < len(left) else None, right[i] if i < len(right) else None)
        if result:
            return result
    return 0


def compare(a: str, b: str) -> int:
    return _compare(parse(a), parse(b))


version_key = cmp_to_key(compare)


def sort_versions(versions: list[str]) -> list[str]:
    return sorted(versions, key=version_key)


def is_stable(version: str) -> bool:
    """False for alpha/beta/rc/cr/milestone/snapshot/preview/ea and a1/b1/m1 shorthands."""
    tokens = re.findall(r"[a-z]+|\d+", version.lower())
    for i, token in enumerate(tokens):
        if token in PRERELEASE_WORDS:
            return False
        next_is_digit = i + 1 < len(tokens) and tokens[i + 1].isdigit()
        if token in ("a", "b", "m") and next_is_digit:
            return False
    return True


# --------------------------------------------------------------------------- ranges


@dataclass(frozen=True)
class Restriction:
    lower: str | None
    lower_inclusive: bool
    upper: str | None
    upper_inclusive: bool

    def contains(self, version: str) -> bool:
        if self.lower is not None:
            c = compare(version, self.lower)
            if c < 0 or (c == 0 and not self.lower_inclusive):
                return False
        if self.upper is not None:
            c = compare(version, self.upper)
            if c > 0 or (c == 0 and not self.upper_inclusive):
                return False
        return True


_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_GROUP = re.compile(r"([\[(])([^\[\]()]*)([\])])")


def parse_range(spec: str) -> list[Restriction]:
    """``[1.0,2.0)``, ``(,1.0]``, ``[1.5,)``, ``[1.0]``, unions; a plain version is exact."""
    text = spec.strip()
    if _VERSION.match(text):
        return [Restriction(text, True, text, True)]
    restrictions = []
    position = 0
    for match in _GROUP.finditer(text):
        if text[position : match.start()].strip(" ,"):
            raise ValueError(f"invalid version range {spec!r}")
        position = match.end()
        opening, body, closing = match.groups()
        parts = [p.strip() for p in body.split(",")]
        if len(parts) == 1:
            if opening != "[" or closing != "]" or not _VERSION.match(parts[0]):
                raise ValueError(f"invalid version range {spec!r}")
            restrictions.append(Restriction(parts[0], True, parts[0], True))
            continue
        if len(parts) != 2 or any(p and not _VERSION.match(p) for p in parts):
            raise ValueError(f"invalid version range {spec!r}")
        lower, upper = parts[0] or None, parts[1] or None
        restrictions.append(Restriction(lower, opening == "[", upper, closing == "]"))
    if not restrictions or text[position:].strip(" ,"):
        raise ValueError(f"invalid version range {spec!r}")
    return restrictions


def in_range(version: str, restrictions: list[Restriction]) -> bool:
    return any(r.contains(version) for r in restrictions)
