"""Runtime proofs for the `filter` parameter (ADR 0038, DuckDB,
integration group).

Four lifecycles, one per load-bearing claim:

- Silver: the predicate applies BEFORE dedup ranking - a key
  whose latest version fails the filter keeps its earlier
  passing version as winner (filter-after-ranking would drop
  the key entirely) - and the filter + dedup + incremental
  lifecycle converges with a full refresh.
- Dimension: filter + incremental (merge with a cursor) - a
  filtered-out member never lands, an in-filter member updates
  in place with its surrogate stable, and a full refresh's
  contents equal the incremental accumulation.
- Staging: the predicate restricts the source scan at build
  time.
- SCD2: the predicate sits on the snapshot's input select - a
  business key that fails it never enters history at all, and
  enters (with no backfilled history) only from the load where
  it starts passing.
"""
from __future__ import annotations

import csv
from pathlib import Path
from types import ModuleType

import pytest

from loom.cli import main
from dbt_harness import dbt_or_skip, duckdb_profiles_yml, run_dbt


def _write_project_shell(project: Path, name: str, *, snapshots: bool = False) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    snapshot_line = "snapshot-paths: ['snapshots']\n" if snapshots else ""
    (project / "dbt_project.yml").write_text(
        f"name: {name}\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        f"profile: {name}\n"
        "model-paths: ['models']\n"
        f"{snapshot_line}"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(name, f"{name}.duckdb"), encoding="utf-8"
    )


def _write_sources(project: Path, tables: list[str]) -> None:
    table_lines = "".join(f"      - name: {table}\n" for table in tables)
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        f"{table_lines}",
        encoding="utf-8",
    )


def _write_pattern(project: Path, pattern: str, generator: str, required: list[str]) -> None:
    (project / "metadata" / "patterns" / f"{pattern}.yml").write_text(
        f"name: {pattern}\n"
        f"generator: {generator}\n"
        "defaults: {owner: test}\n"
        f"required_params: [{', '.join(required)}]\n",
        encoding="utf-8",
    )


def _write_instances(project: Path, pattern: str, body: str) -> None:
    (project / "metadata" / "instances" / f"{pattern}.yml").write_text(
        f"pattern: {pattern}\n"
        "instances:\n"
        f"{body}",
        encoding="utf-8",
    )


