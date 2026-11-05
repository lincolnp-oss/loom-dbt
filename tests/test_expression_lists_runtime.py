"""Name-list expression runtime proof (ADR 0037, PR E2).

Generated silver and obt models whose `columns` lists mix the
three item forms - a plain string column, a {source, alias}
rename, and an {expression, alias} item - compile and build
under a real `dbt build` on DuckDB, and the built values are
the items' semantics. Every dbt subprocess carries the
standing telemetry opt-outs (via the harness env).
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
        "name: expression_lists_proof\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: expression_lists_proof\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(
            "expression_lists_proof",
            str(project / "expression_lists_proof.duckdb"),
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
        "id,order_id,customer_id,product_name,quantity,unit_price\n"
        "100,500,1,Widget,3,250\n"
        "101,501,2,Gadget,2,999\n",
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
        "bronze_customers",
        "bronze",
        {
            "description": "Customers as landed",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
        },
        pattern="bronze",
    )
    write_pattern_instance(
        project,
        "silver_customers",
        "silver",
        {
            "description": "Cleaned customers with a rename and an expression",
            "owner": "analytics",
            "input_models": ["bronze_customers"],
            "dedup_key": "id",
            "columns": [
                "id",
                {"source": "customer_name", "alias": "name"},
                {"expression": "upper(customer_email)", "alias": "email_upper"},
            ],
        },
        pattern="silver",
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
                {"source": "order_id", "alias": "order_id"},
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "product_name", "alias": "product_name"},
                {"source": "quantity", "alias": "quantity"},
                {"source": "unit_price", "alias": "unit_price"},
            ],
        },
        pattern="staging",
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
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "obt_order_lines",
        "obt",
        {
            "description": "Order lines with a rename and a computed total",
            "owner": "analytics",
            "input_models": ["stg_order_items"],
            "columns": [
                "order_item_id",
                {"source": "product_name", "alias": "product"},
                {"expression": "quantity * unit_price", "alias": "line_total"},
            ],
            "joins": [
                {
                    "model": "stg_customers",
                    "on": ["customer_id"],
                    "columns": ["customer_name"],
                }
            ],
        },
        pattern="obt",
    )


def test_name_list_expression_models_build_under_dbt(tmp_path: Path) -> None:
    dbt = dbt_or_skip()
    project = tmp_path / "expression_lists_proof"
    project.mkdir()
    _write_project(project)

    assert main(["generate", "--write", "--project-dir", str(project)]) == 0

    run_dbt(dbt, project, "seed")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(
        str(project / "expression_lists_proof.duckdb"), read_only=True
    )
    try:
        silver = connection.execute(
            "select id, name, email_upper from silver_customers order by id"
        ).fetchall()
        obt = connection.execute(
            "select order_item_id, product, line_total, customer_name "
            "from obt_order_lines order by order_item_id"
        ).fetchall()
    finally:
        connection.close()

    assert silver == [
        (1, "Ada", "ADA@EXAMPLE.COM"),
        (2, "Grace", "GRACE@EXAMPLE.COM"),
    ]
    assert obt == [
        (100, "Widget", 750, "Ada"),
        (101, "Gadget", 1998, "Grace"),
    ]
