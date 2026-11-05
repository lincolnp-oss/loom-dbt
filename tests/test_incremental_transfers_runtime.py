"""Runtime lifecycles for incremental link, bronze and normalized_table (DuckDB, integration group).

Per generator: seed → build → mutate → build → assert contents,
plus a `--full-refresh` rebuild whose result must equal the
incremental result (or, for bronze's append window, the source -
the full refresh is the moment restated rows land). The hub test
(`tests/test_incremental_hub.py`) is the shape precedent.
"""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from loom.cli import main
from dbt_harness import dbt_or_skip, duckdb_profiles_yml, run_dbt

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

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

# ------------------------------------------------------------------
# Bronze: append with a strict cursor window
# ------------------------------------------------------------------

@pytest.mark.integration
def test_bronze_append_cursor_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "bronze_append"
    _write_project_shell(project, "bronze_append")
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: events\n",
        encoding="utf-8",
    )
    _write_pattern(project, "bronze", ["description", "inline_source"])
    _write_instances(
        project,
        "bronze",
        "  - name: bronze_events\n"
        "    params:\n"
        "      description: Raw event landings.\n"
        "      inline_source: {name: raw, table: events}\n"
        "      incremental: {strategy: append, cursor_column: event_seq}\n",
    )
    _write_seed(
        project, "events", ("event_id", "event_seq", "payload"),
        [(1, 100, "a"), (2, 200, "b")],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "bronze_append",
        "select event_id, event_seq, payload from main.bronze_events "
        "order by event_id",
    )
    assert rows == [(1, 100, "a"), (2, 200, "b")]

    # Mutate: a restated row at the already-loaded cursor maximum
    # (200) and one genuinely new row beyond it.
    _write_seed(
        project, "events", ("event_id", "event_seq", "payload"),
        [(1, 100, "a"), (2, 200, "b-restated"), (3, 300, "c")],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "bronze_append",
        "select event_id, event_seq, payload from main.bronze_events "
        "order by event_id",
    )
    # Append-mode honesty, executable (design §3.2 mirrors §3.1):
    # the new row lands, the boundary restatement does NOT - the
    # strict window reads only rows after the loaded maximum.
    assert rows == [(1, 100, "a"), (2, 200, "b"), (3, 300, "c")]

    # A full refresh is a rebuild: it equals the source exactly,
    # restatement included.
    run_dbt(dbt, project, "build", "--full-refresh")
    rows = _read_rows(
        duckdb, project, "bronze_append",
        "select event_id, event_seq, payload from main.bronze_events "
        "order by event_id",
    )
    assert rows == [(1, 100, "a"), (2, 200, "b-restated"), (3, 300, "c")]

# ------------------------------------------------------------------
# Link: append guarded on the link hash
# ------------------------------------------------------------------

