# Contributing to Loom

Contributions are accepted under the [MIT License](LICENSE). No contributor licence agreement is required.

## Development setup

Loom uses `uv` and the committed lockfile:

```sh
uv sync --locked --group integration
```

Run commands through the synced environment (`uv run --locked --no-sync ...`) so test runs do not mutate it.

## Checks

```sh
uv run --locked --no-sync python -m pytest -q            # default suite
uv run --locked --no-sync python -m pytest tests/test_distribution.py tests/test_source_structure.py -q
uv run --locked --no-sync python -m pytest --cov -q      # 80% coverage floor (full suite only)
uv run --locked --no-sync python -m pytest -m integration -q
uv build --offline
```

The `--cov` run measures coverage for the `loom` package against the 80% floor configured in `pyproject.toml`; run it against the full default suite only - subset runs cover part of the package and are expected to fall below the floor. The default suite excludes tests marked `integration`; the integration selection includes real dbt lifecycle tests and isolated consumer installation, and may need locked wheels in uv's cache - report a blocked check as blocked, never as passed. After the initial sync, `--no-sync` keeps test runs from changing the shared environment; do not run a sync concurrently with another contributor task's tests.

CI runs the same checks on GitHub-hosted runners for every push to `main` and every pull request: the default suite with the coverage floor across Python 3.11–3.14 on Ubuntu (pull requests run a single Python to bound cost; the full matrix runs on pushes to `main`), the default suite on Windows, the dbt runtime integration tests, the installed-consumer test, and a quickstart end-to-end leg. There is no self-hosted runner fleet and there are no database services: DuckDB is the only executed dialect and it runs in-process, so the quickstart executes on DuckDB only.

Report a blocked check as blocked, never as passed.

## Change expectations

- Keep changes focused; prefer small, scoped diffs.
- Every behaviour change ships with tests under `tests/`; bug fixes add regression tests.
- Documentation describes current implementation only. If a change alters behaviour, update `docs/architecture.md` (and the README / command reference where they speak to the same surface) in the same change.
- Record significant design decisions as ADRs under `docs/adr/`.
- Add a `CHANGELOG.md` entry under *Unreleased* for user-visible changes.
- Python ≥ 3.11, type hints on new functions, no formatter/linter mandates - match the surrounding style.
- Generated files carry the Loom provenance header; renderers must keep emitting it.

See the [command reference](docs/commands.md) for the practitioner surface and the architecture document's contributor workflow and code map for where things live.
