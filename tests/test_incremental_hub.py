from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from loom.cli import main
from dbt_harness import duckdb_profiles_yml, run_dbt

@pytest.mark.integration
def test_persistent_hub_keeps_new_identities_without_duplicates(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "incremental_hub"
    _write_duckdb_project(project)
    _write_customer_seed(project, [(1, "Original One"), (2, "Original Two")])

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "run", "--select", "+hub_customers")
    first_rows = _read_hub_rows(duckdb, project)
    assert [row[1] for row in first_rows] == ["1", "2"]
    assert [row[0] for row in first_rows] == [
        hashlib.sha256(key.encode()).hexdigest() for key in ("1", "2")
    ]

    _write_customer_seed(
        project,
        [(1, "Changed One"), (2, "Original Two"), (3, "New Three")],
    )
    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "run", "--select", "+hub_customers")

    rows = _read_hub_rows(duckdb, project)
    assert [row[1] for row in rows] == ["1", "2", "3"]
    assert len({row[0] for row in rows}) == 3
    assert len(rows) == 3
    assert [row[0] for row in rows] == [
        hashlib.sha256(key.encode()).hexdigest() for key in ("1", "2", "3")
    ]

def _write_duckdb_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: incremental_hub_test\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: incremental_hub_test\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml("incremental_hub_test", "incremental_hub.duckdb"),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "staging.yml").write_text(
        "name: staging\n"
        "generator: staging\n"
        "defaults: {owner: test}\n"
        "required_params: [description, source_ref, columns]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "hub.yml").write_text(
        "name: hub\n"
        "generator: hub\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, business_key]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Test customer staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "hub.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_customers\n"
        "    params:\n"
        "      description: Persist customer identities.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "      retention: persistent\n",
        encoding="utf-8",
    )

def _write_customer_seed(project: Path, rows: list[tuple[int, str]]) -> None:
    with (project / "seeds" / "customers.csv").open(
        "w", newline="", encoding="utf-8"
    ) as seed_file:
        writer = csv.writer(seed_file)
        writer.writerow(("id", "customer_name"))
        writer.writerows(rows)

def _read_hub_rows(duckdb: ModuleType, project: Path) -> list[tuple[str, str]]:
    connection = duckdb.connect(str(project / "incremental_hub.duckdb"), read_only=True)
    try:
        rows = connection.execute(
            "select customers_hk, customer_id from main.hub_customers "
            "order by customer_id"
        ).fetchall()
    finally:
        connection.close()
    return rows
