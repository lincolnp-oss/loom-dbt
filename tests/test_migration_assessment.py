"""Migration assessment (ADR 0025).

The assessment reads a dbt manifest + the (possibly empty) Loom
inventory and drafts registrations and generator-fit
proposals with recorded evidence. These tests pin: a staging fit for
a simple source-backed model, a bronze fit for a select-star model, a
silver fit for a dedup shape, a no-fit with named blocking signals for
a bespoke model, interface flags for a contracted public model,
recognition of already-known models, and --write behaviour
(write once, never overwrite).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from loom.cli import main
from manifest_builders import manifest_node
from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance
from loom.migration_assessment import (
    _interface_flags,
    _materialization_scope,
    _projected_outputs,
    _scaffold_review_todos,
    migration_assessment,
    write_drafts,
    write_draft_registrations,
    write_drafts,
)
from loom.reporting import render_convert_text


def _model(
    name: str,
    *,
    deps: list[str] | None = None,
    raw: str = "select 1",
    config: dict | None = None,
    columns: dict | None = None,
    description: str | None = None,
    meta: dict | None = None,
    version: int | None = None,
    latest_version: int | None = None,
    resource_type: str = "model",
) -> dict:
    node = manifest_node(
        name,
        package="legacy",
        deps=deps,
        raw_code=raw,
        resource_type=resource_type,
        extra={"config": config or {}, "columns": columns or {}},
    )
    if meta is not None:
        node["meta"] = meta
    if description is not None:
        node["description"] = description
    if version is not None:
        node["version"] = version
    if latest_version is not None:
        node["latest_version"] = latest_version
    return node


def _manifest(
    project: Path,
    nodes: dict,
    sources: dict | None = None,
    extra_sections: dict | None = None,
    adapter_type: str | None = None,
) -> Path:
    # The assessment now runs the same manifest freshness
    # guard as plan/coverage, so a fixture manifest must be backed
    # by model files whose contents match each node's raw_code and
    # which predate the manifest's generated_at - write the files
    # first, then stamp the manifest (the test_reporting.py /
    # test_migration_plan.py pattern).
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") != "model":
            continue
        if node.get("package_name") != "legacy":
            continue
        original = node.get("original_file_path")
        raw = node.get("raw_code")
        if not isinstance(original, str) or not isinstance(raw, str):
            continue
        source = project / original
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists() or source.read_text(encoding="utf-8") != raw:
            source.write_text(raw, encoding="utf-8")
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    metadata: dict = {
        "project_name": "legacy",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if adapter_type is not None:
        metadata["adapter_type"] = adapter_type
    document = {
        "metadata": metadata,
        "nodes": nodes,
        "sources": sources or {},
        "exposures": {},
    }
    document.update(extra_sections or {})
    path = target / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


_SOURCE = {
    "source.legacy.raw.orders": {
        "name": "orders",
        "source_name": "raw",
        "resource_type": "source",
    }
}


def _nodes() -> dict:
    return {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
        ),
        "model.legacy.raw_dump": _model(
            "raw_dump",
            deps=["source.legacy.raw.orders"],
            raw="select * from {{ source('raw', 'orders') }}",
        ),
        "model.legacy.silver_orders": _model(
            "silver_orders",
            deps=["model.legacy.stg_orders"],
            raw=(
                "select * from (select *, row_number() over "
                "(partition by order_id order by loaded_at desc) as rn "
                "from {{ ref('stg_orders') }}) where rn = 1"
            ),
        ),
        "model.legacy.revenue_rollup": _model(
            "revenue_rollup",
            deps=["model.legacy.stg_orders", "model.legacy.silver_orders"],
            raw=(
                "select a.x, sum(b.y) from {{ ref('stg_orders') }} a "
                "join {{ ref('silver_orders') }} b on a.id = b.id "
                "group by a.x"
            ),
            config={
                "materialized": "table",
                "contract": {"enforced": True},
                "access": "public",
            },
        ),
        "model.legacy.downstream": _model(
            "downstream", deps=["model.legacy.revenue_rollup"]
        ),
        "test.legacy.not_null_stg_orders_order_id": {
            "name": "not_null_stg_orders_order_id",
            "resource_type": "test",
            "package_name": "legacy",
            "depends_on": {"nodes": ["model.legacy.stg_orders"]},
        },
    }


def test_assessment_covers_drafts_fits_and_flags(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}

    assert report["summary"]["project_models"] == 5
    assert report["summary"]["known_to_loom"] == 0
    # Project reporting is the readiness distribution,
    # replacing the three yields: of the five models, one draft
    # is ready to promote, two need authoring, and two are not
    # draftable (no fit, no layer).
    assert report["summary"]["readiness"] == {
        "ready_to_promote": 1,
        "needs_authoring": 2,
        "not_draftable": 2,
    }

    staging = by_name["stg_orders"]
    assert staging["draft_registration"]["classification"] == "staging"
    assert staging["fit"]["generator"] == "staging"
    assert staging["fit"]["drafted_params"]["source_ref"] == {
        "name": "raw",
        "table": "orders",
    }
    # The shared source fixture documents no source columns, so
    # neither documented output can be paired: both draft as
    # unresolved placeholders, never as identity guesses.
    assert staging["fit"]["drafted_params"]["columns"] == [
        {"source": "__TODO__", "alias": "customer_id"},
        {"source": "__TODO__", "alias": "order_id"},
    ]
    assert "1 test(s)" in " ".join(staging["interface_flags"])

    assert by_name["raw_dump"]["fit"]["generator"] == "bronze"
    assert by_name["raw_dump"]["fit"]["drafted_params"]["inline_source"] == {
        "name": "raw",
        "table": "orders",
    }

    silver = by_name["silver_orders"]
    assert silver["fit"]["generator"] == "silver"
    assert silver["fit"]["drafted_params"]["dedup_key"] == "order_id"

    bespoke = by_name["revenue_rollup"]
    assert "no_fit" in bespoke["fit"]
    assert "JOIN" in bespoke["fit"]["no_fit"]
    flags = " ".join(bespoke["interface_flags"])
    assert "enforced dbt contract" in flags
    assert "access is 'public'" in flags
    assert "1 downstream consumer(s)" in flags


def test_assessment_recognises_known_models(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "stg_orders.yml").write_text(
        "name: stg_orders\nclassification: staging\n",
        encoding="utf-8",
    )
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    # A registered model is known but NOT controlled, and it
    # still receives a generator-fit proposal.
    assert by_name["stg_orders"]["state"] == "known_not_controlled"
    assert by_name["stg_orders"]["status"] == "known to Loom, not controlled"
    assert "draft_registration" not in by_name["stg_orders"]
    assert by_name["stg_orders"]["fit"]["generator"] == "staging"
    assert report["summary"]["known_to_loom"] == 1
    assert report["summary"]["known_not_controlled"] == 1
    assert report["summary"]["unknown_to_loom"] == 4
    # The readiness distribution counts the registered model
    # too - registration gates drafting, not reporting.
    assert report["summary"]["readiness"] == {
        "ready_to_promote": 1,
        "needs_authoring": 2,
        "not_draftable": 2,
    }


def test_write_drafts_writes_once_and_never_overwrites(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    written = write_draft_registrations(tmp_path, report)
    assert len(written) == 5
    target = tmp_path / "local" / "registrations" / "stg_orders.yml"
    assert "Drafted by `loom register`" in target.read_text(
        encoding="utf-8"
    )
    target.write_text("practitioner-edited\n", encoding="utf-8")
    written_again = write_draft_registrations(tmp_path, report)
    assert written_again == []
    assert target.read_text(encoding="utf-8") == "practitioner-edited\n"


def test_cli_emits_json_and_writes_drafts_on_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["project"] == "legacy"
    assert not (tmp_path / "local" / "registrations").exists()
    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--write",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert len(report["registrations_written"]) == 5
    # Register writes registrations only - no conversion
    # drafts, and no conversion key in its payload.
    assert "drafts_written" not in report
    assert not (tmp_path / "local" / "drafts").exists()
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(tmp_path),
                "--write",
                "--include-registered",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    # The three fit candidates (staging, bronze, silver)
    # receive drafts. The register run above registered every
    # model first, so the drafting pool is empty by default -
    # the run names --include-registered to restore the reach
    # (the registration gate).
    assert len(report["drafts_written"]) == 3
    # Convert writes conversion draft sets only - no
    # registrations beyond the five the register run already wrote,
    # and no registrations key in its payload.
    assert "registrations_written" not in report
    assert (
        len(list((tmp_path / "local" / "registrations").glob("*.yml"))) == 5
    )


def test_assessment_accepts_relative_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The playbook's documented `--project-dir.` invocation
    # failed because the resolved manifest path was checked against
    # the unresolved relative root.
    _manifest(tmp_path, _nodes(), _SOURCE)
    monkeypatch.chdir(tmp_path)
    report = migration_assessment(Path("."))
    assert report["summary"]["project_models"] == 5
    assert main(["register", "--project-dir", "."]) == 0
    assert main(["convert", "--project-dir", "."]) == 0


def test_registration_gates_drafting_with_override(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "silver_orders.yml").write_text(
        "name: silver_orders\nclassification: medallion silver\n",
        encoding="utf-8",
    )
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    assert by_name["silver_orders"]["state"] == "known_not_controlled"
    assert by_name["silver_orders"]["fit"]["generator"] == "silver"
    # The registered model is still counted and reported - its
    # fit proposal and readiness account are computed.
    assert by_name["silver_orders"]["readiness"]["state"] == (
        "ready_to_promote"
    )

    written = write_drafts(tmp_path, report)
    names = sorted(Path(path).stem for path in written)
    # By default only the unknown fits (stg_orders, raw_dump)
    # are drafted; registration has withdrawn silver_orders
    # from the drafting pool, and the no-fit models get no draft.
    assert names == ["raw_dump", "stg_orders"]
    assert not (tmp_path / "local" / "drafts" / "silver_orders.yml").exists()
    # The override restores the pre-gate reach exactly.
    written = write_drafts(tmp_path, report, include_registered=True)
    assert [Path(path).stem for path in written] == ["silver_orders"]
    bundle = (tmp_path / "local" / "drafts" / "silver_orders.yml")
    text = bundle.read_text(encoding="utf-8")
    assert "NOT Loom metadata" in text
    assert "document_type" not in text
    assert "current_state: known_not_controlled" in text
    assert "review_todos" in text
    # Never overwrites.
    assert write_drafts(tmp_path, report, include_registered=True) == []
    # Drafts are not loaded as inventory: validation still passes and
    # no duplicate-name clash appears.
    validate_project(tmp_path)


# --- Readiness -------------------------------------------------


def test_readiness_accounts_name_holes_and_kinds(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}

    # A clean fit with harvested description/owner is ready,
    # strictly: no holes, no blockers.
    silver = by_name["silver_orders"]["readiness"]
    assert silver == {
        "state": "ready_to_promote",
        "basis": "fit",
        "holes": [],
        "blockers": [],
    }
    # The staging fit carries two sentinel evidence holes (the
    # outputs the documented columns could not pair) and the
    # TODO description/owner authoring holes.
    staging = by_name["stg_orders"]["readiness"]
    assert staging["state"] == "needs_authoring"
    assert staging["basis"] == "fit"
    assert [hole["kind"] for hole in staging["holes"]] == [
        "evidence",
        "evidence",
        "authoring",
        "authoring",
    ]
    assert any(
        "'customer_id'" in hole["detail"] for hole in staging["holes"]
    )
    # The bronze fit's pass-through caveat is an evidence hole:
    # Loom could not verify the claim against documented columns.
    bronze = by_name["raw_dump"]["readiness"]
    assert bronze["state"] == "needs_authoring"
    assert [hole["kind"] for hole in bronze["holes"]] == ["evidence"]
    assert bronze["holes"][0]["detail"].startswith("caveat:")
    # No fit and no layer: not draftable, no draft basis.
    downstream = by_name["downstream"]["readiness"]
    assert downstream["state"] == "not_draftable"
    assert downstream["basis"] is None


def test_readiness_lives_in_record_and_report_never_instance(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    record = _load_yaml(_drafts_dir(tmp_path) / "stg_orders.yml")
    assert record["readiness"]["state"] == "needs_authoring"
    assert len(record["readiness"]["holes"]) == 4
    instance_text = (
        _drafts_dir(tmp_path) / "instances" / "stg_orders.yml"
    ).read_text(encoding="utf-8")
    assert "readiness" not in instance_text


def test_readiness_blocker_is_not_draftable(tmp_path: Path) -> None:
    # An inexpressible construct is a blocker, not a hole:
    # no generator can express it even once the transform is
    # authored, so the model is not draftable - though its
    # authoring draft is still written, carrying the account.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select id as customer_id, name as customer_name from "
        "{{ source('raw', 'customers') }} where id > 0",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}},
            }
        },
        columns={"customer_id": {}, "customer_name": {}},
    )
    readiness = record["readiness"]
    assert readiness["state"] == "not_draftable"
    assert readiness["basis"] == "authoring"
    assert any(
        "a row filter (WHERE)" in blocker
        for blocker in readiness["blockers"]
    )
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    draft = _load_yaml(_drafts_dir(tmp_path) / "stg_customers.yml")
    assert draft["readiness"]["state"] == "not_draftable"
    assert draft["readiness"]["blockers"] == readiness["blockers"]


def test_readiness_transition_when_holes_completed_in_draft(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    # The practitioner completes every hole in the draft
    # instance: real column mappings, a real description and
    # owner. The record on disk is never rewritten - the
    # re-assessment re-derives the account and reports the
    # transition against the stored one.
    instance_path = _drafts_dir(tmp_path) / "instances" / "stg_orders.yml"
    document = _load_yaml(instance_path)
    params = document["instances"][0]["params"]
    for entry in params["columns"]:
        entry["source"] = entry["alias"]
    params["description"] = "Orders as landed."
    params["owner"] = "analytics"
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_orders"
    )
    assert record["readiness"]["state"] == "ready_to_promote"
    assert record["readiness_transition"] == {
        "from": {"state": "needs_authoring", "holes": 4},
        "to": {"state": "ready_to_promote", "holes": 0},
    }
    assert reassessed["summary"]["readiness_transitions"] == 1
    text = render_convert_text(reassessed)
    assert "Readiness transitions (1):" in text
    assert "stg_orders: needs authoring (4 hole(s))" in text


def test_readiness_evidence_hole_closes_from_project_evidence(
    tmp_path: Path,
) -> None:
    # The other side of an evidence hole: the practitioner
    # documents the source columns, and a re-assessment pairs
    # the mappings itself - the untouched draft's account
    # improves, and the transition is reported.
    def build(source_columns: dict) -> None:
        nodes = {
            "model.legacy.stg_pair": _model(
                "stg_pair",
                deps=["source.legacy.raw.pairs"],
                raw=(
                    "select gamma as alpha, delta as beta from "
                    "{{ source('raw', 'pairs') }}"
                ),
                columns={"alpha": {}, "beta": {}},
                description="Pairs as landed.",
                meta={"owner": "analytics"},
            )
        }
        sources = {
            "source.legacy.raw.pairs": {
                "name": "pairs",
                "source_name": "raw",
                "resource_type": "source",
                "columns": source_columns,
            }
        }
        _manifest(tmp_path, nodes, sources)

    build({"gamma": {}, "delta": {}})
    report = migration_assessment(tmp_path)
    record = report["models"][0]
    assert record["readiness"]["state"] == "needs_authoring"
    assert len(record["readiness"]["holes"]) == 2
    write_drafts(tmp_path, report)

    build({"alpha": {}, "beta": {}})
    reassessed = migration_assessment(tmp_path)
    record = reassessed["models"][0]
    # D3 supersedes the last leg of the pre-D3 expectation
    # (updated with reasoning, pi-remediation Lane M2): the
    # account's own evidence holes close from the improved
    # project evidence, but the ON-DISK draft instance still
    # carries the unresolved placeholders, and readiness now
    # validates the effective instance - the file promotion
    # would move - so the model is not ready until the stale
    # draft is refreshed. The validation holes name the file.
    assert record["readiness"]["state"] == "needs_authoring"
    assert len(record["readiness"]["holes"]) == 1
    assert all(
        "unresolved placeholder" in hole["detail"]
        for hole in record["readiness"]["holes"]
    )
    # Refresh path: remove the stale instance, let convert
    # draft afresh from the improved evidence, and the account
    # reaches ready with the transition reported.
    (
        tmp_path / "local" / "drafts" / "instances" / "stg_pair.yml"
    ).unlink()
    write_drafts(tmp_path, reassessed)
    reassessed = migration_assessment(tmp_path)
    record = reassessed["models"][0]
    assert record["readiness"]["state"] == "ready_to_promote"
    assert record["readiness_transition"] == {
        "from": {"state": "needs_authoring", "holes": 2},
        "to": {"state": "ready_to_promote", "holes": 0},
    }


# ---------------------------------------------------------------
# M3 completeness tier: a fit draft accounts for every output
# the original model's final projection produces, not only the
# manifest's documented columns - an undocumented projected
# output is drafted as an unresolved placeholder (staging) or
# named in a caveat (bronze), and readiness can never certify
# the draft while it stands unaccounted.
# ---------------------------------------------------------------

_CUSTOMERS_SOURCE = {
    "source.legacy.raw.customers": {
        "name": "customers",
        "source_name": "raw",
        "resource_type": "source",
        "columns": {"id": {}, "name": {}},
    }
}

_JAFFLE_STG_CUSTOMERS_SQL = (
    "with source as (select * from {{ source('raw', 'customers') }}), "
    "renamed as (select id as customer_id, name as customer_name "
    "from source) select * from renamed"
)


def test_projected_outputs_enumerates_the_final_projection() -> None:
    # Plain columns name themselves; renames name their alias.
    assert _projected_outputs(
        "select id as customer_id, name from {{ source('raw', 'c') }}"
    ) == ["customer_id", "name"]
    # Qualified columns and implicit aliases.
    assert _projected_outputs(
        "select t.id, t.name customer_name from x t"
    ) == ["id", "customer_name"]
    # A bare star in the final statement is resolved through the
    # CTE it reads - the jaffle renamed-CTE shape.
    assert _projected_outputs(_JAFFLE_STG_CUSTOMERS_SQL) == [
        "customer_id",
        "customer_name",
    ]
    # DISTINCT does not change the output names.
    assert _projected_outputs("select distinct id, name from x") == [
        "id",
        "name",
    ]
    # A macro invocation names its alias.
    assert _projected_outputs(
        "select id, {{ cents_to_dollars('price') }} as dollars from x"
    ) == ["id", "dollars"]


def test_projected_outputs_bounds_return_none() -> None:
    # A star over a source: the outputs are whatever the source
    # carries - not enumerable from the SQL.
    assert (
        _projected_outputs("select * from {{ source('raw', 'c') }}")
        is None
    )
    # A star chain ending at a source star is the same bound.
    assert (
        _projected_outputs(
            "with a as (select * from {{ source('raw', 'c') }}) "
            "select * from a"
        )
        is None
    )
    # A star mixed with explicit items cannot be resolved either.
    assert _projected_outputs("select *, id from x") is None
    # An expression item does not name its output plainly enough
    # for a column mapping to account for it mechanically.
    assert _projected_outputs("select upper(name) as shout from x") is None
    # An unaliased macro call names no output.
    assert (
        _projected_outputs("select {{ cents_to_dollars('price') }} from x")
        is None
    )


def test_staging_fit_accounts_for_undocumented_projected_output(
    tmp_path: Path,
) -> None:
    # The jaffle stg_customers shape: the manifest documents the
    # key column only, so customer_name used to vanish from the
    # draft while readiness certified it ready. It is now
    # drafted as an unresolved placeholder and named in the
    # evidence, the review TODOs, and a readiness hole.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        _JAFFLE_STG_CUSTOMERS_SQL,
        ["source.legacy.raw.customers"],
        sources=_CUSTOMERS_SOURCE,
        columns={"customer_id": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    assert fit["drafted_params"]["columns"] == [
        {"source": "__TODO__", "alias": "customer_id"},
        {"source": "__TODO__", "alias": "customer_name"},
    ]
    evidence = " ".join(fit["evidence"])
    assert "does not document: customer_name" in evidence
    readiness = record["readiness"]
    assert readiness["state"] == "needs_authoring"
    assert any(
        hole["kind"] == "evidence" and "customer_name" in hole["detail"]
        for hole in readiness["holes"]
    )
    # The written draft record names the undocumented output in
    # its review TODOs, so the practitioner is told what to draft.
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    draft = _load_yaml(_drafts_dir(tmp_path) / "stg_customers.yml")
    assert any(
        "customer_name" in todo for todo in draft["review_todos"]
    )


def test_staging_fit_fully_documented_projection_unchanged(
    tmp_path: Path,
) -> None:
    # The regression guard: a fully documented projection
    # produces exactly the pre-M3 draft - the accounting adds
    # no placeholder, no evidence, and no hole.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select id as customer_id, name as customer_name from "
        "{{ source('raw', 'customers') }}",
        ["source.legacy.raw.customers"],
        sources=_CUSTOMERS_SOURCE,
        columns={"customer_id": {}, "customer_name": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    assert fit["drafted_params"]["columns"] == [
        {"source": "__TODO__", "alias": "customer_id"},
        {"source": "__TODO__", "alias": "customer_name"},
    ]
    evidence = " ".join(fit["evidence"])
    assert "does not document" not in evidence
    assert "could not be enumerated" not in evidence
    assert not any(
        "does not document" in hole["detail"]
        or "could not be enumerated" in hole["detail"]
        for hole in record["readiness"]["holes"]
    )


def test_staging_fit_unenumerable_projection_falls_back_to_named_hole(
    tmp_path: Path,
) -> None:
    # The bound: a star mixed with explicit items cannot be
    # enumerated, so the draft falls back to a named caveat -
    # an evidence hole - instead of silence.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select *, name as customer_name from "
        "{{ source('raw', 'customers') }}",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}, "email": {}},
            }
        },
        columns={"customer_name": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    evidence = " ".join(fit["evidence"])
    assert "could not be enumerated" in evidence
    readiness = record["readiness"]
    assert readiness["state"] == "needs_authoring"
    assert any(
        hole["kind"] == "evidence"
        and "could not be enumerated" in hole["detail"]
        for hole in readiness["holes"]
    )


def test_bronze_fit_accounts_for_undocumented_projected_output(
    tmp_path: Path,
) -> None:
    # A bronze draft carries no column mapping, so an explicitly
    # projected output beyond the documented set is accounted by
    # naming it in a caveat - an evidence hole - while the
    # pass-through claim itself is unchanged.
    record = _assess_one(
        tmp_path,
        "bronze_customers",
        "select id, name from {{ source('raw', 'customers') }}",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}},
            }
        },
        columns={"id": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "bronze"
    caveats = [e for e in fit["evidence"] if e.startswith("caveat:")]
    assert any("does not document: name" in c for c in caveats)
    readiness = record["readiness"]
    assert readiness["state"] == "needs_authoring"
    assert any(
        hole["kind"] == "evidence" and "name" in hole["detail"]
        for hole in readiness["holes"]
    )


def test_undocumented_output_hole_closes_when_mapping_completed(
    tmp_path: Path,
) -> None:
    # Completing only the mappings the draft names - in the
    # on-disk instance - closes the accounting hole on
    # re-assessment: the draft's readiness is derived, so the
    # transition is reported against the stored account.
    nodes = {
        "model.legacy.stg_customers": _model(
            "stg_customers",
            deps=["source.legacy.raw.customers"],
            raw=_JAFFLE_STG_CUSTOMERS_SQL,
            columns={"customer_id": {}},
            description="Customers as landed.",
            meta={"owner": "analytics"},
        )
    }
    _manifest(tmp_path, nodes, _CUSTOMERS_SOURCE)
    report = migration_assessment(tmp_path)
    record = report["models"][0]
    assert record["readiness"]["state"] == "needs_authoring"
    write_drafts(tmp_path, report)
    instance_path = (
        _drafts_dir(tmp_path) / "instances" / "stg_customers.yml"
    )
    document = _load_yaml(instance_path)
    params = document["instances"][0]["params"]
    completed = {"customer_id": "id", "customer_name": "name"}
    for entry in params["columns"]:
        entry["source"] = completed[entry["alias"]]
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = reassessed["models"][0]
    assert record["readiness"]["state"] == "ready_to_promote"
    assert record["readiness_transition"]["from"]["state"] == (
        "needs_authoring"
    )
    assert record["readiness_transition"]["to"]["state"] == (
        "ready_to_promote"
    )


def _bronze_node(name: str, path: str) -> dict:
    node = _model(
        name,
        deps=["source.legacy.raw.orders"],
        raw="select * from {{ source('raw', 'orders') }}",
    )
    node["original_file_path"] = path
    return node


def _staging_node(name: str, path: str) -> dict:
    node = _model(
        name,
        deps=["source.legacy.raw.orders"],
        raw=(
            "select id as order_id, customer_id from "
            "{{ source('raw', 'orders') }}"
        ),
        columns={"order_id": {}, "customer_id": {}},
    )
    node["original_file_path"] = path
    return node


def _draft_for(
    project: Path, node: dict, model_paths: list[str] | None = None
) -> dict:
    """Assess a one-model project and return its conversion draft."""
    if model_paths is not None:
        configured = ", ".join(f'"{path}"' for path in model_paths)
        (project / "dbt_project.yml").write_text(
            f"name: legacy\nprofile: legacy\nmodel-paths: [{configured}]\n",
            encoding="utf-8",
        )
    _manifest(project, {f"model.legacy.{node['name']}": node}, _SOURCE)
    report = migration_assessment(project)
    written = write_drafts(project, report)
    assert len(written) == 1
    return yaml.safe_load(Path(written[0]).read_text(encoding="utf-8"))


def _draft_params(project: Path, name: str) -> dict:
    """The drafted params of one model, read from its emitted
    loadable instance document (the bundle no longer embeds
    them; the instance document is the single source)."""
    document = yaml.safe_load(
        (
            project
            / "local"
            / "drafts"
            / "instances"
            / f"{name}.yml"
        ).read_text(encoding="utf-8")
    )
    assert "document_type" not in document
    (instance,) = document["instances"]
    assert instance["name"] == name
    return instance["params"]


def test_conversion_draft_proposes_nested_subpath(tmp_path: Path) -> None:
    bundle = _draft_for(
        tmp_path, _bronze_node("raw_extract", "models/finance/emea/raw_extract.sql")
    )
    assert bundle["generator"] == "bronze"
    assert _draft_params(tmp_path, "raw_extract")["output_subpath"] == "finance/emea"
    assert any(
        "does not move the file" in todo for todo in bundle["review_todos"]
    )


def test_conversion_draft_proposes_deep_subpath(tmp_path: Path) -> None:
    bundle = _draft_for(
        tmp_path, _bronze_node("raw_extract", "models/a/b/c/raw_extract.sql")
    )
    assert _draft_params(tmp_path, "raw_extract")["output_subpath"] == "a/b/c"


def test_conversion_draft_reads_root_from_dbt_project(tmp_path: Path) -> None:
    bundle = _draft_for(
        tmp_path,
        _bronze_node("raw_extract", "src/models/finance/raw_extract.sql"),
        model_paths=["src/models"],
    )
    assert _draft_params(tmp_path, "raw_extract")["output_subpath"] == "finance"


def test_conversion_draft_staging_layer_segment_not_doubled(
    tmp_path: Path,
) -> None:
    # The model's `staging` directory is the staging generator's own
    # layer segment (generation writes under
    # <root>/_generated/staging), not practitioner nesting: the
    # draft proposes no output_subpath, and the TODO declares the
    # move to the flat generated path instead of doubling the
    # segment into models/_generated/staging/staging/.
    bundle = _draft_for(
        tmp_path, _staging_node("stg_orders", "models/staging/stg_orders.sql")
    )
    assert bundle["generator"] == "staging"
    assert "output_subpath" not in _draft_params(tmp_path, "stg_orders")
    assert any(
        "models/_generated/staging/stg_orders.sql" in todo
        and "own layer segment" in todo
        for todo in bundle["review_todos"]
    )
    assert not any(
        "_generated/staging/staging" in todo for todo in bundle["review_todos"]
    )


def test_conversion_draft_staging_nested_under_layer_segment(
    tmp_path: Path,
) -> None:
    # Only the leading layer segment is stripped: nesting below it
    # is the practitioner's and is carried over.
    bundle = _draft_for(
        tmp_path,
        _staging_node("stg_orders", "models/staging/ecom/stg_orders.sql"),
    )
    assert _draft_params(tmp_path, "stg_orders")["output_subpath"] == "ecom"
    assert any(
        "models/_generated/staging/ecom/stg_orders.sql" in todo
        and "own layer segment" in todo
        for todo in bundle["review_todos"]
    )


def test_conversion_draft_staging_segment_not_first_is_untouched(
    tmp_path: Path,
) -> None:
    # A `staging` segment that does not lead the relative path is
    # practitioner nesting and is carried over whole.
    bundle = _draft_for(
        tmp_path,
        _staging_node("stg_orders", "models/ecom/staging/stg_orders.sql"),
    )
    assert (
        _draft_params(tmp_path, "stg_orders")["output_subpath"]
        == "ecom/staging"
    )
    assert any(
        "models/_generated/staging/ecom/staging/stg_orders.sql" in todo
        for todo in bundle["review_todos"]
    )
    assert not any(
        "own layer segment" in todo for todo in bundle["review_todos"]
    )


def test_conversion_draft_staging_under_generated_staging_stays(
    tmp_path: Path,
) -> None:
    bundle = _draft_for(
        tmp_path,
        _staging_node("stg_orders", "models/_generated/staging/raw/stg_orders.sql"),
    )
    assert _draft_params(tmp_path, "stg_orders")["output_subpath"] == "raw"
    assert any(
        "does not move the file" in todo for todo in bundle["review_todos"]
    )
    assert not any(
        "_generated/staging/_generated/staging" in todo
        for todo in bundle["review_todos"]
    )


def test_conversion_draft_explains_unsafe_directory(tmp_path: Path) -> None:
    bundle = _draft_for(
        tmp_path, _bronze_node("raw_extract", "models/my folder/raw_extract.sql")
    )
    assert "output_subpath" not in _draft_params(tmp_path, "raw_extract")
    assert any(
        "could not be carried over" in todo and "my folder" in todo
        for todo in bundle["review_todos"]
    )


def test_conversion_draft_explains_model_outside_model_paths(
    tmp_path: Path,
) -> None:
    bundle = _draft_for(
        tmp_path,
        _bronze_node("raw_extract", "elsewhere/raw_extract.sql"),
        model_paths=["models"],
    )
    assert "output_subpath" not in _draft_params(tmp_path, "raw_extract")
    assert any(
        "outside the configured model-paths" in todo
        for todo in bundle["review_todos"]
    )


def test_conversion_draft_flat_model_gets_no_subpath(
    tmp_path: Path,
) -> None:
    bundle = _draft_for(
        tmp_path, _bronze_node("raw_extract", "models/raw_extract.sql")
    )
    assert "output_subpath" not in _draft_params(tmp_path, "raw_extract")
    assert not any(
        "output_subpath" in todo for todo in bundle["review_todos"]
    )


def test_controlled_models_get_no_fit_and_no_drafts(tmp_path: Path) -> None:
    # sources.yml is a model-path YAML file, so it must
    # predate the manifest or the freshness guard (correctly)
    # reports the manifest as stale.
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n",
        encoding="utf-8",
    )
    _manifest(tmp_path, _nodes(), _SOURCE)
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
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    assert by_name["stg_orders"]["state"] == "controlled_by_loom"
    assert "fit" not in by_name["stg_orders"]
    assert report["summary"]["controlled_by_loom"] == 1
    written = write_drafts(tmp_path, report)
    assert all("stg_orders" not in path for path in written)


# --- Honesty pass ------------------------------------------------
# These fixtures mirror the synthetic manifest an audit proved
# the silent drops against: an incremental + grouped + granted
# staging model, an ephemeral twin, a versioned model, snapshot/seed
# nodes, and semantic/metric/unit-test sections.


def _honesty_nodes() -> dict:
    return {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
            config={
                "materialized": "incremental",
                "incremental_strategy": "merge",
                "unique_key": "order_id",
                "on_schema_change": "sync_all_columns",
                "group": "finance",
                "grants": {"select": ["analyst"]},
                "partition_by": {"field": "ordered_at", "data_type": "date"},
            },
        ),
        "model.legacy.stg_ephemeral": _model(
            "stg_ephemeral",
            deps=["source.legacy.raw.orders"],
            raw="select id as order_id from {{ source('raw', 'orders') }}",
            columns={"order_id": {}},
            config={"materialized": "ephemeral"},
        ),
        "model.legacy.dim_customers.v1": _model(
            "dim_customers", version=1, latest_version=2
        ),
        "model.legacy.dim_customers.v2": _model(
            "dim_customers", version=2, latest_version=2
        ),
        "snapshot.legacy.orders_snapshot": _model(
            "orders_snapshot",
            deps=["model.legacy.stg_orders"],
            resource_type="snapshot",
        ),
        "seed.legacy.country_codes": _model(
            "country_codes", resource_type="seed"
        ),
        "test.legacy.assert_positive": _model(
            "assert_positive",
            deps=["model.legacy.stg_orders"],
            resource_type="test",
        ),
    }


def _honesty_sections() -> dict:
    return {
        "exposures": {
            "exposure.legacy.exec_dash": {
                "name": "exec_dash",
                "resource_type": "exposure",
                "depends_on": {"nodes": ["model.legacy.stg_orders"]},
            }
        },
        "semantic_models": {
            "semantic_model.legacy.orders_sm": {
                "name": "orders_sm",
                "resource_type": "semantic_model",
                "depends_on": {"nodes": ["model.legacy.stg_orders"]},
            }
        },
        "metrics": {
            "metric.legacy.revenue": {
                "name": "revenue",
                "resource_type": "metric",
                "depends_on": {"nodes": ["semantic_model.legacy.orders_sm"]},
            }
        },
        "unit_tests": {
            "unit_test.legacy.stg_orders.test_x": {
                "name": "test_x",
                "resource_type": "unit_test",
                "depends_on": {"nodes": ["model.legacy.stg_orders"]},
            }
        },
    }


def test_assessment_flags_incremental_ephemeral_group_grants_physical(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, _honesty_nodes(), _SOURCE, _honesty_sections())
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}

    # The fit is still proposed (flags, not refusal)...
    staging = by_name["stg_orders"]
    assert staging["fit"]["generator"] == "staging"
    #...but nothing about its build semantics is silent any more.
    flags = " ".join(staging["interface_flags"])
    assert "materialized as 'incremental'" in flags
    assert "incremental_strategy='merge'" in flags
    assert "unique_key='order_id'" in flags
    assert "on_schema_change='sync_all_columns'" in flags
    assert "full rebuilds" in flags
    assert "group 'finance'" in flags
    assert "grants" in flags and "analyst" in flags
    assert "partition_by" in flags and "re-apply" in flags

    ephemeral_flags = " ".join(by_name["stg_ephemeral"]["interface_flags"])
    assert "materialized as 'ephemeral'" in ephemeral_flags


def test_assessment_counts_semantic_consumers_and_unit_tests(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, _honesty_nodes(), _SOURCE, _honesty_sections())
    report = migration_assessment(tmp_path)
    staging = {m["name"]: m for m in report["models"]}["stg_orders"]
    flags = " ".join(staging["interface_flags"])
    # Consumers: snapshot node + exposure + semantic model (the
    # metric hangs off the semantic model, not the model directly).
    assert "3 downstream consumer(s)" in flags
    # Attached tests: the singular test node + the unit test.
    assert "2 test(s) are attached" in flags
    # The count is decomposed by kind, in the flag and as
    # structured record data.
    assert "(1 snapshot, 1 exposure and 1 semantic model)" in flags
    assert staging["consumers_by_kind"] == {
        "snapshots": 1,
        "exposures": 1,
        "semantic models": 1,
    }
    # The metric reachable via the model's semantic model is
    # named - in the record and in a flag - not just counted.
    assert staging["semantic_layer"] == {
        "semantic_models": ["orders_sm"],
        "metrics": ["revenue"],
        "saved_queries": [],
    }
    assert "the semantic layer reads this model via semantic model(s) orders_sm" in flags
    assert "1 metric(s) reachable (revenue)" in flags


def test_assessment_reports_seen_but_not_assessed(tmp_path: Path) -> None:
    _manifest(tmp_path, _honesty_nodes(), _SOURCE, _honesty_sections())
    report = migration_assessment(tmp_path)
    assert report["summary"]["seen_but_not_assessed"] == {
        "metric": 1,
        "seed": 1,
        "semantic_model": 1,
        "snapshot": 1,
        "test": 1,
    }
    # Model counts stay model counts: 2 staging models + the
    # versioned pair.
    assert report["summary"]["project_models"] == 4


def test_assessment_distinguishes_model_versions(tmp_path: Path) -> None:
    _manifest(tmp_path, _honesty_nodes(), _SOURCE, _honesty_sections())
    report = migration_assessment(tmp_path)
    versions = sorted(
        m["version"] for m in report["models"] if m["name"] == "dim_customers"
    )
    assert versions == [1, 2]
    for record in report["models"]:
        if record["name"] == "dim_customers":
            assert record["latest_version"] == 2


def test_fit_drafts_carry_manifest_descriptions(tmp_path: Path) -> None:
    node = _model(
        "stg_orders",
        deps=["source.legacy.raw.orders"],
        raw=(
            "select id as order_id, customer_id from "
            "{{ source('raw', 'orders') }}"
        ),
        columns={
            "order_id": {"description": "Order identifier."},
            "customer_id": {"description": "The buyer."},
        },
        description="Orders staging extract.",
    )
    node["original_file_path"] = "models/stg_orders.sql"
    _manifest(tmp_path, {"model.legacy.stg_orders": node}, _SOURCE)
    report = migration_assessment(tmp_path)
    fit = report["models"][0]["fit"]
    # The model description is drafted verbatim, not as a TODO, and
    # the column descriptions are drafted into the column_docs
    # parameter (instance metadata carries a per-column
    # description field; the bundle sidecar and its
    # re-apply-by-hand TODO are retired).
    assert fit["drafted_params"]["description"] == "Orders staging extract."
    assert fit["drafted_params"]["column_docs"] == {
        "customer_id": "The buyer.",
        "order_id": "Order identifier.",
    }
    assert "column_descriptions" not in fit
    written = write_drafts(tmp_path, report)
    assert len(written) == 1
    bundle = yaml.safe_load(Path(written[0]).read_text(encoding="utf-8"))
    assert _draft_params(tmp_path, "stg_orders")["description"] == (
        "Orders staging extract."
    )
    assert _draft_params(tmp_path, "stg_orders")["column_docs"]["order_id"] == (
        "Order identifier."
    )
    assert "column_descriptions" not in bundle
    assert not any(
        "Re-apply the manifest's column descriptions" in todo
        for todo in bundle["review_todos"]
    )


# --- Harvest: declared facts copied into drafts ----------------------
# Description + owner, column_docs / column_types,
# declared incremental config, source rationale.
# The regression shape the owner asked for: one manifest model
# carrying every declared fact - drafts must carry them verbatim -
# and silent models keeping the TODO/boilerplate fallbacks.


def test_registration_drafts_harvest_description_and_owner(
    tmp_path: Path,
) -> None:
    node = _model(
        "orders_mart",
        description="Orders mart, one row per order.\nCurated by hand.",
        meta={"owner": "data-platform"},
    )
    _manifest(tmp_path, {"model.legacy.orders_mart": node}, _SOURCE)
    report = migration_assessment(tmp_path)
    draft = report["models"][0]["draft_registration"]
    assert draft["description"] == (
        "Orders mart, one row per order.\nCurated by hand."
    )
    assert draft["owner"] == "data-platform"
    assert "copied verbatim from the dbt manifest" in draft["evidence"]
    assert "meta.owner" in draft["evidence"]

    written = write_draft_registrations(tmp_path, report)
    assert len(written) == 1
    document = yaml.safe_load(Path(written[0]).read_text(encoding="utf-8"))
    assert document["description"] == (
        "Orders mart, one row per order.\nCurated by hand."
    )
    assert document["owner"] == "data-platform"


def test_registration_drafts_fall_back_when_manifest_is_silent(
    tmp_path: Path,
) -> None:
    node = _model("orders_mart")
    _manifest(tmp_path, {"model.legacy.orders_mart": node}, _SOURCE)
    report = migration_assessment(tmp_path)
    draft = report["models"][0]["draft_registration"]
    assert "description" not in draft
    assert "owner" not in draft

    written = write_draft_registrations(tmp_path, report)
    document = yaml.safe_load(Path(written[0]).read_text(encoding="utf-8"))
    assert document["description"].startswith(
        "Drafted by `loom register`"
    )
    assert document["owner"] == "TODO"


def test_owner_harvest_reads_meta_owner_only(tmp_path: Path) -> None:
    # No alias guessing: team / owner_email are not read,
    # and a non-string or empty owner is not a declaration.
    cases = [
        {"team": "data-platform"},
        {"owner_email": "owner@example.com"},
        {"owner": ""},
        {"owner": "   "},
        {"owner": 42},
    ]
    for index, meta in enumerate(cases):
        project = tmp_path / f"case{index}"
        project.mkdir()
        node = _model("orders_mart", meta=meta)
        _manifest(project, {"model.legacy.orders_mart": node}, _SOURCE)
        report = migration_assessment(project)
        draft = report["models"][0]["draft_registration"]
        assert "owner" not in draft, meta
        written = write_draft_registrations(project, report)
        document = yaml.safe_load(
            Path(written[0]).read_text(encoding="utf-8")
        )
        assert document["owner"] == "TODO", meta


def _rich_silver_project(tmp_path: Path) -> dict:
    """One silver candidate carrying every WP-A declared fact."""
    nodes = {
        "model.legacy.upstream": _model("upstream", raw="select 1"),
        "model.legacy.silver_settlements": _model(
            "silver_settlements",
            deps=["model.legacy.upstream"],
            raw=(
                "-- Dedup keeps the latest settlement per participant.\n"
                "-- Rationale: exports repeat settlements (issue 4552).\n"
                "select * from (select *, row_number() over "
                "(partition by participant_id, settlement_id "
                "order by updated_at desc) as rn "
                "from {{ ref('upstream') }}) where rn = 1"
            ),
            columns={
                "participant_id": {
                    "description": "The transit agency.",
                    "data_type": "STRING",
                },
                "settlement_id": {
                    "description": "Unique settlement identifier.",
                    "data_type": "INT64",
                },
            },
            description="Settlements deduplicated to the latest record.",
            meta={"owner": "payments-team"},
            config={
                "materialized": "incremental",
                "incremental_strategy": "merge",
                "unique_key": "settlement_id",
                "on_schema_change": "fail",
            },
        ),
    }
    nodes["model.legacy.silver_settlements"]["original_file_path"] = (
        "models/silver_settlements.sql"
    )
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    return next(
        m for m in report["models"] if m["name"] == "silver_settlements"
    )


def test_conversion_draft_harvests_owner_columns_types_incremental(
    tmp_path: Path,
) -> None:
    record = _rich_silver_project(tmp_path)
    fit = record["fit"]
    assert fit["generator"] == "silver"
    params = fit["drafted_params"]
    # Owner harvested from meta.owner, not TODO.
    assert params["owner"] == "payments-team"
    assert params["description"] == (
        "Settlements deduplicated to the latest record."
    )
    # Documented descriptions and types drafted verbatim -
    # the types keep the manifest's own spellings (STRING / INT64),
    # copied, never translated into Loom's generic vocabulary.
    assert params["column_docs"] == {
        "participant_id": "The transit agency.",
        "settlement_id": "Unique settlement identifier.",
    }
    assert params["column_types"] == {
        "participant_id": "STRING",
        "settlement_id": "INT64",
    }
    evidence = " ".join(fit["evidence"])
    assert "UNVERIFIED" in evidence
    assert "contract_enforced" in evidence
    # The declared incremental configuration is drafted into
    # the incremental block (incremental_strategy -> strategy)...
    assert params["incremental"] == {
        "strategy": "merge",
        "unique_key": "settlement_id",
        "on_schema_change": "fail",
    }
    #...so the loss-warning flag does not fire for this model.
    assert "incremental" not in " ".join(record.get("interface_flags", []))
    # The leading SQL comment block rides the proposal
    # verbatim.
    assert fit["source_rationale"] == (
        "-- Dedup keeps the latest settlement per participant.\n"
        "-- Rationale: exports repeat settlements (issue 4552)."
    )


def test_conversion_draft_bundle_carries_harvest(tmp_path: Path) -> None:
    _rich_silver_project(tmp_path)
    report = migration_assessment(tmp_path)
    written = write_drafts(tmp_path, report)
    assert len(written) == 1
    bundle = yaml.safe_load(Path(written[0]).read_text(encoding="utf-8"))
    params = _draft_params(tmp_path, "silver_settlements")
    assert params["owner"] == "payments-team"
    assert params["column_types"] == {
        "participant_id": "STRING",
        "settlement_id": "INT64",
    }
    assert params["incremental"]["strategy"] == "merge"
    assert bundle["source_rationale"].startswith(
        "-- Dedup keeps the latest settlement"
    )
    assert any(
        "source_rationale" in todo for todo in bundle["review_todos"]
    )


def test_incremental_partial_declaration_drafted_as_declared(
    tmp_path: Path,
) -> None:
    # Only unique_key declared: the block carries exactly that -
    # no strategy is invented - and the evidence names the gap.
    record = _assess_one_config(
        tmp_path,
        config={
            "materialized": "incremental",
            "unique_key": "order_id",
        },
    )
    params = record["fit"]["drafted_params"]
    assert params["incremental"] == {"unique_key": "order_id"}
    evidence = " ".join(record["fit"]["evidence"])
    assert "a strategy must be chosen" in evidence
    assert "incremental" not in " ".join(record.get("interface_flags", []))


def _assess_one_config(tmp_path: Path, *, config: dict) -> dict:
    nodes = {
        "model.legacy.upstream": _model("upstream", raw="select 1"),
        "model.legacy.silver_orders": _model(
            "silver_orders",
            deps=["model.legacy.upstream"],
            raw=(
                "select * from (select *, row_number() over "
                "(partition by order_id order by loaded_at desc) as rn "
                "from {{ ref('upstream') }}) where rn = 1"
            ),
            config=config,
        ),
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    return next(m for m in report["models"] if m["name"] == "silver_orders")


def test_incremental_flag_stays_without_a_carrying_draft(
    tmp_path: Path,
) -> None:
    nodes = {
        "model.legacy.upstream": _model("upstream", raw="select 1"),
        # No fit (bespoke shape): nothing drafts the configuration,
        # so the loss warning stays.
        "model.legacy.bespoke_incr": _model(
            "bespoke_incr",
            deps=["model.legacy.upstream"],
            raw=(
                "select x, sum(y) from {{ ref('upstream') }} "
                "group by x"
            ),
            config={
                "materialized": "incremental",
                "incremental_strategy": "merge",
                "unique_key": "x",
            },
        ),
        # A staging fit cannot carry the incremental block (the
        # staging contract admits no incremental parameter), so its
        # flag stays too - the loss is still real for staging.
        "model.legacy.stg_incr": _model(
            "stg_incr",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}},
            config={
                "materialized": "incremental",
                "incremental_strategy": "merge",
                "unique_key": "order_id",
            },
        ),
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    assert "no_fit" in by_name["bespoke_incr"]["fit"]
    assert "materialized as 'incremental'" in " ".join(
        by_name["bespoke_incr"]["interface_flags"]
    )
    staging = by_name["stg_incr"]
    assert staging["fit"]["generator"] == "staging"
    assert "incremental" not in staging["fit"]["drafted_params"]
    assert "materialized as 'incremental'" in " ".join(
        staging["interface_flags"]
    )


def test_source_rationale_skips_jinja_config_header(tmp_path: Path) -> None:
    node = _model(
        "stg_orders",
        deps=["source.legacy.raw.orders"],
        raw=(
            "{{ config(\n"
            "    materialized='table'\n"
            ") }}\n"
            "-- Why: raw orders land hourly and reloads must be cheap.\n"
            "-- Do not rename columns; downstream reads them by name.\n"
            "\n"
            "select id as order_id from {{ source('raw', 'orders') }}"
        ),
        columns={"order_id": {}},
    )
    node["original_file_path"] = "models/stg_orders.sql"
    _manifest(tmp_path, {"model.legacy.stg_orders": node}, _SOURCE)
    report = migration_assessment(tmp_path)
    fit = report["models"][0]["fit"]
    assert fit["generator"] == "staging"
    assert fit["source_rationale"] == (
        "-- Why: raw orders land hourly and reloads must be cheap.\n"
        "-- Do not rename columns; downstream reads them by name."
    )
    written = write_drafts(tmp_path, report)
    bundle = yaml.safe_load(Path(written[0]).read_text(encoding="utf-8"))
    assert bundle["source_rationale"] == fit["source_rationale"]


def test_no_source_rationale_without_comments(tmp_path: Path) -> None:
    record = _assess_one_config(tmp_path, config={"materialized": "table"})
    assert "source_rationale" not in record["fit"]


def test_source_rationale_harvests_all_comments_in_source_order(
    tmp_path: Path,
) -> None:
    # All comments are harvested: leading, inline and
    # trailing SQL comments are all copied, in source order, with
    # no relevance selection. Comment-like text inside string
    # literals is data, and Jinja comments are not SQL comments:
    # neither is harvested.
    record = _assess_one(
        tmp_path,
        "silver_orders",
        "{# jinja note: -- not a SQL comment #}\n"
        "-- Leading rationale line one.\n"
        "/* leading block\n"
        "   second line */\n"
        "select * from (\n"
        "  select *, 'a -- not a comment' as note,\n"
        "  'x /* also data */ y' as note2,\n"
        "  row_number() over (partition by order_id "
        "order by loaded_at desc) as rn\n"
        "  -- inline rationale inside the subquery\n"
        "  from {{ ref('upstream') }}\n"
        ") where rn = 1\n"
        "-- trailing rationale\n",
        ["model.legacy.upstream"],
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    assert fit["source_rationale"] == (
        "-- Leading rationale line one.\n"
        "/* leading block\n"
        "   second line */\n"
        "-- inline rationale inside the subquery\n"
        "-- trailing rationale"
    )
    assert "-- not a comment" not in fit["source_rationale"]
    assert "also data" not in fit["source_rationale"]
    assert "not a SQL comment" not in fit["source_rationale"]


def test_source_rationale_settlements_shape_inline_issue_links(
    tmp_path: Path,
) -> None:
    # Regression for the Cal-ITP spot-check that motivated the
    # widening: int_payments__settlements_deduped carries its
    # rationale inline inside a CTE (issue links at SQL lines
    # 10-13), not in a leading block, so the original
    # harvest found nothing. The widened harvest carries all
    # four comment lines verbatim.
    node = _model(
        "int_payments__settlements_deduped",
        deps=["model.legacy.int_littlepay__unioned_settlements"],
        raw=(
            "{{ config(materialized='view') }}\n"
            "WITH settlements_unioned AS (\n"
            "    SELECT *\n"
            "    FROM {{ ref('int_littlepay__unioned_settlements') }}\n"
            "),\n"
            "\n"
            "int_payments__settlements_deduped AS (\n"
            "    SELECT\n"
            "        *\n"
            "    FROM settlements_unioned\n"
            "    -- see: https://github.com/cal-itp/data-infra/issues/4552 and\n"
            "    -- https://github.com/cal-itp/data-infra/issues/5584\n"
            "    -- we have cases where same settlement comes in with two statuses\n"
            "    -- only want to keep one instance -- the more recent one\n"
            "    QUALIFY ROW_NUMBER() OVER\n"
            "        (PARTITION BY participant_id, settlement_id\n"
            "        ORDER BY record_updated_timestamp_utc DESC, littlepay_export_ts DESC, _line_number ASC) = 1\n"
            ")\n"
            "\n"
            "SELECT * FROM int_payments__settlements_deduped"
        ),
    )
    node["original_file_path"] = (
        "models/intermediate/payments/int_payments__settlements_deduped.sql"
    )
    nodes = {
        "model.legacy.int_littlepay__unioned_settlements": _model(
            "int_littlepay__unioned_settlements", raw="select 1"
        ),
        "model.legacy.int_payments__settlements_deduped": node,
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(
        m
        for m in report["models"]
        if m["name"] == "int_payments__settlements_deduped"
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    expected = (
        "-- see: https://github.com/cal-itp/data-infra/issues/4552 and\n"
        "-- https://github.com/cal-itp/data-infra/issues/5584\n"
        "-- we have cases where same settlement comes in with two statuses\n"
        "-- only want to keep one instance -- the more recent one"
    )
    assert fit["source_rationale"] == expected
    written = write_drafts(tmp_path, report)
    # The unified writer drafts every assessed model in one
    # pass: the silver fit below and the layered no-fit model
    # upstream of it (an authoring draft).
    assert len(written) == 2
    settlements = next(
        path for path in written if "settlements_deduped" in path
    )
    bundle = yaml.safe_load(Path(settlements).read_text(encoding="utf-8"))
    assert bundle["source_rationale"] == expected
    assert any(
        "prune" in todo for todo in bundle["review_todos"]
    )


# --- Fit rules read the model's output, not its tokens ----------------
#
# Regression fixtures, built from the CTE
# style of the three real projects the evaluation assessed
# (jaffle-shop, Stellar, Cal-ITP). Before the tightening the rules matched
# tokens anywhere in the file: 7 of 10 proposals read in full were
# semantic false positives.


def _assess_one(
    tmp_path: Path,
    name: str,
    raw: str,
    deps: list[str],
    *,
    sources: dict | None = None,
    columns: dict | None = None,
) -> dict:
    nodes: dict = {
        "model.legacy.upstream": _model("upstream", raw="select 1"),
    }
    nodes[f"model.legacy.{name}"] = _model(
        name, deps=deps, raw=raw, columns=columns
    )
    _manifest(tmp_path, nodes, sources)
    report = migration_assessment(tmp_path)
    return next(m for m in report["models"] if m["name"] == name)


def test_fit_bronze_rejects_cte_wrap_renames(tmp_path: Path) -> None:
    # The jaffle-shop idiom: renames inside CTEs, final SELECT *.
    # Bronze would emit the source's original column names and break
    # every downstream consumer; the honest proposal is staging,
    # whose column drafting exists for exactly this rename shape.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "with source as (select * from {{ source('raw', 'customers') }}), "
        "renamed as (select id as customer_id, name as customer_name "
        "from source) select * from renamed",
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
    )
    assert record["fit"]["generator"] == "staging"


def test_fit_bronze_rejects_distinct_and_transforms(tmp_path: Path) -> None:
    # Cal-ITP mart shape: SELECT DISTINCT plus a function transform
    # over a single source. Neither survives a bronze conversion.
    record = _assess_one(
        tmp_path,
        "dim_download_configs",
        "select distinct id, to_json_string(payload) as payload_json "
        "from {{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
    )
    assert "no_fit" in record["fit"]
    assert "SELECT DISTINCT" in record["fit"]["no_fit"]


def test_fit_bronze_accepts_explicit_columns_matching_source(
    tmp_path: Path,
) -> None:
    # An explicit column list is a pass-through when the manifest
    # documents the same columns on the model and on the source.
    sources = {
        "source.legacy.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}, "name": {}},
        }
    }
    record = _assess_one(
        tmp_path,
        "raw_customers",
        "select id, name from {{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=sources,
        columns={"id": {}, "name": {}},
    )
    assert record["fit"]["generator"] == "bronze"


def test_fit_bronze_subset_projection_falls_to_staging(
    tmp_path: Path,
) -> None:
    # A named subset projection with no documented columns to prove
    # it covers the source: bronze's SELECT * would add columns, so
    # the fit must not fire; staging drafts the subset explicitly.
    record = _assess_one(
        tmp_path,
        "raw_customers",
        "select id, name from {{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
    )
    assert record["fit"]["generator"] == "staging"


def test_fit_silver_rejects_window_tokens_off_the_output(
    tmp_path: Path,
) -> None:
    # Cal-ITP percentile mart: DISTINCT in the first CTE, a window
    # function, and a GROUP BY summary as the final shape. Nothing
    # is de-duplicated.
    record = _assess_one(
        tmp_path,
        "fct_message_age_summary",
        "with days as (select distinct day from {{ ref('upstream') }}), "
        "ages as (select day, percentile_cont(0.5) within group "
        "(order by age) over (partition by day) as median_age from days) "
        "select day, max(median_age) as median_age from ages group by day",
        ["model.legacy.upstream"],
    )
    assert "no_fit" in record["fit"]


def test_fit_silver_rejects_lag_lead_history(tmp_path: Path) -> None:
    # Cal-ITP feed-version history: ROW_NUMBER is an output column
    # alongside LAG/LEAD and no = 1 filter exists - every row is the
    # point of the model. A dedup conversion would delete history.
    record = _assess_one(
        tmp_path,
        "int_feed_version_history",
        "select feed_id, row_number() over (partition by feed_id "
        "order by version) as feed_version_number, lag(name) over "
        "(partition by feed_id order by version) as previous_name "
        "from {{ ref('upstream') }}",
        ["model.legacy.upstream"],
    )
    assert "no_fit" in record["fit"]
    assert "no dedup filter binding" in record["fit"]["no_fit"]


def test_fit_silver_strips_alias_prefix_from_dedup_key(
    tmp_path: Path,
) -> None:
    # Stellar's *_current shape: QUALIFY dedup over alias-qualified
    # columns. The drafted key must be the column, never the alias
    # (the original drafts proposed `dedup_key: a`).
    record = _assess_one(
        tmp_path,
        "accounts_current",
        "select * from {{ ref('upstream') }} a "
        "qualify row_number() over (partition by a.account_id "
        "order by a.closed_at desc) = 1",
        ["model.legacy.upstream"],
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    assert fit["drafted_params"]["dedup_key"] == "account_id"
    assert fit["drafted_params"]["dedup_order"] == "closed_at DESC"


def test_fit_silver_preserves_dedup_order_by(tmp_path: Path) -> None:
    # Cal-ITP's genuine QUALIFY dedup: the three-level ORDER BY is
    # the tie-break that decides which duplicate survives. It must
    # be drafted into dedup_order, not silently dropped.
    record = _assess_one(
        tmp_path,
        "int_payments__settlements_deduped",
        "with deduped as (select * from {{ ref('upstream') }} "
        "qualify row_number() over (partition by participant_id, "
        "settlement_id order by record_updated_timestamp_utc desc, "
        "littlepay_export_ts desc, _line_number asc) = 1) "
        "select * from deduped",
        ["model.legacy.upstream"],
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    assert fit["drafted_params"]["dedup_key"] == (
        "participant_id, settlement_id"
    )
    assert fit["drafted_params"]["dedup_order"] == (
        "record_updated_timestamp_utc DESC, "
        "littlepay_export_ts DESC, _line_number ASC"
    )
    assert any("dedup_order" in line for line in fit["evidence"])


def test_fit_silver_rejects_dedup_joined_downstream(tmp_path: Path) -> None:
    # A real dedup CTE whose result is joined to an aggregation
    # before output is not a silver model, however genuine the
    # dedup is in isolation.
    record = _assess_one(
        tmp_path,
        "accounts_enriched",
        "with deduped as (select * from {{ ref('upstream') }} a "
        "qualify row_number() over (partition by a.account_id "
        "order by a.closed_at desc) = 1), created as "
        "(select account_id, min(created_at) as created_at "
        "from {{ ref('upstream') }} group by account_id) "
        "select d.*, c.created_at from deduped d "
        "join created c on d.account_id = c.account_id",
        ["model.legacy.upstream"],
    )
    assert "no_fit" in record["fit"]


def test_fit_silver_rejects_distinct_only(tmp_path: Path) -> None:
    # Before the output-shape tightening a bare SELECT DISTINCT earned a silver fit with
    # `dedup_key: TODO`; a distinct is not a derivable dedup.
    record = _assess_one(
        tmp_path,
        "distinct_pairs",
        "select distinct a, b from {{ ref('upstream') }}",
        ["model.legacy.upstream"],
    )
    assert "no_fit" in record["fit"]
def test_interface_flags_ignore_dbt_default_access() -> None:
    # Dbt resolves config.access to 'protected'
    # for every model, configured or not (verified against a dbt
    # 1.12 manifest), so the original flag fired on 705/705 models
    # in real migration sessions. The default carries no
    # conversion consequence - a Loom-generated replacement sets no
    # access and lands on the same value - so it must not flag.
    defaulted = {"config": {"access": "protected", "materialized": "table"}}
    assert _interface_flags(defaulted, {}, attached_tests=0) == []
    # No access key at all is the same dbt default.
    assert _interface_flags({"config": {}}, {}, attached_tests=0) == []


def test_interface_flags_private_access_widening() -> None:
    # 'private' was previously exempt from flagging, yet a
    # Loom-generated replacement drops the restriction and defaults
    # to 'protected', silently widening who can reference the model.
    node = {"config": {"access": "private", "materialized": "table"}}
    flags = _interface_flags(node, {}, attached_tests=0)
    assert len(flags) == 1
    assert "access is 'private'" in flags[0]
    assert "widening" in flags[0]
    # The original message wrongly claimed outside consumers "may
    # depend on its interface" for non-private access; the private
    # flag must not repeat that claim.
    assert "may depend on its interface" not in flags[0]


def test_interface_flags_lead_with_contract_tests_consumers() -> None:
    # The flag list leads with the contract,
    # attached tests and downstream consumers - what a conversion
    # puts at risk for other people - before configuration flags
    # such as access.
    node = {
        "config": {
            "materialized": "table",
            "contract": {"enforced": True},
            "access": "public",
        }
    }
    flags = _interface_flags(
        node, {"models": 1, "exposures": 1}, attached_tests=3
    )
    assert "enforced dbt contract" in flags[0]
    assert "3 test(s)" in flags[1]
    assert "2 downstream consumer(s)" in flags[2]
    # The folded count is decomposed by kind in the flag.
    assert "(1 model and 1 exposure)" in flags[2]
    assert "access is 'public'" in flags[3]
# --- Manifest provenance (adapter mismatch) ---------------------------
# All three real projects failed `dbt parse` with their
# own toolchain and were rescued by a substitute-DuckDB parse; Loom
# consumed the fictional-adapter manifests with no warning. The
# substitute parse is blessed for assessment only - but labelled.


def _profile(project: Path, adapter: str) -> None:
    """Fabricate dbt_project.yml + profiles.yml whose active target
    declares the given adapter type (the shape the profile machinery
    reads). Written before the manifest so the manifest's
    generated_at post-dates them."""
    (project / "dbt_project.yml").write_text(
        yaml.safe_dump({"name": "legacy", "profile": "legacy"}),
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "legacy": {
                    "target": "dev",
                    "outputs": {"dev": {"type": adapter}},
                }
            }
        ),
        encoding="utf-8",
    )


def test_assessment_warns_on_substitute_adapter_parse(
    tmp_path: Path,
) -> None:
    # The canonical case: a BigQuery project parsed with a
    # substitute DuckDB profile.
    _profile(tmp_path, "bigquery")
    _manifest(tmp_path, _nodes(), _SOURCE, adapter_type="duckdb")
    report = migration_assessment(tmp_path)
    assert len(report["warnings"]) == 1
    warning = report["warnings"][0]
    assert "'duckdb'" in warning
    assert "'bigquery'" in warning
    assert "substitute-adapter parse" in warning
    # A warning, never a failure: the assessment itself is complete.
    assert report["summary"]["project_models"] == 5


def test_assessment_adapter_warning_surfaces_in_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _profile(tmp_path, "bigquery")
    _manifest(tmp_path, _nodes(), _SOURCE, adapter_type="duckdb")
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    # Report warnings render as a headed section - the
    # family headline, then the full generated text wrapped.
    assert "Warnings (1):" in text
    assert "! Adapter mismatch" in text
    assert "manifest was produced with the 'duckdb' adapter" in text
    # Both commands run the one engine, so the warning
    # surfaces in the convert half too.
    assert main(["convert", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Warnings (1):" in text
    assert "! Adapter mismatch" in text
    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert len(report["warnings"]) == 1
    assert "substitute-adapter parse" in report["warnings"][0]


def test_assessment_no_adapter_warning_when_adapters_match(
    tmp_path: Path,
) -> None:
    _profile(tmp_path, "bigquery")
    _manifest(tmp_path, _nodes(), _SOURCE, adapter_type="bigquery")
    report = migration_assessment(tmp_path)
    assert report["warnings"] == []


def test_assessment_no_adapter_warning_for_synapse_alias(
    tmp_path: Path,
) -> None:
    # Dbt-synapse registers (and profiles declare) the type
    # "synapse"; Loom's adapter map keys the same adapter
    # "azuresynapse". Either spelling on the profile side is a
    # match for a "synapse" manifest, not a mismatch.
    for label, profile_type in (
        ("declared", "synapse"),
        ("loom-key", "azuresynapse"),
    ):
        project = tmp_path / label
        project.mkdir()
        _profile(project, profile_type)
        _manifest(project, _nodes(), _SOURCE, adapter_type="synapse")
        report = migration_assessment(project)
        assert report["warnings"] == []


def test_assessment_no_adapter_warning_without_manifest_adapter(
    tmp_path: Path,
) -> None:
    _profile(tmp_path, "bigquery")
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    assert report["warnings"] == []
    assert report["summary"]["project_models"] == 5


def test_assessment_completes_without_resolvable_profile(
    tmp_path: Path,
) -> None:
    # No dbt_project.yml / profiles.yml at all: absence of evidence
    # is not a mismatch, and the assessment must not fail.
    _manifest(tmp_path, _nodes(), _SOURCE, adapter_type="bigquery")
    report = migration_assessment(tmp_path)
    assert report["warnings"] == []
    assert report["summary"]["project_models"] == 5


def test_assessment_completes_with_malformed_profiles(
    tmp_path: Path,
) -> None:
    # An unreadable profiles.yml is unresolvable, not fatal.
    (tmp_path / "dbt_project.yml").write_text(
        "name: legacy\nprofile: legacy\n", encoding="utf-8"
    )
    (tmp_path / "profiles.yml").write_text("legacy: [", encoding="utf-8")
    _manifest(tmp_path, _nodes(), _SOURCE, adapter_type="bigquery")
    report = migration_assessment(tmp_path)
    assert report["warnings"] == []
    assert report["summary"]["project_models"] == 5


# --- Pattern synthesis + promotable instance sets ---------------------
#
# Conversion drafts are emitted as a set: a review bundle per model,
# a loadable instance document per model, and - when the project has
# no pattern for a fitted generator - ONE synthesised draft pattern
# definition per generator for the whole run. Everything stays under
# local/drafts/ (never loaded) until the practitioner
# promotes the set into metadata/.


def _silver_model(name: str, upstream: str) -> dict:
    return _model(
        name,
        deps=[f"model.legacy.{upstream}"],
        raw=(
            "select * from (select *, row_number() over "
            "(partition by order_id order by loaded_at desc) as rn "
            f"from {{{{ ref('{upstream}') }}}}) where rn = 1"
        ),
    )


def _drafts_dir(project: Path) -> Path:
    return project / "local" / "drafts"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_one_synthesised_pattern_per_generator_for_whole_run(
    tmp_path: Path,
) -> None:
    nodes = {
        "model.legacy.upstream_a": _model("upstream_a"),
        "model.legacy.upstream_b": _model("upstream_b"),
        "model.legacy.silver_a": _silver_model("silver_a", "upstream_a"),
        "model.legacy.silver_b": _silver_model("silver_b", "upstream_b"),
    }
    _manifest(tmp_path, nodes)
    report = migration_assessment(tmp_path)
    written = write_drafts(tmp_path, report)
    assert len(written) == 2

    patterns = sorted(
        path.name for path in (_drafts_dir(tmp_path) / "patterns").glob("*.yml")
    )
    # Two silver fits, no existing pattern: exactly one synthesis.
    assert patterns == ["silver_pattern.yml"]
    definition = _load_yaml(
        _drafts_dir(tmp_path) / "patterns" / "silver_pattern.yml"
    )
    assert "document_type" not in definition
    assert definition["name"] == "silver_pattern"
    assert definition["generator"] == "silver"
    assert definition["required_params"] == ["input_models", "dedup_key"]

    for name in ("silver_a", "silver_b"):
        bundle = _load_yaml(_drafts_dir(tmp_path) / f"{name}.yml")
        assert bundle["suggested_pattern"]["name"] == "silver_pattern"
        assert "synthesised" in bundle["suggested_pattern"]["status"]
        assert bundle["instance"]["pattern"] == "silver_pattern"
        assert bundle["instance"]["file"] == (
            f"local/drafts/instances/{name}.yml"
        )
        instance_doc = _load_yaml(
            _drafts_dir(tmp_path) / "instances" / f"{name}.yml"
        )
        assert "document_type" not in instance_doc
        assert instance_doc["pattern"] == "silver_pattern"
        (instance,) = instance_doc["instances"]
        assert instance["name"] == name
        assert instance["params"]["dedup_key"] == "order_id"


def test_bundle_spells_out_promotion(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "silver_orders.yml")
    steps = "\n".join(bundle["promotion"])
    assert (
        "local/drafts/patterns/silver_pattern.yml" in steps
    )
    assert "metadata/patterns/" in steps
    assert "metadata/instances/" in steps
    assert "loom validate" in steps
    assert "equivalence" in steps


def test_fit_draft_carries_declared_interface(tmp_path: Path) -> None:
    # The copy half, under the unified draft: the declared
    # interface (contract, generic tests, unit tests, config) is
    # standard furniture of EVERY draft - a fit's record carries
    # the same verbatim copy a scaffold used to carry alone.
    nodes = {
        "model.legacy.stg_customers": _model(
            "stg_customers",
            deps=["source.legacy.raw.customers"],
            raw=(
                "select id as customer_id from "
                "{{ source('raw', 'customers') }}"
            ),
            columns={
                "customer_id": {
                    "data_type": "integer",
                    "constraints": [{"type": "not_null"}],
                }
            },
            config={
                "materialized": "view",
                "contract": {"enforced": True},
            },
        ),
        "test.legacy.not_null_stg_customers_customer_id": {
            "name": "not_null_stg_customers_customer_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "customer_id",
            "test_metadata": {
                "name": "not_null",
                "kwargs": {"column_name": "customer_id"},
            },
            "depends_on": {"nodes": ["model.legacy.stg_customers"]},
        },
    }
    sources = {
        "source.legacy.raw.customers": {
            "name": "customers",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}},
        }
    }
    sections = {
        "unit_tests": {
            "unit_test.legacy.stg_customers.test_ids": {
                "name": "test_ids",
                "resource_type": "unit_test",
                "depends_on": {"nodes": ["model.legacy.stg_customers"]},
                "given": [],
                "expect": {"rows": [{"customer_id": 1}]},
            }
        }
    }
    _manifest(tmp_path, nodes, sources, sections)
    report = migration_assessment(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "stg_customers"
    )
    assert record["fit"]["generator"] == "staging"
    interface = record["declared_interface"]
    assert interface["contract"]["enforced"] is True
    assert interface["contract"]["columns"]["customer_id"][
        "data_type"
    ] == "integer"
    assert interface["generic_tests"] == [
        {"test": "not_null", "column_name": "customer_id",
         "kwargs": {"column_name": "customer_id"}}
    ]
    assert interface["unit_tests"][0]["name"] == "test_ids"
    assert interface["config"]["materialized"] == "view"

    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_customers.yml")
    assert bundle["basis"] == "fit"
    assert bundle["declared_interface"] == interface


def test_promotion_is_move_only_with_final_pattern_name(
    tmp_path: Path,
) -> None:
    # The synthesised pattern carries its final name from
    # the start, the instance references it immediately, and the
    # promotion steps are moves - no rename step anywhere.
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "silver_orders.yml")
    assert bundle["suggested_pattern"]["name"] == "silver_pattern"
    assert bundle["instance"]["pattern"] == "silver_pattern"
    instance_doc = _load_yaml(
        _drafts_dir(tmp_path) / "instances" / "silver_orders.yml"
    )
    assert instance_doc["pattern"] == "silver_pattern"
    steps = "\n".join(bundle["promotion"])
    assert "renaming it as you do" not in steps
    assert "Move local/drafts/patterns/silver_pattern.yml into " \
        "metadata/patterns/ unchanged" in steps
    assert "Move local/drafts/instances/silver_orders.yml into " \
        "metadata/instances/ unchanged" in steps


def test_registered_model_promotion_names_registration_cleanup(
    tmp_path: Path,
) -> None:
    # Registration cleanup: a registered model's draft
    # spells out deleting its registration at promotion; an
    # unknown model's draft has no such step.
    _manifest(tmp_path, _nodes(), _SOURCE)
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "silver_orders.yml").write_text(
        "format_version: '1.0'\n"
        "name: silver_orders\nclassification: medallion silver\n",
        encoding="utf-8",
    )
    report = migration_assessment(tmp_path)
    # A registered model's draft exists only under the
    # registration-gate override.
    write_drafts(tmp_path, report, include_registered=True)
    registered = _load_yaml(_drafts_dir(tmp_path) / "silver_orders.yml")
    assert any(
        "Delete the model's registration "
        "(local/registrations/silver_orders.yml)" in step
        for step in registered["promotion"]
    )
    unknown = _load_yaml(_drafts_dir(tmp_path) / "stg_orders.yml")
    assert not any(
        "Delete the model's registration" in step
        for step in unknown["promotion"]
    )


def test_draft_set_is_never_overwritten(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    pattern_path = (
        _drafts_dir(tmp_path) / "patterns" / "silver_pattern.yml"
    )
    instance_path = _drafts_dir(tmp_path) / "instances" / "silver_orders.yml"
    # A practitioner's edit to the synthesised pattern must survive a
    # re-run exactly like a bundle does.
    edited = pattern_path.read_text(encoding="utf-8") + "# practitioner note\n"
    pattern_path.write_text(edited, encoding="utf-8")
    instance_before = instance_path.read_text(encoding="utf-8")
    assert write_drafts(tmp_path, report) == []
    assert pattern_path.read_text(encoding="utf-8") == edited
    assert instance_path.read_text(encoding="utf-8") == instance_before


def test_staging_fit_non_view_materialization_gets_a_named_todo(
    tmp_path: Path,
) -> None:
    # Owner ruling (2026-11-04): staging emits no materialization
    # config, so a staging candidate materialized as anything but
    # a view carries a named TODO on its draft record; a view (or
    # an undeclared materialization, dbt's default) carries none.
    def nodes(materialized: str | None) -> dict:
        return {
            "model.legacy.stg_orders": _model(
                "stg_orders",
                deps=["source.legacy.raw.orders"],
                raw=(
                    "select id as order_id, customer_id from "
                    "{{ source('raw', 'orders') }}"
                ),
                columns={"order_id": {}, "customer_id": {}},
                config=(
                    {"materialized": materialized} if materialized else {}
                ),
            )
        }

    for materialized, expected in (
        ("table", True),
        ("incremental", True),
        ("view", False),
        (None, False),
    ):
        project = tmp_path / (materialized or "default")
        project.mkdir()
        _manifest(project, nodes(materialized), _SOURCE)
        report = migration_assessment(project)
        write_drafts(project, report)
        record = yaml.safe_load(
            (project / "local" / "drafts" / "stg_orders.yml").read_text(
                encoding="utf-8"
            )
        )
        todos = " ".join(record["review_todos"])
        assert ("materialization change" in todos) == expected, materialized
        if expected:
            assert f"materialized as {materialized!r}" in todos


def test_write_drafts_reports_divergence_and_never_overwrites(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    assert write_drafts(tmp_path, report)
    assert "draft_divergences" not in report

    # A clean re-run is byte-stable: nothing written, nothing
    # reported divergent.
    report = migration_assessment(tmp_path)
    assert write_drafts(tmp_path, report) == []
    assert "draft_divergences" not in report

    # An edited instance is reported (kind: instance), never
    # overwritten; the record derived from its parsed params is
    # unchanged, so no record divergence stands.
    instance_path = _drafts_dir(tmp_path) / "instances" / "stg_orders.yml"
    edited = instance_path.read_text(encoding="utf-8") + "# note\n"
    instance_path.write_text(edited, encoding="utf-8")
    report = migration_assessment(tmp_path)
    assert write_drafts(tmp_path, report) == []
    divergences = report["draft_divergences"]
    assert [(e["model"], e["kind"]) for e in divergences] == [
        ("stg_orders", "instance")
    ]
    assert instance_path.read_text(encoding="utf-8") == edited
    assert "Draft divergences (1):" in render_convert_text(report)

    # An edited synthesised pattern and an edited record are
    # reported under their own kinds (the pattern once, though
    # several drafts share it).
    pattern_path = _drafts_dir(tmp_path) / "patterns" / "silver_pattern.yml"
    pattern_path.write_text(
        pattern_path.read_text(encoding="utf-8") + "# note\n",
        encoding="utf-8",
    )
    record_path = _drafts_dir(tmp_path) / "silver_orders.yml"
    record_path.write_text(
        record_path.read_text(encoding="utf-8") + "# note\n",
        encoding="utf-8",
    )
    report = migration_assessment(tmp_path)
    assert write_drafts(tmp_path, report) == []
    kinds = {(e["model"], e["kind"]) for e in report["draft_divergences"]}
    assert ("silver_pattern", "pattern") in kinds
    assert ("silver_orders", "record") in kinds


def test_drafts_are_not_loaded_as_inventory(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    # Nothing in metadata/ or local/registrations/: validation sees
    # an empty inventory, and a fresh assessment still reports the
    # fitted models as unknown - the drafts changed nothing.
    assert validate_project(tmp_path) == []
    again = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in again["models"]}
    assert by_name["stg_orders"]["state"] == "unknown_to_loom"
    assert by_name["silver_orders"]["state"] == "unknown_to_loom"


def test_existing_pattern_is_referenced_not_synthesised(
    tmp_path: Path,
) -> None:
    # Sources.yml must predate the manifest (freshness guard).
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n",
        encoding="utf-8",
    )
    _manifest(tmp_path, _nodes(), _SOURCE)
    write_pattern_instance(
        tmp_path,
        "other_staging",
        "staging",
        {
            "description": "An existing staging instance.",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "orders"},
            "columns": [{"source": "id", "alias": "order_id"}],
        },
        pattern="raw_staging",
    )
    # A silver pattern definition with no instances yet also counts
    # as existing: synthesis is only for generators with no pattern.
    definitions = tmp_path / "metadata" / "patterns"
    (definitions / "lonely_silver.yml").write_text(
        "name: lonely_silver\ngenerator: silver\n"
        "required_params: [input_models, dedup_key]\n",
        encoding="utf-8",
    )
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)

    staging_bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_orders.yml")
    # M3 (b): the existing-pattern branch now also carries the
    # pattern's required_params - raw_staging declares none.
    assert staging_bundle["suggested_pattern"] == {
        "name": "raw_staging",
        "required_params": [],
        "status": "existing pattern definition for this generator",
    }
    assert staging_bundle["instance"]["pattern"] == "raw_staging"
    silver_bundle = _load_yaml(_drafts_dir(tmp_path) / "silver_orders.yml")
    assert silver_bundle["instance"]["pattern"] == "lonely_silver"
    patterns = sorted(
        path.name for path in (_drafts_dir(tmp_path) / "patterns").glob("*.yml")
    )
    # Only bronze - the one generator with no pattern anywhere in the
    # project - is synthesised.
    assert patterns == ["bronze_pattern.yml"]


def test_several_existing_patterns_stay_a_practitioner_choice(
    tmp_path: Path,
) -> None:
    # Sources.yml must predate the manifest (freshness guard).
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n",
        encoding="utf-8",
    )
    _manifest(tmp_path, _nodes(), _SOURCE)
    for pattern, model in (
        ("raw_staging", "other_a"),
        ("alt_staging", "other_b"),
    ):
        write_pattern_instance(
            tmp_path,
            model,
            "staging",
            {
                "description": "An existing staging instance.",
                "owner": "analytics",
                "source_ref": {"name": "raw", "table": "orders"},
                "columns": [{"source": "id", "alias": "order_id"}],
            },
            pattern=pattern,
        )
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_orders.yml")
    assert bundle["suggested_pattern"]["name"] == "TODO"
    assert bundle["instance"]["pattern"] == "TODO"
    steps = "\n".join(bundle["promotion"])
    assert "alt_staging" in steps and "raw_staging" in steps
    assert not (
        _drafts_dir(tmp_path) / "patterns" / "staging_pattern.yml"
    ).exists()


def test_promoted_draft_set_moves_unchanged(
    tmp_path: Path,
) -> None:
    # The whole point of the final-form shape: promotion is
    # moving the emitted set into metadata/ unchanged - the files
    # below are byte-identical after the move, pattern names and
    # references already final. The unified-draft documents do
    # not emit `format_version`, and the inventory loader does
    # not read it either - so the promoted set validates exactly
    # as drafted.
    import shutil

    sources = {
        "source.legacy.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "resource_type": "source",
            # Documented source columns let the staging draft infer
            # the id -> order_id rename instead of drafting an
            # identity mapping the authored source cannot satisfy.
            "columns": {"id": {}, "customer_id": {}},
        },
        "source.legacy.crm.customers": {
            "name": "customers",
            "source_name": "crm",
            "resource_type": "source",
        },
    }
    nodes = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
        ),
        "model.legacy.raw_customers": _model(
            "raw_customers",
            deps=["source.legacy.crm.customers"],
            raw="select * from {{ source('crm', 'customers') }}",
        ),
        "model.legacy.silver_orders": _silver_model(
            "silver_orders", "stg_orders"
        ),
    }
    # Sources.yml must predate the manifest (freshness guard).
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: orders\n        columns:\n"
        "          - name: id\n          - name: customer_id\n",
        encoding="utf-8",
    )
    _manifest(tmp_path, nodes, sources)
    report = migration_assessment(tmp_path)
    written = write_drafts(tmp_path, report)
    assert len(written) == 3

    patterns_out = tmp_path / "metadata" / "patterns"
    instances_out = tmp_path / "metadata" / "instances"
    patterns_out.mkdir(parents=True)
    instances_out.mkdir(parents=True)
    for path in (_drafts_dir(tmp_path) / "patterns").glob("*.yml"):
        shutil.copy(path, patterns_out / path.name)
    for path in (_drafts_dir(tmp_path) / "instances").glob("*.yml"):
        shutil.copy(path, instances_out / path.name)

    # The move changed nothing: every promoted file is
    # byte-identical to the draft it came from.
    for source_dir, out_dir in (
        (_drafts_dir(tmp_path) / "patterns", patterns_out),
        (_drafts_dir(tmp_path) / "instances", instances_out),
    ):
        for path in source_dir.glob("*.yml"):
            assert (out_dir / path.name).read_bytes() == path.read_bytes()

    # The promoted set is loadable inventory exactly as drafted:
    # validation accepts it with no content edits, and the fitted
    # models are now generated instances.
    entries = {
        entry.data["name"]: entry
        for entry in validate_project(tmp_path)
    }
    assert entries["stg_orders"].is_generated
    assert entries["silver_orders"].is_generated
# --- Two-yield reporting, fits-first text -----------------------------
#
# A re-run found the assessment conflating register
# (625/625 on Cal-ITP) with convert (10/625) under the single word
# "migration", burying the first fit at line 902 of 1,326 in text
# output (P-9), claiming "no join signals" over UNNEST-flatten
# staging models (A-1), and asserting a bronze pass-through the
# manifest could not verify when it documented no source columns
# (A-2).


def test_convert_text_leads_with_readiness_and_fits_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    assert main(["convert", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()
    # The headline carries the run's mode: a bare convert is a
    # preview (dry run) under the write-mode convention.
    assert lines[0] == "Convert: legacy - dry run - nothing written"
    # The headline is the readiness distribution,
    # replacing the three yields: one draft is ready to promote,
    # two need authoring, and the two no-fit models here sit in
    # no dbt layer, so they are not draftable.
    assert lines[1] == (
        "Readiness: 1 ready to promote · 2 needs authoring "
        "· 2 not draftable"
    )
    # Fits-first (P-9): the Convert section precedes the no-fit
    # record, so conversions triage without reading the register.
    assert text.index("Conversion drafts proposed (3):") < text.index(
        "No fit, no layer (2):"
    )
    assert "stg_orders - convert: staging" in text
    assert "raw_dump - convert: bronze" in text
    assert "silver_orders - convert: silver" in text


def test_register_text_leads_with_readiness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()
    assert lines[0] == "Register: legacy - dry run - nothing written"
    assert lines[1] == (
        "Readiness: 1 ready to promote · 2 needs authoring "
        "· 2 not draftable"
    )
    assert "Unknown to Loom - action needed (5):" in text
    assert "stg_orders (staging)" in text


# --- The flat-verb CLI split ---
#
# A grouped spelling (`loom migration assess` /
# `loom migration plan`) was retired: the operations are the flat verbs `loom
# register` and `loom convert` over the one assessment engine,
# and `loom plan` stands alone (it phases both acts). No retired
# name is an alias.


def test_convert_write_drafts_writes_no_registrations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(tmp_path),
                "--write",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert len(report["drafts_written"]) == 3
    assert "registrations_written" not in report
    assert not (tmp_path / "local" / "registrations").exists()
    # The draft sets are complete: bundle, loadable
    # instance, and one synthesised pattern per fitted generator.
    drafts = tmp_path / "local" / "drafts"
    bundle = (drafts / "stg_orders.yml").read_text(encoding="utf-8")
    assert "Drafted by `loom convert`" in bundle
    assert (drafts / "instances" / "stg_orders.yml").exists()
    assert len(list((drafts / "patterns").glob("*.yml"))) == 3


def test_fit_staging_blocked_by_unnest(tmp_path: Path) -> None:
    # A-1: an UNNEST flattens an array column into rows - a join in
    # effect. The model below once earned a staging fit
    # whose evidence claimed "no join signals" over exactly the
    # construct that changes its grain (the Cal-ITP NTD shape).
    record = _assess_one(
        tmp_path,
        "stg_events",
        "select order_id, item from {{ source('raw', 'orders') }}, "
        "unnest(line_items) as item",
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
        columns={"order_id": {}, "item": {}},
    )
    assert "no_fit" in record["fit"]
    assert "UNNEST" in record["fit"]["no_fit"]


def test_fit_staging_evidence_names_unnest_among_checked_signals(
    tmp_path: Path,
) -> None:
    # The staging evidence sentence states exactly what the check
    # inspects; UNNEST is now one of the inspected signals.
    record = _assess_one(
        tmp_path,
        "stg_orders",
        "select id as order_id, customer_id from "
        "{{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
        columns={"order_id": {}, "customer_id": {}},
    )
    assert record["fit"]["generator"] == "staging"
    assert any(
        "no aggregation, join, UNNEST, window or macro signals" in line
        for line in record["fit"]["evidence"]
    )


def test_fit_staging_blocked_by_row_filter(tmp_path: Path) -> None:
    # A WHERE filter is not expressible by the staging (or
    # bronze) generator. It used to earn a staging fit that
    # silently dropped the filter.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select id as customer_id, name as customer_name from "
        "{{ source('raw', 'customers') }} where id > 0",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}},
            }
        },
        columns={"customer_id": {}, "customer_name": {}},
    )
    assert "no_fit" in record["fit"]
    assert "a row filter (WHERE)" in record["fit"]["no_fit"]


def test_fit_staging_blocked_by_row_filter_in_cte(tmp_path: Path) -> None:
    # The filter binds the output from inside the renamed CTE
    # (the jaffle idiom) just as one on the final statement does.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "with renamed as (select id as customer_id, "
        "name as customer_name from {{ source('raw', 'customers') }} "
        "where id > 0) select * from renamed",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}},
            }
        },
        columns={"customer_id": {}, "customer_name": {}},
    )
    assert "no_fit" in record["fit"]
    assert "a row filter (WHERE)" in record["fit"]["no_fit"]


def test_fit_staging_blocked_by_plain_sql_expression(
    tmp_path: Path,
) -> None:
    # A plain-SQL expression column raises no Jinja signal and
    # the staging draft is built from documentation, so the
    # computation used to be silently dropped by a fit.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select id as customer_id, upper(name) as customer_name "
        "from {{ source('raw', 'customers') }}",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}},
            }
        },
        columns={"customer_id": {}, "customer_name": {}},
    )
    assert "no_fit" in record["fit"]
    assert "plain-SQL column expression" in record["fit"]["no_fit"]
    assert "upper(name) as customer_name" in record["fit"]["no_fit"]


def test_fit_staging_blocked_by_limit(tmp_path: Path) -> None:
    # LIMIT: the model once earned a staging fit and the draft
    # silently dropped the limit.
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select id as customer_id, name as customer_name from "
        "{{ source('raw', 'customers') }} limit 10",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}},
            }
        },
        columns={"customer_id": {}, "customer_name": {}},
    )
    assert "no_fit" in record["fit"]
    assert "a LIMIT clause" in record["fit"]["no_fit"]


def test_fit_staging_comment_banners_are_not_expressions(
    tmp_path: Path,
) -> None:
    # The jaffle staging idiom: `-- ---------- ids` banner
    # comments sit inside the SELECT list above plain renames.
    # Comment text is not SQL - the fit must survive it (this
    # exact shape briefly demoted every jaffle staging model
    # when the expression detector first ran unstripped).
    record = _assess_one(
        tmp_path,
        "stg_customers",
        "select\n"
        "    ---------- ids\n"
        "    id as customer_id,\n"
        "    ---------- text\n"
        "    name as customer_name\n"
        "from {{ source('raw', 'customers') }}",
        ["source.legacy.raw.customers"],
        sources={
            "source.legacy.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "name": {}},
            }
        },
        columns={"customer_id": {}, "customer_name": {}},
    )
    assert record["fit"]["generator"] == "staging"


def test_fit_bronze_blocked_by_row_filter_and_limit(
    tmp_path: Path,
) -> None:
    # A bronze pass-through claim is over rows as well as columns:
    # `select * ... where/limit` is not the source unchanged.
    for suffix in ("where id > 0", "limit 10"):
        record = _assess_one(
            tmp_path,
            "raw_customers",
            f"select * from {{{{ source('raw', 'customers') }}}} {suffix}",
            ["source.legacy.raw.customers"],
            sources={
                "source.legacy.raw.customers": {
                    "name": "customers",
                    "source_name": "raw",
                    "resource_type": "source",
                    "columns": {"id": {}, "name": {}},
                }
            },
            columns={"id": {}, "name": {}},
        )
        assert "no_fit" in record["fit"], suffix


def test_fit_bronze_caveat_when_source_columns_undocumented(
    tmp_path: Path,
) -> None:
    # A-2: _SOURCE documents no columns, so the pass-through claim
    # cannot be checked against a documented source column list -
    # the proposal must carry the caveat.
    record = _assess_one(
        tmp_path,
        "raw_dump",
        "select * from {{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
    )
    assert record["fit"]["generator"] == "bronze"
    assert any(
        "documents no columns for the source" in line
        for line in record["fit"]["evidence"]
    )


def test_fit_bronze_no_caveat_when_source_columns_documented(
    tmp_path: Path,
) -> None:
    # The caveat fires exactly when it applies: a source whose
    # columns the manifest documents gets the plain claim.
    sources = {
        "source.legacy.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}, "total": {}},
        }
    }
    record = _assess_one(
        tmp_path,
        "raw_dump",
        "select * from {{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=sources,
    )
    assert record["fit"]["generator"] == "bronze"
    assert not any(
        "documents no columns" in line for line in record["fit"]["evidence"]
    )


# --- The scaffold tier ---
#
# For every model in a dbt layer that no generator rule matched,
# the assessment drafts a scaffold: a starting instance carrying
# only copied facts (layer, pre-wired inputs, declared interface),
# a target generator by layer as the authoring destination, and
# the transform as the single named authoring hole. A scaffold is
# never a fit claim - the word is kept out of its artefacts.


def _scaffold_nodes() -> dict:
    enriched = _model(
        "int_orders_enriched",
        deps=["model.legacy.stg_orders", "model.legacy.int_customers"],
        raw=(
            "select o.order_id, c.customer_id from "
            "{{ ref('stg_orders') }} o join {{ ref('int_customers') }} c "
            "on o.customer_id = c.customer_id"
        ),
        config={
            "materialized": "table",
            "contract": {"enforced": True},
            "access": "public",
            "group": "finance",
            "partition_by": {"field": "ordered_at", "data_type": "date"},
        },
        columns={
            "order_id": {
                "data_type": "integer",
                "constraints": [{"type": "not_null"}],
            },
            "customer_id": {"data_type": "integer"},
        },
        description="Orders enriched with customer aggregates.",
    )
    enriched["original_file_path"] = (
        "models/intermediate/int_orders_enriched.sql"
    )
    customers = _model(
        "int_customers",
        deps=["model.legacy.stg_orders"],
        raw=(
            "select customer_id, count(*) as orders from "
            "{{ ref('stg_orders') }} group by customer_id"
        ),
        config={"materialized": "table"},
    )
    customers["original_file_path"] = "models/intermediate/int_customers.sql"
    totals = _model(
        "int_daily_totals",
        deps=["model.legacy.stg_orders", "model.legacy.int_customers"],
        raw=(
            "select o.order_id from {{ ref('stg_orders') }} o "
            "join {{ ref('int_customers') }} c "
            "on o.customer_id = c.customer_id"
        ),
        config={"materialized": "view"},
    )
    totals["original_file_path"] = "models/intermediate/int_daily_totals.sql"
    revenue = _model(
        "fct_revenue",
        deps=["model.legacy.int_orders_enriched"],
        raw=(
            "select customer_id, sum(amount) as revenue from "
            "{{ ref('int_orders_enriched') }} group by customer_id"
        ),
        config={"materialized": "table"},
    )
    revenue["original_file_path"] = "models/mart/fct_revenue.sql"
    products = _model(
        "dim_products",
        deps=["model.legacy.stg_orders"],
        raw=(
            "select product_id, lag(price) over "
            "(partition by product_id order by day) as prev_price "
            "from {{ ref('stg_orders') }}"
        ),
        config={"materialized": "table"},
    )
    products["original_file_path"] = "models/mart/dim_products.sql"
    daily = _model(
        "daily_revenue",
        deps=["model.legacy.fct_revenue"],
        raw="select * from {{ ref('fct_revenue') }}",
        config={"materialized": "table"},
    )
    daily["original_file_path"] = "models/mart/daily_revenue.sql"
    events = _model(
        "stg_events",
        deps=["source.legacy.raw.orders"],
        raw=(
            "select order_id, item from {{ source('raw', 'orders') }}, "
            "unnest(line_items) as item"
        ),
        columns={"order_id": {}, "item": {}},
    )
    events["original_file_path"] = "models/staging/stg_events.sql"
    plain = _model(
        "revenue_rollup",
        deps=["model.legacy.stg_orders", "model.legacy.int_customers"],
        raw=(
            "select a.x, sum(b.y) from {{ ref('stg_orders') }} a "
            "join {{ ref('int_customers') }} b on a.id = b.id "
            "group by a.x"
        ),
    )
    plain["original_file_path"] = "models/revenue_rollup.sql"
    return {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
        ),
        "model.legacy.int_customers": customers,
        "model.legacy.int_orders_enriched": enriched,
        "model.legacy.int_daily_totals": totals,
        "model.legacy.fct_revenue": revenue,
        "model.legacy.dim_products": products,
        "model.legacy.daily_revenue": daily,
        "model.legacy.stg_events": events,
        "model.legacy.revenue_rollup": plain,
        "test.legacy.unique_int_orders_enriched_order_id": {
            "name": "unique_int_orders_enriched_order_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "order_id",
            "test_metadata": {
                "name": "unique",
                "kwargs": {
                    "column_name": "order_id",
                    "model": "ref('int_orders_enriched')",
                },
            },
            "depends_on": {"nodes": ["model.legacy.int_orders_enriched"]},
        },
        "test.legacy.relationships_int_orders_enriched_customer": {
            "name": "relationships_int_orders_enriched_customer",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "customer_id",
            "test_metadata": {
                "name": "relationships",
                "kwargs": {
                    "column_name": "customer_id",
                    "to": "ref('int_customers')",
                    "field": "customer_id",
                },
            },
            "depends_on": {"nodes": ["model.legacy.int_orders_enriched"]},
        },
        "test.legacy.accepted_values_int_orders_enriched_status": {
            "name": "accepted_values_int_orders_enriched_status",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "status",
            "test_metadata": {
                "name": "accepted_values",
                "kwargs": {
                    "column_name": "status",
                    "values": ["placed", "shipped"],
                    "model": "ref('int_orders_enriched')",
                },
            },
            "depends_on": {"nodes": ["model.legacy.int_orders_enriched"]},
        },
        # A singular test: bespoke SQL, no test_metadata at all.
        "test.legacy.test_enriched_orders_have_customers": {
            "name": "test_enriched_orders_have_customers",
            "resource_type": "test",
            "package_name": "legacy",
            "raw_code": (
                "select * from {{ ref('int_orders_enriched') }} "
                "where customer_id is null"
            ),
            "depends_on": {"nodes": ["model.legacy.int_orders_enriched"]},
        },
    }


def _scaffold_sections() -> dict:
    return {
        "unit_tests": {
            "unit_test.legacy.int_orders_enriched.test_enrichment": {
                "name": "test_enrichment",
                "resource_type": "unit_test",
                "depends_on": {
                    "nodes": [
                        "model.legacy.int_orders_enriched",
                        "model.legacy.stg_orders",
                        "model.legacy.int_customers",
                    ]
                },
                "given": [
                    {
                        "input": "ref('stg_orders')",
                        "rows": [{"order_id": 1, "customer_id": 7}],
                    },
                    {
                        "input": "ref('int_customers')",
                        "rows": [{"customer_id": 7}],
                    },
                ],
                "expect": {"rows": [{"order_id": 1, "customer_id": 7}]},
            }
        },
        "semantic_models": {
            "semantic_model.legacy.orders_enriched": {
                "name": "orders_enriched",
                "resource_type": "semantic_model",
                "depends_on": {
                    "nodes": ["model.legacy.int_orders_enriched"]
                },
                "entities": [
                    {"name": "order", "type": "primary", "expr": "order_id"}
                ],
                "dimensions": [
                    {
                        "name": "ordered_at",
                        "type": "time",
                        "type_params": {"time_granularity": "day"},
                    }
                ],
                "measures": [
                    {"name": "order_total", "agg": "sum", "expr": "amount"}
                ],
            }
        },
    }


def _scaffold_report(project: Path) -> dict:
    _manifest(project, _scaffold_nodes(), _SOURCE, _scaffold_sections())
    return migration_assessment(project)


def test_scaffold_for_layered_no_fit_model(tmp_path: Path) -> None:
    report = _scaffold_report(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    scaffold = by_name["int_orders_enriched"]["scaffold"]
    assert scaffold["layer"] == "intermediate"
    assert scaffold["target_generator"] == "normalized_table"
    assert scaffold["state"] == "untouched"
    # The exact input set from the manifest graph, pre-wired.
    assert scaffold["inputs"]["models"] == ["stg_orders", "int_customers"]
    # Two model inputs cannot wire a single-input generator's
    # input_models parameter; it stays in the hole rather than
    # being guessed.
    assert "input_models" not in scaffold["drafted_params"]
    hole = scaffold["authoring_hole"]
    assert hole["name"] == "transform"
    assert hole["params"] == [
        "input_models",
        "entity_columns",
        "attribute_columns",
    ]
    # The readiness distribution replaces the three
    # yields: the one fit and all seven authoring drafts carry
    # holes, and the one unlayered no-fit model is not
    # draftable.
    assert report["summary"]["readiness"] == {
        "ready_to_promote": 0,
        "needs_authoring": 8,
        "not_draftable": 1,
    }
    assert report["summary"]["scaffolds_promoted"] == 0


def test_scaffold_declared_interface_copied_verbatim(
    tmp_path: Path,
) -> None:
    report = _scaffold_report(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    interface = by_name["int_orders_enriched"]["scaffold"][
        "declared_interface"
    ]
    # Enforced contract: the declared column spec, verbatim.
    assert interface["contract"] == {
        "enforced": True,
        "columns": {
            "order_id": {
                "data_type": "integer",
                "constraints": [{"type": "not_null"}],
            },
            "customer_id": {"data_type": "integer"},
        },
    }
    # The unit test's fixtures are the proof oracle, verbatim.
    unit_test = interface["unit_tests"][0]
    assert unit_test["name"] == "test_enrichment"
    assert unit_test["given"] == [
        {"input": "ref('stg_orders')", "rows": [{"order_id": 1, "customer_id": 7}]},
        {"input": "ref('int_customers')", "rows": [{"customer_id": 7}]},
    ]
    assert unit_test["expect"] == {
        "rows": [{"order_id": 1, "customer_id": 7}]
    }
    # Generic tests: key candidacy and declared FK targets, copied
    # as declared - the relationships target is the project's own
    # declaration, not an inference from names.
    generic = {entry["test"]: entry for entry in interface["generic_tests"]}
    assert generic["unique"]["column_name"] == "order_id"
    assert generic["relationships"]["kwargs"]["to"] == "ref('int_customers')"
    assert generic["relationships"]["kwargs"]["field"] == "customer_id"
    # Accepted_values and singular tests are copied too -
    # before, both were counted in the bundle's test-count flag
    # but absent from the interface the proof checks against.
    assert generic["accepted_values"]["column_name"] == "status"
    assert generic["accepted_values"]["kwargs"]["values"] == [
        "placed",
        "shipped",
    ]
    assert generic["singular"]["name"] == (
        "test_enriched_orders_have_customers"
    )
    assert "customer_id is null" in generic["singular"]["sql"]
    # Semantic model: declared grain and keys, verbatim.
    semantic = interface["semantic_models"][0]
    assert semantic["entities"] == [
        {"name": "order", "type": "primary", "expr": "order_id"}
    ]
    assert semantic["measures"] == [
        {"name": "order_total", "agg": "sum", "expr": "amount"}
    ]
    # Model config facts.
    assert interface["config"] == {
        "materialized": "table",
        "partition_by": {"field": "ordered_at", "data_type": "date"},
        "access": "public",
        "group": "finance",
    }


def test_scaffold_targets_and_wiring_by_layer(tmp_path: Path) -> None:
    report = _scaffold_report(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}

    def scaffold_of(name: str) -> dict:
        return by_name[name]["scaffold"]

    # Mart layer: classification names the destination.
    assert scaffold_of("fct_revenue")["target_generator"] == "fact"
    assert scaffold_of("fct_revenue")["drafted_params"]["input_models"] == [
        "int_orders_enriched"
    ]
    assert scaffold_of("fct_revenue")["authoring_hole"]["params"] == [
        "dimension_keys",
        "measure_columns",
    ]
    assert scaffold_of("dim_products")["target_generator"] == "dimension"
    # Mart layer by folder alone (no naming prefix) → mart.
    assert scaffold_of("daily_revenue")["layer"] == "mart"
    assert scaffold_of("daily_revenue")["target_generator"] == "mart"
    # Intermediate declared as a view → view, inputs wired as a list.
    totals = scaffold_of("int_daily_totals")
    assert totals["target_generator"] == "view"
    assert totals["drafted_params"]["input_models"] == [
        "stg_orders",
        "int_customers",
    ]
    # The view contract requires select_columns (as the
    # renderer and validation always did), so the hole names it
    # instead of coming back empty.
    assert totals["authoring_hole"]["params"] == ["select_columns"]
    # Staging layer: the one source is wired as source_ref; the
    # column mapping is the hole.
    events = scaffold_of("stg_events")
    assert events["layer"] == "staging"
    assert events["target_generator"] == "staging"
    assert events["drafted_params"]["source_ref"] == {
        "name": "raw",
        "table": "orders",
    }
    assert events["authoring_hole"]["params"] == ["columns"]


def test_no_scaffold_without_layer_or_with_fit(tmp_path: Path) -> None:
    report = _scaffold_report(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    # No layer from folder or prefix: registration only.
    assert "scaffold" not in by_name["revenue_rollup"]
    # A generator fit leaves nothing to scaffold.
    assert by_name["stg_orders"]["fit"]["generator"] == "staging"
    assert "scaffold" not in by_name["stg_orders"]


def test_scaffold_drafts_written_once_and_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _scaffold_nodes(), _SOURCE, _scaffold_sections())
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(tmp_path),
                "--write",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    # Seven authoring drafts plus the fixture's one fit draft,
    # written by the one unified writer.
    assert len(report["drafts_written"]) == 8
    scaffolds = tmp_path / "local" / "drafts"
    bundle_path = scaffolds / "int_orders_enriched.yml"
    bundle_text = bundle_path.read_text(encoding="utf-8")
    bundle = yaml.safe_load(bundle_text)
    assert "document_type" not in bundle
    assert bundle["generator"] == "normalized_table"
    assert bundle["basis"] == "authoring"
    assert bundle["declared_interface"]["contract"]["enforced"] is True
    instance_path = scaffolds / "instances" / "int_orders_enriched.yml"
    instance_text = instance_path.read_text(encoding="utf-8")
    instance = yaml.safe_load(instance_text)
    assert instance["pattern"] == "normalized_table_pattern"
    params = instance["instances"][0]["params"]
    assert params["description"] == (
        "Orders enriched with customer aggregates."
    )
    assert "input_models" not in params
    # One synthesised pattern per target generator for the run,
    # shared with the conversion sets (staging is both a fitted
    # generator here and a scaffold target).
    patterns = sorted(
        path.name
        for path in (tmp_path / "local" / "drafts" / "patterns").glob(
            "*.yml"
        )
    )
    assert patterns == [
        "dimension_pattern.yml",
        "fact_pattern.yml",
        "mart_pattern.yml",
        "normalized_table_pattern.yml",
        "staging_pattern.yml",
        "view_pattern.yml",
    ]
    # The synthesised view pattern's required params come
    # from the generator contract, which now names select_columns
    # - a promoted view set validates with the hole filled.
    view_pattern = yaml.safe_load(
        (
            tmp_path
            / "local"
            / "drafts"
            / "patterns"
            / "view_pattern.yml"
        ).read_text(encoding="utf-8")
    )
    assert view_pattern["required_params"] == [
        "input_models",
        "select_columns",
    ]
    # The hard rule, restated for the unified draft: an
    # authoring draft makes no fit claim - its basis is
    # `authoring`, its generator is named by layer with the
    # no-shape-matched note, and the only place the fit
    # vocabulary appears is the no_fit_reason's negation (the
    # named blocking account the draft must carry).
    bundle_doc = yaml.safe_load(bundle_text)
    assert bundle_doc["basis"] == "authoring"
    assert "no generator shape was matched" in bundle_text
    assert "no generator shape was matched" in bundle_doc["generator_note"]
    assert not re.search(r"\bfit\b", instance_text, re.IGNORECASE)
    # Never overwrite: a re-run writes nothing and the bundle is
    # byte-identical.
    report2 = migration_assessment(tmp_path)
    assert write_drafts(tmp_path, report2) == []
    assert bundle_path.read_text(encoding="utf-8") == bundle_text


def test_convert_text_readiness_headline_and_scaffold_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _scaffold_nodes(), _SOURCE, _scaffold_sections())
    assert main(["convert", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()
    assert lines[1] == (
        "Readiness: 0 ready to promote · 8 needs authoring "
        "· 1 not draftable"
    )
    # Tier order in the body: fits, then scaffolds, then the
    # remaining no-fit record (the one unlayered bespoke model).
    assert text.index("Conversion drafts proposed (1):") < text.index(
        "Scaffolds drafted (7):"
    )
    assert text.index("Scaffolds drafted (7):") < text.index(
        "No fit, no layer (1):"
    )
    # The default scaffold section is the layer × target
    # grouping; the per-model scaffold lines (inputs, state) are
    # the verbose form.
    assert "intermediate → normalized_table: 2" in text
    assert (
        main(["convert", "--project-dir", str(tmp_path), "--verbose"]) == 0
    )
    verbose_text = capsys.readouterr().out
    assert (
        "int_orders_enriched (unknown_to_loom) - scaffold: intermediate "
        "layer, target normalized_table; inputs: stg_orders, "
        "int_customers" in verbose_text
    )
    assert "revenue_rollup - no fit:" in text


def test_scaffold_state_promoted_on_reassessment(tmp_path: Path) -> None:
    totals = _model(
        "int_order_totals",
        deps=["model.legacy.stg_orders"],
        raw=(
            "select customer_id, sum(total) as total from "
            "{{ ref('stg_orders') }} group by customer_id"
        ),
        config={"materialized": "table"},
        columns={"customer_id": {}, "total": {}},
    )
    totals["original_file_path"] = "models/intermediate/int_order_totals.sql"
    nodes = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id, total from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}, "total": {}},
        ),
        "model.legacy.int_order_totals": totals,
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "int_order_totals"
    )
    # Before: a scaffold, untouched; its one model input is wired.
    assert record["scaffold"]["state"] == "untouched"
    assert record["scaffold"]["drafted_params"]["input_models"] == [
        "stg_orders"
    ]
    assert write_drafts(tmp_path, report)
    # The practitioner registers the input, authors the transform
    # (completing the hole), and promotes the instance into the
    # load path.
    registrations = {
        **report,
        "models": [
            m for m in report["models"] if m["name"] == "stg_orders"
        ],
    }
    write_draft_registrations(tmp_path, registrations)
    write_pattern_instance(
        tmp_path,
        "int_order_totals",
        "normalized_table",
        {
            "input_models": ["stg_orders"],
            "entity_columns": ["customer_id"],
            "attribute_columns": ["total"],
            "description": "Order totals by customer.",
            "owner": "analytics",
        },
        pattern="order_totals_normalized",
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "int_order_totals"
    )
    assert record["state"] == "controlled_by_loom"
    # The re-assessment reports the scaffold as promoted, derived
    # locally from the drafts directory and the load path.
    assert record["scaffold"]["state"] == "promoted"
    assert reassessed["summary"]["scaffolds_promoted"] == 1
    # The promoted model leaves the readiness distribution
    # (it is controlled now); the input model, registered by
    # the practitioner above, still carries its account -
    # registration gates drafting, not reporting.
    assert reassessed["summary"]["readiness"] == {
        "ready_to_promote": 0,
        "needs_authoring": 1,
        "not_draftable": 0,
    }


# --- Register/convert invoke the manifest freshness guard --------
#
# `check`, `coverage` and `plan`
# hard-failed on a stale manifest naming the stale files, while
# `register`/`convert` - whose output derives entirely from the
# manifest - exited 0 with no warning in text or JSON. The
# assessment now invokes the same guard (`validate_model_freshness`)
# as the sibling commands, before any report is produced.


def _make_manifest_stale(project: Path) -> Path:
    """Touch one model file so it is newer than the manifest.

    Returns the stale file. Mirrors the plan suite's stale-manifest
    construction (mtime bumped past generated_at)."""
    manifest = project / "target" / "manifest.json"
    generated_at = datetime.fromisoformat(
        json.loads(manifest.read_text(encoding="utf-8"))["metadata"][
            "generated_at"
        ]
    )
    stale = project / "models" / "stg_orders.sql"
    future = generated_at.timestamp() + 60
    os.utime(stale, (future, future))
    return stale


def test_assessment_rejects_stale_manifest(tmp_path: Path) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    stale = _make_manifest_stale(tmp_path)
    with pytest.raises(LoomError, match="dbt manifest is stale") as excinfo:
        migration_assessment(tmp_path)
    # The error names the stale file, exactly as plan/coverage do.
    # The path is project-relative - the form
    # a practitioner navigates by - listed with its reason.
    assert "stg_orders.sql" in str(excinfo.value)
    assert "models/stg_orders.sql (changed after parsing)" in str(excinfo.value)
    assert str(stale) not in str(excinfo.value)
    # It also names the remedy, like the not-found error.
    assert "re-run `dbt parse` to refresh the manifest" in str(excinfo.value)


@pytest.mark.parametrize("command", ["register", "convert"])
@pytest.mark.parametrize("format_args", [[], ["--format", "json"]])
def test_register_convert_reject_stale_manifest_in_both_formats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    format_args: list[str],
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    _make_manifest_stale(tmp_path)
    assert (
        main([command, "--project-dir", str(tmp_path), *format_args]) == 1
    )
    captured = capsys.readouterr()
    # Hard fail before any assessment output, in either format:
    # nothing on stdout; the stale error on stderr.
    assert captured.out == ""
    assert "dbt manifest is stale" in captured.err
    assert "stg_orders.sql" in captured.err
    assert "changed after parsing" in captured.err
    assert "re-run `dbt parse` to refresh the manifest" in captured.err


@pytest.mark.parametrize("command", ["register", "convert"])
@pytest.mark.parametrize("format_args", [[], ["--format", "json"]])
def test_register_convert_accept_fresh_manifest_in_both_formats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    format_args: list[str],
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    assert (
        main([command, "--project-dir", str(tmp_path), *format_args]) == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    if format_args:
        assert json.loads(captured.out)["summary"]["project_models"] == 5
    else:
        assert "State: 5 project models" in captured.out
# --- Provenance declaration + bounded fit abstentions -------------
#
# Regression fixtures, extracted
# from the round's Cal-ITP run: the enghouse silver draft whose
# QUALIFY carries an OR escape, the two GTFS staging drafts that
# rendered struct-field extraction as identity renames, and the
# settlements silver fit that must survive both abstentions. The
# SQL below is the models' authored SQL verbatim (trimmed only of
# the trailing config block dbt adds in the manifest).

_ENGHOUSE_TRANSACTIONS_SQL = """
WITH transactions AS (
    SELECT
        operator_id,
        id,
        operation,
        terminal_id,
        mapping_terminal_id,
        mapping_merchant_id,
        timestamp,
        amount,
        payment_reference,
        spdh_response,
        response_type,
        response_message,
        token,
        issuer_response,
        core_response,
        rrn,
        authorization_code,
        par,
        brand,
        agency,
        dt,
        _line_number,
        _payments_key,
        _payment_reference_key,
        _content_hash
    FROM {{ ref('stg_enghouse__transactions') }}
),

