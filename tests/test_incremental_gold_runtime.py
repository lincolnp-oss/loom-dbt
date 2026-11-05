"""Runtime lifecycles for incremental gold (DuckDB, integration group).

The centrepiece is design §5.3's acceptance case: grain-period
recomputation. A grain restated INSIDE the lookback window is
recomputed over the driving input's full history, so its stored
value equals exactly what a full rebuild would compute - there is
no incremental-aggregate drift, for any measure. A grain restated
BEYOND the lookback never enters the affected set and stays
stale, asserted here as EXPECTED behaviour: §5.3's published
stance ("exact for grains touched within the lookback window and
stale - silently - for everything else; full-refresh is the
reconciliation", E-G2). A periodic ``--full-refresh`` reconciles,
asserted as whole-table equality with the rebuild.

The ``__loaded_at`` asymmetry (§5.2) is asserted by value: a
touched grain's stamp advances to its recompute run, an untouched
grain's stamp is bit-for-bit the value its first build wrote -
two rows in one table carrying stamps from different runs, by
design.
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

_HEADER = ("sale_date", "region", "amount")
_SELECT = (
    "select cast(sale_date as varchar), region, total_amount, lines, "
    "__loaded_at from main.gold_sales_daily order by sale_date, region"
)

def _write_gold_project(project: Path, block: str) -> None:
    """A gold instance over a hand-written silver input model.

    The input is a registered hand-written model over a seed, so
    the fixture controls every input row exactly - the §5
    semantics turn on which grains the window names, which the
    seed's sale_date values decide.
    """
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "silver_sales.yml").write_text(
        "name: silver_sales\n"
        "classification: silver\n"
        "description: Hand-written sales.\n"
        "owner: test\n",
        encoding="utf-8",
    )
    (project / "models" / "silver_sales.sql").write_text(
        "select cast(sale_date as date) as sale_date, region, "
        "cast(amount as integer) as amount\n"
        "from {{ ref('sales_extract') }}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "gold.yml").write_text(
        "name: gold\n"
        "generator: gold\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "gold.yml").write_text(
        "pattern: gold\n"
        "instances:\n"
        "  - name: gold_sales_daily\n"
        "    params:\n"
        "      description: Daily sales by region.\n"
        "      input_models: [silver_sales]\n"
        "      aggregation:\n"
        "        group_by: [sale_date, region]\n"
        "        measures:\n"
        '          - {expression: "sum(amount)", alias: total_amount}\n'
        '          - {expression: "count(*)", alias: lines}\n'
        f"      incremental: {block}\n",
        encoding="utf-8",
    )

def _values(rows: list[tuple]) -> list[tuple]:
    """The grain values of a read, stamps dropped."""
    return [row[:4] for row in rows]

def _stamps(rows: list[tuple]) -> dict[tuple, object]:
    return {(row[0], row[1]): row[4] for row in rows}

# ------------------------------------------------------------------
# delete+insert: within-lookback exactness, beyond-lookback
# staleness (expected), the stamp asymmetry
# ------------------------------------------------------------------

@pytest.mark.integration
def test_gold_delete_insert_grain_recomputation_lifecycle(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "gold_delete_insert"
    _write_project_shell(project, "gold_delete_insert")
    _write_gold_project(
        project,
        "{strategy: delete+insert, cursor_column: sale_date, "
        "lookback: 3, lookback_unit: day}",
    )

    # Load 1: grains on 01-10 (which later runs' windows will not
    # reach), 01-17 and 01-18. Target max cursor = 01-18.
    _write_seed(
        project, "sales_extract", _HEADER,
        [
            ("2026-01-10", "EU", 70),
            ("2026-01-17", "EU", 100),
            ("2026-01-17", "EU", 50),
            ("2026-01-18", "US", 200),
        ],
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "gold_delete_insert", _SELECT)
    assert _values(rows) == [
        ("2026-01-10", "EU", 70, 1),
        ("2026-01-17", "EU", 150, 2),
        ("2026-01-18", "US", 200, 1),
    ]
    stamps_1 = _stamps(rows)
    # The §5.2 output set, in order - no other input column, no
    # input stamp.
    columns = _read_rows(
        duckdb, project, "gold_delete_insert",
        "select column_name from information_schema.columns "
        "where table_name = 'gold_sales_daily' order by ordinal_position",
    )
    assert [row[0] for row in columns] == [
        "sale_date",
        "region",
        "total_amount",
        "lines",
        "__loaded_at",
    ]

    # Load 2: a restated row for grain (01-17, EU) - inside the
    # lookback (window bound = target max 01-18 − 3d = 01-15) -
    # and a new grain (01-19, US). The full history is retained:
    # the batch recomputes each touched grain over all its rows.
    _write_seed(
        project, "sales_extract", _HEADER,
        [
            ("2026-01-10", "EU", 70),
            ("2026-01-17", "EU", 100),
            ("2026-01-17", "EU", 50),
            ("2026-01-17", "EU", 25),
            ("2026-01-18", "US", 200),
            ("2026-01-19", "US", 300),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "gold_delete_insert", _SELECT)
    # The restated grain is EXACT - 175/3 is what a full rebuild
    # computes from the same input, not a delta over the stored
    # 150/2. The new grain lands.
    assert _values(rows) == [
        ("2026-01-10", "EU", 70, 1),
        ("2026-01-17", "EU", 175, 3),
        ("2026-01-18", "US", 200, 1),
        ("2026-01-19", "US", 300, 1),
    ]
    stamps_2 = _stamps(rows)
    # Stamp asymmetry (§5.2): the untouched grain (01-10, EU) -
    # no driving row inside the window - keeps its first-build
    # stamp bit-for-bit; the recomputed grains carry run 2's.
    assert stamps_2[("2026-01-10", "EU")] == stamps_1[("2026-01-10", "EU")]
    assert stamps_2[("2026-01-17", "EU")] > stamps_1[("2026-01-17", "EU")]
    assert stamps_2[("2026-01-18", "US")] > stamps_1[("2026-01-18", "US")]

    # Load 3: a restated row for grain (01-10, EU) arrives BEYOND
    # the lookback (window bound = target max 01-19 − 3d = 01-16;
    # the row's sale_date is 01-10). The grain never enters the
    # affected set: its stored row stays stale - EXPECTED
    # behaviour (E-G2), asserted as such, not as a defect.
    _write_seed(
        project, "sales_extract", _HEADER,
        [
            ("2026-01-10", "EU", 70),
            ("2026-01-10", "EU", 30),
            ("2026-01-17", "EU", 100),
            ("2026-01-17", "EU", 50),
            ("2026-01-17", "EU", 25),
            ("2026-01-18", "US", 200),
            ("2026-01-19", "US", 300),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "gold_delete_insert", _SELECT)
    assert _values(rows) == [
        ("2026-01-10", "EU", 70, 1),  # stale, by design (E-G2)
        ("2026-01-17", "EU", 175, 3),
        ("2026-01-18", "US", 200, 1),
        ("2026-01-19", "US", 300, 1),
    ]
    # Untouched again: value AND stamp both still run 1's.
    assert _stamps(rows)[("2026-01-10", "EU")] == stamps_1[("2026-01-10", "EU")]

    # The documented reconciliation: a full refresh recomputes
    # every grain from the current input - whole-table equality
    # with the rebuild, the beyond-lookback grain included.
    run_dbt(dbt, project, "build", "--full-refresh")
    rows = _read_rows(duckdb, project, "gold_delete_insert", _SELECT)
    assert _values(rows) == [
        ("2026-01-10", "EU", 100, 2),
        ("2026-01-17", "EU", 175, 3),
        ("2026-01-18", "US", 200, 1),
        ("2026-01-19", "US", 300, 1),
    ]

# ------------------------------------------------------------------
# merge: the same recomputation under the other strategy
# ------------------------------------------------------------------

@pytest.mark.integration
def test_gold_merge_grain_recomputation_lifecycle(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "gold_merge"
    _write_project_shell(project, "gold_merge")
    _write_gold_project(
        project,
        "{strategy: merge, cursor_column: sale_date, "
        "lookback: 3, lookback_unit: day}",
    )

    _write_seed(
        project, "sales_extract", _HEADER,
        [
            ("2026-02-01", "EU", 40),
            ("2026-02-02", "US", 90),
        ],
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "gold_merge", _SELECT)
    assert _values(rows) == [
        ("2026-02-01", "EU", 40, 1),
        ("2026-02-02", "US", 90, 1),
    ]

    # A restated row for (02-01, EU) inside the lookback: merge
    # replaces exactly that grain's row with the full-history
    # recomputation; the other grain is untouched.
    _write_seed(
        project, "sales_extract", _HEADER,
        [
            ("2026-02-01", "EU", 40),
            ("2026-02-01", "EU", 15),
            ("2026-02-02", "US", 90),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    rows = _read_rows(duckdb, project, "gold_merge", _SELECT)
    assert _values(rows) == [
        ("2026-02-01", "EU", 55, 2),
        ("2026-02-02", "US", 90, 1),
    ]

    run_dbt(dbt, project, "build", "--full-refresh")
    refreshed = _read_rows(duckdb, project, "gold_merge", _SELECT)
    assert _values(refreshed) == _values(rows)
