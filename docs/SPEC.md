# Build `stackcheck-mcp` — an MCP server for latest stable versions + cross-package compatibility

You are going to design, implement, test and package an open-source MCP server called **`stackcheck-mcp`**. Read this whole document before writing any code. It contains decisions that were already made with the project owner, plus lessons learned from investigating existing tools and from real experiments against the registries. Do not re-litigate the decisions; do flag anything you find to be factually wrong.

---

## 1. Why this project exists

Coding agents pin dependency versions from stale training data. The usual fix (web searches) burns thousands of tokens per library and is often wrong. Existing MCP servers fix the *staleness* but not the *compatibility* problem.

Real example that motivates the project (verified on 2026-10-27):

- `npm` `dist-tags.latest` for `typescript` is **7.0.2**.
- But `typescript-eslint@8.72.1` (latest) declares `peerDependencies.typescript: ">=4.8.4 <6.1.0"`.
- So an agent following "always use latest" produces a stack where linting breaks. The correct pin is **typescript 6.0.3**.
- Every existing MCP server investigated (see §10) would have answered 7.0.2.

`stackcheck-mcp` returns the latest stable version of each requested package **and** reports every incompatibility between them, with a suggested compatible version — in **one call** and a **compact JSON** answer, so the agent spends a few hundred tokens instead of tens of thousands.

---

## 2. Fixed decisions (agreed with the owner — do not change)

| Topic | Decision |
|---|---|
| Name | `stackcheck-mcp` (PyPI package and MCP server name) |
| License | Apache-2.0, open source, public GitHub repo |
| Language | Python (target `requires-python >=3.12`) |
| Install | `uvx stackcheck-mcp` must be all a user needs |
| Transports | stdio (default) and streamable HTTP via a flag (`--http`, `--host`, `--port`) |
| Scope v1 | npm, PyPI, Maven (latest + BOM alignment), runtimes via endoflife.date |
| Registries | Public registries only (no private registries, no auth) in v1 |
| Input | Lists of package names with optional constraints (no manifest files, no file paths) |
| Core rule | **Never downgrade.** The primary answer is always the latest stable version. Incompatibilities are **reported** with their cause and an **indicative suggestion** (highest compatible version), never applied |
| Deprecated / yanked | **Flag** them, never silently exclude |
| Runtime targets | Optional parameters; defaults: Node = active LTS, Python = latest stable cycle, Java = latest LTS |
| Real resolvers | Running `uv` / `npm` is **off by default**; enabled only by a startup flag `--resolver-validation`. They are **mandatory in the project's own test suite and CI** |
| Output | Compact structured JSON; omit empty fields |
| Cache | On-disk, configurable TTL, serves stale data when the network fails |
| Tool surface | 2–3 focused tools (see §4) |
| Not in v1 | Vulnerability lookups (OSV), cooldown/min-age, private registries, manifest parsing, Java-minimum checks per Maven library. Put these in a "Roadmap" section of the README |

---

## 3. Definitions of "stable" (get these exactly right)

- **npm**: only versions matching `^\d+\.\d+\.\d+$` (no `-canary`, `-beta`, `-rc`, `-dev`, `-experimental`, `-insiders`, …). The `latest` dist-tag is an **upper bound**: never return a stable version greater than `dist-tags.latest` (maintainers sometimes publish a higher stable under `next` before promoting it). If `latest` itself is not stable, use the highest stable version.
- **PyPI**: PEP 440 via `packaging.version.Version`; exclude `is_prerelease` and `is_devrelease`. A version whose files are **all** yanked counts as yanked → still a candidate only if nothing else exists, and always flagged. Prefer non-yanked.
- **Maven**: exclude versions whose qualifier matches (case-insensitive) `alpha|beta|rc|cr|m\d|milestone|snapshot|preview|ea|b\d` (e.g. `-M1`, `.RC2`, `-SNAPSHOT`). Use `maven-metadata.xml` `<versions>` and compute the max with Maven ordering rules (implement or use a small, tested comparator; do not sort strings lexicographically).
- **Runtimes** (endoflife.date): see §6.

Deprecated npm versions (field `deprecated` in metadata) and yanked PyPI versions are reported in `issues` with `kind: "deprecated"` / `"yanked"` when they are the latest.

---

## 4. MCP tools

Keep tool descriptions short: every word is loaded into the agent's context.

