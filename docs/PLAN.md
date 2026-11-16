# Implementation plan and verification notes

Written 2026-10-27, before coding, as asked by `docs/SPEC.md` §13.

## 1. Facts verified live (2026-10-27)

| Fact | Result |
|---|---|
| MCP SDK | `mcp` 2.3.0. `from mcp.server.mcpserver import MCPServer` works; `mcp.server.fastmcp` raises `ModuleNotFoundError`. `MCPServer(name, instructions=, version=, lifespan=)`, `@server.tool(...)`, `run("stdio" \| "streamable-http", host=, port=)`. Client: `mcp.Client(StdioServerParameters(...))` or `mcp.Client(server)` in-process. |
| Structured output | A return annotation `Annotated[CallToolResult, SomeTypedDict]` lets the tool return its own compact JSON text and still publish an `outputSchema` / validated `structuredContent`. The default text rendering uses `indent=2`, which costs tokens, so we build the `CallToolResult` ourselves. |
| npm abbreviated docs | Top-level keys `dist-tags, modified, name, versions`; per version `dependencies, peerDependencies, peerDependenciesMeta, engines, deprecated, dist, …`; no `time`. `typescript` 8.7 MB. Reference-stack versions and peer ranges match SPEC §9. |
| node-semver 0.9.1 | All ranges in SPEC §5.1.6 evaluate as stated. `make_range(r, loose)` raises `ValueError` for `workspace:*`, `npm:foo@1`, `latest`, garbage. `""`/`x`/`*` mean any. No type hints (mypy override needed). |
| PyPI Simple JSON | `api-version` 1.4; keys as in SPEC. Files carry no `version` field: version must be parsed from the filename. `core-metadata` is a hash dict (truthy); `<url>.metadata` returns METADATA. Per-version JSON (`/pypi/<n>/<v>/json`) is ~30 KB. `project-status` (PEP 792) is present. |
| endoflife.date v1 | Schema 1.2.1, fields as in SPEC. `node` answers **301 → nodejs**, so aliases are resolved by the API (no alias map needed, but `follow_redirects` is required). Unknown slug → 404 HTML. `/api/v1/products` lists 486 products with aliases. |
| Maven Central | Reachable. `spring-boot-starter-web` `<release>` and `<latest>` are **4.2.0-RC1** (pre-release; latest stable 4.1.2). Versions like `1.0.0.RELEASE` exist. `spring-boot-dependencies` 4.1.2 has 46 `import` BOMs and no parent; `jackson-bom` uses a parent and nested properties (`jackson.version.databind` → `${jackson.version}`). `spring-boot-starter-parent` has `<java.version>17</java.version>`: an authoritative, machine-readable Spring Boot Java baseline. |
| Resolvers | `uv` 0.13.6 supports `pip compile - --python-version --no-header --no-annotate --quiet --python-platform`. npm 11.19 rejects typescript 7.0.2 + typescript-eslint 8.72.1 with `ERESOLVE` under `--package-lock-only --ignore-scripts`. |

## 2. Discrepancies with the spec (flagged, with the choice made)

