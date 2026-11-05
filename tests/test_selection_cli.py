"""--select / --exclude on the model-scoped commands (plan
7.6): per-command pools, validate's document-granular scoping,
and search running on the selector engine behind the legacy
flags."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.cli import main
from metadata_helpers import write_model_manifest, write_pattern_instance

_FIELDS = {"description": "Customer domain", "owner": "analytics"}


def _write_sources(project: Path) -> None:
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )


def _write_staging(project: Path) -> None:
    _write_sources(project)
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
        },
        pattern="staging",
    )


def _write_hub(
    project: Path,
    name: str,
    *,
    pattern: str = "hubs",
    business_key: str = "customer_id",
    input_models: list[str] | None = None,
) -> None:
    write_pattern_instance(
        project,
        name,
        "hub",
        {
            **_FIELDS,
            "input_models": input_models or ["stg_customers"],
            "business_key": business_key,
        },
        pattern=pattern,
    )


def _write_satellite(project: Path) -> None:
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        {
            **_FIELDS,
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["email", "address"],
            "record_source": "crm",
        },
        pattern="satellites",
    )


def _write_dv_project(project: Path) -> None:
    _write_staging(project)
    _write_hub(project, "hub_customer")
    _write_satellite(project)


# --- validate scoping (7.6) -------------------------------------


def test_validate_scoped_shared_document_is_validated_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_staging(tmp_path)
    _write_hub(tmp_path, "hub_customer")
    # hub_broken shares hub_customer's instance document; its
    # business key is not an output of its staging input, a
    # relationship error that only exists when the document is
    # validated.
    _write_hub(tmp_path, "hub_broken", business_key="ghost_key")

    assert main(["validate", "--project-dir", str(tmp_path)]) == 1
    capsys.readouterr()
    # Selecting the healthy model still validates its whole
    # document, broken sibling included.
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_customer",
            ]
        )
        == 1
    )
    assert "hub_broken" in capsys.readouterr().err


def test_validate_scoped_excludes_unrelated_broken_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_staging(tmp_path)
    _write_hub(tmp_path, "hub_customer")
    _write_hub(
        tmp_path,
        "hub_broken",
        pattern="broken_hubs",
        business_key="ghost_key",
    )

    assert main(["validate", "--project-dir", str(tmp_path)]) == 1
    capsys.readouterr()
    # The broken model lives in its own document: scoping to
    # hub_customer never deep-validates it.
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_customer",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Scoped validation" in out
    assert "hub_customer" in out


def test_validate_scoped_relationships_resolve_against_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_staging(tmp_path)
    _write_hub(tmp_path, "hub_customer")
    # hub_customer's staging input lives in an excluded
    # document under this selection; the reference resolves
    # against the inventory and validation passes.
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_customer",
            ]
        )
        == 0
    )
    capsys.readouterr()
    # A reference to a model that exists nowhere is a
    # resolvability failure in the scoped run too.
    _write_hub(
        tmp_path,
        "hub_ghost",
        pattern="ghost_hubs",
        input_models=["ghost_model"],
    )
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_ghost",
            ]
        )
        == 1
    )
    assert "ghost_model" in capsys.readouterr().err


def test_validate_scoped_unmanaged_selection_reports_no_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dv_project(tmp_path)
    write_model_manifest(
        tmp_path, ["stg_customers", "hub_customer", "extra_model"]
    )

    # extra_model exists only in the manifest: it has no
    # authoring documents, is named as such, and is not an
    # error.
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "extra_model",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "extra_model" in out
    assert "nothing to validate" in out

    # Mixed selection: the generated model validates, the
    # unmanaged one is reported alongside.
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_customer",
                "--select",
                "extra_model",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["scoped"] is True
    assert payload["selection"]["without_documents"] == ["extra_model"]
    assert payload["selection"]["documents"]
    assert payload["ok"] is True


def test_validate_scoped_unmatched_selector_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dv_project(tmp_path)
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "no_such_model",
            ]
        )
        == 1
    )
    assert "matched no models" in capsys.readouterr().err


def test_validate_scoped_tier2_name_is_not_a_model_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # raw.customers is a tier-2 record (a source) whose
    # identity is its bare name. The scoped-validate pool is
    # the model records only (plan 7.6), so naming it errors
    # as an unmatched model selection - it must not be
    # reported as a selected model with no authoring
    # documents, which is what the all-records pool did.
    _write_dv_project(tmp_path)
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "raw.customers",
            ]
        )
        == 1
    )
    assert "matched no models" in capsys.readouterr().err


def test_validate_scoped_summary_names_entries_and_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The closing line names what was validated - the
    # entries and the authoring documents carrying them -
    # singular and plural alike ("inventory entries" is
    # retired). The document figure counts the documents
    # carrying the validated entries; the run's header
    # separately reports the wider included set (instance
    # documents plus their pattern definitions).
    _write_dv_project(tmp_path)
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_customer",
            ]
        )
        == 0
    )
    assert (
        "Validated 1 entry in 1 authoring document."
        in capsys.readouterr().out
    )
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "stg_customers",
                "--select",
                "hub_customer",
            ]
        )
        == 0
    )
    assert (
        "Validated 2 entries in 2 authoring documents."
        in capsys.readouterr().out
    )


# --- C-3: scoped validate folds long model lists -------------------
#
# A wide selection used to print every matched name on one
# unwrapped line (58 names, ~1,300 characters, on the
# assessment's Cal-ITP run). Past the shared fold limit the
# lists now follow the fold grammar the sibling surfaces
# use: first ten, the folded count, and the way to unfold.
# Short lists keep the historical inline form, and the
# JSON form always carries the full lists.


def test_validate_scoped_no_documents_list_folds_past_ten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = [f"model_{index:02d}" for index in range(12)]
    write_model_manifest(tmp_path, names)
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "is:unmanaged",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "the selection matched 12 model(s), and none of them" in out
    assert "First 10: model_00, model_01" in out
    assert "… and 2 more (full list: --verbose, or --format json)" in out
    assert "model_10" not in out
    assert "model_11" not in out

    # The JSON form is unchanged and complete.
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "is:unmanaged",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selection"]["selected"] == names


def test_validate_scoped_short_list_stays_inline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_model_manifest(tmp_path, ["model_a", "model_b", "model_c"])
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "is:unmanaged",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "matched 3 model(s) (model_a, model_b, model_c), and" in out
    assert "First 10" not in out


def test_validate_scoped_extra_and_without_lists_fold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_staging(tmp_path)
    # Twelve hubs share one instance document: selecting
    # one validates the document whole, so the other eleven
    # are extra models beyond the selection.
    hubs = [f"hub_{letter}" for letter in "abcdefghijkl"]
    for name in hubs:
        _write_hub(tmp_path, name)
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_a",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "for 1 selected model(s) (hub_a)." in out
    assert "also contain 11 model(s) beyond the selection," in out
    assert "First 10: hub_b, hub_c" in out
    assert "… and 1 more (full list: --verbose, or --format json)" in out
    assert "hub_l" not in out

    # Selected models without documents fold the same way:
    # twelve manifest-only models join the selection
    # alongside hub_a, which has a document.
    extras = [f"extra_{index:02d}" for index in range(12)]
    write_model_manifest(
        tmp_path, ["stg_customers", "hub_a", *extras]
    )
    assert (
        main(
            [
                "validate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "hub_a",
                "--select",
                "is:unmanaged",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert (
        "Selected models without authoring documents (nothing "
        "to validate for them):"
    ) in out
    assert "First 10: extra_00, extra_01" in out
    assert "… and 2 more (full list: --verbose, or --format json)" in out
    assert "extra_11" not in out


# --- coverage and check -----------------------------------------


def _write_registration_project(project: Path) -> None:
    registration = project / "local" / "registrations" / "model_a.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: model_a\nclassification: mart\n"
        "description: A registered model\nowner: analytics\n",
        encoding="utf-8",
    )
    write_model_manifest(project, ["model_a", "model_b"])


def test_coverage_scoped_to_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_registration_project(tmp_path)
    # model_b has no inventory record: unscoped coverage fails.
    assert main(["coverage", "--project-dir", str(tmp_path)]) == 1
    capsys.readouterr()
    assert (
        main(
            [
                "coverage",
                "--project-dir",
                str(tmp_path),
                "--select",
                "model_a",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["missing_models"] == []


def test_check_scoped_to_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_registration_project(tmp_path)
    assert main(["check", "--project-dir", str(tmp_path)]) == 1
    capsys.readouterr()
    assert (
        main(
            ["check", "--project-dir", str(tmp_path), "--select", "model_a"]
        )
        == 0
    )
    capsys.readouterr()
    # Scoping to the uncovered model keeps its failure: the
    # scoped verdict describes the selection, not the project.
    assert (
        main(
            ["check", "--project-dir", str(tmp_path), "--select", "model_b"]
        )
        == 1
    )


# --- register and convert ---------------------------------------


def _write_assessment_manifest(project: Path, names: list[str]) -> None:
    """write_model_manifest plus the empty sources/exposures
    sections the migration assessment requires."""
    path = write_model_manifest(project, names)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.setdefault("sources", {})
    document.setdefault("exposures", {})
    path.write_text(json.dumps(document), encoding="utf-8")


def test_register_and_convert_selection_narrows_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_assessment_manifest(tmp_path, ["alpha", "beta"])

    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--select",
                "alpha",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [model["name"] for model in payload["models"]] == ["alpha"]
    assert payload["summary"]["project_models"] == 1
    assert payload["selection"]["matched_models"] == 1

    assert (
        main(
            [
                "convert",
                "--project-dir",
                str(tmp_path),
                "--select",
                "beta",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [model["name"] for model in payload["models"]] == ["beta"]


def test_register_selection_cannot_reach_macros(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_assessment_manifest(tmp_path, ["alpha"])
    # Macros are never selectable into a model pool (7.6): the
    # branch matches nothing register can act on.
    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--select",
                "kind:macro",
            ]
        )
        == 1
    )
    assert "matched no models" in capsys.readouterr().err


# --- prove -------------------------------------------------------


def _equivalence_project(project: Path) -> None:
    def manifest(alpha_sql: str, beta_sql: str, extra: bool) -> str:
        nodes = {
            "model.proj.alpha": {
                "name": "alpha",
                "resource_type": "model",
                "package_name": "proj",
                "compiled_code": alpha_sql,
                "columns": {"id": {}, "name": {}},
                "config": {"materialized": "view"},
                "depends_on": {"nodes": []},
            },
            "model.proj.beta": {
                "name": "beta",
                "resource_type": "model",
                "package_name": "proj",
                "compiled_code": beta_sql,
                "columns": {"id": {}},
                "config": {"materialized": "view"},
                "depends_on": {"nodes": ["model.proj.alpha"]},
            },
        }
        if extra:
            nodes["model.proj.gamma"] = {
                "name": "gamma",
                "resource_type": "model",
                "package_name": "proj",
                "compiled_code": "select 1",
                "columns": {"id": {}},
                "config": {"materialized": "view"},
                "depends_on": {"nodes": []},
            }
        return json.dumps(
            {
                "metadata": {
                    "project_name": "proj",
                    "adapter_type": "duckdb",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                },
                "nodes": nodes,
            }
        )

    directory = project / "local" / "equivalence"
    directory.mkdir(parents=True)
    (directory / "original.manifest.json").write_text(
        manifest("select id, name from t", "select id from t", False),
        encoding="utf-8",
    )
    (directory / "candidate.manifest.json").write_text(
        manifest("select id, name from t", "select id, other from t", True),
        encoding="utf-8",
    )


def test_prove_selection_narrows_subjects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _equivalence_project(tmp_path)
    assert (
        main(
            [
                "prove",
                "--project-dir",
                str(tmp_path),
                "--select",
                "alpha",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [model["name"] for model in payload["models"]] == ["alpha"]
    assert payload["summary"] == {
        "subjects": 1,
        "proven": 1,
        "inconclusive": 0,
    }
    # A graph form traverses the candidate manifest's own
    # graph: beta depends on alpha, so alpha+ reaches it.
    assert (
        main(
            [
                "prove",
                "--project-dir",
                str(tmp_path),
                "--select",
                "alpha+",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [model["name"] for model in payload["models"]] == [
        "alpha",
        "beta",
    ]
    # The candidate-only non-subject is in prove's pool too.
    assert (
        main(
            [
                "prove",
                "--project-dir",
                str(tmp_path),
                "--select",
                "gamma",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["models"] == []
    assert payload["non_subjects"] == ["gamma"]
    # An unknown name is an unmatched-selector error.
    assert (
        main(
            ["prove", "--project-dir", str(tmp_path), "--select", "nope"]
        )
        == 1
    )
    assert "matched no models" in capsys.readouterr().err


# --- search ------------------------------------------------------


def _write_search_project(project: Path) -> None:
    _write_dv_project(project)
    write_model_manifest(
        project,
        ["stg_customers", "hub_customer", "sat_customer_details", "extra_model"],
    )


def test_search_selector_arguments_run_on_the_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_search_project(tmp_path)
    assert (
        main(["search", "hub_customer", "--project-dir", str(tmp_path)]) == 0
    )
    out = capsys.readouterr().out
    assert "hub_customer" in out
    assert "1 match" in out

    # --select on search is the same selectors as the query.
    assert (
        main(
            [
                "search",
                "--project-dir",
                str(tmp_path),
                "--select",
                "generator:hub",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in payload["results"]] == ["hub_customer"]
    assert payload["results"][0]["band"] == "full"


def test_search_legacy_flags_are_retired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_search_project(tmp_path)
    # The legacy nine-flag filter interface is retired (plan
    # 7.1, clean break): every spelling meets the ordinary
    # unknown-option error, and selector arguments are the one
    # interface.
    for flag in (
        "--text",
        "--generator",
        "--classification",
        "--owner",
        "--tag",
        "--column",
        "--key",
        "--dependency",
        "--paradigm",
    ):
        with pytest.raises(SystemExit) as error:
            main(["search", "--project-dir", str(tmp_path), flag, "x"])
        assert error.value.code == 2
    capsys.readouterr()

    # The selector form expresses the same questions.
    assert (
        main(
            ["search", "--project-dir", str(tmp_path), "generator:hub"]
        )
        == 0
    )
    assert "hub_customer" in capsys.readouterr().out


def test_search_banding_is_labelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_search_project(tmp_path)
    assert (
        main(
            [
                "search",
                "key:customer_id,column:address",
                "--project-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    # The satellite matches the full shape; the hub (key only)
    # and the staging model (address column only) are close
    # matches, and the banding is labelled, never silent.
    assert "Shape banding is active" in out
    assert "1 full matches, 2 close matches" in out
    assert "sat_customer_details" in out
    assert "Close matches" in out

    # The identical string under --select takes only the full
    # match (the guardrail at the CLI surface).
    assert (
        main(
            [
                "generate",
                "--project-dir",
                str(tmp_path),
                "--select",
                "key:customer_id,column:address",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    planned = json.dumps(payload)
    assert "sat_customer_details" in planned
    assert "hub_customer" not in planned


def test_search_zero_result_names_the_emptying_criterion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_search_project(tmp_path)
    assert (
        main(
            [
                "search",
                "generator:hub,owner:nobody",
                "--project-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "owner:nobody" in out
    assert "emptied the set" in out
    assert "analytics" in out  # the owner value that does exist
