"""MCP integration: in-process client for exact answers, stdio subprocess for the transport."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult, TextContent

from stackcheck_mcp.http import HttpClient
from stackcheck_mcp.server import build_server
from stackcheck_mcp.stack import App, Settings
from tests.conftest import FIXTURE_CACHE, TODAY, OfflineTransport, ReadOnlyCache
from tests.reference_stack import MAVEN, MAVEN_BOM, REFERENCE

EXPECTED_NPM = {
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
EXPECTED_PYPI = {
    "fastapi": "0.162.0",
    "pydantic": "2.14.1",
    "uvicorn": "0.54.0",
    "sqlalchemy": "2.1.6",
    "pytest": "9.1.1",
    "playwright": "1.64.1",
    "ruff": "0.17.3",
    "mypy": "2.4.0",
}
TYPESCRIPT_ISSUE = {
    "kind": "peer",
    "ecosystem": "npm",
    "package": "typescript@7.0.2",
    "required_by": "typescript-eslint@8.72.1",
    "range": ">=4.8.4 <6.1.0",
    "suggestion": {"typescript": "6.0.3"},
}


def offline_app(transport: httpx2.AsyncBaseTransport | None = None) -> App:
    return App(
        settings=Settings(today=lambda: TODAY),
        cache=ReadOnlyCache(FIXTURE_CACHE, ttl=10.0**10),
        http_factory=lambda: HttpClient(transport=transport or OfflineTransport(), attempts=1),
    )


def payload(result: CallToolResult) -> Any:
    assert not result.is_error, result.content
    (block,) = result.content
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == result.structured_content
    compact = json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False)
    assert block.text == compact
    return result.structured_content


def assert_no_empty_values(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            assert item not in (None, [], {}, ""), value
            assert_no_empty_values(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_empty_values(item)


async def test_reference_stack_end_to_end() -> None:
    async with Client(build_server(offline_app())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"check_stack", "get_runtime_info", "explain_issue"}
        result = payload(await client.call_tool("check_stack", REFERENCE))
    jsonschema.validate(result, tools["check_stack"].output_schema)  # type: ignore[arg-type]
    assert_no_empty_values(result)
    assert list(result) == ["targets", "npm", "pypi", "runtimes", "issues", "validation"]
    assert result["targets"] == {"node": "24.22.1", "python": "3.15.2"}
    assert result["npm"] == EXPECTED_NPM
    assert result["pypi"] == EXPECTED_PYPI
    assert result["issues"] == [TYPESCRIPT_ISSUE]
    assert result["validation"] == {"npm": "skipped", "pypi": "skipped"}
    assert result["runtimes"]["nodejs"] == {
        "latest": "26.12.1",
        "lts": "24.22.1",
        "next_lts": {"version": "26", "from": "2026-10-28"},
        "maintained": ["26", "24", "22"],
        "eol": "2028-04-30",
    }
    assert result["runtimes"]["sqlite"] == {"latest": "3.54.1", "maintained": ["3"]}


async def test_maven_with_bom() -> None:
    request = {
        "maven": MAVEN,
        "maven_bom": MAVEN_BOM,
        "targets": {"java": "21"},
    }
    async with Client(build_server(offline_app())) as client:
        result = payload(await client.call_tool("check_stack", request))
    assert result["targets"] == {"java": "21.0.13+9"}
    assert result["maven"] == {
        "org.springframework.boot:spring-boot-starter-web": "4.1.2",
        "com.fasterxml.jackson.core:jackson-databind": "2.22.4",
        "org.springframework.boot:spring-boot-dependencies": "4.1.2",
    }
    assert [i["kind"] for i in result["issues"]] == ["bom"]  # Java 21 >= baseline 17
    assert "validation" not in result  # resolvers only cover npm and PyPI


async def test_targets_and_partial_errors() -> None:
    async with Client(build_server(offline_app())) as client:
        result = payload(
            await client.call_tool(
                "check_stack",
                {
                    "npm": ["vite", "no-such-pkg-zzz"],
                    "pypi": ["fastapi"],
                    "targets": {"node": "18", "python": "3.9", "ruby": "3"},
                },
            )
        )
    assert result["targets"] == {"node": "18.20.8", "python": "3.9.25"}
    assert {i["kind"] for i in result["issues"]} == {"engines", "python"}
    assert result["errors"]["targets"] == {"ruby": "unknown target (use node, python, java)"}
    assert set(result["warnings"]["targets"]) == {"node", "python"}  # both cycles are EOL
    # Not in the fixtures and the network is off: reported, not fatal.
    assert "no-such-pkg-zzz" in result["errors"]["npm"]
    assert result["npm"] == {"vite": "8.3.5"}


async def test_a_crashing_ecosystem_does_not_fail_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stackcheck_mcp import pypi

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("unexpected")

    monkeypatch.setattr(pypi, "check_pypi", boom)
    async with Client(build_server(offline_app())) as client:
        result = payload(
            await client.call_tool("check_stack", {"npm": ["react"], "pypi": ["fastapi"]})
        )
    assert result["npm"] == {"react": "19.3.1"}
    assert result["errors"] == {"pypi": {"*": "internal error: KeyError"}}


async def test_empty_request_is_a_tool_error() -> None:
    async with Client(build_server(offline_app())) as client:
        result = await client.call_tool("check_stack", {"npm": []})
    assert result.is_error
    assert "at least one" in result.content[0].text  # type: ignore[union-attr]


async def test_get_runtime_info_with_unknown_slug() -> None:
    not_found = httpx2.MockTransport(lambda request: httpx2.Response(404))
    async with Client(build_server(offline_app(not_found))) as client:
        result = payload(
            await client.call_tool("get_runtime_info", {"products": ["python", "pythn"]})
        )
    assert result["runtimes"]["python"]["latest"] == "3.15.2"
    assert result["errors"]["pythn"].startswith("unknown product; did you mean: python")


async def test_explain_issue() -> None:
    async with Client(build_server(offline_app())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        calls = [
            {"ecosystem": "npm", "package": "typescript-eslint", "version": "8.72.1"},
            {"ecosystem": "pypi", "package": "fastapi[standard]"},
            {"ecosystem": "maven", "package": "org.springframework.boot:spring-boot-starter-web"},
            {"ecosystem": "npm", "package": "typescript", "version": "9.9.9"},
        ]
        npm, pypi, maven, missing = [
            payload(await client.call_tool("explain_issue", call)) for call in calls
        ]
        invalid = await client.call_tool("explain_issue", {"ecosystem": "cargo", "package": "x"})
    for result in (npm, pypi, maven, missing):
        jsonschema.validate(result, tools["explain_issue"].output_schema)  # type: ignore[arg-type]
    assert npm == {
        "ecosystem": "npm",
        "package": "typescript-eslint",
        "version": "8.72.1",
        "peerDependencies": {
            "eslint": "^8.57.0 || ^9.0.0 || ^10.0.0",
            "typescript": ">=4.8.4 <6.1.0",
        },
        "engines": {"node": "^18.18.0 || ^20.9.0 || >=21.1.0"},
    }
    assert pypi["version"] == "0.162.0" and pypi["requires_python"] == ">=3.10"
    assert 'uvicorn[standard]>=0.12.0; extra == "standard"' in pypi["requires_dist"]
    assert not any('extra == "all"' in line for line in pypi["requires_dist"])
    assert "standard" in pypi["extras"]
    assert maven["version"] == "4.1.2"
    assert "org.springframework.boot:spring-boot-starter-tomcat:4.1.2" in maven["dependencies"]
    assert missing["error"] == "version not found (only stable versions are indexed)"
    assert invalid.is_error


@pytest.fixture
def stdio_params(tmp_path: Path) -> StdioServerParameters:
    cache = tmp_path / "cache"
    shutil.copytree(FIXTURE_CACHE, cache)
    dead_proxy = "http://127.0.0.1:9"  # any registry call fails fast: the test stays offline
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "stackcheck_mcp"],
        env={
            "STACKCHECK_CACHE_DIR": str(cache),
            "STACKCHECK_CACHE_TTL": str(10**10),
            "STACKCHECK_CACHE_MAX_AGE": "0",  # copied fixtures keep old mtimes
            "HTTPS_PROXY": dead_proxy,
            "HTTP_PROXY": dead_proxy,
            "NO_PROXY": "",
        },
    )


async def test_stdio_transport(stdio_params: StdioServerParameters) -> None:
    async with Client(stdio_params, read_timeout_seconds=60) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        stack = payload(await client.call_tool("check_stack", REFERENCE))
        runtimes = payload(await client.call_tool("get_runtime_info", {"products": ["nodejs"]}))
    jsonschema.validate(stack, tools["check_stack"].output_schema)  # type: ignore[arg-type]
    jsonschema.validate(runtimes, tools["get_runtime_info"].output_schema)  # type: ignore[arg-type]
    # Versions do not depend on the date; targets do (the server uses the real clock).
    assert stack["npm"] == EXPECTED_NPM and stack["pypi"] == EXPECTED_PYPI
    assert TYPESCRIPT_ISSUE in stack["issues"]
    assert "stale" not in stack
    assert runtimes["runtimes"]["nodejs"]["latest"] == "26.12.1"


async def test_streamable_http_transport(stdio_params: StdioServerParameters) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, **(stdio_params.env or {})}
    process = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-m", "stackcheck_mcp", "--http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                await asyncio.sleep(0.1)
        async with Client(f"http://127.0.0.1:{port}/mcp", read_timeout_seconds=60) as client:
            result = payload(await client.call_tool("get_runtime_info", {"products": ["python"]}))
        assert result["runtimes"]["python"]["latest"] == "3.15.2"
    finally:
        process.terminate()
        process.wait(timeout=10)


class StallingTransport(httpx2.AsyncBaseTransport):
    """A registry that never answers in time."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(30)
        return httpx2.Response(500)