1. **`httpx` vs `httpx2`.** `mcp` 2.3.0 no longer depends on `httpx`; it depends on `httpx2` 2.16 (pydantic's maintained fork, same API, `trust_env` honours `HTTPS_PROXY` / `SSL_CERT_FILE`, OS trust store on Windows/macOS). `httpx`'s last stable is 0.28.1 (2024). We use `httpx2` to avoid shipping two HTTP stacks. Swapping back is a one-line import change.
2. **Java LTS has no `ltsFrom`.** `eclipse-temurin` / `oracle-jdk` mark LTS cycles with `isLts: true, ltsFrom: null`; Node uses `ltsFrom` dates. Active LTS = `ltsFrom <= today`, or `isLts` when `ltsFrom` is null.
3. **`"alternative": null` in the §4.1 example** contradicts "omit empty fields". We omit null/empty fields: no `suggestion` and no `alternative` means no compatible version was found.
4. **No issue kind for the Spring Boot Java check** (§5.3.3). We add kind `java`.
5. Java slug: **`eclipse-temurin`** (vendor-neutral OpenJDK builds with LTS flags; current LTS 25, latest 25.0.5+8).

## 3. Design

- **Pure decision functions** in `npm.py`, `pypi.py`, `maven.py`, `runtimes.py`: reduced docs in, picks + issues out. Fetch-and-reduce functions sit next to them and are the only code that touches the network.
- **`cache.py`**: one JSON file per entry, `<dir>/v1/<namespace>/<url-quoted key>.json`, `{key, fetched_at, data}`. Atomic writes (`mkstemp` in the same dir, then `os.replace`). Per-version documents (PyPI metadata of `name==version`, Maven POMs) are immutable and never expire. `Fetcher` = shared HTTP client + cache + a per-call `stale` flag. TTL 0 disables the cache.
- **Fixtures are cache snapshots**: `scripts/record_fixtures.py` runs the real fetchers into `tests/fixtures/cache/`. Offline tests seed a temp cache from it and use an HTTP transport that fails every request, so the default suite cannot reach the network.
- **`stack.py`** orchestrates `check_stack` (parsing, concurrency, output assembly) so `server.py` stays thin.
- **Output contract** (`models.py`, `TypedDict`s with `NotRequired`): `targets, npm, pypi, maven, runtimes, issues, errors, validation, stale`. Issue = `kind, ecosystem, package, required_by?, range?, target?, suggestion?, alternative?, severity? ("warn"; absent = error), message?`.

## 4. Milestones (one commit each, tests green)

a. runtimes + cache + http · b. npm · c. PyPI · d. MCP server + stdio integration test · e. resolver validation + differential tests · f. Maven (+ BOM, Spring Boot Java check) · g. packaging, CI, README, server.json, `explain_issue`.

## 5. Found during implementation

6. **`isMaintained` includes paid extended support** (Node 18/20, Spring Boot 3.5 are
   `isEol: true, isMaintained: true`). `maintained` therefore also requires the EOL date to
   be in the future.
7. **pydantic drops unknown keys of a `TypedDict` argument**, so `targets: {"ruby": "3"}`
   would vanish silently. `Targets` allows extra keys and the server reports them in
   `errors.targets`.
8. **Maven version order**: implemented from the POM reference's "Version Order
   Specification" and tested against every example in it. Running Maven's own
   `ComparableVersion` from the `maven-artifact` jar as an oracle would be a stronger
   check, but that was not allowed in this environment (it means executing a downloaded jar).
9. **Spring Boot Java baseline**: `java.version` is set directly in
   `spring-boot-starter-parent` (17 for 3.x/4.x, 1.8 for 2.x), so the check reads that POM
   without walking its 140 KB parent BOM.
10. **PyPI per-version metadata** uses the JSON API (`/pypi/<name>/<version>/json`, ~30 KB,
    cached forever, reduced to `requires_dist`/`requires_python`). Using PEP 658
    `.metadata` would mean storing one file URL per version in the reduced index.
11. **Resolver validation never sees user input on a command line**: PyPI requirements go
    through stdin and npm pins into a generated `package.json`.

## 6. Results (2026-10-27)

- Offline suite: `uv run pytest` passes (fixtures recorded 2026-10-27, dates frozen).
- `-m resolvers`: every issue-free set resolves with uv/npm; every conflict pair is rejected.
- `-m live`: property tests pass against the live registries.
- Reference stack, live: cold 1.6 s, warm 60–80 ms, 963 bytes of compact JSON.

## 7. Audit follow-up (2026-11-16)

Found by a full review and live probes, fixed one commit each on `audit-fixes`:

12. **Maven build-date versions**: `commons-collections` answered `20040616`, because
    Maven orders it above `3.2.2`. Such versions are now skipped unless nothing else exists
    or the user's range names one. Central's search index has the same bug (its
    `latestVersion` is `20040616`), so `sweep.py` compares publication dates instead.
13. **Shared dependencies**: two requested packages can need incompatible versions of an
    unrequested one (`fastapi==0.95.0` + `sse-starlette` on starlette;
    `eslint-config-airbnb` + `eslint-plugin-unicorn` on eslint). New issue kind `shared`,
    one level deep. Deeper conflicts stay a `--resolver-validation` matter, and the README
    says so.
14. **Wheels are checked for Linux x86_64** (`any`, `manylinux*_x86_64`), the platform the
    markers and `uv --python-platform linux` assume. Releases without any file are not
    candidates. Cache layout `v2`. The PyPI fixtures were re-reduced from the live Simple
    API, keeping only files uploaded before their recording time.
15. `suggestion` always names the flagged package, and `alternative` a `required_by` one.
    engines/python/java fixes moved to `suggestion`.
16. Robustness: 100 entries per list, a 45 s per-call deadline, `Retry-After`, 404s cached
    for 15 minutes, large JSON parsed off the event loop, cache pruning (60 days), and the
    whole process tree killed on resolver timeouts (npm is a `.cmd` shim on Windows).
    Resolvers use the public registries whatever the user's config says.
17. UX: `name@latest` accepted, EOL targets reported in `warnings`, `runtimes: ["java"]`
    works, no non-loopback HTTP bind without `--allow-remote`.
