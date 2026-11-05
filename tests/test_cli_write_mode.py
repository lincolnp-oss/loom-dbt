"""The CLI write-mode convention (dry-run by default).

Pins the package's load-bearing behaviours: mutating commands
(generate, register, convert, snapshot-sources) preview by
default and write only under --write or LOOM_WRITE; the flag
wins over the variable and the mode banner names the mode's
source; previews end with the apply hint and the shared
would-write / would-change / unchanged counts; --check keeps
its precedence as the drift mode; --format/--verbose reach
every command; colour flows through the one path (terminal
only, NO_COLOR opts out, JSON never coloured); and every
command's help carries the standard summary, description and
example.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.cli import _parser, _write_mode, main
from loom.reporting import (
    bold,
    colour_enabled,
    paint,
    set_colour_enabled,
)
from manifest_builders import manifest_node
from metadata_helpers import write_pattern_instance


@pytest.fixture(autouse=True)
def _no_env_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_WRITE", raising=False)


# --- Mode resolution -------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, (False, "default")),
        ("1", (True, "env")),
        ("true", (True, "env")),
        ("TRUE", (True, "env")),
        ("yes", (True, "env")),
        ("0", (False, "default")),
        ("false", (False, "default")),
        ("No", (False, "default")),
        (" no ", (False, "default")),
    ],
)
def test_write_mode_env_values(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: tuple
) -> None:
    if value is not None:
        monkeypatch.setenv("LOOM_WRITE", value)
    args = _parser().parse_args(["generate"])
    assert _write_mode(args) == expected


def test_write_mode_flag_wins_over_env_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOOM_WRITE", "0")
    args = _parser().parse_args(["generate", "--write"])
    assert _write_mode(args) == (True, "flag")
    args = _parser().parse_args(
        ["snapshot-sources", "--database", "x.duckdb"]
    )
    assert _write_mode(args) == (False, "default")


# --- Assessment fixtures (the migration suite's shape) ---------


def _model(name: str, **kwargs) -> dict:
    return manifest_node(
        name,
        package="legacy",
        deps=kwargs.get("deps"),
        raw_code=kwargs.get("raw", "select 1"),
        extra={
            "config": kwargs.get("config") or {},
            "columns": kwargs.get("columns") or {},
        },
    )


def _manifest(project: Path, nodes: dict, sources: dict | None = None) -> Path:
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        original, raw = node.get("original_file_path"), node.get("raw_code")
        if not isinstance(original, str) or not isinstance(raw, str):
            continue
        source = project / original
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists() or source.read_text(encoding="utf-8") != raw:
            source.write_text(raw, encoding="utf-8")
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata": {
            "project_name": "legacy",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "nodes": nodes,
        "sources": sources or {},
        "exposures": {},
    }
    path = target / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


_SOURCE = {
    "source.legacy.raw.orders": {
        "name": "orders",
        "source_name": "raw",
        "resource_type": "source",
    }
}


def _legacy_project(project: Path) -> None:
    nodes = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
        )
    }
    _manifest(project, nodes, _SOURCE)


# --- register / convert previews --------------------------------


def test_register_preview_then_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_project(tmp_path)
    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["mode_source"] == "default"
    assert report["drafts_preview"]["would_write"] == 1
    assert report["drafts_preview"]["would_change"] == 0
    assert report["drafts_preview"]["unchanged"] == 0
    assert report["apply_hint"].startswith("loom register --write")
    assert not (tmp_path / "local" / "registrations").exists()

    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert (
        out.splitlines()[0] == "Register: legacy - dry run - nothing written"
    )
    assert "Would write: 1 file(s) · Would change: 0 file(s)" in out
    assert out.rstrip().endswith(
        "loom register --write --project-dir " + str(tmp_path)
    )

    assert main(["register", "--project-dir", str(tmp_path), "--write"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Register: legacy - writing"
    assert (tmp_path / "local" / "registrations" / "stg_orders.yml").exists()

    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    # The written registration makes the model known: it leaves
    # the drafting pool, so a later preview has nothing to draft.
    assert report["drafts_preview"]["would_write"] == 0
    assert report["drafts_preview"]["files"] == []


def test_register_env_write_states_its_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOOM_WRITE", "1")
    _legacy_project(tmp_path)
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Register: legacy - writing (LOOM_WRITE)"
    assert (tmp_path / "local" / "registrations" / "stg_orders.yml").exists()


def test_convert_preview_counts_draft_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_project(tmp_path)
    assert (
        main(["convert", "--project-dir", str(tmp_path), "--format", "json"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    preview = report["drafts_preview"]
    # A record, an instance document and the synthesised staging
    # pattern: the whole draft set a write run would create.
    assert preview["would_write"] == 3
    assert preview["would_change"] == 0
    assert preview["unchanged"] == 0
    kinds = {entry["kind"] for entry in preview["files"]}
    assert kinds == {"record", "instance", "pattern"}
    assert not (tmp_path / "local" / "drafts").exists()

    assert main(["convert", "--project-dir", str(tmp_path), "--write"]) == 0
    capsys.readouterr()
    assert (tmp_path / "local" / "drafts" / "stg_orders.yml").exists()
    assert (
        main(["convert", "--project-dir", str(tmp_path), "--format", "json"])
        == 0
    )
    preview = json.loads(capsys.readouterr().out)["drafts_preview"]
    assert preview["would_write"] == 0
    assert preview["unchanged"] == 3


# --- generate modes ----------------------------------------------

_STAGING = {
    "description": "Customer staging.",
    "owner": "analytics",
    "inline_source": {"name": "raw", "schema": "main", "table": "customers"},
    "columns": [{"source": "id", "alias": "customer_id"}],
}


def _generate_project(project: Path) -> None:
    write_pattern_instance(
        project, "stg_customers", "staging", _STAGING, pattern="raw_staging"
    )


def test_generate_preview_and_write_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _generate_project(tmp_path)
    assert (
        main(
            [
                "generate",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["mode_source"] == "default"
    assert payload["counts"]["would_write"] >= 1
    assert payload["counts"]["would_change"] == 0
    assert payload["apply_hint"].startswith("loom generate --write")
    assert not (tmp_path / "models" / "_generated").exists()

    assert (
        main(
            [
                "generate",
                "--write",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "write"
    assert payload["counts"]["models"] == 1
    assert (
        tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    ).exists()


def test_generate_check_takes_precedence_over_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _generate_project(tmp_path)
    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    target = (
        tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    )
    target.unlink()
    # --check with --write: the drift mode wins, reports the
    # missing file, and still writes nothing.
    assert (
        main(
            [
                "generate",
                "--write",
                "--check",
                "--project-dir",
                str(tmp_path),
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "missing:" in out
    assert not target.exists()


# --- snapshot-sources --------------------------------------------


def _snapshot_project(project: Path) -> None:
    (project / "dbt_project.yml").write_text(
        "name: sample\n", encoding="utf-8"
    )
    source = {"name": "ecom", "table": "raw_customers", "schema": "raw"}
    path = project / "models" / "_generated" / "staging" / "ecom.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# GENERATED FILE. Source: fixture\nversion: 2\nsources:\n"
        "  - name: ecom\n    tables:\n      - name: raw_customers\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customer source",
            "owner": "analytics",
            "inline_source": source,
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="raw_staging",
    )
    manifest = project / "target" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "sample",
                    "adapter_type": "duckdb",
                    "generated_at": "2999-01-01T00:00:00+00:00",
                },
                "sources": {
                    "source.sample.ecom.raw_customers": {
                        "package_name": "sample",
                        "source_name": "ecom",
                        "name": "raw_customers",
                        "database": "local",
                        "schema": "raw",
                        "identifier": "raw_customers",
                        "original_file_path": path.relative_to(
                            project
                        ).as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    (project / ".gitignore").write_text("target/\n", encoding="utf-8")


def test_snapshot_sources_preview_and_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    duckdb = pytest.importorskip("duckdb")
    _snapshot_project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    baseline = tmp_path / "target" / "loom" / "source-structure.json"
    argv = [
        "snapshot-sources",
        "--project-dir",
        str(tmp_path),
        "--database",
        database.name,
    ]

    assert main([*argv, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["tables"] == 1
    assert payload["sources"] == 1
    assert payload["exists"] is False
    assert not baseline.exists()

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "dry run - nothing written" in out
    assert "Would capture:" in out
    assert out.rstrip().endswith(
        "loom snapshot-sources --write --project-dir "
        + str(tmp_path)
        + " --database "
        + database.name
    )
    assert not baseline.exists()

    assert main([*argv, "--write"]) == 0
    out = capsys.readouterr().out
    assert "writing" in out
    assert "Captured source-structure baseline:" in out
    assert baseline.exists()

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "already exists" in out


# --- format parity on the remaining commands ----------------------


def test_validate_format_and_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _generate_project(tmp_path)
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "validate"
    assert payload["ok"] is True
    assert payload["entries"] >= 1

    assert main(["validate", "--project-dir", str(tmp_path), "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "stg_customers" in out
    assert "Validated" in out


def test_check_format_json_reports_the_gate_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_project(tmp_path)
    assert (
        main(
            [
                "check",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "stg_orders" in payload["missing_models"]


def test_create_new_format_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _generate_project(tmp_path)
    assert (
        main(
            [
                "create-new",
                "unit-test",
                "stg_customers",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "create-new"
    assert payload["kind"] == "unit-test"
    assert "unit_tests:" in payload["snippet"]


# --- the one colour path ------------------------------------------


class _Stream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_colour_enabled_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert colour_enabled(_Stream(True)) is True
    assert colour_enabled(_Stream(False)) is False
    monkeypatch.setenv("NO_COLOR", "")
    assert colour_enabled(_Stream(True)) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert colour_enabled(_Stream(True)) is False


def test_paint_honours_the_switch() -> None:
    try:
        set_colour_enabled(False)
        assert bold("x") == "x"
        assert paint("x", "1", enabled=True) == "\x1b[1mx\x1b[0m"
        set_colour_enabled(True)
        assert bold("x") == "\x1b[1mx\x1b[0m"
    finally:
        set_colour_enabled(False)


def test_colour_reaches_text_never_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "loom.cli.colour_enabled", lambda stream=None: True
    )
    try:
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "\x1b[1m" in capsys.readouterr().out

        _legacy_project(tmp_path)
        assert main(["register", "--project-dir", str(tmp_path)]) == 0
        assert "\x1b[" in capsys.readouterr().out

        assert (
            main(
                [
                    "register",
                    "--project-dir",
                    str(tmp_path),
                    "--format",
                    "json",
                ]
            )
            == 0
        )
        assert "\x1b" not in capsys.readouterr().out
    finally:
        set_colour_enabled(False)


# --- the help standard ---------------------------------------------


def _subparsers() -> dict:
    parser = _parser()
    action = next(
        a
        for a in parser._actions
        if a.__class__.__name__ == "_SubParsersAction"
    )
    return action.choices


def test_help_standard_every_command() -> None:
    choices = _subparsers()
    assert len(choices) == 13
    for name, subparser in choices.items():
        help_text = subparser.format_help()
        assert "Example:" in help_text, name
        options = {
            option
            for action in subparser._actions
            for option in action.option_strings
        }
        assert "--format" in options, name
        assert "--verbose" in options, name


def test_project_dir_help_states_its_default_on_every_command() -> None:
    # QOL F-10: --project-dir is defined once (_add_project_dir_arg)
    # and every command page states its default.
    import re

    choices = _subparsers()
    assert len(choices) == 13
    for name, subparser in choices.items():
        flat = re.sub(r"\s+", " ", subparser.format_help())
        assert "--project-dir" in flat, name
        assert "default: the current directory" in flat, name


def test_help_mutating_commands_state_the_preview_default() -> None:
    choices = _subparsers()
    for name in ("generate", "register", "convert", "snapshot-sources"):
        help_text = choices[name].format_help()
        assert "--write" in help_text, name
        assert "preview" in help_text.lower(), name
    for name, subparser in choices.items():
        help_text = subparser.format_help()
        assert "--write-drafts" not in help_text, name
        assert "--dry-run" not in help_text, name


def test_root_help_states_the_write_convention(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "LOOM_WRITE" in out
    assert "--write" in out
    assert "\x1b" not in out


def test_retired_flags_are_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["generate", "--dry-run"])
    with pytest.raises(SystemExit):
        main(["register", "--write-drafts"])
    with pytest.raises(SystemExit):
        main(["convert", "--write-drafts"])
