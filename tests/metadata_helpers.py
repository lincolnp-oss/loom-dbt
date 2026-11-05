from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def write_model_manifest(
    project: Path, names: list[str], project_name: str = "sample"
) -> Path:
    path = project / "target" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes = {}
    for name in names:
        source = project / "models" / f"{name}.sql"
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source.write_text("select 1\n", encoding="utf-8")
        nodes[f"model.{project_name}.{name}"] = {
            "name": name,
            "resource_type": "model",
            "package_name": project_name,
            "original_file_path": f"models/{name}.sql",
            "raw_code": source.read_text(encoding="utf-8"),
        }
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": project_name,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "dbt_schema_version": (
                        "https://schemas.getdbt.com/dbt/manifest/v12.json"
                    ),
                    "dbt_version": "1.12.5",
                },
                "nodes": nodes,
            }
        ),
        encoding="utf-8",
    )
    return path


def _definition_path(project: Path, pattern: str) -> Path:
    return project / "metadata" / "patterns" / f"{pattern}.yml"


def _pattern_path(project: Path, pattern: str) -> Path:
    return project / "metadata" / "instances" / f"{pattern}.yml"


def write_pattern_instance(
    project: Path,
    name: str,
    generator: str,
    params: dict[str, Any],
    *,
    pattern: str,
) -> Path:
    definition_path = _definition_path(project, pattern)
    definition_path.parent.mkdir(parents=True, exist_ok=True)
    if not definition_path.exists():
        definition_path.write_text(
            yaml.safe_dump(
                {
                    "name": pattern,
                    "generator": generator,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    path = _pattern_path(project, pattern)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        instances = read_pattern(project, pattern)["instances"]
    else:
        instances = []
    for instance in instances:
        if instance["name"] == name:
            instance["params"] = params
            break
    else:
        instances.append({"name": name, "params": params})
    return write_pattern(
        project,
        pattern,
        {
            "pattern": pattern,
            "instances": instances,
        },
    )


def read_pattern(project: Path, pattern: str) -> dict[str, Any]:
    return yaml.safe_load(_pattern_path(project, pattern).read_text(encoding="utf-8"))


def write_pattern(project: Path, pattern: str, document: dict[str, Any]) -> Path:
    path = _pattern_path(project, pattern)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path
