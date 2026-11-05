"""Migration-pathway equivalence proof (ADR 0025): the pathway's gate.

Runs the playbook end-to-end on a fixture legacy project: assess a
hand-written staging-shaped model, take the drafted fit proposal,
generate the Loom candidate in a scratch copy, build BOTH projects
against identical seed data, and compare the resulting tables. Only
contents over the original model's columns are compared - the
generator's added `__loaded_at` provenance column is an expected,
explicitly-listed addition, not a difference.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom.cli import main
from loom.migration_assessment import migration_assessment
from dbt_harness import dbt_env, duckdb_profiles_yml


_ROWS = [(101, 1, "placed"), (102, 1, "shipped"), (103, 2, "placed")]


def _scaffold(project: Path, db_file: str) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: legacy_orders\nversion: '1.0.0'\nconfig-version: 2\n"
        "profile: legacy_orders\nmodel-paths: ['models']\nseed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml("legacy_orders", db_file), encoding="utf-8"
    )
    (project / "seeds" / "orders.csv").write_text(
        "id,customer_id,status\n"
        + "".join(f"{a},{b},{c}\n" for a, b, c in _ROWS),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    schema: main\n"
        "    tables:\n      - name: orders\n        columns:\n"
        "          - name: id\n          - name: customer_id\n"
        "          - name: status\n",
        encoding="utf-8",
    )


def _dbt(dbt: str, project: Path, *args: str) -> None:
    result = subprocess.run(
        [dbt, *args, "--profiles-dir", "."],
        cwd=project,
        env=dbt_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.integration
def test_staging_fit_converts_with_equivalent_output(tmp_path: Path) -> None:
    duckdb = pytest.importorskip(
        "duckdb", reason="requires the integration group"
    )
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    # --- the legacy project: one hand-written staging-shaped model
    original = tmp_path / "original"
    _scaffold(original, "original.duckdb")
    (original / "models" / "stg_orders.sql").write_text(
        "select id as order_id, customer_id, status "
        "from {{ source('raw', 'orders') }}\n",
        encoding="utf-8",
    )
    (original / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_orders\n    columns:\n"
        "      - name: order_id\n      - name: customer_id\n"
        "      - name: status\n",
        encoding="utf-8",
    )
    _dbt(dbt, original, "seed")
    _dbt(dbt, original, "build")

    # --- stage 1: assessment drafts the fit
    report = migration_assessment(original)
    (record,) = [m for m in report["models"] if m["name"] == "stg_orders"]
    fit = record["fit"]
    assert fit["generator"] == "staging"
    params = fit["drafted_params"]
    assert {"source": "id", "alias": "order_id"} in params["columns"]
    assert any("rename inferred" in note for note in fit["evidence"])

    # --- stage 2: the candidate, in a scratch copy (playbook step).
    # The reviewer fills the drafted TODOs; the authored model file is
    # absent here - in a real swap the practitioner removes it, which
    # The ownership rules make removal an explicit act.
    migrated = tmp_path / "migrated"
    _scaffold(migrated, "migrated.duckdb")
    (migrated / "metadata" / "patterns").mkdir(parents=True)
    (migrated / "metadata" / "instances").mkdir()
    (migrated / "metadata" / "patterns" / "staging.yml").write_text(
        "name: staging\ngenerator: staging\n"
        "required_params: [description, owner, source_ref, columns]\n",
        encoding="utf-8",
    )
    params["description"] = "Orders staging (migrated from hand-written SQL)."
    params["owner"] = "analytics"
    import yaml

    (migrated / "metadata" / "instances" / "staging.yml").write_text(
        yaml.safe_dump(
            {
                "pattern": "staging",
                "instances": [{"name": "stg_orders", "params": params}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert main(["validate", "--project-dir", str(migrated)]) == 0
    assert main(["generate", "--write", "--project-dir", str(migrated)]) == 0
    _dbt(dbt, migrated, "seed")
    _dbt(dbt, migrated, "build")

    # --- stage 3: equivalence over the original model's columns
    def rows(db: Path) -> list[tuple]:
        connection = duckdb.connect(str(db), read_only=True)
        try:
            return connection.execute(
                "select order_id, customer_id, status from main.stg_orders "
                "order by order_id"
            ).fetchall()
        finally:
            connection.close()

    original_rows = rows(original / "original.duckdb")
    migrated_rows = rows(migrated / "migrated.duckdb")
    assert original_rows == _ROWS
    assert migrated_rows == original_rows
