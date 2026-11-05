"""Validation of column_docs / column_types metadata.

Shape and vocabulary checks are record-level; the named-column
check runs where the model's output columns are derivable. Where
they are not (bronze), declarations are the practitioner's own
knowledge and validate unchecked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance


def _write_hub(project: Path) -> None:
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "customers"},
            "columns": [{"source": "customer_id", "alias": "customer_id"}],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            "description": "Customer hub",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )


def _write_dimension(project: Path, **extra: object) -> None:
    write_pattern_instance(
        project,
        "dim_customers",
        "dimension",
        {
            "description": "Customer dimension",
            "owner": "analytics",
            "input_models": ["hub_customer"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_hk"],
            **extra,
        },
        pattern="dimension",
    )


def test_column_docs_on_real_columns_validate(tmp_path: Path) -> None:
    _write_hub(tmp_path)
    _write_dimension(
        tmp_path,
        column_docs={"customer_hk": "Vault hash", "customer_id": "Natural key"},
        column_types={"customer_id": "integer", "__loaded_at": "timestamp"},
    )
    entries = validate_project(tmp_path)
    assert len(entries) == 3


def test_column_docs_unknown_column_fails(tmp_path: Path) -> None:
    _write_hub(tmp_path)
    _write_dimension(tmp_path, column_docs={"custmer_hk": "typo"})
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert "column_docs names column(s)" in str(error.value)
    assert "custmer_hk" in str(error.value)


def test_column_types_unknown_column_fails(tmp_path: Path) -> None:
    _write_hub(tmp_path)
    _write_dimension(tmp_path, column_types={"record_source": "text"})
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert "column_types names column(s)" in str(error.value)


def test_column_types_must_use_generic_vocabulary(tmp_path: Path) -> None:
    _write_hub(tmp_path)
    _write_dimension(tmp_path, column_types={"customer_id": "varchar(50)"})
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert "is not a generic data type" in str(error.value)


def test_column_types_bare_decimal_fails(tmp_path: Path) -> None:
    _write_hub(tmp_path)
    _write_dimension(tmp_path, column_types={"customer_id": "decimal"})
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert "decimal(precision,scale)" in str(error.value)


def test_column_docs_values_must_be_non_empty_strings(tmp_path: Path) -> None:
    _write_hub(tmp_path)
    _write_dimension(tmp_path, column_docs={"customer_id": 5})
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert "column_docs.customer_id must be a non-empty string" in str(error.value)


def test_bronze_declarations_are_not_subset_checked(tmp_path: Path) -> None:
    """Bronze outputs are source columns Loom cannot see: a declared
    doc/type for any of them validates (the SELECT * honesty rule)."""
    write_pattern_instance(
        tmp_path,
        "bronze_customers",
        "bronze",
        {
            "description": "Raw customers",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "customers"},
            "column_docs": {"anything_from_source": "Declared by the practitioner"},
            "column_types": {"anything_from_source": "text"},
        },
        pattern="bronze",
    )
    entries = validate_project(tmp_path)
    assert len(entries) == 1
