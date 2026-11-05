"""`loom check` verifies generated instances' macro references
against the manifest (Tier 1)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from loom.cli import main

ROOT = Path(__file__).parents[1]
QUICKSTART = ROOT / "examples" / "quickstart"


def _project(
    tmp_path: Path, macros: dict | None, column_param: int | None = None
) -> Path:
    project = tmp_path / "mini"
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir(parents=True)
    (project / "models" / "_generated" / "staging").mkdir(parents=True)
    (project / "target").mkdir()
    (project / "dbt_project.yml").write_text("name: mini\n", encoding="utf-8")
    shutil.copy(
        QUICKSTART / "metadata" / "patterns" / "staging.yml",
        project / "metadata" / "patterns" / "staging.yml",
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "format_version: '1.0'\n"
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_events\n"
        "    params:\n"
        "      description: Events staging.\n"
        "      source_ref: {name: raw, table: events}\n"
        "      columns:\n"
        "        - {source: id, alias: id}\n"
        "        - source: created_at\n"
        "          alias: created_date\n"
        "          macro: widgets.format_date\n"
        "          macro_args: ['iso']\n"
        + (
            f"          macro_column_param: {column_param}\n"
            if column_param is not None
            else ""
        ),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: events\n",
        encoding="utf-8",
    )
    sql = "select id, created_at from raw.events\n"
    model_file = project / "models/_generated/staging/stg_events.sql"
    model_file.write_text(sql, encoding="utf-8")
    document = {
        "metadata": {
            "project_name": "mini",
            "generated_at": "2999-01-01T00:00:00+00:00",
        },
        "nodes": {
            "model.mini.stg_events": {
                "resource_type": "model",
                "package_name": "mini",
                "name": "stg_events",
                "original_file_path": (
                    "models/_generated/staging/stg_events.sql"
                ),
                "raw_code": sql,
                "depends_on": {"nodes": []},
                "columns": {},
                "config": {"materialized": "view"},
            }
        },
        "sources": {},
        "macros": macros or {},
    }
    (project / "target" / "manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return project


_FORMAT_DATE = {
    "name": "format_date",
    "package_name": "widgets",
    "arguments": [{"name": "column_name"}, {"name": "fmt"}],
    "macro_sql": "{% macro format_date(column_name, fmt) %}x{% endmacro %}",
}


def test_check_verifies_instance_macro_against_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, {"macro.widgets.format_date": _FORMAT_DATE})
    assert main(["check", "--project-dir", str(project)]) == 0
    assert "Inventory coverage is current." in capsys.readouterr().out


def test_check_fails_for_macro_missing_from_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, None)
    assert main(["check", "--project-dir", str(project)]) == 1
    err = capsys.readouterr().err
    assert "columns[1].macro" in err
    assert "'widgets.format_date' is not defined in the manifest" in err


def test_check_fails_for_macro_arity_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    macro = dict(_FORMAT_DATE)
    macro["arguments"] = [{"name": "column_name"}]
    macro["macro_sql"] = "{% macro format_date(column_name) %}x{% endmacro %}"
    project = _project(tmp_path, {"macro.widgets.format_date": macro})
    assert main(["check", "--project-dir", str(project)]) == 1
    assert "but the call supplies 2" in capsys.readouterr().err


def test_check_verifies_tier2_column_position_against_signature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Tier 2: the column fills position 2 (fmt) and the single
    # macro_arg fills position 1 - within the 2-parameter
    # signature, so the reference verifies.
    project = _project(
        tmp_path, {"macro.widgets.format_date": _FORMAT_DATE}, column_param=2
    )
    assert main(["check", "--project-dir", str(project)]) == 0
    assert "Inventory coverage is current." in capsys.readouterr().out

