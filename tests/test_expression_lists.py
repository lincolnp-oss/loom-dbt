"""The name-list expression surfaces (ADR 0037, PR E2).

Silver and obt `columns` items may be a string (a plain input
column, meaning unchanged), a ``{source, alias}`` mapping (a
rename), or an ``{expression, alias}`` mapping (the uniform
expression grammar). There is no macro slot on these surfaces.
These tests pin the item grammar per generator, the renderers'
emission, and the output-column derivation.
"""
from __future__ import annotations

import pytest

from loom.dialects import DIALECTS
from loom.inventory_records import _validate_record
from loom.output_columns import resolve_output_columns
from loom.renderers import get_renderer

PATH = "metadata/instances/x.yml"


def _silver(columns: list) -> dict:
    return {
        "name": "silver_customers",
        "generator": "silver",
        "description": "Cleaned customers.",
        "owner": "analytics",
        "input_models": ["bronze_customers"],
        "dedup_key": "id",
        "columns": columns,
    }


def _obt(columns: list) -> dict:
    return {
        "name": "obt_order_lines",
        "generator": "obt",
        "description": "Order lines wide table.",
        "owner": "analytics",
        "input_models": ["stg_order_items"],
        "columns": columns,
        "joins": [
            {
                "model": "hub_customer",
                "on": ["customer_id"],
                "columns": ["customer_hk"],
            }
        ],
    }


MIXED = [
    "id",
    {"source": "customer_name", "alias": "name"},
    {"expression": "upper(customer_email)", "alias": "email_upper"},
]


@pytest.mark.parametrize("build", [_silver, _obt])
def test_name_list_items_validate(build) -> None:
    assert _validate_record(PATH, build(MIXED), is_registration=False) == []
    assert _validate_record(PATH, build(["id", "customer_name"]), is_registration=False) == []


@pytest.mark.parametrize("build", [_silver, _obt])
@pytest.mark.parametrize(
    "item, fragment",
    [
        ({"source": "a", "expression": "b", "alias": "c"}, "exactly one of"),
        ({"alias": "c"}, "exactly one of"),
        ({"expression": "upper(x)"}, ".alias"),
        ({"source": "customer_name"}, ".alias"),
        ({"expression": "x; y", "alias": "c"}, "must not contain Jinja"),
        ({"expression": "{{ x }}", "alias": "c"}, "must not contain Jinja"),
        ({"source": "a", "alias": "c", "macro": "m"}, "unknown key"),
        (42, "must be a string or an object"),
    ],
)
def test_name_list_item_rejections(build, item, fragment: str) -> None:
    errors = _validate_record(PATH, build([item]), is_registration=False)
    assert any(fragment in error for error in errors), errors


def test_silver_mixed_columns_render_and_derive() -> None:
    data = _silver(MIXED)
    sql = get_renderer("silver", "duckdb").render(data, DIALECTS["duckdb"], None)
    assert "id, customer_name AS name, upper(customer_email) AS email_upper" in sql
    assert sql.rstrip().endswith("WHERE rn = 1")
    outputs = resolve_output_columns(data)
    assert [column.name for column in outputs or []] == [
        "id",
        "name",
        "email_upper",
        "__loaded_at",
    ]


def test_silver_string_columns_render_unchanged() -> None:
    data = _silver(["id", "customer_name"])
    sql = get_renderer("silver", "duckdb").render(data, DIALECTS["duckdb"], None)
    assert "SELECT id, customer_name, __loaded_at" in sql


def test_obt_mixed_columns_render_and_derive() -> None:
    data = _obt(MIXED)
    sql = get_renderer("obt", "duckdb").render(data, DIALECTS["duckdb"], None)
    assert "t.id AS id" in sql
    assert "t.customer_name AS name" in sql
    assert "upper(customer_email) AS email_upper" in sql
    assert "j0.customer_hk AS customer_hk" in sql
    outputs = resolve_output_columns(data)
    assert [column.name for column in outputs or []] == [
        "id",
        "name",
        "email_upper",
        "customer_hk",
        "__loaded_at",
    ]
