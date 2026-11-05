"""The bridge generator (ADR 0036) - renderer shape,
path-traversal validation and the resolver. Runtime behaviour is
proven in tests/test_bridge_runtime.py; the all-dialect parse
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

BRIDGE = {
    "name": "bridge_customer_orders",
    "parent": {"model": "hub_customer"},
    "path": [{"link": "link_order_customer", "hub": "hub_order"}],
}


def test_bridge_shape() -> None:
    renderer = get_renderer("bridge", "duckdb")
    sql = renderer.render(BRIDGE, DUCKDB, None)
    assert "-- Generator: bridge" in sql
    assert "materialized='table'" in sql
    assert "a.customer_hk" in sql
    assert "h1.order_hk" in sql
    # Projections are aliased to their bare names: ClickHouse
    # retains the qualifier in output names when the name exists
    # on more than one joined source (Code 47 otherwise).
    assert "a.customer_hk AS customer_hk" in sql
    assert "h1.order_hk AS order_hk" in sql
    assert "ON l1.customer_hk = a.customer_hk" in sql
    assert "ON h1.order_hk = l1.order_hk" in sql
    assert "__weighting_factor" not in sql


def test_bridge_weighting_adds_equal_factor() -> None:
    renderer = get_renderer("bridge", "duckdb")
    sql = renderer.render({**BRIDGE, "weighting": "equal"}, DUCKDB, None)
    assert "1.0 / COUNT(*) OVER (PARTITION BY a.customer_hk) AS __weighting_factor" in sql
    tests = renderer.render_tests({**BRIDGE, "weighting": "equal"}, DUCKDB, None) or ""
    assert "- name: __weighting_factor" in tests


def test_bridge_rejects_unknown_weighting() -> None:
    renderer = get_renderer("bridge", "duckdb")
    with pytest.raises(LoomError, match="weighting 'custom' is not supported"):
        renderer.render({**BRIDGE, "weighting": "custom"}, DUCKDB, None)


def test_bridge_resolver_outputs() -> None:
    columns = resolve_output_columns(
        {**BRIDGE, "generator": "bridge", "weighting": "equal"}
    )
    assert [c.name for c in columns] == [
        "customer_hk",
        "order_hk",
        "__weighting_factor",
        "__loaded_at",
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


def _project(tmp_path: Path) -> Path:
    _write_staging(tmp_path, "stg_orders", ["order_id", "customer_id"])
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


def _write_bridge(project: Path, **overrides: object) -> None:
    params = {
        "description": "Customer to orders bridge.",
        "owner": "analytics",
        "parent": {"model": "hub_customer"},
        "path": [{"link": "link_order_customer", "hub": "hub_order"}],
    }
    params.update(overrides)
    write_pattern_instance(project, "bridge_customer_orders", "bridge", params, pattern="bridge")


def test_bridge_project_validates(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_bridge(project)
    validate_project(project)


def test_bridge_prefix_is_structural(tmp_path: Path) -> None:
    project = _project(tmp_path)
    write_pattern_instance(
        project, "customer_orders_bridge", "bridge",
        {
            "description": "bridge",
            "owner": "analytics",
            "parent": {"model": "hub_customer"},
            "path": [{"link": "link_order_customer", "hub": "hub_order"}],
        },
        pattern="bridge",
    )
    with pytest.raises(LoomError, match="must start with 'bridge_'"):
        validate_project(project)


def test_bridge_step_link_must_connect_the_reached_hub(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_staging(project, "stg_products", ["product_id"])
    _write_hub(project, "hub_product", "stg_products", "product_id")
    # link_order_customer connects order/customer; starting from
    # hub_product the step cannot traverse it.
    _write_bridge(
        project,
        parent={"model": "hub_product"},
        path=[{"link": "link_order_customer", "hub": "hub_order"}],
    )
    with pytest.raises(LoomError, match="does not connect the reached hub"):
        validate_project(project)


def test_bridge_step_hub_must_be_a_link_endpoint(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_staging(project, "stg_products", ["product_id"])
    _write_hub(project, "hub_product", "stg_products", "product_id")
    _write_bridge(
        project,
        path=[{"link": "link_order_customer", "hub": "hub_product"}],
    )
    with pytest.raises(LoomError, match="is not an endpoint of link"):
        validate_project(project)


def test_bridge_path_never_revisits_a_hub(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_bridge(
        project,
        path=[
            {"link": "link_order_customer", "hub": "hub_order"},
            {"link": "link_order_customer", "hub": "hub_customer"},
        ],
    )
    with pytest.raises(LoomError, match="never revisits a hub"):
        validate_project(project)
