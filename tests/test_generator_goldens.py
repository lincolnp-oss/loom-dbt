"""Per-generator full-output goldens.

One golden per registered generator (its canonical instance,
DuckDB dialect, full rendered output, unnormalised beyond the
trailing newline) plus the machinery variants whose output shape
differs structurally. Byte-identity is the programme's invariant -
a normalised golden would hide exactly the drift a golden exists
to catch.

Regenerate after an intended output change:
``LOOM_UPDATE_GOLDENS=1 pytest tests/test_generator_goldens.py``
and review the diff like code.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from generator_conformance import CANONICAL_INSTANCES
from loom.dialects import DIALECTS
from loom.renderers import get_renderer

GOLDENS_DIR = Path(__file__).parent / "goldens"


def _canonical(generator: str) -> tuple[str, dict]:
    for name, gen, params in CANONICAL_INSTANCES:
        if gen == generator:
            return name, {"name": name, **params}
    raise AssertionError(f"no canonical instance for {generator}")


# Machinery-variant params, owned here rather than imported from
# the parse matrix: the satellite shapes are canonicalised out of
# multi-active / effectivity shapes out of the parse matrix, so the
# goldens must not depend on those entries existing there. Copied
# verbatim from the earlier parse CASES; scd2 / fact variants are
# copied too so every golden variant has exactly one home.
_VARIANT_PARAMS: dict[tuple[str, str], dict] = {
    ("satellite", "non_historised"): {
        "name": "sat_order_events",
        "parent": {
            "model": "link_order_customer",
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
        "input_models": ["stg_order_items"],
        "variant": "non_historised",
        "attribute_columns": ["product_name", "quantity"],
        "record_source": "pos",
    },
    ("satellite", "multi_active"): {
        "name": "sat_customer_phones",
        "parent": {"model": "hub_customer", "key": "customer_id"},
        "input_models": ["stg_customer_phones"],
        "variant": "multi_active",
        "subsequence_key": "phone_type",
        "attribute_columns": ["phone_number"],
        "record_source": "crm",
    },
    ("satellite", "effectivity"): {
        "name": "sat_order_customer_effectivity",
        "parent": {
            "model": "link_order_customer",
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
        "input_models": ["stg_orders"],
        "variant": "effectivity",
        "driving_key": "order_id",
        "record_source": "oms",
    },
    ("scd2_dimension", "type_3"): {
        "name": "dim_customers_scd3",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
        "tracked_columns": ["customer_name", "customer_email"],
        "variant": "type_3",
        "previous_value_columns": ["customer_name"],
    },
    ("scd2_dimension", "type_6"): {
        "name": "dim_customers_scd6",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
        "tracked_columns": ["customer_name", "customer_email"],
        "variant": "type_6",
        "previous_value_columns": ["customer_name"],
    },
    ("fact", "periodic_snapshot"): {
        "name": "orders_periodic_fact",
        "input_models": ["stg_orders"],
        "dimension_keys": ["customer_id", "period_key"],
        "measure_columns": ["quantity"],
        "variant": "periodic_snapshot",
        "period_key": "period_key",
    },
    ("staging", "macro_tier2"): {
        "name": "stg_macro_tier2",
        "inline_source": {"name": "raw", "table": "events", "schema": "raw"},
        "columns": [
            {"source": "id", "alias": "event_id"},
            {
                "source": "amount",
                "alias": "amount_usd",
                "macro": "cents_to_dollars",
                "macro_args": ["USD"],
            },
            {
                "source": "created_at",
                "alias": "created_date",
                "macro": "dbt.date_trunc",
                "macro_args": ["day"],
                "macro_column_param": 2,
            },
            {
                "source": "amount",
                "alias": "amount_band",
                "macro": "band_amount",
                "macro_args": ["low", "high"],
                "macro_column_param": 3,
            },
        ],
    },
    ("staging", "expression"): {
        # The pi session's motivating shape (ADR 0037): a plain
        # SQL computation carried verbatim under its alias,
        # beside ordinary source columns.
        "name": "stg_payments_expr",
        "inline_source": {"name": "raw", "table": "payments", "schema": "raw"},
        "columns": [
            {"source": "id", "alias": "payment_id"},
            {"source": "customer_id", "alias": "customer_id"},
            {"expression": "amount / 100", "alias": "amount"},
        ],
    },
    ("view", "expression"): {
        "name": "vw_customer_names",
        "input_models": ["n_customers"],
        "select_columns": [
            {"source": "n_customers.customer_id", "alias": "customer_id"},
            {
                "expression": "n_customers.first_name || ' ' || n_customers.last_name",
                "alias": "full_name",
            },
        ],
    },
    ("gold", "expression_group_by"): {
        "name": "gold_monthly_orders",
        "input_models": ["stg_orders"],
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
            ],
        },
    },
    ("mart", "expression_group_by"): {
        "name": "mart_daily_orders",
        "input_models": ["fact_orders"],
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
    ("silver", "expression"): {
        # ADR 0037 name-list forms (PR E2): a plain column, a
        # rename, and an expression item in one list.
        "name": "silver_customers_expr",
        "input_models": ["bronze_customers"],
        "dedup_key": "id",
        "columns": [
            "id",
            {"source": "customer_name", "alias": "name"},
            {"expression": "upper(customer_email)", "alias": "email_upper"},
        ],
    },
    ("obt", "expression"): {
        "name": "obt_order_lines_expr",
        "input_models": ["stg_order_items"],
        "columns": [
            "order_item_id",
            {"source": "product_name", "alias": "product"},
            {"expression": "quantity * unit_price", "alias": "line_total"},
        ],
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
    ("dimension", "expression"): {
        # ADR 0037 Kimball name-list forms (PR E3): a plain
        # attribute, a rename, and an expression attribute.
        "name": "dim_customers_expr",
        "input_models": ["hub_customer"],
        "natural_key": "customer_id",
        "surrogate_key": "customer_sk",
        "attribute_columns": [
            "customer_hk",
            {"source": "customer_name", "alias": "name"},
            {"expression": "upper(customer_email)", "alias": "email_upper"},
        ],
    },
    ("fact", "expression"): {
        "name": "fact_orders_expr",
        "input_models": ["link_order_customer"],
        "dimension_keys": ["order_hk", "customer_hk"],
        "measure_columns": [
            "quantity",
            {"source": "gross_amount", "alias": "gross"},
            {"expression": "quantity * unit_price", "alias": "line_total"},
        ],
    },
    ("fact", "accumulating_snapshot"): {
        "name": "orders_accumulating_fact",
        "input_models": ["stg_orders"],
        "dimension_keys": ["customer_id", "ordered_key", "shipped_key"],
        "measure_columns": ["quantity"],
        "variant": "accumulating_snapshot",
        "milestone_keys": ["ordered_key", "shipped_key"],
    },
    ("staging", "filter"): {
        # The filter parameter (ADR 0038): a verbatim predicate
        # over the source scan, beside plain and expression items.
        "name": "stg_customers_filtered",
        "inline_source": {"name": "raw", "table": "customers", "schema": "raw"},
        "columns": [
            {"source": "id", "alias": "customer_id"},
            {"source": "customer_name", "alias": "customer_name"},
            {"expression": "upper(customer_email)", "alias": "email_upper"},
        ],
        "filter": "is_active = 1",
    },
    ("silver", "filter"): {
        # Filter before dedup ranking: the predicate sits inside
        # the ranking subquery.
        "name": "silver_customers_filtered",
        "input_models": ["bronze_customers"],
        "dedup_key": "id",
        "columns": ["id", "customer_name", "customer_email"],
        "filter": "status = 'active'",
    },
    ("silver", "filter_incremental"): {
        # Single-WHERE composition: filter first, the affected-key
        # restriction ANDed behind it inside its gate.
        "name": "silver_customers_filtered_incr",
        "input_models": ["bronze_customers"],
        "dedup_key": "id",
        "columns": ["id", "customer_name", "customer_email"],
        "filter": "status = 'active'",
        "incremental": {"strategy": "merge", "unique_key": "id"},
    },
    ("dimension", "filter"): {
        "name": "dim_customers_filtered",
        "input_models": ["hub_customer"],
        "natural_key": "customer_id",
        "surrogate_key": "customer_sk",
        "attribute_columns": ["customer_hk"],
        "filter": "customer_id IS NOT NULL",
    },
    ("dimension", "filter_incremental"): {
        # Single-WHERE composition: filter first, the cursor
        # predicate ANDed behind it inside its gate.
        "name": "dim_customers_filtered_incr",
        "input_models": ["hub_customer"],
        "natural_key": "customer_id",
        "surrogate_key": "customer_sk",
        "attribute_columns": ["customer_hk"],
        "filter": "customer_id IS NOT NULL",
        "incremental": {"strategy": "merge", "cursor_column": "loaded_at"},
    },
    ("fact", "filter"): {
        "name": "fact_orders_filtered",
        "input_models": ["stg_orders"],
        "dimension_keys": ["customer_id"],
        "measure_columns": ["quantity"],
        "filter": "quantity > 0",
    },
    ("fact", "filter_incremental"): {
        "name": "fact_orders_filtered_incr",
        "input_models": ["stg_orders"],
        "dimension_keys": ["customer_id"],
        "measure_columns": ["quantity"],
        "filter": "quantity > 0",
        "incremental": {
            "strategy": "merge",
            "unique_key": "order_id",
            "cursor_column": "ordered_at",
        },
    },
    ("scd2_dimension", "filter"): {
        # The predicate lands on the snapshot's input select
        # (this case renders the snapshot: output_path None).
        "name": "dim_customers_scd2_filtered",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
        "tracked_columns": ["customer_name", "customer_email"],
        "filter": "is_active = 1",
    },
    ("normalized_table", "filter"): {
        "name": "n_customers_filtered",
        "input_models": ["stg_customers"],
        "entity_columns": ["customer_id"],
        "attribute_columns": ["customer_name", "customer_email"],
        "filter": "region = 'AU'",
    },
}


def _case(generator: str, variant: str | None) -> dict:
    try:
        return _VARIANT_PARAMS[(generator, variant)]  # type: ignore[arg-type]
    except KeyError:
        raise AssertionError(f"no variant params for {generator}/{variant}") from None


# (golden id, generator, render params, output_path sentinel)
def _golden_cases() -> list[tuple[str, str, dict, object]]:
    cases: list[tuple[str, str, dict, object]] = []
    seen: set[str] = set()
    for _, generator, _ in CANONICAL_INSTANCES:
        if generator in seen:
            continue
        seen.add(generator)
        name, params = _canonical(generator)
        cases.append((name, generator, params, None))
    # Machinery variants (params owned by _VARIANT_PARAMS above,
    # plus the canonical record-tracking instance).
    name, params = "sat_customer_tracking", {
        "name": "sat_customer_tracking",
        **dict(CANONICAL_INSTANCES[10][2]),
    }
    cases.append((name, "satellite", params, None))
    for variant in ("non_historised", "multi_active", "effectivity"):
        params = _case("satellite", variant)
        cases.append((params["name"], "satellite", params, None))
    name, params = _canonical("satellite")
    cases.append(
        ("sat_customer_details_current", "satellite", params, Path("current"))
    )
    for variant in ("type_3", "type_6"):
        params = _case("scd2_dimension", variant)
        cases.append((params["name"], "scd2_dimension", params, None))
    for variant in ("periodic_snapshot", "accumulating_snapshot"):
        params = _case("fact", variant)
        cases.append((params["name"], "fact", params, None))
    params = _case("staging", "macro_tier2")
    cases.append((params["name"], "staging", params, None))
    for generator, variant in (
        ("staging", "expression"),
        ("view", "expression"),
        ("gold", "expression_group_by"),
        ("mart", "expression_group_by"),
        ("silver", "expression"),
        ("obt", "expression"),
        ("dimension", "expression"),
        ("fact", "expression"),
    ):
        params = _case(generator, variant)
        cases.append((params["name"], generator, params, None))
    for generator, variant in (
        ("staging", "filter"),
        ("silver", "filter"),
        ("silver", "filter_incremental"),
        ("dimension", "filter"),
        ("dimension", "filter_incremental"),
        ("fact", "filter"),
        ("fact", "filter_incremental"),
        ("scd2_dimension", "filter"),
        ("normalized_table", "filter"),
    ):
        params = _case(generator, variant)
        cases.append((params["name"], generator, params, None))
    return cases


GOLDEN_CASES = _golden_cases()


@pytest.mark.parametrize(
    "golden_id,generator,params,output_path",
    GOLDEN_CASES,
    ids=[case[0] for case in GOLDEN_CASES],
)
def test_generator_golden(
    golden_id: str, generator: str, params: dict, output_path: object
) -> None:
    rendered = get_renderer(generator, "duckdb").render(
        params,
        DIALECTS["duckdb"],
        output_path,  # type: ignore[arg-type]
    )
    golden_file = GOLDENS_DIR / f"{golden_id}.sql"
    if os.environ.get("LOOM_UPDATE_GOLDENS") == "1":
        GOLDENS_DIR.mkdir(exist_ok=True)
        golden_file.write_text(rendered.rstrip("\n") + "\n", encoding="utf-8")
        return
    assert golden_file.exists(), (
        f"missing golden {golden_file.name}; regenerate with "
        "LOOM_UPDATE_GOLDENS=1 and review the output"
    )
    expected = golden_file.read_text(encoding="utf-8")
    assert rendered.rstrip("\n") + "\n" == expected, (
        f"rendered output for {golden_id} differs from its golden; "
        "if the change is intended, regenerate with "
        "LOOM_UPDATE_GOLDENS=1 and review the diff"
    )


def test_every_golden_render_ends_with_a_trailing_newline() -> None:
    # QOL A-8: a generated file is a well-formed text file. The
    # golden comparison above normalises the trailing newline (it
    # predates the guarantee), so pin the guarantee itself here,
    # across every generator's canonical render.
    for golden_id, generator, params, output_path in GOLDEN_CASES:
        rendered = get_renderer(generator, "duckdb").render(
            params,
            DIALECTS["duckdb"],
            output_path,  # type: ignore[arg-type]
        )
        assert rendered.endswith("\n"), (
            f"{golden_id}: rendered SQL does not end with a newline"
        )
