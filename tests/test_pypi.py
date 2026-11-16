import json
from typing import Any

import httpx2
import pytest
from packaging.specifiers import SpecifierSet

from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.pypi import (
    Pick,
    PypiIndex,
    _wheel_tags,
    check_pypi,
    check_shared,
    check_wheels,
    fetch_index,
    marker_env,
    parse_spec,
    reduce_json_project,
    reduce_simple,
    requirements_on,
    select,
    shared_requirements,
    version_info,
    wheel_ok,
)
from tests.conftest import mock_http
from tests.reference_stack import PYPI

PY315 = "3.15.2"


def simple(files: list[dict[str, Any]], versions: list[str] | None = None, **extra: Any) -> Any:
    """Raw PEP 691 project page."""
    for f in files:
        f.setdefault("yanked", False)
        f.setdefault("requires-python", None)
    if versions is None:
        versions = sorted({f["filename"].split("-")[1].removesuffix(".tar.gz") for f in files})
    return {"name": "demo", "versions": versions, "files": files, **extra}


# ----------------------------------------------------------------- reference stack


async def test_reference_stack_is_issue_free_on_python_315(fetcher: Fetcher) -> None:
    result = await check_pypi(fetcher, PYPI, PY315)
    assert result.errors == {}
    assert result.picks == {
        "fastapi": "0.162.0",
        "pydantic": "2.14.1",
        "uvicorn": "0.54.0",
        "sqlalchemy": "2.1.6",
        "pytest": "9.1.1",
        "playwright": "1.64.1",
        "ruff": "0.17.3",
        "mypy": "2.4.0",
    }
    assert result.issues == []
    assert "uvicorn[standard]==0.54.0" in result.pins
    assert result.specs[:3] == ["fastapi", "pydantic", "uvicorn[standard]"]


async def test_user_constraint_respected_without_false_conflict(fetcher: Fetcher) -> None:
    result = await check_pypi(fetcher, ["fastapi", "pydantic<2.13"], PY315)
    assert result.picks == {"fastapi": "0.162.0", "pydantic": "2.12.5"}  # same as uv
    assert result.issues == []


async def test_requires_conflict_without_any_fix(fetcher: Fetcher) -> None:
    result = await check_pypi(fetcher, ["pydantic", "pydantic-core<2.50"], PY315)
    assert result.picks == {"pydantic": "2.14.1", "pydantic-core": "2.49.0"}
    # No stable pydantic pins pydantic-core 2.49.0, and the user excluded 2.50.1.
    assert result.issues == [
        {
            "kind": "requires",
            "ecosystem": "pypi",
            "package": "pydantic-core==2.49.0",
            "required_by": "pydantic==2.14.1",
            "range": "==2.50.1",
        }
    ]


async def test_requires_conflict_with_alternative(fetcher: Fetcher) -> None:
    result = await check_pypi(fetcher, ["pydantic", "pydantic-core<2.47"], PY315)
    requires = next(i for i in result.issues if i["kind"] == "requires")
    assert requires["package"] == "pydantic-core==2.46.5"
    assert requires["alternative"] == {"pydantic": "2.13.5"}
    # 2.46.5 predates CPython 3.15: flagged, but installable from the sdist.
    wheels = next(i for i in result.issues if i["kind"] == "wheels")
    assert wheels["package"] == "pydantic-core==2.46.5" and wheels["severity"] == "warn"


async def test_requires_conflict_with_suggestion(fetcher: Fetcher) -> None:
    result = await check_pypi(fetcher, ["pydantic", "pydantic-core"], PY315)
    assert result.issues == []
    index = await fetch_index(fetcher, "pydantic-core")
    pydantic = await fetch_index(fetcher, "pydantic")
    from stackcheck_mcp.pypi import check_requires, fetch_meta

    picks = {
        "pydantic": Pick(parse_spec("pydantic"), "2.14.1", pydantic),
        "pydantic-core": Pick(parse_spec("pydantic-core"), "2.50.0", index),
    }
    metas = {"pydantic": await fetch_meta(fetcher, "pydantic", "2.14.1")}
    issues, pending = check_requires(picks, metas, marker_env(PY315))
    assert issues[0]["suggestion"] == {"pydantic-core": "2.50.1"} and pending == []