filtered_transactions AS (
    SELECT
        *,
        CASE
            WHEN operation = 'REFUND' THEN 'CREDIT'
            ELSE 'DEBIT'
        END AS settlement_type,
    FROM transactions
    WHERE operation in ('DEBT_RECOVERY_AUTO', 'ONLINE_CLEARING', 'DEBT_RECOVERY_MANUAL', 'PREAUTH_FINAL', 'REFUND')
),

int_payments__transactions_deduped_enghouse AS (
    SELECT * FROM filtered_transactions
    -- Per operator_id + payment_reference, keep the most recent row of each settlement_type, so a
    -- CREDIT (refund) and a DEBIT (non-refund) for the same payment_reference are kept as separate
    -- rows. payment_reference is not unique between operators.
    --
    -- Rows with null payment reference are not expected, but would be removed otherwise,
    -- so this keeps them.
    QUALIFY
        ROW_NUMBER() OVER (
            PARTITION BY operator_id, payment_reference, settlement_type
            ORDER BY (timestamp IS NOT NULL) DESC, timestamp DESC, dt DESC
        ) = 1
        OR payment_reference IS NULL
)

SELECT
    operator_id,
    id,
    operation,
    terminal_id,
    mapping_terminal_id,
    mapping_merchant_id,
    timestamp,
    amount,
    payment_reference,
    spdh_response,
    response_type,
    response_message,
    token,
    issuer_response,
    core_response,
    rrn,
    authorization_code,
    par,
    brand,
    settlement_type,
    agency,
    dt,
    _line_number,
    _payments_key,
    _payment_reference_key,
    _content_hash
