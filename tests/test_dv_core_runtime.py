"""DV core hardening runtime proof on DuckDB (ADR 0030).

Two projects:

1. A multi-source hub (CRM + web staging, per-source provenance,
   earliest declared input winning a contested key), a ternary
   link over order items, and a standard satellite parented on
   that link - the satellite's computed parent hashes must equal
   the link's own hashes at runtime, not just in the SQL text.
2. A non-historised (event) link: a re-sent event with corrected
   payload keeps its first-seen payload, and a new event appends.
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
def test_multi_source_hub_ternary_link_and_its_satellite(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "dv_core_runtime"
    _write_core_project(project)
    _write_seed(project, "customers_crm", ["id"], [(1,), (2,)])
    _write_seed(project, "customers_web", ["id"], [(2,), (3,)])
    _write_seed(
        project,
        "order_items",
        ["id", "order_id", "customer_id", "product_name", "quantity"],
        [
            (11, 101, 1, "Widget A", 2),
            (12, 101, 1, "Widget B", 1),
            (13, 102, 2, "Widget A", 5),
            (14, 103, 3, "Widget A", 1),
        ],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert (
        main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"])
        == 0
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")
    # A second identical build must not duplicate the persistent
    # hub's rows or append duplicate satellite rows.
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(str(project / "dv_core.duckdb"), read_only=True)
    try:
        hub_rows = connection.execute(
            "select customer_hk, customer_id, __record_source "
            "from main.hub_customer order by customer_id"
        ).fetchall()
        link_rows = connection.execute(
            "select order_customer_product_hk, order_hk, customer_hk, product_hk "
            "from main.link_order_customer_product"
        ).fetchall()
        satellite_rows = connection.execute(
            "select order_customer_product_hk, quantity "
            "from main.sat_order_line_details"
        ).fetchall()
    finally:
        connection.close()

    # Customer 2 arrives from both sources; crm is declared first
    # and wins. Provenance is per source.
    assert hub_rows == [
        (_sha256("1"), "1", "crm"),
        (_sha256("2"), "2", "crm"),
        (_sha256("3"), "3", "web"),
    ]

    def link_hash(order_id: int, customer_id: int, product: str) -> str:
        return _sha256(
            _sha256(str(order_id))
            + _sha256(str(customer_id))
            + _sha256(product.upper())
        )

    expected_links = {
        link_hash(101, 1, "Widget A"),
        link_hash(101, 1, "Widget B"),
        link_hash(102, 2, "Widget A"),
        link_hash(103, 3, "Widget A"),
    }
    assert {row[0] for row in link_rows} == expected_links
    assert len(link_rows) == 4
    # The satellite joined its input to the link on the computed
    # parent hash: its keys are exactly the link's hashes.
    assert {row[0] for row in satellite_rows} == expected_links
    assert {row[0]: row[1] for row in satellite_rows} == {
        link_hash(101, 1, "Widget A"): 2,
        link_hash(101, 1, "Widget B"): 1,
        link_hash(102, 2, "Widget A"): 5,
        link_hash(103, 3, "Widget A"): 1,
    }

@pytest.mark.integration
def test_non_historised_link_inserts_each_event_once(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "nh_link_runtime"
    _write_nh_project(project)

    header = ["id", "order_id", "product_name", "quantity"]
    _write_seed(
        project,
        "order_items",
        header,
        [(11, 101, "Widget A", 2), (12, 101, "Widget B", 1)],
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert (
        main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"])
        == 0
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")

    # Event 11 is re-sent with a corrected quantity (an immutable
    # event keeps its first-seen payload); event 13 is new.
    _write_seed(
        project,
        "order_items",
        header,
        [
            (11, 101, "Widget A", 99),
            (12, 101, "Widget B", 1),
            (13, 102, "Widget A", 5),
        ],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(str(project / "nh_link.duckdb"), read_only=True)
    try:
        rows = connection.execute(
            "select order_item_id, quantity from main.link_order_item_events "
            "order by order_item_id"
        ).fetchall()
        hashes = connection.execute(
            "select order_item_events_hk from main.link_order_item_events"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [("11", 2), ("12", 1), ("13", 5)]

    def event_hash(order_id: int, product: str, item_id: int) -> str:
        # Endpoint hashes in declared order, then the normalized
        # dependent-child key as the final component (ADR 0030).
        return _sha256(
            _sha256(str(order_id)) + _sha256(product.upper()) + str(item_id)
        )

    assert {row[0] for row in hashes} == {
        event_hash(101, "Widget A", 11),
        event_hash(101, "Widget B", 12),
        event_hash(102, "Widget A", 13),
    }

# -- project writers ----------------------------------------------------

def _write_project_shell(
    project: Path, name: str, duckdb_file: str, sources: list[str]
) -> None:
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
        duckdb_profiles_yml(name, duckdb_file), encoding="utf-8"
    )
    tables = "".join(f"      - name: {table}\n" for table in sources)
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n" + tables,
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
    (project / "metadata" / "patterns" / "link.yml").write_text(
        "name: link\n"
        "generator: link\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, endpoints]\n",
        encoding="utf-8",
    )

def _write_core_project(project: Path) -> None:
    _write_project_shell(
        project,
        "dv_core_runtime_test",
        "dv_core.duckdb",
        ["customers_crm", "customers_web", "order_items"],
    )
    (project / "metadata" / "patterns" / "multi_hub.yml").write_text(
        "name: multi_hub\n"
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
        "  - name: stg_customers_crm\n"
        "    params:\n"
        "      description: CRM customer staging.\n"
        "      source_ref: {name: raw, table: customers_crm}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "  - name: stg_customers_web\n"
        "    params:\n"
        "      description: Web customer staging.\n"
        "      source_ref: {name: raw, table: customers_web}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "  - name: stg_order_items\n"
        "    params:\n"
        "      description: Order items staging.\n"
        "      source_ref: {name: raw, table: order_items}\n"
        "      columns:\n"
        "        - {source: id, alias: order_item_id}\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: customer_id, alias: customer_id}\n"
        "        - {source: product_name, alias: product_name}\n"
        "        - {source: quantity, alias: quantity}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "hub.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_order\n"
        "    params:\n"
        "      description: Order hub.\n"
        "      input_models: [stg_order_items]\n"
        "      business_key: order_id\n"
        "  - name: hub_product\n"
        "    params:\n"
        "      description: Product hub.\n"
        "      input_models: [stg_order_items]\n"
        "      business_key: product_name\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "multi_hub.yml").write_text(
        "pattern: multi_hub\n"
        "instances:\n"
        "  - name: hub_customer\n"
        "    params:\n"
        "      description: Customer hub from CRM and web.\n"
        "      input_models: [stg_customers_crm, stg_customers_web]\n"
        "      business_key: customer_id\n"
        "      retention: persistent\n"
        "      record_source: {stg_customers_crm: crm, stg_customers_web: web}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "link.yml").write_text(
        "pattern: link\n"
        "instances:\n"
        "  - name: link_order_customer_product\n"
        "    params:\n"
        "      description: Ternary order/customer/product link.\n"
        "      input_models: [stg_order_items]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_customer\n"
        "          key: customer_id\n"
        "        - {hub: hub_product, key: product_name}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "satellite.yml").write_text(
        "pattern: satellite\n"
        "instances:\n"
        "  - name: sat_order_line_details\n"
        "    params:\n"
        "      description: Quantities on the ternary link.\n"
        "      parent:\n"
        "        model: link_order_customer_product\n"
        "        endpoints:\n"
        "          - hub: hub_order\n"
        "            key: order_id\n"
        "          - hub: hub_customer\n"
        "            key: customer_id\n"
        "          - {hub: hub_product, key: product_name}\n"
        "      input_models: [stg_order_items]\n"
        "      attribute_columns: [quantity]\n"
        "      record_source: orders\n",
        encoding="utf-8",
    )

def _write_nh_project(project: Path) -> None:
    _write_project_shell(
        project, "nh_link_runtime_test", "nh_link.duckdb", ["order_items"]
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_order_items\n"
        "    params:\n"
        "      description: Order items staging.\n"
        "      source_ref: {name: raw, table: order_items}\n"
        "      columns:\n"
        "        - {source: id, alias: order_item_id}\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: product_name, alias: product_name}\n"
        "        - {source: quantity, alias: quantity}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "hub.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_order\n"
        "    params:\n"
        "      description: Order hub.\n"
        "      input_models: [stg_order_items]\n"
        "      business_key: order_id\n"
        "  - name: hub_product\n"
        "    params:\n"
        "      description: Product hub.\n"
        "      input_models: [stg_order_items]\n"
        "      business_key: product_name\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "link.yml").write_text(
        "pattern: link\n"
        "instances:\n"
        "  - name: link_order_item_events\n"
        "    params:\n"
        "      description: Order item events, non-historised.\n"
        "      input_models: [stg_order_items]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_product\n"
        "          key: product_name\n"
        "      variant: non_historised\n"
        "      dependent_child_key: order_item_id\n"
        "      payload_columns: [quantity]\n",
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