### 4.1 `check_stack` (the main tool)

Input (all optional except at least one list):

```json
{
  "npm":     ["react", "react-dom", "@types/react", "typescript", "typescript-eslint", "vite@^8"],
  "pypi":    ["fastapi", "pydantic>=2", "uvicorn[standard]", "sqlalchemy"],
  "maven":   ["org.springframework.boot:spring-boot-starter-web", "com.fasterxml.jackson.core:jackson-databind"],
  "maven_bom": "org.springframework.boot:spring-boot-dependencies",
  "runtimes": ["nodejs", "python", "sqlite"],
  "targets": {"node": "24", "python": "3.14", "java": "21"}
}
```

- npm constraint syntax: `name@range` (scoped names: split on the **last** `@` with index > 0, e.g. `@types/react@^19`).
- PyPI constraint syntax: PEP 508 (`packaging.requirements.Requirement`), extras allowed.
- Maven: `groupId:artifactId[:version-range-or-version]`.
- `targets` override the runtime defaults (§6).

Output (compact; omit any empty key):

```json
{
  "targets": {"node": "24.22.1", "python": "3.15.2"},
  "npm":  {"react": "19.3.1", "typescript": "7.0.2", "typescript-eslint": "8.72.1"},
  "pypi": {"fastapi": "0.162.0", "pydantic": "2.14.1"},
  "maven": {"org.springframework.boot:spring-boot-starter-web": "x.y.z"},
  "runtimes": {"nodejs": {"lts": "24.22.1", "latest": "26.12.1", "next_lts": {"version": "26", "from": "2026-10-28"}}},
  "issues": [
    {
      "kind": "peer",
      "ecosystem": "npm",
      "package": "typescript@7.0.2",
      "required_by": "typescript-eslint@8.72.1",
      "range": ">=4.8.4 <6.1.0",
      "suggestion": {"typescript": "6.0.3"},
      "alternative": null
    }
  ],
  "errors": {"npm": {"left-pad-zzz": "not found"}},
  "validation": {"npm": "skipped", "pypi": "skipped"}
}
```

Issue kinds: `peer` (npm peerDependencies), `requires` (PyPI requires_dist between requested packages), `engines` (npm `engines.node` vs Node target), `python` (requires_python vs Python target), `wheels` (no wheel for the Python target), `bom` (Maven version differs from the one managed by the BOM), `deprecated`, `yanked`, `unsatisfiable` (user constraint matches nothing), `resolver` (real resolver rejected the set; only with `--resolver-validation`).

`suggestion` semantics (indicative, never applied):
- `suggestion`: highest stable version of the **constrained** package that satisfies **all** constraints imposed on it by the other requested packages (at their latest) and by the user's own constraint.
- `alternative`: if no such version exists, the highest stable version of the **constraining** package whose declared range accepts the constrained package's latest. `null` if neither exists.

### 4.2 `get_runtime_info`

Input: `{"products": ["nodejs", "python", "eclipse-temurin", "sqlite", "spring-boot"]}` (any endoflife.date slug).
Output per product: `latest`, `lts` (if the product has LTS), `next_lts` (if a cycle has `ltsFrom` in the future), `maintained` (list of maintained cycle names, newest first, max 5), `eol` of the recommended cycle. Unknown slug → error with up to 3 close slug suggestions (edit distance against the cached product list).

### 4.3 `explain_issue` (optional, implement last)

Input: one package + version + ecosystem. Output: full peer/requires/engines/requires_python declarations of that exact version, so the agent can dig into a conflict without fetching whole registry documents. Keep it small.

---

## 5. Compatibility checks — algorithms

### 5.1 npm

1. Fetch **abbreviated** metadata: `GET https://registry.npmjs.org/<name>` with header `Accept: application/vnd.npm.install-v1+json`. URL-encode scoped names (`@types%2freact`; e.g. `urllib.parse.quote(name, safe="@")`). Abbreviated docs contain `dist-tags`, `versions[v].{dependencies, peerDependencies, peerDependenciesMeta, engines, deprecated, …}`, `modified` — **no per-version publish times** (those are only in the full document, which you do not need in v1).
   - Measured sizes: `typescript` abbreviated ≈ 8.7 MB vs full ≈ 15.9 MB; `react` 3.0 MB vs 7.1 MB. Therefore **cache only a reduced form**: stable versions only, and per version only `peerDependencies`, `peerDependenciesMeta`, `engines`, `deprecated`; plus `dist-tags`. That is a few KB per package.
