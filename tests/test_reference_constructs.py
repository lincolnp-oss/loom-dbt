"""Reference constructs (ADR 0034) - reference_hub and
reference_table renderers, validation, and the output-column
resolver. Runtime behaviour is proven in
tests/test_reference_runtime.py; the all-dialect parse matrix
lives in test_generated_sql_parse.py.
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

REF_HUB = {
    "name": "ref_country",
    "input_models": ["stg_country_codes"],
    "business_key": "country_code",
    "record_source": "refdata",
}

REF_TABLE = {
    "name": "ref_country_table",
    "reference_hub": "ref_country",
    "satellites": ["sat_country_details"],
    "business_key": "country_code",
    "attribute_columns": ["country_name"],
}


def _by_name(*records: dict) -> dict:
    return {record["name"].casefold(): record for record in records}


# -- reference_hub renderer ---------------------------------------------------


def test_ref_hub_tests_cover_hash_and_code() -> None:
    renderer = get_renderer("reference_hub", "duckdb")
    tests = renderer.render_tests(REF_HUB, DUCKDB, None) or ""
    assert "- name: country_hk" in tests
    assert "- name: country_code" in tests


def test_ref_hub_resolver_outputs() -> None:
    columns = resolve_output_columns({**REF_HUB, "generator": "reference_hub"})
    assert [(c.name, c.fixed_type) for c in columns] == [
        ("country_hk", "text"),
        ("country_code", None),
        ("__record_source", "text"),
        ("__loaded_at", "timestamp"),
    ]


# -- reference_table renderer ---------------------------------------------------


def test_ref_table_full_shape_has_no_latest_resolution() -> None:
    renderer = get_renderer("reference_table", "duckdb")
    sql = renderer.render({**REF_TABLE, "variant": "full"}, DUCKDB, None)
    assert "ROW_NUMBER" not in sql
    assert "JOIN {{ ref('sat_country_details') }} AS s" in sql


def test_ref_table_snapshot_filters_by_timestamp() -> None:
    renderer = get_renderer("reference_table", "duckdb")
    sql = renderer.render(
        {**REF_TABLE, "variant": "snapshot", "snapshot_timestamp": "2026-01-01T00:00:00"},
        DUCKDB,
        None,
    )
    assert "WHERE __loaded_at <= '2026-01-01T00:00:00'" in sql
    assert "s.rn = 1" in sql


def test_ref_table_snapshot_requires_timestamp() -> None:
    renderer = get_renderer("reference_table", "duckdb")
    with pytest.raises(LoomError, match="requires a 'snapshot_timestamp'"):
        renderer.render({**REF_TABLE, "variant": "snapshot"}, DUCKDB, None)


def test_ref_table_variant_is_a_closed_enum() -> None:
    renderer = get_renderer("reference_table", "duckdb")
    with pytest.raises(LoomError, match="variant 'hourly' is not supported"):
        renderer.render({**REF_TABLE, "variant": "hourly"}, DUCKDB, None)


def test_ref_table_tests_unique_only_where_one_row_per_code() -> None:
    renderer = get_renderer("reference_table", "duckdb")
    latest = renderer.render_tests(REF_TABLE, DUCKDB, None) or ""
    assert "- unique" in latest
    full = renderer.render_tests({**REF_TABLE, "variant": "full"}, DUCKDB, None) or ""
    assert "- unique" not in full


def test_ref_table_resolver_outputs() -> None:
    columns = resolve_output_columns({**REF_TABLE, "generator": "reference_table"})
    assert [c.name for c in columns] == [
        "country_hk",
        "country_code",
        "country_name",
        "__record_source",
        "__loaded_at",
    ]


def test_satellite_resolver_accepts_reference_hub_parent() -> None:
    sat = {
        "name": "sat_country_details",
        "generator": "satellite",
        "parent": {"model": "ref_country", "key": "country_code"},
        "attribute_columns": ["country_name"],
        "record_source": "refdata",
    }
    ref_hub = {"name": "ref_country", "generator": "reference_hub"}
    columns = resolve_output_columns(sat, _by_name(ref_hub))
    assert [c.name for c in columns] == [
        "country_hk",
        "__hash_diff",
        "country_name",
        "__loaded_at",
        "__record_source",
    ]


# -- validation -----------------------------------------------------------------


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


def _project(tmp_path: Path, *, sat_variant: str = "standard") -> Path:
    _write_staging(tmp_path, "stg_country_codes", ["country_code", "country_name"])
    write_pattern_instance(
        tmp_path, "ref_country", "reference_hub",
        {
            "description": "Country reference codes.",
            "owner": "analytics",
            "input_models": ["stg_country_codes"],
            "business_key": "country_code",
            "record_source": "refdata",
        },
        pattern="ref",
    )
    write_pattern_instance(
        tmp_path, "sat_country_details", "satellite",
        {
            "description": "Country names.",
            "owner": "analytics",
            "parent": {"model": "ref_country", "key": "country_code"},
            "input_models": ["stg_country_codes"],
            "variant": sat_variant,
            "attribute_columns": ["country_name"],
            "record_source": "refdata",
        },
        pattern="sat",
    )
    return tmp_path


def _write_table(project: Path, **overrides: object) -> None:
    params = {
        "description": "Flattened country reference.",
        "owner": "analytics",
        "reference_hub": "ref_country",
        "satellites": ["sat_country_details"],
        "business_key": "country_code",
        "attribute_columns": ["country_name"],
    }
    params.update(overrides)
    write_pattern_instance(
        project, "ref_country_table", "reference_table", params, pattern="reftable"
    )


def test_reference_project_validates(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_table(project)
    validate_project(project)


def test_ref_hub_name_prefix_is_structural(tmp_path: Path) -> None:
    _write_staging(tmp_path, "stg_country_codes", ["country_code", "country_name"])
    write_pattern_instance(
        tmp_path, "country_ref", "reference_hub",
        {
            "description": "Country reference codes.",
            "owner": "analytics",
            "input_models": ["stg_country_codes"],
            "business_key": "country_code",
            "record_source": "refdata",
        },
        pattern="ref",
    )
    with pytest.raises(LoomError, match="must start with 'ref_'"):
        validate_project(tmp_path)


def test_ref_table_business_key_must_mirror_the_hub(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_table(project, business_key="code")
    with pytest.raises(LoomError, match="must match reference hub"):
        validate_project(project)


def test_ref_table_attributes_must_be_the_satellites(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_table(project, attribute_columns=["country_name", "capital"])
    with pytest.raises(LoomError, match="not carried by satellite"):
        validate_project(project)


def test_ref_table_latest_rejects_non_historised_satellite(tmp_path: Path) -> None:
    project = _project(tmp_path, sat_variant="non_historised")
    _write_table(project)
    with pytest.raises(LoomError, match="variant 'latest' requires satellite 'sat_country_details' to be a standard satellite"):
        validate_project(project)


def test_ref_table_full_accepts_non_historised_satellite(tmp_path: Path) -> None:
    project = _project(tmp_path, sat_variant="non_historised")
    _write_table(project, variant="full")
    validate_project(project)


def test_ref_table_snapshot_requires_timestamp_at_validation(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _write_table(project, variant="snapshot")
    with pytest.raises(LoomError, match="'snapshot_timestamp' is required"):
        validate_project(project)
