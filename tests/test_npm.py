import json
from typing import Any

import httpx2
import pytest

from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.npm import (
    NpmDoc,
    NpmSpec,
    candidates,
    check_npm,
    check_shared,
    decide,
    fetch_doc,
    parse_spec,
    pick,
    reduce_doc,
    shared_peers,
)
from stackcheck_mcp.semver_utils import is_evaluable, satisfies, sort_versions
from tests.conftest import mock_http
from tests.reference_stack import NPM


def doc(versions: dict[str, dict[str, Any]], latest: str | None = None) -> NpmDoc:
    """Build a reduced doc from a raw-like ``{version: metadata}`` map."""
    raw: dict[str, Any] = {"name": "x", "versions": versions}
    if latest:
        raw["dist-tags"] = {"latest": latest}
    return reduce_doc(raw)


async def load(fetcher: Fetcher, names: list[str]) -> dict[str, NpmDoc]:
    return {name: await fetch_doc(fetcher, name) for name in names}


# ----------------------------------------------------------------- reference stack


async def test_reference_stack_reports_only_the_typescript_peer_conflict(
    fetcher: Fetcher,
) -> None:
    result = await check_npm(fetcher, NPM, "24.22.1")
    assert result.errors == {}
    assert result.picks == {
        "react": "19.3.1",
        "react-dom": "19.3.1",
        "@types/react": "19.3.0",
        "@types/react-dom": "19.3.0",
        "typescript": "7.0.2",
        "typescript-eslint": "8.72.1",
        "eslint": "10.13.0",
        "vite": "8.3.5",
        "@vitejs/plugin-react": "6.1.2",
        "vitest": "5.0.5",
    }
    assert result.issues == [
        {
            "kind": "peer",
            "ecosystem": "npm",
            "package": "typescript@7.0.2",
            "required_by": "typescript-eslint@8.72.1",
            "range": ">=4.8.4 <6.1.0",
            "suggestion": {"typescript": "6.0.3"},
        }
    ]


async def test_user_range_steers_the_pick_and_clears_the_conflict(fetcher: Fetcher) -> None:
    result = await check_npm(fetcher, ["typescript@~6.0", "typescript-eslint"], None)
    assert result.picks == {"typescript": "6.0.3", "typescript-eslint": "8.72.1"}
    assert result.issues == []
    assert result.pins == ["typescript@6.0.3", "typescript-eslint@8.72.1"]
    assert result.specs == ["typescript@~6.0", "typescript-eslint"]


async def test_conflict_with_user_range_has_no_suggestion(fetcher: Fetcher) -> None:
    result = await check_npm(fetcher, ["typescript@^7", "typescript-eslint"], None)
    (issue,) = result.issues
    assert issue["kind"] == "peer"
    # No typescript satisfies both ^7 and <6.1.0, and no typescript-eslint accepts 7.
    assert "suggestion" not in issue and "alternative" not in issue


async def test_unsatisfiable_user_range(fetcher: Fetcher) -> None:
    result = await check_npm(fetcher, ["vite@^99"], None)
    assert result.picks == {}
    assert result.issues == [
        {
            "kind": "unsatisfiable",
            "ecosystem": "npm",
            "package": "vite",
            "range": "^99",
            "message": "no stable version matches; latest is 8.3.5",
        }
    ]


async def test_canaries_and_dist_tags_are_ignored(fetcher: Fetcher) -> None:
    docs = await load(fetcher, ["react", "@types/react", "typescript"])
    for item in docs.values():
        assert all(v.count(".") == 2 and "-" not in v for v in item["versions"])
    assert pick(docs["react"], None) == "19.3.1"  # not the canary/next/backport tags
    assert pick(docs["@types/react"], None) == "19.3.0"  # not a tsX.Y tag
    assert pick(docs["typescript"], "<7") == "6.0.3"


