"""Staging macro-slot runtime proof.

A generated staging model carrying both macro tiers - a project
macro in the slot's column-first position and a package macro
(`dbt.date_trunc`) pinned to a later position via
`macro_column_param` - compiles and builds under a real
`dbt build` on DuckDB, and the built values are the macro
semantics. This is the runtime evidence the macro slot lacked:
its rendering was previously pinned only by golden text, and the
`{% call %}` block form those goldens carried is rejected by dbt
for ordinary macros ("two values for the special caller
argument"), so no generated macro model could compile at all.
Every dbt subprocess carries the standing telemetry opt-outs
(via the harness env).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dbt_harness import dbt_or_skip, duckdb_profiles_yml, run_dbt

from loom.cli import main
from metadata_helpers import write_pattern_instance

pytestmark = pytest.mark.integration


def _write_project(project: Path) -> None:
    (project / "seeds").mkdir(parents=True)
    (project / "models").mkdir()
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: macro_proof\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: macro_proof\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n"
        "macro-paths: ['macros']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml(
            "macro_proof", str(project / "macro_proof.duckdb"), target="dev"
        ),
        encoding="utf-8",
    )
    (project / "seeds" / "events.csv").write_text(
        "id,amount,created_at\n"
        "1,12345,2026-01-02 03:04:05\n"
        "2,999,2025-12-31 23:59:59\n",
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: events\n",
        encoding="utf-8",
    )
    (project / "macros" / "cents_to_dollars.sql").write_text(
        "{% macro cents_to_dollars(column, currency) %}"
        "({{ column }} / 100)"
        "{% endmacro %}\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_events",
        "staging",
        {
            "description": "Event staging with macro columns",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "events"},
            "columns": [
                {"source": "id", "alias": "event_id"},
                {
                    "source": "amount",
                    "alias": "amount_usd",
                    "macro": "cents_to_dollars",
                    "macro_args": ["USD"],
                },
                {
                    "source": "created_at",
                    "alias": "created_date",
                    "macro": "dbt.date_trunc",
                    "macro_args": ["day"],
                    "macro_column_param": 2,
                },
            ],
        },
        pattern="staging",
    )


def test_staging_macro_model_builds_under_dbt(tmp_path: Path) -> None:
    dbt = dbt_or_skip()
    project = tmp_path / "macro_proof"
    project.mkdir()
    _write_project(project)

    assert main(["generate", "--write", "--project-dir", str(project)]) == 0
    generated = (
        project / "models" / "_generated" / "staging" / "stg_events.sql"
    ).read_text(encoding="utf-8")
    assert "{{ cents_to_dollars('amount', 'USD') }}" in generated
    assert "{{ dbt.date_trunc('day', 'created_at') }}" in generated
    assert "{% call" not in generated

    # The staging model reads the seed through a dbt source, and
    # sources carry no DAG edge to seeds, so seed first.
    run_dbt(dbt, project, "seed")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(
        str(project / "macro_proof.duckdb"), read_only=True
    )
    try:
        rows = connection.execute(
            "select event_id, amount_usd, created_date::date "
            "from stg_events order by event_id"
        ).fetchall()
    finally:
        connection.close()
    assert [(row[0], row[1]) for row in rows] == [(1, 123.45), (2, 9.99)]
    assert [str(row[2]) for row in rows] == ["2026-01-02", "2025-12-31"]
