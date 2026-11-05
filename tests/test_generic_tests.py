"""`generic_tests` carriage: validation shape and the
properties-opt-out conflict."""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance


def _project(tmp_path: Path, params_extra: dict) -> Path:
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Generic tests validation",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "customer"},
            "columns": [{"source": "id", "alias": "customer_id"}],
            **params_extra,
        },
        pattern="staging",
    )
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: customer\n",
        encoding="utf-8",
    )
    return tmp_path


def test_generic_tests_declaration_validates(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {
            "generic_tests": [
                {"test": "not_null", "column_name": "customer_id"},
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {"to": "ref('customers')", "field": "id"},
                    "config": {"severity": "warn"},
                },
                {"test": "dbt_utils.recency"},
            ]
        },
    )
    entries = validate_project(project)
    assert entries[0].data["generic_tests"][1]["kwargs"]["field"] == "id"


@pytest.mark.parametrize(
    "declaration, fragment",
    [
        ([], "must be a non-empty list when declared"),
        ([{"column_name": "customer_id"}], ".test is required"),
        ([{"test": "two words"}], "is not a safe test reference"),
        ([{"test": "ok", "bogus": 1}], "has unknown key(s): bogus"),
        ([{"test": "ok", "column_name": "9bad"}], "must be a safe SQL identifier"),
        ([{"test": "ok", "kwargs": ["no"]}], ".kwargs must be a mapping"),
        ([{"test": "ok", "config": "warn"}], ".config must be a mapping"),
        (
            [{"test": "ok", "kwargs": {"__TODO__": "args"}}],
            "carries the '__TODO__' sentinel",
        ),
        (
            [
                {"test": "unique", "column_name": "customer_id"},
                {"test": "unique", "column_name": "customer_id"},
            ],
            "exactly duplicates generic_tests[0]",
        ),
    ],
)
def test_generic_tests_shape_errors(
    tmp_path: Path, declaration: list, fragment: str
) -> None:
    project = _project(tmp_path, {"generic_tests": declaration})
    with pytest.raises(LoomError) as error:
        validate_project(project)
    assert fragment in str(error.value)


def test_same_test_with_differing_kwargs_is_not_a_duplicate(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        {
            "generic_tests": [
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {"to": "ref('a')", "field": "id"},
                },
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {"to": "ref('b')", "field": "id"},
                },
            ]
        },
    )
    validate_project(project)


def test_reserved_model_kwarg_rejected_in_direct_kwargs(
    tmp_path: Path,
) -> None:
    # A hand declaration copying the manifest's declared
    # form carries dbt's injected `model` kwarg. Rendered
    # verbatim it fails `dbt parse` ('Test arguments include
    # "model", which is a reserved argument'); validation
    # rejects it here instead.
    project = _project(
        tmp_path,
        {
            "generic_tests": [
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {
                        "to": "ref('customers')",
                        "field": "id",
                        "model": "ref('stg_customer')",
                    },
                }
            ]
        },
    )
    with pytest.raises(LoomError) as error:
        validate_project(project)
    message = str(error.value)
    assert "kwargs declares 'model'" in message
    assert "reserved argument" in message


def test_reserved_model_kwarg_rejected_in_arguments_wrapper(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        {
            "generic_tests": [
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {
                        "arguments": {
                            "to": "ref('customers')",
                            "field": "id",
                            "model": "ref('stg_customer')",
                        }
                    },
                }
            ]
        },
    )
    with pytest.raises(LoomError) as error:
        validate_project(project)
    assert "kwargs.arguments declares 'model'" in str(error.value)


def test_column_name_kwarg_is_not_rejected(tmp_path: Path) -> None:
    # Scope precision: dbt reserves `model` only. A
    # `column_name` kwarg is accepted by `dbt parse` (it is
    # redundant beside the declaration's own column, not
    # reserved), so validation must not reject it - in
    # either kwargs form.
    project = _project(
        tmp_path,
        {
            "generic_tests": [
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {
                        "to": "ref('customers')",
                        "field": "id",
                        "column_name": "customer_id",
                    },
                },
                {
                    "test": "relationships",
                    "column_name": "customer_id",
                    "kwargs": {
                        "arguments": {
                            "to": "ref('customers')",
                            "field": "id",
                            "column_name": "customer_id",
                        }
                    },
                },
            ]
        },
    )
    validate_project(project)


def test_carriage_conflicts_with_properties_optout(tmp_path: Path) -> None:
    # The carriage/opt-out interaction, resolved as a validation
    # error naming both keys - carriage needs the document the
    # opt-out suppresses.
    project = _project(
        tmp_path,
        {
            "skip_properties_document": True,
            "generic_tests": [{"test": "not_null", "column_name": "customer_id"}],
        },
    )
    with pytest.raises(LoomError) as error:
        validate_project(project)
    message = str(error.value)
    assert "'generic_tests' cannot be declared" in message
    assert "'skip_properties_document' is true" in message


def test_generic_tests_barred_from_pattern_defaults(tmp_path: Path) -> None:
    import yaml

    project = _project(tmp_path, {})
    definition = project / "metadata" / "patterns" / "staging.yml"
    document = yaml.safe_load(definition.read_text(encoding="utf-8"))
    document["defaults"] = {"generic_tests": [{"test": "not_null"}]}
    definition.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(LoomError) as error:
        validate_project(project)
    assert "defaults must not define 'generic_tests'" in str(error.value)