async def test_engines_against_old_node_target(fetcher: Fetcher) -> None:
    result = await check_npm(fetcher, ["vite", "eslint", "react"], "18.20.8")
    by_package = {i["package"]: i for i in result.issues}
    vite = by_package["vite@8.3.5"]
    assert vite["kind"] == "engines"
    assert vite["range"] == "^20.19.0 || >=22.12.0"
    assert vite["target"] == "node 18.20.8"
    # 6.4.5 declares engines.node "^18.0.0 || ^20.0.0 || >=22.0.0".
    assert vite["suggestion"] == {"vite": "6.4.5"}
    assert "eslint@10.13.0" in by_package
    assert "react@19.3.1" not in by_package  # engines >=0.10.0


async def test_deprecated_latest_is_flagged_not_hidden(fetcher: Fetcher) -> None:
    result = await check_npm(fetcher, ["request"], None)
    assert result.picks == {"request": "2.88.2"}
    (issue,) = result.issues
    assert issue["kind"] == "deprecated" and issue["severity"] == "warn"
    assert "request has been deprecated" in issue["message"]


# ----------------------------------------------------------------- pure rules


def test_version_ordering_is_numeric() -> None:
    assert sort_versions(["6.0.9", "6.0.10", "6.0.2"]) == ["6.0.2", "6.0.9", "6.0.10"]
    assert pick(doc({"6.0.9": {}, "6.0.10": {}}), None) == "6.0.10"
    assert satisfies("6.0.10", "~6.0.9") is True


def test_stable_filter_and_latest_cap() -> None:
    raw = {
        "8.3.5": {},
        "9.0.0": {},  # published under `next`, not promoted yet
        "9.1.0-beta.1": {},
        "8.4.0-canary.1": {},
        "8.3.6+build": {},
    }
    assert candidates(doc(raw, latest="8.3.5")) == ["8.3.5"]
    # When `latest` is itself a pre-release, use the highest stable version.
    assert pick(doc(raw, latest="9.1.0-beta.1"), None) == "9.0.0"
    assert pick(doc(raw), None) == "9.0.0"


def test_parse_spec() -> None:
    assert parse_spec("@types/react@^19") == NpmSpec("@types/react", "^19")
    assert parse_spec("@types/react") == NpmSpec("@types/react")
    assert parse_spec(" vite@^8 ") == NpmSpec("vite", "^8")
    assert parse_spec("react@") == NpmSpec("react")
    assert parse_spec("typescript@>=5 <7") == NpmSpec("typescript", ">=5 <7")
    assert parse_spec("react@latest") == NpmSpec("react")
    assert parse_spec("@types/react@latest") == NpmSpec("@types/react")
    with pytest.raises(ValueError, match="dist-tag 'next' not supported"):
        parse_spec("react@next")
    with pytest.raises(ValueError, match="invalid version range"):
        parse_spec("react@!!garbage")
    for bad in ["bad name", "../etc", "@scope/a/b", "a\\b", "x\ny", "", "@/x", "react@npm:foo@1"]:
        with pytest.raises(ValueError):
            parse_spec(bad)


@pytest.mark.parametrize(
    "spec",
    [
        "workspace:*",
        "npm:other@^1",
        "file:../b",
        "git+https://x/y.git",
        "github:a/b",
        "latest",
        "next",
        "user/repo",
        "!!garbage",
    ],
)
def test_unevaluable_peer_ranges_never_create_issues(spec: str) -> None:
    assert not is_evaluable(spec)
    docs = {"a": doc({"1.0.0": {"peerDependencies": {"b": spec}}}), "b": doc({"2.0.0": {}})}
    _, issues, errors = decide([NpmSpec("a"), NpmSpec("b")], docs, None)
    assert issues == [] and errors == {}


def test_optional_peers_still_constrain_when_requested() -> None:
    a = doc(
        {
            "1.0.0": {
                "peerDependencies": {"b": "^1.0.0", "c": "^1.0.0"},
                "peerDependenciesMeta": {"b": {"optional": True}, "c": {"optional": True}},
            }
        }
    )
    docs = {"a": a, "b": doc({"1.5.0": {}, "2.0.0": {}})}
    _, issues, _ = decide([NpmSpec("a"), NpmSpec("b")], docs, None)
    assert issues == [
        {
            "kind": "peer",
            "ecosystem": "npm",
            "package": "b@2.0.0",
            "required_by": "a@1.0.0",
            "range": "^1.0.0",
            "suggestion": {"b": "1.5.0"},
        }
    ]
    # `c` is optional and not requested: no constraint at all.


