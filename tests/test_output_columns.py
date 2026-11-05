"""The general output-column resolver.

Each case pins the resolver against the corresponding renderer's
SELECT construction. Silver/gold cases encode the normative
output sets.
"""
from __future__ import annotations

from loom.output_columns import OutputColumn, resolve_output_columns


def rec(name: str, generator: str, **params: object) -> dict:
    return {"name": name, "generator": generator, **params}


def names(columns: list[OutputColumn] | None) -> list[str] | None:
    return None if columns is None else [c.name for c in columns]


def test_staging_outputs_are_aliases_plus_stamp() -> None:
    columns = resolve_output_columns(
        rec(
            "stg_customers",
            "staging",
            columns=[{"source": "id", "alias": "customer_id"}, {"source": "n", "alias": "name"}],
        )
    )
    assert names(columns) == ["customer_id", "name", "__loaded_at"]
    assert columns[-1].fixed_type == "timestamp"


def test_staging_without_columns_is_underivable() -> None:
    assert resolve_output_columns(rec("stg_x", "staging")) is None
    assert resolve_output_columns(rec("stg_x", "staging", columns=[])) is None


def test_hub_outputs_with_record_source() -> None:
    columns = resolve_output_columns(
        rec(
            "hub_customer",
            "hub",
            business_key="customer_id",
            retention="persistent",
            record_source="crm",
        )
    )
    assert names(columns) == [
        "customer_hk",
        "customer_id",
        "__record_source",
        "__loaded_at",
    ]


def test_scd2_view_outputs() -> None:
    columns = resolve_output_columns(
        rec(
            "dim_customers_scd2",
            "scd2_dimension",
            business_key="customer_id",
            tracked_columns=["customer_id", "customer_name", "customer_email"],
        )
    )
    assert names(columns) == [
        "customer_id",
        "customer_name",
        "customer_email",
        "__valid_from",
        "__valid_to",
        "__is_deleted",
    ]
    by_name = {c.name: c for c in columns}
    assert by_name["__valid_from"].fixed_type == "timestamp"
    assert by_name["__is_deleted"].fixed_type == "boolean"


def test_fact_output_order() -> None:
    columns = resolve_output_columns(
        rec(
            "fact_orders",
            "fact",
            dimension_keys=["customer_id", "order_id"],
            measure_columns=["quantity", "total"],
        )
    )
    assert names(columns) == ["customer_id", "order_id", "__loaded_at", "quantity", "total"]


def test_dimension_outputs() -> None:
    columns = resolve_output_columns(
        rec(
            "dim_customers",
            "dimension",
            natural_key="customer_id",
            surrogate_key="customer_sk",
            attribute_columns=["customer_hk"],
        )
    )
    assert names(columns) == ["customer_sk", "customer_id", "customer_hk", "__loaded_at"]
    assert columns[0].fixed_type == "bigint"


def test_bronze_is_underivable() -> None:
    assert resolve_output_columns(rec("bronze_x", "bronze")) is None


def test_view_with_star_is_underivable() -> None:
    assert (
        resolve_output_columns(
            rec("rpt_x", "view", select_columns=[{"source": "*"}])
        )
        is None
    )


def test_obt_outputs_dedupe_with_join_aliases() -> None:
    columns = resolve_output_columns(
        rec(
            "obt_orders",
            "obt",
            columns=["line_id", "amount"],
            joins=[
                {"model": "dim_a", "columns": ["order_id", "region"]},
                {"model": "dim_b", "columns": ["region", "channel"]},
            ],
        )
    )
    # The source model's declared columns project first, then
    # the joins' columns with the repeat-alias rule, then the stamp.
    assert names(columns) == [
        "line_id",
        "amount",
        "order_id",
        "region",
        "j1_region",
        "channel",
        "__loaded_at",
    ]


def test_obt_source_stamp_entry_is_not_duplicated() -> None:
    columns = resolve_output_columns(
        rec(
            "obt_orders",
            "obt",
            columns=["line_id", "__loaded_at"],
            joins=[{"model": "dim_a", "columns": ["line_id"]}],
        )
    )
    assert names(columns) == ["line_id", "j0_line_id", "__loaded_at"]


