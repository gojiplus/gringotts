# Contributing

Development uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-groups
uv run pytest
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pydoclint src/
```

Conformance with the fleet standard is checked by
[preen](https://github.com/gojiplus/preen):

```bash
uvx preen check --strict
```

To run the test suite against Postgres instead of SQLite:

```bash
GRINGOTTS_TEST_DATABASE_URL=postgresql://... uv run pytest
```

Releases are tag-driven (`preen release X.Y.Z`); never hand-edit versions —
the git tag is the version.