FROM int_payments__transactions_deduped_enghouse
"""

_ENGHOUSE_COLUMNS = [
    "operator_id", "id", "operation", "terminal_id",
    "mapping_terminal_id", "mapping_merchant_id", "timestamp",
    "amount", "payment_reference", "spdh_response", "response_type",
    "response_message", "token", "issuer_response", "core_response",
    "rrn", "authorization_code", "par", "brand", "settlement_type",
    "agency", "dt", "_line_number", "_payments_key",
    "_payment_reference_key", "_content_hash",
]

# The input model documents 25 of the 26 (no settlement_type - it
# is derived by the CASE in the model above).
_ENGHOUSE_INPUT_COLUMNS = [
    c for c in _ENGHOUSE_COLUMNS if c != "settlement_type"
]

_SERVICE_ALERTS_SQL = """
WITH stg_gtfs_rt__service_alerts AS (
    SELECT
        dt,
        hour,
        base64_url,

        metadata.extract_ts AS _extract_ts,
        metadata.extract_config.extracted_at AS _config_extract_ts,
        metadata.extract_config.name AS _name,

        TIMESTAMP_SECONDS(header.timestamp) AS header_timestamp,
        header.incrementality AS header_incrementality,
        header.gtfsRealtimeVersion AS header_version,

        id,

        alert.activePeriod AS active_period,
        alert.informedEntity AS informed_entity,

        alert.cause AS cause,
        alert.effect AS effect,

        alert.headerText AS header_text,
        alert.descriptionText AS description_text,
        alert.ttsHeaderText AS tts_header_text,
        alert.ttsDescriptionText AS tts_description_text,
        alert.url AS url,

        alert.severityLevel AS severity_level

    FROM {{ source('external_gtfs_rt', 'service_alerts') }}
)

