from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom.cli import main
from dbt_harness import duckdb_profiles_yml, run_dbt

@pytest.mark.integration
def test_scd2_dimension_exposes_full_history_with_boolean_is_deleted(
    tmp_path: Path,
) -> None:
    """Regression: the generated SCD2 dimension must expose full
    snapshot history (closed rows and hard-delete tombstones) with
    is_deleted as a real boolean, matching the documented contract
    ("history semantics, not just current-row projection").

    The installed-consumer test failed in CI because the dimension
    view filtered to current rows only (WHERE dbt_valid_to IS NULL)
    and selected dbt-duckdb's VARCHAR dbt_is_deleted raw ('False' as
    a string). The snapshot itself was always correct; this test
    exercises the whole lifecycle against the generated artifacts so
    the scenario is proven without the isolated-install machinery.
    """
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "scd2_runtime"
    _write_duckdb_project(project)
    (project / "seeds" / "customers.csv").write_text(
        "id,name\n7,Ada\n", encoding="utf-8"
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd2")

    db_path = str(project / "scd2_runtime.duckdb")

    def dimension_rows() -> list[tuple]:
        connection = duckdb.connect(db_path, read_only=True)
        try:
            return connection.execute(
                "select customer_name, __is_deleted, __valid_to is null "
                "from main.dim_customers_scd2 order by __valid_from"
            ).fetchall()
        finally:
            connection.close()

    # Initial load: one current row, boolean False.
    assert dimension_rows() == [("Ada", False, True)]

    # Rename: the Ada row closes, the Adelaide row opens; both are
    # visible in the dimension, with boolean is_deleted values.
    with duckdb.connect(db_path) as connection:
        connection.execute("update main.customers set name = 'Adelaide' where id = 7")
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd2")
    assert dimension_rows() == [("Ada", False, False), ("Adelaide", False, True)]

    # Hard delete: a tombstone row becomes the current row, flagged
    # with a boolean True; history remains fully visible.
    with duckdb.connect(db_path) as connection:
        connection.execute("delete from main.customers where id = 7")
    run_dbt(dbt, project, "build", "--select", "+dim_customers_scd2")
    rows = dimension_rows()
    assert len(rows) == 3
    assert [row for row in rows if row[2]] == [("Adelaide", True, True)]

def _write_duckdb_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: scd2_runtime_test\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: scd2_runtime_test\n"
        "model-paths: ['models']\n"
        "snapshot-paths: ['snapshots']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('scd2_runtime_test', 'scd2_runtime.duckdb'),
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
    (project / "metadata" / "patterns" / "scd2.yml").write_text(
        "name: scd2\n"
        "generator: scd2_dimension\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, business_key, tracked_columns]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customer staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "scd2.yml").write_text(
        "pattern: scd2\n"
        "instances:\n"
        "  - name: dim_customers_scd2\n"
        "    params:\n"
        "      description: Customer history.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "      tracked_columns: [customer_name]\n",
        encoding="utf-8",
    )