async def test_python_target_too_old(fetcher: Fetcher) -> None:
    result = await check_pypi(fetcher, ["fastapi", "sqlalchemy", "ruff"], "3.9.25")
    by_package = {i["package"]: i for i in result.issues}
    assert set(by_package) == {"fastapi==0.162.0", "sqlalchemy==2.1.6"}  # ruff needs >=3.7
    fastapi = by_package["fastapi==0.162.0"]
    assert fastapi["kind"] == "python" and fastapi["range"] == ">=3.10"
    assert fastapi["target"] == "python 3.9.25"
    assert fastapi["suggestion"] == {"fastapi": "0.128.8"}
    assert by_package["sqlalchemy==2.1.6"]["suggestion"] == {"sqlalchemy": "2.0.55"}


async def test_wheels_for_future_python(fetcher: Fetcher) -> None:
    names = ["pydantic-core", "ruff", "sqlalchemy", "greenlet", "playwright", "fastapi"]
    picks = {}
    for name in names:
        index = await fetch_index(fetcher, name)
        picks[name] = Pick(parse_spec(name), next(reversed(index["versions"])), index)
    assert check_wheels(picks, PY315, set()) == []  # cp315 wheels, abi3 or pure everywhere
    flagged = {i["package"].split("==")[0]: i for i in check_wheels(picks, "3.16.0", set())}
    # ruff/playwright ship py3-none-<platform> wheels; sqlalchemy/fastapi a pure wheel.
    assert set(flagged) == {"pydantic-core", "greenlet"}
    assert all(i["severity"] == "warn" for i in flagged.values())  # sdists exist


async def test_unsatisfiable_and_not_found() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/simple/nonexistent-pkg-zzz/":
            return httpx2.Response(404)
        files = [{"filename": "demo-1.0-py3-none-any.whl"}]
        return httpx2.Response(200, content=json.dumps(simple(files, ["1.0"])))

    http = mock_http(handler)
    result = await check_pypi(
        Fetcher(http, Cache(None)), ["demo>=99", "nonexistent-pkg-zzz", "x @ https://e/x.whl"], None
    )
    assert result.errors == {
        "nonexistent-pkg-zzz": "not found",
        "x @ https://e/x.whl": "URL requirements are not supported",
    }
    assert result.issues == [
        {
            "kind": "unsatisfiable",
            "ecosystem": "pypi",
            "package": "demo",
            "range": ">=99",
            "message": "no stable version matches; latest is 1.0",
        }
    ]
    await http.aclose()


# ----------------------------------------------------------------- pure rules


def test_parse_spec() -> None:
    spec = parse_spec("Uvicorn[Standard]>=0.30")
    assert spec.name == "uvicorn" and spec.extras == {"standard"}
    assert spec.specifier == SpecifierSet(">=0.30")
    assert spec.raw == "uvicorn[standard]>=0.30"
    assert spec.pin("0.54.0") == "uvicorn[standard]==0.54.0"
    assert parse_spec("Foo_Bar.baz").name == "foo-bar-baz"
    for bad in ["bad name", "../x", "x==", "", 'x; python_version < "3"', "x @ https://e/x"]:
        with pytest.raises(ValueError):
            parse_spec(bad)


def test_stable_filter() -> None:
    files = [
        {"filename": f"demo-{v}-py3-none-any.whl"}
        for v in ["1.0", "1.0.post1", "1.1rc1", "1.1.dev0", "2.0a1", "1.0.10", "1.0.9"]
    ]
    index = reduce_simple(simple(files, [*(f["filename"].split("-")[1] for f in files), "junk!"]))
    assert list(index["versions"]) == ["1.0", "1.0.post1", "1.0.9", "1.0.10"]


