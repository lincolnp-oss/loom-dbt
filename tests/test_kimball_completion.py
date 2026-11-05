"""Kimball completion (ADR 0031) - fact flavours and SCD 3/6.

Renderer shapes, record validation and the output-column resolver
for the `fact` variants (transaction / periodic_snapshot /
accumulating_snapshot) and the `scd2_dimension` variants (type_2 /
type_3 / type_6). Runtime proof lives in
tests/test_kimball_completion_runtime.py.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from loom.dialects import DIALECTS, get_dialect
from render_execution import compile_sql
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.output_columns import resolve_output_columns
from loom.renderers import SCD2Renderer, get_renderer
from metadata_helpers import write_pattern_instance


DUCKDB = get_dialect("duckdb")

FACT = {
    "name": "fact_orders",
    "input_models": ["stg_orders"],
    "dimension_keys": ["customer_key", "period_key", "order_date_key", "shipped_date_key"],
    "measure_columns": ["quantity"],
}

SCD2 = {
    "name": "dim_customers",
    "input_models": ["stg_customers"],
    "business_key": "customer_id",
    "tracked_columns": ["customer_name", "customer_email"],
}


def _fact(**overrides):
    params = {**FACT, **overrides}
    renderer = get_renderer("fact", "duckdb")
    return renderer


def _render_fact(**overrides) -> str:
    params = {**FACT, **overrides}
    return get_renderer("fact", "duckdb").render(params, DUCKDB, None)


def _fact_tests(**overrides) -> str:
    params = {**FACT, **overrides}
    return get_renderer("fact", "duckdb").render_tests(params, DUCKDB, None) or ""


def _render_scd2_view(**overrides) -> str:
    params = {**SCD2, **overrides}
    return get_renderer("scd2_dimension", "duckdb").render(
        params, DUCKDB, Path("models/dim_customers.sql")
    )


def _scd2_tests(**overrides) -> str:
    params = {**SCD2, **overrides}
    return (
        get_renderer("scd2_dimension", "duckdb").render_tests(params, DUCKDB, None)
        or ""
    )


# -- fact flavours -----------------------------------------------------


def test_accumulating_fact_tests_exempt_milestone_keys_from_not_null() -> None:
    tests = _fact_tests(
        variant="accumulating_snapshot",
        milestone_keys=["order_date_key", "shipped_date_key"],
    )
    assert "- name: order_date_key" not in tests
    assert "- name: shipped_date_key" not in tests
    # Non-milestone keys and measures keep their not_null tests.
    assert "- name: customer_key" in tests
    assert "- name: quantity" in tests
    assert tests.count("not_null") == 3  # customer_key, period_key, quantity


def test_transaction_fact_tests_keep_not_null_on_every_key() -> None:
    tests = _fact_tests()
    assert tests.count("not_null") == len(FACT["dimension_keys"]) + 1


def test_fact_renderer_rejects_unknown_variant() -> None:
    with pytest.raises(LoomError, match="fact variant 'weekly' is not supported"):
        _render_fact(variant="weekly")


# -- SCD variants --------------------------------------------------------


def test_type3_tests_unique_is_unconditional() -> None:
    tests = _scd2_tests(variant="type_3", previous_value_columns=["customer_name"])
    assert "- unique\n" in tests
    assert "where: __valid_to IS NULL" not in tests


def test_type6_tests_keep_current_row_uniqueness() -> None:
    tests = _scd2_tests(variant="type_6", previous_value_columns=["customer_name"])
    assert "where: __valid_to IS NULL" in tests


def test_scd2_renderer_rejects_unknown_variant() -> None:
    with pytest.raises(LoomError, match="variant 'type_9' is not supported"):
        _render_scd2_view(variant="type_9")


def test_scd2_renderer_requires_previous_columns_for_type3() -> None:
    with pytest.raises(LoomError, match="requires a non-empty"):
        _render_scd2_view(variant="type_3")


# -- resolver ------------------------------------------------------------


def test_resolver_type3_outputs() -> None:
    columns = resolve_output_columns(
        {**SCD2, "generator": "scd2_dimension", "variant": "type_3",
         "previous_value_columns": ["customer_name"]}
    )
    assert [c.name for c in columns] == [
        "customer_id",
        "customer_name",
        "customer_email",
        "previous_customer_name",
        "__valid_from",
        "__is_deleted",
    ]


def test_resolver_type6_outputs() -> None:
    columns = resolve_output_columns(
        {**SCD2, "generator": "scd2_dimension", "variant": "type_6",
         "previous_value_columns": ["customer_name"]}
    )
    assert [c.name for c in columns] == [
        "customer_id",
        "customer_name",
        "customer_email",
        "current_customer_name",
        "current_customer_email",
        "previous_customer_name",
        "__valid_from",
        "__valid_to",
        "__is_deleted",
    ]


# -- validation ------------------------------------------------------------

_PATTERNS = {
    "staging.yml": (
        "name: staging\n"
        "generator: staging\n"
        "required_params: [description, source_ref, columns]\n"
    ),
    "fact_pattern.yml": (
        "name: fact_pattern\n"
        "generator: fact\n"
        "required_params: [description, input_models, dimension_keys, measure_columns]\n"
    ),
    "scd2_pattern.yml": (
        "name: scd2_pattern\n"
        "generator: scd2_dimension\n"
        "required_params: [description, input_models, business_key, tracked_columns]\n"
    ),
}


def _project(tmp_path: Path, instances: dict[str, str]) -> Path:
    patterns = tmp_path / "metadata" / "patterns"
    patterns.mkdir(parents=True)
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: orders\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    for filename, body in _PATTERNS.items():
        (patterns / filename).write_text(
            f"defaults: {{owner: test}}\n{body}",
            encoding="utf-8",
        )
    instance_dir = tmp_path / "metadata" / "instances"
    instance_dir.mkdir(parents=True)
    (instance_dir / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_orders\n"
        "    params:\n"
        "      description: Orders staging.\n"
        "      source_ref: {name: raw, table: orders}\n"
        "      columns:\n"
        "        - {source: customer_id, alias: customer_key}\n"
        "        - {source: period, alias: period_key}\n"
        "        - {source: ordered_on, alias: order_date_key}\n"
        "        - {source: shipped_on, alias: shipped_date_key}\n"
        "        - {source: qty, alias: quantity}\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customers staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n"
        "        - {source: email, alias: customer_email}\n",
        encoding="utf-8",
    )
    for filename, body in instances.items():
        (instance_dir / filename).write_text(body, encoding="utf-8")
    return tmp_path


def _fact_instance(**params) -> str:
    lines = [
        "pattern: fact_pattern",
        "instances:",
        "  - name: fact_orders",
        "    params:",
        "      description: Orders fact.",
        "      input_models: [stg_orders]",
        "      dimension_keys: [customer_key, period_key, order_date_key, shipped_date_key]",
        "      measure_columns: [quantity]",
    ]
    for key, value in params.items():
        lines.append(f"      {key}: {value}")
    return "\n".join(lines) + "\n"


def _scd2_instance(**params) -> str:
    tracked = params.pop("tracked_columns", "[customer_name, customer_email]")
    lines = [
        "pattern: scd2_pattern",
        "instances:",
        "  - name: dim_customers",
        "    params:",
        "      description: Customers dimension.",
        "      input_models: [stg_customers]",
        "      business_key: customer_id",
        f"      tracked_columns: {tracked}",
    ]
    for key, value in params.items():
        lines.append(f"      {key}: {value}")
    return "\n".join(lines) + "\n"


def test_periodic_fact_validates(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {"facts.yml": _fact_instance(variant="periodic_snapshot", period_key="period_key")},
    )
    validate_project(project)


def test_periodic_fact_requires_period_key(tmp_path: Path) -> None:
    project = _project(
        tmp_path, {"facts.yml": _fact_instance(variant="periodic_snapshot")}
    )
    with pytest.raises(LoomError, match="requires a 'period_key'"):
        validate_project(project)


def test_periodic_fact_period_key_must_be_a_dimension_key(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {"facts.yml": _fact_instance(variant="periodic_snapshot", period_key="quantity")},
    )
    with pytest.raises(LoomError, match="must be one of the fact's dimension_keys"):
        validate_project(project)


def test_period_key_rejected_on_transaction_fact(tmp_path: Path) -> None:
    project = _project(tmp_path, {"facts.yml": _fact_instance(period_key="period_key")})
    with pytest.raises(LoomError, match="only accepted for the periodic_snapshot"):
        validate_project(project)


def test_accumulating_fact_requires_milestone_keys(tmp_path: Path) -> None:
    project = _project(
        tmp_path, {"facts.yml": _fact_instance(variant="accumulating_snapshot")}
    )
    with pytest.raises(LoomError, match="requires a non-empty 'milestone_keys' list"):
        validate_project(project)


def test_accumulating_fact_milestone_keys_must_be_dimension_keys(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        {
            "facts.yml": _fact_instance(
                variant="accumulating_snapshot", milestone_keys="[delivered_date_key]"
            )
        },
    )
    with pytest.raises(LoomError, match="must be drawn from the fact's dimension_keys"):
        validate_project(project)


def test_fact_variant_enum_is_closed(tmp_path: Path) -> None:
    project = _project(tmp_path, {"facts.yml": _fact_instance(variant="weekly")})
    with pytest.raises(LoomError, match="'variant' must be one of"):
        validate_project(project)


def test_type3_dimension_validates(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {
            "dims.yml": _scd2_instance(
                variant="type_3", previous_value_columns="[customer_name]"
            )
        },
    )
    validate_project(project)


def test_type3_requires_previous_value_columns(tmp_path: Path) -> None:
    project = _project(tmp_path, {"dims.yml": _scd2_instance(variant="type_3")})
    with pytest.raises(
        LoomError, match="requires a non-empty 'previous_value_columns' list"
    ):
        validate_project(project)


def test_previous_value_columns_rejected_on_type2(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {"dims.yml": _scd2_instance(previous_value_columns="[customer_name]")},
    )
    with pytest.raises(LoomError, match="only accepted for the type_3 and type_6"):
        validate_project(project)


def test_previous_value_columns_must_be_tracked(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {
            "dims.yml": _scd2_instance(
                variant="type_6", previous_value_columns="[customer_city]"
            )
        },
    )
    with pytest.raises(LoomError, match="must be drawn from tracked_columns"):
        validate_project(project)


def test_previous_value_columns_must_not_include_business_key(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        {
            "dims.yml": _scd2_instance(
                variant="type_3",
                tracked_columns="[customer_id, customer_name]",
                previous_value_columns="[customer_id]",
            )
        },
    )
    with pytest.raises(LoomError, match="must not include the business_key"):
        validate_project(project)


def test_generated_previous_column_collision_is_rejected(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {
            "dims.yml": _scd2_instance(
                variant="type_3",
                tracked_columns="[customer_name, previous_customer_name]",
                previous_value_columns="[customer_name]",
            )
        },
    )
    with pytest.raises(LoomError, match="collide with declared SCD2 columns"):
        validate_project(project)


def test_scd2_variant_enum_is_closed(tmp_path: Path) -> None:
    project = _project(tmp_path, {"dims.yml": _scd2_instance(variant="type_9")})
    with pytest.raises(LoomError, match="'variant' must be one of"):
        validate_project(project)


# -- Generated-SQL execution -----------------------------------------
# The SCD2 view is EXECUTES_EXEMPT in the conformance harness
# (snapshot machinery), so this is the only default-suite execution
# of the view; the keep-list SCD2 test is integration-deselected.


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def test_dimension_view_exposes_history_with_boolean_is_deleted(
    conn,
) -> None:
    # Stub the snapshot relation the way dbt-duckdb materializes
    # it: dbt_is_deleted is VARCHAR ('False'/'True'), not boolean
    # (the root cause). The view must still expose closed
    # rows and tombstones, with real booleans.
    conn.execute(
        "CREATE TABLE dim_customers_scd2_history ("
        "customer_id INTEGER, customer_name VARCHAR, "
        "dbt_valid_from TIMESTAMP, dbt_valid_to TIMESTAMP, "
        "dbt_is_deleted VARCHAR)"
    )
    conn.execute(
        "INSERT INTO dim_customers_scd2_history VALUES "
        "(7, 'Ada', TIMESTAMP '2026-01-01', TIMESTAMP '2026-02-01', 'False'), "
        "(7, 'Adelaide', TIMESTAMP '2026-02-01', TIMESTAMP '2026-03-01', 'False'), "
        "(7, 'Adelaide', TIMESTAMP '2026-03-01', NULL, 'True')"
    )
    renderer = get_renderer("scd2_dimension", "duckdb")
    view_sql = renderer.render(
        {
            "name": "dim_customers_scd2",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name"],
        },
        get_dialect("duckdb"),
        Path("models/dim_customers_scd2.sql"),
    )
    rows = conn.execute(
        f"SELECT customer_name, __is_deleted, __valid_to IS NULL "
        f"FROM ({compile_sql(view_sql)}) ORDER BY __valid_from"
    ).fetchall()
    assert rows == [
        ("Ada", False, False),
        ("Adelaide", False, False),
        ("Adelaide", True, True),
    ]


# -- SCD2 snapshot-name branch
# A dimension named stg_* derives its snapshot name by stripping
# the prefix (stg_customers -> customers_history). No golden uses
# a stg_*-named dimension, so deleting these two render tests
# dropped both derivation branches' coverage.


def test_scd2_snapshot_name_strips_stg_prefix() -> None:
    renderer = get_renderer("scd2_dimension", "duckdb")
    sql = renderer.render(
        {
            "name": "stg_customers",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name"],
        },
        get_dialect("duckdb"),
        None,
    )
    assert "customers_history" in sql
    assert "strategy='check'" in sql
    assert "unique_key='customer_id'" in sql


def test_scd2_view_name_strips_stg_prefix() -> None:
    renderer = get_renderer("scd2_dimension", "duckdb")
    sql = renderer.render(
        {
            "name": "stg_customers",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name"],
        },
        get_dialect("duckdb"),
        Path("models/dim_customers.sql"),
    )
    # History semantics: the dimension exposes open and
    # closed rows, so there is no current-row filter, and
    # is_deleted is normalized to a boolean rather than selected
    # raw (dbt-duckdb materializes it as VARCHAR).
    assert "WHERE" not in sql
    assert "CAST(dbt_is_deleted AS BOOLEAN) AS __is_deleted" in sql
    assert "customers_history" in sql



class TestStep2Scd2DegenerateGuards:
        def test_scd2_empty_tracked_columns_rejected(self, tmp_path: Path) -> None:
            _register(tmp_path, "stg_customers", "staging")
            write_pattern_instance(
                tmp_path,
                "dim_customers_scd2",
                "scd2_dimension",
                {
                    "description": "SCD2",
                    "owner": "analytics",
                    "input_models": ["stg_customers"],
                    "business_key": "customer_id",
                    "tracked_columns": [],
                },
                pattern="scd2_dimension",
            )
            message = _validation_error(tmp_path)
            assert "'tracked_columns' must be a non-empty list" in message

        def test_scd2_view_does_not_repeat_business_key(self) -> None:
            renderer = SCD2Renderer(DIALECTS["duckdb"])
            sql = renderer.render(
                {
                    "name": "dim_customers",
                    "input_models": ["stg_customers"],
                    "business_key": "customer_id",
                    "tracked_columns": [],
                },
                renderer.dialect,
                Path("dim_customers.sql"),
            )
            assert sql.count("customer_id,") == 1


# Validation helpers.

def _register(project: Path, name: str, classification: str = "external") -> None:
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True, exist_ok=True)
    (registrations / f"{name}.yml").write_text(
        f"name: {name}\n"
        f"classification: {classification}\n"
        "description: Registered input.\n"
        "owner: analytics\n",
        encoding="utf-8",
    )

def _validation_error(project: Path) -> str:
    with pytest.raises(LoomError) as error:
        validate_project(project)
    return str(error.value)
