"""The unified text-output grammar.

Pins the grammar's load-bearing properties: flag codes in
default text reconcile with the JSON `interface_flags` (no flag
silently disappears in either mode), name lists fold past ten
with `--verbose` as the unfold, `loom check` fails with the short
coverage gate form whose counts agree with `coverage`, the
stale-manifest error names every changed file in one report, and
validation failures collapse each root cause to one line.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from loom.cli import main
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.manifest import read_manifest, validate_model_freshness
from loom.reporting import render_convert_text, render_coverage_text, render_register_text
from metadata_helpers import write_model_manifest

_CONTRACT = (
    "carries an enforced dbt contract (contract: {enforced: true}); "
    "a Loom-generated replacement drops enforcement unless "
    "`contract_enforced` is declared in the Loom metadata for "
    "the replacement (with column_types completing its types)"
)
_TESTS = (
    "3 test(s) are attached to this model in the manifest; "
    "a swap must keep or deliberately replace them"
)
_CONSUMERS = (
    "5 downstream consumer(s) in the manifest "
    "(models, snapshots, exposures, semantic models, metrics, "
    "saved queries and other projects' models)"
)
_ACCESS = (
    "access is 'public' (dbt Mesh); consumers outside this "
    "project may depend on its interface"
)
_GROUP = (
    "belongs to dbt group 'core' (config.group); a Loom "
    "conversion does not carry group membership, so the group "
    "must be re-declared by hand or cross-group references "
    "may break"
)
_DEPRECATION = (
    "carries a dbt deprecation date (2027-01-01); a "
    "Loom conversion does not carry it, so the deprecation "
    "must be re-declared by hand"
)
_INCREMENTAL = (
    "materialized as 'incremental' (unique_key='id'); a "
    "Loom-generated replacement does not carry this "
    "incremental configuration, so converting it would "
    "silently turn incremental builds into full rebuilds"
)
_GRANTS = (
    "carries dbt grants (select: [analyst]); grants are not "
    "carried by a Loom conversion; re-apply them by hand"
)
_PHYSICAL = (
    "carries physical warehouse configuration "
    "(partition_by={'field': 'day'}) that is not carried by a "
    "Loom conversion; re-apply it by hand or warehouse "
    "cost/performance may regress"
)
_NOVEL = "a flag wording no release has emitted before"


def _report(models: list[dict]) -> dict:
    summary = {
        "project_models": len(models),
        "controlled": 0,
        "known_not_controlled": 0,
        "unknown_to_loom": len(models),
        "registered": len(models),
        "scaffolded": sum(1 for m in models if "scaffold" in m),
        "conversion_drafted": sum(1 for m in models if "fit" in m),
    }
    return {
        "project": "census",
        "manifest": "target/manifest.json",
        "manifest_provenance": "parse-produced",
        "warnings": [],
        "summary": summary,
        "models": models,
    }


def _unknown(name: str, flags: list[str], **extra: object) -> dict:
    model: dict = {
        "name": name,
        "state": "unknown_to_loom",
        "draft_registration": {"classification": "mart"},
        "interface_flags": flags,
    }
    model.update(extra)
    return model


def test_flag_codes_and_census_reconcile_with_json_flags() -> None:
    fit_flags = [
        _CONTRACT,
        _TESTS,
        _CONSUMERS,
        _ACCESS,
        _GROUP,
        _DEPRECATION,
        _INCREMENTAL,
        _GRANTS,
        _PHYSICAL,
    ]
    report = _report(
        [
            _unknown(
                "fct_flagged",
                fit_flags,
                fit={"generator": "fact", "evidence": ["fits"]},
            ),
            _unknown(
                "dim_scaffold",
                ["1 downstream consumer(s) in the manifest (models)"],
                scaffold={
                    "layer": "mart",
                    "target_generator": "dimension",
                    "state": "untouched",
                    "inputs": [],
                },
            ),
        ]
    )
    text = render_convert_text(report)
    # Every flag class on the fit maps to a code on its entry line.
    fit_line = next(
        line for line in text.splitlines() if "fct_flagged" in line
    )
    for code in (
        "[contract]",
        "[tests:3]",
        "[consumers:5]",
        "[access]",
        "[config]",
        "[materialized]",
        "[physical]",
    ):
        assert code in fit_line
    # The legend names every class present.
    flat = re.sub(r"\s+", " ", text)
    assert "[config] other declared configuration" in flat
    assert "[physical] physical warehouse configuration" in flat
    # The scaffold's flags are counted, not dropped.
    assert "Flagged scaffolds: 1 of 1" in text
    # Verbose keeps every prose flag verbatim - nothing disappears.
    verbose = render_convert_text(report, verbose=True)
    for flag in fit_flags:
        assert flag in verbose
    # Default text carries none of the prose.
    assert "downstream consumer(s) in the manifest" not in text


def test_register_census_counts_unclassifiable_flags_as_other() -> None:
    report = _report(
        [
            _unknown(
                "a_model",
                [
                    "2 test(s) are attached to this model in the "
                    "manifest; a swap must keep or deliberately "
                    "replace them",
                    _NOVEL,
                ],
            ),
            _unknown(
                "b_model",
                ["4 downstream consumer(s) in the manifest (models)"],
            ),
        ]
    )
    text = render_register_text(report)
    # Census order is fixed and counts flag occurrences per
    # class (the per-model counts ride in the codes); the
    # unclassifiable flag is counted under "other flags", never
    # dropped.
    assert (
        "Flagged (2 of 2): consumers 1 · attached tests 1 · "
        "other flags 1 (flag occurrences; per-model flags: "
        "--verbose, or JSON)"
        in text
    )
    assert _NOVEL not in text
    verbose = render_register_text(report, verbose=True)
    assert _NOVEL in verbose


def test_register_next_line_points_forward_after_a_write_run() -> None:
    # QOL FR: after `register --write`, the closing Next line used
    # to repeat the pre-write instruction ("...then run
    # `loom register --write` to write them"). A preview keeps
    # that instruction; a write run's Next points forward instead.
    preview = render_register_text(_report([_unknown("a_model", [])]))
    assert "then run `loom register --write` to write them" in preview

    report = _report([_unknown("a_model", [])])
    report["registrations_written"] = ["local/registrations/a_model.yml"]
    written_text = render_register_text(report)
    assert "Registration drafts written (1):" in written_text
    next_line = next(
        line for line in written_text.splitlines() if line.startswith("Next:")
    )
    assert "`loom register --write`" not in next_line
    assert "review them under local/registrations/" in next_line
    assert "`loom validate`" in next_line


def test_register_folds_unknown_names_past_ten() -> None:
    report = _report([_unknown(f"model_{i:02d}", []) for i in range(25)])
    text = render_register_text(report)
    assert "Unknown to Loom - action needed (25):" in text
    assert "First 10: model_00, model_01" in text
    assert "… and 15 more (full list: --verbose, or --format json)" in text
    assert "model_24" not in text
    verbose = render_register_text(report, verbose=True)
    assert "model_24 (mart)" in verbose


def _misplaced_payload() -> dict:
    return {
        "ok": False,
        "missing_models": [],
        "orphan_inventory_entries": [],
        "duplicate_inventory_entries": [],
        "duplicate_manifest_models": [],
        "versioned_manifest_models": [],
        "misplaced_generated_models": ["stg_orders"],
    }


def test_coverage_text_misplaced_names_the_pending_step() -> None:
    # QOL B-3: mid-adoption (instance promoted, swap pending),
    # coverage used to report only PROBLEMS FOUND. The report
    # now names the pending step and the remedy.
    from loom.reporting import render_check_gate_text

    text = render_coverage_text(_misplaced_payload())
    assert "Misplaced generated models (1):" in text
    assert "complete the swap" in text
    assert "`loom generate --write`" in text
    assert "re-run `dbt parse`" in text
    # The register/orphan remedy does not apply to this state.
    assert "remove orphan entries" not in text

    gate = render_check_gate_text(_misplaced_payload())
    assert "1 generated models at unexpected output paths" in gate
    assert "complete the swap" in gate
    assert "`loom generate --write`" in gate


def test_coverage_text_folds_missing_names() -> None:
    payload = {
        "ok": False,
        "missing_models": [f"m{i:02d}" for i in range(15)],
        "orphan_inventory_entries": [],
        "duplicate_inventory_entries": [],
        "duplicate_manifest_models": [],
        "versioned_manifest_models": [],
        "misplaced_generated_models": [],
    }
    text = render_coverage_text(payload)
    assert "State: 0 of 15 project models covered · 0 orphan entries" in text
    assert "… and 5 more (full list: --verbose, or --format json)" in text
    assert "m14" not in text
    verbose = render_coverage_text(payload, verbose=True)
    assert "m14" in verbose


def _write_manifest(project: Path, names: list[str]) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    models_dir = project / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    nodes = {}
    for name in names:
        (models_dir / f"{name}.sql").write_text("select 1\n", encoding="utf-8")
        nodes[f"model.sample.{name}"] = {
            "name": name,
            "resource_type": "model",
            "package_name": "sample",
            "original_file_path": f"models/{name}.sql",
            "depends_on": {"nodes": []},
            "raw_code": "select 1\n",
            "config": {},
            "columns": {},
        }
    (project / "dbt_project.yml").write_text(
        "name: sample\nprofile: sample\nmodel-paths: ['models']\n",
        encoding="utf-8",
    )
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "sample",
                    "generated_at": "2999-01-01T00:00:00Z",
                    "adapter_type": "duckdb",
                },
                "nodes": nodes,
            }
        ),
        encoding="utf-8",
    )


def test_check_failure_prints_gate_form_agreeing_with_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_manifest(tmp_path, ["alpha_model", "beta_model"])
    assert main(["check", "--project-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    # The gate form is the whole failure output:
    # the counts and the next step - no names, and no
    # 24k-character single line.
    assert captured.err == ""
    assert (
        "Inventory coverage: PROBLEMS FOUND - 2 of 2 project "
        "models missing from inventory" in captured.out
    )
    assert "Next: loom register drafts a registration" in captured.out
    assert "alpha_model" not in captured.out
    assert all(len(line) < 500 for line in captured.out.splitlines())
    # The counts agree with coverage's JSON payload.
    assert (
        main(
            [
                "coverage",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert sorted(payload["missing_models"]) == ["alpha_model", "beta_model"]


def test_stale_error_names_every_changed_file_once(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "name: example\n", encoding="utf-8"
    )
    names = [f"model_{i:02d}" for i in range(12)]
    path = write_model_manifest(tmp_path, names, "example")
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    # Touch every model file past the manifest's generated_at.
    import datetime

    generated_at = datetime.datetime.fromisoformat(
        document["metadata"]["generated_at"]
    )
    future = generated_at.timestamp() + 60
    for name in names:
        source = tmp_path / "models" / f"{name}.sql"
        os.utime(source, (future, future))
    with pytest.raises(LoomError) as excinfo:
        validate_model_freshness(tmp_path, path, document)
    message = str(excinfo.value)
    assert "12 changed files" in message
    assert "models/model_00.sql (changed after parsing)" in message
    assert "models/model_09.sql (changed after parsing)" in message
    # Folded past ten, remedy on its own line, project-relative.
    assert "… and 2 more changed files" in message
    assert "models/model_11.sql" not in message
    assert str(tmp_path) not in message
    assert message.endswith("Remedy: re-run `dbt parse` to refresh the manifest")


def test_validation_failure_collapses_root_causes(tmp_path: Path) -> None:
    # Design E2's worked example, on the real quickstart: the
    # bridge instance duplicated with empty params. Before, one
    # root cause (the duplicate's missing params) was stated
    # five times across six ungrouped lines; the grouped form
    # states it once, next to the duplicate it caused.
    import shutil

    import yaml

    quickstart = Path(__file__).parent.parent / "examples" / "quickstart"
    project = tmp_path / "quickstart"
    shutil.copytree(quickstart, project)
    doc_path = project / "metadata" / "instances" / "bridge.yml"
    doc = yaml.safe_load(doc_path.read_text(encoding="utf-8"))
    target = next(
        i for i in doc["instances"] if i["name"] == "bridge_customer_orders"
    )
    duplicate = dict(target)
    duplicate["params"] = {}
    doc["instances"].append(duplicate)
    doc_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    with pytest.raises(LoomError) as excinfo:
        validate_project(project)
    message = str(excinfo.value)
    assert message == (
        "Validation failed: 2 problems in metadata/instances/bridge.yml\n"
        "  instances[bridge_customer_orders]:\n"
        "    - missing required param(s) for pattern 'vault_bridge': "
        "description, parent, path\n"
        "    - duplicate inventory name 'bridge_customer_orders' "
        "(declared 2 times in this file)\n"
        "Fix the file(s) above and re-run `loom validate`."
    )
