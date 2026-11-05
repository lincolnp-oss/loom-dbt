"""Practitioner-controlled nested output layout (ADR 0026).

Flat output is the default and must stay byte-identical; an opt-in
`output_subpath` (literal, pattern-inherited, or placeholder-derived)
moves generated files under the configured roots through one shared
resolver used by generation, the drift check, search and coverage.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.cli import main
from loom.errors import LoomError
from loom.inventory import check_inventory_coverage
from manifest_builders import manifest_node, write_manifest
from loom.output_layout import expected_model_path, resolve_output_subpath
from metadata_helpers import write_pattern_instance


def _bronze(project: Path, name: str, params: dict, pattern: str = "medallion_bronze") -> None:
    write_pattern_instance(
        project,
        name,
        "bronze",
        {"inline_source": {"name": "raw", "table": "orders"}, **params},
        pattern=pattern,
    )


def _generate(project: Path, *extra: str) -> int:
    return main(
        ["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb", *extra]
    )


def test_resolver_placeholders_and_safety() -> None:
    data = {
        "name": "hub_customer",
        "generator": "hub",
        "_paradigm": "dv",
        "owner": "analytics",
        "tags": ["zeta", "core"],
        "output_subpath": "{paradigm}/{owner}/{tags}",
    }
    assert resolve_output_subpath(data) == "dv/analytics/core/zeta"
    assert resolve_output_subpath({"name": "x"}) == ""
    with pytest.raises(LoomError):
        resolve_output_subpath({"name": "x", "output_subpath": "../escape"})
    with pytest.raises(LoomError):
        resolve_output_subpath({"name": "x", "output_subpath": "{owner}"})
    with pytest.raises(LoomError):
        resolve_output_subpath({"name": "x", "output_subpath": "{bogus}"})


def test_generate_writes_nested_and_flat_unchanged(tmp_path: Path) -> None:
    _bronze(tmp_path, "bronze_orders", {"output_subpath": "raw/orders"})
    _bronze(tmp_path, "bronze_customers", {})
    assert _generate(tmp_path) == 0
    assert (tmp_path / "models" / "raw" / "orders" / "bronze_orders.sql").exists()
    assert (tmp_path / "models" / "bronze_customers.sql").exists()
    # The shared sources document stays at its project-level home.
    assert (tmp_path / "models" / "_generated/staging" / "sources.yml").exists()
    # Drift check agrees with the nested plan.
    assert _generate(tmp_path, "--check") == 0


def test_generate_check_reports_stale_file_after_layout_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _bronze(tmp_path, "bronze_orders", {})
    assert _generate(tmp_path) == 0
    _bronze(tmp_path, "bronze_orders", {"output_subpath": "raw"})
    assert _generate(tmp_path, "--check") == 1
    out = capsys.readouterr().out
    assert "missing: models/raw/bronze_orders.sql" in out
    assert "stale: models/bronze_orders.sql" in out


def test_generate_rejects_unsafe_subpath(tmp_path: Path) -> None:
    _bronze(tmp_path, "bronze_orders", {"output_subpath": "../outside"})
    assert main(["validate", "--project-dir", str(tmp_path)]) == 1


def test_search_exposes_output_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _bronze(tmp_path, "bronze_orders", {"output_subpath": "{generator}/raw"})
    assert (
        main(
            [
                "search",
                "bronze_orders",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["output_path"] == (
        "models/bronze/raw/bronze_orders.sql"
    )


def _manifest_with_model_at(project: Path, name: str, relative: str) -> None:
    model_file = project / relative
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_text("select 1\n", encoding="utf-8")
    (project / "dbt_project.yml").write_text(
        "name: sample\nprofile: sample\nmodel-paths: ['models']\n",
        encoding="utf-8",
    )
    write_manifest(
        project,
        {
            f"model.sample.{name}": manifest_node(
                name,
                package="sample",
                raw_code="select 1\n",
                extra={"original_file_path": relative},
            )
        },
        project_name="sample",
        sections={"sources": {}, "exposures": {}},
    )


def test_expected_model_path_uses_each_generators_model_root() -> None:
    # Generation writes staging SQL under <model-root>/_generated/staging
    # (the staging spec's last output root); the expected-path resolver
    # must agree, or every staging model reads as "misplaced".
    assert expected_model_path(
        "models", {"name": "stg_orders", "generator": "staging"}
    ) == "models/_generated/staging/stg_orders.sql"
    assert expected_model_path(
        "models",
        {
            "name": "stg_orders",
            "generator": "staging",
            "output_subpath": "raw",
        },
    ) == "models/_generated/staging/raw/stg_orders.sql"
    assert expected_model_path(
        "models", {"name": "hub_customer", "generator": "hub"}
    ) == "models/hub_customer.sql"
    # The SCD2 manifest model is the view (second output root).
    assert expected_model_path(
        "models", {"name": "dim_customers_scd2", "generator": "scd2_dimension"}
    ) == "models/dim_customers_scd2.sql"


def test_coverage_accepts_staging_model_under_generated_staging(
    tmp_path: Path,
) -> None:
    # Regression (installed-consumer CI): the consumer
    # project's staging models live at models/_generated/staging/<name>.sql
    # and `loom check` flagged all of them as misplaced.
    write_pattern_instance(
        tmp_path,
        "stg_orders",
        "staging",
        {
            "description": "Orders staging.",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "orders"},
            "columns": [{"source": "id", "alias": "order_id"}],
        },
        pattern="raw_staging",
    )
    _manifest_with_model_at(
        tmp_path, "stg_orders", "models/_generated/staging/stg_orders.sql"
    )
    coverage = check_inventory_coverage(tmp_path)
    assert coverage.misplaced_generated_models == []
    assert coverage.ok


def test_coverage_flags_misplaced_generated_model(tmp_path: Path) -> None:
    _bronze(tmp_path, "bronze_orders", {"output_subpath": "raw"})
    _manifest_with_model_at(tmp_path, "bronze_orders", "models/bronze_orders.sql")
    coverage = check_inventory_coverage(tmp_path)
    assert coverage.misplaced_generated_models == ["bronze_orders"]
    assert not coverage.ok


def test_coverage_accepts_model_at_expected_nested_path(tmp_path: Path) -> None:
    _bronze(tmp_path, "bronze_orders", {"output_subpath": "raw"})
    _manifest_with_model_at(
        tmp_path, "bronze_orders", "models/raw/bronze_orders.sql"
    )
    coverage = check_inventory_coverage(tmp_path)
    assert coverage.misplaced_generated_models == []
    assert coverage.ok


def test_scd2_identical_roots_collide_and_abort(tmp_path: Path) -> None:
    # With model-paths == snapshot-paths, an SCD2 model's snapshot
    # and view resolve to one file with two contents: the plan phase
    # must refuse rather than write one over the other.
    (tmp_path / "dbt_project.yml").write_text(
        "name: sample\nprofile: sample\n"
        "model-paths: ['models']\nsnapshot-paths: ['models']\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        tmp_path,
        "stg_customers",
        "staging",
        {
            "description": "Customers staging.",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="raw_staging",
    )
    write_pattern_instance(
        tmp_path,
        "dim_customers_scd2",
        "scd2_dimension",
        {
            "description": "Customer history.",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_id"],
        },
        pattern="slowly_changing",
    )
    assert _generate(tmp_path) == 1
    assert not (tmp_path / "models" / "dim_customers_scd2.sql").exists()