@pytest.mark.integration
def test_link_append_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "link_append"
    _write_project_shell(project, "link_append")
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: orders\n",
        encoding="utf-8",
    )
    _write_pattern(
        project, "staging", ["description", "source_ref", "columns"]
    )
    _write_instances(
        project,
        "staging",
        "  - name: stg_orders\n"
        "    params:\n"
        "      description: Order staging.\n"
        "      source_ref: {name: raw, table: orders}\n"
        "      columns:\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: customer_id, alias: customer_id}\n",
    )
    _write_pattern(project, "hub", ["description", "input_models", "business_key"])
    _write_instances(
        project,
        "hub",
        "  - name: hub_order\n"
        "    params:\n"
        "      description: Order hub.\n"
        "      input_models: [stg_orders]\n"
        "      business_key: order_id\n"
        "  - name: hub_customer\n"
        "    params:\n"
        "      description: Customer hub.\n"
        "      input_models: [stg_orders]\n"
        "      business_key: customer_id\n",
    )
    _write_pattern(
        project,
        "link",
        [
            "description", "input_models", "endpoints",
        ],
    )
    _write_instances(
        project,
        "link",
        "  - name: link_order_customer\n"
        "    params:\n"
        "      description: Order/customer link.\n"
        "      input_models: [stg_orders]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_customer\n"
        "          key: customer_id\n"
        "      incremental: {strategy: append}\n",
    )
    _write_seed(
        project, "orders", ("order_id", "customer_id"), [(1, 1), (2, 1)]
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    pairs = _read_rows(
        duckdb, project, "link_append",
        "select order_hk, customer_hk from main.link_order_customer",
    )
    assert sorted(pairs) == sorted(
        [(_sha("1"), _sha("1")), (_sha("2"), _sha("1"))]
    )

    # Mutate: a new relationship (3, 2); the existing pairs are
    # re-observed in the extract and must not duplicate.
    _write_seed(
        project, "orders", ("order_id", "customer_id"),
        [(1, 1), (2, 1), (3, 2)],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "link_append",
        "select order_customer_hk, order_hk, customer_hk "
        "from main.link_order_customer",
    )
    assert len(rows) == 3
    assert len({row[0] for row in rows}) == 3
    assert sorted((row[1], row[2]) for row in rows) == sorted(
        [(_sha("1"), _sha("1")), (_sha("2"), _sha("1")), (_sha("3"), _sha("2"))]
    )
    # The link hash is the hash of the concatenated component hashes.
    for link_hash, order_hk, customer_hk in rows:
        assert link_hash == _sha(order_hk + customer_hk)

    # Full-refresh equality: the rebuilt link holds exactly the
    # pairs the incremental loads accumulated.
    before = sorted(rows)
    run_dbt(dbt, project, "build", "--full-refresh")
    after = _read_rows(
        duckdb, project, "link_append",
        "select order_customer_hk, order_hk, customer_hk "
        "from main.link_order_customer",
    )
    assert sorted(after) == before

# ------------------------------------------------------------------
# Normalized table: delete+insert with a cursor window
# ------------------------------------------------------------------

@pytest.mark.integration
def test_normalized_table_delete_insert_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "normalized_incremental"
    _write_project_shell(project, "normalized_incremental")
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    _write_pattern(
        project, "staging", ["description", "source_ref", "columns"]
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
        "        - {source: country, alias: country}\n"
        "        - {source: updated_seq, alias: updated_seq}\n",
    )
    _write_pattern(
        project,
        "normalized_table",
        ["description", "input_models", "entity_columns", "attribute_columns"],
    )
    _write_instances(
        project,
        "normalized_table",
        "  - name: n_customer\n"
        "    params:\n"
        "      description: Customer entity.\n"
        "      input_models: [stg_customers]\n"
        "      entity_columns: [customer_id]\n"
        "      attribute_columns: [country, updated_seq]\n"
        "      incremental: {strategy: delete+insert, cursor_column: updated_seq}\n",
    )
    _write_seed(
        project, "customers", ("customer_id", "country", "updated_seq"),
        [(1, "US", 10), (2, "CA", 10)],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "normalized_incremental",
        "select customer_id, country, updated_seq from main.n_customer "
        "order by customer_id",
    )
    assert rows == [(1, "US", 10), (2, "CA", 10)]

    # Mutate: customer 1's attribute is corrected (higher cursor),
    # customer 3 appears; customer 2 is untouched.
    _write_seed(
        project, "customers", ("customer_id", "country", "updated_seq"),
        [(1, "MX", 20), (2, "CA", 10), (3, "FR", 20)],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(
        duckdb, project, "normalized_incremental",
        "select customer_id, country, updated_seq from main.n_customer "
        "order by customer_id",
    )
    assert rows == [(1, "MX", 20), (2, "CA", 10), (3, "FR", 20)]

    # Full-refresh equality: delete+insert over the cursor window
    # converges to the rebuilt contents on the same fixture.
    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(
        duckdb, project, "normalized_incremental",
        "select customer_id, country, updated_seq from main.n_customer "
        "order by customer_id",
    )
    assert refreshed == rows
