"""The quickstart's exact generated-file set - single-sourced.

`loom generate` on `examples/quickstart` must produce exactly these
files, no more and no fewer. The installed-consumer test asserts the
set end-to-end; any future consumer (a new quickstart leg, a docs
check) imports it from here instead of restating it, so a generation
change updates exactly one list.
"""

from __future__ import annotations

QUICKSTART_GENERATED_FILES: set[str] = {
        "models/_generated/staging/stg_customers.sql",
        "models/_generated/staging/stg_orders.sql",
        "models/_generated/staging/stg_order_items.sql",
        "models/_generated/staging/stg_products.sql",
        "models/vault/hub_customer.sql",
        "models/vault/hub_order.sql",
        "models/vault/hub_product.sql",
        "models/vault/link_order_customer.sql",
        "models/vault/link_order_customer_product.sql",
        "models/vault/link_order_item_events.sql",
        "models/vault/sat_customer_details.sql",
        "models/vault/sat_customer_details_current.sql",
        "models/vault/sat_order_customer_details.sql",
        "models/vault/sat_order_customer_details_current.sql",
        "models/vault/sat_customer_tracking.sql",
        "models/vault/pit_customer.sql",
        "models/vault/sat_order_customer_effectivity.sql",
        "models/vault/sat_order_customer_effectivity_windows.sql",
        "models/vault/bridge_customer_orders.sql",
        "models/vault/dim_customers_scd2.sql",
        "models/vault/dim_customers_scd6.sql",
        "models/kimball/fact_orders.sql",
        "models/kimball/dim_customers.sql",
        "models/bronze_customers.sql",
        "models/bronze_orders.sql",
        "models/bronze_order_items.sql",
        "models/silver_customers.sql",
        "models/silver_orders.sql",
        "models/silver_order_items.sql",
        "models/gold_customer_orders.sql",
        "models/n_customers.sql",
        "models/n_orders.sql",
        "models/vw_customer_orders.sql",
        "models/obt_order_lines.sql",
        "models/mart_customer_orders.sql",
        "models/_generated/tests/vault/data_hub_customer.yml",
        "models/_generated/tests/vault/data_hub_order.yml",
        "models/_generated/tests/vault/data_hub_product.yml",
        "models/_generated/tests/vault/data_link_order_customer.yml",
        "models/_generated/tests/vault/data_link_order_customer_product.yml",
        "models/_generated/tests/vault/data_link_order_item_events.yml",
        "models/_generated/tests/vault/data_sat_customer_details.yml",
        "models/_generated/tests/vault/data_sat_order_customer_details.yml",
        "models/_generated/tests/vault/data_sat_customer_tracking.yml",
        "models/_generated/tests/vault/data_pit_customer.yml",
        "models/_generated/tests/vault/data_sat_order_customer_effectivity.yml",
        "models/_generated/tests/vault/data_bridge_customer_orders.yml",
        "models/_generated/tests/vault/data_dim_customers_scd2.yml",
        "models/_generated/tests/vault/data_dim_customers_scd6.yml",
        "models/_generated/tests/kimball/data_fact_orders.yml",
        "models/_generated/tests/kimball/data_dim_customers.yml",
        "models/_generated/tests/data_gold_customer_orders.yml",
        "models/_generated/tests/data_mart_customer_orders.yml",
        "models/_generated/tests/data_n_customers.yml",
        "models/_generated/tests/data_n_orders.yml",
        "models/_generated/tests/data_silver_customers.yml",
        "models/_generated/tests/data_silver_orders.yml",
        "models/_generated/tests/data_silver_order_items.yml",
        # Properties-only schema documents for the
        # generators that emit no data tests (staging, bronze, obt,
        # view); tested models carry their properties inside the
        # data_ documents above.
        "models/_generated/tests/properties_stg_customers.yml",
        "models/_generated/tests/properties_stg_orders.yml",
        "models/_generated/tests/properties_stg_order_items.yml",
        "models/_generated/tests/properties_stg_products.yml",
        "models/_generated/tests/properties_bronze_customers.yml",
        "models/_generated/tests/properties_bronze_orders.yml",
        "models/_generated/tests/properties_bronze_order_items.yml",
        "models/_generated/tests/properties_obt_order_lines.yml",
        "models/_generated/tests/properties_vw_customer_orders.yml",
}
