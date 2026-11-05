"""Satellite second wave (ADR 0028) - multi_active and
effectivity variants. Renderer shapes, record validation and the
output-column resolver. Runtime behaviour is proven in
tests/test_satellite_second_wave_runtime.py; the all-dialect parse
matrix lives in test_generated_sql_parse.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.dialects import get_dialect
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.output_columns import resolve_output_columns
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance


DUCKDB = get_dialect("duckdb")

MULTI = {
    "name": "sat_customer_phones",
    "parent": {"model": "hub_customer", "key": "customer_id"},
    "input_models": ["stg_customer_phones"],
    "variant": "multi_active",
    "subsequence_key": "phone_type",
    "attribute_columns": ["phone_number"],
    "record_source": "crm",
}

EFF = {
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
}


def _render(params: dict, output: object = None) -> str:
    renderer = get_renderer("satellite", "duckdb")
    return renderer.render(params, DUCKDB, output)  # type: ignore[arg-type]


def _tests(params: dict) -> str:
    renderer = get_renderer("satellite", "duckdb")
    return renderer.render_tests(params, DUCKDB, None) or ""


# -- multi_active renderer -------------------------------------------------


def test_multi_active_current_view_partitions_by_subsequence() -> None:
    view = _render(MULTI, "current")
    assert "PARTITION BY customer_hk, phone_type" in view
    assert "    phone_type," in view


def test_multi_active_requires_subsequence_key() -> None:
    params = {k: v for k, v in MULTI.items() if k != "subsequence_key"}
    with pytest.raises(LoomError, match="requires a 'subsequence_key'"):
        _render(params)


def test_multi_active_tests_cover_hash_diff_and_subsequence() -> None:
    tests = _tests(MULTI)
    assert "- name: __hash_diff" in tests
    assert "- name: phone_type" in tests


def _by_name(*records: dict) -> dict:
    return {record["name"].casefold(): record for record in records}


def test_multi_active_resolver_outputs() -> None:
    hub = {"name": "hub_customer", "generator": "hub", "business_key": "customer_id"}
    columns = resolve_output_columns({**MULTI, "generator": "satellite"}, _by_name(hub))
    assert [c.name for c in columns] == [
        "customer_hk",
        "__hash_diff",
        "phone_type",
        "phone_number",
        "__loaded_at",
        "__record_source",
    ]


# -- effectivity renderer ----------------------------------------------------


def test_effectivity_windows_view_derives_date_windows() -> None:
    view = _render(EFF, "windows")
    assert "-- View: effectivity windows of sat_order_customer_effectivity" in view
    assert "MIN(ldts) AS __valid_from" in view
    assert "CASE WHEN ldts = island_end THEN next_ldts END" in view
    assert "CROSS JOIN calendar" in view
    # The grid's projections are aliased to their bare names: the
    # islands CTE consumes them bare, and ClickHouse retains the
    # qualifier in subquery output names when the name exists on
    # more than one joined source (Code 47 otherwise).
    assert "p.order_customer_hk AS order_customer_hk" in view
    assert "p.order_hk AS order_hk" in view
    assert "c.ldts AS ldts" in view


def test_effectivity_rejects_attributes() -> None:
    with pytest.raises(LoomError, match="takes no 'attribute_columns'"):
        _render({**EFF, "attribute_columns": ["quantity"]})


def test_effectivity_requires_a_link_parent() -> None:
    params = {
        "name": "sat_x",
        "parent": {"model": "hub_customer", "key": "customer_id"},
        "input_models": ["stg_customers"],
        "variant": "effectivity",
        "driving_key": "customer_id",
        "record_source": "crm",
    }
    with pytest.raises(LoomError, match="requires a link parent"):
        _render(params)


def test_effectivity_driving_key_must_be_an_endpoint_key() -> None:
    with pytest.raises(LoomError, match="is not one of the parent link's endpoint keys"):
        _render({**EFF, "driving_key": "product_id"})


def test_effectivity_has_no_current_view() -> None:
    with pytest.raises(LoomError, match="current-state view"):
        _render(EFF, "current")


def test_standard_has_no_windows_view() -> None:
    standard = {k: v for k, v in MULTI.items() if k not in ("variant", "subsequence_key")}
    with pytest.raises(LoomError, match="windows view"):
        _render(standard, "windows")


def test_effectivity_tests_cover_keys_without_hash_diff() -> None:
    tests = _tests(EFF)
    assert "- name: order_customer_hk" in tests
    assert "- name: order_hk" in tests
    assert "__hash_diff" not in tests


def test_effectivity_resolver_outputs() -> None:
    link = {"name": "link_order_customer", "generator": "link"}
    columns = resolve_output_columns({**EFF, "generator": "satellite"}, _by_name(link))
    assert [c.name for c in columns] == [
        "order_customer_hk",
        "order_hk",
        "__loaded_at",
        "__record_source",
    ]


# -- validation ---------------------------------------------------------------


def _write_staging(project: Path, name: str, columns: list[str]) -> None:
    write_pattern_instance(
        project,
        name,
        "staging",
        {
            "description": "staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": name},
            "columns": [{"source": c, "alias": c} for c in columns],
        },
        pattern="staging",
    )


def _write_hub(project: Path, name: str, input_model: str, key: str) -> None:
    write_pattern_instance(
        project,
        name,
        "hub",
        {
            "description": "hub",
            "owner": "analytics",
            "input_models": [input_model],
            "business_key": key,
        },
        pattern="hub",
    )


def _project_with_link(tmp_path: Path) -> Path:
    _write_staging(tmp_path, "stg_orders", ["order_id", "customer_id", "quantity"])
    _write_staging(tmp_path, "stg_customers", ["customer_id", "customer_name"])
    _write_hub(tmp_path, "hub_order", "stg_orders", "order_id")
    _write_hub(tmp_path, "hub_customer", "stg_customers", "customer_id")
    write_pattern_instance(
        tmp_path,
        "link_order_customer",
        "link",
        {
            "description": "link",
            "owner": "analytics",
            "input_models": ["stg_orders"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
        pattern="link",
    )
    return tmp_path


def _sat_params(**overrides: object) -> dict:
    params = {
        "description": "Effectivity of order-customer relationships.",
        "owner": "analytics",
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
    }
    params.update(overrides)
    return params


def test_effectivity_project_validates(tmp_path: Path) -> None:
    project = _project_with_link(tmp_path)
    write_pattern_instance(
        project, "sat_order_customer_effectivity", "satellite",
        _sat_params(), pattern="sat",
    )
    validate_project(project)


def test_effectivity_validation_requires_driving_key(tmp_path: Path) -> None:
    project = _project_with_link(tmp_path)
    params = _sat_params()
    del params["driving_key"]
    write_pattern_instance(
        project, "sat_order_customer_effectivity", "satellite", params, pattern="sat"
    )
    with pytest.raises(LoomError, match="requires a 'driving_key'"):
        validate_project(project)


def test_effectivity_validation_rejects_non_endpoint_driving_key(
    tmp_path: Path,
) -> None:
    project = _project_with_link(tmp_path)
    write_pattern_instance(
        project, "sat_order_customer_effectivity", "satellite",
        _sat_params(driving_key="quantity"), pattern="sat",
    )
    with pytest.raises(LoomError, match="must be one of the parent link's endpoint keys"):
        validate_project(project)


def test_driving_key_rejected_on_standard_variant(tmp_path: Path) -> None:
    project = _project_with_link(tmp_path)
    write_pattern_instance(
        project, "sat_order_customer_details", "satellite",
        _sat_params(variant="standard", attribute_columns=["quantity"]),
        pattern="sat",
    )
    with pytest.raises(LoomError, match="'driving_key' is only accepted"):
        validate_project(project)


def test_multi_active_project_validates(tmp_path: Path) -> None:
    _write_staging(
        tmp_path, "stg_customer_phones", ["customer_id", "phone_type", "phone_number"]
    )
    _write_staging(tmp_path, "stg_customers", ["customer_id", "customer_name"])
    _write_hub(tmp_path, "hub_customer", "stg_customers", "customer_id")
    write_pattern_instance(
        tmp_path, "sat_customer_phones", "satellite",
        {
            "description": "Customer phone numbers, several per customer.",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customer_phones"],
            "variant": "multi_active",
            "subsequence_key": "phone_type",
            "attribute_columns": ["phone_number"],
            "record_source": "crm",
        },
        pattern="sat",
    )
    validate_project(tmp_path)


def test_multi_active_validation_requires_subsequence_key(tmp_path: Path) -> None:
    _write_staging(
        tmp_path, "stg_customer_phones", ["customer_id", "phone_type", "phone_number"]
    )
    _write_staging(tmp_path, "stg_customers", ["customer_id", "customer_name"])
    _write_hub(tmp_path, "hub_customer", "stg_customers", "customer_id")
    write_pattern_instance(
        tmp_path, "sat_customer_phones", "satellite",
        {
            "description": "Customer phone numbers.",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customer_phones"],
            "variant": "multi_active",
            "attribute_columns": ["phone_number"],
            "record_source": "crm",
        },
        pattern="sat",
    )
    with pytest.raises(LoomError, match="requires a 'subsequence_key'"):
        validate_project(tmp_path)


def test_subsequence_key_must_not_repeat_an_attribute(tmp_path: Path) -> None:
    _write_staging(
        tmp_path, "stg_customer_phones", ["customer_id", "phone_type", "phone_number"]
    )
    _write_staging(tmp_path, "stg_customers", ["customer_id", "customer_name"])
    _write_hub(tmp_path, "hub_customer", "stg_customers", "customer_id")
    write_pattern_instance(
        tmp_path, "sat_customer_phones", "satellite",
        {
            "description": "Customer phone numbers.",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customer_phones"],
            "variant": "multi_active",
            "subsequence_key": "phone_number",
            "attribute_columns": ["phone_number"],
            "record_source": "crm",
        },
        pattern="sat",
    )
    with pytest.raises(LoomError, match="must not also be an attribute column"):
        validate_project(tmp_path)
