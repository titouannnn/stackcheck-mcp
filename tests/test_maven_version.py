"""Maven ordering, checked against every example of the POM reference's
"Version Order Specification" (https://maven.apache.org/pom.html)."""

import pytest

from stackcheck_mcp.maven_version import (
    compare,
    in_range,
    is_stable,
    parse,
    parse_range,
    sort_versions,
)


@pytest.mark.parametrize(
    ("version", "canonical"),
    [
        ("1.0.0", [1]),
        ("1.ga", [1]),
        ("1.final", [1]),
        ("1.0", [1]),
        ("1.", [1]),
        ("1-", [1]),
        ("1_", [1]),
        ("1.0.0-foo.0.0", [1, ["foo"]]),
        ("1.0.0-0.0.0", [1]),
        # the spec writes this as 1-1.foo-bar-1-baz-0.1
        ("1-1.foo-bar1baz-.1", [1, [1, "foo", ["bar", [1, ["baz", [0, 1]]]]]]),
    ],
)
def test_trimming(version: str, canonical: list[object]) -> None:
    assert parse(version) == canonical


@pytest.mark.parametrize(
    ("smaller", "larger"),
    [
        ("1", "1.1"),
        ("1-snapshot", "1"),
        ("1", "1-sp"),
        ("1-foo2", "1-foo10"),
        ("1-foo", "1-1"),
        ("1-1", "1.1"),
        ("1-ga", "1-sp"),
        ("1-ga.1", "1-sp.1"),
        ("1-sp-1", "1-ga-1"),
        ("1.K", "1.7"),
        ("5.aardvark", "5.zebra"),
        ("1.b", "1." + chr(0x3B1)),  # Greek alpha sorts after b
        # alpha < a1 < beta < b1 < milestone < m1 < rc = cr < snapshot < "" < sp
        ("1-alpha", "1-a1"),
        ("1-a1", "1-beta"),
        ("1-beta", "1-b1"),
        ("1-b1", "1-milestone"),
        ("1-milestone", "1-m1"),
        ("1-m1", "1-rc"),
        ("1-rc", "1-snapshot"),
        ("1-snapshot", "1"),
        # everyday cases
        ("6.0.9", "6.0.10"),
        ("4.2.0-RC1", "4.2.0"),
        ("4.1.2", "4.2.0-M1"),
        ("2.0-rc1", "2.0"),
        ("1.0.0.RELEASE", "1.0.1.RELEASE"),
        ("33.3.1-android", "33.3.1-jre"),
    ],
)
def test_ordering(smaller: str, larger: str) -> None:
    assert compare(smaller, larger) < 0
    assert compare(larger, smaller) > 0


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1.foo", "1-foo"),
        ("1.ga", "1-ga"),
        ("1-ga", "1-0"),
        ("1-0", "1_0"),
        ("1_0", "1.0"),
        ("1.0", "1"),
        ("1-a1", "1-alpha-1"),
        ("1.0-alpha1", "1.0-ALPHA1"),
        ("1-rc", "1-cr"),
        ("1.0.0.RELEASE", "1.0.0"),
        ("2.0.0.Final", "2.0.0"),
    ],
)
def test_equality(a: str, b: str) -> None:
    assert compare(a, b) == 0


def test_sort_is_never_lexicographic() -> None:
    assert sort_versions(["3.10.0", "3.9.0", "3.2.0-RC1", "3.2.0"]) == [
        "3.2.0-RC1",
        "3.2.0",
        "3.9.0",
        "3.10.0",
    ]


@pytest.mark.parametrize(
    "version",
    [
        "4.2.0-RC1",
        "4.1.0-M4",
        "1.0.M1",
        "2.0.0.CR2",
        "1.0-SNAPSHOT",
        "3.0.0-beta-1",
        "25-ea",
        "1.0-alpha",
        "1.0-a1",
        "1.0.b2",
        "7.0.0-preview.3",
        "1.0-milestone-2",
        "1.0-Beta",
    ],
)
def test_prereleases(version: str) -> None:
    assert not is_stable(version)


@pytest.mark.parametrize(
    "version",
    [
        "4.1.2",
        "1.0.0.RELEASE",
        "2.0.0.Final",
        "33.3.1-jre",
        "33.3.1-android",
        "1.0-sp1",
        "5.3.ga",
        "2.22.4",
        "1.0-m",
        "1.0-release-candidate-free",
        "9.4.58.v20250814",
    ],
)
def test_stable(version: str) -> None:
    assert is_stable(version)


def test_ranges() -> None:
    assert in_range("1.5", parse_range("[1.0,2.0)"))
    assert not in_range("2.0", parse_range("[1.0,2.0)"))
    assert in_range("2.0-rc1", parse_range("[1.0,2.0)"))  # Maven semantics
    assert in_range("1.0", parse_range("(,1.0]")) and not in_range("1.1", parse_range("(,1.0]"))
    assert in_range("9.9", parse_range("[1.5,)")) and not in_range("1.4", parse_range("[1.5,)"))
    assert in_range("1.2", parse_range("[1.2]")) and not in_range("1.2.1", parse_range("[1.2]"))
    union = parse_range("[1,2),[3,4)")
    assert in_range("1.5", union) and in_range("3.1", union) and not in_range("2.5", union)
    assert in_range("4.1.2", parse_range("4.1.2"))  # plain version: exact
    assert not in_range("4.1.3", parse_range("4.1.2"))
    for bad in ["[1,2", "1,2]", "[a b]", "[1,2,3]", "(1.0)", "", "[1,2) junk"]:
        with pytest.raises(ValueError):
            parse_range(bad)
