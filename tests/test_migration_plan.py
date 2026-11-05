from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.cli import main
from loom.migration_plan import migration_plan
from manifest_builders import manifest_node


def _write_manifest(
    project: Path,
    *,
    nodes: dict[str, dict[str, object]] | None = None,
    extras: dict[str, object] | None = None,
    generated_at: str | None = None,
) -> Path:
    path = project / "target" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "metadata": {
            "project_name": "pilot",
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        },
        "nodes": nodes or {},
    }
    if extras:
        document.update(extras)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _model(
    name: str,
    dependencies: list[str],
    *,
    package: str = "pilot",
) -> dict[str, object]:
    return manifest_node(name, package=package, deps=dependencies)


def _cycle_manifest(project: Path) -> Path:
    for name in ("old_model", "new_model", "direct", "transitive"):
        source = project / "models" / f"{name}.sql"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("select 1\n", encoding="utf-8")
    nodes: dict[str, dict[str, object]] = {
        "model.pilot.old_model": _model("old_model", ["model.pilot.transitive"]),
        "model.pilot.new_model": _model("new_model", []),
        "model.pilot.direct": _model("direct", ["model.pilot.old_model"]),
        "model.pilot.transitive": _model("transitive", ["model.pilot.direct"]),
        "model.other.external_model": _model(
            "external_model", ["model.pilot.old_model"], package="other"
        ),
    }
    return _write_manifest(
        project,
        nodes=nodes,
        extras={
            "exposures": {
                "exposure.pilot.dashboard": {
                    "name": "dashboard",
                    "resource_type": "exposure",
                    "depends_on": {"nodes": ["model.pilot.transitive"]},
                }
            },
            "metrics": {
                "metric.pilot.active_customers": {
                    "name": "active_customers",
                    "resource_type": "metric",
                    "depends_on": {"nodes": ["semantic_model.pilot.usage"]},
                }
            },
            "semantic_models": {
                "semantic_model.pilot.usage": {
                    "name": "usage",
                    "resource_type": "semantic_model",
                    "depends_on": {"nodes": ["model.pilot.transitive"]},
                }
            },
        },
    )


def test_migration_plan_includes_direct_transitive_and_observed_consumers(
    tmp_path: Path,
) -> None:
    _cycle_manifest(tmp_path)

    result = migration_plan(tmp_path, "old_model", "new_model")

    inventory = result["inventory"]
    assert [model["name"] for model in inventory["direct_dependents"]] == ["direct"]
    assert [model["name"] for model in inventory["transitive_dependents"]] == [
        "transitive"
    ]
    assert inventory["transitive_dependents"][0]["path"] == [
        "model.pilot.old_model",
        "model.pilot.direct",
        "model.pilot.transitive",
    ]
    consumers = inventory["observed_manifest_consumers"]
    assert {item["unique_id"] for item in consumers} == {
        "model.other.external_model",
        "exposure.pilot.dashboard",
        "metric.pilot.active_customers",
        "semantic_model.pilot.usage",
    }
    metric = next(
        item
        for item in consumers
        if item["unique_id"] == "metric.pilot.active_customers"
    )
    assert metric["path"] == [
        "model.pilot.old_model",
        "model.pilot.direct",
        "model.pilot.transitive",
        "semantic_model.pilot.usage",
        "metric.pilot.active_customers",
    ]
    assert inventory["external_consumers"]["status"] == "unknown"
    assert "count" not in inventory
    assert "No equivalence" in " ".join(result["limitations"])
    assert [phase["phase"] for phase in result["migration_phases"]] == [
        "build_alongside",
        "prove_invariants",
        "move_dependent_refs_deliberately",
        "retire_later",
    ]


@pytest.mark.parametrize(
    ("old_model", "new_model", "error"),
    [
        ("old_model", "old_model", "distinct models"),
        ("missing", "new_model", "was not found"),
    ],
)
def test_migration_plan_rejects_invalid_model_inputs(
    tmp_path: Path, old_model: str, new_model: str, error: str
) -> None:
    _cycle_manifest(tmp_path)

    with pytest.raises(LoomError, match=error):
        migration_plan(tmp_path, old_model, new_model)