@pytest.mark.parametrize("listed", [["1.0", "2.0-dev-9429"], []])
def test_legacy_sdist_name_is_not_read_as_a_version(listed: list[str]) -> None:
    # Real case: selenium's `selenium-2.0-dev-9429.tar.gz` was picked as version "9429".
    files = [
        {"filename": "demo-1.0-py3-none-any.whl"},
        {"filename": "demo-2.0-dev-9429.tar.gz"},
        {"filename": "demo-2.0-dev-9429.tar.bz2"},
        {"filename": "other-3.0-py3-none-any.whl"},
    ]
    index = reduce_simple(simple(files, listed))
    assert list(index["versions"]) == ["1.0"]


def test_yanked_detection_requires_all_files() -> None:
    files = [
        {"filename": "demo-1.0-py3-none-any.whl"},
        {"filename": "demo-1.0.tar.gz", "yanked": "broken"},
        {"filename": "demo-1.1-py3-none-any.whl", "yanked": "bad build"},
        {"filename": "demo-1.1.tar.gz", "yanked": True},
    ]
    index = reduce_simple(simple(files, ["1.0", "1.1"]))
    assert "yanked" not in version_info(index, "1.0")
    assert version_info(index, "1.1")["yanked"] == "bad build"


def test_release_without_files_is_not_a_candidate() -> None:
    # 1.2 is listed (its files were deleted), 1.3 only ships an egg: nothing to install.
    files = [
        {"filename": "demo-1.1-py3-none-any.whl"},
        {"filename": "demo-1.3-py3.12.egg"},
    ]
    index = reduce_simple(simple(files, ["1.1", "1.2", "1.3"]))
    assert list(index["versions"]) == ["1.1"]
    assert select(index, parse_spec("demo"))[0] == "1.1"


def test_yanked_latest_is_flagged_and_skipped() -> None:
    files = [
        {"filename": "demo-1.0-py3-none-any.whl"},
        {"filename": "demo-1.1-py3-none-any.whl", "yanked": "bad build"},
    ]
    index = reduce_simple(simple(files, ["1.0", "1.1"]))
    version, issues = select(index, parse_spec("demo"))
    assert version == "1.0"
    assert issues == [
        {
            "kind": "yanked",
            "ecosystem": "pypi",
            "package": "demo==1.1",
            "severity": "warn",
            "message": "bad build",
            "suggestion": {"demo": "1.0"},
        }
    ]
    # Only yanked versions match: still a candidate, always flagged.
    version, issues = select(index, parse_spec("demo>1.0"))
    assert version == "1.1" and issues[0]["kind"] == "yanked" and "suggestion" not in issues[0]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("x-1.0-py3-none-any.whl", (True, set(), None)),
        ("x-1.0-py2.py3-none-any.whl", (True, set(), None)),
        ("x-1.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", (True, set(), None)),
        # Other platforms do not count: the checks target CPython on Linux x86_64.
        ("x-1.0-py3-none-macosx_11_0_arm64.whl", (False, set(), None)),
        ("x-1.0-cp315-cp315-musllinux_1_2_x86_64.whl", (False, set(), None)),
        ("x-1.0-cp315-cp315-manylinux_2_17_aarch64.whl", (False, set(), None)),
        ("x-1.0-cp35-cp35m-manylinux1_x86_64.whl", (False, {5}, None)),
        ("x-1.0-cp39-abi3-manylinux_2_17_x86_64.whl", (False, set(), 9)),
        ("x-1.0-cp315-cp315-manylinux_2_17_x86_64.whl", (False, {15}, None)),
        ("x-1.0-cp315-cp315t-manylinux_2_17_x86_64.whl", (False, set(), None)),  # free-threaded
        ("x-1.0-cp38-cp38m-win_amd64.whl", (False, set(), None)),
        ("x-1.0-cp38-cp38m-manylinux2010_x86_64.whl", (False, {8}, None)),
        ("x-1.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl", (False, set(), None)),
        ("not-a-wheel.whl", (False, set(), None)),
    ],
)
def test_wheel_tags(filename: str, expected: tuple[bool, set[int], int | None]) -> None:
    assert _wheel_tags(filename) == expected


