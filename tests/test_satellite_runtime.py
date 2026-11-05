"""Satellite lifecycle runtime proof on DuckDB (ADR 0028).

One project, three satellites over hub_customer (standard,
non-historised, record-tracking), driven through three extracts:

1. customers 1 (Alice) and 2 (Bob) appear;
2. the identical extract is loaded again (no-change reload);
3. customer 1's email changes, customer 2 disappears, customer 3
   (Carol) appears.

The standard satellite must gain exactly the changed/new rows, the
current view must show the latest payload per key, the
non-historised satellite must stay idempotent for unchanged
payloads while accepting a changed payload as a new row, and the
record-tracking satellite must record customer 2's deletion.
"""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom.cli import main
from dbt_harness import duckdb_profiles_yml, run_dbt

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

@pytest.mark.integration
def test_satellite_lifecycle_on_duckdb(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "satellite_runtime"
    _write_duckdb_project(project)

    alice_v1 = (1, "Alice", "alice@example.com")
    alice_v2 = (1, "Alice", "alice@new.example.com")
    bob = (2, "Bob", "bob@example.com")
    carol = (3, "Carol", "carol@example.com")

    def load(rows: list[tuple[int, str, str]]) -> None:
        _write_seed(project, "customers", ["id", "customer_name", "customer_email"], rows)
        run_dbt(dbt, project, "seed", "--full-refresh")
        run_dbt(dbt, project, "build")

    def query(sql: str) -> list[tuple]:
        connection = duckdb.connect(
            str(project / "satellite_runtime.duckdb"), read_only=True
        )
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    hk = {cid: _sha256(str(cid)) for cid in (1, 2, 3)}

    # -- Load 1: two customers appear.
    load([alice_v1, bob])
    standard = query(
        "select customer_hk, customer_name, customer_email, __hash_diff "
        "from main.sat_customer_details"
    )
    assert {(r[0], r[1], r[2]) for r in standard} == {
        (hk[1], "Alice", "alice@example.com"),
        (hk[2], "Bob", "bob@example.com"),
    }
    # Hash diff = SHA-256 of the canonical payload string (ADR 0028).
    diffs = {r[0]: r[3] for r in standard}
    assert diffs[hk[1]] == _sha256("Alice|alice@example.com")
    assert diffs[hk[2]] == _sha256("Bob|bob@example.com")
    tracking = query(
        "select customer_hk, __is_deleted from main.sat_customer_tracking"
    )
    assert {(r[0], r[1]) for r in tracking} == {(hk[1], False), (hk[2], False)}
    events = query("select customer_hk from main.sat_customer_events")
    assert {r[0] for r in events} == {hk[1], hk[2]}

    # -- Load 2: identical extract - nothing may change anywhere.
    load([alice_v1, bob])
    assert len(query("select * from main.sat_customer_details")) == 2
    assert len(query("select * from main.sat_customer_tracking")) == 2
    assert len(query("select * from main.sat_customer_events")) == 2

    # -- Load 3: Alice changes email, Bob disappears, Carol appears.
    load([alice_v2, carol])
    standard = query(
        "select customer_hk, customer_email from main.sat_customer_details"
    )
    assert len(standard) == 4  # Alice v1 + v2, Bob, Carol
    current = query(
        "select customer_hk, customer_email "
        "from main.sat_customer_details_current"
    )
    assert {(r[0], r[1]) for r in current} == {
        (hk[1], "alice@new.example.com"),
        (hk[2], "bob@example.com"),
        (hk[3], "carol@example.com"),
    }
    # Record tracking: Carol's appearance and Bob's deletion are new
    # rows; Alice's payload change is not a tracking event.
    tracking = query(
        "select customer_hk, __is_deleted from main.sat_customer_tracking "
        "order by __loaded_at, customer_hk, __is_deleted"
    )
    assert len(tracking) == 4
    assert (hk[3], False) in {(r[0], r[1]) for r in tracking}
    assert (hk[2], True) in {(r[0], r[1]) for r in tracking}
    # Non-historised: Alice's changed payload and Carol are new rows;
    # Bob's original row is retained (the variant never deletes).
    events = query(
        "select customer_hk, customer_email from main.sat_customer_events"
    )
    assert {(r[0], r[1]) for r in events} == {
        (hk[1], "alice@example.com"),
        (hk[1], "alice@new.example.com"),
        (hk[2], "bob@example.com"),
        (hk[3], "carol@example.com"),
    }

def _write_duckdb_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: satellite_runtime_test\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: satellite_runtime_test\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('satellite_runtime_test', 'satellite_runtime.duckdb'),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "staging.yml").write_text(
        "name: staging\n"
        "generator: staging\n"
        "defaults: {owner: test}\n"
        "required_params: [description, source_ref, columns]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "hub.yml").write_text(
        "name: hub\n"
        "generator: hub\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, business_key]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "satellite.yml").write_text(
        "name: satellite\n"
        "generator: satellite\n"
        "defaults: {owner: test}\n"
        "required_params: [description, parent, input_models, record_source]\n",
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
        "        - {source: customer_name, alias: customer_name}\n"
        "        - {source: customer_email, alias: customer_email}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "hub.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_customer\n"
        "    params:\n"
        "      description: Customer hub.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "satellite.yml").write_text(
        "pattern: satellite\n"
        "instances:\n"
        "  - name: sat_customer_details\n"
        "    params:\n"
        "      description: Customer descriptive history.\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      input_models: [stg_customers]\n"
        "      attribute_columns: [customer_name, customer_email]\n"
        "      record_source: crm\n"
        "  - name: sat_customer_events\n"
        "    params:\n"
        "      description: Customer payload events, non-historised.\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      input_models: [stg_customers]\n"
        "      variant: non_historised\n"
        "      attribute_columns: [customer_name, customer_email]\n"
        "      record_source: crm\n"
        "  - name: sat_customer_tracking\n"
        "    params:\n"
        "      description: Customer appearance tracking.\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      input_models: [stg_customers]\n"
        "      variant: record_tracking\n"
        "      record_source: crm\n",
        encoding="utf-8",
    )

def _write_seed(
    project: Path, table: str, header: list[str], rows: list[tuple]
) -> None:
    with (project / "seeds" / f"{table}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as seed_file:
        writer = csv.writer(seed_file)
        writer.writerow(header)
        writer.writerows(rows)

