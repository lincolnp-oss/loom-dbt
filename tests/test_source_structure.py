from __future__ import annotations

import json
import builtins
from copy import deepcopy
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.cli import main
from loom.source_structure import (
    _diff_source_structure,
    capture_source_structure,
    compare_source_structure,
    inspect_sources,
)
from metadata_helpers import write_pattern_instance


def _project(project: Path, *, native: bool = False) -> Path:
    (project / "dbt_project.yml").write_text("name: sample\n", encoding="utf-8")
    source = {"name": "ecom", "table": "raw_customers"}
    if native:
        path = project / "models" / "sources.yml"
        path.parent.mkdir()
        path.write_text(
            "version: 2\nsources:\n  - name: ecom\n    tables:\n"
            "      - name: raw_customers\n",
            encoding="utf-8",
        )
    else:
        source["schema"] = "raw"
        path = project / "models" / "_generated/staging" / "ecom.yml"
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
            "source_ref" if native else "inline_source": source,
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
                    "generated_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=10)
                    ).isoformat(),
                },
                "sources": {
                    "source.sample.ecom.raw_customers": {
                        "package_name": "sample",
                        "source_name": "ecom",
                        "name": "raw_customers",
                        "database": "local",
                        "schema": "raw",
                        "identifier": "raw_customers",
                        "original_file_path": path.relative_to(project).as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.mark.parametrize("native", [False, True])
def test_inspect_local_source_columns_and_unmapped_fields(
    tmp_path: Path, native: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path, native=native)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer, extra varchar)")
    size = database.stat().st_size
    result = inspect_sources(tmp_path, database.name)
    assert result == {
        "project": "sample",
        "sources": [
            {
                "source": "ecom",
                "table": "raw_customers",
                "database": "local",
                "schema": "raw",
                "identifier": "raw_customers",
                "mapped_columns": [{"column": "id", "models": ["stg_customers"]}],
                "columns": [
                    {
                        "name": "id",
                        "type": "INTEGER",
                        "ordinal": 1,
                        "mapped_by": ["stg_customers"],
                    },
                    {"name": "extra", "type": "VARCHAR", "ordinal": 2, "mapped_by": []},
                ],
            }
        ],
    }
    assert (
        main(
            [
                "inspect-sources",
                "--project-dir",
                str(tmp_path),
                "--database",
                database.name,
                "--source",
                "ecom",
                "--table",
                "raw_customers",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == result
    assert database.stat().st_size == size


def test_missing_source_relation_is_not_reported_as_empty(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    with duckdb.connect(str(tmp_path / "local.duckdb")) as connection:
        connection.execute("create schema raw")
    with pytest.raises(LoomError, match="physical relation.* was not found"):
        inspect_sources(tmp_path, "local.duckdb")


def test_stale_manifest_and_unsafe_database_path_are_rejected(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    manifest = _project(tmp_path)
    with duckdb.connect(str(tmp_path / "local.duckdb")):
        pass
    outside = tmp_path.parent / f"{tmp_path.name}-outside.duckdb"
    with duckdb.connect(str(outside)):
        pass
    with pytest.raises(LoomError, match="expected an existing file inside"):
        inspect_sources(tmp_path, outside)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["metadata"]["generated_at"] = "2000-01-01T00:00:00Z"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LoomError, match="manifest is stale"):
        inspect_sources(tmp_path, "local.duckdb")


def test_invalid_selection_or_manifest_fails_before_connection(tmp_path: Path) -> None:
    manifest = _project(tmp_path)
    with pytest.raises(LoomError, match="--table requires --source"):
        inspect_sources(tmp_path, "local.duckdb", table="raw_customers")
    with pytest.raises(LoomError, match="no generated staging sources match"):
        inspect_sources(tmp_path, "local.duckdb", source="not_a_source")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["metadata"]["adapter_type"] = "postgres"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LoomError, match="DuckDB dbt manifest"):
        inspect_sources(tmp_path, "local.duckdb")


def _git_project(project: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    (project / ".gitignore").write_text("target/\n", encoding="utf-8")


def test_capture_requires_ignored_path_and_never_overwrites(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    with pytest.raises(LoomError, match="cannot verify baseline is git-ignored"):
        capture_source_structure(tmp_path, database.name, output_path="schema.json")
    _git_project(tmp_path)
    with pytest.raises(LoomError, match="must be git-ignored"):
        capture_source_structure(tmp_path, database.name, output_path="schema.json")
    baseline = capture_source_structure(tmp_path, database.name)
    assert baseline == tmp_path / "target" / "loom" / "source-structure.json"
    original = baseline.read_bytes()
    assert "format_version" not in json.loads(original)
    assert "schema_version" not in json.loads(original)
    assert compare_source_structure(tmp_path, database.name) == {
        "project": "sample",
        "changes": [],
    }
    assert baseline.read_bytes() == original
    with pytest.raises(LoomError, match="baseline already exists"):
        capture_source_structure(tmp_path, database.name)
    assert baseline.read_bytes() == original
    assert not list(baseline.parent.glob(".loom-structure-*"))
    assert (
        main(
            [
                "diff-sources",
                "--project-dir",
                str(tmp_path),
                "--database",
                database.name,
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"project": "sample", "changes": []}


def test_source_structure_comparison_reports_add_remove_type_and_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute(
            "create table raw.raw_customers (id integer, legacy varchar)"
        )
    _git_project(tmp_path)
    capture_source_structure(tmp_path, database.name)
    with duckdb.connect(str(database)) as connection:
        connection.execute("drop table raw.raw_customers")
        connection.execute(
            "create table raw.raw_customers (legacy varchar, id bigint, added date)"
        )

    report = compare_source_structure(tmp_path, database.name)
    assert report["changes"] == [
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "column_added",
            "column": "added",
            "mapped_by": [],
        },
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "column_type_changed",
            "column": "id",
            "from": "INTEGER",
            "to": "BIGINT",
            "mapped_by": ["stg_customers"],
        },
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "column_reordered",
            "column": "id",
            "from": 1,
            "to": 2,
            "mapped_by": ["stg_customers"],
        },
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "column_reordered",
            "column": "legacy",
            "from": 2,
            "to": 1,
            "mapped_by": [],
        },
    ]
    assert (
        main(
            [
                "diff-sources",
                "--project-dir",
                str(tmp_path),
                "--database",
                database.name,
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == report


def test_source_structure_comparison_marks_removed_mapped_column(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    _git_project(tmp_path)
    capture_source_structure(tmp_path, database.name)
    with duckdb.connect(str(database)) as connection:
        connection.execute("drop table raw.raw_customers")
        connection.execute("create table raw.raw_customers (other integer)")
    report = compare_source_structure(tmp_path, database.name)
    removed = next(
        change for change in report["changes"] if change["change"] == "column_removed"
    )
    assert removed["column"] == "id"
    assert removed["mapped_by"] == ["stg_customers"]
    assert (
        next(
            change for change in report["changes"] if change["change"] == "column_added"
        )["column"]
        == "other"
    )


def test_source_structure_comparison_reports_removed_managed_source(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    _git_project(tmp_path)
    capture_source_structure(tmp_path, database.name)
    (tmp_path / "metadata" / "instances" / "raw_staging.yml").unlink()

    report = compare_source_structure(tmp_path, database.name)
    assert report["changes"] == [
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "source_removed",
        }
    ]


def test_source_structure_comparison_reports_resolved_relation_move(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    _git_project(tmp_path)
    baseline = capture_source_structure(tmp_path, database.name)
    document = json.loads(baseline.read_text(encoding="utf-8"))
    document["sources"][0]["schema"] = "old_raw"
    baseline.write_text(json.dumps(document), encoding="utf-8")

    report = compare_source_structure(tmp_path, database.name)
    assert report["changes"] == [
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "source_location_changed",
            "from": {
                "database": "local",
                "schema": "old_raw",
                "identifier": "raw_customers",
            },
            "to": {
                "database": "local",
                "schema": "raw",
                "identifier": "raw_customers",
            },
        }
    ]


def test_source_structure_comparison_reports_type_change_without_reorder(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    database = tmp_path / "local.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    _git_project(tmp_path)
    capture_source_structure(tmp_path, database.name)
    with duckdb.connect(str(database)) as connection:
        connection.execute("drop table raw.raw_customers")
        connection.execute("create table raw.raw_customers (id bigint)")

    assert compare_source_structure(tmp_path, database.name)["changes"] == [
        {
            "source": "ecom",
            "table": "raw_customers",
            "change": "column_type_changed",
            "column": "id",
            "from": "INTEGER",
            "to": "BIGINT",
            "mapped_by": ["stg_customers"],
        }
    ]


def test_invalid_baseline_is_explicit(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    _project(tmp_path)
    with duckdb.connect(str(tmp_path / "local.duckdb")) as connection:
        connection.execute("create schema raw")
        connection.execute("create table raw.raw_customers (id integer)")
    baseline = tmp_path / "bad.json"
    baseline.write_text('{"project":"sample","sources":[{}]}')
    with pytest.raises(LoomError, match=r"sources\[0\] is invalid"):
        compare_source_structure(tmp_path, "local.duckdb", baseline_path=baseline.name)


def test_cli_reports_missing_optional_duckdb_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _project(tmp_path)
    (tmp_path / "local.duckdb").touch()
    original = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("injected unavailable optional module")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    assert (
        main(
            [
                "inspect-sources",
                "--project-dir",
                str(tmp_path),
                "--database",
                "local.duckdb",
            ]
        )
        == 1
    )
    assert "source inspection needs duckdb" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("invalid-json", "cannot read source-structure baseline"),
        ("duplicate-source", "duplicate source ECOM.CUSTOMERS"),
        ("duplicate-column", r"sources\[0\] has duplicate column names or ordinals"),
        ("duplicate-ordinal", r"sources\[0\] has duplicate column names or ordinals"),
        ("boolean-ordinal", r"sources\[0\] is invalid"),
    ],
)
def test_baseline_validation_finishes_before_source_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    message: str,
) -> None:
    source = {
        "source": "ecom",
        "table": "customers",
        "database": "local",
        "schema": "raw",
        "identifier": "customers",
        "columns": [{"name": "id", "type": "INTEGER", "ordinal": 1}],
    }
    document = {"project": "sample", "sources": [source]}
    if change == "duplicate-source":
        document["sources"].append({**source, "source": "ECOM", "table": "CUSTOMERS"})
    elif change == "duplicate-column":
        source["columns"].append({"name": "ID", "type": "INTEGER", "ordinal": 2})
    elif change == "duplicate-ordinal":
        source["columns"].append({"name": "other", "type": "INTEGER", "ordinal": 1})
    elif change == "boolean-ordinal":
        source["columns"][0]["ordinal"] = True
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        "{" if change == "invalid-json" else json.dumps(document),
        encoding="utf-8",
    )
    before = baseline.read_bytes(), baseline.stat().st_mtime_ns

    def unexpected_inspection(*args, **kwargs):
        pytest.fail("invalid baseline must be rejected before inspecting sources")

    monkeypatch.setattr("loom.source_structure.inspect_sources", unexpected_inspection)
    with pytest.raises(LoomError, match=message):
        compare_source_structure(tmp_path, "unused.duckdb", baseline_path=baseline.name)
    assert (baseline.read_bytes(), baseline.stat().st_mtime_ns) == before


def test_pure_structure_diff_preserves_order_and_current_mapping_without_mutation() -> (
    None
):
    customer = {
        "source": "ecom",
        "table": "customers",
        "database": "local",
        "schema": "old_raw",
        "identifier": "customers",
        "columns": [
            {"name": "id", "type": "INTEGER", "ordinal": 1, "mapped_by": ["old_model"]},
            {
                "name": "legacy",
                "type": "VARCHAR",
                "ordinal": 2,
                "mapped_by": ["old_model"],
            },
        ],
    }
    previous = [{**customer, "source": "removed"}, customer]
    current = [
        {**customer, "source": "added"},
        {
            **customer,
            "source": "ECOM",
            "schema": "raw",
            "mapped_columns": [
                {"column": "id", "models": ["stg_z", "stg_a"]},
                {"column": "legacy", "models": ["stg_current"]},
            ],
            "columns": [
                {"name": "added", "type": "DATE", "ordinal": 1},
                {"name": "ID", "type": "BIGINT", "ordinal": 2},
            ],
        },
    ]
    before = deepcopy((previous, current))
    label = {"source": "ECOM", "table": "customers"}
    assert _diff_source_structure(previous, current) == [
        {"source": "added", "table": "customers", "change": "source_added"},
        {
            **label,
            "change": "source_location_changed",
            "from": {
                "database": "local",
                "schema": "old_raw",
                "identifier": "customers",
            },
            "to": {"database": "local", "schema": "raw", "identifier": "customers"},
        },
        {**label, "change": "column_added", "column": "added", "mapped_by": []},
        {
            **label,
            "change": "column_type_changed",
            "column": "ID",
            "from": "INTEGER",
            "to": "BIGINT",
            "mapped_by": ["stg_a", "stg_z"],
        },
        {
            **label,
            "change": "column_reordered",
            "column": "ID",
            "from": 1,
            "to": 2,
            "mapped_by": ["stg_a", "stg_z"],
        },
        {
            **label,
            "change": "column_removed",
            "column": "legacy",
            "mapped_by": ["stg_current"],
        },
        {"source": "removed", "table": "customers", "change": "source_removed"},
    ]
    assert (previous, current) == before


def test_comparison_rejects_project_mismatch_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "project": "sample",
                "sources": [],
            }
        ),
        encoding="utf-8",
    )

    def inspect(root, database, manifest, *, _allow_empty):
        assert root == tmp_path
        assert database == "unused.duckdb"
        assert manifest == "target/manifest.json"
        assert _allow_empty is True
        return {"project": "other", "sources": []}

    monkeypatch.setattr("loom.source_structure.inspect_sources", inspect)
    with pytest.raises(
        LoomError, match="baseline project 'sample' does not match 'other'"
    ):
        compare_source_structure(tmp_path, "unused.duckdb", baseline_path=baseline.name)
