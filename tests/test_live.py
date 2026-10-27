"""Live registry tests (``uv run pytest -m live``, nightly in CI).

Registries drift, so these assert properties checked against independent raw fetches,
never exact version numbers.
"""

import re
from pathlib import Path
from typing import Any

import httpx2
import pytest
from packaging.version import Version

from stackcheck_mcp import stack
from stackcheck_mcp.cache import Cache
from stackcheck_mcp.semver_utils import satisfies, version_key
from tests.reference_stack import MAVEN, MAVEN_BOM, NPM, PYPI, RUNTIMES

pytestmark = pytest.mark.live
STABLE = re.compile(r"^\d+\.\d+\.\d+$")


@pytest.fixture
async def result(tmp_path: Path) -> Any:
    app = stack.App(cache=Cache(tmp_path / "cache"))
    answer = await stack.check_stack(
        app,
        npm_specs=NPM,
        pypi_specs=PYPI,
        maven_specs=MAVEN,
        maven_bom=MAVEN_BOM,
        runtimes=RUNTIMES,
    )
    await app.aclose()
    return answer


async def raw_npm(name: str) -> Any:
    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"https://registry.npmjs.org/{name.replace('/', '%2F')}",
            headers={"Accept": "application/vnd.npm.install-v1+json"},
        )
        response.raise_for_status()
        return response.json()


async def test_reference_stack_has_no_errors_and_is_fresh(result: Any) -> None:
    assert "errors" not in result, result.get("errors")
    assert "stale" not in result
    assert set(result["targets"]) == {"node", "python", "java"}


async def test_npm_versions_are_stable_and_capped_by_latest(result: Any) -> None:
    for name, version in result["npm"].items():
        assert STABLE.match(version), (name, version)  # never a canary/rc/next
        latest = (await raw_npm(name))["dist-tags"]["latest"]
        if STABLE.match(latest):
            assert version_key(version) <= version_key(latest), (name, version, latest)


async def test_typescript_cap_produces_a_peer_issue_while_it_holds(result: Any) -> None:
    eslint_doc = await raw_npm("typescript-eslint")
    ts_latest = (await raw_npm("typescript"))["dist-tags"]["latest"]
    cap = eslint_doc["versions"][eslint_doc["dist-tags"]["latest"]]["peerDependencies"][
        "typescript"
    ]
    capped = satisfies(ts_latest, cap) is False
    peer = [
        i
        for i in result.get("issues", [])
        if i["kind"] == "peer" and i["package"].startswith("typescript@")
    ]
    assert bool(peer) == capped, (cap, ts_latest, peer)
    for issue in peer:
        assert satisfies(issue["suggestion"]["typescript"], cap)


async def test_pypi_versions_are_final_releases(result: Any) -> None:
    for name, version in result["pypi"].items():
        parsed = Version(version)
        assert not parsed.is_prerelease and not parsed.is_devrelease, (name, version)
    wheels = [i for i in result.get("issues", []) if i["kind"] == "wheels"]
    assert not any(i["package"].startswith("ruff==") for i in wheels)  # py3-none-* wheels


async def test_maven_versions_are_stable(result: Any) -> None:
    for coords, version in result["maven"].items():
        assert not re.search(r"(?i)(rc|m)\d|snapshot|alpha|beta", version), (coords, version)


async def test_runtimes(result: Any) -> None:
    node = result["runtimes"]["nodejs"]
    assert "lts" in node and "latest" in node
    assert result["targets"]["node"] == node["lts"]
    assert result["targets"]["python"] == result["runtimes"]["python"]["latest"]
