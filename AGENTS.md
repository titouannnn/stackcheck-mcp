# AGENTS.md — stackcheck-mcp

Instructions for any coding agent working in this repository. Read this file at the start of every session. The full product specification lives in `docs/SPEC.md` (the original build prompt); read the relevant section before changing behaviour.

## What this project is

An open-source (Apache-2.0) Python MCP server. Given lists of npm / PyPI / Maven packages and runtimes, it returns the **latest stable version** of each and **reports incompatibilities** between them (npm peerDependencies, PyPI requires_dist / requires_python / wheels, Maven BOM alignment, Node engines), with an indicative suggestion. One call, compact JSON. Installed with `uvx stackcheck-mcp`.

## Commands

```bash
uv sync                                   # set up the environment (dev group included)
uv run pytest                             # offline unit + integration tests (default, must stay green)
uv run pytest -m live                     # live registry tests (network; assert properties, not exact versions)
uv run pytest -m resolvers                # differential tests vs real uv/npm (needs node + uv on PATH)
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/
uv run stackcheck-mcp                     # stdio server
uv run stackcheck-mcp --http --port 8765  # streamable HTTP
uv run stackcheck-mcp --resolver-validation
```

Before declaring any task done: `ruff`, `mypy --strict src/` and `pytest` must pass. If you touched resolution logic, also run `pytest -m resolvers`.

## Architecture

```
src/stackcheck_mcp/
  __main__.py      CLI flags only (--http, --host, --port, --resolver-validation, --log-level)
  server.py        MCPServer + tool definitions — thin, no business logic
  models.py        typed results and issues (the output contract)
  http.py cache.py shared async HTTP client, on-disk reduced cache
  npm.py pypi.py maven.py runtimes.py   fetch → reduce → decide
  semver_utils.py maven_version.py      version parsing/ordering
  validation.py    optional uv / npm subprocess validation
tests/fixtures/    reduced recorded registry data (offline tests)
```

Rule: **decision logic is pure** (reduced metadata in, picks + issues out). Network and subprocesses stay at the edges. Every new decision rule gets an offline unit test with a fixture.

## Invariants — never break these

1. **Never downgrade.** The primary answer for each package is its latest stable version. Incompatibilities go in `issues` with `suggestion` / `alternative`, which are indicative only.
2. **Stable means stable.** npm: `^\d+\.\d+\.\d+$` and ≤ `dist-tags.latest`. PyPI: no pre/dev releases (PEP 440). Maven: no alpha/beta/rc/cr/M/milestone/snapshot/preview/ea. No canary ever appears in an answer.
3. **Flag, don't hide.** Deprecated (npm) and yanked (PyPI) latest versions are reported as issues, not silently skipped.
4. **Compare versions, never strings.** `6.0.10 > 6.0.9`.
5. **Unevaluable ranges never create issues** (`workspace:*`, `npm:` aliases, `file:`, `git:`, dist-tag names, garbage).
6. **Optional npm peers still constrain** when the peer package is part of the request.
7. **Real resolvers are opt-in.** Never run `uv` or `npm` unless `--resolver-validation` was passed at startup. When they run: `--ignore-scripts`, temp dir, timeout, `asyncio.create_subprocess_exec`, never `shell=True`.
8. **One failure never fails the call.** Per-package errors go in `errors`.
9. **Output stays compact.** Omit empty fields. Any change to the output shape updates `models.py`, the README schema and the tests together.
10. **stdout belongs to the stdio transport.** Log to stderr only.
11. **Public registries only, no credentials** (v1).

## Conventions

- Python ≥ 3.12, full type hints, `mypy --strict` clean, `ruff` formatting.
- Async I/O with one shared `httpx.AsyncClient` (bounded concurrency, retries, `trust_env=True`).
- Cache only **reduced** documents (npm abbreviated metadata is ~9 MB for `typescript`; we keep a few KB). Atomic writes. Serve stale on network failure and mark `"stale": true`.
- Names: normalize PyPI names with `canonicalize_name`; URL-encode scoped npm names (`quote(name, safe="@")`); validate names before building URLs or paths.
- Tool docstrings are read by agents: 1–2 sentences plus one example. Every extra word costs tokens in every client.
- Small, focused commits; one milestone or fix per commit, tests included.

## Verify, don't recall

Registries, APIs and SDKs change. Before relying on any external format or library API, check it (registry response, installed package signature, official docs). Known traps:

- Python MCP SDK is **v2.x**: `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Do not write v1 code from memory.
- npm abbreviated metadata (`Accept: application/vnd.npm.install-v1+json`) has **no publish times**.
- `nodesemver.satisfies` returns `False` (not an error) for non-semver ranges — guard with `make_range`.
- PyPI `info.version` is not a stability guarantee; apply our own rules. Platform wheels tagged `py3-none-<platform>` (e.g. ruff) are compatible with any Python 3 target.
- endoflife.date: sort releases by `releaseDate` yourself; `latest` can be null; LTS status depends on `ltsFrom` vs today (tests freeze the date).
- The npm package `endoflife-mcp` is endoflife.ai (commercial), not endoflife.date.
- Never pin a dependency version from memory: query the registry.

## Tests

- Default `pytest` is **offline** and deterministic (fixtures + frozen dates). Never add network calls to unmarked tests.
- `live` tests assert properties ("typescript-eslint still caps typescript → a `peer` issue exists", "no canary returned"), never exact version numbers.
- `resolvers` tests are the main correctness guarantee: every tested set we call issue-free must pass `uv pip compile` / `npm install --package-lock-only`; every `peer` / `requires` / `shared` conflict we report must be rejected by them. If they disagree with us, our logic is wrong until proven otherwise. Conflicts deeper than one shared dependency are out of scope without `--resolver-validation`: pick test stacks accordingly.
- The reference stack (React/Vite/TypeScript front, FastAPI/Pydantic/SQLAlchemy back, see `docs/SPEC.md` §9) is the primary end-to-end fixture. When refreshing fixtures, record the date in the fixture file.

## Do not

- Do not add features listed as out of scope for v1 (OSV/vulnerabilities, cooldown, private registries/auth, manifest parsing, per-library Java minimums) without the owner asking. Note ideas in the README roadmap instead.
- Do not copy code from other MCP servers (e.g. package-version-check-mcp) without preserving their license notice.
- Do not weaken a failing test to make it pass; fix the logic or explain why the test was wrong.
- Do not commit cache files, recorded secrets or large raw registry dumps (fixtures must be reduced).