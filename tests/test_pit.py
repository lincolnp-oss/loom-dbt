"""The pit generator (ADR 0035) - renderer shape, ghost
literal per dialect family, validation and the resolver. Runtime
behaviour is proven in tests/test_pit_runtime.py; the all-dialect
parse matrix lives in test_generated_sql_parse.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.inventory import validate_project
from loom.output_columns import resolve_output_columns
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance


PIT = {
    "name": "pit_customer",
    "parent": {"model": "hub_customer", "key": "customer_id"},
    "satellites": ["sat_customer_details", "sat_customer_tracking"],
}

LINK_PIT = {
    "name": "pit_order_customer",
    "parent": {
        "model": "link_order_customer",
        "endpoints": [
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    },
    "satellites": ["sat_order_customer_details"],
}


def _by_name(*records: dict) -> dict:
    return {record["name"].casefold(): record for record in records}


def test_pit_link_parent_uses_link_hash_column() -> None:
    renderer = get_renderer("pit", "duckdb")
    sql = renderer.render(LINK_PIT, renderer.dialect, None)
    assert "k.order_customer_hk" in sql


def test_pit_requires_satellites() -> None:
    renderer = get_renderer("pit", "duckdb")
    with pytest.raises(LoomError, match="non-empty 'satellites'"):
        renderer.render({**PIT, "satellites": []}, renderer.dialect, None)


def test_pit_tests_gate_and_shape() -> None:
    renderer = get_renderer("pit", "duckdb")
    tests = renderer.render_tests(PIT, renderer.dialect, None) or ""
    assert "- name: customer_hk" in tests
    assert "- name: __snapshot_date" in tests
    assert renderer.render_tests({**PIT, "name": "customer_pit"}, renderer.dialect, None) is None


def test_pit_resolver_outputs() -> None:
    hub = {"name": "hub_customer", "generator": "hub", "business_key": "customer_id"}
    columns = resolve_output_columns({**PIT, "generator": "pit"}, _by_name(hub))
    assert [(c.name, c.fixed_type) for c in columns] == [
        ("customer_hk", "text"),
        ("__snapshot_date", "timestamp"),
        ("__sat_customer_details_ldts", "timestamp"),
        ("__sat_customer_tracking_ldts", "timestamp"),
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


def _project(tmp_path: Path) -> Path:
    _write_staging(tmp_path, "stg_customers", ["customer_id", "customer_name"])
    write_pattern_instance(
        tmp_path, "hub_customer", "hub",
        {
            "description": "hub",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )
    write_pattern_instance(
        tmp_path, "sat_customer_details", "satellite",
        {
            "description": "details",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["customer_name"],
            "record_source": "crm",
        },
        pattern="sat",
    )
    return tmp_path


def _write_pit(project: Path, **overrides: object) -> None:
    params = {
        "description": "Customer PIT.",
        "owner": "analytics",
        "parent": {"model": "hub_customer", "key": "customer_id"},
        "satellites": ["sat_customer_details"],
    }
    params.update(overrides)
    write_pattern_instance(project, "pit_customer", "pit", params, pattern="pit")


def test_pit_project_validates(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_pit(project)
    validate_project(project)


def test_pit_prefix_is_structural(tmp_path: Path) -> None:
    project = _project(tmp_path)
    write_pattern_instance(
        project, "customer_pit", "pit",
        {
            "description": "Customer PIT.",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "satellites": ["sat_customer_details"],
        },
        pattern="pit",
    )
    with pytest.raises(LoomError, match="must start with 'pit_'"):
        validate_project(project)


def test_pit_satellite_must_share_the_parent(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_staging(project, "stg_orders", ["order_id", "customer_id"])
    write_pattern_instance(
        project, "hub_order", "hub",
        {
            "description": "hub",
            "owner": "analytics",
            "input_models": ["stg_orders"],
            "business_key": "order_id",
        },
        pattern="hub",
    )
    write_pattern_instance(
        project, "sat_order_details", "satellite",
        {
            "description": "details",
            "owner": "analytics",
            "parent": {"model": "hub_order", "key": "order_id"},
            "input_models": ["stg_orders"],
            "attribute_columns": ["customer_id"],
            "record_source": "oms",
        },
        pattern="sat",
    )
    _write_pit(project, satellites=["sat_order_details"])
    with pytest.raises(LoomError, match="has parent 'hub_order', not 'hub_customer'"):
        validate_project(project)


def test_pit_parent_block_key_must_match_the_hub(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_pit(project, parent={"model": "hub_customer", "key": "cust_id"})
    with pytest.raises(LoomError, match="must match 'hub_customer' business_key"):
        validate_project(project)


def test_pit_rejects_effectivity_satellites(tmp_path: Path) -> None:
    project = _project(tmp_path)
    write_pattern_instance(
        project, "sat_customer_effectivity", "satellite",
        {
            "description": "effectivity",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "variant": "effectivity",
            "record_source": "crm",
        },
        pattern="sat",
    )
    _write_pit(project, satellites=["sat_customer_effectivity"])
    with pytest.raises(LoomError, match="point-in-time answer is its windows view"):
        validate_project(project)