2. Pick `latest` per §3, constrained by the user range if any.
3. For every requested package A at its picked version, for every `peerDependencies[B] = range` where B is also requested: if `B@picked` does not satisfy `range`, emit a `peer` issue. Optional peers (`peerDependenciesMeta[B].optional`) **still constrain** when B is present.
4. If a Node target exists, check `engines.node` of each picked version → `engines` issue.
5. Compute `suggestion` / `alternative` as in §4.1. Do **not** iterate to a "fixpoint" that changes the primary answer — the primary answer never moves.
6. Range evaluation: use the PyPI package `node-semver` (import name `nodesemver`, v0.9.1 verified) with `loose=True`. Verified correct on real ranges: `>=4.8.4 <6.1.0`, `^8.0.0`, `^0.1.7 || ^0.2.0`, `^22.0.0 || >=24.0.0`, `^5.0.0-beta.5 || >=5.0.0`, `*`, `~6.0.9` (6.0.10 ✓), `1.x`, `1.2 - 1.6`. **Pitfall**: for non-semver ranges (`workspace:*`, `npm:foo@1`, `file:`, `git:`, dist-tag names like `latest`, garbage) `satisfies` returns `False`, which would create false conflicts. Treat a range as *unevaluable* when it contains `:` or `nodesemver.make_range(r, loose=True)` fails/returns None; unevaluable ranges never produce issues (optionally a low-severity note).
7. Version ordering: compare numeric tuples, never strings (`"6.0.10" < "6.0.9"` lexicographically).

### 5.2 PyPI (default mode: metadata only, no uv)

1. Version list + per-file metadata: prefer the PEP 691 JSON Simple API `GET https://pypi.org/simple/<name>/` with `Accept: application/vnd.pypi.simple.v1+json` (files with `yanked`, `requires-python`, `upload-time`, plus `versions`). Fallback: `https://pypi.org/pypi/<name>/json`.
   - Do **not** just use `info.version` from the JSON API (that is what package-version-check-mcp does); apply §3 yourself.
2. Per-version dependency metadata: `GET https://pypi.org/pypi/<name>/<version>/json` → `info.requires_dist`, `info.requires_python`. Cache reduced. Lighter alternative (verified available): Simple API files carry `core-metadata` (PEP 714); when truthy, `GET <file url>.metadata` returns just the wheel's METADATA (parse `Requires-Dist` / `Requires-Python` with `email.parser`). Simple API top-level keys verified: `files, meta, name, project-status, versions`; file keys: `core-metadata, data-dist-info-metadata, filename, hashes, provenance, requires-python, size, upload-time, url, yanked`.
3. `requires_python` of each picked version vs Python target → `python` issue (`packaging.specifiers.SpecifierSet`).
4. `requires_dist` between **requested** packages: parse with `Requirement`; evaluate markers for the target environment (`python_version`, `python_full_version`, `implementation_name=cpython`, `sys_platform=linux`, `platform_system=Linux` by default); requirements guarded by `extra == "x"` apply only if the user requested that extra. If the requirement's specifier rejects the other package's picked version → `requires` issue with suggestion/alternative.
5. Wheel check for the Python target: parse wheel filenames with `packaging.utils.parse_wheel_filename`. Compatible if any wheel is `py3-none-any`/`py2.py3-none-any`, or has a `cp3XY` tag equal to the target, or an `abi3` wheel with `cp3XY ≤ target`. If only an sdist exists for the target → `wheels` issue (severity "warn": it will build from source). Pure-Python wheels (`ruff`, `playwright` ship platform wheels tagged `py3-none-<platform>`) count as compatible.
   - Verified on 2026-10-27: Python 3.15 released 2026-10-09; `pydantic-core 2.50.1`, `sqlalchemy 2.1.6`, `greenlet 3.5.6`, `mypy 2.4.0`, `httptools 0.9.0`, `uvloop 0.23.0` already ship `cp315` wheels; `ruff 0.17.3` ships no cp-tagged wheels (it is `py3-none-*`) — must NOT be flagged.
6. Normalize names per PEP 503 (`packaging.utils.canonicalize_name`) everywhere.

### 5.3 Maven

