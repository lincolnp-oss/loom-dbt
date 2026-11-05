from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from loom.errors import LoomError
from loom.inventory import validate_project
from loom.manifest import check_file_timestamp, generated_at, read_manifest


def _project_file(root: Path, path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise LoomError(f"{label}: cannot resolve {candidate}: {error}") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LoomError(f"{label}: expected an existing file inside {root}")
    return resolved


def _project_root(project_dir: str | Path) -> Path:
    try:
        root = Path(project_dir).expanduser().resolve(strict=True)
    except OSError as error:
        raise LoomError(
            f"{project_dir}: cannot resolve project directory: {error}"
        ) from error
    if not root.is_dir():
        raise LoomError(f"{root}: project path is not a directory")
    return root


def _manifest_sources(
    root: Path, manifest_path: str | Path
) -> tuple[str, dict[tuple[str, str], dict[str, Any]]]:
    path, document = read_manifest(
        root, manifest_path, sections=("sources",), project_local=True
    )
    metadata = document["metadata"]
    if metadata.get("adapter_type") != "duckdb":
        raise LoomError(f"{path}: source inspection requires a DuckDB dbt manifest")
    timestamp = generated_at(path, document)
    sources = document["sources"]
    project_name = metadata["project_name"]
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for unique_id, node in sources.items():
        if not isinstance(node, dict):
            raise LoomError(f"{path}: source {unique_id!r} must be an object")
        if node.get("package_name") != project_name:
            continue
        source, table = node.get("source_name"), node.get("name")
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(table, str)
            or not table
        ):
            raise LoomError(
                f"{path}: source {unique_id!r} must have source_name and name"
            )
        key = (source.casefold(), table.casefold())
        if key in result:
            raise LoomError(f"{path}: ambiguous source {source}.{table}")
        original = node.get("original_file_path")
        if not isinstance(original, str) or not original:
            raise LoomError(f"{path}: source {unique_id!r} has no original_file_path")
        source_path = _project_file(root, original, f"source {unique_id!r}")
        check_file_timestamp(path, source_path, timestamp)
        for field in ("database", "schema", "identifier"):
            if not isinstance(node.get(field), str) or not node[field]:
                raise LoomError(f"{path}: source {unique_id!r} has no resolved {field}")
        result[key] = node
    return project_name, result


def inspect_sources(
    project_dir: str | Path,
    database_path: str | Path,
    manifest_path: str | Path = "target/manifest.json",
    *,
    source: str | None = None,
    table: str | None = None,
    _allow_empty: bool = False,
) -> dict[str, Any]:
    """Inspect mapped local DuckDB sources without reading rows or writing files."""
    root = _project_root(project_dir)
    if table and not source:
        raise LoomError("--table requires --source")
    entries = validate_project(root)
    selected: dict[tuple[str, str], dict[str, set[str]]] = {}
    for entry in entries:
        data = entry.data
        if data.get("generator") != "staging" or not entry.is_generated:
            continue
        reference = data.get("source_ref") or data.get("inline_source")
        if not isinstance(reference, dict):
            continue
        key = (reference["name"].casefold(), reference["table"].casefold())
        if source and key[0] != source.casefold():
            continue
        if table and key[1] != table.casefold():
            continue
        mappings = selected.setdefault(key, {})
        for column in data["columns"]:
            # Expression items (ADR 0037) map no source column;
            # they are not part of the source structure.
            if isinstance(column, dict) and isinstance(column.get("source"), str):
                mappings.setdefault(column["source"].casefold(), set()).add(
                    data["name"]
                )
    if not selected and not _allow_empty:
        raise LoomError("no generated staging sources match the requested selection")
    project_name, sources = _manifest_sources(root, manifest_path)
    database = _project_file(root, database_path, "database")
    try:
        import duckdb
    except ImportError as error:
        raise LoomError(
            "source inspection needs duckdb; install the optional dbt-duckdb "
            "integration dependencies"
        ) from error

    results: list[dict[str, Any]] = []
    try:
        with duckdb.connect(str(database), read_only=True) as connection:
            known_databases = {
                name: Path(path).resolve()
                for name, path in connection.execute(
                    "select database_name, path from duckdb_databases() "
                    "where path is not null"
                ).fetchall()
            }
            for key in sorted(selected):
                if key not in sources:
                    raise LoomError(
                        f"dbt manifest has no project source "
                        f"{key[0]}.{key[1]}; reparse the project"
                    )
                node = sources[key]
                catalog = node["database"]
                if known_databases.get(catalog) != database:
                    raise LoomError(
                        f"source {node['source_name']}.{node['name']}: resolved database "
                        f"{catalog!r} does not match the selected local DuckDB file"
                    )
                columns = connection.execute(
                    "select column_name, data_type, ordinal_position "
                    "from information_schema.columns "
                    "where table_catalog = ? and table_schema = ? and table_name = ? "
                    "order by ordinal_position",
                    [catalog, node["schema"], node["identifier"]],
                ).fetchall()
                if not columns:
                    raise LoomError(
                        f"source {node['source_name']}.{node['name']}: physical relation "
                        f"{catalog}.{node['schema']}.{node['identifier']} was not found"
                    )
                results.append(
                    {
                        "source": node["source_name"],
                        "table": node["name"],
                        "database": catalog,
                        "schema": node["schema"],
                        "identifier": node["identifier"],
                        "mapped_columns": [
                            {"column": column, "models": sorted(models)}
                            for column, models in sorted(selected[key].items())
                        ],
                        "columns": [
                            {
                                "name": name,
                                "type": data_type,
                                "ordinal": ordinal,
                                "mapped_by": sorted(
                                    selected[key].get(name.casefold(), set())
                                ),
                            }
                            for name, data_type, ordinal in columns
                        ],
                    }
                )
    except duckdb.Error as error:
        raise LoomError(f"cannot inspect local DuckDB sources: {error}") from error
    return {"project": project_name, "sources": results}


