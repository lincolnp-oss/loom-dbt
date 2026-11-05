"""Kimball expression runtime proof (ADR 0037 extension, PR E3).

A generated dimension carrying an {expression, alias} attribute
and a generated fact carrying an {expression, alias} measure
compile and build under a real `dbt build` on DuckDB, and the
built values are the items' semantics. Every dbt subprocess
carries the standing telemetry opt-outs (via the harness env).
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
        "name: expression_kimball_proof\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: expression_kimball_proof\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(
            "expression_kimball_proof",
            str(project / "expression_kimball_proof.duckdb"),
            target="dev",
        ),
        encoding="utf-8",
    )
    (project / "seeds" / "customers.csv").write_text(
        "id,customer_name,customer_email\n"
        "1,Ada,ada@example.com\n"
        "2,Grace,grace@example.com\n",
        encoding="utf-8",
    )
    (project / "seeds" / "order_items.csv").write_text(
        "id,customer_id,quantity,unit_price,gross_amount\n"
        "100,1,3,250,750\n"
        "101,2,2,999,1998\n",
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: order_items\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customers staging",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "customers"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "customer_name", "alias": "customer_name"},
                {"source": "customer_email", "alias": "customer_email"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "dim_customer",
        "dimension",
        {
            "description": "Customer dimension with an expression attribute",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": [
                {"source": "customer_name", "alias": "name"},
                {"expression": "upper(customer_email)", "alias": "email_upper"},
            ],
        },
        pattern="dimension",
    )
    write_pattern_instance(
        project,
        "stg_order_items",
        "staging",
        {
            "description": "Order items staging",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "order_items"},
            "columns": [
                {"source": "id", "alias": "order_item_id"},
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "quantity", "alias": "quantity"},
                {"source": "unit_price", "alias": "unit_price"},
                {"source": "gross_amount", "alias": "gross_amount"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "fct_order_items",
        "fact",
        {
            "description": "Order item facts with an expression measure",
            "owner": "analytics",
            "input_models": ["stg_order_items"],
            "dimension_keys": ["customer_id"],
            "measure_columns": [
                "quantity",
                {"source": "gross_amount", "alias": "gross"},
                {"expression": "quantity * unit_price", "alias": "line_total"},
            ],
        },
        pattern="fact",
    )


def test_kimball_expression_models_build_under_dbt(tmp_path: Path) -> None:
    dbt = dbt_or_skip()
    project = tmp_path / "expression_kimball_proof"
    project.mkdir()
    _write_project(project)

    assert main(["generate", "--write", "--project-dir", str(project)]) == 0

    run_dbt(dbt, project, "seed")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(
        str(project / "expression_kimball_proof.duckdb"), read_only=True
    )
    try:
        dimension = connection.execute(
            "select customer_id, name, email_upper from dim_customer "
            "order by customer_id"
        ).fetchall()
        fact = connection.execute(
            "select customer_id, quantity, gross, line_total "
            "from fct_order_items order by customer_id"
        ).fetchall()
    finally:
        connection.close()

    assert dimension == [
        (1, "Ada", "ADA@EXAMPLE.COM"),
        (2, "Grace", "GRACE@EXAMPLE.COM"),
    ]
    assert fact == [
        (1, 3, 750, 750),
        (2, 2, 1998, 1998),
    ]