SELECT * FROM stg_gtfs_rt__service_alerts
"""

_VALIDATION_NOTICES_SQL = """
WITH raw_validation_notices AS (
    SELECT * FROM {{ source('external_gtfs_schedule', 'validation_notices') }}
),

stg_gtfs_schedule__validation_notices AS (
    SELECT
        dt,
        base64_url,
        ts,
        metadata.gtfs_validator_version,
        metadata.extract_config.name AS name,
        metadata.extract_config.url AS url,
        metadata.extract_config.feed_type AS feed_type,
        metadata.extract_config.extracted_at AS _config_extract_ts,
        code,
        severity,
        totalNotices AS total_notices,
        sampleNotices AS sample_notices
    FROM raw_validation_notices
)

SELECT * FROM stg_gtfs_schedule__validation_notices
"""

_SETTLEMENTS_SQL = """
WITH settlements_unioned AS (
    SELECT *
    FROM {{ ref('int_littlepay__unioned_settlements') }}
),

int_payments__settlements_deduped AS (
    SELECT
        *
    FROM settlements_unioned
    -- see: https://github.com/cal-itp/data-infra/issues/4552 and
    -- https://github.com/cal-itp/data-infra/issues/5584
    -- we have cases where same settlement comes in with two statuses
    -- only want to keep one instance -- the more recent one
    QUALIFY ROW_NUMBER() OVER
        (PARTITION BY participant_id, settlement_id
        ORDER BY record_updated_timestamp_utc DESC, littlepay_export_ts DESC, _line_number ASC) = 1
)