def _baseline_sources(inspection: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": inspection["project"],
        "sources": [
            {
                field: source[field]
                for field in (
                    "source",
                    "table",
                    "database",
                    "schema",
                    "identifier",
                    "columns",
                )
            }
            for source in inspection["sources"]
        ],
    }


def _output_file(root: Path, path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved_parent = candidate.parent.resolve(strict=False)
    except OSError as error:
        raise LoomError(
            f"baseline: cannot resolve output directory: {error}"
        ) from error
    if not resolved_parent.is_relative_to(root):
        raise LoomError(f"baseline path must stay inside project directory {root}")
    if candidate.is_symlink():
        raise LoomError(f"baseline path must not be a symlink: {candidate}")
    return resolved_parent / candidate.name


def capture_source_structure(
    project_dir: str | Path,
    database_path: str | Path,
    manifest_path: str | Path = "target/manifest.json",
    output_path: str | Path = "target/loom/source-structure.json",
) -> Path:
    """Capture a source-structure baseline atomically without replacing an existing file."""
    root = _project_root(project_dir)
    inspection = inspect_sources(root, database_path, manifest_path)
    baseline_json = (
        json.dumps(_baseline_sources(inspection), indent=2, sort_keys=True) + "\n"
    )
    output = _output_file(root, output_path)
    relative_output = output.relative_to(root).as_posix()
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "--", relative_output],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise LoomError(f"cannot verify baseline is git-ignored: {error}") from error
    if ignored.returncode not in {0, 1}:
        detail = ignored.stderr.decode("utf-8", errors="replace").strip()
        raise LoomError(
            f"cannot verify baseline is git-ignored: git exited {ignored.returncode}: {detail}"
        )
    if ignored.returncode == 1:
        raise LoomError(
            f"{relative_output}: baseline path must be git-ignored so capture stays local"
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LoomError(
            f"{output.parent}: cannot create baseline directory: {error}"
        ) from error
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".loom-structure-", dir=output.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(baseline_json)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    except FileExistsError as error:
        raise LoomError(
            f"{output}: baseline already exists; choose a new path or remove it "
            "explicitly before recapturing"
        ) from error
    except OSError as error:
        raise LoomError(
            f"{output}: cannot capture source-structure baseline: {error}"
        ) from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError as error:
                raise LoomError(
                    f"{temporary}: cannot remove temporary source-structure file: {error}"
                ) from error
    return output


def _read_source_baseline(baseline: Path) -> dict[str, Any]:
    """Decode and validate the complete baseline before inspecting live sources."""
    try:
        document = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LoomError(
            f"{baseline}: cannot read source-structure baseline: {error}"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"project", "sources"}
        or not isinstance(document.get("project"), str)
        or not isinstance(document.get("sources"), list)
    ):
        raise LoomError(
            f"{baseline}: invalid source-structure baseline structure; "
            "capture a fresh baseline with snapshot-sources"
        )
    seen_sources: set[tuple[str, str]] = set()
    for index, item in enumerate(document["sources"]):
        if (
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in ("source", "table", "database", "schema", "identifier")
            )
            or not isinstance(item.get("columns"), list)
            or any(
                not isinstance(column, dict)
                or not isinstance(column.get("name"), str)
                or not isinstance(column.get("type"), str)
                or type(column.get("ordinal")) is not int
                or column["ordinal"] < 1
                for column in item["columns"]
            )
        ):
            raise LoomError(f"{baseline}: sources[{index}] is invalid")
        source_key = item["source"].casefold(), item["table"].casefold()
        if source_key in seen_sources:
            raise LoomError(
                f"{baseline}: duplicate source {item['source']}.{item['table']}"
            )
        seen_sources.add(source_key)
        column_names = [column["name"].casefold() for column in item["columns"]]
        ordinals = [column["ordinal"] for column in item["columns"]]
        if len(set(column_names)) != len(column_names) or len(set(ordinals)) != len(
            ordinals
        ):
            raise LoomError(
                f"{baseline}: sources[{index}] has duplicate column names or ordinals"
            )
    return document