1. Latest: `https://repo1.maven.org/maven2/<group/with/slashes>/<artifact>/maven-metadata.xml` → `<versioning><versions>`; apply §3. (Do not trust `<release>`/`<latest>` blindly; they can be pre-releases.)
2. BOM alignment (only if `maven_bom` is given): resolve the BOM's latest stable version, download its POM (`.../<v>/<artifact>-<v>.pom`), read `<dependencyManagement>`, resolve `${property}` placeholders from `<properties>`, and follow `<scope>import</scope>` BOMs recursively (depth limit 5, cycle guard). For each requested artifact managed by the BOM: if the BOM-managed version ≠ the latest stable → `bom` issue with `suggestion` = BOM-managed version. (For Spring Boot users the BOM-managed version is usually what they want; the latest is still the primary answer per the core rule.)
3. Java target: v1 only checks Spring Boot's minimum Java against `targets.java`. Get that minimum from an authoritative source at runtime if one exists (e.g. the `java.version` property in `spring-boot-starter-parent` POM for that version); if you cannot find a reliable machine-readable source, document it and skip — **never hardcode values from memory**.
4. Note: Maven Central was not reachable from the environment where this research was done, so nothing Maven-related here was verified live. Verify endpoints and formats yourself first.

### 5.4 `--resolver-validation` (opt-in at server start)

When enabled and the binaries exist (check with `shutil.which`; if missing, report `validation: {"npm": "unavailable"}` and continue):
- **PyPI**: write the picked set pinned (`name==version`, extras kept) to stdin of
  `uv pip compile - --python-version <target> --no-header --no-annotate --quiet`. Exit ≠ 0 → `resolver` issue containing the last ~15 lines of uv's message (it is precise: e.g. "Because nonexistent-pkg-zzz was not found in the package registry…"). Also run once **unpinned** and report uv's own choice as `suggestion` when it differs. Verified: unpinned resolution of the reference Python stack returns exactly the latest versions; `fastapi` + `pydantic<2.13` correctly yields `pydantic==2.12.5`.
- **npm**: in a fresh temp dir, write a `package.json` with the pinned set, run `npm install --package-lock-only --ignore-scripts --no-audit --no-fund`. `ERESOLVE` / non-zero exit → `resolver` issue with the trimmed message. Never run install scripts.
- Use `asyncio.create_subprocess_exec` with a timeout (default 120 s), never `shell=True`.

---

## 6. Runtimes (endoflife.date API v1)

- `GET https://endoflife.date/api/v1/products/<slug>` → `{schema_version, generated_at, last_modified, result: {name, aliases, label, category, tags, versionCommand, identifiers, labels, links, releases: [...]}}`.
- Each release has: `name, codename, label, releaseDate, isLts, ltsFrom, isEoas, eoasFrom, isEol, eolFrom, isEoes, eoesFrom, isMaintained, latest: {name, date, link}, custom`. `latest` may be null → fall back to `name`.
- Sort releases by `releaseDate` desc yourself; do not assume order.
- Default targets:
  - **Node**: highest release with `isLts == true` and `ltsFrom <= today` (active LTS). Also expose `next_lts` = release with `ltsFrom > today`. Verified 2026-10-27: Node 24 LTS (latest 24.22.1), Node 26 becomes LTS on **2026-10-28** — a great test case for date-boundary logic.
  - **Python**: highest release with `isMaintained` and `releaseDate <= today` (verified: 3.15, latest 3.15.2; 3.14 latest 3.14.10).
  - **Java**: latest LTS of the slug you choose (check which slug exists, e.g. `eclipse-temurin` or `oracle-jdk`; document the choice).
- Accept common aliases in input (`node` → `nodejs`); verify whether the API resolves aliases itself before adding a map.
- `targets` can be a cycle (`"24"`, `"3.14"`) — resolve it to that cycle's latest patch.

---

## 7. Non-functional requirements

