"""Optional validation of the picks by real resolvers (``--resolver-validation`` only).

* PyPI: ``uv pip compile -`` on the pinned set (requirements on stdin), plus one unpinned
  run whose choices become the ``suggestion`` when the pinned set is rejected.
* npm: ``npm install --package-lock-only --ignore-scripts`` in a fresh temp dir.

User input never reaches a command line: it goes through stdin or a ``package.json``.
No shell, no install scripts, a timeout on every run that kills the whole process tree
(``npm`` is a ``.cmd`` shim on Windows). Both resolvers use the public registries, like the
checks themselves, whatever the user's ``.npmrc`` / ``uv.toml`` say.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name

from stackcheck_mcp.models import Issue, ValidationStatus

log = logging.getLogger(__name__)

MESSAGE_LINES = 15
NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_INDEX = "https://pypi.org/simple"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


@dataclass
class Outcome:
    status: ValidationStatus
    issues: list[Issue] = field(default_factory=list)


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


async def run(
    command: list[str], *, cwd: Path, time_limit: float, stdin: str | None = None
) -> Completed | None:
    """Run without a shell; ``None`` past ``time_limit`` seconds (the process tree is killed)."""
    if sys.platform == "win32":
        group: dict[str, int | bool] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}  # its own process group, killed as one
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **group,  # type: ignore[arg-type]
    )
    try:
        out, err = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), time_limit
        )
    except TimeoutError:
        await _kill_tree(process)
        return None
    returncode = process.returncode if process.returncode is not None else -1
    return Completed(returncode, out.decode(errors="replace"), err.decode(errors="replace"))


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill ``process`` and its descendants (killing only the shim would orphan node)."""
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(killer.wait(), 10)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, 9)  # SIGKILL to the session's group
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


def tail(text: str, lines: int = MESSAGE_LINES) -> str:
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def parse_compiled(output: str) -> dict[str, str]:
    """``{canonical name: version}`` from ``uv pip compile`` output."""
    pins: dict[str, str] = {}
    for line in output.splitlines():
        if match := _PIN.match(line.strip()):
            pins[canonicalize_name(match.group(1))] = match.group(2)
    return pins


def _pin_name(pin: str) -> str:
    return canonicalize_name(re.split(r"[\[=]", pin, maxsplit=1)[0])


async def validate_pypi(
    pins: list[str], specs: list[str], python: str | None, time_limit: float
) -> Outcome:
    uv = shutil.which("uv")
    if uv is None:
        return Outcome("unavailable")
    command = [uv, "pip", "compile", "-", "--no-header", "--no-annotate", "--quiet"]
    command += ["--color", "never", "--python-platform", "linux"]
    command += ["--no-config", "--default-index", PYPI_INDEX]
    if python:
        command += ["--python-version", ".".join(python.split(".")[:2])]
    with tempfile.TemporaryDirectory(prefix="stackcheck-uv-") as tmp:
        pinned, unpinned = await asyncio.gather(
            run(command, cwd=Path(tmp), time_limit=time_limit, stdin="\n".join(pins) + "\n"),
            run(command, cwd=Path(tmp), time_limit=time_limit, stdin="\n".join(specs) + "\n"),
        )
    if pinned is None:
        return Outcome("timeout")
    if pinned.returncode == 0:
        return Outcome("ok")
    issue: Issue = {
        "kind": "resolver",
        "ecosystem": "pypi",
        "package": "uv pip compile",
        "message": tail(pinned.stderr or pinned.stdout),
    }
    if unpinned is not None and unpinned.returncode == 0:
        chosen = parse_compiled(unpinned.stdout)
        ours = {_pin_name(p): p.rpartition("==")[2] for p in pins}
        differing = {n: v for n, v in chosen.items() if n in ours and ours[n] != v}
        if differing:
            issue["suggestion"] = differing
    return Outcome("failed", [issue])


def clean_npm_message(stderr: str) -> str:
    lines = []
    for line in stderr.splitlines():
        text = line.removeprefix("npm error").removeprefix("npm ERR!").strip()
        if not text or "complete log of this run" in text or text.startswith("For a full report"):
            continue
        if re.search(r"[\\/]_logs[\\/]|npm-cache", text):
            continue
        lines.append(text)
    return "\n".join(lines[:MESSAGE_LINES])


async def validate_npm(pins: list[str], time_limit: float) -> Outcome:
    npm = shutil.which("npm")
    if npm is None:
        return Outcome("unavailable")
    dependencies = dict(pin.rsplit("@", 1) for pin in pins)
    manifest: dict[str, object] = {
        "name": "stackcheck-validation",
        "version": "0.0.0",
        "private": True,
        "dependencies": dependencies,
    }
    command = [npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit"]
    command += ["--no-fund", "--no-update-notifier", "--loglevel", "error"]
    command += ["--registry", NPM_REGISTRY]
    with tempfile.TemporaryDirectory(prefix="stackcheck-npm-") as tmp:
        (Path(tmp) / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        completed = await run(command, cwd=Path(tmp), time_limit=time_limit)
    if completed is None:
        return Outcome("timeout")
    if completed.returncode == 0:
        return Outcome("ok")
    issue: Issue = {
        "kind": "resolver",
        "ecosystem": "npm",
        "package": "npm install",
        "message": clean_npm_message(completed.stderr or completed.stdout),
    }
    return Outcome("failed", [issue])
