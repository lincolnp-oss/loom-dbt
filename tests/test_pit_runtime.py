"""PIT runtime proof on DuckDB (ADR 0035).

Three loads over a hub with a standard and a record-tracking
satellite. The PIT must show: at the first snapshot every present
key resolves to that load; a key appearing in load 2 carries the
GHOST timestamp at the load-1 snapshot; an unchanged key keeps
pointing at load 1 at later snapshots (the PIT's whole purpose);
and the tracking satellite's deletion row becomes Bob's effective
row at the load-3 snapshot.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from loom.cli import main
from dbt_harness import duckdb_profiles_yml, run_dbt

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def _write_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: pit_runtime\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: pit_runtime\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('pit_runtime', 'pit_runtime.duckdb'),
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
    patterns = {
        "staging.yml": ("staging", "staging", "[description, source_ref, columns]"),
        "hub.yml": ("hub", "hub", "[description, input_models, business_key]"),
        "sat.yml": (
            "sat",
            "satellite",
            "[description, parent, input_models, record_source]",
        ),
        "pit.yml": (
            "pit",
            "pit",
            "[description, parent, satellites]",
        ),
    }
    for filename, (name, generator, required) in patterns.items():
        (project / "metadata" / "patterns" / filename).write_text(
            f"name: {name}\n"
            f"generator: {generator}\n"
            "defaults: {owner: test}\n"
            f"required_params: {required}\n",
            encoding="utf-8",
        )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customers.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n"
        "        - {source: email, alias: customer_email}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "vault.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_customer\n"
        "    params:\n"
        "      description: Customer hub.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "      retention: persistent\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "satellites.yml").write_text(
        "pattern: sat\n"
        "instances:\n"
        "  - name: sat_customer_details\n"
        "    params:\n"
        "      description: Customer details.\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      input_models: [stg_customers]\n"
        "      record_source: crm\n"
        "      attribute_columns: [customer_name, customer_email]\n"
        "  - name: sat_customer_tracking\n"
        "    params:\n"
        "      description: Customer tracking.\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      input_models: [stg_customers]\n"
        "      record_source: crm\n"
        "      variant: record_tracking\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "pits.yml").write_text(
        "pattern: pit\n"
        "instances:\n"
        "  - name: pit_customer\n"
        "    params:\n"
        "      description: Customer point-in-time.\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      satellites: [sat_customer_details, sat_customer_tracking]\n",
        encoding="utf-8",
    )

@pytest.mark.integration
def test_pit_lifecycle_on_duckdb(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "pit_runtime"
    _write_project(project)

    def load(rows: list[tuple[int, str, str]]) -> None:
        (project / "seeds" / "customers.csv").write_text(
            "id,name,email\n" + "".join(f"{i},{n},{e}\n" for i, n, e in rows),
            encoding="utf-8",
        )
        run_dbt(dbt, project, "seed", "--full-refresh")
        run_dbt(dbt, project, "build")

    def query(sql: str) -> list[tuple]:
        connection = duckdb.connect(str(project / "pit_runtime.duckdb"), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    alice, bob, carol = (_sha256("1"), _sha256("2"), _sha256("3"))

    def pit_rows() -> list[tuple]:
        return query(
            "select customer_hk, __snapshot_date, __sat_customer_details_ldts, "
            "__sat_customer_tracking_ldts from main.pit_customer"
        )

    def satellite_ldts(model: str, hk: str) -> list:
        return sorted(
            row[0]
            for row in query(
                f"select __loaded_at from main.{model} "
                f"where customer_hk = '{hk}'"
            )
        )

    # -- Load 1: Alice + Bob. (Each satellite stamps its own load
    # instant, so the calendar holds one snapshot per satellite
    # load; assertions are stated against the satellites' own
    # timestamps rather than an assumed single batch instant.)
    load([(1, "Alice", "alice@example.com"), (2, "Bob", "bob@example.com")])
    rows = pit_rows()
    snapshots = sorted({row[1] for row in rows})
    assert len(rows) == 2 * len(snapshots)
    by_key = {(row[0], row[1]): row for row in rows}
    # At the latest snapshot, each key resolves to its load-1 rows.
    assert by_key[(alice, snapshots[-1])][2] == satellite_ldts(
        "sat_customer_details", alice
    )[-1]
    assert by_key[(bob, snapshots[-1])][3] == satellite_ldts(
        "sat_customer_tracking", bob
    )[-1]

    # -- Load 2: Alice changes email, Carol appears.
    load(
        [
            (1, "Alice", "alice@new.example.com"),
            (2, "Bob", "bob@example.com"),
            (3, "Carol", "carol@example.com"),
        ]
    )
    rows = pit_rows()
    snapshots = sorted({row[1] for row in rows})
    assert len(rows) == 3 * len(snapshots)
    by_key = {(row[0], row[1]): row for row in rows}
    alice_v1 = satellite_ldts("sat_customer_details", alice)[0]
    alice_v2 = satellite_ldts("sat_customer_details", alice)[-1]
    # Carol at the earliest snapshot: ghost - she had no row then.
    assert by_key[(carol, snapshots[0])][2].year == 1
    # Carol at the latest snapshot: her load-2 row.
    assert by_key[(carol, snapshots[-1])][2] == satellite_ldts(
        "sat_customer_details", carol
    )[-1]
    # Alice: v1 at the snapshot her first row defines, v2 at the end.
    assert by_key[(alice, alice_v1)][2] == alice_v1
    assert by_key[(alice, snapshots[-1])][2] == alice_v2
    # Bob unchanged: his pointer is frozen at his load-1 row.
    assert by_key[(bob, snapshots[-1])][2] == satellite_ldts(
        "sat_customer_details", bob
    )[-1]

    # -- Load 3: Bob disappears (tracking records the deletion).
    load([(1, "Alice", "alice@new.example.com"), (3, "Carol", "carol@example.com")])
    rows = pit_rows()
    snapshots = sorted({row[1] for row in rows})
    assert len(rows) == 3 * len(snapshots)
    by_key = {(row[0], row[1]): row for row in rows}
    bob_tracking = satellite_ldts("sat_customer_tracking", bob)
    assert len(bob_tracking) == 2  # appearance + deletion
    # Before the deletion snapshot Bob's tracking pointer is his
    # appearance; at the latest snapshot it is the deletion row.
    assert by_key[(bob, alice_v2)][3] == bob_tracking[0]
    assert by_key[(bob, snapshots[-1])][3] == bob_tracking[-1]

    # -- Regression: the ghost sentinel must survive a
    # positive-offset session. DuckDB's CURRENT_TIMESTAMP is
    # TIMESTAMPTZ; with TIMESTAMPTZ stamp columns the PIT's
    # COALESCE promotes the naive year-1 sentinel, which a
    # session east of UTC interprets as local time - an instant
    # in year 0 (BC) UTC, unrepresentable as a Python datetime,
    # so the client silently returns a str ('0001-12-31 (BC)
    # 13:55:08+00' under Australia/Sydney). With naive TIMESTAMP
    # stamps (the duckdb dialect's current_timestamp is
    # `localtimestamp`) the value is a datetime in any session
    # timezone. The first block reads the built PIT under a
    # Sydney session (this is what failed on the Sydney-hosted CI
    # fleet); the second evaluates the PIT's own ghost
    # expression against the real satellite table in that
    # session, which fails pre-fix in ANY host timezone.
    connection = duckdb.connect(
        str(project / "pit_runtime.duckdb"), read_only=True
    )
    try:
        connection.execute("SET TimeZone='Australia/Sydney'")
        sydney_rows = connection.execute(
            "select customer_hk, __snapshot_date, "
            "__sat_customer_details_ldts, __sat_customer_tracking_ldts "
            "from main.pit_customer"
        ).fetchall()
        ghost = connection.execute(
            "SELECT COALESCE("
            "  (SELECT MAX(__loaded_at) FROM main.sat_customer_details"
            "   WHERE customer_hk = 'no-such-key'),"
            "  TIMESTAMP '0001-01-01 00:00:00')"
        ).fetchone()[0]
    finally:
        connection.close()
    for row in sydney_rows:
        for cell in row[1:]:
            assert isinstance(cell, datetime), (
                f"PIT timestamp cell arrived as {type(cell).__name__} "
                f"({cell!r}) under Australia/Sydney"
            )
    sydney_by_key = {(row[0], row[1]): row for row in sydney_rows}
    earliest = min(row[1] for row in sydney_rows)
    assert sydney_by_key[(carol, earliest)][2].year == 1
    assert isinstance(ghost, datetime), (
        f"ghost expression arrived as {type(ghost).__name__} "
        f"({ghost!r}) under Australia/Sydney"
    )
    assert ghost.year == 1
