"""Bridge runtime proof on DuckDB (ADR 0036).

A two-step bridge (customer → orders → products) over seeded
orders: the flattened rows must be exactly the path combinations,
and equal weighting must give each anchor hub weights summing to
1 - the fan-out-safety property the column exists for.
"""
from __future__ import annotations

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
def test_bridge_two_step_path_and_weighting_on_duckdb(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "bridge_runtime"
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: bridge_runtime\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: bridge_runtime\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('bridge_runtime', 'bridge_runtime.duckdb'),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: orders\n"
        "      - name: order_items\n",
        encoding="utf-8",
    )
    patterns = {
        "staging.yml": ("staging", "staging", "[description, source_ref, columns]"),
        "hub.yml": ("hub", "hub", "[description, input_models, business_key]"),
        "link.yml": (
            "link",
            "link",
            "[description, input_models, endpoints]",
        ),
        "bridge.yml": ("bridge", "bridge", "[description, parent, path]"),
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
        "  - name: stg_orders\n"
        "    params:\n"
        "      description: Orders.\n"
        "      source_ref: {name: raw, table: orders}\n"
        "      columns:\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: customer_id, alias: customer_id}\n"
        "  - name: stg_order_items\n"
        "    params:\n"
        "      description: Order items.\n"
        "      source_ref: {name: raw, table: order_items}\n"
        "      columns:\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: product_id, alias: product_id}\n",
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
        "  - name: hub_order\n"
        "    params:\n"
        "      description: Order hub.\n"
        "      input_models: [stg_orders]\n"
        "      business_key: order_id\n"
        "  - name: hub_product\n"
        "    params:\n"
        "      description: Product hub.\n"
        "      input_models: [stg_order_items]\n"
        "      business_key: product_id\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "links.yml").write_text(
        "pattern: link\n"
        "instances:\n"
        "  - name: link_order_customer\n"
        "    params:\n"
        "      description: Order-customer link.\n"
        "      input_models: [stg_orders]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_customer\n"
        "          key: customer_id\n"
        "  - name: link_order_product\n"
        "    params:\n"
        "      description: Order-product link.\n"
        "      input_models: [stg_order_items]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_product\n"
        "          key: product_id\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "bridges.yml").write_text(
        "pattern: bridge\n"
        "instances:\n"
        "  - name: bridge_customer_products\n"
        "    params:\n"
        "      description: Customers to the products they ordered.\n"
        "      parent: {model: hub_customer}\n"
        "      weighting: equal\n"
        "      path:\n"
        "        - {link: link_order_customer, hub: hub_order}\n"
        "        - {link: link_order_product, hub: hub_product}\n",
        encoding="utf-8",
    )
    (project / "seeds" / "customers.csv").write_text("id\n7\n9\n", encoding="utf-8")
    (project / "seeds" / "orders.csv").write_text(
        "order_id,customer_id\n101,7\n102,7\n103,9\n", encoding="utf-8"
    )
    (project / "seeds" / "order_items.csv").write_text(
        "order_id,product_id\n101,1\n101,2\n102,1\n103,3\n", encoding="utf-8"
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    run_dbt(dbt, project, "seed")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(str(project / "bridge_runtime.duckdb"), read_only=True)
    try:
        rows = connection.execute(
            "select customer_hk, order_hk, product_hk, __weighting_factor "
            "from main.bridge_customer_products"
        ).fetchall()
    finally:
        connection.close()

    expected = {
        (_sha256("7"), _sha256("101"), _sha256("1")),
        (_sha256("7"), _sha256("101"), _sha256("2")),
        (_sha256("7"), _sha256("102"), _sha256("1")),
        (_sha256("9"), _sha256("103"), _sha256("3")),
    }
    assert {(r[0], r[1], r[2]) for r in rows} == expected
    # Equal weighting: each customer's factors sum to 1.
    totals: dict[str, float] = {}
    for customer_hk, _, _, factor in rows:
        totals[customer_hk] = totals.get(customer_hk, 0.0) + float(factor)
    assert totals[_sha256("7")] == pytest.approx(1.0)
    assert totals[_sha256("9")] == pytest.approx(1.0)
    assert all(
        float(factor) == pytest.approx(1.0 / 3)
        for _, _, _, factor in rows
        if factor != 1.0
    )