def _chain() -> dict[str, dict]:
    staging = rec(
        "stg_customers",
        "staging",
        columns=[{"source": "id", "alias": "customer_id"}, {"source": "src", "alias": "__record_source"}],
    )
    bronze = rec("bronze_customers", "bronze")
    silver_over_staging = rec(
        "silver_customers", "silver", input_models=["stg_customers"], dedup_key="customer_id"
    )
    silver_over_bronze = rec(
        "silver_raw", "silver", input_models=["bronze_customers"], dedup_key="customer_id"
    )
    return {
        "stg_customers": staging,
        "bronze_customers": bronze,
        "silver_customers": silver_over_staging,
        "silver_raw": silver_over_bronze,
    }


def test_silver_output_is_input_set_without_rn() -> None:
    by_name = _chain()
    columns = resolve_output_columns(by_name["silver_customers"], by_name)
    # Silver's set is the input's set - no added stamp,
    # and the internal dedup rn column is never an output column.
    assert names(columns) == ["customer_id", "__record_source", "__loaded_at"]


def test_silver_over_bronze_is_underivable() -> None:
    by_name = _chain()
    assert resolve_output_columns(by_name["silver_raw"], by_name) is None


def test_silver_declared_columns_are_the_output_set() -> None:
    # With a dedup key and declared columns, the renderer
    # projects exactly those columns (plus the carried stamp when
    # omitted) - derivable even over an underivable input.
    silver = rec(
        "silver_customers",
        "silver",
        input_models=["bronze_customers"],
        dedup_key="id",
        columns=["id", "customer_name"],
    )
    assert names(resolve_output_columns(silver, _chain())) == [
        "id",
        "customer_name",
        "__loaded_at",
    ]


def test_silver_declared_columns_keep_declared_stamp_position() -> None:
    silver = rec(
        "silver_customers",
        "silver",
        input_models=["bronze_customers"],
        dedup_key="id",
        columns=["id", "__loaded_at", "customer_name"],
    )
    assert names(resolve_output_columns(silver, _chain())) == [
        "id",
        "__loaded_at",
        "customer_name",
    ]


def test_silver_cycle_is_underivable_not_a_hang() -> None:
    a = rec("silver_a", "silver", input_models=["silver_b"], dedup_key="id")
    b = rec("silver_b", "silver", input_models=["silver_a"], dedup_key="id")
    by_name = {"silver_a": a, "silver_b": b}
    assert resolve_output_columns(a, by_name) is None


def test_gold_grand_total_outputs() -> None:
    columns = resolve_output_columns(
        rec(
            "gold_total",
            "gold",
            aggregation={
                "group_by": [],
                "measures": [{"column": "amount", "expression": "SUM(amount)", "alias": "total"}],
            },
        )
    )
    assert names(columns) == ["total", "__loaded_at"]


def test_gold_pass_through_appends_stamp_to_input_set() -> None:
    by_name = _chain()
    gold = rec("gold_customers", "gold", input_models=["silver_customers"])
    columns = resolve_output_columns(gold, by_name)
    # The input already carries __loaded_at:
    # the catalogue lists it once.
    assert names(columns) == ["customer_id", "__record_source", "__loaded_at"]


def test_gold_pass_through_over_bronze_is_underivable() -> None:
    by_name = _chain()
    gold = rec("gold_raw", "gold", input_models=["bronze_customers"])
    assert resolve_output_columns(gold, by_name) is None


def test_view_outputs_are_aliases() -> None:
    # Kept deliberately: this is the only
    # default-suite test exercising alias derivation for a select
    # column without an explicit alias (output_columns.py's
    # from_ref.split(".")[-1] branch); the view golden's columns
    # all carry aliases, so deleting it dropped that branch's
    # coverage.
    columns = resolve_output_columns(
        rec(
            "rpt_customers",
            "view",
            select_columns=[
                {"source": "n_customer.customer_id"},
                {"source": "n_order.total", "alias": "order_total"},
            ],
        )
    )
    assert names(columns) == ["customer_id", "order_total"]


def test_unknown_generator_is_underivable() -> None:
    assert resolve_output_columns(rec("x", "nope")) is None
