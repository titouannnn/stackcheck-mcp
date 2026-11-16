# stackcheck-mcp

An MCP server that gives coding agents the **latest stable version** of every npm, PyPI and
Maven package in a stack, **and every incompatibility between them**, in one call and a few
hundred tokens of JSON.

<!-- mcp-name: io.github.titouannnn/stackcheck-mcp -->

## Why

Agents pin dependencies from stale training data, and "always use latest" is not enough
either. On 2026-10-27:

- `typescript` `dist-tags.latest` is **7.0.2**;
- `typescript-eslint@8.72.1` (latest) declares `peerDependencies.typescript: ">=4.8.4 <6.1.0"`;
- so "latest everything" breaks linting. The compatible pin is **typescript 6.0.3**.

Version-lookup servers answer 7.0.2. `stackcheck-mcp` answers 7.0.2 too (it never
downgrades), and also tells you, with the suggested fix:

```json
{"kind":"peer","ecosystem":"npm","package":"typescript@7.0.2",
 "required_by":"typescript-eslint@8.72.1","range":">=4.8.4 <6.1.0",
 "suggestion":{"typescript":"6.0.3"}}
```

## Install

Claude Code:

```bash
claude mcp add stackcheck -- uvx stackcheck-mcp
```

Any client with a JSON config (Claude Desktop, Cursor, VS Code, …):

```json
{
  "mcpServers": {
    "stackcheck": {"command": "uvx", "args": ["stackcheck-mcp"]}
  }
}
```

Streamable HTTP instead of stdio:

```bash
uvx stackcheck-mcp --http --host 127.0.0.1 --port 8000   # endpoint: http://127.0.0.1:8000/mcp
```

Only `uv` is needed. Public registries only, no credentials.

### Options

| Flag | Default | |
|---|---|---|
| `--http` | off (stdio) | serve streamable HTTP |
| `--host`, `--port` | `127.0.0.1`, `8000` | HTTP bind address |
| `--resolver-validation` | off | also check the picks with real resolvers (see below) |
| `--resolver-timeout` | `120` | seconds per resolver run |
| `--log-level` | `WARNING` | logs go to stderr (stdout is the stdio transport) |

| Environment | Default | |
|---|---|---|
| `STACKCHECK_CACHE_DIR` | platform cache dir (`platformdirs`) | on-disk cache of reduced registry data |
| `STACKCHECK_CACHE_TTL` | `21600` (6 h) | seconds; `0` disables the cache |
| `HTTPS_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR` | | honoured (the OS trust store is used on Windows/macOS) |

## Tools

### `check_stack`

```json
{
  "npm": ["react", "react-dom", "@types/react", "typescript", "typescript-eslint", "vite@^8"],
  "pypi": ["fastapi", "pydantic>=2", "uvicorn[standard]", "sqlalchemy"],
  "maven": ["org.springframework.boot:spring-boot-starter-web", "com.fasterxml.jackson.core:jackson-databind"],
  "maven_bom": "org.springframework.boot:spring-boot-dependencies",
  "runtimes": ["nodejs", "python", "sqlite"],
  "targets": {"node": "24", "python": "3.14", "java": "21"}
}
```

All fields are optional, but at least one list is required.

