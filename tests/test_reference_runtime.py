"""Reference constructs runtime proof on DuckDB (ADR 0034).

Reference lifecycle over two loads: codes are never forgotten
(the AU row survives untouched), a renamed code historises in the
satellite while the latest reference table shows the new name, the
full table keeps both names, and a snapshot-mode table pinned
before the second load stays empty for the renamed code's new
state - the as-of filter works against real load timestamps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom.cli import main
from dbt_harness import duckdb_profiles_yml, run_dbt

def _write_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: ref_runtime\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: ref_runtime\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('ref_runtime', 'ref_runtime.duckdb'),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: country_codes\n",
        encoding="utf-8",
    )
    patterns = {
        "staging.yml": ("staging", "staging", "[description, source_ref, columns]"),
        "ref.yml": (
            "ref",
            "reference_hub",
            "[description, input_models, business_key, record_source]",
        ),
        "sat.yml": (
            "sat",
            "satellite",
            "[description, parent, input_models, record_source]",
        ),
        "reftable.yml": (
            "reftable",
            "reference_table",
            "[description, reference_hub, satellites, business_key, attribute_columns]",
        ),
    }
    for filename, (name, generator, required) in patterns.items():
        (project / "metadata" / "patterns" / filename).write_text(
            f"name: {name}\n"
            f"generator: {generator}\n"
            "defaults: {owner: test}\n"
            f"required_params: {required}\n",
            encoding="utf-8",
        )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_country_codes\n"
        "    params:\n"
        "      description: Country codes.\n"
        "      source_ref: {name: raw, table: country_codes}\n"
        "      columns:\n"
        "        - {source: code, alias: country_code}\n"
        "        - {source: name, alias: country_name}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "reference.yml").write_text(
        "pattern: ref\n"
        "instances:\n"
        "  - name: ref_country\n"
        "    params:\n"
        "      description: Country reference codes.\n"
        "      input_models: [stg_country_codes]\n"
        "      business_key: country_code\n"
        "      record_source: refdata\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "satellites.yml").write_text(
        "pattern: sat\n"
        "instances:\n"
        "  - name: sat_country_details\n"
        "    params:\n"
        "      description: Country names.\n"
        "      parent: {model: ref_country, key: country_code}\n"
        "      input_models: [stg_country_codes]\n"
        "      record_source: refdata\n"
        "      attribute_columns: [country_name]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "tables.yml").write_text(
        "pattern: reftable\n"
        "instances:\n"
        "  - name: ref_country_latest\n"
        "    params:\n"
        "      description: Current country reference.\n"
        "      reference_hub: ref_country\n"
        "      satellites: [sat_country_details]\n"
        "      business_key: country_code\n"
        "      attribute_columns: [country_name]\n"
        "  - name: ref_country_full\n"
        "    params:\n"
        "      description: Full country reference history.\n"
        "      reference_hub: ref_country\n"
        "      satellites: [sat_country_details]\n"
        "      business_key: country_code\n"
        "      attribute_columns: [country_name]\n"
        "      variant: full\n"
        "  - name: ref_country_2000\n"
        "    params:\n"
        "      description: Country reference as of the year 2000.\n"
        "      reference_hub: ref_country\n"
        "      satellites: [sat_country_details]\n"
        "      business_key: country_code\n"
        "      attribute_columns: [country_name]\n"
        "      variant: snapshot\n"
        "      snapshot_timestamp: '2000-01-01T00:00:00'\n",
        encoding="utf-8",
    )

@pytest.mark.integration
def test_reference_constructs_lifecycle_on_duckdb(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "ref_runtime"
    _write_project(project)

    def load(rows: list[tuple[str, str]]) -> None:
        (project / "seeds" / "country_codes.csv").write_text(
            "code,name\n" + "".join(f"{c},{n}\n" for c, n in rows),
            encoding="utf-8",
        )
        run_dbt(dbt, project, "seed", "--full-refresh")
        run_dbt(dbt, project, "build")

    def query(sql: str) -> list[tuple]:
        connection = duckdb.connect(str(project / "ref_runtime.duckdb"), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    # -- Load 1: AU + NZ appear.
    load([("AU", "Australia"), ("NZ", "New Zealand")])
    assert len(query("select * from main.ref_country")) == 2
    latest = query(
        "select country_code, country_name from main.ref_country_latest "
        "order by country_code"
    )
    assert latest == [("AU", "Australia"), ("NZ", "New Zealand")]
    # A snapshot pinned to the year 2000 predates every load: empty.
    assert query("select * from main.ref_country_2000") == []

    # -- Load 2: NZ is renamed, FR appears, AU untouched.
    load([("AU", "Australia"), ("NZ", "Aotearoa New Zealand"), ("FR", "France")])
    # Reference hub is persistent: 3 codes, no duplicates on reload.
    assert len(query("select * from main.ref_country")) == 3
    # Satellite history: AU x1, NZ x2, FR x1.
    assert len(query("select * from main.sat_country_details")) == 4
    latest = query(
        "select country_code, country_name from main.ref_country_latest "
        "order by country_code"
    )
    assert latest == [
        ("AU", "Australia"),
        ("FR", "France"),
        ("NZ", "Aotearoa New Zealand"),
    ]
    full = query(
        "select country_code, country_name from main.ref_country_full "
        "order by country_code, __loaded_at"
    )
    assert full == [
        ("AU", "Australia"),
        ("FR", "France"),
        ("NZ", "New Zealand"),
        ("NZ", "Aotearoa New Zealand"),
    ]
    assert query("select * from main.ref_country_2000") == []
