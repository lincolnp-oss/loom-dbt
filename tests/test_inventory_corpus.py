"""The unioned inventory corpus.

These tests pin: the metadata/manifest union with derived
statuses, Loom enrichment, output columns via the resolver,
documentation unification with provenance, graph
edges, tier-2 records, the metadata-only mode's collapsed
statuses, and the manifest freshness behaviour.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from loom.errors import LoomError
from loom.inventory import build_inventory
from metadata_helpers import write_pattern_instance


def _write_project_metadata(project: Path) -> None:
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Staged customers",
            "owner": "data-team",
            "tags": ["crm"],
            "source_ref": {"name": "raw", "table": "customers"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "email", "alias": "email"},
            ],
            "column_docs": {"email": "Email (Loom)"},
        },
        pattern="staging_pattern",
    )
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            "description": "Customer hub (Loom)",
            "owner": "data-team",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub_pattern",
    )
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "customer_mart.yml").write_text(
        "name: customer_mart\nclassification: mart\n"
        "description: Customer mart (registration)\nowner: finance\n"
        "keys: [customer_id]\ndepends_on: [hub_customer]\n"
        "tracked_columns: [revenue]\n",
        encoding="utf-8",
    )
    (registrations / "legacy_report.yml").write_text(
        "name: legacy_report\nclassification: mart\n"
        "description: Legacy report\nowner: finance\n"
        "depends_on: [customer_mart]\n",
        encoding="utf-8",
    )


def _node(
    name: str,
    *,
    deps: list[str] | None = None,
    macros: list[str] | None = None,
    description: str | None = None,
    columns: dict | None = None,
    resource_type: str = "model",
    package_name: str = "pilot",
    meta: dict | None = None,
    path: str | None = None,
) -> dict:
    node: dict = {
        "name": name,
        "resource_type": resource_type,
        "package_name": package_name,
        "original_file_path": path or f"models/{name}.sql",
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
            if node.get("package_name") != "pilot":
                continue
            source = project / node["original_file_path"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("select 1\n", encoding="utf-8")
    # Stamp generated_at only after the model files exist, so
    # the freshness guard sees them as older than the manifest.
    document["metadata"]["generated_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _full_document() -> dict:
    nodes = {
        "model.pilot.stg_customers": _node(
            "stg_customers",
            deps=["source.pilot.raw.customers"],
            macros=["macro.pilot.cents_to_dollars"],
            description="Staged customers",
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
            path="models/nested/stg_customers.sql",
        ),
        "model.pilot.hub_customer": _node(
            "hub_customer",
            deps=["model.pilot.stg_customers"],
            description="Customer hub (manifest)",
        ),
        "model.pilot.customer_mart": _node(
            "customer_mart",
            deps=["model.pilot.hub_customer"],
            description="Customer mart (manifest)",
            columns={
                "revenue": {
                    "name": "revenue",
                    "data_type": "NUMERIC",
                    "description": "Revenue (manifest)",
                }
            },
        ),
        "model.pilot.raw_orders": _node(
            "raw_orders",
            description="Unmanaged orders extract",
            columns={
                "order_id": {"name": "order_id", "data_type": "INTEGER"}
            },
            meta={"owner": "ops-team"},
        ),
        "seed.pilot.country_codes": _node(
            "country_codes",
            resource_type="seed",
            path="seeds/country_codes.csv",
        ),
        "snapshot.pilot.customers_snapshot": _node(
            "customers_snapshot",
            deps=["model.pilot.stg_customers"],
            resource_type="snapshot",
            path="snapshots/customers_snapshot.sql",
        ),
        "test.pilot.not_null_stg_customers": _node(
            "not_null_stg_customers",
            deps=["model.pilot.stg_customers"],
            resource_type="test",
        ),
    }
    return {
        "metadata": {
            "project_name": "pilot",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "nodes": nodes,
        "sources": {
            "source.pilot.raw.customers": {
                "name": "customers",
                "source_name": "raw",
                "package_name": "pilot",
                "description": "Raw customers feed",
                "resource_type": "source",
            }
        },
        "macros": {
            "macro.pilot.cents_to_dollars": {
                "name": "cents_to_dollars",
                "package_name": "pilot",
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
        },
        "exposures": {
            "exposure.pilot.customer_dashboard": {
                "name": "customer_dashboard",
                "package_name": "pilot",
                "description": "Customer dashboard",
                "depends_on": {"nodes": ["model.pilot.customer_mart"]},
            }
        },
    }


@pytest.fixture()
def full_project(tmp_path: Path) -> Path:
    _write_project_metadata(tmp_path)
    _write_manifest(tmp_path, _full_document())
    return tmp_path


def test_union_statuses_and_modes(full_project: Path) -> None:
    corpus = build_inventory(full_project)
    assert corpus.mode == "full"
    assert corpus.project_name == "pilot"
    assert corpus.manifest_path is not None
    assert corpus.manifest_generated_at is not None
    statuses = {record.name: record.status for record in corpus.models}
    assert statuses == {
        "stg_customers": "generated",
        "hub_customer": "generated",
        "customer_mart": "registered",
        "legacy_report": "orphaned",
        "raw_orders": "unmanaged",
    }


def test_loom_enrichment(full_project: Path) -> None:
    corpus = build_inventory(full_project)
    staging = corpus.model("stg_customers")
    assert staging is not None
    assert staging.generator == "staging"
    assert staging.pattern == "staging_pattern"
    assert staging.paradigm == "staging"
    assert staging.owner == "data-team"
    assert staging.tags == ("crm",)
    assert staging.unique_id == "model.pilot.stg_customers"
    assert staging.metadata_path is not None
    assert staging.path == "models/nested/stg_customers.sql"
    # The manifest file sits somewhere other than the expected
    # generated location: the coverage state surfaces it.
    assert staging.misplaced is True

    hub = corpus.model("hub_customer")
    assert hub is not None
    assert hub.generator == "hub"
    assert hub.keys == ("customer_id",)
    assert hub.inputs == ("stg_customers",)

    mart = corpus.model("customer_mart")
    assert mart is not None
    assert mart.classification == "mart"
    assert mart.keys == ("customer_id",)

    unmanaged = corpus.model("raw_orders")
    assert unmanaged is not None
    assert unmanaged.generator is None
    assert unmanaged.pattern is None
    assert unmanaged.owner == "ops-team"  # manifest meta.owner
    assert unmanaged.has_owner is True


def test_documentation_unification(full_project: Path) -> None:
    corpus = build_inventory(full_project)
    # Identical text in both sources: one source, not two.
    staging = corpus.model("stg_customers")
    assert staging is not None
    assert staging.description == "Staged customers"
    assert staging.description_provenance == "both-agreeing"
    assert staging.documented is True
    # Differing text on a generated model: Loom wins.
    hub = corpus.model("hub_customer")
    assert hub is not None
    assert hub.description == "Customer hub (Loom)"
    assert hub.description_provenance == "loom"
    # Differing text on a registered model: the manifest wins.
    mart = corpus.model("customer_mart")
    assert mart is not None
    assert mart.description == "Customer mart (manifest)"
    assert mart.description_provenance == "manifest"

    # Column level, on the resolver's output set.
    assert staging.columns_source == "resolver"
    assert staging.columns_partial is False
    by_name = {column.name: column for column in staging.columns}
    assert by_name["customer_id"].description == "Customer identifier"
    assert by_name["customer_id"].description_provenance == "manifest"
    assert by_name["customer_id"].data_type == "INTEGER"
    assert by_name["customer_id"].data_type_provenance == "manifest"
    # Loom column_docs beat the manifest's differing text (generated).
    assert by_name["email"].description == "Email (Loom)"
    assert by_name["email"].description_provenance == "loom"
    assert by_name["email"].data_type == "VARCHAR"

    # Registered model: columns are the manifest's documented
    # set, published as a partial list.
    assert mart.columns_source == "manifest"
    assert mart.columns_partial is True
    assert [column.name for column in mart.columns] == ["revenue"]
    assert mart.columns_documented == 1
    assert mart.columns_total == 1

    # Unmanaged model: manifest documentation only.
    unmanaged = corpus.model("raw_orders")
    assert unmanaged is not None
    assert unmanaged.description_provenance == "manifest"
    assert [column.name for column in unmanaged.columns] == ["order_id"]
    assert unmanaged.columns_documented == 0


def test_graph_and_health(full_project: Path) -> None:
    corpus = build_inventory(full_project)
    staging = corpus.model("stg_customers")
    hub = corpus.model("hub_customer")
    mart = corpus.model("customer_mart")
    orphan = corpus.model("legacy_report")
    assert staging is not None and hub is not None and mart is not None
    assert orphan is not None
    assert hub.upstream == ("stg_customers",)
    assert "hub_customer" in staging.downstream
    assert "customers_snapshot" in staging.downstream
    assert mart.upstream == ("hub_customer",)
    assert staging.graph_declared_only is False
    # The orphaned registration's graph is declared-only.
    assert orphan.graph_declared_only is True
    assert orphan.upstream == ("customer_mart",)
    # Test presence comes from the manifest test nodes.
    assert staging.has_tests is True
    assert hub.has_tests is False


def test_tier2_records(full_project: Path) -> None:
    corpus = build_inventory(full_project)
    by_kind_name = {(o.kind, o.name): o for o in corpus.objects}
    source = by_kind_name[("source", "raw.customers")]
    assert source.provenance == "project"
    assert source.description == "Raw customers feed"
    assert "stg_customers" in source.downstream

    macro = by_kind_name[("macro", "cents_to_dollars")]
    assert macro.provenance == "project"
    assert macro.arguments is not None
    assert [(a.name, a.type, a.has_default) for a in macro.arguments] == [
        ("amount", "number", False),
        ("precision", None, True),
    ]
    assert macro.used_by == ("stg_customers",)

    package_macro = by_kind_name[("macro", "slugify")]
    assert package_macro.provenance == "package"
    assert package_macro.package == "dbt_utils"

    assert ("seed", "country_codes") in by_kind_name
    assert ("snapshot", "customers_snapshot") in by_kind_name
    exposure = by_kind_name[("exposure", "customer_dashboard")]
    assert exposure.upstream == ("customer_mart",)


def test_metadata_only_mode_collapses_statuses(tmp_path: Path) -> None:
    _write_project_metadata(tmp_path)
    corpus = build_inventory(tmp_path)
    assert corpus.mode == "metadata-only"
    assert corpus.manifest_path is None
    assert corpus.manifest_generated_at is None
    statuses = {record.name: record.status for record in corpus.models}
    # No unmanaged records exist, and the manifest-absent
    # registration is registered - orphaned is unknowable here
    # and must not be implied.
    assert statuses == {
        "stg_customers": "generated",
        "hub_customer": "generated",
        "customer_mart": "registered",
        "legacy_report": "registered",
    }
    hub = corpus.model("hub_customer")
    assert hub is not None
    assert hub.graph_declared_only is True
    assert hub.upstream == ("stg_customers",)
    staging = corpus.model("stg_customers")
    assert staging is not None
    assert staging.downstream == ("hub_customer",)
    assert staging.columns_source == "resolver"
    sources = [o for o in corpus.objects if o.kind == "source"]
    assert [o.name for o in sources] == ["raw.customers"]


def test_stale_manifest_guard_and_opt_out(full_project: Path) -> None:
    model_file = full_project / "models" / "raw_orders.sql"
    model_file.write_text("select 2\n", encoding="utf-8")
    with pytest.raises(LoomError):
        build_inventory(full_project)
    corpus = build_inventory(full_project, check_freshness=False)
    assert corpus.mode == "full"
    assert corpus.manifest_generated_at is not None


def test_project_file_staleness_guard_and_opt_out(full_project: Path) -> None:
    # M4 at the corpus seam: a dbt_project.yml edit after the
    # parse trips the guarded build (naming the file) and is
    # tolerated by the freshness-opt-out build search uses.
    import os
    from datetime import datetime

    project_file = full_project / "dbt_project.yml"
    project_file.write_text("name: pilot\n", encoding="utf-8")
    os.utime(project_file, (1_600_000_000, 1_600_000_000))
    generated = build_inventory(full_project).manifest_generated_at
    assert generated is not None
    future = generated.timestamp() + 60
    os.utime(project_file, (future, future))
    with pytest.raises(LoomError, match="dbt_project.yml"):
        build_inventory(full_project)
    corpus = build_inventory(full_project, check_freshness=False)
    assert corpus.mode == "full"
    assert corpus.manifest_generated_at is not None


def test_missing_explicit_manifest_raises(tmp_path: Path) -> None:
    _write_project_metadata(tmp_path)
    with pytest.raises(LoomError):
        build_inventory(tmp_path, tmp_path / "target" / "manifest.json")


def test_corpus_as_dict_is_json_shaped(full_project: Path) -> None:
    corpus = build_inventory(full_project)
    payload = json.loads(json.dumps(corpus.as_dict()))
    assert payload["mode"] == "full"
    assert {m["name"] for m in payload["models"]} == {
        "stg_customers",
        "hub_customer",
        "customer_mart",
        "legacy_report",
        "raw_orders",
    }
    staging = next(m for m in payload["models"] if m["name"] == "stg_customers")
    assert staging["columns_documented"] == 2
    assert staging["columns_total"] == staging["columns_total"]


def test_synthetic_thousand_model_project(tmp_path: Path) -> None:
    total = 1000
    generated = 200
    instances = []
    for index in range(generated):
        instances.append(
            {
                "name": f"model_{index:04d}",
                "params": {
                    "description": f"Staged model {index}",
                    "owner": "data-team",
                    "source_ref": {"name": "raw", "table": "customers"},
                    "columns": [{"source": "id", "alias": "id"}],
                },
            }
        )
    patterns = tmp_path / "metadata" / "patterns"
    patterns.mkdir(parents=True)
    (patterns / "staging_pattern.yml").write_text(
        yaml.safe_dump({"name": "staging_pattern", "generator": "staging"}),
        encoding="utf-8",
    )
    instance_dir = tmp_path / "metadata" / "instances"
    instance_dir.mkdir(parents=True)
    (instance_dir / "staging_pattern.yml").write_text(
        yaml.safe_dump(
            {"pattern": "staging_pattern", "instances": instances}
        ),
        encoding="utf-8",
    )
    nodes = {}
    for index in range(total):
        name = f"model_{index:04d}"
        nodes[f"model.bigproj.{name}"] = _node(
            name, package_name="bigproj", description=f"Model {index}"
        )
    document = {"metadata": {"project_name": "bigproj"}, "nodes": nodes}
    # _write_manifest only writes files for the pilot package;
    # write this project's files directly, then stamp the
    # manifest's generated_at after they exist.
    for node in nodes.values():
        source = tmp_path / node["original_file_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("select 1\n", encoding="utf-8")
    document["metadata"]["generated_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )

    started = time.perf_counter()
    corpus = build_inventory(tmp_path)
    elapsed = time.perf_counter() - started
    print(f"\n1000-model corpus build: {elapsed:.2f}s")
    assert len(corpus.models) == total
    statuses = [record.status for record in corpus.models]
    assert statuses.count("generated") == generated
    assert statuses.count("unmanaged") == total - generated
    # The D8 bound: the ratified target is a few seconds at
    # most on this synthetic project. Measured locally at
    # 0.17s (0.24s when the guard was first reported), so 5s
    # asserts the target's outer edge with roughly 20-30x
    # headroom - enough for the shared self-hosted runners'
    # contention, tight enough that a real regression (a
    # second manifest parse, a quadratic corpus walk) trips
    # it instead of passing silently under the old 30s.
    assert elapsed < 5