def _write_seed(project: Path, table: str, header: tuple, rows: list[tuple]) -> None:
    with (project / "seeds" / f"{table}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as seed_file:
        writer = csv.writer(seed_file)
        writer.writerow(header)
        writer.writerows(rows)


def _read_rows(
    duckdb: ModuleType, project: Path, db_name: str, sql: str
) -> list[tuple]:
    connection = duckdb.connect(str(project / f"{db_name}.duckdb"), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _generate(project: Path) -> None:
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(
        ["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]
    ) == 0


# ------------------------------------------------------------------
# Silver: filter before dedup ranking, incremental lifecycle
# ------------------------------------------------------------------

_SILVER_HEADER = ("order_id", "status", "record_updated_at", "__loaded_at")
_SILVER_SELECT = (
    "select order_id, status, cast(record_updated_at as varchar) "
    "from main.silver_orders order by order_id"
)


@pytest.mark.integration
def test_silver_filter_dedup_incremental_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "silver_filter"
    _write_project_shell(project, "silver_filter")
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "bronze_orders.yml").write_text(
        "name: bronze_orders\n"
        "classification: bronze\n"
        "description: Hand-written order extracts.\n"
        "owner: test\n",
        encoding="utf-8",
    )
    (project / "models" / "bronze_orders.sql").write_text(
        "select order_id, status, record_updated_at, __loaded_at\n"
        "from {{ ref('orders_extract') }}\n",
        encoding="utf-8",
    )
    _write_pattern(
        project, "silver", "silver", ["description", "input_models", "dedup_key"]
    )
    _write_instances(
        project,
        "silver",
        "  - name: silver_orders\n"
        "    params:\n"
        "      description: Deduped live orders.\n"
        "      input_models: [bronze_orders]\n"
        "      dedup_key: order_id\n"
        "      dedup_order: record_updated_at DESC\n"
        "      columns: [order_id, status, record_updated_at]\n"
        "      filter: \"status <> 'void'\"\n"
        "      incremental: {strategy: merge, lookback: 4, lookback_unit: day}\n",
    )

    # Load 1: order 1's LATEST version (business 01-06) is void
    # and fails the filter; its earlier shipped version (01-05)
    # passes. Filter-before-ranking keeps 'shipped' as the
    # winner; ranking first would crown the void row and the
    # filter would then drop order 1 entirely.
    _write_seed(
        project, "orders_extract", _SILVER_HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (1, "void", "2026-01-06", "2026-01-05"),
            (2, "placed", "2026-01-01", "2026-01-01"),
        ],
    )
    _generate(project)
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_filter", _SILVER_SELECT)
    assert rows == [(1, "shipped", "2026-01-05"), (2, "placed", "2026-01-01")]

    # Load 2 (incremental): order 2 ships (newer business time,
    # in-window load stamp) and order 3 lands. Order 1's rows are
    # untouched; its winner must survive the recomputation.
    _write_seed(
        project, "orders_extract", _SILVER_HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (1, "void", "2026-01-06", "2026-01-05"),
            (2, "placed", "2026-01-01", "2026-01-01"),
            (2, "shipped", "2026-01-07", "2026-01-10"),
            (3, "placed", "2026-01-06", "2026-01-10"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_filter", _SILVER_SELECT)
    assert rows == [
        (1, "shipped", "2026-01-05"),
        (2, "shipped", "2026-01-07"),
        (3, "placed", "2026-01-06"),
    ]

    # Full-refresh equality: the deliberate rebuild over the same
    # input lands on exactly the incremental accumulation.
    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(duckdb, project, "silver_filter", _SILVER_SELECT)
    assert refreshed == rows


# ------------------------------------------------------------------
# Dimension: filter + incremental merge/cursor lifecycle
# ------------------------------------------------------------------

_DIM_HEADER = ("customer_id", "customer_name", "region", "updated_at")
_DIM_SELECT = (
    "select customer_id, customer_sk, customer_name, region "
    "from main.dim_customer order by customer_id"
)


@pytest.mark.integration
def test_dimension_filter_incremental_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "dim_filter"
    _write_project_shell(project, "dim_filter")
    _write_sources(project, ["customers"])
    _write_pattern(
        project, "staging", "staging", ["description", "source_ref", "columns"]
    )
    _write_instances(
        project,
        "staging",
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customer staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: customer_id, alias: customer_id}\n"
        "        - {source: customer_name, alias: customer_name}\n"
        "        - {source: region, alias: region}\n"
        "        - {source: updated_at, alias: updated_at}\n",
    )
    _write_pattern(
        project,
        "dimension",
        "dimension",
        ["description", "input_models", "natural_key", "surrogate_key", "attribute_columns"],
    )
    _write_instances(
        project,
        "dimension",
        "  - name: dim_customer\n"
        "    params:\n"
        "      description: AU customer dimension.\n"
        "      input_models: [stg_customers]\n"
        "      natural_key: customer_id\n"
        "      surrogate_key: customer_sk\n"
        "      attribute_columns: [customer_name, region, updated_at]\n"
        "      filter: \"region = 'AU'\"\n"
        "      incremental: {strategy: merge, cursor_column: updated_at}\n",
    )

    # Load 1: Bob (NZ) fails the filter and never lands.
    _write_seed(
        project, "customers", _DIM_HEADER,
        [
            (1, "Alice", "AU", "2026-01-01"),
            (2, "Bob", "NZ", "2026-01-01"),
            (3, "Carol", "AU", "2026-01-01"),
        ],
    )
    _generate(project)
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "dim_filter", _DIM_SELECT)
    assert rows == [(1, 1, "Alice", "AU"), (3, 2, "Carol", "AU")]

    # Load 2 (incremental, cursor on updated_at): Carol is
    # renamed in-window and Dave (AU) arrives; Bob is renamed
    # too but still fails the filter, so his rename is invisible.
    _write_seed(
        project, "customers", _DIM_HEADER,
        [
            (1, "Alice", "AU", "2026-01-01"),
            (2, "Bobby", "NZ", "2026-01-02"),
            (3, "Caroline", "AU", "2026-01-02"),
            (4, "Dave", "AU", "2026-01-02"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "dim_filter", _DIM_SELECT)
    by_key = {row[0]: row for row in rows}
    assert set(by_key) == {1, 3, 4}  # Bob never lands
    # Existing members keep their surrogates (merge excludes the
    # surrogate column); the rename landed on Carol's row.
    assert by_key[1][1] == 1 and by_key[3][1] == 2
    assert by_key[3][2] == "Caroline"
    assert by_key[4][2] == "Dave"

    # Full refresh: contents equal the incremental accumulation
    # (surrogates renumber positionally - the documented identity
    # reset a full refresh means for dimension).
    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(duckdb, project, "dim_filter", _DIM_SELECT)
    assert {row[0]: (row[2], row[3]) for row in refreshed} == {
        row[0]: (row[2], row[3]) for row in rows
    }
    assert refreshed == [
        (1, 1, "Alice", "AU"),
        (3, 2, "Caroline", "AU"),
        (4, 3, "Dave", "AU"),
    ]


# ------------------------------------------------------------------
# Staging: the predicate restricts the source scan
# ------------------------------------------------------------------


@pytest.mark.integration
def test_staging_filter_restricts_the_source_scan(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "stg_filter"
    _write_project_shell(project, "stg_filter")
    _write_sources(project, ["customers"])
    _write_pattern(
        project, "staging", "staging", ["description", "source_ref", "columns"]
    )
    _write_instances(
        project,
        "staging",
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Active customers only.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n"
        "      filter: \"is_active = 1\"\n",
    )
    _write_seed(
        project, "customers", ("id", "name", "is_active"),
        [(7, "Ada", 1), (8, "Grace", 0), (9, "Linus", 1)],
    )
    _generate(project)
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "stg_filter",
        "select customer_id, customer_name from main.stg_customers "
        "order by customer_id",
    )
    assert rows == [(7, "Ada"), (9, "Linus")]


# ------------------------------------------------------------------
# SCD2: filtered-out rows never enter history
# ------------------------------------------------------------------


@pytest.mark.integration
def test_scd2_filter_keeps_rows_out_of_history(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "scd2_filter"
    _write_project_shell(project, "scd2_filter", snapshots=True)
    _write_sources(project, ["customers"])
    _write_pattern(
        project, "staging", "staging", ["description", "source_ref", "columns"]
    )
    _write_instances(
        project,
        "staging",
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customer staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n"
        "        - {source: is_active, alias: is_active}\n",
    )
    _write_pattern(
        project,
        "scd2",
        "scd2_dimension",
        ["description", "input_models", "business_key", "tracked_columns"],
    )
    _write_instances(
        project,
        "scd2",
        "  - name: dim_customers_scd2\n"
        "    params:\n"
        "      description: Active customer history.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "      tracked_columns: [customer_name]\n"
        "      filter: \"is_active = 1\"\n",
    )
    (project / "seeds" / "customers.csv").write_text(
        "id,name,is_active\n7,Ada,1\n8,Grace,0\n", encoding="utf-8"
    )
    _generate(project)
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd2")

    db_file = "scd2_filter"

    def dimension_rows() -> list[tuple]:
        return _read_rows(
            duckdb, project, db_file,
            "select customer_id, customer_name, __valid_to is null "
            "from main.dim_customers_scd2 "
            "order by customer_id, __valid_from",
        )

    # Grace fails the filter: she is not in the dimension at
    # all - no current row, no history row.
    assert dimension_rows() == [(7, "Ada", True)]

    # Grace becomes active: from this load she enters history,
    # as exactly one current row - nothing is backfilled for the
    # loads she was filtered out of. Ada renames in the same
    # step and her history grows normally.
    db_path = str(project / f"{db_file}.duckdb")
    with duckdb.connect(db_path) as connection:
        connection.execute("update main.customers set is_active = 1 where id = 8")
        connection.execute("update main.customers set name = 'Adelaide' where id = 7")
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd2")
    assert dimension_rows() == [
        (7, "Ada", False),
        (7, "Adelaide", True),
        (8, "Grace", True),
    ]
