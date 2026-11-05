"""The Kimball name-list expression surfaces (ADR 0037 extension).

Dimension `attribute_columns` and fact `measure_columns` items
may be a string (a plain column, meaning unchanged), a
``{source, alias}`` mapping (a rename, accepted via the shared
name-list machinery - one grammar, stated once), or an
``{expression, alias}`` mapping (the uniform expression
grammar). scd2 `tracked_columns` and satellite
`attribute_columns` stay name-only: their items feed change
detection and hashdiff computation, and mapping items there are
rejected.
"""
from __future__ import annotations

import pytest

from loom.dialects import DIALECTS
from loom.inventory_records import _validate_record
from loom.output_columns import resolve_output_columns
from loom.renderers import get_renderer

PATH = "metadata/instances/x.yml"


def _dimension(attributes: list) -> dict:
    return {
        "name": "dim_customer",
        "generator": "dimension",
        "description": "Customer dimension.",
        "owner": "analytics",
        "input_models": ["stg_customers"],
        "natural_key": "customer_id",
        "surrogate_key": "customer_sk",
        "attribute_columns": attributes,
    }


def _fact(measures: list, **extra: object) -> dict:
    data = {
        "name": "fct_orders",
        "generator": "fact",
        "description": "Order facts.",
        "owner": "analytics",
        "input_models": ["stg_orders"],
        "dimension_keys": ["customer_id", "order_date"],
        "measure_columns": measures,
    }
    data.update(extra)
    return data


DIM_MIXED = [
    "city",
    {"source": "customer_name", "alias": "name"},
    {"expression": "upper(email)", "alias": "email_upper"},
]
FACT_MIXED = [
    "quantity",
    {"source": "gross_amount", "alias": "gross"},
    {"expression": "quantity * unit_price", "alias": "line_total"},
]


def test_dimension_items_validate() -> None:
    assert _validate_record(PATH, _dimension(DIM_MIXED), is_registration=False) == []
    assert _validate_record(PATH, _dimension(["city", "tier"]), is_registration=False) == []


def test_fact_items_validate_all_variants() -> None:
    for variant in ("transaction", "periodic_snapshot", "accumulating_snapshot"):
        data = _fact(FACT_MIXED, variant=variant)
        if variant == "periodic_snapshot":
            data["period_key"] = "order_date"
        if variant == "accumulating_snapshot":
            data["milestone_keys"] = ["customer_id"]
        assert _validate_record(PATH, data, is_registration=False) == [], variant


@pytest.mark.parametrize("build", [_dimension, _fact])
@pytest.mark.parametrize(
    "item, fragment",
    [
        ({"source": "a", "expression": "b", "alias": "c"}, "exactly one of"),
        ({"expression": "upper(x)"}, ".alias"),
        ({"source": "customer_name"}, ".alias"),
        ({"expression": "x; y", "alias": "c"}, "must not contain Jinja"),
        ({"expression": "{% set x = 1 %}", "alias": "c"}, "must not contain Jinja"),
        ({"expression": "", "alias": "c"}, "non-empty string"),
        ({"expression": "x", "alias": "c", "macro": "m"}, "unknown key"),
        (42, "must be a string or an object"),
    ],
)
def test_kimball_item_rejections(build, item, fragment: str) -> None:
    errors = _validate_record(PATH, build([item]), is_registration=False)
    assert any(fragment in error for error in errors), errors


def test_dimension_render_and_derive() -> None:
    data = _dimension(DIM_MIXED)
    sql = get_renderer("dimension", "duckdb").render(
        data, DIALECTS["duckdb"], None
    )
    assert "city," in sql
    assert "customer_name AS name" in sql
    assert "upper(email) AS email_upper" in sql
    outputs = resolve_output_columns(data)
    assert [column.name for column in outputs or []] == [
        "customer_sk",
        "customer_id",
        "city",
        "name",
        "email_upper",
        "__loaded_at",
    ]


