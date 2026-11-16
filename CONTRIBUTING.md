# Contributing

Thanks for helping. Read [AGENTS.md](AGENTS.md) first: it lists the invariants (never
downgrade, stable means stable, flag don't hide, …) that every change must keep. The
product specification is [docs/SPEC.md](docs/SPEC.md).

## Setup and checks

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/
uv run pytest                 # offline and deterministic: must stay green
uv run pytest -m resolvers    # if you touched resolution logic (needs node + uv)
uv run pytest -m live         # optional: live registries
```

## Rules of thumb

- Decision logic is pure: reduced metadata in, picks and issues out. Each new rule gets an
  offline unit test with a fixture.
- Never add network calls to unmarked tests. Fixtures are reduced cache entries under
  `tests/fixtures/cache/`. Record their date when you refresh them.
- A change to the output shape updates `models.py`, the README schema and the tests in the
  same commit.
- A change to a reduced document's shape bumps `LAYOUT_VERSION` in `cache.py`.
- If a resolver test disagrees with us, our logic is wrong until proven otherwise. Do not
  weaken a failing test.
- Make small, focused commits: one fix or feature per commit, with its tests.
- Out of scope for v1 (OSV, cooldown, private registries, manifest parsing, per-library
  Java minimums): open an issue or add the idea to the README roadmap.

Security issues go through [SECURITY.md](SECURITY.md), not public issues.
