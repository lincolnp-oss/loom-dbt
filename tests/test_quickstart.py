from __future__ import annotations

from pathlib import Path

from loom.inventory import validate_project


ROOT = Path(__file__).parents[1]


def test_quickstart_inventory_is_valid_and_coherent() -> None:
    entries = validate_project(ROOT / "examples" / "quickstart")

    assert {(entry.data["name"], entry.data.get("generator")) for entry in entries} == {
        ("stg_customers", "staging"),
        ("stg_orders", "staging"),
        ("stg_order_items", "staging"),
        ("stg_products", "staging"),
        ("hub_customer", "hub"),
        ("hub_order", "hub"),
        ("hub_product", "hub"),
        ("link_order_customer", "link"),
        ("link_order_customer_product", "link"),
        ("link_order_item_events", "link"),
        ("sat_customer_details", "satellite"),
        ("sat_order_customer_details", "satellite"),
        ("sat_customer_tracking", "satellite"),
        ("pit_customer", "pit"),
        ("sat_order_customer_effectivity", "satellite"),
        ("bridge_customer_orders", "bridge"),
        ("dim_customers_scd2", "scd2_dimension"),
        ("dim_customers_scd6", "scd2_dimension"),
        ("fact_orders", "fact"),
        ("dim_customers", "dimension"),
        ("bronze_customers", "bronze"),
        ("bronze_orders", "bronze"),
        ("bronze_order_items", "bronze"),
        ("silver_customers", "silver"),
        ("silver_orders", "silver"),
        ("silver_order_items", "silver"),
        ("gold_customer_orders", "gold"),
        ("n_customers", "normalized_table"),
        ("n_orders", "normalized_table"),
        ("vw_customer_orders", "view"),
        ("obt_order_lines", "obt"),
        ("mart_customer_orders", "mart"),
        ("fct_order_totals", None),
    }
    staging = [
        entry.data for entry in entries if entry.data.get("generator") == "staging"
    ]
    assert len(staging) == 4
    assert all(
        data["source_ref"]["name"] == "raw" and "inline_source" not in data
        for data in staging
    )
