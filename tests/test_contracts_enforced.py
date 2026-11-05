"""Opt-in enforced model contracts (ADR 0032).

Envelope resolution (pattern default, per-instance override),
authoring-time completeness validation, contract rendering per
dialect, and emission into the model's single schema document.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from loom.cli import main
from loom.dialects import get_dialect, render_contract_data_type
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.properties import model_properties_entry
from metadata_helpers import write_pattern_instance

BASE_TYPES = {"widget_id": "integer", "record_source": "text"}


def _definition(project: Path, pattern: str, generator: str, defaults: dict) -> None:
    path = project / "metadata" / "patterns" / f"{pattern}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "name": pattern,
                "generator": generator,
                "defaults": defaults,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _staging(project: Path, **extra: Any) -> None:
    write_pattern_instance(
        project,
        "stg_widgets",
        "staging",
        {
            "description": "Widget staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "widgets"},
            "columns": [
                {"source": "widget_id", "alias": "widget_id"},
                {"source": "record_source", "alias": "record_source"},
            ],
            **extra,
        },
        pattern="staging",
    )


def _hub(project: Path, **extra: Any) -> None:
    write_pattern_instance(
        project,
        "hub_widget",
        "hub",
        {
            "description": "Widget hub",
            "owner": "analytics",
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
            **extra,
        },
        pattern="hub",
    )


def _dimension(project: Path, **extra: Any) -> None:
    write_pattern_instance(
        project,
        "dim_widgets",
        "dimension",
        {
            "description": "Widget dimension",
            "owner": "analytics",
            "input_models": ["hub_widget"],
            "natural_key": "widget_id",
            "surrogate_key": "widget_sk",
            "attribute_columns": ["widget_hk"],
            **extra,
        },
        pattern="dimension",
    )


def test_pattern_default_with_instance_opt_out(tmp_path: Path) -> None:
    # The pattern enforces; the instance opts out. Without the
    # opt-out the hub would fail completeness (widget_id untyped).
    _definition(
        tmp_path, "hub", "hub",
        {"description": "Widget hub", "owner": "analytics", "contract_enforced": True},
    )
    _staging(tmp_path)
    # The instance omits description/owner (they come from the
    # defaults) and overrides only the envelope key.
    write_pattern_instance(
        tmp_path,
        "hub_widget",
        "hub",
        {
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
            "contract_enforced": False,
        },
        pattern="hub",
    )
    _dimension(tmp_path, contract_enforced=True, column_types={"widget_id": "integer", "widget_hk": "text"})
    entries = validate_project(tmp_path)
    hub = next(e for e in entries if e.data.get("name") == "hub_widget")
    assert hub.data["contract_enforced"] is False


def test_completeness_failure_names_untyped_columns(tmp_path: Path) -> None:
    _staging(tmp_path)
    _hub(tmp_path)
    _dimension(tmp_path, contract_enforced=True)
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    message = str(caught.value)
    assert "contract_enforced model 'dim_widgets'" in message
    assert "widget_id" in message
    assert "declare their types in column_types" in message


def test_completeness_passes_when_all_typed(tmp_path: Path) -> None:
    _staging(tmp_path, contract_enforced=True, column_types=BASE_TYPES)
    _hub(tmp_path, contract_enforced=True, column_types={"widget_id": "integer"})
    _dimension(tmp_path, contract_enforced=True, column_types={"widget_id": "integer", "widget_hk": "text"})
    validate_project(tmp_path)  # must not raise


def test_underivable_outputs_fail_closed(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "bronze_widgets",
        "bronze",
        {
            "description": "Widget bronze",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "widgets"},
            "contract_enforced": True,
        },
        pattern="bronze",
    )
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "output columns cannot be derived" in str(caught.value)


def test_non_boolean_value_rejected(tmp_path: Path) -> None:
    _staging(tmp_path)
    _hub(tmp_path, contract_enforced="yes")
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "'contract_enforced' must be true or false" in str(caught.value)


def test_bare_decimal_declaration_counts_as_untyped(tmp_path: Path) -> None:
    _staging(tmp_path)
    _hub(tmp_path)
    _dimension(
        tmp_path,
        contract_enforced=True,
        column_types={"widget_id": "decimal"},
    )
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "untyped output column(s): widget_id" in str(caught.value)


def test_contract_type_rendering() -> None:
    duckdb = get_dialect("duckdb")
    postgres = get_dialect("postgres")
    clickhouse = get_dialect("clickhouse")
    snowflake = get_dialect("snowflake")
    assert render_contract_data_type(duckdb, "integer") == "INTEGER"
    assert render_contract_data_type(duckdb, "decimal(10,2)") == "DECIMAL(10,2)"
    # DuckDB's Loom stamp columns are naive TIMESTAMP
    # (current_timestamp is `localtimestamp`), and DuckDB reports
    # them as TIMESTAMP.
    assert render_contract_data_type(duckdb, "timestamp") == "TIMESTAMP"
    assert render_contract_data_type(postgres, "text") == "character varying"
    assert render_contract_data_type(postgres, "timestamp") == (
        "timestamp without time zone"
    )
    assert render_contract_data_type(postgres, "decimal(10,2)") == "numeric(10,2)"
    assert render_contract_data_type(clickhouse, "integer") == "Int32"
    # No contract map yet: descriptive spelling is the fallback.
    assert render_contract_data_type(snowflake, "timestamp") == "TIMESTAMP_NTZ"
    assert render_contract_data_type(duckdb, "decimal") is None
    assert duckdb.contract_proven is True
    assert postgres.contract_proven is False


def test_emission_shape_and_constraints() -> None:
    data = {
        "name": "dim_widgets",
        "generator": "dimension",
        "description": "Widget dimension",
        "input_models": ["hub_widget"],
        "natural_key": "widget_id",
        "surrogate_key": "widget_sk",
        "attribute_columns": ["widget_hk"],
        "column_types": {"widget_id": "integer", "widget_hk": "text"},
        "contract_enforced": True,
    }
    entry = model_properties_entry(
        data,
        get_dialect("duckdb"),
        not_null_columns=frozenset({"widget_id", "widget_sk"}),
    )
    assert entry is not None
    assert entry["config"] == {"contract": {"enforced": True}}
    columns = {c["name"]: c for c in entry["columns"]}
    assert all("data_type" in c for c in columns.values())
    assert columns["widget_hk"]["data_type"] == "VARCHAR"
    assert columns["widget_id"]["constraints"] == [{"type": "not_null"}]
    assert "constraints" not in columns["widget_hk"]
    assert "constraints" not in columns["__loaded_at"]


def test_no_config_without_opt_in() -> None:
    data = {
        "name": "dim_widgets",
        "generator": "dimension",
        "description": "Widget dimension",
        "input_models": ["hub_widget"],
        "natural_key": "widget_id",
        "surrogate_key": "widget_sk",
        "attribute_columns": ["widget_hk"],
    }
    entry = model_properties_entry(data, get_dialect("duckdb"))
    assert entry is not None and "config" not in entry


def test_scd2_constraints_dropped_and_warned(tmp_path: Path, capsys) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_widgets",
        "staging",
        {
            "description": "Widget staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "widgets"},
            "columns": [
                {"source": "widget_id", "alias": "widget_id"},
                {"source": "widget_name", "alias": "widget_name"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        tmp_path,
        "dim_widgets_scd2",
        "scd2_dimension",
        {
            "description": "SCD2 widgets",
            "owner": "analytics",
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
            "tracked_columns": ["widget_name"],
            "contract_enforced": True,
            "column_types": {"widget_id": "integer", "widget_name": "text"},
        },
        pattern="scd2_dimension",
    )
    assert main(["validate", "--project-dir", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "view, whose contract carries names and types only" in err

    entry = model_properties_entry(
        {
            "name": "dim_widgets_scd2",
            "generator": "scd2_dimension",
            "description": "SCD2 widgets",
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
            "tracked_columns": ["widget_name"],
            "column_types": {"widget_id": "integer", "widget_name": "text"},
            "contract_enforced": True,
        },
        get_dialect("duckdb"),
        not_null_columns=frozenset({"widget_id"}),
    )
    assert entry is not None
    assert entry["config"] == {"contract": {"enforced": True}}
    assert all("constraints" not in c for c in entry["columns"])


def test_generate_emits_contract_into_single_document(
    tmp_path: Path, capsys
) -> None:
    _staging(tmp_path)
    _hub(tmp_path)
    _dimension(tmp_path, contract_enforced=True, column_types={"widget_id": "integer", "widget_hk": "text"})
    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    document = yaml.safe_load(
        (tmp_path / "models" / "_generated/tests" / "data_dim_widgets.yml").read_text(
            encoding="utf-8"
        )
    )
    (model,) = document["models"]
    assert model["config"] == {"contract": {"enforced": True}}
    columns = {c["name"]: c for c in model["columns"]}
    assert all("data_type" in c for c in columns.values())
    # Constraints track the data tests in the same document exactly.
    for name, column in columns.items():
        has_not_null_test = "not_null" in (column.get("tests") or [])
        has_constraint = column.get("constraints") == [{"type": "not_null"}]
        assert has_constraint == has_not_null_test, name
    assert columns["widget_id"]["constraints"] == [{"type": "not_null"}]


def test_generate_warns_on_unproven_dialect(tmp_path: Path, capsys) -> None:
    _staging(tmp_path)
    _hub(tmp_path)
    _dimension(tmp_path, contract_enforced=True, column_types={"widget_id": "integer", "widget_hk": "text"})
    rc = main(
        ["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "snowflake"]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "contract enforcement is unproven on snowflake" in err
