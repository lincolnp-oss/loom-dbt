"""The search experience and its acceptance narratives,
executed by running the commands over three fixtures:
a Data Vault project with a full manifest (Loom-known
models, an unmanaged model, an
orphaned registration, project and package macros), a
jaffle-shaped manifest-only project (every model unmanaged),
and the checked-in first-project example (metadata-only mode).

Every expectation below was captured from the commands' real
output on these fixtures.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.cli import main
from loom.reporting import set_colour_enabled
from metadata_helpers import write_pattern_instance

ROOT = Path(__file__).parents[1]
FIRST_PROJECT = ROOT / "examples" / "first-project"

_FIELDS = {"description": "Customer domain", "owner": "analytics"}


# --- The Data Vault fixture --------------------------------------


def _node(
    name: str,
    *,
    deps: list[str] | None = None,
    macros: list[str] | None = None,
    description: str | None = None,
    columns: dict | None = None,
    resource_type: str = "model",
    package_name: str = "sample",
    meta: dict | None = None,
) -> dict:
    node: dict = {
        "name": name,
        "resource_type": resource_type,
        "package_name": package_name,
        "original_file_path": f"models/{name}.sql",
        "depends_on": {"nodes": deps or [], "macros": macros or []},
        "raw_code": "select 1\n",
    }
    if description is not None:
        node["description"] = description
    if columns is not None:
        node["columns"] = columns
    if meta is not None:
        node["meta"] = meta
    return node


def _write_manifest(project: Path, document: dict) -> Path:
    for node in document["nodes"].values():
        if node.get("resource_type") == "model":
            source = project / node["original_file_path"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("select 1\n", encoding="utf-8")
    document["metadata"]["generated_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_dv_project(project: Path) -> None:
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            **_FIELDS,
            "source_ref": {"name": "raw", "table": "customers"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "email", "alias": "email"},
                {"source": "addr", "alias": "address"},
            ],
            "column_docs": {"email": "Customer email address"},
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            **_FIELDS,
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hubs",
    )
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        {
            **_FIELDS,
            "description": "Customer details satellite",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["email", "address"],
            "record_source": "crm",
        },
        pattern="satellites",
    )
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "legacy_report.yml").write_text(
        "name: legacy_report\nclassification: mart\n"
        "description: Legacy report\nowner: finance\n"
        "keys: [customer_id]\ndepends_on: [sat_customer_details]\n"
        "tracked_columns: [email]\n",
        encoding="utf-8",
    )
    nodes = {
        "model.sample.stg_customers": _node(
            "stg_customers",
            deps=["source.sample.raw.customers"],
            macros=["macro.sample.cents_to_dollars"],
            description="Customer domain",
            columns={
                "customer_id": {
                    "name": "customer_id",
                    "data_type": "INTEGER",
                    "description": "Customer identifier",
                },
                "email": {
                    "name": "email",
                    "data_type": "VARCHAR",
                    "description": "Email (manifest)",
                },
            },
        ),
        "model.sample.hub_customer": _node(
            "hub_customer", deps=["model.sample.stg_customers"]
        ),
        "model.sample.sat_customer_details": _node(
            "sat_customer_details",
            deps=[
                "model.sample.hub_customer",
                "model.sample.stg_customers",
            ],
        ),
        "model.sample.orders_unmanaged": _node(
            "orders_unmanaged",
            deps=["model.sample.stg_customers"],
            description="Unmanaged orders extract",
            columns={
                "order_id": {"name": "order_id", "data_type": "INTEGER"}
            },
            meta={"owner": "ops-team"},
        ),
        "model.sample.mystery_model": _node("mystery_model"),
        "test.sample.not_null_stg_customers": _node(
            "not_null_stg_customers",
            deps=["model.sample.stg_customers"],
            resource_type="test",
        ),
    }
    macros: dict = {
        "macro.sample.cents_to_dollars": {
            "name": "cents_to_dollars",
            "package_name": "sample",
            "description": "Convert cents to dollars",
            "macro_sql": (
                "{% macro cents_to_dollars(amount, precision=2) %}"
                "{{ amount / 100 }}{% endmacro %}"
            ),
            "arguments": [
                {"name": "amount", "type": "number"},
                {"name": "precision"},
            ],
            "depends_on": {"macros": [], "nodes": []},
        },
        "macro.dbt_utils.slugify": {
            "name": "slugify",
            "package_name": "dbt_utils",
            "macro_sql": "{% macro slugify(text) %}{{ text }}{% endmacro %}",
            "arguments": [{"name": "text", "type": "string"}],
            "depends_on": {"macros": [], "nodes": []},
        },
    }
    for index in range(11):
        name = f"macro_{index:02d}"
        macros[f"macro.dbt_utils.{name}"] = {
            "name": name,
            "package_name": "dbt_utils",
            "macro_sql": f"{{% macro {name}(x) %}}{{{{ x }}}}{{% endmacro %}}",
            "arguments": [{"name": "x"}],
            "depends_on": {"macros": [], "nodes": []},
        }
    _write_manifest(
        project,
        {
            "metadata": {"project_name": "sample"},
            "nodes": nodes,
            "sources": {
                "source.sample.raw.customers": {
                    "name": "customers",
                    "source_name": "raw",
                    "package_name": "sample",
                    "description": "Raw customers feed",
                    "resource_type": "source",
                }
            },
            "macros": macros,
            "exposures": {},
        },
    )


@pytest.fixture()
def dv_project(tmp_path: Path) -> Path:
    _write_dv_project(tmp_path)
    return tmp_path


def _touch_after_manifest(project: Path, relative: str) -> Path:
    """Move one project file's mtime past the manifest's
    generation time (the FIX-CHECK 4 staleness shape)."""
    import os

    target = project / relative
    if not target.exists():
        target.write_text("name: sample\n", encoding="utf-8")
    manifest = json.loads(
        (project / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    generated = datetime.fromisoformat(
        manifest["metadata"]["generated_at"]
    ).timestamp()
    os.utime(target, (generated + 60, generated + 60))
    return target


def test_search_answers_and_labels_age_after_project_file_edit(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # M4 (FIX-CHECK 4, first direction): a dbt_project.yml edit
    # after the parse used to refuse search outright, against
    # its shipped never-refuses contract. Search now answers
    # and labels the manifest's age; the guarded coverage
    # command still refuses, naming the file.
    _touch_after_manifest(dv_project, "dbt_project.yml")
    out = _search(dv_project, capsys, "hub_customer")
    assert "Search:" in out
    assert "Manifest: generated" in out
    assert "ago)" in out
    assert (
        main(["coverage", "--project-dir", str(dv_project)]) == 1
    )
    assert "dbt_project.yml" in capsys.readouterr().err


def test_search_answers_and_labels_age_after_model_file_edit(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # M4 (FIX-CHECK 4, second direction): a model-file edit
    # after the parse — search answers with the age label
    # present; coverage refuses naming the model file.
    _touch_after_manifest(dv_project, "models/hub_customer.sql")
    out = _search(dv_project, capsys, "hub_customer")
    assert "Search:" in out
    assert "Manifest: generated" in out
    assert "ago)" in out
    assert (
        main(["coverage", "--project-dir", str(dv_project)]) == 1
    )
    assert "hub_customer.sql" in capsys.readouterr().err


def _search(project: Path, capsys, *args: str) -> str:
    assert main(["search", *args, "--project-dir", str(project)]) == 0
    return capsys.readouterr().out


# --- Narrative 1: orientation (1 command) -------------------------


def test_overview_orients_on_the_dv_project(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(dv_project, capsys)
    assert "sample overview (metadata + manifest)" in out
    assert "Manifest: generated" in out
    # The total the census below breaks down: 6 models, 13
    # macros, 1 source, 1 test.
    assert "Objects: 21 total" in out
    assert (
        "Models: 6 (generated 3, unmanaged 2, orphaned 1)" in out
    )
    assert "Generators: hub 1, satellite 1, staging 1" in out
    assert "Owners: analytics 3" in out
    assert "unowned 1" in out
    assert "Macros: 13 (1 project, 12 package - dbt_utils 12)" in out
    # Suggested queries are drawn from the project's actual
    # state: the exception queries that return non-empty sets.
    assert "Suggested queries:" in out
    assert "loom search is:unmanaged - 2 unmanaged models" in out
    # Singular counts pluralise correctly: one orphaned
    # model, one model with no owner.
    assert "loom search is:orphaned - 1 orphaned model" in out
    assert "loom search has:no-owner - 1 model with no owner" in out

    assert (
        main(
            ["search", "--project-dir", str(dv_project), "--format", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["overview"]["total_objects"] == 6 + 13 + 1 + 1
    assert payload["overview"]["documentation"]["models_total"] == 6


def test_overview_on_first_project_is_metadata_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _search(FIRST_PROJECT, capsys)
    assert "loom_first_project overview (metadata only)" in out
    assert "metadata only - no manifest found" in out
    assert "Models: 4 (generated 3, registered 1)" in out
    assert "Owners: analytics 4 · unowned 0" in out
    assert (
        "loom search has:undocumented-columns - 3 models with "
        "undocumented columns" in out
    )
    # Metadata alone cannot prove test presence, so the
    # has:no-tests suggestion is not offered in this mode.
    assert "has:no-tests" not in out


# --- Narrative 2: duplication check (2 queries + 1 inspection) ----


def test_duplication_check_bands_close_matches(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Query one: existence by key.
    out = _search(dv_project, capsys, "key:customer_id")
    assert "hub_customer" in out
    assert "sat_customer_details" in out

    # Query two: the requirement's shape, comma-intersected.
    # The satellite is the full match; the hub (key and input),
    # the staging model (both columns) and the registered
    # report (key and one column) are close matches, banded
    # and labelled - never silently intersected away.
    out = _search(
        dv_project,
        capsys,
        "key:customer_id,column:email,column:address,input:stg_customers",
    )
    assert "Shape banding is active" in out
    assert "1 full matches, 3 close matches" in out
    full_section, _, close_section = out.partition(
        "Close matches (by shape overlap):"
    )
    assert "sat_customer_details" in full_section
    assert "hub_customer" in close_section
    assert "stg_customers" in close_section
    assert "legacy_report" in close_section
    assert "differs:" in close_section

    # Inspection: the top candidate in detail view, with the
    # documentation a practitioner judges the match by.
    out = _search(dv_project, capsys, "sat_customer_details")
    assert "sat_customer_details (generated)" in out
    assert "owner: analytics" in out
    assert "keys: customer_id" in out
    assert "inputs: stg_customers" in out
    assert "- email" in out
    assert "- address" in out


def test_detail_view_shows_column_documentation(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(dv_project, capsys, "kind:model,stg_customers")
    assert "stg_customers (generated)" in out
    assert "Customer identifier" in out
    # The Loom column_docs text wins over the manifest's
    # differing text for a generated model (plan 6.6).
    assert "Customer email address" in out
    assert "Email (manifest)" not in out
    assert "graph: 0 upstream, 3 downstream" in out
    assert "tests: present" in out


def test_duplication_check_on_first_project_finds_no_full_match(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Zero full matches is a useful answer: the closest shapes
    # come back banded instead of an empty page.
    out = _search(
        FIRST_PROJECT,
        capsys,
        "key:customer_id,column:customer_email,input:stg_customers",
    )
    assert "Shape banding is active" in out
    assert "0 full matches, 3 close matches" in out
    assert "hub_customer" in out


# --- Narrative 3: impact (1 query) ---------------------------------


def test_impact_query_lists_the_downstream_set(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(dv_project, capsys, "downstream:hub_customer")
    assert "Search: 3 matches" in out
    assert "hub_customer" in out
    assert "sat_customer_details" in out
    assert "legacy_report" in out
    assert "stg_customers" not in out


# --- Narrative 4: hygiene (1 query each) ---------------------------


def test_hygiene_orphaned_single_hit_shows_the_detail_view(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(dv_project, capsys, "is:orphaned")
    assert "Search: 1 match" in out
    assert "legacy_report (orphaned)" in out


def _model_rows(out: str) -> list[str]:
    lines = out.splitlines()
    if "Models:" not in lines:
        return []
    rows = []
    for line in lines[lines.index("Models:") + 1 :]:
        if not line.strip():
            break
        rows.append(line.split()[0])
    return rows


def test_hygiene_unowned_and_undocumented_model_sets(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # has:no-owner is scoped to models (M5): the tier-2
    # records no longer inflate the set, so the one unowned
    # model is the whole result and renders as its detail
    # view - before the scoping, fifteen macro/source/test
    # records matched alongside it.
    out = _search(dv_project, capsys, "has:no-owner")
    assert "Search: 1 match" in out
    assert "mystery_model (unmanaged)" in out
    assert "Macros:" not in out

    out = _search(dv_project, capsys, "has:no-description")
    assert _model_rows(out) == ["mystery_model"]


def test_hygiene_unmanaged_and_untested_sets(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(dv_project, capsys, "is:unmanaged")
    assert "orders_unmanaged" in out
    assert "mystery_model" in out
    assert "Facets: status: unmanaged 2" in out

    out = _search(dv_project, capsys, "has:no-tests")
    assert set(_model_rows(out)) == {
        "hub_customer",
        "sat_customer_details",
        "legacy_report",
        "orders_unmanaged",
        "mystery_model",
    }

    out = _search(dv_project, capsys, "has:undocumented-columns")
    assert "hub_customer" in _model_rows(out)


def test_hygiene_on_first_project_no_unowned_models(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every first-project model has an owner, so the scoped
    # has:no-owner set is empty and the output is the
    # zero-result diagnosis. Before the M5 scoping, the
    # raw.customers source record matched (tier-2 records
    # carried no owner by construction) and the query
    # reported a hit that was not a model at all.
    out = _search(FIRST_PROJECT, capsys, "has:no-owner")
    assert "Search: 0 matches (metadata only)" in out
    assert "No matches" in out
    assert "raw.customers" not in out


# --- Narrative 5: macro reuse (1 query) ----------------------------


def test_macro_reuse_kind_promotion_and_detail(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kind:macro promotes macros to the primary table with
    # signature and used-by facts, project and package alike.
    # The project's 13 macros pass the fold limit, so the
    # full promoted table is exercised unfolded (--verbose);
    # the fold itself is pinned by
    # test_promoted_kind_table_past_the_cap_folds_with_a_narrowing.
    out = _search(dv_project, capsys, "kind:macro", "--verbose")
    assert "Macros:" in out
    assert "cents_to_dollars" in out
    assert "slugify" in out
    assert "used by: 1" in out

    # One provenance is isolable (12 package macros - again
    # past the fold limit, so unfolded).
    out = _search(
        dv_project, capsys, "kind:macro,is:package", "--verbose"
    )
    assert "slugify" in out
    assert "cents_to_dollars" not in out

    # The single project macro resolves to its detail view:
    # signature with arguments, and who calls it.
    out = _search(dv_project, capsys, "kind:macro,is:project")
    assert "cents_to_dollars (macro, project)" in out
    assert "signature: cents_to_dollars(amount, precision)" in out
    assert "amount (number)" in out
    assert "precision (has default)" in out
    assert "used by (1):" in out
    assert "- stg_customers" in out


def test_tier2_groups_are_capped_with_a_narrowing_query(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A union query returning models plus every macro: the
    # macros ride beneath the model table in grouped sections,
    # and the 12-strong package group caps at ten with its
    # full count and the narrowing query.
    out = _search(dv_project, capsys, "customer", "kind:macro")
    assert "Models:" in out
    assert "Macros - project (1):" in out
    assert "Macros - package dbt_utils (12):" in out
    assert "and 2 more" in out
    assert 'narrow with: loom search "kind:macro,package:dbt_utils"' in out


# --- The scripting and JSON forms ----------------------------------


def test_count_only_and_names_only(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(dv_project, capsys, "is:unmanaged", "--count-only")
    assert out == "2\n"

    out = _search(dv_project, capsys, "customer", "--names-only")
    assert out.splitlines() == [
        "hub_customer",
        "sat_customer_details",
        "stg_customers",
        "not_null_stg_customers",
        "raw.customers",
        "legacy_report",
    ]

    assert (
        main(
            [
                "search",
                "is:unmanaged",
                "--project-dir",
                str(dv_project),
                "--count-only",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "search",
        "count": 2,
        "full": 2,
        "close": 0,
    }


def test_count_only_under_banding_distinguishes_full_and_close(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two shape qualifiers engage banding: sat_customer_details
    # carries both the key and the column (the one full match);
    # hub_customer, legacy_report and stg_customers satisfy
    # only one of the two and land close. The text count names
    # both counts with the headline's own labels and wording
    # instead of merging them into one number; the JSON form
    # is unchanged.
    out = _search(
        dv_project,
        capsys,
        "key:customer_id,column:address",
        "--count-only",
    )
    assert out == "1 full matches, 3 close matches\n"

    assert (
        main(
            [
                "search",
                "key:customer_id,column:address",
                "--project-dir",
                str(dv_project),
                "--count-only",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "search",
        "count": 4,
        "full": 1,
        "close": 3,
    }


def test_json_fields_choose_the_record_fields(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "search",
                "generator:hub",
                "--project-dir",
                str(dv_project),
                "--format",
                "json",
                "--fields",
                "name,status",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == [
        {"name": "hub_customer", "status": "generated"}
    ]

    assert (
        main(
            [
                "search",
                "hub_customer",
                "--project-dir",
                str(dv_project),
                "--format",
                "json",
                "--fields",
                "name,bogus",
            ]
        )
        == 1
    )
    assert "unknown --fields" in capsys.readouterr().err


def test_search_colour_paints_status_never_json(
    dv_project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("loom.cli.colour_enabled", lambda stream=None: True)
    try:
        out = _search(dv_project, capsys, "key:customer_id")
        # Status is the primary colour carrier: generated is
        # painted green in the table.
        assert "\x1b[32m" in out
        assert (
            main(
                [
                    "search",
                    "key:customer_id",
                    "--project-dir",
                    str(dv_project),
                    "--format",
                    "json",
                ]
            )
            == 0
        )
        assert "\x1b" not in capsys.readouterr().out
    finally:
        set_colour_enabled(False)


# --- The jaffle-shaped fixture -------------------------------------


def _write_jaffle(project: Path) -> None:
    def jaffle_node(name: str, deps: list[str]) -> dict:
        return _node(name, deps=deps, package_name="jaffle_shop")

    nodes = {
        "model.jaffle_shop.stg_customers": jaffle_node(
            "stg_customers", ["source.jaffle_shop.raw_customers"]
        ),
        "model.jaffle_shop.stg_orders": jaffle_node(
            "stg_orders", ["source.jaffle_shop.raw_orders"]
        ),
        "model.jaffle_shop.stg_order_items": jaffle_node(
            "stg_order_items", ["source.jaffle_shop.raw_order_items"]
        ),
        "model.jaffle_shop.stg_products": jaffle_node(
            "stg_products", ["source.jaffle_shop.raw_products"]
        ),
        "model.jaffle_shop.stg_supplies": jaffle_node(
            "stg_supplies", ["source.jaffle_shop.raw_supplies"]
        ),
        "model.jaffle_shop.stg_locations": jaffle_node(
            "stg_locations", ["source.jaffle_shop.raw_locations"]
        ),
        "model.jaffle_shop.int_orders__pivoted": jaffle_node(
            "int_orders__pivoted", ["model.jaffle_shop.stg_order_items"]
        ),
        "model.jaffle_shop.orders": jaffle_node(
            "orders",
            [
                "model.jaffle_shop.int_orders__pivoted",
                "model.jaffle_shop.stg_orders",
                "model.jaffle_shop.stg_customers",
            ],
        ),
        "model.jaffle_shop.customers": jaffle_node(
            "customers",
            [
                "model.jaffle_shop.stg_customers",
                "model.jaffle_shop.orders",
            ],
        ),
    }
    _write_manifest(
        project,
        {
            "metadata": {"project_name": "jaffle_shop"},
            "nodes": nodes,
            "sources": {},
            "macros": {},
            "exposures": {},
        },
    )


@pytest.fixture()
def jaffle_project(tmp_path: Path) -> Path:
    _write_jaffle(tmp_path)
    return tmp_path


def test_jaffle_overview_and_hygiene(
    jaffle_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(jaffle_project, capsys)
    assert "jaffle_shop overview (metadata + manifest)" in out
    assert "Models: 9 (unmanaged 9)" in out
    assert "loom search is:unmanaged - 9 unmanaged models" in out


def test_jaffle_impact_and_scripting_forms(
    jaffle_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _search(jaffle_project, capsys, "downstream:stg_customers")
    assert "customers" in out
    assert "orders" in out
    assert "stg_orders" not in out

    out = _search(jaffle_project, capsys, "stg_*", "--names-only")
    assert set(out.splitlines()) == {
        "stg_customers",
        "stg_orders",
        "stg_order_items",
        "stg_products",
        "stg_supplies",
        "stg_locations",
    }

    assert (
        main(
            [
                "search",
                "is:unmanaged",
                "--project-dir",
                str(jaffle_project),
                "--count-only",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 9


# --- M5: hygiene counts are model counts ---------------------------
#
# The real jaffle_shop composition from the assessment's
# ground truth: 13 models, 27 test nodes spread over 9 of
# them (so exactly 4 models have no tests), no owners
# anywhere, and tier-2 volume on top. Before the scoping,
# has:no-tests headlined 708 on the real project - every
# macro, test and source record matched, since tier-2
# records carry has_tests as false by construction.


def _write_jaffle_full(project: Path) -> None:
    tested = [
        "customers",
        "orders",
        "int_orders__pivoted",
        "stg_customers",
        "stg_locations",
        "stg_order_items",
        "stg_orders",
        "stg_products",
        "stg_supplies",
    ]
    untested = [
        "locations",
        "metricflow_time_spine",
        "products",
        "supplies",
    ]
    nodes: dict = {}
    for name in tested + untested:
        nodes[f"model.jaffle_shop.{name}"] = _node(
            name, package_name="jaffle_shop"
        )
    for name in tested:
        for index in range(3):
            test_name = f"test_on_{name}_{index}"
            nodes[f"test.jaffle_shop.{test_name}"] = _node(
                test_name,
                deps=[f"model.jaffle_shop.{name}"],
                resource_type="test",
                package_name="jaffle_shop",
            )
    macros = {
        f"macro.dbt_utils.macro_{index:02d}": {
            "name": f"macro_{index:02d}",
            "package_name": "dbt_utils",
            "macro_sql": (
                f"{{% macro macro_{index:02d}(x) %}}"
                "{{ x }}{% endmacro %}"
            ),
            "arguments": [{"name": "x"}],
            "depends_on": {"macros": [], "nodes": []},
        }
        for index in range(40)
    }
    sources = {
        f"source.jaffle_shop.raw.{table}": {
            "name": table,
            "source_name": "raw",
            "package_name": "jaffle_shop",
            "resource_type": "source",
        }
        for table in (
            "customers",
            "orders",
            "order_items",
            "products",
            "supplies",
            "locations",
        )
    }
    _write_manifest(
        project,
        {
            "metadata": {"project_name": "jaffle_shop"},
            "nodes": nodes,
            "sources": sources,
            "macros": macros,
            "exposures": {},
        },
    )


def test_jaffle_hygiene_headline_is_the_model_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_jaffle_full(tmp_path)
    out = _search(tmp_path, capsys, "has:no-tests")
    # The ground-truth pin: headline, facets and the model
    # table all say 4, and only models appear.
    assert "Search: 4 matches (metadata + manifest)" in out
    assert "kind: model 4" in out
    assert set(_model_rows(out)) == {
        "locations",
        "metricflow_time_spine",
        "products",
        "supplies",
    }
    assert "Macros:" not in out
    assert "Tests:" not in out

    assert (
        main(
            [
                "search",
                "has:no-tests",
                "--project-dir",
                str(tmp_path),
                "--count-only",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "4"

    # The overview's suggestion and the query agree (the
    # pre-fix pair read 4 in the overview against 708 in
    # the headline).
    out = _search(tmp_path, capsys)
    assert "loom search has:no-tests - 4 models with no tests" in out

    # No model on the project has an owner: the scoped
    # count is the 13 models, not 13 plus every tier-2
    # record.
    assert (
        main(
            [
                "search",
                "has:no-owner",
                "--project-dir",
                str(tmp_path),
                "--count-only",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "13"


# --- M8: a bare name opens the named model's detail view -----------


def _detail_block(out: str) -> list[str]:
    """The detail-view lines of a single-hit or promoted
    search output: everything after the Query line's blank
    separator, up to the Other matches header if present."""
    lines = out.splitlines()
    # The Query line is located by prefix, not by fixed index:
    # the composed tree (Lane F over Lane S) prints the
    # manifest age label between the headline and the Query
    # line.
    query_index = next(
        i for i, line in enumerate(lines) if line.startswith("Query: ")
    )
    start = query_index + 2
    block = lines[start:]
    if any(line.startswith("Other matches") for line in block):
        cut = next(
            i
            for i, line in enumerate(block)
            if line.startswith("Other matches")
        )
        block = block[: cut - 1]
    return block


def test_bare_name_promotes_the_named_model_detail_view(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # stg_customers' test embeds its name, so the bare name
    # matches two records; the model's detail view leads
    # and the test is listed compactly beneath it.
    out = _search(dv_project, capsys, "stg_customers")
    assert "Search: 2 matches" in out
    assert "stg_customers (generated)" in out
    assert "tests: present" in out
    assert "Other matches (1):" in out
    assert "not_null_stg_customers (test)" in out

    # The promoted detail is the same detail view the
    # taught kind:model route produces for the record.
    detail = _search(dv_project, capsys, "kind:model,stg_customers")
    assert "Other matches" not in detail
    assert _detail_block(out) == _detail_block(detail)

    # The scripting forms are untouched by the promotion.
    assert (
        main(
            [
                "search",
                "stg_customers",
                "--project-dir",
                str(dv_project),
                "--count-only",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "2"
    out = _search(dv_project, capsys, "stg_customers", "--names-only")
    assert set(out.splitlines()) == {
        "stg_customers",
        "not_null_stg_customers",
    }

    # JSON keeps its existing shape: no promotion key, the
    # same two result records.
    assert (
        main(
            [
                "search",
                "stg_customers",
                "--project-dir",
                str(dv_project),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "command",
        "query",
        "corpus",
        "banded",
        "facets",
        "results",
        "close_matches",
    }
    assert len(payload["results"]) == 2


# --- C-4: --similar-to states the omitted count --------------------


def test_similar_to_states_the_no_overlap_omitted_count(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Five models stand against hub_customer; three share
    # some shape with it and two share nothing. Before this
    # change the two silently vanished, indistinguishable
    # from never having been considered.
    out = _search(dv_project, capsys, "--similar-to", "hub_customer")
    assert "Similar to hub_customer: 3 close matches" in out
    assert (
        "2 further model(s) share no shape overlap with "
        "hub_customer and are omitted."
    ) in out

    assert (
        main(
            [
                "search",
                "--similar-to",
                "hub_customer",
                "--project-dir",
                str(dv_project),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["similar_to"]["omitted_no_overlap"] == 2
    assert len(payload["results"]) == 3


# --- C-1: metadata-only is:unmanaged restates the mode -------------


def test_metadata_only_unmanaged_zero_result_restates_the_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # FIRST_PROJECT is metadata-only: no model can carry
    # the unmanaged status there, so the empty set is a
    # property of the mode and the output says so, instead
    # of letting it read as "no unmanaged models".
    out = _search(FIRST_PROJECT, capsys, "is:unmanaged")
    assert "Search: 0 matches (metadata only)" in out
    assert "unmanaged is unknowable in metadata-only mode" in out
    assert "dbt parse to widen the corpus" in out

    # A different empty metadata-only search carries no
    # such restatement.
    out = _search(FIRST_PROJECT, capsys, "has:no-owner")
    assert "Search: 0 matches" in out
    assert "unknowable" not in out


# --- The promoted kind table folds past the cap -------------------


def test_promoted_kind_table_past_the_cap_folds_with_a_narrowing(
    dv_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # kind:macro promotes 13 macros to the primary table.
    # Past ten it folds like the grouped tier-2 sections -
    # superseding the F-12 behaviour (print in full, end
    # with a --names-only/--count-only hint). The dbt_utils
    # package group is the largest (12 of 13), so the
    # narrowing query intersects kind with that package.
    out = _search(dv_project, capsys, "kind:macro")
    assert "Macros:" in out
    assert "macro_09" in out
    assert "macro_10" not in out
    assert "cents_to_dollars" not in out
    assert out.rstrip().endswith(
        '  … and 3 more - narrow with: '
        'loom search "kind:macro,package:dbt_utils"'
    )
