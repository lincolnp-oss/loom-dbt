"""Satellite generator (ADR 0028): contract, validation,
renderer shapes and the output-column resolver.

Runtime behaviour (multi-load change detection, record-tracking
appearance/deletion) is proven in tests/test_satellite_runtime.py;
the all-dialect parse matrix lives in test_generated_sql_parse.py.
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import sqlglot

from loom.cli import main
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.output_columns import OutputColumn, resolve_output_columns
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance

HUB_SAT = {
    "name": "sat_customer_details",
    "parent": {"model": "hub_customer", "key": "customer_id"},
    "input_models": ["stg_customers"],
    "attribute_columns": ["customer_name", "customer_email"],
    "record_source": "crm",
}

LINK_SAT = {
    "name": "sat_order_customer_details",
    "parent": {
        "model": "link_order_customer",
        "endpoints": [
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    },
    "input_models": ["stg_order_items"],
    "attribute_columns": ["product_name", "quantity"],
    "record_source": "pos",
}

RTS_SAT = {
    "name": "sat_customer_tracking",
    "parent": {"model": "hub_customer", "key": "customer_id"},
    "input_models": ["stg_customers"],
    "variant": "record_tracking",
    "record_source": "crm",
}


def _compile(rendered: str, *, incremental: bool) -> str:
    environment = jinja2.Environment()
    return environment.from_string(rendered).render(
        config=lambda **kwargs: "",
        ref=lambda name: name,
        source=lambda source_name, table: table,
        is_incremental=lambda: incremental,
        this="sat_customer_details",
    )


# -- registry / contract -------------------------------------------------


def test_migration_assessment_classifies_satellite_names() -> None:
    # Classification only (like hub_/link_): a hand-written sat_ model
    # is labelled a data-vault satellite; no generator fit is proposed
    # for the satellite shape in this build.
    from loom.migration_assessment import _classification

    label, evidence = _classification("sat_customer_details", "incremental")
    assert label == "data-vault satellite"
    assert "sat_" in evidence


# -- renderer shapes -------------------------------------------------------


def test_link_satellite_computes_the_link_hash_from_both_endpoints() -> None:
    renderer = get_renderer("satellite", "duckdb")
    sql = renderer.render(LINK_SAT, renderer.dialect, None)
    assert "AS order_customer_hk" in sql
    assert "JOIN {{ ref('link_order_customer') }} AS p" in sql
    # The link hash is the hash of the concatenated endpoint hashes,
    # as in the link renderer itself.
    link = get_renderer("link", "duckdb")
    link_sql = link.render(
        {
            "name": "link_order_customer",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
        link.dialect,
        None,
    )
    assert "concat(" in sql and "concat(" in link_sql


def test_non_historised_recomputes_the_diff_over_held_rows() -> None:
    params = dict(HUB_SAT, name="sat_customer_events", variant="non_historised")
    renderer = get_renderer("satellite", "duckdb")
    sql = renderer.render(params, renderer.dialect, None)
    assert "Variant: non_historised" in sql
    assert "NOT EXISTS" in sql
    assert "t.customer_name" in sql  # diff recomputed over {{ this }}
    #...but the diff is never projected as a stored column.
    projection = sql.split("FROM staged AS st")[0].rsplit("SELECT", 1)[1]
    assert "__hash_diff" not in projection


def test_renderer_fails_explicitly_on_bad_shapes() -> None:
    renderer = get_renderer("satellite", "duckdb")
    with pytest.raises(LoomError, match="variant"):
        renderer.render(dict(HUB_SAT, variant="computed"), renderer.dialect, None)
    with pytest.raises(LoomError, match="attribute_columns"):
        renderer.render(
            {k: v for k, v in HUB_SAT.items() if k != "attribute_columns"},
            renderer.dialect,
            None,
        )
    with pytest.raises(LoomError, match="attribute_columns"):
        renderer.render(dict(RTS_SAT, attribute_columns=["x"]), renderer.dialect, None)
    with pytest.raises(LoomError, match="parent block requires a 'key'"):
        renderer.render(
            {**HUB_SAT, "parent": {"model": "hub_customer"}},
            renderer.dialect,
            None,
        )
    with pytest.raises(LoomError, match="current-state view"):
        renderer.render(RTS_SAT, renderer.dialect, "current")


def test_incremental_branches_parse_in_the_target_grammar() -> None:
    # The parse matrix compiles with is_incremental() false; the
    # incremental branches (anti-joins against {{ this }}) must also
    # parse per dialect.
    sqlglot_dialects = {
        "duckdb": "duckdb",
        "snowflake": "snowflake",
        "bigquery": "bigquery",
        "postgres": "postgres",
        "redshift": "redshift",
        "databricks": "databricks",
        "azuresynapse": "tsql",
        "clickhouse": "clickhouse",
    }
    for name, params in (("standard", HUB_SAT), ("rts", RTS_SAT)):
        for dialect_name, read in sqlglot_dialects.items():
            renderer = get_renderer("satellite", dialect_name)
            compiled = _compile(
                renderer.render(params, renderer.dialect, None), incremental=True
            )
            parsed = sqlglot.parse(compiled, read=read)
            assert parsed and all(s is not None for s in parsed), (name, dialect_name)


def test_generated_data_tests_per_variant() -> None:
    renderer = get_renderer("satellite", "duckdb")
    tests = renderer.render_tests(HUB_SAT, renderer.dialect, None)
    assert tests is not None
    assert "- name: customer_hk" in tests
    assert "- name: __hash_diff" in tests
    # No relationships test to the parent (ADR 0028): satellite rows
    # legitimately outlive a current-retention hub's rows.
    assert "relationships:" not in tests
    rts_tests = renderer.render_tests(RTS_SAT, renderer.dialect, None)
    assert rts_tests is not None
    assert "- name: __is_deleted" in rts_tests
    assert "__hash_diff" not in rts_tests
    assert (
        renderer.render_tests(dict(HUB_SAT, name="customer_sat"), renderer.dialect, None)
        is None
    )


# -- validation ------------------------------------------------------------


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


def _write_hub(project: Path) -> None:
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            "description": "hub",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )


def _satellite_params(**overrides: object) -> dict:
    params = {
        "description": "Customer descriptive history.",
        "owner": "analytics",
        "parent": {"model": "hub_customer", "key": "customer_id"},
        "input_models": ["stg_customers"],
        "attribute_columns": ["customer_name", "customer_email"],
        "record_source": "crm",
    }
    params.update(overrides)
    return params


def _project_with_hub(tmp_path: Path) -> Path:
    _write_staging(
        tmp_path, "stg_customers", ["customer_id", "customer_name", "customer_email"]
    )
    _write_hub(tmp_path)
    return tmp_path


def test_valid_hub_satellite_project_validates(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project, "sat_customer_details", "satellite", _satellite_params(), pattern="sat"
    )
    entries = validate_project(project)
    assert any(e.data.get("generator") == "satellite" for e in entries)


def test_satellite_name_must_carry_the_sat_prefix(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project, "customer_details", "satellite", _satellite_params(), pattern="sat"
    )
    with pytest.raises(LoomError, match="must start with 'sat_'"):
        validate_project(project)


def test_satellite_variant_is_a_closed_enum(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        _satellite_params(variant="computed"),
        pattern="sat",
    )
    with pytest.raises(LoomError, match="'variant' must be one of"):
        validate_project(project)


def test_record_tracking_rejects_payload_columns(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project,
        "sat_customer_tracking",
        "satellite",
        _satellite_params(variant="record_tracking"),
        pattern="sat",
    )
    with pytest.raises(LoomError, match="record_tracking"):
        validate_project(project)


def test_parent_block_key_must_match_the_hub_business_key(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        _satellite_params(
            parent={"model": "hub_customer", "key": "customer_name"}
        ),
        pattern="sat",
    )
    with pytest.raises(LoomError, match="must match 'hub_customer' business_key"):
        validate_project(project)


def test_payload_columns_must_be_staging_aliases(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        _satellite_params(attribute_columns=["customer_name", "loyalty_tier"]),
        pattern="sat",
    )
    with pytest.raises(LoomError, match="loyalty_tier"):
        validate_project(project)


def test_parent_must_be_a_hub_or_link(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        _satellite_params(
            parent={"model": "stg_customers", "key": "customer_id"}
        ),
        pattern="sat",
    )
    with pytest.raises(LoomError, match="not an inventoried hub, link or reference hub"):
        validate_project(project)


def test_link_parent_endpoints_must_match_the_link(tmp_path: Path) -> None:
    project = _project_with_hub(tmp_path)
    _write_staging(
        project, "stg_orders", ["order_id", "customer_id", "product_name", "quantity"]
    )
    write_pattern_instance(
        project,
        "hub_order",
        "hub",
        {
            "description": "hub",
            "owner": "analytics",
            "input_models": ["stg_orders"],
            "business_key": "order_id",
        },
        pattern="hub",
    )
    write_pattern_instance(
        project,
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
    write_pattern_instance(
        project,
        "sat_order_customer_details",
        "satellite",
        {
            "description": "Order-line payload on the link.",
            "owner": "analytics",
            "parent": {
                "model": "link_order_customer",
                "endpoints": [
                    {"hub": "hub_order", "key": "order_id"},
                    # wrong: link says customer_id
                    {"hub": "hub_customer", "key": "customer_name"},
                ],
            },
            "input_models": ["stg_orders"],
            "attribute_columns": ["product_name"],
            "record_source": "pos",
        },
        pattern="sat",
    )
    with pytest.raises(LoomError, match="parent endpoints must match"):
        validate_project(project)


# -- output-column resolver ---------------------------------------------------


def _by_name(*records: dict) -> dict:
    return {r["name"].casefold(): r for r in records}


def test_resolver_columns_per_variant() -> None:
    hub = {"name": "hub_customer", "generator": "hub", "business_key": "customer_id"}
    standard = resolve_output_columns(
        {"name": "sat_customer_details", "generator": "satellite", **HUB_SAT},
        _by_name(hub),
    )
    assert standard == [
        OutputColumn("customer_hk", "text"),
        OutputColumn("__hash_diff", "text"),
        OutputColumn("customer_name"),
        OutputColumn("customer_email"),
        OutputColumn("__loaded_at", "timestamp"),
        OutputColumn("__record_source", "text"),
    ]
    tracking = resolve_output_columns(
        {"name": "sat_customer_tracking", "generator": "satellite", **RTS_SAT},
        _by_name(hub),
    )
    assert tracking == [
        OutputColumn("customer_hk", "text"),
        OutputColumn("__loaded_at", "timestamp"),
        OutputColumn("__record_source", "text"),
        OutputColumn("__is_deleted", "boolean"),
    ]
    link = {
        "name": "link_order_customer",
        "generator": "link",
        "endpoints": [
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    }
    on_link = resolve_output_columns(
        {"name": "sat_order_customer_details", "generator": "satellite", **LINK_SAT},
        _by_name(link),
    )
    assert on_link is not None
    assert on_link[0] == OutputColumn("order_customer_hk", "text")
    assert (
        resolve_output_columns(
            {"name": "sat_x", "generator": "satellite", **HUB_SAT}, _by_name()
        )
        is None
    )


# -- generation -----------------------------------------------------------------


def test_generate_writes_table_and_current_view_for_standard_only(
    tmp_path: Path,
) -> None:
    project = _project_with_hub(tmp_path)
    write_pattern_instance(
        project, "sat_customer_details", "satellite", _satellite_params(), pattern="sat"
    )
    rts_params = _satellite_params(variant="record_tracking")
    del rts_params["attribute_columns"]
    write_pattern_instance(
        project, "sat_customer_tracking", "satellite", rts_params, pattern="sat"
    )

    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0
    models = project / "models"
    assert (models / "sat_customer_details.sql").exists()
    assert (models / "sat_customer_details_current.sql").exists()
    assert (models / "sat_customer_tracking.sql").exists()
    assert not (models / "sat_customer_tracking_current.sql").exists()
    tests = (models / "_generated/tests" / "data_sat_customer_details.yml").read_text(
        encoding="utf-8"
    )
    assert "- name: customer_hk" in tests
    # Drift check is clean right after generation.
    assert (
        main(
            [
                "generate",
                "--project-dir",
                str(project),
                "--dialect",
                "duckdb",
                "--check",
            ]
        )
        == 0
    )
