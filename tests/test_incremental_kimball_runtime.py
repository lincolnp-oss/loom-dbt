"""Runtime lifecycles for incremental fact and dimension (DuckDB, integration group).

Per design §8.3, in the hub test's shape: seed → build → mutate →
build → assert contents, plus a `--full-refresh` rebuild. Fact key
mode must converge on a corrected row; fact append mode must NOT
capture a row restated at the loaded cursor maximum (the §3.1
honesty claim, asserted as expected behaviour); the accumulating
snapshot's merge must fill a milestone in place; and the dimension
lifecycle is the wave's acceptance centrepiece - existing members'
surrogate keys are unchanged by an incremental merge (facts that
reference them still resolve), while a genuinely new member that
sorts before them is numbered from the target maximum, not
positionally.
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from loom.cli import main
from dbt_harness import dbt_or_skip, duckdb_profiles_yml, run_dbt

def _write_project_shell(project: Path, name: str) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        f"name: {name}\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        f"profile: {name}\n"
        "model-paths: ['models']\n"
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

def _write_pattern(project: Path, pattern: str, required: list[str]) -> None:
    (project / "metadata" / "patterns" / f"{pattern}.yml").write_text(
        f"name: {pattern}\n"
        f"generator: {pattern}\n"
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

def _staging_instance(name: str, table: str, columns: list[str]) -> str:
    column_lines = "".join(
        f"        - {{source: {column}, alias: {column}}}\n" for column in columns
    )
    return (
        f"  - name: {name}\n"
        "    params:\n"
        f"      description: {name} staging.\n"
        f"      source_ref: {{name: raw, table: {table}}}\n"
        "      columns:\n"
        f"{column_lines}"
    )

_FACT_REQUIRED = ["description", "input_models", "dimension_keys", "measure_columns"]
_STAGING_REQUIRED = ["description", "source_ref", "columns"]

# ------------------------------------------------------------------
# Fact: key mode (merge) - a corrected row converges
# ------------------------------------------------------------------

@pytest.mark.integration
def test_fact_merge_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "fact_merge"
    _write_project_shell(project, "fact_merge")
    _write_sources(project, ["order_lines"])
    _write_pattern(project, "staging", _STAGING_REQUIRED)
    _write_instances(
        project,
        "staging",
        _staging_instance(
            "stg_order_lines",
            "order_lines",
            ["order_id", "customer_id", "quantity", "amount"],
        ),
    )
    _write_pattern(project, "fact", _FACT_REQUIRED)
    _write_instances(
        project,
        "fact",
        "  - name: fct_order_lines\n"
        "    params:\n"
        "      description: Order line facts.\n"
        "      input_models: [stg_order_lines]\n"
        "      dimension_keys: [order_id, customer_id]\n"
        "      measure_columns: [quantity, amount]\n"
        "      incremental: {strategy: merge, unique_key: order_id}\n",
    )
    _write_seed(
        project, "order_lines",
        ("order_id", "customer_id", "quantity", "amount"),
        [(1, 1, 2, 20), (2, 1, 1, 10)],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "fact_merge",
        "select order_id, customer_id, quantity, amount "
        "from main.fct_order_lines order by order_id",
    )
    assert rows == [(1, 1, 2, 20), (2, 1, 1, 10)]

    # Mutate: order 1's quantity is corrected, order 3 appears.
    _write_seed(
        project, "order_lines",
        ("order_id", "customer_id", "quantity", "amount"),
        [(1, 1, 5, 20), (2, 1, 1, 10), (3, 2, 4, 40)],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "fact_merge",
        "select order_id, customer_id, quantity, amount "
        "from main.fct_order_lines order by order_id",
    )
    # The correction converged on the grain - no duplicate order 1.
    assert rows == [(1, 1, 5, 20), (2, 1, 1, 10), (3, 2, 4, 40)]

    # Full-refresh equality: the merge accumulation equals the
    # rebuilt contents on the same fixture.
    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(
        duckdb, project, "fact_merge",
        "select order_id, customer_id, quantity, amount "
        "from main.fct_order_lines order by order_id",
    )
    assert refreshed == rows

# ------------------------------------------------------------------
# Fact: append mode - the strict window's honesty claim
# ------------------------------------------------------------------

@pytest.mark.integration
def test_fact_append_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "fact_append"
    _write_project_shell(project, "fact_append")
    _write_sources(project, ["events"])
    _write_pattern(project, "staging", _STAGING_REQUIRED)
    _write_instances(
        project,
        "staging",
        _staging_instance(
            "stg_events", "events", ["event_id", "event_seq", "amount"]
        ),
    )
    _write_pattern(project, "fact", _FACT_REQUIRED)
    _write_instances(
        project,
        "fact",
        "  - name: fct_events\n"
        "    params:\n"
        "      description: Event facts.\n"
        "      input_models: [stg_events]\n"
        "      dimension_keys: [event_id, event_seq]\n"
        "      measure_columns: [amount]\n"
        "      incremental: {strategy: append, cursor_column: event_seq}\n",
    )
    _write_seed(
        project, "events", ("event_id", "event_seq", "amount"),
        [(1, 100, 50), (2, 200, 70)],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "fact_append",
        "select event_id, event_seq, amount from main.fct_events "
        "order by event_id",
    )
    assert rows == [(1, 100, 50), (2, 200, 70)]

    # Mutate: the seq-200 row is restated (70 -> 75) at the cursor
    # value already loaded, and one new row lands beyond it.
    _write_seed(
        project, "events", ("event_id", "event_seq", "amount"),
        [(1, 100, 50), (2, 200, 75), (3, 300, 90)],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "fact_append",
        "select event_id, event_seq, amount from main.fct_events "
        "order by event_id",
    )
    # Append-mode honesty, executable (design §3.1): the new row
    # lands; the boundary restatement does NOT - the strict window
    # reads only rows after the loaded maximum.
    assert rows == [(1, 100, 50), (2, 200, 70), (3, 300, 90)]

    # A full refresh is a rebuild: it equals the source exactly,
    # restatement included.
    run_dbt(dbt, project, "build", "--full-refresh")
    rows = _read_rows(
        duckdb, project, "fact_append",
        "select event_id, event_seq, amount from main.fct_events "
        "order by event_id",
    )
    assert rows == [(1, 100, 50), (2, 200, 75), (3, 300, 90)]

# ------------------------------------------------------------------
# Fact: accumulating snapshot - a milestone fills in place
# ------------------------------------------------------------------

@pytest.mark.integration
def test_fact_accumulating_merge_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "fact_accumulating"
    _write_project_shell(project, "fact_accumulating")
    _write_sources(project, ["shipments"])
    _write_pattern(project, "staging", _STAGING_REQUIRED)
    _write_instances(
        project,
        "staging",
        _staging_instance(
            "stg_shipments",
            "shipments",
            ["order_id", "ordered_date", "shipped_date", "amount"],
        ),
    )
    _write_pattern(project, "fact", _FACT_REQUIRED)
    _write_instances(
        project,
        "fact",
        "  - name: fct_shipments\n"
        "    params:\n"
        "      description: Shipment accumulating snapshot.\n"
        "      input_models: [stg_shipments]\n"
        "      variant: accumulating_snapshot\n"
        "      dimension_keys: [order_id, ordered_date, shipped_date]\n"
        "      milestone_keys: [shipped_date]\n"
        "      measure_columns: [amount]\n"
        "      incremental: {strategy: merge, unique_key: order_id}\n",
    )
    select = (
        "select order_id, ordered_date::varchar, shipped_date::varchar, "
        "amount from main.fct_shipments order by order_id"
    )
    _write_seed(
        project, "shipments",
        ("order_id", "ordered_date", "shipped_date", "amount"),
        [(1, "2026-01-01", None, 100), (2, "2026-01-02", "2026-01-03", 200)],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "fact_accumulating", select)
    assert rows == [
        (1, "2026-01-01", None, 100),
        (2, "2026-01-02", "2026-01-03", 200),
    ]

    # Mutate: order 1 ships (its milestone lands), order 3 starts
    # its process unshipped.
    _write_seed(
        project, "shipments",
        ("order_id", "ordered_date", "shipped_date", "amount"),
        [
            (1, "2026-01-01", "2026-01-05", 100),
            (2, "2026-01-02", "2026-01-03", 200),
            (3, "2026-01-04", None, 300),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "fact_accumulating", select)
    # The milestone filled IN PLACE: still one row per grain, with
    # order 1's shipped_date updated by the merge - the
    # update-in-place load the variant exists for.
    assert rows == [
        (1, "2026-01-01", "2026-01-05", 100),
        (2, "2026-01-02", "2026-01-03", 200),
        (3, "2026-01-04", None, 300),
    ]

    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(duckdb, project, "fact_accumulating", select)
    assert refreshed == rows

# ------------------------------------------------------------------
# Dimension: merge - surrogate stability across an incremental load
# ------------------------------------------------------------------

def _surrogate_map(
    duckdb: ModuleType, project: Path, db_name: str
) -> dict[int, tuple[int, str]]:
    rows = _read_rows(
        duckdb, project, db_name,
        "select customer_id, customer_sk, customer_name "
        "from main.dim_customer",
    )
    return {row[0]: (row[1], row[2]) for row in rows}

def _resolved_references(
    duckdb: ModuleType, project: Path, db_name: str
) -> list[tuple]:
    """Fact-style references (order, surrogate) resolved via the dim."""
    connection = duckdb.connect(str(project / f"{db_name}.duckdb"), read_only=True)
    try:
        return connection.execute(
            "select r.order_id, d.customer_id from "
            "(values (101, 1), (102, 2)) as r(order_id, customer_sk) "
            "join main.dim_customer d on d.customer_sk = r.customer_sk "
            "order by r.order_id"
        ).fetchall()
    finally:
        connection.close()

@pytest.mark.integration
def test_dimension_merge_surrogate_stability(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "dim_merge"
    _write_project_shell(project, "dim_merge")
    _write_sources(project, ["customers"])
    _write_pattern(project, "staging", _STAGING_REQUIRED)
    _write_instances(
        project,
        "staging",
        _staging_instance(
            "stg_customers", "customers", ["customer_id", "customer_name"]
        ),
    )
    _write_pattern(
        project,
        "dimension",
        ["description", "input_models", "natural_key", "surrogate_key", "attribute_columns"],
    )
    _write_instances(
        project,
        "dimension",
        "  - name: dim_customer\n"
        "    params:\n"
        "      description: Customer dimension.\n"
        "      input_models: [stg_customers]\n"
        "      natural_key: customer_id\n"
        "      surrogate_key: customer_sk\n"
        "      attribute_columns: [customer_name]\n"
        "      incremental: {strategy: merge}\n",
    )
    _write_seed(
        project, "customers", ("customer_id", "customer_name"),
        [(1, "Alice"), (2, "Bob")],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    before = _surrogate_map(duckdb, project, "dim_merge")
    assert before == {1: (1, "Alice"), 2: (2, "Bob")}
    assert _resolved_references(duckdb, project, "dim_merge") == [
        (101, 1),
        (102, 2),
    ]

    # Mutate: customer 2 is renamed, and customer 0 arrives - a
    # member that sorts BEFORE both existing members. A positional
    # rebuild would renumber everyone (0->1, 1->2, 2->3) and
    # silently re-point every fact reference.
    _write_seed(
        project, "customers", ("customer_id", "customer_name"),
        [(0, "Zed"), (1, "Alice"), (2, "Robert")],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    after = _surrogate_map(duckdb, project, "dim_merge")
    # Surrogate stability: existing members keep their keys, the
    # attribute update landed, and the new member is numbered from
    # the target maximum (2) + its batch position - 3, not the 1 a
    # rebuild would assign it.
    assert after == {0: (3, "Zed"), 1: (1, "Alice"), 2: (2, "Robert")}
    # Fact-style references to the pre-existing surrogates still
    # resolve to the same members.
    assert _resolved_references(duckdb, project, "dim_merge") == [
        (101, 1),
        (102, 2),
    ]

    # A full refresh is a deliberate rebuild: contents (natural key
    # + attributes) equal the incremental state, while surrogates
    # renumber positionally - the identity reset --full-refresh
    # means for every incremental model.
    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _surrogate_map(duckdb, project, "dim_merge")
    assert {key: name for key, (_, name) in refreshed.items()} == {
        key: name for key, (_, name) in after.items()
    }
    assert refreshed == {0: (1, "Zed"), 1: (2, "Alice"), 2: (3, "Robert")}
