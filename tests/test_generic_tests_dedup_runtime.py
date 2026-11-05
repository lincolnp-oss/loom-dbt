"""The duplicate-test defect, proven at dbt level (integration group).

A gold instance whose `generic_tests` carriage repeats the
tests the gold generator emits on its measure columns -
exactly the declarations convert's carriage produces from a
hand-written model's schema - generated two `not_null`
entries per column before the fix, and `dbt parse` failed:
"dbt found two data_tests with the name
not_null_gold_sales_daily_total_amount". This project
declares the full emitted set as carriage (one entry
carrying a `severity: warn` config, the convert-preserved
case), generates, and parses: the document must carry each
test once, the config must ride on the surviving entry, and
dbt must name each data test exactly once.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from loom.cli import main
from dbt_harness import dbt_or_skip, duckdb_profiles_yml, run_dbt


def _write_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: dedup_proof\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: dedup_proof\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml("dedup_proof", "dedup_proof.duckdb"),
        encoding="utf-8",
    )
    (project / "seeds" / "sales_extract.csv").write_text(
        "sale_date,region,amount\n2026-01-17,EU,100\n",
        encoding="utf-8",
    )
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "silver_sales.yml").write_text(
        "name: silver_sales\n"
        "classification: silver\n"
        "description: Hand-written sales.\n"
        "owner: test\n",
        encoding="utf-8",
    )
    (project / "models" / "silver_sales.sql").write_text(
        "select cast(sale_date as date) as sale_date, region, "
        "cast(amount as integer) as amount\n"
        "from {{ ref('sales_extract') }}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "patterns" / "gold.yml").write_text(
        "name: gold\n"
        "generator: gold\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models]\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "gold.yml").write_text(
        "pattern: gold\n"
        "instances:\n"
        "  - name: gold_sales_daily\n"
        "    params:\n"
        "      description: Daily sales by region.\n"
        "      input_models: [silver_sales]\n"
        "      aggregation:\n"
        "        group_by: [sale_date, region]\n"
        "        measures:\n"
        '          - {expression: "sum(amount)", alias: total_amount}\n'
        '          - {expression: "count(*)", alias: lines}\n'
        "      generic_tests:\n"
        "        - {test: not_null, column_name: total_amount}\n"
        "        - test: not_null\n"
        "          column_name: lines\n"
        "          config: {severity: warn}\n",
        encoding="utf-8",
    )


@pytest.mark.integration
def test_gold_carriage_of_the_emitted_set_parses(tmp_path: Path) -> None:
    dbt = dbt_or_skip()
    project = tmp_path / "dedup_proof"
    _write_project(project)

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert (
        main(
            [
                "generate",
                "--write",
                "--project-dir",
                str(project),
                "--dialect",
                "duckdb",
            ]
        )
        == 0
    )

    documents = list(project.rglob("data_gold_sales_daily.yml"))
    assert len(documents) == 1
    document = yaml.safe_load(documents[0].read_text(encoding="utf-8"))
    model = document["models"][0]
    columns = {c["name"]: c for c in model["columns"]}
    # Each emitted test stands exactly once...
    assert columns["total_amount"]["tests"] == ["not_null"]
    # ...and the config-carrying duplicate's config rides on
    # the single surviving entry.
    assert columns["lines"]["tests"] == [
        {"not_null": {"config": {"severity": "warn"}}}
    ]

    run_dbt(dbt, project, "parse")
    manifest = json.loads(
        (project / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    names = [
        node["name"]
        for node in manifest["nodes"].values()
        if node.get("resource_type") == "test"
    ]
    assert names.count("not_null_gold_sales_daily_total_amount") == 1
    assert names.count("not_null_gold_sales_daily_lines") == 1
    lines_test = next(
        node
        for node in manifest["nodes"].values()
        if node.get("name") == "not_null_gold_sales_daily_lines"
    )
    assert lines_test["config"]["severity"] == "warn"
