"""Runtime proof for gold's fixed LEFT JOIN (DuckDB, integration group).

Gold's authorable `joins` entries render LEFT JOIN, matching obt
and view: a driving-input row whose join key is unmatched in a
joined input survives the aggregation, its measures over the
joined input aggregating the null-extended row (a SUM is NULL, a
COUNT of the joined key is 0). Under the renderer's previous
plain (inner) JOIN the same row was dropped from the output
entirely; the unmatched customer below is the discriminating
case, and this test fails against the pre-change SQL.
"""
from __future__ import annotations

import csv
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


@pytest.mark.integration
def test_gold_left_join_keeps_unmatched_driving_rows(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = dbt_or_skip()
    project = tmp_path / "gold_left_join"
    _write_project_shell(project, "gold_left_join")

    # Two registered hand-written silver inputs over seeds, so the
    # fixture controls every input row exactly.
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True)
    for model, description in (
        ("silver_customers", "Hand-written customers."),
        ("silver_orders", "Hand-written orders."),
    ):
        (registrations / f"{model}.yml").write_text(
            f"name: {model}\n"
            "classification: silver\n"
            f"description: {description}\n"
            "owner: test\n",
            encoding="utf-8",
        )
    (project / "models" / "silver_customers.sql").write_text(
        "select cast(customer_id as integer) as customer_id, "
        "customer_name\n"
        "from {{ ref('customers_extract') }}\n",
        encoding="utf-8",
    )
    (project / "models" / "silver_orders.sql").write_text(
        "select cast(customer_id as integer) as customer_id, "
        "cast(amount as integer) as amount\n"
        "from {{ ref('orders_extract') }}\n",
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
        "  - name: gold_customer_totals\n"
        "    params:\n"
        "      description: Per-customer order totals.\n"
        "      input_models: [silver_customers, silver_orders]\n"
        "      joins:\n"
        "        - {model: silver_orders, on: [customer_id]}\n"
        "      aggregation:\n"
        "        group_by: [customer_id]\n"
        "        measures:\n"
        '          - {expression: "sum(silver_orders.amount)", alias: total_amount}\n'
        '          - {expression: "count(silver_orders.customer_id)", alias: order_count}\n',
        encoding="utf-8",
    )

    # Customer 3 has no orders: the unmatched driving key.
    _write_seed(
        project, "customers_extract", ("customer_id", "customer_name"),
        [(1, "Ada"), (2, "Ben"), (3, "Cid")],
    )
    _write_seed(
        project, "orders_extract", ("customer_id", "amount"),
        [(1, 100), (1, 200), (2, 50)],
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed", "--full-refresh")
    # Gold generates a not_null test on every measure alias. The
    # unmatched customer's total_amount is NULL by design here -
    # that null is the behaviour under test - so that one
    # generated test is excluded from the build and the null is
    # asserted against the table directly below. Every other
    # node, the generated order_count not_null test included,
    # builds and passes.
    run_dbt(
        dbt, project, "build",
        "--exclude", "not_null_gold_customer_totals_total_amount",
    )
    rows = _read_rows(
        duckdb, project, "gold_left_join",
        "select customer_id, total_amount, order_count "
        "from main.gold_customer_totals order by customer_id",
    )
    # The unmatched customer survives with null-aggregated
    # measures; an inner join would have dropped the row.
    assert rows == [
        (1, 300, 2),
        (2, 50, 1),
        (3, None, 0),
    ]
