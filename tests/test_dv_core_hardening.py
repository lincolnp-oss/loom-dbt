"""Multi-source hubs, n-ary links (ADR 0030),
dependent-child keys and non-historised links.

Covers the renderer shapes, the record + relationship validation,
and the catalogue resolver for the generalised hub/link contracts.
Runtime proof lives in tests/test_dv_core_runtime.py.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from loom.dialects import DIALECTS
from render_execution import compile_sql, materialize, render
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.output_columns import resolve_output_columns
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance


# -- shared project builders ----------------------------------------


def _write_staging(project: Path, name: str, columns: list[str]) -> None:
    write_pattern_instance(
        project,
        name,
        "staging",
        {
            "description": f"{name} staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": name},
            "columns": [{"source": c, "alias": c} for c in columns],
        },
        pattern="staging",
    )


def _write_hub(
    project: Path, name: str, input_model: str, business_key: str, **extra: object
) -> None:
    params: dict = {
        "description": f"{name} hub",
        "owner": "analytics",
        "input_models": [input_model],
        "business_key": business_key,
    }
    params.update(extra)
    write_pattern_instance(project, name, "hub", params, pattern="hub")


def _order_project(tmp_path: Path) -> Path:
    """Staging + three hubs over one order-items staging model."""
    _write_staging(
        tmp_path,
        "stg_order_items",
        ["order_id", "customer_id", "product_name", "order_item_id", "quantity"],
    )
    _write_hub(tmp_path, "hub_order", "stg_order_items", "order_id")
    _write_hub(tmp_path, "hub_customer", "stg_order_items", "customer_id")
    _write_hub(tmp_path, "hub_product", "stg_order_items", "product_name")
    return tmp_path


def _link_params(**overrides: object) -> dict:
    params: dict = {
        "name": "link_order_customer",
        "input_models": ["stg_order_items"],
        "endpoints": [
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    }
    params.update(overrides)
    return params


def _write_link(project: Path, name: str, **params: object) -> None:
    full: dict = {
        "description": f"{name} link",
        "owner": "analytics",
        "input_models": ["stg_order_items"],
        "endpoints": [
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    }
    full.update(params)
    write_pattern_instance(project, name, "link", full, pattern="link")


# -- multi-source hub renderer ---------------------------------------


def _multi_hub_params(**overrides: object) -> dict:
    params: dict = {
        "name": "hub_customer",
        "input_models": ["stg_customers_crm", "stg_customers_web"],
        "business_key": "customer_id",
        "retention": "persistent",
        "record_source": {"stg_customers_crm": "crm", "stg_customers_web": "web"},
    }
    params.update(overrides)
    return params


def test_multi_source_hub_unions_inputs_and_ranks_by_declared_order() -> None:
    renderer = get_renderer("hub", "duckdb")
    sql = renderer.render(_multi_hub_params(), DIALECTS["duckdb"], None)
    assert "UNION ALL" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY customer_hk" in sql
    assert "ORDER BY __source_ordinal" in sql
    assert "'crm' AS __record_source" in sql
    assert "'web' AS __record_source" in sql
    assert "0 AS __source_ordinal" in sql
    assert "1 AS __source_ordinal" in sql
    # Persistent + append, anti-joined on the hash like the
    # single-input form.
    assert "materialized='incremental'" in sql
    assert "incremental_strategy='append'" in sql
    assert "target.customer_hk" in sql


def test_multi_source_hub_without_provenance_emits_no_audit_columns() -> None:
    renderer = get_renderer("hub", "duckdb")
    params = _multi_hub_params()
    del params["record_source"]
    del params["retention"]
    sql = renderer.render(params, DIALECTS["duckdb"], None)
    assert "{{ config(materialized='table') }}" in sql
    assert "__record_source" not in sql
    assert "__loaded_at" not in sql


def test_multi_source_hub_databricks_merges_and_excludes_provenance() -> None:
    renderer = get_renderer("hub", "databricks")
    sql = renderer.render(_multi_hub_params(), DIALECTS["databricks"], None)
    assert "incremental_strategy='merge'" in sql
    assert "unique_key='customer_hk'" in sql
    assert "merge_exclude_columns=['__record_source', '__loaded_at']" in sql


# -- multi-source hub validation --------------------------------------


def _multi_hub_project(tmp_path: Path, **hub_overrides: object) -> Path:
    _write_staging(tmp_path, "stg_customers_crm", ["customer_id", "customer_name"])
    _write_staging(tmp_path, "stg_customers_web", ["customer_id", "customer_name"])
    params: dict = {
        "description": "Customer hub",
        "owner": "analytics",
        "input_models": ["stg_customers_crm", "stg_customers_web"],
        "business_key": "customer_id",
        "retention": "persistent",
        "record_source": {"stg_customers_crm": "crm", "stg_customers_web": "web"},
    }
    params.update(hub_overrides)
    for key in [key for key, value in hub_overrides.items() if value is None]:
        params.pop(key, None)
    write_pattern_instance(tmp_path, "hub_customer", "hub", params, pattern="hub")
    return tmp_path


def test_multi_source_hub_project_validates(tmp_path: Path) -> None:
    entries = validate_project(_multi_hub_project(tmp_path))
    assert any(e.data.get("generator") == "hub" for e in entries)


def test_hub_rejects_the_retired_singular_input_parameter(
    tmp_path: Path,
) -> None:
    # 'input_model' is no longer a hub parameter at all: the
    # consolidated surface names 'input_models' only, so the old key
    # is rejected as an unknown parameter.
    project = _multi_hub_project(tmp_path, input_model="stg_customers_crm")
    with pytest.raises(LoomError, match="unknown parameter 'input_model'"):
        validate_project(project)


def test_hub_with_no_input_is_rejected(tmp_path: Path) -> None:
    _write_staging(tmp_path, "stg_customers", ["customer_id"])
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {"description": "hub", "owner": "analytics", "business_key": "customer_id"},
        pattern="hub",
    )
    with pytest.raises(LoomError, match="'input_models' is a required property"):
        validate_project(tmp_path)


def test_input_models_entries_must_be_distinct(tmp_path: Path) -> None:
    # A single-entry 'input_models' is now legal (the old "at least
    # two" rule is gone); what remains forbidden is naming the same
    # input twice.
    project = _multi_hub_project(
        tmp_path,
        input_models=["stg_customers_crm", "stg_customers_crm"],
        record_source=None,
    )
    with pytest.raises(LoomError, match="'input_models' entries must be distinct"):
        validate_project(project)


def test_record_source_must_cover_every_input(tmp_path: Path) -> None:
    project = _multi_hub_project(
        tmp_path, record_source={"stg_customers_crm": "crm"}
    )
    with pytest.raises(LoomError, match="must name exactly the hub's input_models"):
        validate_project(project)


def test_record_source_requires_persistent_retention(tmp_path: Path) -> None:
    project = _multi_hub_project(tmp_path, retention="current")
    with pytest.raises(LoomError, match="record_source requires retention"):
        validate_project(project)


def test_string_record_source_applies_uniformly_to_every_input(
    tmp_path: Path,
) -> None:
    # The string form of 'record_source' is uniform provenance: it is
    # accepted alongside multiple 'input_models' and labels every
    # source (the old singular/multi split is gone).
    project = _multi_hub_project(tmp_path, record_source="crm")
    entries = validate_project(project)
    assert any(e.data.get("generator") == "hub" for e in entries)


def test_multi_source_hub_key_must_be_an_alias_of_every_input(
    tmp_path: Path,
) -> None:
    _write_staging(tmp_path, "stg_customers_crm", ["customer_id"])
    _write_staging(tmp_path, "stg_customers_web", ["web_customer_ref"])
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            "description": "hub",
            "owner": "analytics",
            "input_models": ["stg_customers_crm", "stg_customers_web"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )
    with pytest.raises(
        LoomError, match="does not generate required hub column alias: customer_id"
    ):
        validate_project(tmp_path)


# -- n-ary link renderer ----------------------------------------------


def _ternary_params(**overrides: object) -> dict:
    params = _link_params(
        name="link_order_customer_product",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_product", "key": "product_name"},
        ],
    )
    params.update(overrides)
    return params


def test_ternary_link_composes_three_hashes_in_declared_order() -> None:
    renderer = get_renderer("link", "duckdb")
    sql = renderer.render(_ternary_params(), DIALECTS["duckdb"], None)
    order_hash = "LOWER(sha256(UPPER(TRIM(CAST(t.order_id AS VARCHAR)))))"
    customer_hash = "LOWER(sha256(UPPER(TRIM(CAST(t.customer_id AS VARCHAR)))))"
    product_hash = "LOWER(sha256(UPPER(TRIM(CAST(t.product_name AS VARCHAR)))))"
    assert (
        f"concat({order_hash}, {customer_hash}, {product_hash})" in sql
    )
    # Third endpoint joins under its own alias and the guard counts it.
    assert "AS e3" in sql
    assert "e3.product_hk" in sql
    assert "t.product_name IS NULL" in sql
    assert "SELECT order_customer_product_hk, order_hk, customer_hk, product_hk" in sql


def test_ternary_link_on_redshift_chains_hashes_with_pipes() -> None:
    renderer = get_renderer("link", "redshift")
    sql = renderer.render(_ternary_params(), DIALECTS["redshift"], None)
    assert " || " in sql
    # Redshift CONCAT takes two arguments; the three-component link
    # hash must not use it.
    link_line = next(
        line for line in sql.splitlines() if "order_customer_product_hk," in line
    )
    assert "concat" not in link_line.lower()


def test_link_tests_cover_every_endpoint() -> None:
    renderer = get_renderer("link", "duckdb")
    tests = renderer.render_tests(_ternary_params(), DIALECTS["duckdb"], None)
    assert tests is not None
    for hub in ("hub_order", "hub_customer", "hub_product"):
        assert f"to: ref('{hub}')" in tests


# -- n-ary / hierarchical link validation ------------------------------


def test_ternary_link_project_validates(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_customer_product",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_product", "key": "product_name"},
        ],
    )
    entries = validate_project(project)
    assert any(e.data.get("generator") == "link" for e in entries)


def test_additional_endpoint_key_must_match_its_hubs_business_key(
    tmp_path: Path,
) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_customer_product",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_product", "key": "order_id"},
        ],
    )
    with pytest.raises(LoomError, match="must match 'hub_product' business_key"):
        validate_project(project)


def test_endpoints_shape_is_checked(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_customer_product",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_product"},
        ],
    )
    with pytest.raises(
        LoomError, match="endpoints.2 must be a mapping with exactly"
    ):
        validate_project(project)


def test_hierarchical_link_is_rejected_explicitly(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_customer_customer",
        endpoints=[
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    )
    with pytest.raises(LoomError, match="hierarchical links"):
        validate_project(project)


def test_same_hub_twice_in_endpoints_is_rejected(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_customer_again",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_order", "key": "order_id"},
        ],
    )
    with pytest.raises(LoomError, match="appears more than once"):
        validate_project(project)


# -- dependent-child keys ----------------------------------------------


def test_dependent_child_key_joins_the_hash_and_is_stored_normalized() -> None:
    renderer = get_renderer("link", "duckdb")
    params = _link_params(dependent_child_key="order_item_id")
    sql = renderer.render(params, DIALECTS["duckdb"], None)
    # Normalized dependent value is the final hash component and a
    # stored column; null dependent keys are guard violations.
    assert "UPPER(TRIM(CAST(t.order_item_id AS VARCHAR)))))" in sql
    assert "AS order_item_id" in sql
    assert "t.order_item_id IS NULL" in sql


def test_dependent_child_key_must_be_distinct_from_endpoint_keys(
    tmp_path: Path,
) -> None:
    project = _order_project(tmp_path)
    _write_link(project, "link_order_customer", dependent_child_key="order_id")
    with pytest.raises(LoomError, match="must be distinct from the endpoint keys"):
        validate_project(project)


def test_dependent_child_key_must_be_a_staging_alias(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(project, "link_order_customer", dependent_child_key="line_no")
    with pytest.raises(LoomError, match="required link column alias"):
        validate_project(project)


# -- non-historised links ---------------------------------------


def _nh_params(**overrides: object) -> dict:
    params = _link_params(
        name="link_order_item_events",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_product", "key": "product_name"},
        ],
        variant="non_historised",
        dependent_child_key="order_item_id",
        payload_columns=["quantity"],
    )
    params.update(overrides)
    return params


def test_non_historised_link_is_an_append_only_event_link() -> None:
    renderer = get_renderer("link", "duckdb")
    sql = renderer.render(_nh_params(), DIALECTS["duckdb"], None)
    assert "-- Variant: non_historised" in sql
    assert "materialized='incremental'" in sql
    assert "incremental_strategy='append'" in sql
    assert "is_incremental()" in sql
    assert "target.order_item_events_hk" in sql
    assert "t.quantity" in sql
    # The fail-fast guard still applies.
    assert "WITH __link_guard AS" in sql


def test_non_historised_link_project_validates(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_item_events",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_product", "key": "product_name"},
        ],
        variant="non_historised",
        dependent_child_key="order_item_id",
        payload_columns=["quantity"],
    )
    entries = validate_project(project)
    assert any(e.data.get("generator") == "link" for e in entries)


def test_non_historised_link_requires_a_dependent_child_key(
    tmp_path: Path,
) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_item_events",
        variant="non_historised",
        payload_columns=["quantity"],
    )
    with pytest.raises(LoomError, match="requires a 'dependent_child_key'"):
        validate_project(project)


def test_non_historised_link_requires_payload(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_item_events",
        variant="non_historised",
        dependent_child_key="order_item_id",
    )
    with pytest.raises(LoomError, match="requires a non-empty 'payload_columns'"):
        validate_project(project)


def test_payload_columns_are_rejected_on_a_standard_link(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(project, "link_order_customer", payload_columns=["quantity"])
    with pytest.raises(LoomError, match="only accepted for the non_historised"):
        validate_project(project)


def test_link_variant_is_a_closed_enum(tmp_path: Path) -> None:
    project = _order_project(tmp_path)
    _write_link(project, "link_order_customer", variant="historised")
    with pytest.raises(LoomError, match="'variant' must be one of"):
        validate_project(project)


def test_link_renderer_rejects_an_unknown_variant() -> None:
    renderer = get_renderer("link", "duckdb")
    with pytest.raises(LoomError, match="link variant 'historised' is not supported"):
        renderer.render(_link_params(variant="historised"), DIALECTS["duckdb"], None)


# -- satellites on generalised link parents ------------------------------


def _write_ternary_link(project: Path) -> None:
    _write_link(
        project,
        "link_order_customer_product",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
            {"hub": "hub_product", "key": "product_name"},
        ],
    )


def _satellite_on_link_params(**overrides: object) -> dict:
    params: dict = {
        "description": "Order line descriptive history",
        "owner": "analytics",
        "parent": {
            "model": "link_order_customer_product",
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
                {"hub": "hub_product", "key": "product_name"},
            ],
        },
        "input_models": ["stg_order_items"],
        "attribute_columns": ["quantity"],
        "record_source": "orders",
    }
    params.update(overrides)
    return params


def test_satellite_on_a_ternary_link_validates_and_hashes_like_the_link(
    tmp_path: Path,
) -> None:
    project = _order_project(tmp_path)
    _write_ternary_link(project)
    params = _satellite_on_link_params()
    write_pattern_instance(
        project, "sat_order_line_details", "satellite", params, pattern="sat"
    )
    entries = validate_project(project)
    assert any(e.data.get("generator") == "satellite" for e in entries)

    # The satellite's computed parent hash must be the link's own
    # three-component composition.
    link_sql = get_renderer("link", "duckdb").render(
        _ternary_params(), DIALECTS["duckdb"], None
    )
    sat_sql = get_renderer("satellite", "duckdb").render(
        {**params, "name": "sat_order_line_details"},
        DIALECTS["duckdb"],
        None,
    )
    order_hash = "LOWER(sha256(UPPER(TRIM(CAST(s.order_id AS VARCHAR)))))"
    customer_hash = "LOWER(sha256(UPPER(TRIM(CAST(s.customer_id AS VARCHAR)))))"
    product_hash = "LOWER(sha256(UPPER(TRIM(CAST(s.product_name AS VARCHAR)))))"
    assert f"concat({order_hash}, {customer_hash}, {product_hash})" in sat_sql
    assert "order_customer_product_hk" in link_sql


def test_satellite_missing_a_parent_endpoint_is_rejected(
    tmp_path: Path,
) -> None:
    project = _order_project(tmp_path)
    _write_ternary_link(project)
    params = _satellite_on_link_params()
    params["parent"]["endpoints"] = params["parent"]["endpoints"][:2]
    write_pattern_instance(
        project, "sat_order_line_details", "satellite", params, pattern="sat"
    )
    with pytest.raises(LoomError, match="parent endpoints must match"):
        validate_project(project)


def test_satellite_on_a_dependent_child_link_must_mirror_the_key(
    tmp_path: Path,
) -> None:
    project = _order_project(tmp_path)
    _write_link(
        project,
        "link_order_item_events",
        endpoints=[
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_product", "key": "product_name"},
        ],
        variant="non_historised",
        dependent_child_key="order_item_id",
        payload_columns=["quantity"],
    )
    params = {
        "description": "Event payload history",
        "owner": "analytics",
        "parent": {
            "model": "link_order_item_events",
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_product", "key": "product_name"},
            ],
        },
        "input_models": ["stg_order_items"],
        "attribute_columns": ["quantity"],
        "record_source": "orders",
    }
    write_pattern_instance(
        project, "sat_event_details", "satellite", params, pattern="sat"
    )
    with pytest.raises(LoomError, match="dependent_child_key"):
        validate_project(project)

    params["parent"]["dependent_child_key"] = "order_item_id"
    write_pattern_instance(
        project, "sat_event_details", "satellite", params, pattern="sat"
    )
    entries = validate_project(project)
    assert any(e.data.get("generator") == "satellite" for e in entries)


# -- resolver ------------------------------------------------------------


def test_resolver_describes_the_generalised_link_shapes() -> None:
    ternary = resolve_output_columns({**_ternary_params(), "generator": "link"})
    assert [c.name for c in ternary or []] == [
        "order_customer_product_hk",
        "order_hk",
        "customer_hk",
        "product_hk",
    ]
    nh = resolve_output_columns({**_nh_params(), "generator": "link"})
    assert [c.name for c in nh or []] == [
        "order_item_events_hk",
        "order_hk",
        "product_hk",
        "order_item_id",
        "quantity",
    ]


def test_resolver_multi_source_hub_with_provenance_has_audit_columns() -> None:
    columns = resolve_output_columns(
        {
            "name": "hub_customer",
            "generator": "hub",
            "business_key": "customer_id",
            "input_models": ["stg_customers_crm", "stg_customers_web"],
            "record_source": {"stg_customers_crm": "crm", "stg_customers_web": "web"},
        }
    )
    assert [c.name for c in columns or []] == [
        "customer_hk",
        "customer_id",
        "__record_source",
        "__loaded_at",
    ]


# -- Generated-SQL execution -----------------------------------------
# Data-semantic execution: hash joinability and the link
# guard are row/exception assertions the conformance chain (counts
# only) does not make.


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def test_hub_and_link_execute_and_join_on_hashes(conn) -> None:
    conn.execute(
        "CREATE TABLE stg_orders (order_id INTEGER, customer_id INTEGER)"
    )
    conn.execute("INSERT INTO stg_orders VALUES (100, 1), (101, 2)")
    conn.execute(
        "CREATE TABLE stg_customers (customer_id INTEGER)"
    )
    conn.execute("INSERT INTO stg_customers VALUES (1), (2)")
    materialize(
        conn,
        "hub_order",
        render(
            "hub",
            {
                "name": "hub_order",
                "input_models": ["stg_orders"],
                "business_key": "order_id",
            },
        ),
    )
    materialize(
        conn,
        "hub_customer",
        render(
            "hub",
            {
                "name": "hub_customer",
                "input_models": ["stg_customers"],
                "business_key": "customer_id",
            },
        ),
    )
    materialize(
        conn,
        "link_order_customer",
        render(
            "link",
            {
                "name": "link_order_customer",
                "input_models": ["stg_orders"],
                "endpoints": [
                    {"hub": "hub_order", "key": "order_id"},
                    {"hub": "hub_customer", "key": "customer_id"},
                ],
            },
        ),
    )
    count = conn.execute("SELECT COUNT(*) FROM link_order_customer").fetchone()
    assert count == (2,)
    matched = conn.execute(
        "SELECT COUNT(*) FROM link_order_customer l "
        "JOIN hub_order ho ON l.order_hk = ho.order_hk "
        "JOIN hub_customer hc ON l.customer_hk = hc.customer_hk"
    ).fetchone()
    assert matched == (2,)


@pytest.mark.parametrize(
    "poison_row",
    [(102, "99"), (102, None), (102, "  ")],
    ids=["unmatched-key", "null-key", "blank-key"],
)
def test_link_guard_raises_on_bad_keys(conn, poison_row) -> None:
    # (ADR 0030) Null, blank or hub-unmatched input keys
    # must fail the link build with the guard message instead of
    # being silently filtered/dropped.
    conn.execute(
        "CREATE TABLE stg_orders (order_id VARCHAR, customer_id VARCHAR)"
    )
    conn.execute("INSERT INTO stg_orders VALUES ('100', '1'), ('101', '2')")
    conn.execute("INSERT INTO stg_orders VALUES (?, ?)", list(poison_row))
    conn.execute("CREATE TABLE stg_customers (customer_id VARCHAR)")
    conn.execute("INSERT INTO stg_customers VALUES ('1'), ('2')")
    materialize(
        conn,
        "hub_order",
        render(
            "hub",
            {
                "name": "hub_order",
                "input_models": ["stg_orders"],
                "business_key": "order_id",
            },
        ),
    )
    materialize(
        conn,
        "hub_customer",
        render(
            "hub",
            {
                "name": "hub_customer",
                "input_models": ["stg_customers"],
                "business_key": "customer_id",
            },
        ),
    )
    with pytest.raises(duckdb.Error, match="Loom link guard"):
        materialize(
            conn,
            "link_order_customer",
            render(
                "link",
                {
                    "name": "link_order_customer",
                    "input_models": ["stg_orders"],
                    "endpoints": [
                        {"hub": "hub_order", "key": "order_id"},
                        {"hub": "hub_customer", "key": "customer_id"},
                    ],
                },
            ),
        )
