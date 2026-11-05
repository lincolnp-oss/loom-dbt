"""Runtime lifecycles for incremental silver (DuckDB, integration group).

The centrepiece is design §4.3's acceptance case: key-scoped
recomputation. The fixture separates the dedup ordering from the
cursor - the dedup orders by the business column
``record_updated_at``, the scan window runs on the load stamp
``__loaded_at`` - so a held winner's own row can sit OUTSIDE
the window while a late-arriving superseded copy of the same key
lands inside it. A window-local dedup would merge the superseded
copy and evict the winner; recomputing the key over the input's
full history keeps the winner. The fixture honours the §4.3
equivalence conditions: every input row is retained across runs
(E-S2), and the lookback (4 days) covers the late copy's arrival
lateness against the target maximum (E-S1).

Also proven: the winner's ``__loaded_at`` is carried from the
winning input row, never restamped (§4.2 - a restamp would read
as the build date, far from every fixture stamp); full-refresh
equality of the incremental accumulation; and delete-blindness
asserted as EXPECTED behaviour - a key deleted outright from the
input is never named by the window, so its stale winner survives
incremental runs until a ``--full-refresh`` (the documented
escape) removes it. The generated ``unique`` test on the dedup
key runs under ``dbt build`` throughout (§7.1).
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

_SILVER_REQUIRED = ["description", "input_models", "dedup_key"]
_HEADER = ("order_id", "status", "record_updated_at", "__loaded_at")
_SELECT = (
    "select order_id, status, cast(record_updated_at as varchar), "
    "cast(__loaded_at as varchar) from main.silver_orders "
    "order by order_id"
)

def _write_silver_project(project: Path, block: str) -> None:
    """A silver instance over a hand-written bronze input model.

    The input is a registered hand-written model over a seed, so
    the fixture controls every row's ``__loaded_at`` as data -
    neither staging (build-time stamp) nor generated bronze (its
    own load stamp) offers that control, and the §4 semantics turn
    on exact stamp values.
    """
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
    _write_pattern(project, "silver", _SILVER_REQUIRED)
    _write_instances(
        project,
        "silver",
        "  - name: silver_orders\n"
        "    params:\n"
        "      description: Deduped orders.\n"
        "      input_models: [bronze_orders]\n"
        "      dedup_key: order_id\n"
        "      dedup_order: record_updated_at DESC\n"
        "      columns: [order_id, status, record_updated_at]\n"
        f"      incremental: {block}\n",
    )

# ------------------------------------------------------------------
# Merge: the late-arriving older copy (the §4.3 centrepiece)
# ------------------------------------------------------------------

@pytest.mark.integration
def test_silver_merge_late_older_copy_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "silver_merge"
    _write_project_shell(project, "silver_merge")
    _write_silver_project(
        project, "{strategy: merge, lookback: 4, lookback_unit: day}"
    )

    # Load 1: order 1's current version (business time 01-05) and
    # order 2 land.
    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (2, "placed", "2026-01-01", "2026-01-01"),
        ],
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert rows == [
        (1, "shipped", "2026-01-05", "2026-01-05"),
        (2, "placed", "2026-01-01", "2026-01-01"),
    ]
    # The internal dedup column never reaches the target (§4.2).
    columns = _read_rows(
        duckdb, project, "silver_merge",
        "select column_name from information_schema.columns "
        "where table_name = 'silver_orders' order by ordinal_position",
    )
    assert [row[0] for row in columns] == [
        "order_id",
        "status",
        "record_updated_at",
        "__loaded_at",
    ]

    # Load 2: order 3's extract arrives late (business 01-06, load
    # stamp 01-10) and raises the target maximum to 01-10.
    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (2, "placed", "2026-01-01", "2026-01-01"),
            (3, "placed", "2026-01-06", "2026-01-10"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert rows == [
        (1, "shipped", "2026-01-05", "2026-01-05"),
        (2, "placed", "2026-01-01", "2026-01-01"),
        (3, "placed", "2026-01-06", "2026-01-10"),
    ]

    # Load 3 - THE CENTREPIECE: a superseded copy of order 1
    # (business time 01-03, older than the held winner's 01-05)
    # arrives late, stamped 01-06. The window (target max 01-10
    # widened by the 4-day lookback → bound 01-06) contains the
    # late copy but NOT the winner's own row (stamped 01-05): the
    # key is named by the window, and only recomputation over the
    # input's full history for that key keeps the held winner. A
    # window-local dedup would merge 'packed' here.
    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (1, "packed", "2026-01-03", "2026-01-06"),
            (2, "placed", "2026-01-01", "2026-01-01"),
            (3, "placed", "2026-01-06", "2026-01-10"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert rows == [
        (1, "shipped", "2026-01-05", "2026-01-05"),
        (2, "placed", "2026-01-01", "2026-01-01"),
        (3, "placed", "2026-01-06", "2026-01-10"),
    ]

    # Load 4: the converse - a late NEWER copy of order 1
    # (business time 01-07) replaces the winner, and the target
    # row carries the winning input row's own load stamp
    # (2026-01-11), not a build-time restamp (§4.2).
    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (1, "packed", "2026-01-03", "2026-01-06"),
            (1, "delivered", "2026-01-07", "2026-01-11"),
            (2, "placed", "2026-01-01", "2026-01-01"),
            (3, "placed", "2026-01-06", "2026-01-10"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert rows == [
        (1, "delivered", "2026-01-07", "2026-01-11"),
        (2, "placed", "2026-01-01", "2026-01-01"),
        (3, "placed", "2026-01-06", "2026-01-10"),
    ]

    # Full-refresh equality: the incremental accumulation equals
    # the rebuilt contents on the same fixture history (no
    # deletes have occurred yet, so E-S1/E-S2 hold).
    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert refreshed == rows

    # Delete-blindness, asserted as EXPECTED behaviour (§4.3):
    # order 2's rows are deleted from the input outright. No
    # input row names the key, so no incremental run can observe
    # the deletion - the stale winner persists...
    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "shipped", "2026-01-05", "2026-01-05"),
            (1, "packed", "2026-01-03", "2026-01-06"),
            (1, "delivered", "2026-01-07", "2026-01-11"),
            (3, "placed", "2026-01-06", "2026-01-10"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert rows == [
        (1, "delivered", "2026-01-07", "2026-01-11"),
        (2, "placed", "2026-01-01", "2026-01-01"),  # stale, by design
        (3, "placed", "2026-01-06", "2026-01-10"),
    ]

    #...until the documented escape: a full refresh reconciles.
    run_dbt(dbt, project, "build", "--full-refresh")
    rows = _read_rows(duckdb, project, "silver_merge", _SELECT)
    assert rows == [
        (1, "delivered", "2026-01-07", "2026-01-11"),
        (3, "placed", "2026-01-06", "2026-01-10"),
    ]

# ------------------------------------------------------------------
# delete+insert: the same recomputation under the other strategy
# ------------------------------------------------------------------

@pytest.mark.integration
def test_silver_delete_insert_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "silver_delete_insert"
    _write_project_shell(project, "silver_delete_insert")
    _write_silver_project(project, "{strategy: delete+insert}")

    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "placed", "2026-01-01", "2026-01-01"),
            (2, "placed", "2026-01-01", "2026-01-01"),
        ],
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_delete_insert", _SELECT)
    assert rows == [
        (1, "placed", "2026-01-01", "2026-01-01"),
        (2, "placed", "2026-01-01", "2026-01-01"),
    ]

    # A newer version of order 1 lands; delete+insert replaces
    # exactly that key's row, carrying the winner's own stamp.
    _write_seed(
        project, "orders_extract", _HEADER,
        [
            (1, "placed", "2026-01-01", "2026-01-01"),
            (1, "shipped", "2026-01-02", "2026-01-02"),
            (2, "placed", "2026-01-01", "2026-01-01"),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "silver_delete_insert", _SELECT)
    assert rows == [
        (1, "shipped", "2026-01-02", "2026-01-02"),
        (2, "placed", "2026-01-01", "2026-01-01"),
    ]

    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(duckdb, project, "silver_delete_insert", _SELECT)
    assert refreshed == rows
