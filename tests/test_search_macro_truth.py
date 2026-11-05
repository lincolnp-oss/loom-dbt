"""Macro truth in search (QOL Lane M): M6 - same-name macros
from different packages coexist as package-qualified records;
M7 - an undocumented macro's signature and slot account come
from its macro_sql signature, not the manifest's empty
arguments list.

The fixture is dbt's canonical collision: the project and the
dbt package both define `generate_schema_name`, and a model
calls the project's (dbt's dispatch resolves the override).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.cli import main


def _node(name: str, *, macros: list[str] | None = None) -> dict:
    return {
        "name": name,
        "resource_type": "model",
        "package_name": "shop",
        "original_file_path": f"models/{name}.sql",
        "depends_on": {"nodes": [], "macros": macros or []},
        "raw_code": "select 1\n",
    }


def _macro(name: str, package: str, macro_sql: str, arguments: list[dict]) -> dict:
    return {
        "name": name,
        "package_name": package,
        "macro_sql": macro_sql,
        "arguments": arguments,
        "depends_on": {"macros": [], "nodes": []},
        "original_file_path": f"macros/{name}.sql",
    }


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0"\n', encoding="utf-8"
    )
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "orders.sql").write_text("select 1\n", encoding="utf-8")
    document = {
        "metadata": {
            "project_name": "shop",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "nodes": {
            # The model calls the project's override: dbt's
            # depends_on records the unique_id it resolved.
            "model.shop.orders": _node(
                "orders", macros=["macro.shop.generate_schema_name"]
            ),
        },
        "sources": {},
        "exposures": {},
        "macros": {
            "macro.shop.generate_schema_name": _macro(
                "generate_schema_name",
                "shop",
                "{% macro generate_schema_name(custom_schema_name, node) %}"
                "x{% endmacro %}",
                [],
            ),
            "macro.dbt.generate_schema_name": _macro(
                "generate_schema_name",
                "dbt",
                "{% macro generate_schema_name(custom_schema_name, node) %}"
                "x{% endmacro %}",
                [
                    {"name": "custom_schema_name", "type": None},
                    {"name": "node", "type": None},
                ],
            ),
            "macro.shop.add_tax": _macro(
                "add_tax",
                "shop",
                "{% macro add_tax(col, rate) %}{{ col }}{% endmacro %}",
                [],
            ),
        },
    }
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return tmp_path


def _search(project: Path, capsys, *args: str) -> str:
    assert main(["search", *args, "--project-dir", str(project)]) == 0
    return capsys.readouterr().out


def _search_json(project: Path, capsys, *args: str) -> dict:
    out = _search(project, capsys, *args, "--format", "json")
    return json.loads(out)


# --- M6: the collision -----------------------------------------


def test_bare_name_search_shows_both_records(project, capsys) -> None:
    out = _search(project, capsys, "generate_schema_name")
    # Both records present in the macro table, each with its
    # provenance and package shown: the project's own override
    # is visible and marked as the project's, carrying the
    # caller; the package record shows none.
    rows = [
        line for line in out.splitlines() if "generate_schema_name" in line
    ]
    assert any("project" in row and "shop" in row and "used by: 1" in row for row in rows)
    assert any("package" in row and "dbt" in row and "used by: 0" in row for row in rows)
    payload = _search_json(project, capsys, "generate_schema_name")
    identities = {
        (item["name"], item["package"]) for item in payload["results"]
    }
    assert identities == {
        ("generate_schema_name", "shop"),
        ("generate_schema_name", "dbt"),
    }


def test_each_record_carries_its_own_used_by(project, capsys) -> None:
    payload = _search_json(project, capsys, "generate_schema_name")
    by_package = {
        item["package"]: item for item in payload["results"]
    }
    # The model resolved to the project's override (dbt
    # dispatch): the project's record shows the caller, the
    # package record truthfully shows none.
    assert by_package["shop"]["used_by"] == ["orders"]
    assert by_package["dbt"]["used_by"] == []


def test_qualified_term_resolves_to_one_record(project, capsys) -> None:
    out = _search(project, capsys, "shop.generate_schema_name")
    assert "generate_schema_name (macro, project)" in out
    out = _search(project, capsys, "dbt.generate_schema_name")
    assert "generate_schema_name (macro, package dbt)" in out


def test_overview_and_kind_counts_reconcile(project, capsys) -> None:
    overview = _search_json(project, capsys)["overview"]
    count = _search_json(project, capsys, "kind:macro", "--count-only")
    assert overview["macros"]["total"] == 3
    assert overview["objects_by_kind"]["macro"] == 3
    assert count["count"] == overview["macros"]["total"]


def test_names_only_prints_qualified_identities(project, capsys) -> None:
    out = _search(project, capsys, "kind:macro", "--names-only")
    assert set(out.split()) == {
        "shop.generate_schema_name",
        "dbt.generate_schema_name",
        "shop.add_tax",
    }


# --- M7: the undocumented signature -----------------------------


def test_undocumented_macro_signature_is_truthful(project, capsys) -> None:
    out = _search(project, capsys, "add_tax")
    assert "signature: add_tax(col, rate)" in out
    assert (
        "slots: staging column mapping - the column fills "
        "'col' at position 1 by default" in out
    )
    assert "takes no arguments" not in out
