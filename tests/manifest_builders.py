"""Shared manifest-dict builders for the test suite.

Several modules each carried a local ``_model`` / ``_manifest_node``
factory producing the same core node shape - name, resource_type,
package_name, ``original_file_path`` under ``models/``, a
``depends_on`` nodes list and ``raw_code`` - with per-module defaults
for the package name and optional fields. The core lives here now;
modules keep thin local wrappers where their defaults or optional
fields differ, so no test's fixture changes shape.

What deliberately stays local: assessment-specific node content
(per-generator node builders in the migration-assessment module),
and full manifest *files* written from real model files - that is
``metadata_helpers.write_model_manifest``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def manifest_node(
    name: str,
    *,
    package: str = "pilot",
    deps: list[str] | None = None,
    raw_code: str = "select 1",
    resource_type: str = "model",
    extra: dict | None = None,
) -> dict:
    """One manifest node dict with the suite's common core shape."""
    node: dict = {
        "name": name,
        "resource_type": resource_type,
        "package_name": package,
        "original_file_path": f"models/{name}.sql",
        "depends_on": {"nodes": deps or []},
        "raw_code": raw_code,
    }
    if extra:
        node.update(extra)
    return node


def write_manifest(
    project: Path,
    nodes: dict[str, dict],
    *,
    project_name: str = "sample",
    sections: dict | None = None,
) -> Path:
    """Write ``target/manifest.json`` holding `nodes` and return it.

    The metadata carries the fields Loom's manifest reader requires:
    the project name and a fresh ``generated_at``.
    """
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": project_name,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                "nodes": nodes,
                **(sections or {}),
            }
        ),
        encoding="utf-8",
    )
    return path
