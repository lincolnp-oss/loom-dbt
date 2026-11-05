"""Expression-item runtime proof (ADR 0037).

Generated models carrying expression items on each mapping-form
surface - a staging column (the pi session's `amount / 100`), a
gold and a mart expression group_by dimension, and a view
select_column - compile and build under a real `dbt build` on
DuckDB, and the built values are the expressions' semantics.
Every dbt subprocess carries the standing telemetry opt-outs
(via the harness env).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dbt_harness import dbt_or_skip, duckdb_profiles_yml, run_dbt

from loom.cli import main
from metadata_helpers import write_pattern_instance

pytestmark = pytest.mark.integration


def _write_project(project: Path) -> None:
    (project / "seeds").mkdir(parents=True)
    (project / "models").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: expression_proof\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: expression_proof\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(
            "expression_proof",
            str(project / "expression_proof.duckdb"),
            target="dev",
        ),
        encoding="utf-8",
    )
    (project / "seeds" / "payments.csv").write_text(
        "id,customer_id,amount,ordered_at\n"
        "1,10,12345,2026-01-15 10:00:00\n"
        "2,10,999,2026-01-20 11:00:00\n"
        "3,11,5000,2026-02-02 09:30:00\n",
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: payments\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_payments",
        "staging",
        {
            "description": "Payments staging with an expression column",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "payments"},
            "columns": [
                {"source": "id", "alias": "payment_id"},
                {"source": "customer_id", "alias": "customer_id"},
                {"expression": "amount / 100", "alias": "amount"},
                {"source": "ordered_at", "alias": "ordered_at"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "gold_monthly_orders",
        "gold",
        {
            "description": "Monthly order totals by customer",
            "owner": "analytics",
            "input_models": ["stg_payments"],
            "aggregation": {
                "group_by": [
                    "customer_id",
                    {
                        "expression": "date_trunc('month', ordered_at)",
                        "alias": "order_month",
                    },
                ],
                "measures": [
                    {"expression": "COUNT(*)", "alias": "order_count"},
                    {"expression": "SUM(amount)", "alias": "total_amount"},
                ],
            },
        },
        pattern="gold",
    )
    write_pattern_instance(
        project,
        "mart_daily_orders",
        "mart",
        {
            "description": "Daily order counts",
            "owner": "analytics",
            "input_models": ["stg_payments"],
            "aggregation": {
                "group_by": [
                    {
                        "expression": "date_trunc('day', ordered_at)",
                        "alias": "order_day",
                    },
                ],
                "measures": [
                    {"expression": "COUNT(*)", "alias": "order_count"},
                ],
            },
        },
        pattern="mart",
    )
    write_pattern_instance(
        project,
        "vw_payment_doubles",
        "view",
        {
            "description": "Payments with doubled amounts",
            "owner": "analytics",
            "input_models": ["stg_payments"],
            "select_columns": [
                {"source": "stg_payments.payment_id", "alias": "payment_id"},
                {"expression": "amount * 2", "alias": "double_amount"},
            ],
        },
        pattern="view",
    )


def test_expression_models_build_under_dbt(tmp_path: Path) -> None:
    dbt = dbt_or_skip()
    project = tmp_path / "expression_proof"
    project.mkdir()
    _write_project(project)

    assert main(["generate", "--write", "--project-dir", str(project)]) == 0
    staging_sql = (
        project / "models" / "_generated" / "staging" / "stg_payments.sql"
    ).read_text(encoding="utf-8")
    assert "amount / 100 AS amount" in staging_sql

    run_dbt(dbt, project, "seed")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(
        str(project / "expression_proof.duckdb"), read_only=True
    )
    try:
        staging = connection.execute(
            "select payment_id, amount from stg_payments order by payment_id"
        ).fetchall()
        gold = connection.execute(
            "select customer_id, order_month::date, order_count, total_amount "
            "from gold_monthly_orders order by customer_id, order_month"
        ).fetchall()
        mart = connection.execute(
            "select order_day::date, order_count from mart_daily_orders "
            "order by order_day"
        ).fetchall()
        view = connection.execute(
            "select payment_id, double_amount from vw_payment_doubles "
            "order by payment_id"
        ).fetchall()
    finally:
        connection.close()

    assert staging == [(1, 123.45), (2, 9.99), (3, 50.0)]
    assert [(row[0], str(row[1]), row[2]) for row in gold] == [
        (10, "2026-01-01", 2),
        (11, "2026-02-01", 1),
    ]
    assert [row[3] for row in gold] == [133.44, 50.0]
    assert [(str(row[0]), row[1]) for row in mart] == [
        ("2026-01-15", 1),
        ("2026-01-20", 1),
        ("2026-02-02", 1),
    ]
    assert view == [(1, 246.90), (2, 19.98), (3, 100.0)]
