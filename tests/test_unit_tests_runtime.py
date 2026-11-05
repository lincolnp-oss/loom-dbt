"""Declared unit tests runtime proof (ADR 0029).

A generated dimension unit test (the design §1.6 shape) and a
generated persistent-hub unit test (with the required
`is_incremental: false` declaration) run green under a real
`dbt build` on DuckDB. Since the 2026-11-04 extension ruling the
project also carries a declared staging unit test and a declared
bronze unit test: both mock the model's own source (the given
input names the source table; generation renders it in dbt's
`source('raw', 'widgets')` form), the practitioner shape that
displaced the old source-fed exclusions. The hub fixture's expected hash is the
deterministic SHA-256 of the normalized business key, computed
here exactly as the renderer computes it: UPPER(TRIM(CAST(key AS
VARCHAR))) then LOWER(SHA256(...)). Every dbt subprocess carries
the standing telemetry opt-outs.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dbt_harness import duckdb_profiles_yml

from loom.cli import main
from metadata_helpers import write_pattern_instance

pytestmark = pytest.mark.integration


def _write_project(project: Path) -> None:
    (project / "seeds").mkdir(parents=True)
    (project / "models").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: unit_proof\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: unit_proof\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(
            "unit_proof", str(project / "unit_proof.duckdb"), target="dev"
        ),
        encoding="utf-8",
    )
    (project / "seeds" / "widgets.csv").write_text(
        "widget_id,record_source\n1,erp\n2,crm\n", encoding="utf-8"
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: widgets\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_widgets",
        "staging",
        {
            "description": "Widget staging",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "widgets"},
            "columns": [
                {"source": "widget_id", "alias": "widget_id"},
                {"source": "record_source", "alias": "record_source"},
            ],
            "unit_tests": [
                {
                    "name": "stg_widgets__rows_pass_through",
                    "given": [
                        {
                            "input": "widgets",
                            "rows": [
                                {"widget_id": 1, "record_source": "erp"},
                                {"widget_id": 2, "record_source": "crm"},
                            ],
                        }
                    ],
                    "expect": {
                        "rows": [
                            {"widget_id": 1, "record_source": "erp"},
                            {"widget_id": 2, "record_source": "crm"},
                        ]
                    },
                }
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "bronze_widgets",
        "bronze",
        {
            "description": "Widgets as landed",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "widgets"},
            "unit_tests": [
                {
                    "name": "bronze_widgets__rows_land",
                    "given": [
                        {
                            "input": "widgets",
                            "rows": [{"widget_id": 1, "record_source": "erp"}],
                        }
                    ],
                    "expect": {
                        "rows": [{"widget_id": 1, "record_source": "erp"}]
                    },
                }
            ],
        },
        pattern="bronze",
    )
    widget_hk_1 = hashlib.sha256(b"1").hexdigest()
    write_pattern_instance(
        project,
        "hub_widget",
        "hub",
        {
            "description": "Widget hub",
            "owner": "analytics",
            "input_models": ["stg_widgets"],
            "business_key": "widget_id",
            "retention": "persistent",
            "record_source": "record_source",
            "unit_tests": [
                {
                    "name": "hub_widget__first_load",
                    "overrides": {"macros": {"is_incremental": False}},
                    # Renderer truth the fixture must match: the
                    # hub's business-key output is the *normalized*
                    # key (text), and `__record_source` is stamped
                    # as the parameter's literal value, not read
                    # from the input row.
                    "given": [
                        {
                            "input": "stg_widgets",
                            "rows": [{"widget_id": 1}],
                        }
                    ],
                    "expect": {
                        "rows": [
                            {
                                "widget_id": "1",
                                "widget_hk": widget_hk_1,
                                "__record_source": "record_source",
                            }
                        ]
                    },
                }
            ],
        },
        pattern="hub",
    )
    write_pattern_instance(
        project,
        "dim_widgets",
        "dimension",
        {
            "description": "Widget dimension",
            "owner": "analytics",
            "input_models": ["hub_widget"],
            "natural_key": "widget_id",
            "surrogate_key": "widget_sk",
            "attribute_columns": ["widget_hk"],
            "unit_tests": [
                {
                    "name": "dim_widgets__surrogate_keys_follow_natural_key_order",
                    "given": [
                        {
                            "input": "hub_widget",
                            "rows": [
                                {"widget_id": 2, "widget_hk": "h2"},
                                {"widget_id": 1, "widget_hk": "h1"},
                            ],
                        }
                    ],
                    "expect": {
                        "rows": [
                            {"widget_sk": 1, "widget_id": 1, "widget_hk": "h1"},
                            {"widget_sk": 2, "widget_id": 2, "widget_hk": "h2"},
                        ]
                    },
                }
            ],
        },
        pattern="dimension",
    )


def test_generated_unit_tests_pass_under_dbt_build(tmp_path: Path) -> None:
    import sys

    sibling = Path(sys.executable).parent / "dbt"
    dbt = str(sibling) if sibling.exists() else shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt is not installed in this environment")

    project = tmp_path / "unit_proof"
    project.mkdir()
    _write_project(project)
    assert main(["generate", "--write", "--project-dir", str(project)]) == 0
    assert (project / "models" / "_generated/tests" / "unit_hub_widget.yml").exists()
    assert (project / "models" / "_generated/tests" / "unit_dim_widgets.yml").exists()
    assert (project / "models" / "_generated/tests" / "unit_stg_widgets.yml").exists()
    assert (project / "models" / "_generated/tests" / "unit_bronze_widgets.yml").exists()
    staging_unit = (
        project / "models" / "_generated/tests" / "unit_stg_widgets.yml"
    ).read_text(encoding="utf-8")
    assert "source('raw', 'widgets')" in staging_unit

    env = dict(os.environ)
    # Seed first (the quickstart's own documented order): the staging
    # model reads the seed through a dbt source, and sources carry no
    # DAG edge to seeds, so a single `dbt build` may schedule the
    # view before the seed lands.
    seed = subprocess.run(
        [dbt, "seed", "--project-dir", str(project), "--profiles-dir", str(project)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project),
        timeout=600,
    )
    assert seed.returncode == 0, (seed.stdout + seed.stderr)[-2000:]
    result = subprocess.run(
        [
            dbt,
            "build",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project),
        timeout=600,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    assert "unit_test" in output
    assert "hub_widget__first_load" in output
    assert "dim_widgets__surrogate_keys_follow_natural_key_order" in output
    assert "stg_widgets__rows_pass_through" in output
    assert "bronze_widgets__rows_land" in output
    assert "ERROR=0" in output
