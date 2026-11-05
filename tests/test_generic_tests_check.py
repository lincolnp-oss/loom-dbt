"""`loom check` carriage rules: duplicate warnings
and manifest attestation, warning by default, error under
--strict."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from loom.check import duplicate_test_warnings
from loom.cli import main
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance

QUICKSTART = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "quickstart"
)


def test_check_warns_on_carried_duplicate_of_a_generator_test(
    tmp_path: Path,
) -> None:
    fields = {"description": "x", "owner": "analytics"}
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            **fields,
            "source_ref": {"name": "raw", "table": "customer"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="staging",
    )
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            **fields,
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "generic_tests": [
                {"test": "unique", "column_name": "customer_hk"},
                {"test": "not_null", "column_name": "customer_id"},
            ],
        },
        pattern="hub",
    )
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: customer\n",
        encoding="utf-8",
    )
    warnings = duplicate_test_warnings(validate_project(tmp_path))
    # The hub generator already emits unique on its key column;
    # the carried not_null on the business-key column is not a
    # duplicate of anything the generator emits.
    assert len(warnings) == 1
    assert "'unique' on column 'customer_hk'" in warnings[0]
    assert "already emits" in warnings[0]


def _link_entry(declarations: list) -> object:
    from loom.inventory import InventoryEntry

    return InventoryEntry(
        "metadata/instances/link.yml",
        {
            "name": "link_order_customer",
            "generator": "link",
            "description": "x",
            "owner": "analytics",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
            "generic_tests": declarations,
        },
    )


def test_check_warns_on_a_duplicate_with_merged_config() -> None:
    # Same name, column and kwargs as the emitted test, with
    # a config: generation emits the test once and merges
    # the config, and the warning says exactly that.
    entry = _link_entry(
        [
            {
                "test": "relationships",
                "column_name": "customer_hk",
                "kwargs": {
                    "to": "ref('hub_customer')",
                    "field": "customer_hk",
                },
                "config": {"severity": "warn"},
            }
        ]
    )
    warnings = duplicate_test_warnings([entry])  # type: ignore[arg-type]
    assert len(warnings) == 1
    assert "generation emits the test once" in warnings[0]
    assert "merging any config the declaration carries" in warnings[0]


def _gold_entry(declarations: list) -> object:
    from loom.inventory import InventoryEntry

    return InventoryEntry(
        "metadata/instances/gold.yml",
        {
            "name": "gold_order_totals",
            "generator": "gold",
            "description": "x",
            "owner": "analytics",
            "input_models": ["stg_orders"],
            "aggregation": {
                "group_by": ["customer_id"],
                "measures": [
                    {
                        "expression": "SUM(quantity)",
                        "alias": "total_quantity",
                    }
                ],
            },
            "generic_tests": declarations,
        },
    )


def test_check_distinguishes_duplicate_declarations_by_config() -> None:
    # Two declarations of the same emitted test (gold emits
    # not_null on every measure alias), differing only in
    # config: the second carries severity: warn. Generation
    # emits the test once with the config merged; check
    # warns once per declaration, and the warnings must be
    # distinguishable - each names its own declaration's
    # config (or that it carries none) - so the practitioner
    # can tell which declaration to drop.
    entry = _gold_entry(
        [
            {"test": "not_null", "column_name": "total_quantity"},
            {
                "test": "not_null",
                "column_name": "total_quantity",
                "config": {"severity": "warn"},
            },
        ]
    )
    warnings = duplicate_test_warnings([entry])  # type: ignore[arg-type]
    assert len(warnings) == 2
    assert warnings[0] != warnings[1]
    assert "(no config)" in warnings[0]
    assert "(config: severity: warn)" in warnings[1]
    assert all("generation emits the test once" in w for w in warnings)


def test_check_does_not_warn_on_differing_kwargs() -> None:
    # The link generator emits a relationships test to
    # hub_customer on customer_hk. A declaration naming a
    # different target is a distinct test to dbt: generation
    # carries it in full and check does not call it a
    # duplicate.
    entry = _link_entry(
        [
            {
                "test": "relationships",
                "column_name": "customer_hk",
                "kwargs": {
                    "to": "ref('hub_order')",
                    "field": "customer_hk",
                },
            }
        ]
    )
    assert duplicate_test_warnings([entry]) == []  # type: ignore[arg-type]


def test_check_warns_across_the_arguments_wrapper() -> None:
    # The emitted form is the `arguments:`-wrapped spelling;
    # a declaration in the wrapped spelling names the same
    # test and warns just as the direct spelling does.
    entry = _link_entry(
        [
            {
                "test": "relationships",
                "column_name": "customer_hk",
                "kwargs": {
                    "arguments": {
                        "to": "ref('hub_customer')",
                        "field": "customer_hk",
                    }
                },
            }
        ]
    )
    warnings = duplicate_test_warnings([entry])  # type: ignore[arg-type]
    assert len(warnings) == 1


def _check_project(
    tmp_path: Path, *, attested: bool
) -> Path:
    """One staging model declaring a generic test, plus a
    manifest that does or does not attest it."""
    project = tmp_path / "mini"
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir(parents=True)
    (project / "models" / "_generated" / "staging").mkdir(parents=True)
    (project / "target").mkdir()
    (project / "dbt_project.yml").write_text("name: mini\n", encoding="utf-8")
    shutil.copy(
        QUICKSTART / "metadata" / "patterns" / "staging.yml",
        project / "metadata" / "patterns" / "staging.yml",
    )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "format_version: '1.0'\n"
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customer staging.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "      generic_tests:\n"
        "        - {test: not_null, column_name: customer_id}\n",
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    sql = "select id as customer_id from raw.customers\n"
    (project / "models/_generated/staging/stg_customers.sql").write_text(
        sql, encoding="utf-8"
    )
    nodes: dict = {
        "model.mini.stg_customers": {
            "resource_type": "model",
            "package_name": "mini",
            "name": "stg_customers",
            "original_file_path": "models/_generated/staging/stg_customers.sql",
            "raw_code": sql,
            "depends_on": {"nodes": []},
            "columns": {},
            "config": {"materialized": "view"},
        }
    }
    if attested:
        nodes["test.mini.not_null_stg_customers_customer_id"] = {
            "resource_type": "test",
            "package_name": "mini",
            "name": "not_null_stg_customers_customer_id",
            "column_name": "customer_id",
            "attached_node": "model.mini.stg_customers",
            "test_metadata": {"name": "not_null", "kwargs": {}},
            "depends_on": {"nodes": ["model.mini.stg_customers"]},
        }
    document = {
        "metadata": {
            "project_name": "mini",
            "generated_at": "2999-01-01T00:00:00+00:00",
        },
        "nodes": nodes,
        "sources": {},
    }
    (project / "target" / "manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return project


def test_check_warns_when_a_declared_test_is_not_attested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _check_project(tmp_path, attested=False)
    rc = main(["check", "--project-dir", str(project)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Inventory coverage is current." in captured.out
    assert "generic_tests declares 'not_null'" in captured.err
    assert "manifest records no such test" in captured.err


def test_check_strict_fails_when_a_declared_test_is_not_attested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _check_project(tmp_path, attested=False)
    rc = main(["check", "--project-dir", str(project), "--strict"])
    assert rc == 1


def test_check_silent_when_a_declared_test_is_attested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _check_project(tmp_path, attested=True)
    rc = main(["check", "--project-dir", str(project)])
    assert rc == 0
    assert "generic_tests" not in capsys.readouterr().err
    rc = main(["check", "--project-dir", str(project), "--strict"])
    assert rc == 0
