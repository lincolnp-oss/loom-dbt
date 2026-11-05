"""The expression item (ADR 0037) on the mapping-form surfaces.

One grammar on every surface: ``expression`` is a non-empty
verbatim SQL string, mutually exclusive with ``source``,
``alias`` required wherever it appears, Jinja delimiters and
statement terminators banned, and no identifier validation
(practitioner-asserted, the gold/mart measures precedent).
These tests pin the grammar per surface (staging columns, view
select_columns, gold/mart aggregation.group_by), the renderers'
emission, and the output-column derivation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.inventory_records import _validate_record
from loom.output_columns import resolve_output_columns
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance

PATH = "metadata/instances/x.yml"


def _staging(columns: list) -> dict:
    return {
        "name": "stg_payments",
        "generator": "staging",
        "description": "Payments staging.",
        "owner": "analytics",
        "inline_source": {"name": "raw", "table": "payments"},
        "columns": columns,
    }


def _gold(group_by: list) -> dict:
    return {
        "name": "gold_order_totals",
        "generator": "gold",
        "description": "Order totals.",
        "owner": "analytics",
        "input_models": ["stg_orders"],
        "aggregation": {
            "group_by": group_by,
            "measures": [
                {"expression": "COUNT(*)", "alias": "order_count"},
            ],
        },
    }


def _mart(group_by: list) -> dict:
    data = _gold(group_by)
    data["name"] = "mart_order_totals"
    data["generator"] = "mart"
    return data


# --- staging columns -------------------------------------------------


def test_staging_expression_item_validates() -> None:
    data = _staging(
        [
            {"source": "id", "alias": "payment_id"},
            {"expression": "amount / 100", "alias": "amount"},
        ]
    )
    assert _validate_record(PATH, data, is_registration=False) == []


@pytest.mark.parametrize(
    "column, fragment",
    [
        (
            {"source": "amount", "expression": "amount / 100", "alias": "amount"},
            "exactly one of 'source' or 'expression'",
        ),
        (
            {"expression": "amount / 100"},
            "'alias' is required with 'expression'",
        ),
        (
            {"expression": "  ", "alias": "amount"},
            "must be a non-empty string",
        ),
        (
            {"expression": "amount / 100; DROP TABLE x", "alias": "amount"},
            "must not contain Jinja delimiters or a statement terminator",
        ),
        (
            {"expression": "{{ amount }} / 100", "alias": "amount"},
            "must not contain Jinja delimiters or a statement terminator",
        ),
        (
            {
                "expression": "amount / 100",
                "alias": "amount",
                "macro": "cents_to_dollars",
            },
            "the macro slot requires 'source'",
        ),
    ],
)
def test_staging_expression_item_rejections(column: dict, fragment: str) -> None:
    errors = _validate_record(
        PATH, _staging([column]), is_registration=False
    )
    assert any(fragment in error for error in errors), errors


def test_staging_expression_renders_verbatim() -> None:
    renderer = get_renderer("staging", "duckdb")
    sql = renderer.render(
        _staging(
            [
                {"source": "id", "alias": "payment_id"},
                {"expression": "amount / 100", "alias": "amount"},
            ]
        ),
        DIALECTS["duckdb"],
        None,
    )
    assert "amount / 100 AS amount" in sql
    assert "id AS payment_id" in sql


def test_staging_expression_output_columns_use_the_alias() -> None:
    outputs = resolve_output_columns(
        _staging(
            [
                {"source": "id", "alias": "payment_id"},
                {"expression": "amount / 100", "alias": "amount"},
            ]
        )
    )
    assert [column.name for column in outputs or []] == [
        "payment_id",
        "amount",
        "__loaded_at",
    ]


def test_staging_expression_outputs_are_not_given_inputs() -> None:
    # Declared unit tests: the scaffold's given row keys the
    # model's SOURCE columns. An expression item has no source
    # column, so it contributes nothing to the given row (its
    # output is asserted on the expect side instead).
    from loom.declared_unit_tests import scaffold_snippet

    snippet = scaffold_snippet(
        _staging(
            [
                {"source": "id", "alias": "payment_id"},
                {"source": "amount", "alias": "amount_cents"},
                {"expression": "amount / 100", "alias": "amount"},
            ]
        )
    )
    assert '- {id: "__TODO__", amount: "__TODO__"}' in snippet
    assert "amount / 100" not in snippet
    # The expression's output is an expect-side column.
    assert 'amount: "__TODO__"' in snippet


# --- view select_columns ----------------------------------------------


def _view_project(project: Path, select_columns: list) -> None:
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customers staging.",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="raw_staging",
    )
    write_pattern_instance(
        project,
        "vw_customer_names",
        "view",
        {
            "description": "Customer names view.",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "select_columns": select_columns,
        },
        pattern="reporting_views",
    )


def test_view_expression_item_validates_and_renders(tmp_path: Path) -> None:
    _view_project(
        tmp_path,
        [
            {"source": "stg_customers.customer_id", "alias": "customer_id"},
            {
                "expression": "upper(stg_customers.customer_name)",
                "alias": "customer_name_upper",
            },
        ],
    )
    entries = validate_project(tmp_path)
    view = next(e for e in entries if e.data["name"] == "vw_customer_names")
    sql = get_renderer("view", "duckdb").render(view.data, DIALECTS["duckdb"], None)
    assert "upper(stg_customers.customer_name) AS customer_name_upper" in sql
    outputs = resolve_output_columns(view.data)
    assert [column.name for column in outputs or []] == [
        "customer_id",
        "customer_name_upper",
    ]


def test_view_expression_and_source_together_are_rejected(
    tmp_path: Path,
) -> None:
    _view_project(
        tmp_path,
        [
            {
                "source": "stg_customers.customer_id",
                "expression": "1 + 1",
                "alias": "customer_id",
            }
        ],
    )
    with pytest.raises(LoomError, match="exactly one of 'source' or 'expression'"):
        validate_project(tmp_path)


# --- gold / mart group_by ----------------------------------------------


@pytest.mark.parametrize("build", [_gold, _mart])
def test_group_by_expression_item_validates(build) -> None:
    data = build(
        [
            "customer_id",
            {"expression": "date_trunc('month', ordered_at)", "alias": "order_month"},
        ]
    )
    assert _validate_record(PATH, data, is_registration=False) == []


@pytest.mark.parametrize("build", [_gold, _mart])
@pytest.mark.parametrize(
    "item, fragment",
    [
        ({"expression": "date_trunc('month', ordered_at)"}, ".alias"),
        ({"expression": "x; y", "alias": "m"}, "must not contain Jinja"),
        ({"alias": "m"}, "must be a non-empty string"),
        ({"expression": "x", "alias": "m", "extra": 1}, "unknown key"),
        (42, "must be a string or an object"),
    ],
)
def test_group_by_expression_item_rejections(build, item, fragment: str) -> None:
    errors = _validate_record(
        PATH, build([item]), is_registration=False
    )
    assert any(fragment in error for error in errors), errors


def test_gold_group_by_expression_renders_in_select_and_group_by() -> None:
    data = _gold(
        [
            "customer_id",
            {"expression": "date_trunc('month', ordered_at)", "alias": "order_month"},
        ]
    )
    sql = get_renderer("gold", "duckdb").render(data, DIALECTS["duckdb"], None)
    assert "date_trunc('month', ordered_at) AS order_month" in sql
    assert "GROUP BY customer_id, date_trunc('month', ordered_at)" in sql
    outputs = resolve_output_columns(data)
    assert [column.name for column in outputs or []] == [
        "customer_id",
        "order_month",
        "order_count",
        "__loaded_at",
    ]


def test_mart_group_by_expression_renders_in_select_and_group_by() -> None:
    data = _mart(
        [{"expression": "date_trunc('day', ordered_at)", "alias": "order_day"}]
    )
    sql = get_renderer("mart", "duckdb").render(data, DIALECTS["duckdb"], None)
    assert "date_trunc('day', ordered_at) AS order_day" in sql
    assert "GROUP BY date_trunc('day', ordered_at)" in sql
    outputs = resolve_output_columns(data)
    assert [column.name for column in outputs or []] == [
        "order_day",
        "order_count",
        "__loaded_at",
    ]
