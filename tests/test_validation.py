"""Offline tests of the resolver-validation plumbing (no uv/npm is ever started here)."""

import json
from pathlib import Path
from typing import Any

import pytest

from stackcheck_mcp import stack, validation
from stackcheck_mcp.validation import Completed, clean_npm_message, parse_compiled, tail
from tests.test_server import offline_app

ERESOLVE = """npm error code ERESOLVE
npm error ERESOLVE unable to resolve dependency tree
npm error
npm error While resolving: stackcheck-validation@0.0.0
npm error Found: typescript@7.0.2
npm error node_modules/typescript
npm error   typescript@"7.0.2" from the root project
npm error
npm error Could not resolve dependency:
npm error peer typescript@">=4.8.4 <6.1.0" from typescript-eslint@8.72.1
npm error Fix the upstream dependency conflict, or retry this command with --force
npm error For a full report see:
npm error C:\\Users\\me\\AppData\\Local\\npm-cache\\_logs\\2026-10-27-eresolve-report.txt
npm error A complete log of this run can be found in: /home/me/.npm/_logs/x-debug-0.log
"""


def test_clean_npm_message() -> None:
    message = clean_npm_message(ERESOLVE)
    assert message.splitlines()[0] == "code ERESOLVE"
    assert 'peer typescript@">=4.8.4 <6.1.0" from typescript-eslint@8.72.1' in message
    assert "_logs" not in message and "complete log" not in message
    assert len(message.splitlines()) <= 15


def test_parse_compiled_and_tail() -> None:
    output = "annotated-types==0.8.0\nPydantic==2.12.5\n# comment\n    # via x\nfastapi==0.162.0\n"
    assert parse_compiled(output) == {
        "annotated-types": "0.8.0",
        "pydantic": "2.12.5",
        "fastapi": "0.162.0",
    }
    assert tail("\n".join(str(i) for i in range(40)), 3) == "37\n38\n39"


async def test_resolvers_never_run_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a resolver was started without --resolver-validation")

    monkeypatch.setattr(validation.asyncio, "create_subprocess_exec", forbidden)
    app = offline_app()
    result = await stack.check_stack(app, npm_specs=["typescript"], pypi_specs=["fastapi"])
    assert result["validation"] == {"npm": "skipped", "pypi": "skipped"}


async def test_missing_binaries_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation.shutil, "which", lambda name: None)
    app = offline_app()
    app.settings.resolver_validation = True
    result = await stack.check_stack(app, npm_specs=["typescript"], pypi_specs=["fastapi"])
    assert result["validation"] == {"npm": "unavailable", "pypi": "unavailable"}


async def test_pypi_failure_carries_uv_message_and_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run(command: list[str], **kwargs: Any) -> Completed:
        calls.append({"command": command, **kwargs})
        if "==" in kwargs["stdin"]:
            return Completed(1, "", "error: No solution found\n  cause: pydantic-core==2.50.1\n")
        return Completed(0, "pydantic==2.14.1\npydantic-core==2.50.1\n", "")

    monkeypatch.setattr(validation.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(validation, "run", fake_run)
    outcome = await validation.validate_pypi(
        ["pydantic==2.14.1", "pydantic-core==2.49.0"],
        ["pydantic", "pydantic-core<2.50"],
        "3.15.2",
        time_limit=5,
    )
    assert outcome.status == "failed"
    assert outcome.issues == [
        {
            "kind": "resolver",
            "ecosystem": "pypi",
            "package": "uv pip compile",
            "message": "error: No solution found\n  cause: pydantic-core==2.50.1",
            "suggestion": {"pydantic-core": "2.50.1"},
        }
    ]
    pinned = next(c for c in calls if "==" in c["stdin"])
    assert pinned["command"][:4] == ["/bin/uv", "pip", "compile", "-"]
    assert "--python-version" in pinned["command"] and "3.15" in pinned["command"]
    assert pinned["stdin"] == "pydantic==2.14.1\npydantic-core==2.49.0\n"
    assert pinned["time_limit"] == 5


async def test_npm_runs_lockfile_only_without_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(command: list[str], *, cwd: Path, **kwargs: Any) -> Completed:
        seen["command"] = command
        seen["manifest"] = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
        return Completed(1, "", ERESOLVE)

    monkeypatch.setattr(validation.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(validation, "run", fake_run)
    outcome = await validation.validate_npm(
        ["typescript@7.0.2", "@types/react@19.3.0"], time_limit=5
    )
    assert {"--package-lock-only", "--ignore-scripts", "--no-audit"} <= set(seen["command"])
    assert seen["manifest"]["dependencies"] == {"typescript": "7.0.2", "@types/react": "19.3.0"}
    assert outcome.status == "failed"
    assert outcome.issues[0]["kind"] == "resolver"
    assert "typescript-eslint@8.72.1" in outcome.issues[0]["message"]


async def test_timeout_kills_the_process(tmp_path: Path) -> None:
    import sys

    completed = await validation.run(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, time_limit=0.5
    )
    assert completed is None
