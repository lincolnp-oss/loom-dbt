"""`loom create-new data-test`: the metadata form for
generated models, the dbt-YAML form for hand-written ones, and
the honesty discipline both share."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.cli import main
from metadata_helpers import write_pattern_instance


def _generated_project(tmp_path: Path, extra: dict | None = None) -> Path:
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "x",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "customer"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "name", "alias": "customer_name"},
            ],
            **(extra or {}),
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


def test_metadata_form_for_a_no_argument_test(tmp_path, capsys) -> None:
    project = _generated_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "stg_customer",
            "--test", "not_null", "--column", "customer_id",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == (
        "generic_tests:\n"
        "  - test: not_null\n"
        "    column_name: customer_id\n"
    )


def test_metadata_form_carries_the_kwargs_sentinel(tmp_path, capsys) -> None:
    # A test whose arguments Loom cannot know: the sentinel
    # stands where the kwargs go - and the carriage validator
    # rejects that key, so the snippet cannot validate
    # unfinished (the rejection itself is pinned in
    # test_generic_tests.py).
    project = _generated_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "stg_customer",
            "--test", "relationships", "--column", "customer_id",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "kwargs: {__TODO__:" in out
    assert "replace with the test's arguments" in out


def test_metadata_form_model_level_package_test(tmp_path, capsys) -> None:
    project = _generated_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "stg_customer",
            "--test", "dbt_utils.expression_is_true",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "- test: dbt_utils.expression_is_true\n" in out
    assert "column_name" not in out
    assert "__TODO__" in out


def test_unknown_column_refused_naming_the_derivable_columns(
    tmp_path, capsys
) -> None:
    project = _generated_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "stg_customer",
            "--test", "not_null", "--column", "nope",
            "--project-dir", str(project),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "has no output column 'nope'" in err
    assert "customer_id, customer_name" in err


def test_optout_model_warns_about_the_carriage_conflict(
    tmp_path, capsys
) -> None:
    project = _generated_project(
        tmp_path, {"skip_properties_document": True}
    )
    rc = main(
        [
            "create-new", "data-test", "stg_customer",
            "--test", "unique", "--column", "customer_id",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "generic_tests:" in captured.out
    assert "conflicts with that opt-out" in captured.err


def test_argument_discipline(tmp_path, capsys) -> None:
    project = _generated_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "stg_customer",
            "--test", "two words", "--project-dir", str(project),
        ]
    )
    assert rc == 1
    assert "is not a safe test reference" in capsys.readouterr().err
    rc = main(
        [
            "create-new", "unit-test", "stg_customer",
            "--test", "not_null", "--project-dir", str(project),
        ]
    )
    assert rc == 1
    assert "belong to the 'data-test' kind" in capsys.readouterr().err


def _handwritten_project(tmp_path: Path, *, documented: bool = True) -> Path:
    project = tmp_path / "hw"
    (project / "models").mkdir(parents=True)
    (project / "target").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: hw_project\n", encoding="utf-8"
    )
    (project / "models" / "orders.sql").write_text(
        "select 1\n", encoding="utf-8"
    )
    columns = {"order_id": {}, "total": {}} if documented else {}
    document = {
        "metadata": {
            "project_name": "hw_project",
            "generated_at": "2999-01-01T00:00:00+00:00",
        },
        "nodes": {
            "model.hw_project.orders": {
                "resource_type": "model",
                "package_name": "hw_project",
                "name": "orders",
                "original_file_path": "models/orders.sql",
                "raw_code": "select 1\n",
                "depends_on": {"nodes": []},
                "columns": columns,
            }
        },
        "sources": {},
    }
    (project / "target" / "manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return project


def test_dbt_yaml_form_for_a_handwritten_model(tmp_path, capsys) -> None:
    project = _handwritten_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "orders",
            "--test", "unique", "--column", "order_id",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == (
        "models:\n"
        "  - name: orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        tests:\n"
        "          - unique\n"
    )


def test_dbt_yaml_form_sentinel_and_model_level(tmp_path, capsys) -> None:
    project = _handwritten_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "orders",
            "--test", "accepted_values", "--column", "total",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "- accepted_values:\n" in out
    assert '__TODO__: "replace with the test\'s arguments"' in out
    rc = main(
        [
            "create-new", "data-test", "orders",
            "--test", "not_null", "--project-dir", str(project),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "    tests:\n      - not_null\n" in out


def test_dbt_yaml_unknown_column_refused(tmp_path, capsys) -> None:
    project = _handwritten_project(tmp_path)
    rc = main(
        [
            "create-new", "data-test", "orders",
            "--test", "unique", "--column", "nope",
            "--project-dir", str(project),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "documents no column 'nope'" in err
    assert "order_id, total" in err


def test_dbt_yaml_undocumented_columns_print_with_a_comment(
    tmp_path, capsys
) -> None:
    project = _handwritten_project(tmp_path, documented=False)
    rc = main(
        [
            "create-new", "data-test", "orders",
            "--test", "unique", "--column", "anything",
            "--project-dir", str(project),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "could not be verified" in out
    assert "- name: anything" in out
