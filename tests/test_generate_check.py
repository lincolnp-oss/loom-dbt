"""`loom generate --check` reports generated-file drift.

The check re-renders everything in memory and compares against disk,
writing nothing. It exits 1 when a file is missing or differs - which
covers both a hand-edited generated file and metadata that was edited
without regenerating. SCD2 outputs are create-only during normal
generation, so the check plans them unconditionally and calls out
their stale state explicitly.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from loom.cli import main

ROOT = Path(__file__).parents[1]
QUICKSTART = ROOT / "examples" / "quickstart"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "quickstart"
    shutil.copytree(
        QUICKSTART,
        project,
        ignore=shutil.ignore_patterns("target", "*.duckdb"),
    )
    return project


def _generate(project: Path) -> int:
    return main(
        ["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]
    )


def _check(project: Path) -> int:
    return main(
        [
            "generate",
            "--check",
            "--project-dir",
            str(project),
            "--dialect",
            "duckdb",
        ]
    )


def test_check_passes_on_a_freshly_generated_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert _generate(project) == 0
    capsys.readouterr()
    assert _check(project) == 0
    assert "are current" in capsys.readouterr().out


def test_check_reports_a_hand_edited_generated_file_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert _generate(project) == 0
    target = project / "models" / "vault" / "hub_customer.sql"
    edited = target.read_text(encoding="utf-8") + "\n-- hand edit\n"
    target.write_text(edited, encoding="utf-8")
    capsys.readouterr()
    assert _check(project) == 1
    out = capsys.readouterr().out
    assert "differs: models/vault/hub_customer.sql" in out
    # The check is read-only: the edit is still there afterwards.
    assert target.read_text(encoding="utf-8") == edited


def test_check_reports_a_missing_generated_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert _generate(project) == 0
    (project / "models" / "vault" / "link_order_customer.sql").unlink()
    capsys.readouterr()
    assert _check(project) == 1
    assert "missing: models/vault/link_order_customer.sql" in capsys.readouterr().out


def test_check_flags_stale_scd2_create_only_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Editing SCD2 metadata cannot be fixed by plain generate (the
    files are create-only), so the check must say so explicitly."""
    project = _project(tmp_path)
    assert _generate(project) == 0
    # Change what the snapshot renders: track an extra column.
    import yaml

    instance = project / "metadata" / "instances" / "scd2.yml"
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    for entry in data["instances"]:
        if entry["name"] == "dim_customers_scd2":
            assert entry["params"]["tracked_columns"] == [
                "customer_name",
                "customer_email",
            ]
            entry["params"]["tracked_columns"] = ["customer_name"]
    instance.write_text(yaml.safe_dump(data), encoding="utf-8")
    capsys.readouterr()
    assert _check(project) == 1
    out = capsys.readouterr().out
    assert "differs: snapshots/vault/dim_customers_scd2.sql" in out
    assert "create-only" in out


def test_check_honours_select(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert _generate(project) == 0
    target = project / "models" / "vault" / "hub_customer.sql"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8"
    )
    capsys.readouterr()
    # An unrelated selection does not see the drift.
    assert (
        main(
            [
                "generate",
                "--check",
                "--select",
                "hub_order",
                "--project-dir",
                str(project),
                "--dialect",
                "duckdb",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "generate",
                "--check",
                "--select",
                "hub_customer",
                "--project-dir",
                str(project),
                "--dialect",
                "duckdb",
            ]
        )
        == 1
    )
