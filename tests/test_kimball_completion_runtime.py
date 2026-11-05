"""Kimball completion runtime proof on DuckDB (ADR 0031): SCD Type 3 / Type 6
dimensions over a real dbt snapshot lifecycle, and the periodic /
accumulating fact flavours building with their grain contracts -
including an accumulating row whose milestone key is still null,
which the generated tests must let through.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom.cli import main
from dbt_harness import duckdb_profiles_yml, run_dbt

def _write_project(project: Path, name: str) -> None:
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
        "snapshot-paths: ['snapshots']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(name, f"{name}.duckdb"), encoding="utf-8"
    )
    (project / "metadata" / "patterns" / "staging.yml").write_text(
        "name: staging\n"
        "generator: staging\n"
        "defaults: {owner: test}\n"
        "required_params: [description, source_ref, columns]\n",
        encoding="utf-8",
    )

@pytest.mark.integration
def test_scd_type3_and_type6_track_previous_and_current_values(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "scd_variants"
    _write_project(project, "scd_variants")
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "scd2_pattern.yml").write_text(
        "name: scd2_pattern\n"
        "generator: scd2_dimension\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, business_key, tracked_columns]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customer staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n"
        "        - {source: email, alias: customer_email}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "dims.yml").write_text(
        "pattern: scd2_pattern\n"
        "instances:\n"
        "  - name: dim_customers_scd3\n"
        "    params:\n"
        "      description: Customer dimension, Type 3.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "      tracked_columns: [customer_name, customer_email]\n"
        "      variant: type_3\n"
        "      previous_value_columns: [customer_name]\n"
        "  - name: dim_customers_scd6\n"
        "    params:\n"
        "      description: Customer dimension, Type 6.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "      tracked_columns: [customer_name, customer_email]\n"
        "      variant: type_6\n"
        "      previous_value_columns: [customer_name]\n",
        encoding="utf-8",
    )
    (project / "seeds" / "customers.csv").write_text(
        "id,name,email\n7,Ada,ada@example.com\n9,Grace,grace@example.com\n",
        encoding="utf-8",
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd3", "+dim_customers_scd6")

    db_path = str(project / "scd_variants.duckdb")

    def rows(sql: str) -> list[tuple]:
        connection = duckdb.connect(db_path, read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    type3 = (
        "select customer_id, customer_name, previous_customer_name "
        "from main.dim_customers_scd3 order by customer_id"
    )
    assert rows(type3) == [(7, "Ada", None), (9, "Grace", None)]

    # Rename Ada in the source; the snapshot closes her old row and
    # opens a new one.
    connection = duckdb.connect(db_path)
    connection.execute("update main.customers set name = 'Ada Lovelace' where id = 7")
    connection.close()
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd3", "+dim_customers_scd6")

    # Type 3: current state carries the immediately previous value.
    assert rows(type3) == [(7, "Ada Lovelace", "Ada"), (9, "Grace", None)]

    # Type 6: both history rows carry the CURRENT name (Type 1
    # element); the open row also carries the previous name.
    type6 = (
        "select customer_name, current_customer_name, previous_customer_name, "
        "__valid_to is null from main.dim_customers_scd6 "
        "where customer_id = 7 order by __valid_from"
    )
    assert rows(type6) == [
        ("Ada", "Ada Lovelace", None, False),
        ("Ada Lovelace", "Ada Lovelace", "Ada", True),
    ]
    grace6 = (
        "select customer_name, current_customer_name, previous_customer_name "
        "from main.dim_customers_scd6 where customer_id = 9"
    )
    assert rows(grace6) == [("Grace", "Grace", None)]

@pytest.mark.integration
def test_fact_flavours_build_with_their_grain_contracts(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "fact_flavours"
    _write_project(project, "fact_flavours")
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: order_events\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "fact_pattern.yml").write_text(
        "name: fact_pattern\n"
        "generator: fact\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, dimension_keys, measure_columns]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_order_events\n"
        "    params:\n"
        "      description: Order events staging.\n"
        "      source_ref: {name: raw, table: order_events}\n"
        "      columns:\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: customer_id, alias: customer_key}\n"
        "        - {source: period, alias: period_key}\n"
        "        - {source: ordered_on, alias: order_date_key}\n"
        "        - {source: shipped_on, alias: shipped_date_key}\n"
        "        - {source: qty, alias: quantity}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "facts.yml").write_text(
        "pattern: fact_pattern\n"
        "instances:\n"
        "  - name: fact_customer_periods\n"
        "    params:\n"
        "      description: Customer activity per period.\n"
        "      input_models: [stg_order_events]\n"
        "      dimension_keys: [customer_key, period_key]\n"
        "      measure_columns: [quantity]\n"
        "      variant: periodic_snapshot\n"
        "      period_key: period_key\n"
        "  - name: fact_order_lifecycle\n"
        "    params:\n"
        "      description: One row per order, milestone keys fill in over time.\n"
        "      input_models: [stg_order_events]\n"
        "      dimension_keys: [order_id, customer_key, order_date_key, shipped_date_key]\n"
        "      measure_columns: [quantity]\n"
        "      variant: accumulating_snapshot\n"
        "      milestone_keys: [order_date_key, shipped_date_key]\n",
        encoding="utf-8",
    )
    (project / "seeds" / "order_events.csv").write_text(
        "order_id,customer_id,period,ordered_on,shipped_on,qty\n"
        "101,7,2026-01,20260102,20260105,3\n"
        "102,7,2026-01,20260120,,1\n"
        "103,9,2026-02,20260201,20260203,2\n",
        encoding="utf-8",
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    # The build runs the generated tests: it must pass even though
    # order 102 has no shipped milestone yet (null shipped_date_key).
    run_dbt(dbt, project, "build")

    db_path = str(project / "fact_flavours.duckdb")
    connection = duckdb.connect(db_path, read_only=True)
    try:
        lifecycle = connection.execute(
            "select order_id, shipped_date_key from main.fact_order_lifecycle "
            "order by order_id"
        ).fetchall()
        periods = connection.execute(
            "select customer_key, period_key, quantity "
            "from main.fact_customer_periods order by customer_key, quantity"
        ).fetchall()
    finally:
        connection.close()
    assert lifecycle == [(101, 20260105), (102, None), (103, 20260203)]
    assert periods == [(7, "2026-01", 1), (7, "2026-01", 3), (9, "2026-02", 2)]