SELECT * FROM int_payments__settlements_deduped
"""

_SETTLEMENTS_COLUMNS = [
    "settlement_id", "participant_id", "aggregation_id",
    "customer_id", "funding_source_id", "settlement_type",
    "transaction_amount", "retrieval_reference_number",
    "littlepay_reference_number", "external_reference_number",
    "record_updated_timestamp_utc", "acquirer",
    "acquirer_response_rrn", "settlement_status",
    "request_created_timestamp_utc", "response_created_timestamp_utc",
    "refund_id", "littlepay_export_date", "littlepay_export_ts",
    "_line_number", "_key", "_payments_key", "_content_hash",
    "feed_version", "instance", "extract_filename", "ts",
]


def _calitp_nodes() -> dict:
    nodes = {
        "model.legacy.stg_enghouse__transactions": _model(
            "stg_enghouse__transactions",
            raw="select 1",
            columns={c: {} for c in _ENGHOUSE_INPUT_COLUMNS},
        ),
        "model.legacy.int_payments__transactions_deduped_enghouse": _model(
            "int_payments__transactions_deduped_enghouse",
            deps=["model.legacy.stg_enghouse__transactions"],
            raw=_ENGHOUSE_TRANSACTIONS_SQL,
            columns={c: {} for c in _ENGHOUSE_COLUMNS},
        ),
        "model.legacy.int_littlepay__unioned_settlements": _model(
            "int_littlepay__unioned_settlements",
            raw="select 1",
            columns={c: {} for c in _SETTLEMENTS_COLUMNS},
        ),
        "model.legacy.int_payments__settlements_deduped": _model(
            "int_payments__settlements_deduped",
            deps=["model.legacy.int_littlepay__unioned_settlements"],
            raw=_SETTLEMENTS_SQL,
            columns={c: {} for c in _SETTLEMENTS_COLUMNS},
        ),
        "model.legacy.stg_gtfs_rt__service_alerts": _model(
            "stg_gtfs_rt__service_alerts",
            deps=["source.legacy.external_gtfs_rt.service_alerts"],
            raw=_SERVICE_ALERTS_SQL,
            columns={"dt": {}, "hour": {}, "base64_url": {}},
        ),
        "model.legacy.stg_gtfs_schedule__validation_notices": _model(
            "stg_gtfs_schedule__validation_notices",
            deps=[
                "source.legacy.external_gtfs_schedule.validation_notices"
            ],
            raw=_VALIDATION_NOTICES_SQL,
            columns={"dt": {}, "base64_url": {}, "ts": {}},
        ),
    }
    return nodes


_CALITP_SOURCES = {
    "source.legacy.external_gtfs_rt.service_alerts": {
        "name": "service_alerts",
        "source_name": "external_gtfs_rt",
        "resource_type": "source",
    },
    "source.legacy.external_gtfs_schedule.validation_notices": {
        "name": "validation_notices",
        "source_name": "external_gtfs_schedule",
        "resource_type": "source",
    },
}


def _calitp_record(project: Path, name: str) -> dict:
    _manifest(project, _calitp_nodes(), _CALITP_SOURCES)
    report = migration_assessment(project)
    return next(m for m in report["models"] if m["name"] == name)


def test_qualify_or_escape_abstains(tmp_path: Path) -> None:
    # The enghouse QUALIFY retains null-key rows
    # through an OR escape the silver contract cannot express.
    # This model was once proposed as a silver fit.
    record = _calitp_record(
        tmp_path, "int_payments__transactions_deduped_enghouse"
    )
    fit = record["fit"]
    assert "generator" not in fit
    reason = fit["no_fit"]
    assert "QUALIFY" in reason
    assert "OR escape" in reason
    assert "OR payment_reference IS NULL" in reason


def test_qualify_or_escape_parenthesised_abstains(
    tmp_path: Path,
) -> None:
    # The same escape with the whole predicate parenthesised.
    record = _assess_one(
        tmp_path,
        "silver_escaped",
        (
            "select * from {{ ref('upstream') }} "
            "qualify (row_number() over (partition by order_id "
            "order by loaded_at desc) = 1 or order_id is null)"
        ),
        ["model.legacy.upstream"],
    )
    assert "generator" not in record["fit"]
    assert "OR escape" in record["fit"]["no_fit"]


def test_struct_extraction_abstains_service_alerts(
    tmp_path: Path,
) -> None:
    # Struct-field extraction drafted as flat
    # identity renames (an earlier over-reach).
    record = _calitp_record(tmp_path, "stg_gtfs_rt__service_alerts")
    fit = record["fit"]
    assert "generator" not in fit
    reason = fit["no_fit"]
    assert "struct-field extraction" in reason
    assert "metadata.extract_config.extracted_at" in reason


def test_struct_extraction_abstains_validation_notices(
    tmp_path: Path,
) -> None:
    record = _calitp_record(
        tmp_path, "stg_gtfs_schedule__validation_notices"
    )
    fit = record["fit"]
    assert "generator" not in fit
    reason = fit["no_fit"]
    assert "struct-field extraction" in reason
    assert "metadata.extract_config.name" in reason


def test_settlements_still_fits_silver(tmp_path: Path) -> None:
    # The control: a plain QUALIFY dedup (no OR escape, no struct
    # extraction, input documenting every output column) still
    # fits silver, with its exact dedup order, and earns no
    # documented-columns caveat.
    record = _calitp_record(
        tmp_path, "int_payments__settlements_deduped"
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    assert fit["drafted_params"]["dedup_key"] == (
        "participant_id, settlement_id"
    )
    assert fit["drafted_params"]["dedup_order"] == (
        "record_updated_timestamp_utc DESC, littlepay_export_ts "
        "DESC, _line_number ASC"
    )
    assert not any(
        line.startswith("caveat:") for line in fit["evidence"]
    )


def test_two_segment_qualified_columns_still_fit(
    tmp_path: Path,
) -> None:
    # A two-segment reference is an ordinary qualified column,
    # not a struct extraction: the abstention must not reach it.
    record = _assess_one(
        tmp_path,
        "stg_orders",
        (
            "select o.id as order_id, o.customer_id from "
            "{{ source('raw', 'orders') }} o"
        ),
        ["source.legacy.raw.orders"],
        sources={
            "source.legacy.raw.orders": {
                "name": "orders",
                "source_name": "raw",
                "resource_type": "source",
                "columns": {"id": {}, "customer_id": {}},
            }
        },
        columns={"order_id": {}, "customer_id": {}},
    )
    assert record["fit"]["generator"] == "staging"


def test_provenance_parse_produced_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # dbt parse does not compile model SQL: no model node carries
    # compiled_code, so the manifest is parse-produced - declared
    # in JSON and text, with no provenance warning.
    _manifest(tmp_path, _calitp_nodes(), _CALITP_SOURCES)
    report = migration_assessment(tmp_path)
    assert report["manifest_provenance"] == "parse-produced"
    assert not any(
        "provenance" in warning for warning in report["warnings"]
    )
    assert main(["convert", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Manifest: parse-produced" in text
    assert "build-produced" not in text


def test_provenance_build_produced_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A build/compile-produced manifest carries compiled SQL on
    # its model nodes: declared build-produced, and the warning
    # (text + JSON) says fit calibration assumes parse-produced.
    nodes = _nodes()
    nodes["model.legacy.stg_orders"]["compiled_code"] = (
        "select id as order_id, customer_id from raw_orders"
    )
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    assert report["manifest_provenance"] == "build-produced"
    assert any(
        "fit calibration assumes a parse-produced manifest" in warning
        for warning in report["warnings"]
    )
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    # Provenance is a header field; the warning is a
    # headed section with the full generated text as its body.
    assert "Manifest: build-produced" in text
    assert "Warnings (1):" in text
    assert "! Build-produced manifest" in text
    assert "fit calibration assumes a parse-produced manifest" in text
    assert main(
        ["convert", "--project-dir", str(tmp_path), "--format", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest_provenance"] == "build-produced"
    assert any(
        "parse-produced" in warning for warning in payload["warnings"]
    )


def test_staging_placeholders_when_source_columns_undocumented(
    tmp_path: Path,
) -> None:
    # A staging mapping drafted against a source the manifest
    # documents no columns for cannot pair any output: every
    # documented output drafts as an unresolved placeholder, the
    # evidence names them and the missing documentation, and no
    # identity-assumption note appears.
    record = _assess_one(
        tmp_path,
        "stg_orders",
        (
            "select id as order_id, customer_id from "
            "{{ source('raw', 'orders') }}"
        ),
        ["source.legacy.raw.orders"],
        sources=_SOURCE,
        columns={"order_id": {}, "customer_id": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    assert fit["drafted_params"]["columns"] == [
        {"source": "__TODO__", "alias": "customer_id"},
        {"source": "__TODO__", "alias": "order_id"},
    ]
    assert any(
        "unresolved placeholders (source: __TODO__): customer_id, order_id"
        in line
        and "documents no source columns" in line
        for line in fit["evidence"]
    )
    assert not any(
        "assuming source column names match" in line
        for line in fit["evidence"]
    )


def test_staging_placeholders_when_source_columns_partial(
    tmp_path: Path,
) -> None:
    # Partial documentation: the documented output keeps its exact
    # match, the undocumented one drafts as a placeholder, and the
    # evidence names it - no identity guess, no assumption caveat.
    sources = {
        "source.legacy.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"customer_id": {}},
        }
    }
    record = _assess_one(
        tmp_path,
        "stg_orders",
        (
            "select id as order_id, customer_id from "
            "{{ source('raw', 'orders') }}"
        ),
        ["source.legacy.raw.orders"],
        sources=sources,
        columns={"order_id": {}, "customer_id": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    assert fit["drafted_params"]["columns"] == [
        {"source": "customer_id", "alias": "customer_id"},
        {"source": "__TODO__", "alias": "order_id"},
    ]
    assert any(
        "unresolved placeholders (source: __TODO__): order_id" in line
        and "no candidate source column remains" in line
        for line in fit["evidence"]
    )
    assert not any(
        "assuming source column names match" in line
        for line in fit["evidence"]
    )


def test_staging_two_renames_draft_two_placeholders(
    tmp_path: Path,
) -> None:
    # The jaffle stg_customers shape: a fully documented source and
    # two renames. The guarded inference pairs only a single
    # unmatched pair, so both outputs draft as placeholders and the
    # evidence names both candidate source columns - the draft
    # invents no mapping.
    sources = {
        "source.legacy.raw.customers": {
            "name": "customers",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}, "name": {}},
        }
    }
    record = _assess_one(
        tmp_path,
        "stg_customers",
        (
            "select id as customer_id, name as customer_name from "
            "{{ source('raw', 'customers') }}"
        ),
        ["source.legacy.raw.customers"],
        sources=sources,
        columns={"customer_id": {}, "customer_name": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    assert fit["drafted_params"]["columns"] == [
        {"source": "__TODO__", "alias": "customer_id"},
        {"source": "__TODO__", "alias": "customer_name"},
    ]
    assert any(
        "unresolved placeholders (source: __TODO__): "
        "customer_id, customer_name" in line
        and "(the candidates): id, name" in line
        for line in fit["evidence"]
    )


def test_bundle_todo_names_placeholder_outputs(tmp_path: Path) -> None:
    # The review bundle's todos name the outputs whose `from` is
    # the placeholder sentinel and point at the original SQL.
    nodes = {
        "model.legacy.stg_customers": _model(
            "stg_customers",
            deps=["source.legacy.raw.customers"],
            raw=(
                "select id as customer_id, name as customer_name "
                "from {{ source('raw', 'customers') }}"
            ),
            columns={"customer_id": {}, "customer_name": {}},
        )
    }
    sources = {
        "source.legacy.raw.customers": {
            "name": "customers",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}, "name": {}},
        }
    }
    _manifest(tmp_path, nodes, sources)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_customers.yml")
    assert any(
        "unresolved placeholders (source: __TODO__) for output(s): "
        "customer_id, customer_name" in todo
        and "original SQL" in todo
        for todo in bundle["review_todos"]
    )


def test_staging_no_caveat_for_inferred_rename(
    tmp_path: Path,
) -> None:
    # The single unmatched pair is the inferred rename, whose
    # drafting note already names the inference to verify - the
    # partial caveat does not pile on.
    sources = {
        "source.legacy.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}, "customer_id": {}},
        }
    }
    record = _assess_one(
        tmp_path,
        "stg_orders",
        (
            "select id as order_id, customer_id from "
            "{{ source('raw', 'orders') }}"
        ),
        ["source.legacy.raw.orders"],
        sources=sources,
        columns={"order_id": {}, "customer_id": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    assert not any(
        line.startswith("caveat:") for line in fit["evidence"]
    )
    # The inference is unchanged by the placeholder mechanism: the
    # single unmatched pair is still drafted as the rename.
    assert fit["drafted_params"]["columns"] == [
        {"source": "customer_id", "alias": "customer_id"},
        {"source": "id", "alias": "order_id"},
    ]


def test_silver_caveat_when_input_columns_undocumented(
    tmp_path: Path,
) -> None:
    record = _assess_one(
        tmp_path,
        "silver_orders",
        (
            "select * from {{ ref('upstream') }} "
            "qualify row_number() over (partition by order_id "
            "order by loaded_at desc) = 1"
        ),
        ["model.legacy.upstream"],
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    assert any(
        "caveat: the manifest documents no columns for the "
        "input model" in line
        for line in fit["evidence"]
    )


def test_silver_caveat_when_input_columns_partial(
    tmp_path: Path,
) -> None:
    # The enghouse shape in miniature: an output column the input
    # does not document (a derived column) is named by the
    # caveat - the generated model projects the input's columns.
    nodes = {
        "model.legacy.upstream": _model(
            "upstream",
            raw="select 1",
            columns={"order_id": {}, "loaded_at": {}},
        ),
        "model.legacy.silver_derived": _model(
            "silver_derived",
            deps=["model.legacy.upstream"],
            raw=(
                "select * from {{ ref('upstream') }} "
                "qualify row_number() over (partition by order_id "
                "order by loaded_at desc) = 1"
            ),
            columns={
                "order_id": {},
                "loaded_at": {},
                "settlement_type": {},
            },
        ),
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "silver_derived"
    )
    fit = record["fit"]
    assert fit["generator"] == "silver"
    caveats = [
        line for line in fit["evidence"] if line.startswith("caveat:")
    ]
    assert len(caveats) == 1
    assert "documents only some of the columns for the input model" in (
        caveats[0]
    )
    assert "settlement_type" in caveats[0]


def test_bronze_caveat_when_source_columns_partial(
    tmp_path: Path,
) -> None:
    sources = {
        "source.legacy.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "resource_type": "source",
            "columns": {"id": {}},
        }
    }
    record = _assess_one(
        tmp_path,
        "raw_dump",
        "select * from {{ source('raw', 'orders') }}",
        ["source.legacy.raw.orders"],
        sources=sources,
        columns={"id": {}, "total": {}},
    )
    fit = record["fit"]
    assert fit["generator"] == "bronze"
    assert any(
        "documents only some of the columns for the source "
        "(1 of 2 drafted columns)" in line
        for line in fit["evidence"]
    )
def test_scaffold_state_deleted_and_missing_on_reassessment(
    tmp_path: Path,
) -> None:
    # A hand-deleted scaffold draft must be
    # visible to the re-assessment. Before, the record was
    # recomputed from the manifest and every candidate reported
    # `untouched`, so a deletion changed nothing observable.
    from loom.reporting import render_convert_text

    report = _scaffold_report(tmp_path)
    # Seven authoring drafts plus the fixture's one fit draft -
    # the unified writer writes both kinds in one pass.
    assert len(write_drafts(tmp_path, report)) == 8
    scaffolds = tmp_path / "local" / "drafts"

    def states() -> dict[str, str]:
        reassessed = migration_assessment(tmp_path)
        return {
            m["name"]: m["scaffold"]["state"]
            for m in reassessed["models"]
            if "scaffold" in m
        }

    # Full draft sets on disk: every scaffold untouched.
    assert set(states().values()) == {"untouched"}
    # Delete one model's starting instance by hand: half its set
    # is gone, and the re-assessment says so - in the record and
    # in the text report.
    (scaffolds / "instances" / "int_customers.yml").unlink()
    after_delete = states()
    assert after_delete["int_customers"] == "deleted"
    assert after_delete["int_orders_enriched"] == "untouched"
    # The grouped scaffold section still names the
    # deleted draft sets in default text (state honesty,
    # restated in the new grammar).
    assert "Deleted draft sets (1): int_customers" in render_convert_text(
        migration_assessment(tmp_path)
    )
    # A re-write completes the set - only the missing half is
    # written - and the state returns to untouched.
    repaired = write_drafts(
        tmp_path, migration_assessment(tmp_path)
    )
    assert repaired == [
        str(scaffolds / "instances" / "int_customers.yml")
    ]
    assert states()["int_customers"] == "untouched"
    # Delete another model's whole set: with draft sets present
    # for its peers, the absence is reported as missing. The
    # scaffold yield is unchanged - the model is still
    # scaffoldable; the state, not the count, carries the news.
    (scaffolds / "fct_revenue.yml").unlink()
    (scaffolds / "instances" / "fct_revenue.yml").unlink()
    assert states()["fct_revenue"] == "missing"
    assert "Missing draft sets (1): fct_revenue" in render_convert_text(
        migration_assessment(tmp_path)
    )
    # The readiness distribution is unchanged - the model is
    # still draftable; the state, not the count, carries the news.
    assert migration_assessment(tmp_path)["summary"]["readiness"] == {
        "ready_to_promote": 0,
        "needs_authoring": 8,
        "not_draftable": 1,
    }


# --- Every declared generic test is copied ------------------------
#
# Only the built-in generic kinds were copied into the scaffold
# declared interface at first, then accepted_values + singular;
# project-defined generics (the Stellar census case: 21 of 25
# flagged tests on liquidity_pools_current) were still counted in
# the interface flags but not copied, silently. Every named
# generic test is now copied verbatim - and marks, never
# drops, a node whose metadata cannot be read.


def _census_nodes() -> dict:
    census = _model(
        "int_census",
        deps=["model.legacy.stg_orders", "model.legacy.int_customers"],
        raw=(
            "select o.order_id, c.customer_id from "
            "{{ ref('stg_orders') }} o join {{ ref('int_customers') }} c "
            "on o.customer_id = c.customer_id"
        ),
        config={"materialized": "table"},
        columns={"order_id": {}, "customer_id": {}, "total": {}},
    )
    census["original_file_path"] = "models/intermediate/int_census.sql"
    customers = _model(
        "int_customers",
        deps=["model.legacy.stg_orders"],
        raw=(
            "select customer_id, count(*) as orders from "
            "{{ ref('stg_orders') }} group by customer_id"
        ),
        config={"materialized": "table"},
    )
    customers["original_file_path"] = "models/intermediate/int_customers.sql"

    def test_node(
        key: str,
        *,
        name: str,
        column_name: str | None = None,
        test_name: str | None = None,
        kwargs: dict | None = None,
        raw_code: str | None = None,
        test_metadata: object =...,
    ) -> dict:
        node: dict = {
            "name": name,
            "resource_type": "test",
            "package_name": "legacy",
            "depends_on": {"nodes": ["model.legacy.int_census"]},
        }
        if column_name is not None:
            node["column_name"] = column_name
        if test_metadata is...:
            if test_name is not None:
                node["test_metadata"] = {
                    "name": test_name,
                    "kwargs": kwargs or {},
                }
            # else: a singular test - no test_metadata at all.
        else:
            node["test_metadata"] = test_metadata
        if raw_code is not None:
            node["raw_code"] = raw_code
        return {f"test.legacy.{key}": node}

    nodes = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
        ),
        "model.legacy.int_customers": customers,
        "model.legacy.int_census": census,
    }
    nodes.update(
        test_node(
            "unique_int_census_order_id",
            name="unique_int_census_order_id",
            column_name="order_id",
            test_name="unique",
            kwargs={
                "column_name": "order_id",
                "model": "ref('int_census')",
            },
        )
    )
    nodes.update(
        test_node(
            "not_null_int_census_customer_id",
            name="not_null_int_census_customer_id",
            column_name="customer_id",
            test_name="not_null",
            kwargs={
                "column_name": "customer_id",
                "model": "ref('int_census')",
            },
        )
    )
    nodes.update(
        test_node(
            "relationships_int_census_customer",
            name="relationships_int_census_customer",
            column_name="customer_id",
            test_name="relationships",
            kwargs={
                "column_name": "customer_id",
                "to": "ref('int_customers')",
                "field": "customer_id",
                "model": "ref('int_census')",
            },
        )
    )
    nodes.update(
        test_node(
            "accepted_values_int_census_status",
            name="accepted_values_int_census_status",
            column_name="status",
            test_name="accepted_values",
            kwargs={
                "column_name": "status",
                "values": ["placed", "shipped"],
                "model": "ref('int_census')",
            },
        )
    )
    # Project-defined generics: a multi-column combination test
    # (no column on the node - the columns ride in the kwargs),
    # an expression test, and an incremental variant.
    nodes.update(
        test_node(
            "unique_combination_of_columns_int_census",
            name="unique_combination_of_columns_int_census_order_id_customer_id",
            test_name="unique_combination_of_columns",
            kwargs={
                "combination_of_columns": ["order_id", "customer_id"],
                "model": "ref('int_census')",
            },
        )
    )
    nodes.update(
        test_node(
            "expression_is_true_int_census_total",
            name="expression_is_true_int_census_total",
            column_name="total",
            test_name="expression_is_true",
            kwargs={
                "expression": "total >= 0",
                "column_name": "total",
                "model": "ref('int_census')",
            },
        )
    )
    nodes.update(
        test_node(
            "incremental_not_null_int_census_loaded_at",
            name="incremental_not_null_int_census_loaded_at",
            column_name="loaded_at",
            test_name="incremental_not_null",
            kwargs={
                "column_name": "loaded_at",
                "model": "ref('int_census')",
            },
        )
    )
    nodes.update(
        test_node(
            "test_census_totals_nonnegative",
            name="test_census_totals_nonnegative",
            raw_code=(
                "select * from {{ ref('int_census') }} where total < 0"
            ),
        )
    )
    return nodes


def _census_sections() -> dict:
    return {
        "unit_tests": {
            "unit_test.legacy.int_census.test_census": {
                "name": "test_census",
                "resource_type": "unit_test",
                "depends_on": {"nodes": ["model.legacy.int_census"]},
                "given": [
                    {
                        "input": "ref('stg_orders')",
                        "rows": [{"order_id": 1, "customer_id": 7}],
                    }
                ],
                "expect": {"rows": [{"order_id": 1, "customer_id": 7}]},
            }
        }
    }


def _attached_test_count(record: dict) -> int:
    for flag in record.get("interface_flags") or []:
        match = re.search(r"(\d+) test\(s\) are attached", flag)
        if match:
            return int(match.group(1))
    raise AssertionError("no attached-tests flag on the record")


def test_every_declared_test_is_copied(tmp_path: Path) -> None:
    _manifest(tmp_path, _census_nodes(), _SOURCE, _census_sections())
    report = migration_assessment(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "int_census"
    )
    # 8 test nodes + 1 unit test are attached to the census model.
    assert _attached_test_count(record) == 9
    interface = record["scaffold"]["declared_interface"]
    generic = interface["generic_tests"]
    # The copy accounts for the whole count: every generic/singular
    # entry plus the unit test, with no `uncopied` remainder.
    assert len(generic) + len(interface["unit_tests"]) == 9
    assert not any(e["test"] == "uncopied" for e in generic)
    by_test: dict[str, list[dict]] = {}
    for entry in generic:
        by_test.setdefault(entry["test"], []).append(entry)
    assert set(by_test) == {
        "unique",
        "not_null",
        "relationships",
        "accepted_values",
        "unique_combination_of_columns",
        "expression_is_true",
        "incremental_not_null",
        "singular",
    }
    # Project-defined generics, verbatim: the combination list
    # rides in the kwargs, and the node carried no single column.
    combination = by_test["unique_combination_of_columns"][0]
    assert combination["kwargs"]["combination_of_columns"] == [
        "order_id",
        "customer_id",
    ]
    assert "column_name" not in combination
    assert by_test["expression_is_true"][0]["kwargs"]["expression"] == (
        "total >= 0"
    )
    assert by_test["expression_is_true"][0]["column_name"] == "total"
    assert by_test["incremental_not_null"][0]["kwargs"] == {
        "column_name": "loaded_at",
        "model": "ref('int_census')",
    }
    # The built-ins still copy exactly as before.
    assert by_test["accepted_values"][0]["kwargs"]["values"] == [
        "placed",
        "shipped",
    ]
    assert "total < 0" in by_test["singular"][0]["sql"]


def test_unreadable_test_metadata_is_marked_not_dropped(
    tmp_path: Path,
) -> None:
    nodes = _census_nodes()
    nodes["test.legacy.mystery_int_census"] = {
        "name": "mystery_int_census",
        "resource_type": "test",
        "package_name": "legacy",
        "test_metadata": "not-a-mapping",
        "depends_on": {"nodes": ["model.legacy.int_census"]},
    }
    _manifest(tmp_path, nodes, _SOURCE, _census_sections())
    report = migration_assessment(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "int_census"
    )
    # The unreadable node is still counted by the flags - and the
    # copy accounts for it with an explicit marker, not silence.
    assert _attached_test_count(record) == 10
    interface = record["scaffold"]["declared_interface"]
    generic = interface["generic_tests"]
    assert len(generic) + len(interface["unit_tests"]) == 10
    markers = [e for e in generic if e["test"] == "uncopied"]
    assert len(markers) == 1
    assert "not copied" in markers[0]["reason"]


# --- Provenance annotation at the point of use --------------------
#
# The manifest's compilation provenance is declared at report
# level and warned on build-produced manifests; a review found the
# declaration was bypassed by a skim of the yield headline or a
# JSON → draft-writing pipeline. The owner's ruling:
# annotate, do not gate. On a build-produced manifest each fit
# carries its provenance basis, the yield headline names it, the
# per-fit text entries are marked, and the written-drafts account
# carries it. Parse-produced output gains none of it.


def _build_produced_nodes() -> dict:
    nodes = _nodes()
    nodes["model.legacy.stg_orders"]["compiled_code"] = (
        "select id as order_id, customer_id from raw_orders"
    )
    return nodes


def test_build_produced_fits_annotated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _build_produced_nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    assert report["manifest_provenance"] == "build-produced"
    fits = [
        m for m in report["models"] if "generator" in (m.get("fit") or {})
    ]
    assert fits
    # (a) JSON: every fit entry carries its provenance basis.
    for model in fits:
        assert model["fit"]["manifest_provenance"] == "build-produced"
    # A no-fit record is not a fit and carries no annotation.
    for model in report["models"]:
        fit = model.get("fit") or {}
        if "generator" not in fit:
            assert "manifest_provenance" not in fit
    # (b) Text: the readiness headline names the provenance, in
    # both commands, and each fit entry is marked.
    assert main(["convert", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    headline = text.splitlines()[1]
    assert headline.startswith("Readiness:")
    assert "(build-produced manifest)" in headline
    for model in fits:
        entry_line = next(
            line
            for line in text.splitlines()
            if model["name"] in line and "- convert:" in line
        )
        assert "[build-produced manifest]" in entry_line
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    register_text = capsys.readouterr().out
    register_headline = register_text.splitlines()[1]
    assert register_headline.startswith("Readiness:")
    assert "(build-produced manifest)" in register_headline


def test_parse_produced_output_unannotated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    assert report["manifest_provenance"] == "parse-produced"
    # (d) No per-fit annotation anywhere on a parse-produced
    # report, and no drafts_provenance key.
    for model in report["models"]:
        assert "manifest_provenance" not in (model.get("fit") or {})
    assert "drafts_provenance" not in report
    assert main(["convert", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "(build-produced manifest)" not in text
    assert "[build-produced manifest]" not in text
    assert text.splitlines()[1].startswith("Readiness:")


def test_write_drafts_account_annotated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # (c) The drafts are written exactly as before (no gate), and
    # the written-drafts account carries the annotation - in JSON
    # and in text. (Two project copies: a second write run on the
    # same project writes nothing, by the never-overwrite rule.)
    json_project = tmp_path / "json_run"
    _manifest(json_project, _build_produced_nodes(), _SOURCE)
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(json_project),
                "--write",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["drafts_written"]
    assert payload["drafts_provenance"] == "build-produced"
    text_project = tmp_path / "text_run"
    _manifest(text_project, _build_produced_nodes(), _SOURCE)
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(text_project),
                "--write",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "Drafts written (3) [build-produced manifest]:" in text


def test_write_drafts_parse_produced_account_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    json_project = tmp_path / "json_run"
    _manifest(json_project, _nodes(), _SOURCE)
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(json_project),
                "--write",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["drafts_written"]
    assert "drafts_provenance" not in payload
    text_project = tmp_path / "text_run"
    _manifest(text_project, _nodes(), _SOURCE)
    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(text_project),
                "--write",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "Drafts written (3):" in text


def test_semantic_layer_names_reachable_saved_queries(tmp_path: Path) -> None:
    sections = _honesty_sections()
    sections["saved_queries"] = {
        "saved_query.legacy.weekly_revenue": {
            "name": "weekly_revenue",
            "resource_type": "saved_query",
            "depends_on": {"nodes": ["metric.legacy.revenue"]},
        }
    }
    _manifest(tmp_path, _honesty_nodes(), _SOURCE, sections)
    report = migration_assessment(tmp_path)
    staging = {m["name"]: m for m in report["models"]}["stg_orders"]
    # The chain model → semantic model → metric → saved query is
    # named end to end, and the saved query joins the
    # seen-but-not-assessed accounting.
    assert staging["semantic_layer"]["metrics"] == ["revenue"]
    assert staging["semantic_layer"]["saved_queries"] == ["weekly_revenue"]
    flags = " ".join(staging["interface_flags"])
    assert "1 saved quer(ies) reachable (weekly_revenue)" in flags
    assert report["summary"]["seen_but_not_assessed"]["saved_query"] == 1


def test_promotion_steps_mention_properties_optout_for_interface_drafts(
    tmp_path: Path,
) -> None:
    # A draft carrying declared tests / semantic models gets
    # the practitioner-YAML guidance in its promotion steps; a
    # draft without them does not.
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    import yaml as _yaml

    def steps_for(model: str) -> str:
        record = _yaml.safe_load(
            (tmp_path / f"local/drafts/{model}.yml").read_text(
                encoding="utf-8"
            )
        )
        return "\n".join(record["promotion"])

    # stg_orders' draft carries declared generic tests.
    assert "skip_properties_document" in steps_for("stg_orders")
    # silver_orders carries no declared interface at all.
    assert "skip_properties_document" not in steps_for("silver_orders")


_EVENTS_SOURCE = {
    "source.legacy.raw.events": {
        "name": "events",
        "source_name": "raw",
        "resource_type": "source",
        "columns": {"id": {}, "created_at": {}, "name": {}},
    }
}

_FORMAT_DATE_MACRO = {
    "name": "format_date",
    "package_name": "widgets",
    "arguments": [{"name": "column_name"}, {"name": "fmt"}],
    "macro_sql": "{% macro format_date(column_name, fmt) %}x{% endmacro %}",
}


def _macro_assess(
    tmp_path: Path, raw: str, macros: dict | None
) -> dict:
    nodes = {
        "model.legacy.stg_events": _model(
            "stg_events",
            deps=["source.legacy.raw.events"],
            raw=raw,
            columns={"id": {}, "created_date": {}, "name": {}},
        )
    }
    _manifest(
        tmp_path,
        nodes,
        _EVENTS_SOURCE,
        {"macros": macros} if macros is not None else None,
    )
    report = migration_assessment(tmp_path)
    return next(m for m in report["models"] if m["name"] == "stg_events")


_MACRO_SQL = (
    "select id, {{ widgets.format_date('created_at', 'iso') }} "
    "as created_date, name from {{ source('raw', 'events') }}"
)


def test_convert_drafts_verified_package_macro(tmp_path: Path) -> None:
    # Tier 1 acceptance: a name-style package-macro call,
    # verified against the manifest's macro entries (existence +
    # arity), drafts as a staging macro column end-to-end.
    record = _macro_assess(
        tmp_path,
        _MACRO_SQL,
        {"macro.widgets.format_date": _FORMAT_DATE_MACRO},
    )
    fit = record["fit"]
    assert fit["generator"] == "staging"
    columns = {
        entry["alias"]: entry for entry in fit["drafted_params"]["columns"]
    }
    assert columns["created_date"] == {
        "source": "created_at",
        "alias": "created_date",
        "macro": "widgets.format_date",
        "macro_args": ["iso"],
    }
    assert any(
        "was verified against the manifest" in line
        for line in fit["evidence"]
    )
    assert not any(
        hole["detail"].startswith("unverified macro")
        for hole in record["readiness"]["holes"]
    )
    # And the written draft instance carries the macro.
    _manifest_report = migration_assessment(tmp_path)
    write_drafts(tmp_path, _manifest_report)
    import yaml as _yaml

    instance = _yaml.safe_load(
        (tmp_path / "local/drafts/instances/stg_events.yml").read_text(
            encoding="utf-8"
        )
    )
    drafted = {
        entry["alias"]: entry
        for entry in instance["instances"][0]["params"]["columns"]
    }
    assert drafted["created_date"]["macro"] == "widgets.format_date"


def test_convert_unverified_macro_is_an_evidence_hole(
    tmp_path: Path,
) -> None:
    # No macros section at all: the call cannot be verified, so
    # the draft carries the plain mapping and readiness carries
    # an evidence hole naming the macro - never a silent pass.
    record = _macro_assess(tmp_path, _MACRO_SQL, None)
    fit = record["fit"]
    assert fit["generator"] == "staging"
    columns = {
        entry["alias"]: entry for entry in fit["drafted_params"]["columns"]
    }
    assert columns["created_date"] == {
        "source": "created_at",
        "alias": "created_date",
    }
    holes = [
        hole["detail"]
        for hole in record["readiness"]["holes"]
        if hole["detail"].startswith("unverified macro")
    ]
    assert len(holes) == 1
    assert "'widgets.format_date'" in holes[0]
    assert "is not defined in the manifest" in holes[0]
    assert record["readiness"]["state"] == "needs_authoring"


def test_convert_macro_arity_mismatch_is_unverified(
    tmp_path: Path,
) -> None:
    macro = dict(_FORMAT_DATE_MACRO)
    macro["arguments"] = [
        {"name": "column_name"},
        {"name": "fmt"},
        {"name": "zone"},
    ]
    macro["macro_sql"] = (
        "{% macro format_date(column_name, fmt, zone) %}x{% endmacro %}"
    )
    record = _macro_assess(
        tmp_path, _MACRO_SQL, {"macro.widgets.format_date": macro}
    )
    holes = [
        hole["detail"]
        for hole in record["readiness"]["holes"]
        if hole["detail"].startswith("unverified macro")
    ]
    assert len(holes) == 1
    assert "but the call supplies 2" in holes[0]


_DATE_TRUNC_MACRO = {
    "macro.dbt.date_trunc": {
        "name": "date_trunc",
        "package_name": "dbt",
        "arguments": [{"name": "datepart"}, {"name": "date"}],
        "macro_sql": "{% macro date_trunc(datepart, date) %}x{% endmacro %}",
    }
}


def test_convert_tier2_literal_first_macro_drafts_with_column_param(
    tmp_path: Path,
) -> None:
    # Tier 2 (supersedes the Tier 1-era does-not-fit test for
    # this shape): the literal-first call drafts under
    # macro_column_param - 'day' is identifier-shaped but is not
    # a documented source column, so 'created_at' at position 2
    # is the unique resolution; verification is against the
    # manifest as for Tier 1.
    record = _macro_assess(
        tmp_path,
        "select id, {{ dbt.date_trunc('day', created_at) }} "
        "as created_date, name from {{ source('raw', 'events') }}",
        _DATE_TRUNC_MACRO,
    )
    fit = record["fit"]
    assert "no_fit" not in fit
    columns = {
        entry["alias"]: entry for entry in fit["drafted_params"]["columns"]
    }
    entry = columns["created_date"]
    assert entry["source"] == "created_at"
    assert entry["macro"] == "dbt.date_trunc"
    assert entry["macro_args"] == ["day"]
    assert entry["macro_column_param"] == 2
    assert any(
        "argument position 2" in line and "'date' parameter" in line
        for line in fit["evidence"]
    )


def test_convert_tier2_unverified_macro_leaves_plain_mapping(
    tmp_path: Path,
) -> None:
    # The macro is absent from the manifest: the position
    # resolves, verification fails, and the draft carries the
    # plain mapping with the unverified-macro evidence hole -
    # the Tier 1 unverifiable treatment, never a silent pass.
    record = _macro_assess(
        tmp_path,
        "select id, {{ dbt.date_trunc('day', created_at) }} "
        "as created_date, name from {{ source('raw', 'events') }}",
        {},
    )
    fit = record["fit"]
    assert "no_fit" not in fit
    columns = {
        entry["alias"]: entry for entry in fit["drafted_params"]["columns"]
    }
    entry = columns["created_date"]
    assert entry["source"] == "created_at"
    assert "macro" not in entry
    assert any(
        "unverified macro 'dbt.date_trunc'" in line for line in fit["evidence"]
    )


def test_convert_tier2_ambiguous_column_leaves_plain_mapping(
    tmp_path: Path,
) -> None:
    # Both quoted arguments name documented source columns, so
    # the call's source column is ambiguous: no macro is
    # drafted and the evidence says why.
    record = _macro_assess(
        tmp_path,
        "select id, {{ dbt.date_trunc('name', created_at) }} "
        "as created_date, name from {{ source('raw', 'events') }}",
        _DATE_TRUNC_MACRO,
    )
    fit = record["fit"]
    assert "no_fit" not in fit
    columns = {
        entry["alias"]: entry for entry in fit["drafted_params"]["columns"]
    }
    assert "macro" not in columns["created_date"]
    assert any(
        "could not be drafted under macro_column_param" in line
        and "ambiguous" in line
        for line in fit["evidence"]
    )


def test_convert_tier3_embedded_macro_call_names_the_macro_path(
    tmp_path: Path,
) -> None:
    # A macro call embedded in a larger expression stays
    # scaffold-class (Tier 3); the no-fit reason names the
    # macro path for the manual rewrite.
    record = _macro_assess(
        tmp_path,
        "select id, coalesce({{ dbt.date_trunc('day', created_at) }}, "
        "created_at) as created_date, name "
        "from {{ source('raw', 'events') }}",
        _DATE_TRUNC_MACRO,
    )
    fit = record["fit"]
    assert "no_fit" in fit
    assert "dbt.date_trunc" in fit["no_fit"]
    assert "embedded" in fit["no_fit"]


def test_convert_bare_project_macro_verified(tmp_path: Path) -> None:
    macro = dict(_FORMAT_DATE_MACRO)
    macro["package_name"] = "legacy"
    record = _macro_assess(
        tmp_path,
        "select id, {{ format_date('created_at', 'iso') }} "
        "as created_date, name from {{ source('raw', 'events') }}",
        {"macro.legacy.format_date": macro},
    )
    columns = {
        entry["alias"]: entry
        for entry in record["fit"]["drafted_params"]["columns"]
    }
    assert columns["created_date"]["macro"] == "format_date"
def test_fit_copies_generic_tests_into_the_draft(tmp_path: Path) -> None:
    # A fitted model's manifest generic tests are
    # copied into the draft's generic_tests - the completeness
    # mirror of the declared-interface copy. dbt-injected kwargs
    # (model, column_name) are not copied; a declared warn
    # severity is; a singular test is not a declaration and is
    # never copied; the copy keys on the attached model, so the
    # relationships TARGET gains nothing.
    nodes: dict = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw=(
                "select id as order_id, customer_id from "
                "{{ source('raw', 'orders') }}"
            ),
            columns={"order_id": {}, "customer_id": {}},
        ),
        "model.legacy.customers": _model(
            "customers",
            deps=["source.legacy.raw.orders"],
            raw="select id as customer_id from {{ source('raw', 'orders') }}",
            columns={"customer_id": {}},
        ),
        "test.legacy.not_null_stg_orders_order_id": {
            "name": "not_null_stg_orders_order_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "order_id",
            "attached_node": "model.legacy.stg_orders",
            "test_metadata": {
                "name": "not_null",
                "kwargs": {
                    "column_name": "order_id",
                    "model": "{{ get_where_subquery(ref('stg_orders')) }}",
                },
            },
            "config": {"severity": "ERROR"},
            "depends_on": {"nodes": ["model.legacy.stg_orders"]},
        },
        "test.legacy.relationships_stg_orders_customer_id": {
            "name": "relationships_stg_orders_customer_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "customer_id",
            "attached_node": "model.legacy.stg_orders",
            "test_metadata": {
                "name": "relationships",
                "kwargs": {
                    "column_name": "customer_id",
                    "to": "ref('customers')",
                    "field": "customer_id",
                    "model": "{{ get_where_subquery(ref('stg_orders')) }}",
                },
            },
            "config": {"severity": "WARN"},
            "depends_on": {
                "nodes": ["model.legacy.stg_orders", "model.legacy.customers"]
            },
        },
        "test.legacy.stg_orders_still_has_customers": {
            "name": "stg_orders_still_has_customers",
            "resource_type": "test",
            "package_name": "legacy",
            "raw_code": (
                "select * from {{ ref('stg_orders') }} "
                "where customer_id is null"
            ),
            "depends_on": {"nodes": ["model.legacy.stg_orders"]},
        },
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(m for m in report["models"] if m["name"] == "stg_orders")
    assert record["fit"]["generator"] == "staging"
    assert record["fit"]["drafted_params"]["generic_tests"] == [
        {"test": "not_null", "column_name": "order_id"},
        {
            "test": "relationships",
            "column_name": "customer_id",
            "kwargs": {"to": "ref('customers')", "field": "customer_id"},
            "config": {"severity": "warn"},
        },
    ]
    assert any(
        "copied into the draft's generic_tests" in line
        for line in record["fit"]["evidence"]
    )
    # Carried content is not a readiness hole.
    assert record["readiness"]["state"] in ("ready", "needs_authoring")
    assert not any(
        "generic" in hole for hole in record["readiness"]["holes"]
    )
    target = next(m for m in report["models"] if m["name"] == "customers")
    assert "generic_tests" not in (target["fit"].get("drafted_params") or {})


# --- Pi remediation Lane M: P1/P6 records, P4 flag, P9 carriage, P10 ---


def _seed_node(name: str) -> dict:
    return {
        "name": name,
        "resource_type": "seed",
        "package_name": "legacy",
        "original_file_path": f"seeds/{name}.csv",
        "depends_on": {"nodes": []},
        "raw_code": "",
    }


def _seed_fed_project(tmp_path: Path) -> dict:
    node = _model(
        "stg_customers",
        deps=["seed.legacy.raw_customers"],
        raw="select id, name from {{ ref('raw_customers') }}",
        columns={"id": {}, "name": {}},
    )
    node["original_file_path"] = "models/staging/stg_customers.sql"
    nodes = {
        "model.legacy.stg_customers": node,
        "seed.legacy.raw_customers": _seed_node("raw_customers"),
    }
    _manifest(tmp_path, nodes)
    return migration_assessment(tmp_path)


def test_seed_fed_staging_names_seeds_and_binding_decision(
    tmp_path: Path,
) -> None:
    report = _seed_fed_project(tmp_path)
    record = next(m for m in report["models"] if m["name"] == "stg_customers")
    # P1(a): the no-fit reason names the seed and the decision,
    # and no longer claims the model has no dependencies.
    reason = record["fit"]["no_fit"]
    assert "raw_customers" in reason
    assert "source_ref" in reason and "inline_source" in reason
    # The authoring record accounts for the seed input.
    scaffold = record["scaffold"]
    assert scaffold["inputs"]["seeds"] == ["raw_customers"]
    # P1(c) + P6: the staging authoring hole names the binding
    # decision and signposts the macro slot.
    note = scaffold["authoring_hole"]["note"]
    assert "seed ref(s) (raw_customers)" in note
    assert "declare the seed(s) as dbt source(s)" in note
    assert "macro slot" in note and "{source, alias, macro}" in note
    # P1(b): the draft's todos name the binding decision, and
    # the pre-wired-inputs boilerplate is gone (nothing wired).
    write_drafts(tmp_path, report)
    draft = yaml.safe_load(
        (tmp_path / "local" / "drafts" / "stg_customers.yml").read_text(
            encoding="utf-8"
        )
    )
    todos = "\n".join(draft["review_todos"])
    assert "Decide the source binding for the seed input(s)" in todos
    assert "pre-wired inputs" not in todos


def test_pre_wired_todo_only_when_inputs_wired() -> None:
    model: dict = {"interface_flags": []}
    wired = _scaffold_review_todos(
        model,
        {
            "target_generator": "normalized_table",
            "drafted_params": {"input_models": ["stg_orders"]},
            "inputs": {"models": ["stg_orders"]},
            "declared_interface": {},
        },
    )
    assert any("pre-wired inputs" in todo for todo in wired)
    unwired = _scaffold_review_todos(
        model,
        {
            "target_generator": "staging",
            "drafted_params": {},
            "inputs": {"seeds": ["raw_customers"]},
            "declared_interface": {},
        },
    )
    assert not any("pre-wired inputs" in todo for todo in unwired)
    assert any("source binding" in todo for todo in unwired)


def test_materialization_scope_walks_dbt_project_tree() -> None:
    config = {
        "model-paths": ["models"],
        "models": {
            "legacy": {
                "+materialized": "table",
                "staging": {"+materialized": "view"},
            }
        },
    }
    scope = _materialization_scope(
        config, "legacy", "models/staging/stg_orders.sql"
    )
    assert scope == {
        "project_value": "table",
        "scoped_value": "view",
        "scoped_depth": 1,
    }
    scope = _materialization_scope(
        config, "legacy", "models/marts/orders.sql"
    )
    assert scope == {
        "project_value": "table",
        "scoped_value": None,
        "scoped_depth": 0,
    }
    assert _materialization_scope({}, "legacy", "models/x.sql") is None


def test_interface_flag_materialization_disclosure() -> None:
    def flags(materialized: str, scope: dict | None) -> list[str]:
        node = {
            "config": {"materialized": materialized},
            "original_file_path": "models/staging/stg_orders.sql",
        }
        return _interface_flags(
            node, {}, 0, materialization_scope=scope
        )

    scoped = {"project_value": "table", "scoped_value": "view", "scoped_depth": 1}
    fired = flags("view", scoped)
    assert any("path-scoped dbt_project config" in f for f in fired)
    # The model's own declaration (resolved differs from every
    # project-config value) is disclosed as a declaration.
    fired = flags("table", {"project_value": "view", "scoped_value": None, "scoped_depth": 0})
    assert any("by the model's own declaration" in f for f in fired)
    # Root-scope config only: the generated path stays under it.
    fired = flags("table", {"project_value": "table", "scoped_value": None, "scoped_depth": 0})
    assert not any("materialization" in f for f in fired)
    # No scope facts: no disclosure either way.
    assert not any(
        "materialization" in f for f in flags("table", None)
    )


def test_assessment_flags_path_scoped_materialization(
    tmp_path: Path,
) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "name: legacy\n"
        "models:\n"
        "  legacy:\n"
        "    +materialized: table\n"
        "    staging:\n"
        "      +materialized: view\n",
        encoding="utf-8",
    )
    node = _model(
        "stg_orders",
        deps=["source.legacy.raw.orders"],
        raw="select id as order_id from {{ source('raw', 'orders') }}",
        config={"materialized": "view"},
        columns={"order_id": {}},
    )
    node["original_file_path"] = "models/staging/stg_orders.sql"
    _manifest(tmp_path, {"model.legacy.stg_orders": node}, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(m for m in report["models"] if m["name"] == "stg_orders")
    assert any(
        "path-scoped dbt_project config" in flag
        for flag in record["interface_flags"]
    )


def test_authoring_draft_carries_generic_tests(tmp_path: Path) -> None:
    nodes: dict = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw="select id as order_id from {{ source('raw', 'orders') }}",
            columns={"order_id": {}},
        ),
        "model.legacy.int_orders_enriched": _model(
            "int_orders_enriched",
            deps=["model.legacy.stg_orders"],
            raw=(
                "select o.order_id, o.order_id * 2 as doubled "
                "from {{ ref('stg_orders') }} o"
            ),
            columns={"order_id": {}, "doubled": {}},
        ),
        "test.legacy.unique_int_orders_enriched_order_id": {
            "name": "unique_int_orders_enriched_order_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "order_id",
            "attached_node": "model.legacy.int_orders_enriched",
            "test_metadata": {
                "name": "unique",
                "kwargs": {
                    "column_name": "order_id",
                    "model": "{{ get_where_subquery(ref('int_orders_enriched')) }}",
                },
            },
            "config": {"severity": "ERROR"},
            "depends_on": {"nodes": ["model.legacy.int_orders_enriched"]},
        },
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "int_orders_enriched"
    )
    scaffold = record["scaffold"]
    assert scaffold["target_generator"] == "normalized_table"
    # P9/D5: the authoring draft's instance params carry the
    # model's generic tests, in carriage form.
    assert scaffold["drafted_params"]["generic_tests"] == [
        {"test": "unique", "column_name": "order_id"}
    ]
    assert any(
        "carriage" in line for line in scaffold["evidence"]
    )
    # And the written draft set: instance carries them, and the
    # record's promotion note states the either/or.
    write_drafts(tmp_path, report)
    instance = yaml.safe_load(
        (
            tmp_path
            / "local"
            / "drafts"
            / "instances"
            / "int_orders_enriched.yml"
        ).read_text(encoding="utf-8")
    )
    params = instance["instances"][0]["params"]
    assert params["generic_tests"] == [
        {"test": "unique", "column_name": "order_id"}
    ]
    draft = yaml.safe_load(
        (
            tmp_path / "local" / "drafts" / "int_orders_enriched.yml"
        ).read_text(encoding="utf-8")
    )
    promotion = "\n".join(draft["promotion"])
    assert "choose exactly one" in promotion
    assert "refuses `generic_tests` together with" in promotion


def test_promotion_names_consolidation_when_siblings_drafted(
    tmp_path: Path,
) -> None:
    nodes: dict = {
        "model.legacy.stg_orders": _model(
            "stg_orders",
            deps=["source.legacy.raw.orders"],
            raw="select id as order_id from {{ source('raw', 'orders') }}",
            columns={"order_id": {}},
        ),
        "model.legacy.stg_customers": _model(
            "stg_customers",
            deps=["source.legacy.raw.orders"],
            raw="select id as customer_id from {{ source('raw', 'orders') }}",
            columns={"customer_id": {}},
        ),
    }
    _manifest(tmp_path, nodes, _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    for name in ("stg_orders", "stg_customers"):
        draft = yaml.safe_load(
            (tmp_path / "local" / "drafts" / f"{name}.yml").read_text(
                encoding="utf-8"
            )
        )
        promotion = "\n".join(draft["promotion"])
        # P10/D6: two instances for one pattern in one run -
        # consolidation is the named default, as-is the exception.
        assert "single instance set for pattern 'staging_pattern'" in promotion
        assert "is the exception" in promotion


# --- Pi remediation Lane M2: P3 readiness validation (ruling D3) ---


def test_readiness_rejects_draft_with_invalid_params(
    tmp_path: Path,
) -> None:
    # The pi-session shape: the practitioner completes every
    # hole in the draft instance, but the completed content
    # contains an unknown parameter and an unknown column key.
    # Pre-D3 this reported ready_to_promote; readiness now
    # validates the effective (on-disk) instance, so it reports
    # not-ready with the validation reasons as holes.
    _manifest(tmp_path, _nodes(), _SOURCE)
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    instance_path = _drafts_dir(tmp_path) / "instances" / "stg_orders.yml"
    document = _load_yaml(instance_path)
    params = document["instances"][0]["params"]
    for entry in params["columns"]:
        entry["source"] = entry["alias"]
    # An invented column key. (The session's own invention was
    # `expression`, which ADR 0037 has since made a real key, so
    # the fixture invents `formula` instead.)
    params["columns"][0]["formula"] = "amount / 100"
    params["inputs"] = {}
    params["description"] = "Orders as landed."
    params["owner"] = "analytics"
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_orders"
    )
    readiness = record["readiness"]
    assert readiness["state"] == "needs_authoring"
    details = [hole["detail"] for hole in readiness["holes"]]
    assert any("unknown parameter 'inputs'" in d for d in details)
    # `loom validate` itself reports the unknown parameter
    # first and the column key only once that is fixed (the
    # session's validation cycles) - readiness tracks the same
    # cascade: remove `inputs` and the 'formula' key stands
    # as the named hole.
    del params["inputs"]
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_orders"
    )
    details = [hole["detail"] for hole in record["readiness"]["holes"]]
    assert record["readiness"]["state"] == "needs_authoring"
    assert any("unknown key(s): formula" in d for d in details)


def test_readiness_rejects_completed_draft_without_source_binding(
    tmp_path: Path,
) -> None:
    # A seed-fed authoring draft completed in every authored
    # respect except the source binding: the staging
    # generator's exactly-one-of obligation is enforced only
    # by validation, and readiness now runs it - the draft is
    # not ready, and the hole names the obligation.
    report = _seed_fed_project(tmp_path)
    write_drafts(tmp_path, report)
    instance_path = (
        _drafts_dir(tmp_path) / "instances" / "stg_customers.yml"
    )
    document = _load_yaml(instance_path)
    params = document["instances"][0]["params"]
    params["description"] = "Customers as landed."
    params["owner"] = "analytics"
    params["columns"] = [
        {"source": "id", "alias": "id"},
        {"source": "name", "alias": "name"},
    ]
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_customers"
    )
    readiness = record["readiness"]
    assert readiness["state"] == "needs_authoring"
    assert any(
        "exactly one of source_ref or inline_source is required"
        in hole["detail"]
        for hole in readiness["holes"]
    )
    # Bind the source and the same draft becomes ready.
    params["inline_source"] = {
        "name": "raw_customers",
        "schema": "main",
        "table": "raw_customers",
    }
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_customers"
    )
    assert record["readiness"]["state"] == "ready_to_promote"


def test_draft_validation_problems_unit() -> None:
    from loom.migration_assessment import _draft_validation_problems

    problems = _draft_validation_problems(
        name="stg_orders",
        generator="staging",
        params={
            "name": "stg_orders",
            "inputs": {},
            "source_ref": {"name": "raw", "table": "orders"},
            "columns": [{"source": "id", "alias": "id"}],
            "description": "Orders.",
            "owner": "analytics",
        },
        pattern_name="staging_pattern",
        definitions={},
    )
    assert any("params must not define 'name'" in p for p in problems)
    assert any("unknown parameter 'inputs'" in p for p in problems)


# --- Pi remediation Lane M3: draft enrichment ---


def test_construct_inventory_names_every_construct_completely(
    tmp_path: Path,
) -> None:
    # (a): three expression items (one longer than the prose
    # truncation bound) and one macro invocation - the record's
    # structured inventory names every one completely, with
    # locations; the prose blocker keeps its summary form.
    long_expr = (
        "case when status_code in ('A', 'B', 'C') and region_code "
        "= 'APAC' then 'priority-queue-one' else 'standard-queue' "
        "end as queue_name"
    )
    sql = (
        "with calc as (select id, amount / 100 as amount, "
        f"{long_expr} from {{{{ source('raw', 'orders') }}}}) "
        "select id, amount, queue_name, "
        "{{ cents_to_dollars('amount') }} as amount_usd, "
        "amount * 2 as doubled from calc"
    )
    node = _model(
        "stg_orders",
        deps=["source.legacy.raw.orders"],
        raw=sql,
        columns={
            "id": {"name": "id", "data_type": "integer"},
            "amount": {"name": "amount", "data_type": "numeric"},
        },
    )
    _manifest(tmp_path, {"model.legacy.stg_orders": node}, _SOURCE)
    report = migration_assessment(tmp_path)
    record = next(m for m in report["models"] if m["name"] == "stg_orders")
    constructs = record["constructs"]
    expressions = [c for c in constructs if c["kind"] == "expression"]
    assert {c["text"] for c in expressions} == {
        "amount / 100 as amount",
        long_expr,
        "amount * 2 as doubled",
    }
    assert all(len(c["text"]) == len(c["text"].strip()) for c in constructs)
    by_text = {c["text"]: c for c in expressions}
    assert by_text["amount / 100 as amount"]["location"] == "cte 'calc'"
    assert by_text["amount * 2 as doubled"]["location"] == "final projection"
    assert by_text["amount / 100 as amount"]["expression"] == "amount / 100"
    assert by_text["amount / 100 as amount"]["alias"] == "amount"
    macros = [c for c in constructs if c["kind"] == "macro"]
    assert len(macros) == 1
    assert macros[0]["reference"] == "cents_to_dollars"
    assert macros[0]["alias"] == "amount_usd"
    assert macros[0]["column"] == "amount"
    assert macros[0]["location"] == "final projection"
    # The bundle carries the same inventory.
    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_orders.yml")
    assert bundle["constructs"] == constructs


def _enrichment_seed_project(tmp_path: Path) -> dict:
    seed = _seed_node("raw_things")
    chain = _model(
        "stg_chain",
        deps=["seed.legacy.raw_things"],
        raw=(
            "with renamed as (select id, amount / 100 as amount "
            "from {{ ref('raw_things') }}) select * from renamed"
        ),
        columns={
            "id": {"name": "id", "data_type": "integer"},
            "amount": {"name": "amount", "data_type": "numeric"},
        },
    )
    bound = _model(
        "stg_bound",
        deps=["seed.legacy.raw_things"],
        raw=(
            "with calc as (select id, price * 2 as doubled "
            "from {{ ref('raw_things') }}) select id from calc"
        ),
        columns={"id": {"name": "id", "data_type": "integer"}},
    )
    _manifest(
        tmp_path,
        {
            "seed.legacy.raw_things": seed,
            "model.legacy.stg_chain": chain,
            "model.legacy.stg_bound": bound,
        },
        None,
    )
    return migration_assessment(tmp_path)


def test_preplacement_chain_resolved_only(tmp_path: Path) -> None:
    # (c) + R2: the chain-resolved expression (final
    # `select *` over the computing CTE) is pre-placed as a
    # real {expression, alias} param, stated as found not
    # fitted, and the model still reports needs_authoring;
    # the chain-bound expression (its CTE's output does not
    # reach the final projection) is named in the record only.
    report = _enrichment_seed_project(tmp_path)
    write_drafts(tmp_path, report)
    chain_doc = _load_yaml(
        _drafts_dir(tmp_path) / "instances" / "stg_chain.yml"
    )
    params = chain_doc["instances"][0]["params"]
    # F1 accounting: the pre-placed expression item is only
    # part of the projection (`id` is projected too), so the
    # draft accounts for `id` as an unresolved placeholder in
    # projection order rather than letting the partial columns
    # read as the whole mapping.
    assert params["columns"] == [
        {"source": "__TODO__", "alias": "id"},
        {"expression": "amount / 100", "alias": "amount"},
    ]
    bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_chain.yml")
    assert bundle["readiness"]["state"] == "needs_authoring"
    assert any(
        "pre-placed" in e and "not fitted" in e
        for e in bundle["evidence"]
    )
    assert any(
        "output(s) the draft does not map: id" in e
        for e in bundle["evidence"]
    )
    assert any(
        "'id'" in hole["detail"] and "placeholder" in hole["detail"]
        for hole in bundle["readiness"]["holes"]
    )
    bound_doc = _load_yaml(
        _drafts_dir(tmp_path) / "instances" / "stg_bound.yml"
    )
    assert "columns" not in bound_doc["instances"][0]["params"]
    bound_bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_bound.yml")
    assert any(
        c["text"] == "price * 2 as doubled"
        and c["location"] == "cte 'calc'"
        for c in bound_bundle["constructs"]
    )


# --- F1: authoring-draft completeness accounting ---


def test_projection_accounting_classifies_every_output() -> None:
    # The account classifies every item of the effective
    # final projection, in projection order; the pre-placement
    # enumeration is exactly its expression subsequence.
    from loom.migration_assessment import (
        _chain_projection_accounting,
        _chain_projection_expressions,
    )

    seedfed = (
        "select id as payment_id, customer_id, "
        "amount / 100 as amount from {{ ref('raw_payments') }}"
    )
    account = _chain_projection_accounting(seedfed)
    assert account == {
        "items": [
            {"kind": "unmapped", "alias": "payment_id"},
            {"kind": "unmapped", "alias": "customer_id"},
            {
                "kind": "expression",
                "expression": "amount / 100",
                "alias": "amount",
            },
        ],
        "complete": True,
    }
    assert _chain_projection_expressions(seedfed) == [
        {"expression": "amount / 100", "alias": "amount"}
    ]
    # Chain-resolved through a bare star over the computing
    # CTE, like the fit enumeration.
    chained = _chain_projection_accounting(
        "with renamed as (select id, amount / 100 as amount "
        "from {{ ref('raw_things') }}) select * from renamed"
    )
    assert chained == {
        "items": [
            {"kind": "unmapped", "alias": "id"},
            {
                "kind": "expression",
                "expression": "amount / 100",
                "alias": "amount",
            },
        ],
        "complete": True,
    }
    # A plain-only projection: every output named, none an
    # expression item.
    plain = _chain_projection_accounting(
        "select id, name from {{ ref('raw_customers') }}"
    )
    assert plain == {
        "items": [
            {"kind": "unmapped", "alias": "id"},
            {"kind": "unmapped", "alias": "name"},
        ],
        "complete": True,
    }
    # An aliased macro invocation and an aliased window item
    # name their outputs but are not pre-placed.
    templated = _chain_projection_accounting(
        "select {{ cents('amount') }} as amount, "
        "row_number() over (order by id) as rn, id "
        "from {{ ref('raw_things') }}"
    )
    assert templated == {
        "items": [
            {"kind": "unmapped", "alias": "amount"},
            {"kind": "unmapped", "alias": "rn"},
            {"kind": "unmapped", "alias": "id"},
        ],
        "complete": True,
    }
    # Residue: a star mixed with explicit items, and an
    # unaliased expression, name no output - the named
    # outputs are still accounted, flagged incomplete.
    starred = _chain_projection_accounting(
        "select *, amount / 100 as amount from {{ ref('raw_things') }}"
    )
    assert starred == {
        "items": [
            {
                "kind": "expression",
                "expression": "amount / 100",
                "alias": "amount",
            }
        ],
        "complete": False,
    }
    unaliased = _chain_projection_accounting(
        "select amount / 100, id from {{ ref('raw_things') }}"
    )
    assert unaliased == {
        "items": [{"kind": "unmapped", "alias": "id"}],
        "complete": False,
    }
    # No effective list anywhere on the chain: no account.
    assert (
        _chain_projection_accounting(
            "select * from {{ ref('raw_things') }}"
        )
        is None
    )


def _f1_payments_project(tmp_path: Path) -> dict:
    node = _model(
        "stg_payments",
        deps=["seed.legacy.raw_payments"],
        raw=(
            "select id as payment_id, customer_id, "
            "amount / 100 as amount from {{ ref('raw_payments') }}"
        ),
        columns={
            "payment_id": {"name": "payment_id", "data_type": "integer"},
            "customer_id": {"name": "customer_id", "data_type": "integer"},
            "amount": {"name": "amount", "data_type": "numeric"},
        },
    )
    node["original_file_path"] = "models/staging/stg_payments.sql"
    nodes = {
        "model.legacy.stg_payments": node,
        "seed.legacy.raw_payments": _seed_node("raw_payments"),
        "test.legacy.unique_stg_payments_payment_id": {
            "name": "unique_stg_payments_payment_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "payment_id",
            "attached_node": "model.legacy.stg_payments",
            "test_metadata": {
                "name": "unique",
                "kwargs": {
                    "column_name": "payment_id",
                    "model": (
                        "{{ get_where_subquery(ref('stg_payments')) }}"
                    ),
                },
            },
            "config": {"severity": "ERROR"},
            "depends_on": {"nodes": ["model.legacy.stg_payments"]},
        },
        "test.legacy.not_null_stg_payments_payment_id": {
            "name": "not_null_stg_payments_payment_id",
            "resource_type": "test",
            "package_name": "legacy",
            "column_name": "payment_id",
            "attached_node": "model.legacy.stg_payments",
            "test_metadata": {
                "name": "not_null",
                "kwargs": {
                    "column_name": "payment_id",
                    "model": (
                        "{{ get_where_subquery(ref('stg_payments')) }}"
                    ),
                },
            },
            "config": {"severity": "ERROR"},
            "depends_on": {"nodes": ["model.legacy.stg_payments"]},
        },
    }
    _manifest(tmp_path, nodes)
    return migration_assessment(tmp_path)


def test_authoring_draft_accounts_for_every_projected_output(
    tmp_path: Path,
) -> None:
    # F1 / FIX-CHECK 1 at the record level: the authoring
    # draft accounts for every projected output - the two
    # unmapped outputs stand as named placeholder holes, so
    # completing only the other named holes (description,
    # owner, source binding) cannot certify the draft; the
    # carried tests on the unmapped payment_id are the
    # symptom, carried verbatim as before.
    report = _f1_payments_project(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "stg_payments"
    )
    scaffold = record["scaffold"]
    assert scaffold["drafted_params"]["columns"] == [
        {"source": "__TODO__", "alias": "payment_id"},
        {"source": "__TODO__", "alias": "customer_id"},
        {"expression": "amount / 100", "alias": "amount"},
    ]
    assert scaffold["drafted_params"]["generic_tests"] == [
        {"test": "unique", "column_name": "payment_id"},
        {"test": "not_null", "column_name": "payment_id"},
    ]
    assert any(
        "output(s) the draft does not map: payment_id, customer_id"
        in line
        for line in scaffold["evidence"]
    )
    assert record["readiness"]["state"] == "needs_authoring"
    hole_details = [
        hole["detail"] for hole in record["readiness"]["holes"]
    ]
    assert any(
        "'payment_id'" in detail and "placeholder" in detail
        for detail in hole_details
    )
    assert any(
        "'customer_id'" in detail and "placeholder" in detail
        for detail in hole_details
    )

    write_drafts(tmp_path, report)
    instance_path = (
        _drafts_dir(tmp_path) / "instances" / "stg_payments.yml"
    )
    document = _load_yaml(instance_path)
    params = document["instances"][0]["params"]
    # Complete ONLY the holes the pre-fix record named:
    # description, owner, and the source binding. The
    # columns placeholders stand.
    params["description"] = "Payments from the raw seed."
    params["owner"] = "analytics"
    params["inline_source"] = {
        "name": "seedfed",
        "schema": "raw",
        "table": "raw_payments",
    }
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_payments"
    )
    assert record["readiness"]["state"] == "needs_authoring"
    hole_details = [
        hole["detail"] for hole in record["readiness"]["holes"]
    ]
    assert any("'payment_id'" in detail for detail in hole_details)
    assert any("'customer_id'" in detail for detail in hole_details)

    # Map the two outputs properly and the loop closes.
    params["columns"] = [
        {"source": "id", "alias": "payment_id"},
        {"source": "customer_id", "alias": "customer_id"},
        {"expression": "amount / 100", "alias": "amount"},
    ]
    instance_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    reassessed = migration_assessment(tmp_path)
    record = next(
        m for m in reassessed["models"] if m["name"] == "stg_payments"
    )
    assert record["readiness"]["state"] == "ready_to_promote"
    assert record["readiness"]["holes"] == []


def test_authoring_accounting_caveat_for_star_mixed_projection(
    tmp_path: Path,
) -> None:
    # The enumeration bound on the authoring path: a star
    # mixed with explicit items cannot be fully enumerated,
    # so the draft places what it can name and the residue
    # stands as a caveat evidence hole - never silence.
    node = _model(
        "stg_wide",
        deps=["seed.legacy.raw_things"],
        raw=(
            "select *, amount / 100 as amount "
            "from {{ ref('raw_things') }}"
        ),
        columns={"amount": {"name": "amount", "data_type": "numeric"}},
    )
    node["original_file_path"] = "models/staging/stg_wide.sql"
    _manifest(
        tmp_path,
        {
            "model.legacy.stg_wide": node,
            "seed.legacy.raw_things": _seed_node("raw_things"),
        },
    )
    report = migration_assessment(tmp_path)
    record = next(m for m in report["models"] if m["name"] == "stg_wide")
    scaffold = record["scaffold"]
    assert scaffold["drafted_params"]["columns"] == [
        {"expression": "amount / 100", "alias": "amount"}
    ]
    assert any(
        line.startswith("caveat: the model's final projection")
        and "could not be enumerated in full" in line
        for line in scaffold["evidence"]
    )
    assert record["readiness"]["state"] == "needs_authoring"
    assert any(
        hole["detail"].startswith(
            "caveat: the model's final projection"
        )
        for hole in record["readiness"]["holes"]
    )


def test_authoring_plain_only_projection_keeps_unwired_hole(
    tmp_path: Path,
) -> None:
    # The fence: with no expression items to pre-place, the
    # accounting does not engage - the columns parameter
    # stays unwired and its authoring hole stands exactly as
    # before.
    report = _seed_fed_project(tmp_path)
    record = next(
        m for m in report["models"] if m["name"] == "stg_customers"
    )
    assert "columns" not in record["scaffold"]["drafted_params"]
    assert any(
        hole["detail"].startswith(
            "the columns parameter of the staging generator "
            "is unwired"
        )
        for hole in record["readiness"]["holes"]
    )


def test_nearest_generator_accounts(tmp_path: Path) -> None:
    # (d): mart-shaped models with no draft name the nearest
    # generator by the joins discriminator; the FOR-loop
    # pivot is named inexpressible, the joined aggregation
    # authorable by hand.
    base_a = _model("stg_a", raw="select 1 as id")
    base_b = _model("stg_b", raw="select 1 as id")
    joined = _model(
        "customers",
        deps=["model.legacy.stg_a", "model.legacy.stg_b"],
        raw=(
            "select a.id, count(*) as n from {{ ref('stg_a') }} a "
            "join {{ ref('stg_b') }} b on a.id = b.id group by a.id"
        ),
    )
    pivot = _model(
        "orders",
        deps=["model.legacy.stg_a", "model.legacy.stg_b"],
        raw=(
            "{% set methods = ['card', 'cash'] %} "
            "select a.id, {% for m in methods %} "
            "sum(case when b.method = '{{ m }}' then b.amount "
            "else 0 end) as {{ m }}_amount, {% endfor %} "
            "count(*) as n from {{ ref('stg_a') }} a "
            "join {{ ref('stg_b') }} b on a.id = b.id group by a.id"
        ),
    )
    unjoined = _model(
        "daily_totals",
        deps=["model.legacy.stg_a", "model.legacy.stg_b"],
        raw=(
            "select id, sum(amount) as total from {{ ref('stg_a') }} "
            "group by id union all select id, sum(amount) as total "
            "from {{ ref('stg_b') }} group by id"
        ),
    )
    _manifest(
        tmp_path,
        {
            "model.legacy.stg_a": base_a,
            "model.legacy.stg_b": base_b,
            "model.legacy.customers": joined,
            "model.legacy.orders": pivot,
            "model.legacy.daily_totals": unjoined,
        },
        None,
    )
    report = migration_assessment(tmp_path)
    by_name = {m["name"]: m for m in report["models"]}
    customers = by_name["customers"]["nearest_generator"]
    assert customers["generator"] == "gold"
    assert customers["suggested_pattern"]["name"] == "gold_pattern"
    assert customers["suggested_pattern"]["required_params"] == [
        "input_models"
    ]
    assert customers["authorable_by_hand"] is True
    assert customers["inexpressible_constructs"] == []
    orders = by_name["orders"]["nearest_generator"]
    assert orders["generator"] == "gold"
    assert orders["authorable_by_hand"] is False
    assert any(
        "for" in text and "compile time" in text
        for text in orders["inexpressible_constructs"]
    )
    assert by_name["daily_totals"]["nearest_generator"]["generator"] == (
        "mart"
    )


def test_suggested_pattern_required_params_per_candidate(
    tmp_path: Path,
) -> None:
    # (b): with several existing patterns for the generator,
    # the TODO branch states each candidate's required params.
    _manifest(tmp_path, _nodes(), _SOURCE)
    definitions = tmp_path / "metadata" / "patterns"
    definitions.mkdir(parents=True, exist_ok=True)
    (definitions / "staging_a.yml").write_text(
        "name: staging_a\ngenerator: staging\n"
        "required_params: [description]\n",
        encoding="utf-8",
    )
    (definitions / "staging_b.yml").write_text(
        "name: staging_b\ngenerator: staging\n"
        "required_params: [owner, columns]\n",
        encoding="utf-8",
    )
    report = migration_assessment(tmp_path)
    write_drafts(tmp_path, report)
    bundle = _load_yaml(_drafts_dir(tmp_path) / "stg_orders.yml")
    suggested = bundle["suggested_pattern"]
    assert suggested["name"] == "TODO"
    assert suggested["required_params"] == {
        "staging_a": ["description"],
        "staging_b": ["owner", "columns"],
    }
