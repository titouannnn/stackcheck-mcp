# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

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