def test_wheel_ok() -> None:
    assert wheel_ok({"abi3": 9}, 15)
    assert not wheel_ok({"abi3": 16}, 15)
    assert wheel_ok({"cp": [14, 15]}, 15)
    assert not wheel_ok({"cp": [13, 14], "sdist": True}, 15)
    assert wheel_ok({"pure": True}, 20)


def test_markers_and_extras() -> None:
    meta = {
        "requires_dist": [
            "b>=1",
            'b<3; extra == "x"',
            'b!=2.5; python_version < "3.11"',
            'b==9; sys_platform == "win32"',
            "c>=1",
            "not a valid requirement ???",
        ]
    }
    env = marker_env(PY315)
    assert requirements_on(meta, "b", env, frozenset()) == [SpecifierSet(">=1")]  # type: ignore[arg-type]
    with_x = requirements_on(meta, "b", env, frozenset({"x"}))  # type: ignore[arg-type]
    assert with_x == [SpecifierSet(">=1"), SpecifierSet("<3")]
    old = requirements_on(meta, "b", marker_env("3.10.4"), frozenset())  # type: ignore[arg-type]
    assert SpecifierSet("!=2.5") in old


def test_reduce_json_project_fallback() -> None:
    raw = {
        "info": {"name": "Demo"},
        "releases": {
            "1.0": [
                {
                    "filename": "demo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
                    "packagetype": "bdist_wheel",
                    "requires_python": ">=3.12",
                    "yanked": False,
                },
                {
                    "filename": "demo-1.0.tar.gz",
                    "packagetype": "sdist",
                    "requires_python": ">=3.12",
                    "yanked": False,
                },
            ],
            "1.1": [
                {
                    "filename": "demo-1.1-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "yanked": True,
                    "yanked_reason": None,
                }
            ],
            "2.0b1": [],
        },
    }
    index = reduce_json_project(raw)
    assert index["name"] == "demo"
    assert version_info(index, "1.0") == {"rp": ">=3.12", "cp": [12], "sdist": True}
    assert version_info(index, "1.1") == {"yanked": "yanked", "pure": True}


async def test_simple_api_failure_falls_back_to_json_api() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith("/simple/"):
            assert request.headers["accept"] == "application/vnd.pypi.simple.v1+json"
            return httpx2.Response(503)
        body = {
            "info": {"name": "demo"},
            "releases": {
                "1.0": [
                    {
                        "filename": "demo-1.0-py3-none-any.whl",
                        "packagetype": "bdist_wheel",
                        "yanked": False,
                    }
                ]
            },
        }
        return httpx2.Response(200, content=json.dumps(body))

    http = mock_http(handler, attempts=1)
    index: PypiIndex = await fetch_index(Fetcher(http, Cache(None)), "demo")
    assert list(index["versions"]) == ["1.0"]
    await http.aclose()


def test_project_status_is_flagged() -> None:
    raw = simple(
        [{"filename": "demo-1.0-py3-none-any.whl"}],
        ["1.0"],
        **{"project-status": {"status": "archived", "reason": "use demo2"}},
    )
    assert reduce_simple(raw)["status"] == "archived: use demo2"
    active = simple(
        [{"filename": "demo-1.0-py3-none-any.whl"}],
        ["1.0"],
        **{"project-status": {"status": "active"}},
    )
    assert "status" not in reduce_simple(active)


# ----------------------------------------------------------------- shared dependencies


def project(name: str, versions: list[str]) -> PypiIndex:
    files = [{"filename": f"{name}-{v}-py3-none-any.whl"} for v in versions]
    return reduce_simple({**simple(files, versions), "name": name})


