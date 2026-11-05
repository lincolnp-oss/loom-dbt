"""Registry-driven conformance harness.

One canonical instance per registered generator, declared here as
data, drives the conformance loop-tests in
``test_generator_conformance.py`` and the golden renders in
``test_generator_goldens.py``. The canonical instances form one
coherent mini-project (a staging → vault → kimball/medallion chain
plus the reference constructs), so the harness can also *generate*
the project and *execute* the generated chain on DuckDB.

Adding a generator: add its canonical instance (in dependency
order), one line to ``EXPECTED_KEYS`` if it carries keys, and one
golden (``LOOM_UPDATE_GOLDENS=1`` regenerates). The loop-tests pick
it up from the registry automatically - no new test code.

Canonical params follow the proven shapes of the quickstart
metadata and the parse matrix's CASES; they are harness fixtures,
not practitioner examples.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

# (name, generator, params) in dependency order: every input /
# parent / reference target is declared before its consumers.
CANONICAL_INSTANCES: list[tuple[str, str, dict]] = [
    (
        "stg_customers",
        "staging",
        {
            "description": "Canonical customer staging.",
            "owner": "conformance",
            "inline_source": {"name": "raw", "table": "customers", "schema": "raw"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "customer_name", "alias": "customer_name"},
                {"source": "customer_email", "alias": "customer_email"},
            ],
        },
    ),
    (
        "stg_order_items",
        "staging",
        {
            "description": "Canonical order-item staging.",
            "owner": "conformance",
            "inline_source": {"name": "raw", "table": "order_items", "schema": "raw"},
            "columns": [
                {"source": "id", "alias": "order_item_id"},
                {"source": "order_id", "alias": "order_id"},
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "product_name", "alias": "product_name"},
                {"source": "quantity", "alias": "quantity"},
            ],
        },
    ),
    (
        "stg_country_codes",
        "staging",
        {
            "description": "Canonical country-code staging.",
            "owner": "conformance",
            "inline_source": {"name": "raw", "table": "country_codes", "schema": "raw"},
            "columns": [
                {"source": "code", "alias": "country_code"},
                {"source": "name", "alias": "country_name"},
            ],
        },
    ),
    (
        "bronze_customers",
        "bronze",
        {
            "description": "Canonical bronze customers.",
            "owner": "conformance",
            "inline_source": {"name": "raw", "table": "customers"},
        },
    ),
    (
        "silver_customers",
        "silver",
        {
            "description": "Canonical silver customers.",
            "owner": "conformance",
            "input_models": ["bronze_customers"],
            "dedup_key": "id",
            "columns": ["id", "customer_name", "customer_email"],
        },
    ),
    (
        "gold_customers",
        "gold",
        {
            "description": "Canonical gold customers.",
            "owner": "conformance",
            "input_models": ["silver_customers"],
            "aggregation": {
                "group_by": ["id"],
                "measures": [
                    {"column": "id", "expression": "COUNT(id)", "alias": "row_count"}
                ],
            },
        },
    ),
    (
        "hub_customer",
        "hub",
        {
            "description": "Canonical customer hub.",
            "owner": "conformance",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
    ),
    (
        "hub_order",
        "hub",
        {
            "description": "Canonical order hub.",
            "owner": "conformance",
            "input_models": ["stg_order_items"],
            "business_key": "order_id",
        },
    ),
    (
        "link_order_customer",
        "link",
        {
            "description": "Canonical order-customer link.",
            "owner": "conformance",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
    ),
    (
        "sat_customer_details",
        "satellite",
        {
            "description": "Canonical customer satellite.",
            "owner": "conformance",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["customer_name", "customer_email"],
            "record_source": "crm",
        },
    ),
    (
        "sat_customer_tracking",
        "satellite",
        {
            "description": "Canonical record-tracking satellite.",
            "owner": "conformance",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "variant": "record_tracking",
            "record_source": "crm",
        },
    ),
    (
        "pit_customer",
        "pit",
        {
            "description": "Canonical customer PIT.",
            "owner": "conformance",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "satellites": ["sat_customer_details", "sat_customer_tracking"],
        },
    ),
    (
        "bridge_customer_orders",
        "bridge",
        {
            "description": "Canonical customer-orders bridge.",
            "owner": "conformance",
            "parent": {"model": "hub_customer"},
            "weighting": "equal",
            "path": [{"link": "link_order_customer", "hub": "hub_order"}],
        },
    ),
    (
        "fact_orders",
        "fact",
        {
            "description": "Canonical orders fact.",
            "owner": "conformance",
            "input_models": ["link_order_customer"],
            "dimension_keys": ["order_hk", "customer_hk"],
            "measure_columns": [],
        },
    ),
    (
        "dim_customers",
        "dimension",
        {
            "description": "Canonical customer dimension.",
            "owner": "conformance",
            "input_models": ["hub_customer"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_hk"],
        },
    ),
    (
        "dim_customers_scd2",
        "scd2_dimension",
        {
            "description": "Canonical SCD2 customer dimension.",
            "owner": "conformance",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name", "customer_email"],
        },
    ),
    (
        "n_customers",
        "normalized_table",
        {
            "description": "Canonical normalized customers.",
            "owner": "conformance",
            "input_models": ["stg_customers"],
            "entity_columns": ["customer_id"],
            "attribute_columns": ["customer_name", "customer_email"],
        },
    ),
    (
        "n_orders",
        "normalized_table",
        {
            "description": "Canonical normalized orders.",
            "owner": "conformance",
            "input_models": ["stg_order_items"],
            "entity_columns": ["order_item_id"],
            "attribute_columns": [
                "order_id",
                "customer_id",
                "product_name",
                "quantity",
            ],
        },
    ),
    (
        "vw_customer_orders",
        "view",
        {
            "description": "Canonical reporting view.",
            "owner": "conformance",
            "input_models": ["n_customers", "n_orders"],
            "joins": [{"model": "n_orders", "on": ["customer_id"]}],
            "select_columns": [
                {"source": "n_customers.customer_id", "alias": "customer_id"},
                {"source": "n_customers.customer_name", "alias": "customer_name"},
                {"source": "n_orders.order_item_id", "alias": "order_item_id"},
                {"source": "n_orders.product_name", "alias": "product_name"},
            ],
        },
    ),
    (
        "obt_order_lines",
        "obt",
        {
            "description": "Canonical one-big-table order lines.",
            "owner": "conformance",
            "input_models": ["stg_order_items"],
            "columns": ["order_item_id", "product_name", "quantity"],
            "joins": [
                {
                    "model": "hub_customer",
                    "on": ["customer_id"],
                    "columns": ["customer_id", "customer_hk"],
                },
                {
                    "model": "hub_order",
                    "on": ["order_id"],
                    "columns": ["order_id", "order_hk"],
                },
            ],
        },
    ),
    (
        "mart_customer_orders",
        "mart",
        {
            "description": "Canonical customer-orders mart.",
            "owner": "conformance",
            "input_models": ["fact_orders"],
            "aggregation": {
                "group_by": ["customer_hk"],
                "measures": [
                    {
                        "column": "order_hk",
                        "expression": "COUNT(order_hk)",
                        "alias": "order_count",
                    }
                ],
            },
        },
    ),
    (
        "ref_country",
        "reference_hub",
        {
            "description": "Canonical country reference hub.",
            "owner": "conformance",
            "input_models": ["stg_country_codes"],
            "business_key": "country_code",
            "record_source": "refdata",
        },
    ),
    (
        "sat_country_details",
        "satellite",
        {
            "description": "Canonical country satellite.",
            "owner": "conformance",
            "parent": {"model": "ref_country", "key": "country_code"},
            "input_models": ["stg_country_codes"],
            "attribute_columns": ["country_name"],
            "record_source": "refdata",
        },
    ),
    (
        "ref_country_table",
        "reference_table",
        {
            "description": "Canonical country reference table.",
            "owner": "conformance",
            "reference_hub": "ref_country",
            "satellites": ["sat_country_details"],
            "business_key": "country_code",
            "attribute_columns": ["country_name"],
        },
    ),
]

CANONICAL_BY_NAME: dict[str, tuple[str, dict]] = {
    name: (generator, params) for name, generator, params in CANONICAL_INSTANCES
}

# Key columns the resolver must carry for the keyed canonical
# instances (the keys-carried invariant). Unkeyed instances
# (staging/bronze/silver/gold/view/obt/mart) assert non-empty output
# only - their key semantics are their inputs'.
EXPECTED_KEYS: dict[str, list[str]] = {
    "hub_customer": ["customer_hk"],
    "hub_order": ["order_hk"],
    "link_order_customer": ["order_customer_hk", "order_hk", "customer_hk"],
    "sat_customer_details": ["customer_hk"],
    "sat_customer_tracking": ["customer_hk"],
    "pit_customer": ["customer_hk"],
    "bridge_customer_orders": ["customer_hk", "order_hk"],
    "fact_orders": ["order_hk", "customer_hk"],
    "dim_customers": ["customer_sk"],
    "ref_country": ["country_hk"],
    "sat_country_details": ["country_hk"],
    "ref_country_table": ["country_hk"],
}

# Generators whose canonical artefact is not a directly executable
# model: the SCD2 snapshot is dbt snapshot machinery (its lifecycle
# is proven by the runtime layer), so the executes invariant skips
# its view, whose source relation is the snapshot itself.
EXECUTES_EXEMPT_GENERATORS = {"scd2_dimension"}

# Stub source tables for the executes invariant: the inline sources
# the canonical staging/bronze instances read, as (columns, rows).
STUB_SOURCES: dict[str, tuple[list[str], list[tuple]]] = {
    "customers": (
        ["id", "customer_name", "customer_email"],
        [(1, "Ann", "ann@example.com"), (2, "Bob", "bob@example.com")],
    ),
    "order_items": (
        ["id", "order_id", "customer_id", "product_name", "quantity"],
        [(10, 100, 1, "Widget", 3), (11, 101, 2, "Gadget", 1)],
    ),
    "country_codes": (
        ["code", "name"],
        [("AU", "Australia"), ("NZ", "New Zealand")],
    ),
}


def write_canonical_project(project: Path) -> None:
    """Scaffold the canonical project (metadata only) under `project`."""
    from metadata_helpers import write_pattern_instance

    (project / "dbt_project.yml").write_text(
        "name: canon\nprofile: canon\n"
        "model-paths: ['models']\nsnapshot-paths: ['snapshots']\n",
        encoding="utf-8",
    )
    for name, generator, params in CANONICAL_INSTANCES:
        write_pattern_instance(
            project, name, generator, params, pattern=f"canon_{generator}"
        )


def expanded_record(name: str) -> dict:
    """The canonical instance as an expanded inventory record."""
    generator, params = CANONICAL_BY_NAME[name]
    return {"name": name, "generator": generator, **params}


def compile_sql(rendered: str) -> str:
    """Compile dbt-flavoured Jinja down to plain SQL (harness stubs).

    The same stub shape as the parse matrix's compiler: config is
    dropped, ref() resolves to the bare relation name, source() to
    the bare table name, and is_incremental() is false (the
    first-load branch - lifecycles belong to the runtime layer).
    """
    environment = jinja2.Environment()
    template = environment.from_string(rendered)
    return template.render(
        config=lambda **kwargs: "",
        ref=lambda name: name,
        source=lambda source_name, table: table,
        is_incremental=lambda: False,
        this="loom_this",
    )
