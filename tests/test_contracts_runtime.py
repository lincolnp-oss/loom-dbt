"""Runtime proof for enforced contracts (ADR 0032).

An opted-in project (staging -> hub -> dimension, all
`contract_enforced`) builds green under a real dbt build on DuckDB:
dbt's contract preflight accepts Loom's declared types, including
the `not_null` constraints. Then one declared type is made wrong
(the hash key declared `integer` instead of text) and the build
must FAIL at preflight - enforcement is real, not decorative.
All dbt subprocesses carry the telemetry opt-outs (AGENTS.md).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from loom.cli import main
from metadata_helpers import write_pattern_instance
from dbt_harness import dbt_env, duckdb_profiles_yml

pytestmark = pytest.mark.integration

def _write_project(project: Path, *, wrong_type: bool = False) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    (project / "dbt_project.yml").write_text(
        "name: contractproof\nversion: '1.0.0'\nprofile: contractproof\n"
        "model-paths: ['models']\nseed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml("contractproof", "proof.duckdb", target="dev"),
        encoding="utf-8",
    )
    seeds = project / "seeds"
    seeds.mkdir(parents=True)
    (seeds / "widgets.csv").write_text(
        "widget_id,record_source\n1,erp\n2,erp\n", encoding="utf-8"
    )
    models = project / "models"
    models.mkdir()
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    schema: main\n"
        "    tables:\n      - name: widgets\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project, "stg_widgets", "staging",
        {
            "description": "Widget staging", "owner": "analytics",
            "source_ref": {"name": "raw", "table": "widgets"},
            "columns": [
                {"source": "widget_id", "alias": "widget_id"},
                {"source": "record_source", "alias": "record_source"},
            ],
            "contract_enforced": True,
            "column_types": {"widget_id": "integer", "record_source": "text"},
        },
        pattern="staging",
    )
    write_pattern_instance(
        project, "hub_widget", "hub",
        {
            "description": "Widget hub", "owner": "analytics",
            "input_models": ["stg_widgets"], "business_key": "widget_id",
            "contract_enforced": True,
            # The hub projects the business key as the normalized
            # TEXT key (proven in the unit-test runtime proof too).
            "column_types": {"widget_id": "text"},
        },
        pattern="hub",
    )
    write_pattern_instance(
        project, "dim_widgets", "dimension",
        {
            "description": "Widget dimension", "owner": "analytics",
            "input_models": ["hub_widget"], "natural_key": "widget_id",
            "surrogate_key": "widget_sk", "attribute_columns": ["widget_hk"],
            "contract_enforced": True,
            "column_types": {
                "widget_id": "text",
                "widget_hk": "integer" if wrong_type else "text",
            },
        },
        pattern="dimension",
    )
    return project


def _dbt() -> str:
    candidate = Path(sys.executable).parent / "dbt"
    assert candidate.exists(), f"dbt executable not found beside {sys.executable}"
    return str(candidate)


def _run_dbt(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dbt_env()
    return subprocess.run(
        [_dbt(), *args, "--profiles-dir", str(project)],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_opted_in_project_builds_and_wrong_type_fails_preflight(
    tmp_path: Path,
) -> None:
    project = _write_project(tmp_path / "proj")
    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project)]) == 0

    schema_doc = yaml.safe_load(
        (project / "models" / "_generated/tests" / "data_dim_widgets.yml").read_text(
            encoding="utf-8"
        )
    )
    (dim,) = schema_doc["models"]
    assert dim["config"] == {"contract": {"enforced": True}}

    seeded = _run_dbt(project, "seed")
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    built = _run_dbt(project, "build")
    assert built.returncode == 0, built.stdout + built.stderr
    assert "PASS=9" in built.stdout

    manifest = json.loads(
        (project / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    dim_node = manifest["nodes"]["model.contractproof.dim_widgets"]
    assert dim_node["config"]["contract"]["enforced"] is True
    staging_node = manifest["nodes"]["model.contractproof.stg_widgets"]
    assert staging_node["config"]["contract"]["enforced"] is True

    # The negative half: a wrong declared type must fail preflight.
    bad = _write_project(tmp_path / "bad", wrong_type=True)
    assert main(["generate", "--write", "--project-dir", str(bad)]) == 0
    assert _run_dbt(bad, "seed").returncode == 0
    failed = _run_dbt(bad, "build")
    assert failed.returncode != 0
    assert "contract" in (failed.stdout + failed.stderr).lower()
