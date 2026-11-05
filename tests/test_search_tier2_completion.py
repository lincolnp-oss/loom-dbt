"""Tier-2 completion in search output, executed by running the
commands over one manifest fixture: the macro detail view's
slot-compatibility account across the signature shapes, and the
`kind:` promotion of every tier-2 kind to a primary table with
kind-appropriate columns.

Every expectation below was captured from the commands' real
output on this fixture.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.cli import main


def _node(
    name: str,
    *,
    deps: list[str] | None = None,
    macros: list[str] | None = None,
    resource_type: str = "model",
) -> dict:
    return {
        "name": name,
        "resource_type": resource_type,
        "package_name": "sample",
        "original_file_path": f"models/{name}.sql",
        "depends_on": {"nodes": deps or [], "macros": macros or []},
        "raw_code": "select 1\n",
    }


def _macro(name: str, macro_sql: str, arguments: list[dict]) -> dict:
    return {
        "name": name,
        "package_name": "sample",
        "macro_sql": macro_sql,
        "arguments": arguments,
        "depends_on": {"macros": [], "nodes": []},
    }


def _write_project(project: Path) -> None:
    (project / "dbt_project.yml").write_text(
        'name: sample\nversion: "1.0"\n', encoding="utf-8"
    )
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    nodes = {
        "model.sample.stg_customers": _node(
            "stg_customers",
            deps=["source.sample.raw.customers", "seed.sample.country_codes"],
            macros=["macro.sample.cents_to_dollars"],
        ),
        "model.sample.stg_orders": _node(
            "stg_orders", deps=["source.sample.raw.orders"]
        ),
        "seed.sample.country_codes": _node(
            "country_codes", resource_type="seed"
        ),
        "seed.sample.region_map": _node("region_map", resource_type="seed"),
        "snapshot.sample.customers_snapshot": _node(
            "customers_snapshot",
            deps=["model.sample.stg_customers"],
            resource_type="snapshot",
        ),
        "snapshot.sample.orders_snapshot": _node(
            "orders_snapshot",
            deps=["model.sample.stg_orders"],
            resource_type="snapshot",
        ),
        "test.sample.unique_stg_customers": _node(
            "unique_stg_customers",
            deps=["model.sample.stg_customers"],
            resource_type="test",
        ),
        "test.sample.not_null_stg_orders": _node(
            "not_null_stg_orders",
            deps=["model.sample.stg_orders"],
            resource_type="test",
        ),
    }
    for node in nodes.values():
        if node["resource_type"] == "model":
            (models / f"{node['name']}.sql").write_text(
                "select 1\n", encoding="utf-8"
            )
    macros = {
        "macro.sample.no_args": _macro(
            "no_args", "{% macro no_args() %}1{% endmacro %}", []
        ),
        "macro.sample.cents_to_dollars": _macro(
            "cents_to_dollars",
            "{% macro cents_to_dollars(amount, precision=2) %}"
            "{{ amount }}{% endmacro %}",
            [{"name": "amount", "type": "number"}, {"name": "precision"}],
        ),
        "macro.sample.trunc_day": _macro(
            "trunc_day",
            "{% macro trunc_day(datepart, date) %}"
            "{{ date }}{% endmacro %}",
            [{"name": "datepart"}, {"name": "date"}],
        ),
        "macro.sample.windowed": _macro(
            "windowed",
            "{% macro windowed(value, unit='day', label) %}"
            "{{ value }}{% endmacro %}",
            [{"name": "value"}, {"name": "unit"}, {"name": "label"}],
        ),
        "macro.sample.shout": _macro(
            "shout",
            "{% macro shout(text) %}{{ text }}{% endmacro %}",
            [{"name": "text"}],
        ),
    }
    document = {
        "metadata": {
            "project_name": "sample",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "nodes": nodes,
        "sources": {
            "source.sample.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "package_name": "sample",
                "resource_type": "source",
                "description": "Raw customers feed",
            },
            "source.sample.raw.orders": {
                "name": "orders",
                "source_name": "raw",
                "package_name": "sample",
                "resource_type": "source",
            },
        },
        "macros": macros,
        "exposures": {
            "exposure.sample.weekly_report": {
                "name": "weekly_report",
                "package_name": "sample",
                "depends_on": {"nodes": ["model.sample.stg_customers"]},
            },
            "exposure.sample.finance_dashboard": {
                "name": "finance_dashboard",
                "package_name": "sample",
                "depends_on": {
                    "nodes": [
                        "model.sample.stg_customers",
                        "model.sample.stg_orders",
                    ]
                },
            },
        },
    }
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    _write_project(tmp_path)
    return tmp_path


def _search(project: Path, capsys, *args: str) -> str:
    assert main(["search", *args, "--project-dir", str(project)]) == 0
    return capsys.readouterr().out


# --- Macro detail: the slot-compatibility account -----------------


def test_macro_detail_zero_arguments_cannot_fill_the_slot(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "no_args")
    assert "no_args (macro, project)" in out
    assert (
        "slots: this macro takes no arguments, so it cannot fill "
        "the staging column mapping slot" in out
    )


def test_macro_detail_default_tail_needs_no_macro_args(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # amount is required, precision defaulted: the column fills
    # amount and the call is complete with no macro_args.
    out = _search(project, capsys, "cents_to_dollars")
    assert "signature: cents_to_dollars(amount, precision)" in out
    assert "used by (1):" in out
    assert "- stg_customers" in out
    assert (
        "slots: staging column mapping - the column fills "
        "'amount' at position 1 by default" in out
    )
    assert (
        "the column alone is a complete call - every other "
        "argument carries a default." in out
    )


def test_macro_detail_non_column_first_signature_names_the_reach(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The date_trunc shape: the column belongs at position 2,
    # and reaching it costs one macro_arg for datepart.
    out = _search(project, capsys, "trunc_day")
    assert (
        "slots: staging column mapping - the column fills "
        "'datepart' at position 1 by default; macro_column_param "
        "can place it at another position." in out
    )
    assert (
        "a mapping must supply at least 1 macro_args alongside "
        "the column: the call has to reach 'date' (position 2), "
        "the last argument without a default." in out
    )


def test_macro_detail_reach_counts_past_defaulted_arguments(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # label is required at position 3, behind a defaulted unit:
    # positional filling has to supply unit's slot too, so the
    # minimum is two macro_args, not one.
    out = _search(project, capsys, "windowed")
    assert (
        "a mapping must supply at least 2 macro_args alongside "
        "the column: the call has to reach 'label' (position 3), "
        "the last argument without a default." in out
    )


def test_macro_detail_single_argument_is_a_plain_fit(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "shout")
    assert (
        "slots: staging column mapping - the column fills "
        "'text' at position 1 by default" in out
    )
    assert "macro_args" not in out
    assert "complete call" not in out


# --- kind: promotion for every tier-2 kind -------------------------


def test_kind_promotion_sources(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "kind:source")
    assert "Sources:" in out
    assert "raw.customers" in out
    assert "downstream: 1" in out
    assert "raw.orders" in out


def test_kind_promotion_seeds(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "kind:seed")
    assert "Seeds:" in out
    assert "country_codes" in out
    assert "downstream: 1" in out
    assert "region_map" in out
    assert "downstream: 0" in out


def test_kind_promotion_snapshots_name_the_model(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "kind:snapshot")
    assert "Snapshots:" in out
    assert "customers_snapshot" in out
    assert "of: stg_customers" in out
    assert "of: stg_orders" in out


def test_kind_promotion_tests_name_the_model(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "kind:test")
    assert "Tests:" in out
    assert "unique_stg_customers" in out
    assert "on: stg_customers" in out
    assert "on: stg_orders" in out


def test_kind_promotion_exposures(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(project, capsys, "kind:exposure")
    assert "Exposures:" in out
    assert "weekly_report" in out
    assert "depends on: 1" in out
    assert "finance_dashboard" in out
    assert "depends on: 2" in out


def test_kind_promotion_tables_are_aligned(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The promoted table aligns its columns like the macro
    # table: the provenance column starts at one offset on
    # every row.
    out = _search(project, capsys, "kind:source")
    rows = [
        line
        for line in out.splitlines()
        if line.startswith("  raw.")
    ]
    assert len(rows) == 2
    offsets = {row.index("project") for row in rows}
    assert len(offsets) == 1