def test_suggestion_satisfies_all_constraints_and_the_user_range() -> None:
    docs = {
        "a": doc({"1.0.0": {"peerDependencies": {"t": ">=4 <6.1"}}}),
        "b": doc({"1.0.0": {"peerDependencies": {"t": "^5.0.0 || ^6.0.0"}}}),
        "t": doc({v: {} for v in ["4.9.0", "5.2.0", "5.9.9", "6.0.9", "6.0.10", "6.1.0", "7.0.0"]}),
    }
    _, issues, _ = decide([NpmSpec("a"), NpmSpec("b"), NpmSpec("t")], docs, None)
    assert [i["required_by"] for i in issues] == ["a@1.0.0", "b@1.0.0"]
    assert all(i["suggestion"] == {"t": "6.0.10"} for i in issues)
    picks, issues, _ = decide([NpmSpec("a"), NpmSpec("b"), NpmSpec("t", "<6")], docs, None)
    assert picks["t"].version == "5.9.9" and issues == []
    picks, issues, _ = decide([NpmSpec("a"), NpmSpec("b"), NpmSpec("t", "^7")], docs, None)
    assert picks["t"].version == "7.0.0"
    assert all("suggestion" not in i for i in issues)


def test_alternative_is_highest_constraining_version_accepting_latest() -> None:
    docs = {
        "plugin": doc(
            {
                "1.0.0": {"peerDependencies": {"host": "^1"}},
                "2.0.0": {"peerDependencies": {"host": "^1 || ^2"}},
                "2.1.0": {"peerDependencies": {"host": "^2"}},
                "3.0.0": {"peerDependencies": {"host": "^1"}},  # regressed cap
            }
        ),
        "host": doc({"1.0.0": {}, "2.0.0": {}}),
    }
    _, issues, _ = decide([NpmSpec("plugin"), NpmSpec("host", "^2")], docs, None)
    assert issues == [
        {
            "kind": "peer",
            "ecosystem": "npm",
            "package": "host@2.0.0",
            "required_by": "plugin@3.0.0",
            "range": "^1",
            "alternative": {"plugin": "2.1.0"},
        }
    ]


def test_primary_answer_never_moves() -> None:
    docs = {
        "a": doc({"1.0.0": {"peerDependencies": {"b": "^1"}}}),
        "b": doc({"1.0.0": {}, "2.0.0": {}}),
    }
    picks, issues, _ = decide([NpmSpec("a"), NpmSpec("b")], docs, None)
    assert picks["b"].version == "2.0.0"
    assert issues[0]["suggestion"] == {"b": "1.0.0"}


def test_reduce_doc_keeps_only_what_checks_need() -> None:
    reduced = reduce_doc(
        {
            "name": "x",
            "dist-tags": {"latest": "1.1.0", "next": "2.0.0-rc.1"},
            "modified": "2026-01-01",
            "versions": {
                "1.0.0": {"engines": ["node >= 0.4"], "deprecated": True, "dist": {"x": 1}},
                "1.1.0": {
                    "dependencies": {"y": "^1"},
                    "peerDependencies": {"p": "^1", "bad": 3},
                    "peerDependenciesMeta": {"p": {"optional": True}, "q": "nonsense"},
                    "engines": {"node": ">=20", "npm": ">=10"},
                },
                "2.0.0-rc.1": {},
            },
        }
    )
    assert reduced == {
        "name": "x",
        "latest": "1.1.0",
        "versions": {"1.0.0": 0, "1.1.0": 1},
        "meta": [
            {"deprecated": "deprecated"},
            {"peer": {"p": "^1"}, "optional": ["p"], "engines": {"node": ">=20", "npm": ">=10"}},
        ],
    }


# ----------------------------------------------------------------- fetch layer


async def test_fetch_encodes_scoped_names_and_asks_for_abbreviated_docs() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=json.dumps({"name": "@types/react", "versions": {}}))

    http = mock_http(handler)
    await fetch_doc(Fetcher(http, Cache(None)), "@types/react")
    assert seen[0].url.raw_path == b"/@types%2Freact"
    assert seen[0].headers["accept"] == "application/vnd.npm.install-v1+json"
    await http.aclose()