- npm: `name` or `name@range` (`@types/react@^19`: the range follows the last `@`).
- PyPI: PEP 508 with extras (`uvicorn[standard]>=0.30`); URLs and markers are rejected.
- Maven: `groupId:artifactId[:version|range]`, e.g. `[3.5,4.0)`. A plain version is exact.
- `maven_bom`: a BOM to align against (`groupId:artifactId[:version|range]`, latest by default).
- `runtimes`: any [endoflife.date](https://endoflife.date) slug (aliases like `node` work).
- `targets`: a cycle (`"24"`, `"3.14"`), a prefix (`"3"`) or an exact version. Defaults:
  Node = active maintained LTS, Python = newest released maintained cycle, Java = latest LTS of
  `eclipse-temurin`. A target is resolved (and shown) only when an ecosystem needs it.

Answer for the reference stack (the [spec](docs/SPEC.md) §9 stack, live on 2026-10-27, 963
bytes):

```json
{
  "targets": {"node": "24.22.1", "python": "3.15.2"},
  "npm": {"react": "19.3.1", "react-dom": "19.3.1", "@types/react": "19.3.0",
          "@types/react-dom": "19.3.0", "typescript": "7.0.2", "typescript-eslint": "8.72.1",
          "eslint": "10.13.0", "vite": "8.3.5", "@vitejs/plugin-react": "6.1.2", "vitest": "5.0.5"},
  "pypi": {"fastapi": "0.162.0", "pydantic": "2.14.1", "uvicorn": "0.54.0", "sqlalchemy": "2.1.6",
           "pytest": "9.1.1", "playwright": "1.64.1", "ruff": "0.17.3", "mypy": "2.4.0"},
  "runtimes": {
    "nodejs": {"latest": "26.12.1", "lts": "24.22.1", "next_lts": {"version": "26", "from": "2026-10-28"},
               "maintained": ["26", "24", "22"], "eol": "2028-04-30"},
    "python": {"latest": "3.15.2", "maintained": ["3.15", "3.14", "3.13", "3.12", "3.11"], "eol": "2031-10-31"},
    "sqlite": {"latest": "3.54.1", "maintained": ["3"]}
  },
  "issues": [
    {"kind": "peer", "ecosystem": "npm", "package": "typescript@7.0.2",
     "required_by": "typescript-eslint@8.72.1", "range": ">=4.8.4 <6.1.0",
     "suggestion": {"typescript": "6.0.3"}}
  ],
  "validation": {"npm": "skipped", "pypi": "skipped"}
}
```

### `get_runtime_info`

`{"products": ["nodejs", "python", "eclipse-temurin"]}` → per product: `latest`, `lts`
(products with LTS), `lts_eol: true` when that LTS is past its end of life (Laravel 6),
`next_lts` (`{"version", "from"}` when a cycle becomes LTS later), `maintained` (newest
first, max 5, past-EOL extended-support cycles excluded) and `eol` of the recommended cycle
(the LTS if still maintained, else the newest maintained). Unknown slugs get up to 3
suggestions.

```json
{"runtimes": {"nodejs": {"latest": "26.12.1", "lts": "24.22.1", "next_lts": {"version": "26", "from": "2026-10-28"},
              "maintained": ["26", "24", "22"], "eol": "2028-04-30"}},
 "errors": {"pythn": "unknown product; did you mean: python, ..."}}
```

### `explain_issue`

`{"ecosystem": "npm", "package": "typescript-eslint", "version": "8.72.1"}` → the declarations
of one version, so an agent can dig into an issue without downloading registry documents:
npm `peerDependencies`/`optionalPeers`/`engines`/`deprecated`, PyPI `requires_dist`
(unconditional lines plus those of the extras named in `package`, e.g. `fastapi[standard]`),
`extras`, `requires_python`, `yanked`; Maven `parent` and direct `dependencies`. The version
defaults to the latest stable one.

## Output schema

Every empty field is omitted: no `null`, `[]` or `{}`. The tools also publish an
`outputSchema`, and the result arrives both as `structuredContent` and as compact JSON text.

`check_stack`:

| Field | |
|---|---|
| `targets` | `{"node"\|"python"\|"java": full version}` used for the checks |
| `npm`, `pypi`, `maven` | `{name: latest stable version}`. PyPI names are PEP 503-normalized; Maven keys are `group:artifact` (the BOM is included) |
| `runtimes` | `{slug: runtime info}` as in `get_runtime_info` |
| `issues` | list of issues (below) |
| `errors` | `{"npm"\|"pypi"\|"maven"\|"runtimes"\|"targets": {item: message}}`. One failure never fails the call |
| `validation` | `{"npm"\|"pypi": "skipped"\|"ok"\|"failed"\|"unavailable"\|"timeout"}` |
| `stale` | `true` when some data came from an expired cache entry because a registry was unreachable |

Issue:

| Field | |
|---|---|
| `kind` | see below |
| `ecosystem` | `npm`, `pypi`, `maven` |
| `package` | the offending package at its picked version (`name@v`, `name==v`, `g:a:v`) |
| `required_by` | the package (or BOM) that declares the constraint |
| `range` | the declared constraint |
| `target` | the runtime target checked (`node 24.22.1`, `python 3.15.2`, `java 21.0.13+9`) |
| `suggestion` | highest stable version of `package` that satisfies every constraint the other requested packages put on it, and your own constraint |
| `alternative` | when there is no suggestion: highest stable version of the constraining package that accepts the picked one |
| `severity` | `"warn"`; absent means error |
| `message` | resolver output, deprecation or yank reason, … |

`suggestion` and `alternative` are indicative only. The primary answer never moves.

| Kind | Meaning |
|---|---|
| `peer` | npm `peerDependencies` between requested packages (optional peers count when requested) |
| `requires` | PyPI `requires_dist` between requested packages (markers evaluated for the target, extras honoured) |
| `engines` | npm `engines.node` vs the Node target |
| `python` | `requires_python` vs the Python target |
| `wheels` | no wheel for the target CPython (`warn` when an sdist exists) |
| `bom` | Maven version differs from the one managed by `maven_bom` (`warn`) |
| `java` | Spring Boot's Java baseline (`java.version` of `spring-boot-starter-parent`) vs the Java target |
| `deprecated` | npm deprecated version, PyPI project archived/deprecated/quarantined (`warn`) |
| `yanked` | the latest PyPI version is yanked (`warn`; the pick prefers a non-yanked one) |
| `unsatisfiable` | your own constraint matches no stable version |
| `resolver` | a real resolver rejected the set (only with `--resolver-validation`) |

## How versions are chosen

- **npm**: versions matching `^\d+\.\d+\.\d+$` only, never above `dist-tags.latest`
  (unless `latest` is itself a pre-release). Canary/next/experimental tags are ignored.
- **PyPI**: PEP 440 final releases (no pre/dev). A version whose files are all yanked is
  used only if nothing else matches, and is always flagged.
- **Maven**: no alpha/beta/rc/cr/milestone/snapshot/preview/ea qualifiers (`-M1`, `.RC2`,
  `-SNAPSHOT`, `a1`, `b2`, …), ordered with Maven's `ComparableVersion` rules. The
  `<release>` tag of `maven-metadata.xml` is not trusted (it is often a release candidate).
  Early build-date versions (`commons-collections` `20040616`, which Maven orders above
  `3.2.2`) are skipped unless the artifact has nothing else or your range names one.
- Version comparisons are numeric, never lexicographic (`6.0.10 > 6.0.9`).
- Ranges that cannot be evaluated (`workspace:*`, `npm:` aliases, `file:`, git URLs, dist-tag
  names) never create issues.

## Resolver validation (`--resolver-validation`)

Off by default. When enabled, and if the binaries are on `PATH`:

- PyPI: `uv pip compile -` on the pinned set (`--python-version <target> --python-platform
  linux`). If uv rejects it, a `resolver` issue carries uv's explanation, and an unpinned run
  supplies uv's own choices as `suggestion`.
- npm: `npm install --package-lock-only --ignore-scripts --no-audit --no-fund` in a fresh
  temp directory. `ERESOLVE` becomes a `resolver` issue.

No shell, no install scripts, a timeout on every run. Package names reach the resolvers
through stdin or a generated `package.json`, never through the command line. This adds about
2 s per call on a warm machine.

## Data sources and caching

npm abbreviated metadata, the PyPI JSON Simple API (PEP 691) plus per-version JSON, Maven
Central `maven-metadata.xml` and POMs, and endoflife.date API v1. Only **reduced** data is
cached (a few KB per package; `typescript`'s 8.7 MB document becomes 2 KB). Per-version data
(PyPI metadata, POMs) never expires. Writes are atomic and safe across processes. When a
registry is unreachable, expired entries are served and the answer says `"stale": true`.

Measured for the reference stack: cold (empty cache) 1.6 s, warm 60–80 ms.

## Roadmap (not in v1)

- Vulnerability data (OSV).
- Cooldown / minimum release age.
- Private registries and authentication.
- Manifest parsing (`package.json`, `pyproject.toml`, `pom.xml`).
- Per-library Java minimums for Maven artifacts (v1 checks Spring Boot only).
- Gradle-style ranges, transitive peer analysis.

## Development

```bash
uv sync --all-extras
uv run pytest                  # offline, deterministic (recorded fixtures, frozen dates)
uv run pytest -m live          # live registries, asserts properties
uv run pytest -m resolvers     # differential tests vs real uv/npm (needs node + uv)
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/
uv run python scripts/record_fixtures.py   # refresh tests/fixtures/cache from the registries
uv run python scripts/reference_run.py     # reference stack, live, with timings
```

The differential tests are the main correctness guarantee. Every set `check_stack` calls
issue-free must resolve with `uv pip compile` / `npm install --package-lock-only`, and every
`peer`/`requires` conflict it reports must be rejected by them. See [AGENTS.md](AGENTS.md)
for the project's invariants and [docs/PLAN.md](docs/PLAN.md) for verified facts and
deviations from the original spec.

## License

Apache-2.0
