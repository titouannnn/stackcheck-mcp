"""Differential tests against the real resolvers: the project's main correctness guarantee.

Run with ``uv run pytest -m resolvers`` (network, ``uv`` and ``npm`` on PATH). Registry
data drifts, so the assertions are properties:

* every set ``check_stack`` calls free of ``peer``/``requires``/``shared`` issues must
  resolve (for these stacks: deeper conflicts are out of scope without
  ``--resolver-validation``);
* for every ``peer``/``requires``/``shared`` issue, the packages it names must be rejected.

If a resolver disagrees with us, our logic is wrong until proven otherwise.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from stackcheck_mcp import stack, validation
from stackcheck_mcp.cache import Cache
from stackcheck_mcp.models import Issue
from tests.reference_stack import NPM, PYPI

pytestmark = [
    pytest.mark.resolvers,
    pytest.mark.skipif(
        not (shutil.which("uv") and shutil.which("npm")), reason="needs uv and npm on PATH"
    ),
]

TIME_LIMIT = 300.0
CONFLICTS = ("peer", "requires", "shared")

NPM_STACKS = [
    NPM,  # typescript 7 vs typescript-eslint's cap: must be rejected
    [s if s != "typescript" else "typescript@~6.0" for s in NPM],  # the suggested fix
    ["react", "react-dom", "@types/react", "@types/react-dom"],
    ["vite", "@vitejs/plugin-react", "vitest"],
    ["eslint-config-airbnb", "eslint-plugin-unicorn"],  # incompatible eslint peers
]
PYPI_STACKS = [
    PYPI,
    ["fastapi", "pydantic<2.13"],
    ["pydantic", "pydantic-core<2.50"],  # deliberately impossible pin
    ["pydantic", "pydantic-core<2.47"],
    ["fastapi==0.95.0", "sse-starlette"],  # incompatible starlette requirements
]


@pytest.fixture
def app(tmp_path: Path) -> stack.App:
    return stack.App(cache=Cache(tmp_path / "cache"))


def named(issue: Issue) -> list[str]:
    """The pins a conflict names: both sides of a pair, or every package sharing a dep."""
    if issue["kind"] == "shared":
        return issue["required_by"].split(", ")
    return [issue["package"], issue["required_by"]]


@pytest.mark.parametrize("specs", NPM_STACKS, ids=lambda s: "+".join(s)[:60])
async def test_npm_agrees_with_npm_install(app: stack.App, specs: list[str]) -> None:
    result = await stack.check_stack(app, npm_specs=specs)
    assert "errors" not in result, result
    picks = result["npm"]
    conflicts = [i for i in result.get("issues", []) if i["kind"] in CONFLICTS]
    pins = [f"{name}@{version}" for name, version in picks.items()]
    outcome = await validation.validate_npm(pins, TIME_LIMIT)
    if not conflicts:
        assert outcome.status == "ok", outcome.issues
    for issue in conflicts:
        rejected = await validation.validate_npm(named(issue), TIME_LIMIT)
        assert rejected.status == "failed", issue
        assert outcome.status == "failed"


@pytest.mark.parametrize("specs", PYPI_STACKS, ids=lambda s: "+".join(s)[:60])
async def test_pypi_agrees_with_uv(app: stack.App, specs: list[str]) -> None:
    result = await stack.check_stack(app, pypi_specs=specs)
    assert "errors" not in result, result
    python = result["targets"]["python"]
    picks = result["pypi"]
    conflicts = [i for i in result.get("issues", []) if i["kind"] in CONFLICTS]
    pins = [f"{name}=={version}" for name, version in picks.items()]
    outcome = await validation.validate_pypi(pins, specs, python, TIME_LIMIT)
    if not conflicts:
        assert outcome.status == "ok", outcome.issues
    for issue in conflicts:
        pinned = named(issue)
        rejected = await validation.validate_pypi(pinned, pinned, python, TIME_LIMIT)
        assert rejected.status == "failed", issue
        assert outcome.status == "failed"


async def test_unpinned_uv_choice_matches_our_pick(app: stack.App) -> None:
    specs = ["fastapi", "pydantic<2.13"]
    result = await stack.check_stack(app, pypi_specs=specs)
    python = result["targets"]["python"]
    with tempfile.TemporaryDirectory() as tmp:
        completed = await validation.run(
            [
                "uv",
                "pip",
                "compile",
                "-",
                "--no-header",
                "--no-annotate",
                "--quiet",
                "--python-platform",
                "linux",
                "--python-version",
                ".".join(python.split(".")[:2]),
            ],
            cwd=Path(tmp),
            time_limit=TIME_LIMIT,
            stdin="\n".join(specs) + "\n",
        )
    assert completed is not None and completed.returncode == 0, completed
    chosen = validation.parse_compiled(completed.stdout)
    assert {name: chosen[name] for name in result["pypi"]} == result["pypi"]


async def test_server_resolver_validation_flag(app: stack.App) -> None:
    app.settings.resolver_validation = True
    result = await stack.check_stack(
        app, npm_specs=["typescript", "typescript-eslint"], pypi_specs=["fastapi", "pydantic"]
    )
    assert result["validation"]["pypi"] == "ok"
    peer = [i for i in result.get("issues", []) if i["kind"] == "peer"]
    resolver = [i for i in result.get("issues", []) if i["kind"] == "resolver"]
    # While typescript-eslint caps typescript below the latest, npm must reject the set too.
    assert result["validation"]["npm"] == ("failed" if peer else "ok")
    assert bool(resolver) == bool(peer)
