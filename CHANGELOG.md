# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-16

### Added

- `check_stack`: latest stable versions for npm, PyPI and Maven packages, with `peer`,
  `requires`, `engines`, `python`, `wheels`, `bom`, `java`, `deprecated`, `yanked`,
  `unsatisfiable` and `resolver` issues, each with an indicative `suggestion` or
  `alternative`.
- `get_runtime_info`: latest, LTS, next LTS, maintained cycles and EOL from endoflife.date.
- `explain_issue`: declarations of one package version.
- stdio (default) and streamable HTTP transports.
- Opt-in `--resolver-validation` with `uv pip compile` and `npm install --package-lock-only`.
- On-disk cache of reduced registry data with TTL and stale fallback.
- `shared` issues: requested packages that need incompatible versions of a package you did
  not request (npm required peers, PyPI `requires_dist`), one level deep.
- `warnings.targets` when a requested target cycle is end-of-life.
- `--call-timeout` (default 45 s per call: partial answer, slow parts in `errors`),
  `--allow-remote`, and `STACKCHECK_CACHE_MAX_AGE` (cache pruning, default 60 days).
- `name@latest` for npm; `node`/`python`/`java` as `runtimes` slugs.
- `SECURITY.md`, `CONTRIBUTING.md`, and a Maven sweep in `scripts/sweep.py`.

### Changed

- `suggestion` is always another version of the flagged package and `alternative` a
  version of a `required_by` package: the fixes for `engines`, `python` and `java` issues
  moved to `suggestion`.
- Wheel checks only count Linux x86_64 wheels (`any`, `manylinux*_x86_64`), the platform
  the other checks assume. The cache layout is now `v2`, and old entries are pruned.
- At most 100 entries per list. A non-loopback `--host` requires `--allow-remote`.
- The resolvers always use the public npm and PyPI registries.

### Fixed

- Maven no longer answers build-date versions (`commons-collections` `20040616` instead of
  `3.2.2`).
- PyPI releases without any installable file are no longer candidates.
- `Retry-After` is honoured. Not-found answers are cached for 15 minutes. Large registry
  documents are parsed off the event loop.
- Resolver timeouts kill the whole process tree (node was left running on Windows).
- Runtime slug suggestions no longer offer unrelated products (`java` → `jira-software`).