def test_shared_requirements_and_conflict() -> None:
    picks = {
        "a": Pick(parse_spec("a"), "2.0", project("a", ["2.0"])),
        "b": Pick(parse_spec("b[x]"), "1.0", project("b", ["1.0"])),
    }
    metas: dict[str, Any] = {
        "a": {"requires_dist": ["c<2", "c>=1; python_version >= '3.10'", "d>=1", "b"]},
        "b": {"requires_dist": ["c>=2; extra == 'x'", "d>=2", "e<3; sys_platform == 'win32'"]},
    }
    shared = shared_requirements(picks, metas, marker_env(PY315))
    # d: lower bounds only (always meet at the newest release); e: marker off; b: requested.
    assert {name: [(p.name, str(s)) for p, s in c] for name, c in shared.items()} == {
        "c": [("a", "<2,>=1"), ("b", ">=2")]
    }
    issues = check_shared(shared, {"c": project("c", ["1.5", "2.1"])})
    assert issues == [
        (
            {
                "kind": "shared",
                "ecosystem": "pypi",
                "package": "c",
                "required_by": "a==2.0, b==1.0",
                "message": "no stable c satisfies a==2.0 (<2,>=1) and b==1.0 (>=2)",
            },
            "c",
        )
    ]
    assert check_shared(shared, {"c": project("c", ["1.5", "2.0", "1.9"])}) == [
        (issues[0][0], "c")
    ]  # 2.0 is not <2
    assert check_shared(shared, {}) == []  # unknown: never a false conflict


async def test_shared_conflict_end_to_end_with_alternative() -> None:
    requires = {"a/2.0": ["c<2"], "a/1.0": ["c>=1"], "b/1.0": ["c>=2"]}
    versions = {"a": ["1.0", "2.0"], "b": ["1.0"], "c": ["1.0", "2.0"]}

    def handler(request: httpx2.Request) -> httpx2.Response:
        parts = request.url.path.strip("/").split("/")
        if parts[0] == "simple":
            name = parts[1]
            files = [{"filename": f"{name}-{v}-py3-none-any.whl"} for v in versions[name]]
            return httpx2.Response(200, json={**simple(files, versions[name]), "name": name})
        info = {"requires_dist": requires[f"{parts[1]}/{parts[2]}"], "requires_python": None}
        return httpx2.Response(200, json={"info": info})

    http = mock_http(handler)
    result = await check_pypi(Fetcher(http, Cache(None)), ["a", "b"], PY315)
    assert result.picks == {"a": "2.0", "b": "1.0"}
    assert result.issues == [
        {
            "kind": "shared",
            "ecosystem": "pypi",
            "package": "c",
            "required_by": "a==2.0, b==1.0",
            "message": "no stable c satisfies a==2.0 (<2) and b==1.0 (>=2)",
            "alternative": {"a": "1.0"},
        }
    ]
    await http.aclose()


async def test_shared_alternative_prefers_the_smallest_step_back() -> None:
    requires = {
        "a/1.0": ["c>=1"],  # old and permissive: two releases back
        "a/2.0": ["c<2"],
        "a/3.0": ["c<2"],
        "a/4.0": ["c<2"],
        "b/1.0": [],  # one step back, and dropped c: a fix too (uv agrees)
        "b/2.0": ["c>=2"],
    }
    versions = {"a": ["1.0", "2.0", "3.0", "4.0"], "b": ["1.0", "2.0"], "c": ["1.0", "2.0"]}

    def handler(request: httpx2.Request) -> httpx2.Response:
        parts = request.url.path.strip("/").split("/")
        if parts[0] == "simple":
            name = parts[1]
            files = [{"filename": f"{name}-{v}-py3-none-any.whl"} for v in versions[name]]
            return httpx2.Response(200, json={**simple(files, versions[name]), "name": name})
        info = {"requires_dist": requires[f"{parts[1]}/{parts[2]}"], "requires_python": None}
        return httpx2.Response(200, json={"info": info})

    http = mock_http(handler)
    result = await check_pypi(Fetcher(http, Cache(None)), ["a", "b"], PY315)
    assert [i["kind"] for i in result.issues] == ["shared"]
    assert result.issues[0]["alternative"] == {"b": "1.0"}
    await http.aclose()
