"""The `filter` parameter (ADR 0038) on its six generators.

One grammar on every surface that admits it: a non-empty
verbatim SQL predicate string, carried into the generated
model's WHERE clause exactly as written - never evaluated,
never identifier-checked (practitioner-asserted, the
expression-item posture of ADR 0037) - with Jinja delimiters
and statement terminators banned. These tests pin the grammar
(contract admission per generator, content rejections, the
unknown-parameter fence on the twelve generators that do not
admit it) and the render shapes the goldens do not cover
(fact append composition, staging's source_ref and star forms,
silver pass-through, the SCD2 history view's immunity).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.contracts import filter_content_errors, validate_against_contract
from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.inventory_records import _validate_record
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance

PATH = "metadata/instances/x.yml"
DUCKDB = DIALECTS["duckdb"]

_ADMITTING = (
    "staging",
    "silver",
    "dimension",
    "fact",
    "scd2_dimension",
    "normalized_table",
)
_FENCED = (
    "bronze",
    "gold",
    "mart",
    "obt",
    "view",
    "hub",
    "link",
    "satellite",
    "pit",
    "bridge",
    "reference_hub",
    "reference_table",
)


def _record(generator: str, **extra: object) -> dict:
    """A minimal record that validates clean for `generator`."""
    base: dict = {"name": "x_model", "generator": generator}
    if generator == "staging":
        base.update(
            description="Staging.",
            owner="analytics",
            inline_source={"name": "raw", "table": "x"},
            columns=[{"source": "id", "alias": "id"}],
        )
    elif generator == "silver":
        base.update(input_models=["bronze_x"], dedup_key="id")
    elif generator == "dimension":
        base.update(
            input_models=["stg_x"],
            natural_key="id",
            surrogate_key="x_sk",
            attribute_columns=["label"],
        )
    elif generator == "fact":
        base.update(
            input_models=["stg_x"],
            dimension_keys=["id"],
            measure_columns=["amount"],
        )
    elif generator == "scd2_dimension":
        base.update(
            description="SCD2.",
            owner="analytics",
            input_models=["stg_x"],
            business_key="id",
            tracked_columns=["label"],
        )
    elif generator == "normalized_table":
        base.update(
            input_models=["stg_x"],
            entity_columns=["id"],
            attribute_columns=["label"],
        )
    base.update(extra)
    return base


# --- Contract admission and the fence ---------------------------------


@pytest.mark.parametrize("generator", _ADMITTING)
def test_filter_is_admitted_and_validates(generator: str) -> None:
    data = _record(generator, filter="status = 'active'")
    assert _validate_record(PATH, data, is_registration=False) == []


@pytest.mark.parametrize("generator", _FENCED)
def test_filter_is_an_unknown_parameter_elsewhere(generator: str) -> None:
    data = {"name": "x_model", "generator": generator, "filter": "a = 1"}
    errors = validate_against_contract(PATH, data)
    assert any(
        f"unknown parameter 'filter' for generator {generator!r}" in error
        for error in errors
    ), errors


# --- Content grammar ----------------------------------------------------


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("", "filter must be a non-empty string"),
        ("   ", "filter must be a non-empty string"),
        (42, "filter must be a non-empty string"),
        (["status = 'active'"], "filter must be a non-empty string"),
        ({"predicate": "status = 'active'"}, "filter must be a non-empty string"),
        (
            "{{ config(materialized='table') }}",
            "filter must not contain Jinja delimiters or a statement terminator",
        ),
        (
            "{% if is_incremental() %} a = 1 {% endif %}",
            "filter must not contain Jinja delimiters or a statement terminator",
        ),
        (
            "status = 'active'; DROP TABLE x",
            "filter must not contain Jinja delimiters or a statement terminator",
        ),
        (
            "a = 1; b = 2",
            "filter must not contain Jinja delimiters or a statement terminator",
        ),
    ],
)
def test_filter_content_rejections(value: object, fragment: str) -> None:
    errors = filter_content_errors(PATH, value)
    assert any(fragment in error for error in errors), errors


def test_filter_content_accepts_a_plain_predicate() -> None:
    assert filter_content_errors(PATH, "status = 'active' AND qty > 0") == []
    # A semicolon inside a string literal is still a terminator
    # to the grammar: the check is textual, like the expression
    # item's - the ADR states the bound rather than parsing SQL.
    assert filter_content_errors(PATH, "note = 'a;b'") != []


@pytest.mark.parametrize("generator", _ADMITTING)
@pytest.mark.parametrize(
    "value, fragment",
    [
        ("", "filter must be a non-empty string"),
        (42, "filter must be a non-empty string"),
        (
            "a = 1; DROP TABLE x",
            "filter must not contain Jinja delimiters or a statement terminator",
        ),
        (
            "{{ a }}",
            "filter must not contain Jinja delimiters or a statement terminator",
        ),
    ],
)
def test_filter_rejections_reach_record_validation(
    generator: str, value: object, fragment: str
) -> None:
    errors = _validate_record(
        PATH, _record(generator, filter=value), is_registration=False
    )
    assert any(fragment in error for error in errors), errors


def test_filter_rejection_names_the_accepted_form() -> None:
    # The error names the accepted form, in the codebase's
    # enumerating style: a non-empty verbatim predicate string.
    errors = filter_content_errors(PATH, "")
    assert "e.g. filter:" in errors[0]


def test_project_validation_rejects_a_jinja_filter(tmp_path: Path) -> None:
    params = {
        "description": "Orders staging.",
        "owner": "analytics",
        "inline_source": {"name": "raw", "table": "orders"},
        "columns": [{"source": "id", "alias": "id"}],
        "filter": "{{ ref('x') }} IS NOT NULL",
    }
    write_pattern_instance(
        tmp_path, "stg_orders", "staging", params, pattern="raw_staging"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "filter must not contain Jinja delimiters" in str(excinfo.value)


# --- Render shapes beyond the goldens -----------------------------------


def _render(generator: str, params: dict, output_path: object = None) -> str:
    return get_renderer(generator, "duckdb").render(params, DUCKDB, output_path)  # type: ignore[arg-type]


def test_fact_append_composes_filter_first_then_strict_cursor() -> None:
    sql = _render(
        "fact",
        {
            "name": "fact_orders",
            "input_models": ["stg_orders"],
            "dimension_keys": ["customer_id"],
            "measure_columns": ["quantity"],
            "filter": "quantity > 0",
            "incremental": {"strategy": "append", "cursor_column": "ordered_at"},
        },
    )
    assert "WHERE quantity > 0\n" in sql
    assert "  AND ordered_at > (SELECT MAX(ordered_at) FROM {{ this }})" in sql


def test_staging_source_ref_form_carries_the_filter() -> None:
    sql = _render(
        "staging",
        {
            "name": "stg_customers",
            "source_ref": {"name": "ecom", "table": "raw_customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
            "filter": "is_active = 1",
        },
    )
    assert sql.endswith("FROM {{ source('ecom', 'raw_customers') }}\nWHERE is_active = 1\n")


def test_staging_star_projection_still_filters_the_scan() -> None:
    sql = _render(
        "staging",
        {
            "name": "stg_customers",
            "inline_source": {"name": "raw", "table": "customers"},
            "filter": "is_active = 1",
        },
    )
    assert "SELECT *," in sql
    assert sql.endswith("WHERE is_active = 1\n")


def test_silver_pass_through_filters_the_scan() -> None:
    # No dedup key: the renderer passes the input through, and
    # the filter still restricts that scan.
    sql = _render(
        "silver",
        {
            "name": "silver_customers",
            "input_models": ["bronze_customers"],
            "filter": "status = 'active'",
        },
    )
    assert sql.endswith("FROM {{ ref('bronze_customers') }}\nWHERE status = 'active'\n")


def test_dimension_filter_without_incremental_is_a_plain_where() -> None:
    sql = _render(
        "dimension",
        _record("dimension", filter="customer_id IS NOT NULL"),
    )
    assert sql.endswith("\nWHERE customer_id IS NOT NULL\n")
    assert "is_incremental" not in sql


def test_scd2_history_view_ignores_the_filter() -> None:
    # The filter restricts the snapshot's input; the dimension
    # view exposes the recorded history and must render exactly
    # as without one.
    params = _record("scd2_dimension", filter="is_active = 1")
    view_with = _render("scd2_dimension", params, output_path=Path("model"))
    view_without = _render(
        "scd2_dimension", _record("scd2_dimension"), output_path=Path("model")
    )
    assert view_with == view_without
    assert "is_active" not in view_with


def test_scd2_snapshot_carries_the_filter_on_its_input_select() -> None:
    sql = _render("scd2_dimension", _record("scd2_dimension", filter="is_active = 1"))
    assert "SELECT * FROM {{ ref('stg_x') }} WHERE is_active = 1" in sql
