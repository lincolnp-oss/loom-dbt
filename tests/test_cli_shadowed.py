"""The shadowed state (P5): a generated model and a hand-written
model sharing a name.

While both files exist dbt cannot parse the project at all, so
the manifest can never be refreshed out of the state - yet
`loom coverage` used to prescribe exactly that (`re-run
`dbt parse``), and `loom generate` completed the
generate-before-remove window without naming it. `loom check`
had the same loop through its gate, which renders from the
coverage payload the state makes unbuildable. These tests pin
the behaviour: generate (preview and write), coverage and
check detect the state from the filesystem - no manifest
read - and name it with its way out; neither coverage nor
check prescribes `dbt parse` for it; and every non-shadowed
output is unchanged (the section and the JSON key appear only
in the state).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.cli import _shadowed_models, main
from loom.inventory import InventoryEntry
from loom.reporting import (
    render_check_shadowed_text,
    render_coverage_shadowed_text,
)

from metadata_helpers import write_pattern_instance

_PAIR_LINE = (
    "  stg_customers: models/_generated/staging/stg_customers.sql "
    "(generated) · models/staging/stg_customers.sql (hand-written)"
)
_LEAD_LINE = (
    "  dbt cannot parse the project while a generated model and a "
    "hand-written model share a name, so every dbt command "
    "(parse, seed, build) will fail until the swap is completed"
)
_NEXT_LINE = (
    "  Next: remove or move the hand-written original(s) once the "
    "generated models are proven, or prove the generated models "
    "in a scratch copy and swap last (adoption playbook, "
    "Steps 6-7)."
)


def _write_shadow_project(project: Path) -> None:
    """A staging instance promoted into the inventory while its
    hand-written original still exists - the state a generate
    run turns into the shadowed state."""
    (project / "models" / "staging").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: sample\nversion: '1.0.0'\nconfig-version: 2\n"
        "profile: sample\n",
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: ecom\n    tables:\n"
        "      - name: raw_customers\n",
        encoding="utf-8",
    )
    (project / "models" / "staging" / "stg_customers.sql").write_text(
        "select id as customer_id "
        "from {{ source('ecom', 'raw_customers') }}\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customers.",
            "owner": "analytics-team",
            "source_ref": {"name": "ecom", "table": "raw_customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="staging_pattern",
    )


def _write_manifest(project: Path) -> None:
    """A fresh manifest recording the hand-written original -
    the manifest a project has when generate is about to run."""
    original = project / "models" / "staging" / "stg_customers.sql"
    manifest = {
        "metadata": {
            "project_name": "sample",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dbt_schema_version": (
                "https://schemas.getdbt.com/dbt/manifest/v12.json"
            ),
        },
        "nodes": {
            "model.sample.stg_customers": {
                "name": "stg_customers",
                "resource_type": "model",
                "package_name": "sample",
                "original_file_path": "models/staging/stg_customers.sql",
                "raw_code": original.read_text(encoding="utf-8"),
            }
        },
    }
    path = project / "target" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _generate(project: Path, *extra: str) -> int:
    return main(
        [
            "generate",
            "--project-dir",
            str(project),
            "--dialect",
            "duckdb",
            *extra,
        ]
    )


def test_generate_write_names_the_shadowed_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)

    assert _generate(tmp_path, "--write") == 0
    out = capsys.readouterr().out

    generated = tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    assert generated.is_file()
    assert "Shadowed models (1):" in out
    assert _PAIR_LINE in out
    assert _LEAD_LINE in out
    assert _NEXT_LINE in out


def test_generate_preview_forecasts_the_shadowed_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)

    assert _generate(tmp_path) == 0
    out = capsys.readouterr().out

    # Nothing was written, but the preview names what the write
    # would bring about, ahead of the apply hint.
    assert not (
        tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    ).exists()
    assert "Shadowed models (1):" in out
    assert _PAIR_LINE in out
    assert out.index("Shadowed models (1):") < out.index(
        "Re-run with --write to apply"
    )


def test_generate_preview_names_an_existing_shadow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    generated = tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    generated.parent.mkdir(parents=True)
    generated.write_text("-- Generated by Loom\nselect 1\n", encoding="utf-8")

    assert _generate(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Shadowed models (1):" in out
    assert _PAIR_LINE in out


def test_generate_write_json_carries_shadowed_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)

    assert _generate(tmp_path, "--write", "--format", "json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["shadowed_models"] == [
        {
            "name": "stg_customers",
            "generated": "models/_generated/staging/stg_customers.sql",
            "hand_written": ["models/staging/stg_customers.sql"],
        }
    ]


def test_generate_without_a_twin_reports_no_shadow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    (tmp_path / "models" / "staging" / "stg_customers.sql").unlink()

    assert _generate(tmp_path, "--write") == 0
    out = capsys.readouterr().out
    assert "Shadowed" not in out

    assert _generate(tmp_path, "--write", "--format", "json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert "shadowed_models" not in payload


def test_coverage_names_the_shadowed_state_instead_of_prescribing_parse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert main(["coverage", "--project-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()

    assert captured.out == (
        "Inventory coverage: CANNOT RUN - shadowed models\n"
        "State: dbt cannot parse the project while a generated "
        "model and a hand-written model share a name, so the "
        "manifest cannot be refreshed and coverage cannot be "
        "checked.\n"
        "\n"
        "Shadowed models (1):\n"
        f"{_PAIR_LINE}\n"
        "\n"
        "Next: remove or move the hand-written original(s) once "
        "the generated models are proven, or prove the generated "
        "models in a scratch copy and swap last (adoption "
        "playbook, Steps 6-7); then re-run coverage.\n"
    )
    # The remedy loop is gone: nothing prescribes the parse that
    # cannot succeed, on either stream.
    assert "dbt parse" not in captured.out
    assert "dbt parse" not in captured.err


def test_coverage_json_reports_the_shadowed_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert (
        main(["coverage", "--project-dir", str(tmp_path), "--format", "json"])
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "state": "shadowed",
        "shadowed_models": [
            {
                "name": "stg_customers",
                "generated": "models/_generated/staging/stg_customers.sql",
                "hand_written": ["models/staging/stg_customers.sql"],
            }
        ],
    }


def test_coverage_shadowed_state_blocks_despite_select(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The unrefreshable manifest is a project fact: a selection
    # cannot carve an exception out of the shadowed state.
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert (
        main(
            [
                "coverage",
                "--project-dir",
                str(tmp_path),
                "--select",
                "stg_customers",
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "CANNOT RUN - shadowed models" in out
    assert "dbt parse" not in out


def test_coverage_works_again_once_the_original_is_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()
    assert main(["coverage", "--project-dir", str(tmp_path)]) == 1
    capsys.readouterr()

    # The prescribed resolution: the hand-written original goes,
    # dbt parses again, and coverage runs as before.
    (tmp_path / "models" / "staging" / "stg_customers.sql").unlink()
    generated = tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    manifest = {
        "metadata": {
            "project_name": "sample",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dbt_schema_version": (
                "https://schemas.getdbt.com/dbt/manifest/v12.json"
            ),
        },
        "nodes": {
            "model.sample.stg_customers": {
                "name": "stg_customers",
                "resource_type": "model",
                "package_name": "sample",
                "original_file_path": (
                    "models/_generated/staging/stg_customers.sql"
                ),
                "raw_code": generated.read_text(encoding="utf-8"),
            }
        },
    }
    (tmp_path / "target" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    assert main(["coverage", "--project-dir", str(tmp_path)]) == 0
    assert "Inventory coverage: OK" in capsys.readouterr().out


def test_shadowed_models_detection_bounds(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "name: sample\n", encoding="utf-8"
    )
    entry = InventoryEntry(
        "metadata/instances/staging.yml",
        {
            "name": "stg_customers",
            "generator": "staging",
        },
    )
    generated = tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    twin = tmp_path / "models" / "staging" / "stg_customers.sql"

    # Neither side on disk: no pair, planned or not.
    assert _shadowed_models(tmp_path, [entry]) == []
    # A hand-written file alone is not a shadow.
    twin.parent.mkdir(parents=True)
    twin.write_text("select 1\n", encoding="utf-8")
    assert _shadowed_models(tmp_path, [entry]) == []
    # A run planning the generated file forecasts the pair.
    assert _shadowed_models(tmp_path, [entry], planned_paths=[generated]) == [
        {
            "name": "stg_customers",
            "generated": "models/_generated/staging/stg_customers.sql",
            "hand_written": ["models/staging/stg_customers.sql"],
        }
    ]
    # Once the generated file exists the pair stands on disk.
    generated.parent.mkdir(parents=True)
    generated.write_text("-- Generated by Loom\nselect 1\n", encoding="utf-8")
    assert len(_shadowed_models(tmp_path, [entry])) == 1
    # The swap completes: no pair.
    twin.unlink()
    assert _shadowed_models(tmp_path, [entry]) == []


def test_shadowed_models_detection_covers_satellite_auxiliary_view(
    tmp_path: Path,
) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "name: sample\n", encoding="utf-8"
    )
    entry = InventoryEntry(
        "metadata/instances/sat.yml",
        {"name": "sat_customer", "generator": "satellite"},
    )
    models = tmp_path / "models"
    (models / "legacy").mkdir(parents=True)
    (models / "sat_customer.sql").write_text(
        "-- Generated by Loom\nselect 1\n", encoding="utf-8"
    )
    (models / "sat_customer_current.sql").write_text(
        "-- Generated by Loom\nselect 1\n", encoding="utf-8"
    )
    (models / "legacy" / "sat_customer_current.sql").write_text(
        "select 1\n", encoding="utf-8"
    )

    # The satellite's own name is not shadowed (its only file is
    # the generated one); its auxiliary current-state view is.
    assert _shadowed_models(tmp_path, [entry]) == [
        {
            "name": "sat_customer_current",
            "generated": "models/sat_customer_current.sql",
            "hand_written": ["models/legacy/sat_customer_current.sql"],
        }
    ]


def test_check_names_the_shadowed_state_instead_of_prescribing_parse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert main(["check", "--project-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()

    assert captured.out == (
        "Inventory coverage: CANNOT RUN - shadowed models\n"
        "State: dbt cannot parse the project while a generated "
        "model and a hand-written model share a name, so the "
        "manifest cannot be refreshed and check cannot run.\n"
        "\n"
        "Shadowed models (1):\n"
        f"{_PAIR_LINE}\n"
        "\n"
        "Next: remove or move the hand-written original(s) once "
        "the generated models are proven, or prove the generated "
        "models in a scratch copy and swap last (adoption "
        "playbook, Steps 6-7); then re-run check.\n"
    )
    # The remedy loop is gone: before the pre-check, check
    # surfaced the stale-manifest error - remedy `dbt parse` -
    # on stderr. Nothing prescribes it now, on either stream.
    assert "dbt parse" not in captured.out
    assert "dbt parse" not in captured.err
    assert captured.err == ""


def test_check_json_reports_the_shadowed_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert (
        main(["check", "--project-dir", str(tmp_path), "--format", "json"])
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    # The one shadowed-state payload shape, shared with
    # coverage: no fabricated coverage counts check cannot
    # compute in this state.
    assert payload == {
        "ok": False,
        "state": "shadowed",
        "shadowed_models": [
            {
                "name": "stg_customers",
                "generated": "models/_generated/staging/stg_customers.sql",
                "hand_written": ["models/staging/stg_customers.sql"],
            }
        ],
    }


def test_check_shadowed_state_blocks_despite_select(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The unrefreshable manifest is a project fact: a selection
    # cannot carve an exception out of the shadowed state (and
    # the selection path itself reads the manifest, so the
    # pre-check must land before it).
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert (
        main(
            [
                "check",
                "--project-dir",
                str(tmp_path),
                "--select",
                "stg_customers",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "CANNOT RUN - shadowed models" in captured.out
    assert "dbt parse" not in captured.out
    assert "dbt parse" not in captured.err


def test_check_shadowed_state_blocks_under_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()

    assert main(["check", "--project-dir", str(tmp_path), "--strict"]) == 1
    captured = capsys.readouterr()
    assert "CANNOT RUN - shadowed models" in captured.out
    assert "dbt parse" not in captured.out
    assert "dbt parse" not in captured.err


def test_check_without_a_shadow_falls_through_to_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No generated file on disk, so no pair: the pre-check
    # reports nothing and check runs its normal gate (which
    # fails here on the misplaced generated model - the
    # manifest still points at the hand-written file).
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)

    assert main(["check", "--project-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "Shadowed" not in captured.out
    assert "Inventory coverage: PROBLEMS FOUND" in captured.out


def test_check_works_again_once_the_original_is_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()
    assert main(["check", "--project-dir", str(tmp_path)]) == 1
    capsys.readouterr()

    # The prescribed resolution: the hand-written original goes,
    # dbt parses again, and check runs as before.
    (tmp_path / "models" / "staging" / "stg_customers.sql").unlink()
    generated = tmp_path / "models" / "_generated" / "staging" / "stg_customers.sql"
    manifest = {
        "metadata": {
            "project_name": "sample",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dbt_schema_version": (
                "https://schemas.getdbt.com/dbt/manifest/v12.json"
            ),
        },
        "nodes": {
            "model.sample.stg_customers": {
                "name": "stg_customers",
                "resource_type": "model",
                "package_name": "sample",
                "original_file_path": (
                    "models/_generated/staging/stg_customers.sql"
                ),
                "raw_code": generated.read_text(encoding="utf-8"),
            }
        },
    }
    (tmp_path / "target" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    assert main(["check", "--project-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "Inventory coverage is current.\n"


def test_check_shadow_detection_never_fails_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Metadata that cannot be loaded narrows detection to no
    # pairs (the #156 guarantee): check falls through to its
    # normal path, which surfaces the load error - no shadowed
    # report, no traceback, no crash inside detection.
    _write_shadow_project(tmp_path)
    _write_manifest(tmp_path)
    assert _generate(tmp_path, "--write") == 0
    capsys.readouterr()
    instance = tmp_path / "metadata" / "instances" / "staging_pattern.yml"
    instance.write_text("instances: [unclosed\n", encoding="utf-8")

    assert main(["check", "--project-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "shadowed" not in captured.out
    assert "shadowed" not in captured.err
    assert "Traceback" not in captured.err


def test_check_shadowed_text_folds_past_ten_pairs() -> None:
    pairs = [
        {
            "name": f"model_{index:02d}",
            "generated": f"models/_generated/staging/model_{index:02d}.sql",
            "hand_written": [f"models/model_{index:02d}.sql"],
        }
        for index in range(12)
    ]
    text = render_check_shadowed_text(pairs)
    assert "Shadowed models (12):" in text
    assert "model_09" in text
    assert "model_10" not in text
    assert "… and 2 more (full list: --verbose, or --format json)" in text
    verbose = render_check_shadowed_text(pairs, verbose=True)
    assert "model_10" in verbose


def test_coverage_shadowed_text_folds_past_ten_pairs() -> None:
    pairs = [
        {
            "name": f"model_{index:02d}",
            "generated": f"models/_generated/staging/model_{index:02d}.sql",
            "hand_written": [f"models/model_{index:02d}.sql"],
        }
        for index in range(12)
    ]
    text = render_coverage_shadowed_text(pairs)
    assert "Shadowed models (12):" in text
    assert "model_09" in text
    assert "model_10" not in text
    assert "… and 2 more (full list: --verbose, or --format json)" in text
    verbose = render_coverage_shadowed_text(pairs, verbose=True)
    assert "model_10" in verbose
