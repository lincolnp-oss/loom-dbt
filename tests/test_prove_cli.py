"""`loom prove` - the static equivalence stage's command
surface: not-run is a report with exit 0, run
verdicts render as the proof record in text and JSON, and a
malformed manifest is an error."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.cli import main


def _manifests(
    project: Path,
    with_candidate_sql: str,
    candidate_columns: list[str] | None = None,
) -> None:
    directory = project / "local" / "equivalence"
    directory.mkdir(parents=True)

    def manifest(sql: str, columns: list[str]) -> str:
        return json.dumps(
            {
                "metadata": {
                    "project_name": "proj",
                    "adapter_type": "duckdb",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                },
                "nodes": {
                    "model.proj.stg_events": {
                        "name": "stg_events",
                        "resource_type": "model",
                        "package_name": "proj",
                        "compiled_code": sql,
                        "columns": {column: {} for column in columns},
                        "config": {"materialized": "view"},
                    }
                },
            }
        )

    (directory / "original.manifest.json").write_text(
        manifest("select id, name from raw.events", ["id", "name"]),
        encoding="utf-8",
    )
    (directory / "candidate.manifest.json").write_text(
        manifest(
            with_candidate_sql,
            candidate_columns or ["id", "name", "__loaded_at"],
        ),
        encoding="utf-8",
    )


def test_prove_reports_not_run_without_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["prove", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Static equivalence: not run" in out
    assert "local/equivalence/original.manifest.json" in out
    assert "local/equivalence/candidate.manifest.json" in out
    # B-1: the warehouse comparison lives at playbook Step 6
    # (Step 3 is "Register the rest").
    assert "Step 6" in out
    assert "Step 3" not in out


def test_prove_renders_verdicts_and_json_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifests(
        tmp_path,
        "select id, name, localtimestamp as __loaded_at from raw.events",
    )
    assert main(["prove", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Static equivalence: 1 proven, 0 inconclusive of 1 models." in out
    assert "stg_events: proven" in out

    assert (
        main(["prove", "--project-dir", str(tmp_path), "--format", "json"])
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["stage"] == "run"
    assert record["summary"] == {"subjects": 1, "proven": 1, "inconclusive": 0}
    assert record["models"][0]["verdict"] == "proven"
    assert record["original"]["adapter"] == "duckdb"


def test_prove_text_carries_the_documentation_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # B-2: a candidate column with no documented counterpart on
    # the original side marks the original's documentation
    # partial, and the text report says so before its Next line.
    _manifests(
        tmp_path,
        "select id, name, nickname from raw.events",
        candidate_columns=["id", "name", "nickname"],
    )
    assert main(["prove", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "no documented counterpart: candidate column" in out
    assert "documentation is partial for 1 of 1 compared models" in out
    assert "documented columns only" in out


def test_prove_errors_on_a_malformed_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "local" / "equivalence"
    directory.mkdir(parents=True)
    (directory / "original.manifest.json").write_text("{nope", encoding="utf-8")
    (directory / "candidate.manifest.json").write_text("{}", encoding="utf-8")
    assert main(["prove", "--project-dir", str(tmp_path)]) == 1
    assert "cannot read the manifest" in capsys.readouterr().err
