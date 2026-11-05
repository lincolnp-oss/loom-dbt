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
from dbt_harness import duckdb_profiles_yml, run_dbt, run_dbt_result

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

@pytest.mark.integration
def test_link_produces_non_empty_rows_matching_hub_hashes(
    tmp_path: Path,
) -> None:
    """Regression: generated links must join staging keys to hub
    hashes and produce the distinct relationships present in the source,
    with hashes identical to the hub hash rule."""
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "link_runtime"
    _write_duckdb_project(project)
    _write_seed(project, "customers", ["id"], [(1,), (2,), (3,)])
    _write_seed(
        project,
        "orders",
        ["id", "customer_id"],
        [(101, 1), (102, 1), (103, 2), (104, 3)],
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    run_dbt(dbt, project, "build")

    connection = duckdb.connect(str(project / "link_runtime.duckdb"), read_only=True)
    try:
        link_rows = connection.execute(
            "select order_customer_hk, order_hk, customer_hk "
            "from main.link_order_customer order by order_hk"
        ).fetchall()
        hub_orders = {
            row[0]
            for row in connection.execute(
                "select order_hk from main.hub_order"
            ).fetchall()
        }
        hub_customers = {
            row[0]
            for row in connection.execute(
                "select customer_hk from main.hub_customer"
            ).fetchall()
        }
    finally:
        connection.close()

    expected_pairs = {(101, 1), (102, 1), (103, 2), (104, 3)}
    expected = {
        (
            _sha256(_sha256(str(order_id)) + _sha256(str(customer_id))),
            _sha256(str(order_id)),
            _sha256(str(customer_id)),
        )
        for order_id, customer_id in expected_pairs
    }
    assert set(link_rows) == expected
    assert len(link_rows) == len(expected_pairs)
    # Every endpoint hash in the link exists in its hub (relationships hold)
    assert {row[1] for row in link_rows} <= hub_orders
    assert {row[2] for row in link_rows} <= hub_customers

@pytest.mark.integration
def test_link_build_fails_on_unmatched_key(tmp_path: Path) -> None:
    """(ADR 0030) An input key with no hub row must fail the
    dbt build via the generated guard, not silently drop the row."""
    pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "link_guard_runtime"
    _write_duckdb_project(project)
    _write_seed(project, "customers", ["id"], [(1,), (2,), (3,)])
    _write_seed(
        project,
        "orders",
        ["id", "customer_id"],
        [(101, 1), (102, 99)],  # customer 99 has no hub row
    )

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    run_dbt(dbt, project, "seed", "--full-refresh")
    result = run_dbt_result(dbt, project, "build")
    assert result.returncode != 0, (
        f"dbt build unexpectedly succeeded\nstdout:\n{result.stdout}"
    )
    assert "Loom link guard" in result.stdout + result.stderr

def _write_duckdb_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: link_runtime_test\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: link_runtime_test\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('link_runtime_test', 'link_runtime.duckdb'),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: orders\n",
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
    (project / "metadata" / "patterns" / "link.yml").write_text(
        "name: link\n"
        "generator: link\n"
        "defaults: {owner: test}\n"
        "required_params: [description, input_models, endpoints]\n",
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
        "  - name: stg_orders\n"
        "    params:\n"
        "      description: Order staging.\n"
        "      source_ref: {name: raw, table: orders}\n"
        "      columns:\n"
        "        - {source: id, alias: order_id}\n"
        "        - {source: customer_id, alias: customer_id}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "hub.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_customer\n"
        "    params:\n"
        "      description: Customer identities.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "  - name: hub_order\n"
        "    params:\n"
        "      description: Order identities.\n"
        "      input_models: [stg_orders]\n"
        "      business_key: order_id\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "link.yml").write_text(
        "pattern: link\n"
        "instances:\n"
        "  - name: link_order_customer\n"
        "    params:\n"
        "      description: Orders placed by customers.\n"
        "      input_models: [stg_orders]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_customer\n"
        "          key: customer_id\n",
        encoding="utf-8",
    )

def _write_seed(
    project: Path, table: str, header: list[str], rows: list[tuple[int, ...]]
) -> None:
    with (project / "seeds" / f"{table}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as seed_file:
        writer = csv.writer(seed_file)
        writer.writerow(header)
        writer.writerows(rows)