def test_fact_render_derive_and_tests() -> None:
    data = _fact(FACT_MIXED)
    renderer = get_renderer("fact", "duckdb")
    sql = renderer.render(data, DIALECTS["duckdb"], None)
    assert "quantity," in sql
    assert "gross_amount AS gross" in sql
    assert "quantity * unit_price AS line_total" in sql
    outputs = resolve_output_columns(data)
    assert [column.name for column in outputs or []] == [
        "customer_id",
        "order_date",
        "__loaded_at",
        "quantity",
        "gross",
        "line_total",
    ]
    tests_yaml = renderer.render_tests(data, DIALECTS["duckdb"], None)
    assert tests_yaml is not None
    assert "- name: line_total" in tests_yaml
    assert "- name: gross" in tests_yaml


def test_fact_incremental_key_may_name_an_expression_measure_alias() -> None:
    data = _fact(
        FACT_MIXED,
        incremental={
            "strategy": "merge",
            "unique_key": ["customer_id", "line_total"],
        },
    )
    assert _validate_record(PATH, data, is_registration=False) == []


def test_dimension_incremental_cursor_may_name_an_expression_attribute_alias() -> None:
    data = _dimension(DIM_MIXED)
    data["incremental"] = {
        "strategy": "merge",
        "cursor_column": "email_upper",
    }
    assert _validate_record(PATH, data, is_registration=False) == []


def test_alias_keyed_column_docs_and_types_validate() -> None:
    data = _dimension(DIM_MIXED)
    data["column_docs"] = {"email_upper": "Upper-cased email."}
    data["column_types"] = {"email_upper": "text"}
    assert _validate_record(PATH, data, is_registration=False) == []


def test_kimball_existence_applies_to_source_items_only(tmp_path) -> None:
    """Rename items' sources are existence-checked against the
    input's outputs; expression items skip the check entirely."""
    from metadata_helpers import write_pattern_instance

    from loom.errors import LoomError
    from loom.inventory import validate_project

    def build_project(root, attributes: list, measures: list) -> None:
        write_pattern_instance(
            root,
            "stg_customers",
            "staging",
            {
                "description": "Customers staging",
                "owner": "analytics",
                "inline_source": {"name": "raw", "table": "customers"},
                "columns": [
                    {"source": "id", "alias": "customer_id"},
                    {"source": "email", "alias": "email"},
                    {"source": "qty", "alias": "quantity"},
                ],
            },
            pattern="staging",
        )
        write_pattern_instance(
            root,
            "dim_customer",
            "dimension",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["stg_customers"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": attributes,
            },
            pattern="dimension",
        )
        write_pattern_instance(
            root,
            "fct_orders",
            "fact",
            {
                "description": "Order facts",
                "owner": "analytics",
                "input_models": ["stg_customers"],
                "dimension_keys": ["customer_id"],
                "measure_columns": measures,
            },
            pattern="fact",
        )

    ok = tmp_path / "ok"
    ok.mkdir()
    build_project(
        ok,
        [{"expression": "upper(not_a_column)", "alias": "email_upper"}],
        [{"expression": "quantity * not_a_column", "alias": "line_total"}],
    )
    validate_project(ok)  # expression identifiers are never checked

    bad = tmp_path / "bad"
    bad.mkdir()
    build_project(
        bad,
        [{"source": "missing_col", "alias": "renamed"}],
        ["quantity"],
    )
    with pytest.raises(LoomError, match="missing_col"):
        validate_project(bad)


def test_excluded_surfaces_still_reject_mapping_items() -> None:
    scd2 = {
        "name": "dim_customers_scd2",
        "generator": "scd2_dimension",
        "description": "SCD2 customers.",
        "owner": "analytics",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
        "tracked_columns": [{"expression": "upper(email)", "alias": "email_upper"}],
    }
    errors = _validate_record(PATH, scd2, is_registration=False)
    assert any("is not of type 'string'" in error for error in errors), errors

    satellite = {
        "name": "sat_customer_details",
        "generator": "satellite",
        "description": "Customer details.",
        "owner": "analytics",
        "parent": {"model": "hub_customer", "key": "customer_id"},
        "input_models": ["stg_customers"],
        "attribute_columns": [
            {"expression": "upper(email)", "alias": "email_upper"}
        ],
        "record_source": "crm",
    }
    errors = _validate_record(PATH, satellite, is_registration=False)
    assert any("is not of type 'string'" in error for error in errors), errors