- **HTTP**: one shared `httpx.AsyncClient` (timeouts, `follow_redirects`, a clear `User-Agent: stackcheck-mcp/<version>`), bounded concurrency (semaphore ~8), retries for transient errors. Respect `HTTPS_PROXY` and `SSL_CERT_FILE` (httpx `trust_env=True`).
- **Cache**: directory `$STACKCHECK_CACHE_DIR` (default: platform cache dir, e.g. via `platformdirs`), TTL `$STACKCHECK_CACHE_TTL` seconds (default 21600 = 6 h; 0 disables). Store **reduced** JSON only; atomic writes (temp file + `replace`); on network failure serve stale entries and add `"stale": true` to the output. Cache must be safe with concurrent server processes.
- **Errors**: never fail the whole call because one package failed; put it in `errors`.
- **Performance target**: a warm-cache `check_stack` for the reference stack (§9) in < 300 ms; cold < 10 s.
- **Logging**: to stderr only (stdout is the stdio transport).
- **Security**: no `shell=True`; `--ignore-scripts` for npm; validate package names (reject path separators, whitespace, control chars) before building URLs or temp files.

---

## 8. MCP SDK — pitfalls already found

- The current Python MCP SDK is **v2.x** (2.3.0 verified on 2026-10-27). `from mcp.server.fastmcp import FastMCP` **raises** in 2.x: FastMCP was renamed to **`MCPServer`** (`from mcp.server.mcpserver import MCPServer`). `MCPServer(name, instructions=..., version=...)`, `@server.tool(...)` decorator, `server.run(transport="stdio" | "streamable-http")`. Check the migration guide (https://py.sdk.modelcontextprotocol.io/v2/migration/) and the installed package's signatures before coding; do not write v1 code from memory.
- Return structured output (typed return / dict) so clients get `structuredContent` plus the JSON text.
- Write tool docstrings for agents: one or two sentences, with one example input. Add server `instructions` telling the agent to call `check_stack` once with the whole stack before pinning versions.

---

## 9. Reference stack and verified ground truth (2026-10-27)

Use this stack as the primary end-to-end fixture. Record (reduced) registry responses into `tests/fixtures/` so unit tests run **offline** and deterministically.

| Package | Latest stable (verified) | Notes for expected issues |
|---|---|---|
| react / react-dom | 19.3.1 | dist-tags also has canary/next/experimental/backport `19.1.10` — must be ignored |
| @types/react / @types/react-dom | 19.3.0 | many `tsX.Y` dist-tags — ignore |
| typescript | **7.0.2** (6.x latest: 6.0.3) | `peer` issue from typescript-eslint → suggestion 6.0.3 |
| typescript-eslint | 8.72.1 | peer `typescript: >=4.8.4 <6.1.0`, peer `eslint: ^8.57.0 \|\| ^9.0.0 \|\| ^10.0.0` |
| vite | 8.3.5 | engines.node `^20.19.0 \|\| >=22.12.0` |
| @vitejs/plugin-react | 6.1.2 | peer `vite: ^8.0.0` (+ optional peers) |
| vitest | 5.0.5 | peer `vite: ^6.4.0 \|\| ^7.0.0 \|\| ^8.0.0`, `@types/node: ^22.0.0 \|\| >=24.0.0` |
| eslint | 10.13.0 | engines.node `^20.19.0 \|\| ^22.13.0 \|\| >=24` |
| fastapi | 0.162.0 (2026-10-23) | requires_python >=3.10 |
| pydantic | 2.14.1 | requires `pydantic-core==2.50.1` |
| uvicorn | 0.54.0 | |
| sqlalchemy | 2.1.6 | |
| pytest / playwright | 9.1.1 / 1.64.1 | |
| ruff | 0.17.3 (published 2026-10-27 00:33 UTC) | must not get a `wheels` issue |
| mypy | 2.4.0 | |
| Node / Python / SQLite | 24.22.1 LTS (26 LTS from 2026-10-28) / 3.15.2 / 3.54.1 | |

Live tests (marked `@pytest.mark.live`, excluded by default) must assert **properties**, not exact numbers (they drift): e.g. "typescript-eslint latest still caps typescript → a `peer` issue exists while the cap holds", "returned npm versions are stable and ≤ dist-tags.latest", "no canary ever returned".

---

## 10. Lessons from existing tools (do not repeat their mistakes)

- **sammcj/mcp-package-version**: npm package frozen since 2025-04; Go repo archived 2026-03-29. Superseded by **sammcj/mcp-devtools**, whose npm logic (`internal/tools/packageversions/npm/npm.go`, `cooldown.go`) has two bugs worth avoiding:
  1. During its 72 h cooldown it falls back to "the most recently published version before the threshold" **without filtering prereleases** → for React (daily canaries) it can recommend a canary.
  2. Its major-version constraint uses `sort.Strings` (lexicographic) over all versions including prereleases.
- **MShekow/package-version-check-mcp** (Apache-2.0, very active, FastMCP-based, uvx/Docker/hosted): good batch input design and good ecosystem coverage; npm = `dist-tags.latest`, PyPI = `info.version`, runtimes via `mise ls-remote` (gives latest Node, **not LTS**). The author deliberately does **no** compatibility checks — that gap is exactly this project's reason to exist. Feel free to read it for registry-format details; do not copy code without keeping its license notice.
- **Context7** returns indexed *documentation* versions, not the latest stable — not a version oracle; complementary.
- **endoflife.date** is the best source for runtime LTS/EOL. Note: the npm package named `endoflife-mcp` belongs to the commercial **endoflife.ai** (different data, tiers, telemetry), not to endoflife.date.
- deps.dev was not testable during research; not used in v1.

---

## 11. Project layout and packaging

```
stackcheck-mcp/
  pyproject.toml            # hatchling; [project.scripts] stackcheck-mcp = "stackcheck_mcp.__main__:main"
  src/stackcheck_mcp/
    __main__.py             # argparse: --http --host --port --resolver-validation --log-level
    server.py               # MCPServer + tools (thin layer)
    models.py               # typed results / issues
    cache.py  http.py
    npm.py  pypi.py  maven.py  runtimes.py
    semver_utils.py  maven_version.py
    validation.py           # uv / npm subprocess validation
  tests/
    fixtures/               # reduced recorded registry data
    test_npm.py test_pypi.py test_maven.py test_runtimes.py test_server.py
    test_validation_differential.py
  README.md  LICENSE (Apache-2.0)  CHANGELOG.md  server.json (MCP registry)
  .github/workflows/ci.yml  release.yml
```

- Pure functions for all decision logic (input: reduced metadata; output: picks + issues), I/O at the edges → offline unit tests.
- Dependencies: `mcp>=2`, `httpx`, `packaging`, `node-semver`, `platformdirs`. Check the current version of each before pinning (use the registries, not memory).
- README: one-line install for Claude Code (`claude mcp add stackcheck -- uvx stackcheck-mcp`), JSON config for other clients, HTTP mode, `--resolver-validation`, env vars, output schema, the TypeScript example as motivation, roadmap (§2 "Not in v1").
- Publish to PyPI via trusted publishing; add `server.json` for the official MCP registry (check its current schema).

---

## 12. Testing strategy (mandatory)

1. **Unit, offline** (default `pytest`): stable filtering per ecosystem (canary/rc/M1/SNAPSHOT/dev/yanked cases), version ordering (`6.0.10 > 6.0.9`), scoped-name parsing, unevaluable ranges, optional peers, suggestion/alternative computation, marker evaluation with extras, wheel-tag compatibility (incl. abi3 and `py3-none-<platform>`), endoflife date boundaries (freeze "today" to 2026-10-27 and 2026-10-28 for the Node 26 LTS switch), BOM property + import resolution, cache TTL/stale behaviour, partial failures.
2. **MCP integration**: start the server over stdio with the SDK client, call each tool, validate the output schema.
3. **Differential tests against real resolvers** (CI, need Node + uv): for a set of stacks (reference stack, the TypeScript conflict, `fastapi` + `pydantic<2.13`, a deliberately impossible pin), assert that every set reported **issue-free** passes `uv pip compile` / `npm install --package-lock-only`, and every reported `peer`/`requires` conflict is rejected by them. This is the project's main correctness guarantee.
4. **Live tests** (`-m live`, nightly in CI) as described in §9.
5. CI matrix: Python 3.12–3.15 on Linux; one macOS and one Windows job for the stdio path; `ruff` + `mypy --strict` on `src/`.

---

## 13. How to work

1. Start by verifying the facts you depend on: MCP SDK API, PyPI Simple API format, endoflife.date v1 schema, Maven Central metadata format. Write down any discrepancy with this document.
2. Propose a short plan (modules, data models, milestones) before coding. Milestones: (a) runtimes + cache, (b) npm, (c) PyPI, (d) MCP server + stdio integration test, (e) resolver validation + differential tests, (f) Maven, (g) packaging/CI/README.
3. Commit after each milestone with green tests.
4. At the end, run the reference stack through `check_stack` and show me the exact JSON output, plus warm/cold timings.