def compare_source_structure(
    project_dir: str | Path,
    database_path: str | Path,
    manifest_path: str | Path = "target/manifest.json",
    baseline_path: str | Path = "target/loom/source-structure.json",
) -> dict[str, Any]:
    """Report physical source-structure differences; never writes or sets failure severity."""
    root = _project_root(project_dir)
    baseline = _project_file(root, baseline_path, "baseline")
    document = _read_source_baseline(baseline)
    inspection = inspect_sources(root, database_path, manifest_path, _allow_empty=True)
    if document["project"] != inspection["project"]:
        raise LoomError(
            f"{baseline}: baseline project {document['project']!r} does not match "
            f"{inspection['project']!r}"
        )
    return {
        "project": inspection["project"],
        "changes": _diff_source_structure(document["sources"], inspection["sources"]),
    }


def _diff_source_structure(
    baseline_sources: list[dict[str, Any]], inspected_sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compare validated baseline and inspection data without project-file access."""
    previous = {
        (item["source"].casefold(), item["table"].casefold()): item
        for item in baseline_sources
    }
    current = {
        (item["source"].casefold(), item["table"].casefold()): item
        for item in inspected_sources
    }
    changes: list[dict[str, Any]] = []
    for key in sorted(previous.keys() | current.keys()):
        before, after = previous.get(key), current.get(key)
        source_label = (after or before)["source"]
        table_label = (after or before)["table"]
        if before is None:
            changes.append(
                {"source": source_label, "table": table_label, "change": "source_added"}
            )
            continue
        if after is None:
            changes.append(
                {
                    "source": source_label,
                    "table": table_label,
                    "change": "source_removed",
                }
            )
            continue
        location_fields = ("database", "schema", "identifier")
        before_location = tuple(before.get(field) for field in location_fields)
        after_location = tuple(after.get(field) for field in location_fields)
        if before_location != after_location:
            changes.append(
                {
                    "source": source_label,
                    "table": table_label,
                    "change": "source_location_changed",
                    "from": dict(zip(location_fields, before_location)),
                    "to": dict(zip(location_fields, after_location)),
                }
            )
        changes.extend(_diff_source_columns(before, after))
    return changes


def _diff_source_columns(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    before_columns = {column["name"].casefold(): column for column in before["columns"]}
    after_columns = {column["name"].casefold(): column for column in after["columns"]}
    mappings = {
        item["column"].casefold(): item["models"]
        for item in after.get("mapped_columns", [])
    }
    changes: list[dict[str, Any]] = []
    for column_key in sorted(before_columns.keys() | after_columns.keys()):
        old, new = before_columns.get(column_key), after_columns.get(column_key)
        if old is None:
            changes.append(
                {
                    "source": after["source"],
                    "table": after["table"],
                    "change": "column_added",
                    "column": new["name"],
                    "mapped_by": sorted(mappings.get(column_key, [])),
                }
            )
            continue
        if new is None:
            changes.append(
                {
                    "source": after["source"],
                    "table": after["table"],
                    "change": "column_removed",
                    "column": old["name"],
                    "mapped_by": sorted(mappings.get(column_key, [])),
                }
            )
            continue
        if old.get("type") != new.get("type"):
            changes.append(
                {
                    "source": after["source"],
                    "table": after["table"],
                    "change": "column_type_changed",
                    "column": new["name"],
                    "from": old.get("type"),
                    "to": new.get("type"),
                    "mapped_by": sorted(mappings.get(column_key, [])),
                }
            )
        if old.get("ordinal") != new.get("ordinal"):
            changes.append(
                {
                    "source": after["source"],
                    "table": after["table"],
                    "change": "column_reordered",
                    "column": new["name"],
                    "from": old.get("ordinal"),
                    "to": new.get("ordinal"),
                    "mapped_by": sorted(mappings.get(column_key, [])),
                }
            )
    return changes