def test_migration_plan_rejects_invalid_dependency_shape(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    for name in ("old_model", "new_model", "broken"):
        (models / f"{name}.sql").write_text("select 1\n", encoding="utf-8")
    nodes = {
        "model.pilot.old_model": _model("old_model", []),
        "model.pilot.new_model": _model("new_model", []),
        "model.pilot.broken": {
            **_model("broken", []),
            "depends_on": {"nodes": "model.pilot.old_model"},
        },
    }
    _write_manifest(tmp_path, nodes=nodes)

    with pytest.raises(LoomError, match="invalid depends_on.nodes"):
        migration_plan(tmp_path, "old_model", "new_model")


def test_migration_plan_rejects_stale_or_missing_manifest(tmp_path: Path) -> None:
    manifest = _cycle_manifest(tmp_path)
    stale = tmp_path / "models" / "old_model.sql"
    generated_at = datetime.fromisoformat(
        json.loads(manifest.read_text(encoding="utf-8"))["metadata"]["generated_at"]
    )
    future = generated_at.timestamp() + 60
    os.utime(stale, (future, future))

    with pytest.raises(LoomError, match="dbt manifest is stale"):
        migration_plan(tmp_path, "old_model", "new_model")

    (tmp_path / "target" / "manifest.json").unlink()
    with pytest.raises(LoomError, match="dbt manifest is required"):
        migration_plan(tmp_path, "old_model", "new_model")


def test_migration_plan_uses_manifest_raw_code_not_checksum(tmp_path: Path) -> None:
    manifest = _cycle_manifest(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["nodes"]["model.pilot.old_model"]["checksum"] = {
        "name": "sha256",
        "checksum": "82c4-not-the-source-file-hash",
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")

    result = migration_plan(tmp_path, "old_model", "new_model")

    assert result["from"]["name"] == "old_model"


def test_migration_plan_matches_crlf_manifest_raw_code(tmp_path: Path) -> None:
    manifest = _cycle_manifest(tmp_path)
    source = tmp_path / "models" / "old_model.sql"
    source.write_bytes(b"select 1\r\n")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    generated_at = datetime.fromisoformat(document["metadata"]["generated_at"])
    past = generated_at.timestamp() - 60
    os.utime(source, (past, past))
    document["nodes"]["model.pilot.old_model"]["raw_code"] = "select 1\r\n"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    result = migration_plan(tmp_path, "old_model", "new_model")

    assert result["from"]["name"] == "old_model"


def test_migration_plan_detects_raw_code_mismatch_even_with_old_mtime(
    tmp_path: Path,
) -> None:
    manifest = _cycle_manifest(tmp_path)
    source = tmp_path / "models" / "old_model.sql"
    source.write_text("select 2\n", encoding="utf-8")
    generated_at = datetime.fromisoformat(
        json.loads(manifest.read_text(encoding="utf-8"))["metadata"]["generated_at"]
    )
    past = generated_at.timestamp() - 60
    os.utime(source, (past, past))

    with pytest.raises(LoomError, match="does not match manifest raw_code"):
        migration_plan(tmp_path, "old_model", "new_model")


def test_migration_plan_cli_emits_json_without_modifying_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _cycle_manifest(tmp_path)
    model = tmp_path / "models" / "direct.sql"
    before = model.read_bytes()

    result = main(
        [
            "plan",
            "--project-dir",
            str(tmp_path),
            "--from",
            "old_model",
            "--to",
            "new_model",
            "--format",
            "json",
        ]
    )

    output = capsys.readouterr()
    assert result == 0
    parsed = json.loads(output.out)
    # The payload names the flat CLI operation.
    assert parsed["operation"] == "plan"
    assert parsed["from"]["name"] == "old_model"
    assert model.read_bytes() == before


def test_migration_plan_annotates_inventory_states(tmp_path: Path) -> None:
    # The from/to models and project-owned dependents carry
    # their Loom inventory state.
    _cycle_manifest(tmp_path)
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "old_model.yml").write_text(
        "name: old_model\nclassification: legacy\n",
        encoding="utf-8",
    )
    result = migration_plan(tmp_path, "old_model", "new_model")
    assert result["from"]["state"] == "known_not_controlled"
    assert result["to"]["state"] == "unknown_to_loom"
    direct = result["inventory"]["direct_dependents"]
    assert direct and all("state" in item for item in direct)


def test_plan_names_reachable_metrics_and_saved_queries(
    tmp_path: Path,
) -> None:
    # Plan's manifest consumers name the semantic layer
    # reachable from the replaced model - the metric and the
    # saved query behind its semantic model appear by name, in
    # the payload and in the text rendering.
    for name in ("orders", "orders_v2"):
        source = tmp_path / "models" / f"{name}.sql"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("select 1\n", encoding="utf-8")
    nodes = {
        "model.pilot.orders": _model("orders", []),
        "model.pilot.orders_v2": _model("orders_v2", []),
    }
    _write_manifest(
        tmp_path,
        nodes=nodes,
        extras={
            "semantic_models": {
                "semantic_model.pilot.orders_sm": {
                    "name": "orders_sm",
                    "resource_type": "semantic_model",
                    "depends_on": {"nodes": ["model.pilot.orders"]},
                }
            },
            "metrics": {
                "metric.pilot.revenue": {
                    "name": "revenue",
                    "resource_type": "metric",
                    "depends_on": {
                        "nodes": ["semantic_model.pilot.orders_sm"]
                    },
                }
            },
            "saved_queries": {
                "saved_query.pilot.weekly_revenue": {
                    "name": "weekly_revenue",
                    "resource_type": "saved_query",
                    "depends_on": {"nodes": ["metric.pilot.revenue"]},
                }
            },
        },
    )
    result = migration_plan(tmp_path, "orders", "orders_v2")
    consumers = result["inventory"]["observed_manifest_consumers"]
    named = {(item["resource_type"], item["name"]) for item in consumers}
    assert ("semantic_model", "orders_sm") in named
    assert ("metric", "revenue") in named
    assert ("saved_query", "weekly_revenue") in named


def test_plan_text_names_semantic_consumers(tmp_path: Path, capsys) -> None:
    _cycle_manifest(tmp_path)
    rc = main(
        [
            "plan",
            "--from", "old_model",
            "--to", "new_model",
            "--project-dir", str(tmp_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "metrics (1): active_customers" in out
    assert "semantic models (1): usage" in out