async def test_slow_registries_give_a_partial_answer_in_time() -> None:
    from stackcheck_mcp import stack
    from stackcheck_mcp.cache import Cache

    app = App(
        settings=Settings(today=lambda: TODAY, call_timeout=0.5),
        cache=Cache(None),
        http_factory=lambda: HttpClient(transport=StallingTransport(), attempts=1),
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await stack.check_stack(
        app, npm_specs=["react"], pypi_specs=["fastapi"], runtimes=["nodejs"]
    )
    explained = await stack.explain(app, "npm", "react")
    runtimes = await stack.get_runtime_info(app, ["python"])
    assert loop.time() - started < 5
    timed_out = "timed out after 0.5s (registries slow); retry"
    assert result == {
        "errors": {
            "targets": {"node": timed_out, "python": timed_out},
            "npm": {"*": timed_out},
            "pypi": {"*": timed_out},
            "runtimes": {"nodejs": timed_out},
        }
    }
    assert explained["error"] == timed_out
    assert runtimes == {"errors": {"python": timed_out}}
    await app.aclose()


async def test_oversized_requests_are_tool_errors() -> None:
    async with Client(build_server(offline_app())) as client:
        result = await client.call_tool("check_stack", {"npm": [f"p{i}" for i in range(101)]})
        products = await client.call_tool("get_runtime_info", {"products": ["x"] * 101})
    assert result.is_error and products.is_error
    assert "at most 100 npm entries per call (101 given)" in result.content[0].text  # type: ignore[union-attr]
