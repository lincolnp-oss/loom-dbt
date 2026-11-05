"""Metadata-declared dbt unit tests (ADR 0029).

The validator in two layers: the dedicated record-level shape
checks and the relationship-phase cross-record checks (uniqueness,
upstream resolution, resolver-based column subsets, the
persistent-hub declaration). Projects are built with the shared
harness in `tests/metadata_helpers.py`.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern, write_pattern_instance

VALID_TESTS: list[dict[str, Any]] = [
    {
        "name": "dim_widgets__keys_in_order",
        "given": [
            {
                "input": "hub_widget",
                "rows": [
                    {"widget_id": 2, "widget_hk": "h2"},
                    {"widget_id": 1, "widget_hk": "h1"},
                ],
            }
        ],
        "expect": {
            "rows": [
                {"widget_sk": 1, "widget_id": 1, "widget_hk": "h1"},
                {"widget_sk": 2, "widget_id": 2, "widget_hk": "h2"},
            ]
        },
    }
]


def _staging(project: Path, columns: list | None = None) -> None:
    write_pattern_instance(
        project,
        "stg_widgets",
        "staging",
        {
            "description": "Widget staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "widgets"},
            "columns": columns or [{"source": "widget_id", "alias": "widget_id"}],
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


def _project(project: Path, tests: list | None = VALID_TESTS) -> Path:
    _staging(project)
    _hub(project)
    _dimension(project, unit_tests=tests)
    return project


def _errors(project: Path, tests: list | None) -> str:
    _project(project, tests)
    with pytest.raises(LoomError) as caught:
        validate_project(project)
    return str(caught.value)


def _mutated(mutator) -> list:
    import copy

    tests = copy.deepcopy(VALID_TESTS)
    mutator(tests)
    return tests


def test_valid_unit_test_validates(tmp_path: Path) -> None:
    entries = validate_project(_project(tmp_path))
    assert any(e.data.get("name") == "dim_widgets" for e in entries)


def test_unit_tests_must_be_non_empty(tmp_path: Path) -> None:
    assert "'unit_tests' must be a non-empty list" in _errors(tmp_path, [])


def test_unknown_nested_keys_rejected(tmp_path: Path) -> None:
    tests = _mutated(lambda t: t[0].update(bogus_key=1))
    message = _errors(tmp_path, tests)
    assert "unknown key(s): bogus_key" in message
    assert "accepted keys are:" in message


def test_test_name_shape_enforced(tmp_path: Path) -> None:
    tests = _mutated(lambda t: t[0].update(name="DimWidgets Keys"))
    message = _errors(tmp_path, tests)
    assert "must be a lowercase snake_case identifier" in message


def test_duplicate_names_across_project_rejected(tmp_path: Path) -> None:
    _staging(tmp_path)
    _hub(
        tmp_path,
        unit_tests=[
            {
                "name": "dim_widgets__keys_in_order",
                "given": [{"input": "stg_widgets", "rows": [{"widget_id": 1}]}],
                "expect": {"rows": [{"widget_id": 1}]},
            }
        ],
    )
    _dimension(tmp_path, unit_tests=VALID_TESTS)
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "must be unique across the project" in str(caught.value)


def test_given_input_must_be_a_declared_upstream(tmp_path: Path) -> None:
    tests = _mutated(lambda t: t[0]["given"][0].update(input="hub_widget_typo"))
    assert "does not name a model in the inventory" in _errors(tmp_path, tests)

    tests = _mutated(lambda t: t[0]["given"][0].update(input="dim_widgets"))
    assert "is not a declared upstream of 'dim_widgets'" in _errors(
        tmp_path, tests
    )


def test_given_and_expect_columns_subset_checked(tmp_path: Path) -> None:
    tests = _mutated(
        lambda t: t[0]["given"][0]["rows"][0].update(widget_hx="h2")
    )
    message = _errors(tmp_path, tests)
    assert "given rows name column 'widget_hx'" in message
    assert "does not generate" in message

    tests = _mutated(lambda t: t[0]["expect"]["rows"][0].update(widget_sx=1))
    assert "expect rows name column 'widget_sx'" in _errors(tmp_path, tests)


def test_sentinel_rejected(tmp_path: Path) -> None:
    tests = _mutated(
        lambda t: t[0]["given"][0]["rows"][0].update(widget_hk="__TODO__")
    )
    message = _errors(tmp_path, tests)
    assert "scaffold sentinel '__TODO__'" in message
    assert "dim_widgets__keys_in_order" in message


def test_unquoted_date_rejected_with_quoting_fix(tmp_path: Path) -> None:
    tests = _mutated(
        lambda t: t[0]["given"][0]["rows"][0].update(
            widget_hk=datetime.date(2026, 1, 1)
        )
    )
    message = _errors(tmp_path, tests)
    assert "unquoted" in message and "quote it as an ISO string" in message


def test_nested_values_rejected(tmp_path: Path) -> None:
    tests = _mutated(
        lambda t: t[0]["given"][0]["rows"][0].update(widget_hk=["h2"])
    )
    message = _errors(tmp_path, tests)
    assert "nested lists and mappings are not supported" in message


def test_csv_format_accepted_and_checked(tmp_path: Path) -> None:
    tests = [
        {
            "name": "dim_widgets__csv_case",
            "given": [
                {
                    "input": "hub_widget",
                    "format": "csv",
                    "rows": "widget_id,widget_hk\n2,h2\n1,h1\n",
                }
            ],
            "expect": {
                "format": "csv",
                "rows": "widget_sk,widget_id,widget_hk\n1,1,h1\n",
            },
        }
    ]
    validate_project(_project(tmp_path, tests))  # must not raise

    bad = [
        {
            "name": "dim_widgets__csv_case",
            "given": [
                {
                    "input": "hub_widget",
                    "format": "csv",
                    "rows": "widget_id,widget_hk\n2,h2,extra\n",
                }
            ],
            "expect": {"rows": [{"widget_sk": 1}]},
        }
    ]
    message = _errors(tmp_path, bad)
    assert "same number of fields as the header" in message


def test_overrides_restricted_to_is_incremental(tmp_path: Path) -> None:
    tests = _mutated(
        lambda t: t[0].update(
            overrides={"macros": {"is_incremental": False, "current_timestamp": True}}
        )
    )
    message = _errors(tmp_path, tests)
    assert "the only macro override supported in v1 is: is_incremental" in message


def test_scd2_remains_excluded_with_its_reason(tmp_path: Path) -> None:
    # The one exclusion that survives the 2026-11-04 extension
    # ruling: dbt unit tests target models, and the model under
    # test for scd2_dimension is a snapshot.
    write_pattern_instance(
        tmp_path,
        "dim_widgets_scd2",
        "scd2_dimension",
        {
            "description": "Widget history",
            "owner": "analytics",
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
            "tracked_columns": ["widget_name"],
            "unit_tests": [
                {
                    "name": "dim_widgets_scd2__x",
                    "given": [{"input": "stg_widgets", "rows": []}],
                    "expect": {"rows": []},
                }
            ],
        },
        pattern="scd2_dimension",
    )
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "'unit_tests' is not supported for generator 'scd2_dimension' in v1" in str(
        caught.value
    )
    assert "the model under test is a dbt snapshot" in str(caught.value)


def _staging_with_tests(tmp_path: Path, given_input: str) -> None:
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
            "unit_tests": [
                {
                    "name": "stg_widgets__rows_pass_through",
                    "given": [
                        {
                            "input": given_input,
                            "rows": [{"widget_id": 1, "widget_name": "alpha"}],
                        }
                    ],
                    "expect": {"rows": [{"widget_id": 1, "widget_name": "alpha"}]},
                }
            ],
        },
        pattern="staging",
    )


def test_staging_unit_tests_name_the_models_own_source(tmp_path: Path) -> None:
    # Extension ruling (2026-11-04): a staging test's given input
    # names the model's source table; validation accepts it.
    _staging_with_tests(tmp_path, "widgets")
    validate_project(tmp_path)


def test_staging_unit_test_given_must_match_the_declared_source(
    tmp_path: Path,
) -> None:
    _staging_with_tests(tmp_path, "gadgets")
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    message = str(caught.value)
    assert "given input 'gadgets' does not name this model's source table" in message
    assert "source 'raw', table 'widgets'" in message


def test_bronze_unit_tests_name_the_models_own_source(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "bronze_widgets",
        "bronze",
        {
            "description": "Widgets as landed",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "widgets"},
            "unit_tests": [
                {
                    "name": "bronze_widgets__rows_land",
                    "given": [
                        {"input": "widgets", "rows": [{"widget_id": 1}]}
                    ],
                    "expect": {"rows": [{"widget_id": 1}]},
                }
            ],
        },
        pattern="bronze",
    )
    validate_project(tmp_path)


def test_satellite_unit_tests_validate(tmp_path: Path) -> None:
    # Satellite was the wiring gap the ruling closed: its given
    # input is its inventory input model, in ref form, like the
    # other Data Vault generators.
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
        "hub_widget",
        "hub",
        {
            "description": "Widget hub",
            "owner": "analytics",
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
        },
        pattern="hub",
    )
    write_pattern_instance(
        tmp_path,
        "sat_widget_details",
        "satellite",
        {
            "description": "Widget names as history",
            "owner": "analytics",
            "parent": {"model": "hub_widget", "key": "widget_id"},
            "input_models": ["stg_widgets"],
            "record_source": "erp",
            "attribute_columns": ["widget_name"],
            "unit_tests": [
                {
                    "name": "sat_widget_details__name_is_held",
                    "given": [
                        {
                            "input": "stg_widgets",
                            "rows": [{"widget_id": 1, "widget_name": "alpha"}],
                        }
                    ],
                    "expect": {"rows": [{"widget_name": "alpha"}]},
                    "overrides": {"macros": {"is_incremental": True}},
                }
            ],
        },
        pattern="satellite",
    )
    validate_project(tmp_path)


def test_pattern_defaults_may_not_define_unit_tests(tmp_path: Path) -> None:
    definition_dir = tmp_path / "metadata" / "patterns"
    definition_dir.mkdir(parents=True)
    (tmp_path / "metadata" / "instances").mkdir(parents=True)
    (definition_dir / "dimension.yml").write_text(
        yaml.safe_dump(
            {
                "name": "dimension",
                "generator": "dimension",
                "defaults": {"unit_tests": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    write_pattern(
        tmp_path,
        "dimension",
        {
            "pattern": "dimension",
            "instances": [
                {
                    "name": "dim_widgets",
                    "params": {
                        "input_models": ["hub_widget"],
                        "natural_key": "widget_id",
                        "surrogate_key": "widget_sk",
                        "attribute_columns": ["widget_hk"],
                    },
                }
            ],
        },
    )
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "defaults must not define 'unit_tests'" in str(caught.value)


def test_persistent_hub_requires_is_incremental_declaration(
    tmp_path: Path,
) -> None:
    hub_tests = [
        {
            "name": "hub_widget__first_load",
            "given": [{"input": "stg_widgets", "rows": [{"widget_id": 1}]}],
            "expect": {"rows": [{"widget_id": 1}]},
        }
    ]
    _staging(tmp_path)
    _hub(tmp_path, retention="persistent", unit_tests=hub_tests)
    with pytest.raises(LoomError) as caught:
        validate_project(tmp_path)
    assert "must declare overrides.macros.is_incremental" in str(caught.value)

    declared = [
        {
            **hub_tests[0],
            "overrides": {"macros": {"is_incremental": False}},
        }
    ]
    _hub(tmp_path, retention="persistent", unit_tests=declared)
    validate_project(tmp_path)  # must not raise


def test_name_convention_warns_not_fails(tmp_path: Path, capsys) -> None:
    from loom.cli import main

    tests = _mutated(lambda t: t[0].update(name="keys_in_order"))
    project = _project(tmp_path, tests)
    assert main(["validate", "--project-dir", str(project)]) == 0
    err = capsys.readouterr().err
    assert "does not start" in err
    assert re.search(r"warning:.*keys_in_order", err)


def test_expect_stamp_warns_not_fails(tmp_path: Path, capsys) -> None:
    from loom.cli import main

    # A hub with record_source emits __loaded_at, so pinning it in
    # expect is legal but brittle: a warning, not an error.
    _staging(
        tmp_path,
        columns=[
            {"source": "widget_id", "alias": "widget_id"},
            {"source": "src", "alias": "record_source"},
        ],
    )
    _hub(
        tmp_path,
        retention="persistent",
        record_source="record_source",
        unit_tests=[
            {
                "name": "hub_widget__pins_stamp",
                "overrides": {"macros": {"is_incremental": False}},
                "given": [
                    {
                        "input": "stg_widgets",
                        "rows": [{"widget_id": 1, "record_source": "a"}],
                    }
                ],
                "expect": {
                    "rows": [
                        {"widget_id": 1, "__loaded_at": "2026-01-01 00:00:00"}
                    ]
                },
            }
        ],
    )
    assert main(["validate", "--project-dir", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "pins '__loaded_at'" in err
    assert "will be brittle" in err