async def test_one_failure_never_fails_the_call() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/left-pad-zzz":
            return httpx2.Response(404, json={"error": "Not found"})
        if request.url.path == "/flaky":
            return httpx2.Response(503)
        body = {"name": "ok", "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {}}}
        return httpx2.Response(200, content=json.dumps(body))

    http = mock_http(handler)
    result = await check_npm(
        Fetcher(http, Cache(None)), ["ok", "left-pad-zzz", "flaky", "bad name", "ok@^1"], None
    )
    assert result.picks == {"ok": "1.0.0"}
    assert result.errors == {
        "left-pad-zzz": "not found",
        "flaky": "HTTP 503",
        "bad name": "invalid npm package name",
        "ok@^1": "duplicate package ignored",
    }
    await http.aclose()


async def test_unpublished_package_is_reported_as_such() -> None:
    # Real case: `contenthook`, whose abbreviated document keeps only these two fields.
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = {"name": "contenthook", "modified": "2024-11-23T15:01:33.779Z"}
        return httpx2.Response(200, content=json.dumps(body))

    http = mock_http(handler)
    result = await check_npm(Fetcher(http, Cache(None)), ["contenthook"], None)
    assert result.errors == {"contenthook": "unpublished"}
    await http.aclose()


# ----------------------------------------------------------------- shared peers


SHARED_DOCS = {
    "config": doc(
        {
            "1.0.0": {"peerDependencies": {"eslint": "^8"}},
            "2.0.0": {"peerDependencies": {"eslint": "^8"}},
        }
    ),
    "plugin": doc(
        {
            "1.0.0": {"peerDependencies": {"eslint": "^8 || ^9"}},
            "2.0.0": {"peerDependencies": {"eslint": ">=9"}},
        }
    ),
    "helper": doc(
        {
            "1.0.0": {
                "peerDependencies": {"eslint": "*", "ts": "^5", "react": "^19"},
                "peerDependenciesMeta": {"ts": {"optional": True}},
            }
        }
    ),
    "react": doc({"19.0.0": {}}),
}


def test_shared_required_peer_conflict() -> None:
    specs = [NpmSpec("config"), NpmSpec("plugin"), NpmSpec("helper"), NpmSpec("react")]
    picks, issues, _ = decide(specs, SHARED_DOCS, None)
    assert issues == []  # no requested package constrains another one
    shared = shared_peers(picks)
    # react is requested (a peer issue, if any); ts is optional and not requested.
    assert list(shared) == ["eslint"]
    eslint = doc({"8.57.1": {}, "9.39.0": {}})
    assert check_shared(shared, {"eslint": eslint}) == [
        {
            "kind": "shared",
            "ecosystem": "npm",
            "package": "eslint",
            "required_by": "config@2.0.0, plugin@2.0.0, helper@1.0.0",
            "message": "no stable eslint satisfies config@2.0.0 (^8) and "
            "plugin@2.0.0 (>=9) and helper@1.0.0 (*)",
            "alternative": {"plugin": "1.0.0"},  # config has no version without ^8
        }
    ]
    assert check_shared(shared, {}) == []  # unknown: never a false conflict
    compatible = shared_peers(
        decide([NpmSpec("plugin", "1.0.0"), NpmSpec("config")], SHARED_DOCS, None)[0]
    )
    assert check_shared(compatible, {"eslint": eslint}) == []


async def test_shared_peer_is_fetched_and_checked() -> None:
    raw = {
        "config": {"versions": {"1.0.0": {"peerDependencies": {"eslint": "^8"}}}},
        "plugin": {"versions": {"1.0.0": {"peerDependencies": {"eslint": ">=9"}}}},
        "eslint": {"versions": {"8.57.1": {}, "9.39.0": {}}},
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        name = request.url.path.strip("/")
        return httpx2.Response(200, content=json.dumps({"name": name, **raw[name]}))

    http = mock_http(handler)
    result = await check_npm(Fetcher(http, Cache(None)), ["config", "plugin"], None)
    assert [(i["kind"], i["package"]) for i in result.issues] == [("shared", "eslint")]
    assert "eslint" not in result.picks  # not requested: never answered
    await http.aclose()
