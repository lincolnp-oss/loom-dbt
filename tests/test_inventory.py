from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.inventory import (
    check_inventory_coverage,
    search_inventory,
    validate_project,
)
from loom.cli import main
from metadata_helpers import write_model_manifest, write_pattern_instance


def _write_manifest(project: Path, nodes: dict[str, dict[str, str]]) -> Path:
    path = project / "target" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for node in nodes.values():
        if node.get("resource_type") == "model" and node.get("package_name") == "pilot":
            original = f"models/{node['name']}.sql"
            source = project / original
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("select 1\n", encoding="utf-8")
            node.update(original_file_path=original, raw_code="select 1\n")
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "pilot",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                "nodes": nodes,
            }
        ),
        encoding="utf-8",
    )
    return path


def _manifest_node(
    name: str, resource_type: str = "model", package_name: str = "pilot"
) -> dict[str, str]:
    return {
        "name": name,
        "resource_type": resource_type,
        "package_name": package_name,
    }


def test_validate_loads_instances_and_registrations_in_stable_order(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "customer-team",
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "tracked_columns": ["email", "status"],
        },
        pattern="history_dimension",
    )
    registration = tmp_path / "local" / "registrations" / "customer_mart.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: customer_mart\nclassification: mart\n"
        "description: Customer revenue\nowner: customer-team\n"
        "tracked_columns: [revenue]\nkeys: [customer_id]\ndepends_on: [dim_customer]\n",
        encoding="utf-8",
    )

    entries = validate_project(tmp_path)

    assert [entry.data["name"] for entry in entries] == [
        "customer_mart",
        "dim_customer",
    ]
    assert [entry.path for entry in entries] == [
        "local/registrations/customer_mart.yml",
        "metadata/instances/history_dimension.yml#instances[dim_customer]",
    ]


def test_search_supports_all_inventory_filters(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "scd2_dimension",
        {
            "description": "Customer profile history",
            "owner": "customer-team",
            "tags": ["pii", "customer"],
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "tracked_columns": ["email", "status"],
        },
        pattern="history_dimension",
    )
    registration = tmp_path / "local" / "registrations" / "customer_mart.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: customer_mart\nclassification: mart\n"
        "description: Customer revenue\nowner: finance\n"
        "tracked_columns: [revenue]\nkeys: [customer_id]\ndepends_on: [dim_customer]\n",
        encoding="utf-8",
    )

    assert [
        item.data["name"] for item in search_inventory(tmp_path, text="PROFILE")
    ] == ["dim_customer"]
    assert [
        item.data["name"] for item in search_inventory(tmp_path, generator="scd2")
    ] == ["dim_customer"]
    assert [
        item.data["name"] for item in search_inventory(tmp_path, owner="FINANCE")
    ] == ["customer_mart"]
    assert [item.data["name"] for item in search_inventory(tmp_path, tag="pi")] == [
        "dim_customer"
    ]
    assert [
        item.data["name"] for item in search_inventory(tmp_path, column="mail")
    ] == ["dim_customer"]
    assert [
        item.data["name"] for item in search_inventory(tmp_path, key="customer_id")
    ] == [
        "customer_mart",
        "dim_customer",
    ]
    assert [
        item.data["name"]
        for item in search_inventory(tmp_path, dependency="dim_customer")
    ] == ["customer_mart"]


def test_search_filters_by_paradigm(tmp_path: Path) -> None:
    # Create native source YAML for staging models
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "sources.yml").write_text(
        "version: 2\nsources:\n"
        "  - name: feed\n"
        "    tables:\n"
        "      - name: customers\n"
        "        columns:\n"
        "          - name: id\n"
        "      - name: orders\n"
        "        columns:\n"
        "          - name: id\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Staging customers",
            "owner": "analytics",
            "source_ref": {"name": "feed", "table": "customers"},
            "columns": [{"source": "id", "alias": "id"}],
        },
        pattern="raw_staging",
    )
    write_pattern_instance(
        tmp_path,
        "stg_orders",
        "staging",
        {
            "description": "Staging orders",
            "owner": "analytics",
            "source_ref": {"name": "feed", "table": "orders"},
            "columns": [{"source": "id", "alias": "id"}],
        },
        pattern="raw_staging",
    )

    # Staging paradigm: 2 staging entries
    assert set(
        item.data["name"] for item in search_inventory(tmp_path, paradigm="staging")
    ) == {
        "stg_customer",
        "stg_orders",
    }
    # DV paradigm: none in this project
    assert (
        set(item.data["name"] for item in search_inventory(tmp_path, paradigm="dv"))
        == set()
    )
    # Kimball paradigm: none in this project
    assert (
        set(
            item.data["name"] for item in search_inventory(tmp_path, paradigm="kimball")
        )
        == set()
    )
    # Unspecified paradigm: registrations only
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "report.yml").write_text(
        "name: revenue_report\nclassification: mart\n"
        "description: Revenue report\nowner: finance\n",
        encoding="utf-8",
    )
    assert set(
        item.data["name"] for item in search_inventory(tmp_path, paradigm="unspecified")
    ) == {
        "revenue_report",
    }


def test_search_does_not_depend_on_target_dialect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "report.yml").write_text(
        "name: revenue_report\nclassification: mart\n"
        "description: Revenue report\nowner: finance\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles.yml").write_text(
        "pilot:\n  target: local\n  outputs:\n"
        "    local:\n      type: unsupported_adapter\n",
        encoding="utf-8",
    )
    (tmp_path / "dbt_project.yml").write_text(
        "name: pilot\nprofile: pilot\n", encoding="utf-8"
    )

    assert main(["search", "revenue_report", "--project-dir", str(tmp_path)]) == 0
    assert "revenue_report" in capsys.readouterr().out


def test_search_rejects_removed_dialect_option(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["search", "--project-dir", str(tmp_path), "--dialect", "duckdb"])


def test_help_groups_commands_by_task_family(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--help"])
    output = capsys.readouterr().out
    families = [
        output.index(f"  {family}:")
        for family in ("author", "adopt", "verify", "sources")
    ]
    assert families == sorted(families)
    # Every command appears exactly once, under its family.
    stripped = [line.strip() for line in output.splitlines()]
    for name in (
        "validate", "generate", "create-new", "search",
        "register", "convert", "plan",
        "check", "coverage", "prove",
        "inspect-sources", "snapshot-sources", "diff-sources",
    ):
        assert sum(
            1 for line in stripped if line.startswith(f"{name} ")
        ) == 1, name


def test_check_help_describes_the_combined_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # QOL A-3: check's top-level one-liner used to describe only
    # the coverage third of the command ("check dbt model
    # inventory coverage"), making it indistinguishable from
    # coverage on the index page. It now states the combined
    # gate purpose its description documents.
    with pytest.raises(SystemExit, match="0"):
        main(["--help"])

    output = capsys.readouterr().out
    lines = output.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.strip().startswith("check ")
    )
    end = next(
        i
        for i, line in enumerate(lines)
        if i > start and line.strip().startswith("coverage ")
    )
    check_help = " ".join(
        " ".join(lines[start:end]).split()
    )  # the root help wraps long one-liners over several lines
    assert "combined gate" in check_help
    assert "validate the metadata" in check_help
    assert "inventory coverage" in check_help
    assert "macro references" in check_help
    assert "generated files" not in check_help


def test_search_separates_generators_from_descriptive_classifications(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_generated",
        "staging",
        {
            "description": "Generated staging",
            "owner": "analytics",
            "inline_source": {"name": "feed", "table": "customers", "schema": "raw"},
            "columns": [{"source": "id", "alias": "id"}],
        },
        pattern="raw_staging",
    )
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "stg_authored.yml").write_text(
        "name: stg_authored\nclassification: staging\n"
        "description: Authored staging\nowner: analytics\nupstream: raw.customer feed\n",
        encoding="utf-8",
    )
    (registrations / "customer_report.yml").write_text(
        "name: customer_report\nclassification: custom_report\n"
        "description: Customer report\nowner: analytics\n",
        encoding="utf-8",
    )
    entries = {entry.data["name"]: entry for entry in validate_project(tmp_path)}
    assert entries["stg_generated"].is_generated
    assert not entries["stg_authored"].is_generated
    assert "is_generated" not in entries["stg_generated"].as_dict()
    assert "is_generated" not in entries["stg_authored"].as_dict()
    assert entries["stg_generated"].data["generator"] == "staging"
    assert "classification" not in entries["stg_generated"].data
    assert "document_type" not in entries["stg_authored"].data
    assert "generator" not in entries["stg_authored"].data
    assert entries["stg_authored"].data["upstream"] == "raw.customer feed"
    assert [
        entry.data["name"] for entry in search_inventory(tmp_path, generator="stag")
    ] == ["stg_generated"]
    assert [
        entry.data["name"]
        for entry in search_inventory(tmp_path, classification="stag")
    ] == ["stg_authored"]
    assert [
        entry.data["name"]
        for entry in search_inventory(tmp_path, dependency="customer feed")
    ] == ["stg_authored"]
    assert [
        entry.data["name"] for entry in search_inventory(tmp_path, text="custom_report")
    ] == ["customer_report"]
    assert search_inventory(tmp_path, generator="custom_report") == []
    assert (
        main(
            [
                "search",
                "--project-dir",
                str(tmp_path),
                "classification:custom_report",
                "--format",
                "json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in result["results"]] == ["customer_report"]
    assert result["results"][0]["classification"] == "custom_report"
    assert result["results"][0]["generator"] is None
    assert result["results"][0]["kind"] == "model"


@pytest.mark.parametrize("generator,classification", [("hub", "mart"), ("", "")])
def test_search_rejects_both_scoped_filters_before_project_access(
    tmp_path: Path,
    generator: str,
    classification: str,
) -> None:
    with pytest.raises(LoomError, match="mutually exclusive.*generated instances"):
        search_inventory(
            tmp_path / "missing",
            generator=generator,
            classification=classification,
        )


@pytest.mark.parametrize("classification", ["hub", "link", "scd2_dimension"])
def test_generated_classifications_remain_reserved(
    tmp_path: Path,
    classification: str,
) -> None:
    path = tmp_path / "local" / "registrations" / "customer.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"name: customer\nclassification: {classification}\n"
        "description: Customer model\nowner: analytics\n",
        encoding="utf-8",
    )
    with pytest.raises(LoomError, match="classification.*reserved for generated"):
        validate_project(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("classification", []),
    ],
)
def test_registrations_reject_invalid_discriminators(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = tmp_path / "local" / "registrations" / "stg_authored.yml"
    path.parent.mkdir(parents=True)
    document = {
        "name": "stg_authored",
        "classification": "staging",
        "description": "Authored staging",
        "owner": "analytics",
    }
    document[field] = value
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LoomError, match="local/registrations/stg_authored.yml"):
        validate_project(tmp_path)


@pytest.mark.parametrize(
    "arguments",
    [
        ["search", "--generator", "hub", "--classification", "mart"],
    ],
)
def test_cli_rejects_retired_search_flags(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_duplicate_names_are_rejected_across_inventory_roots(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "customer-team",
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "tracked_columns": ["email", "status"],
        },
        pattern="history_dimension",
    )
    registration = tmp_path / "local" / "registrations" / "dim_customer.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: dim_customer\nclassification: mart\n"
        "description: Customer mart\nowner: analytics\n",
        encoding="utf-8",
    )

    with pytest.raises(LoomError, match="duplicate inventory name 'dim_customer'"):
        validate_project(tmp_path)


def test_invalid_instance_name_has_actionable_schema_error(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "Dim-Customer",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "customer-team",
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "tracked_columns": ["email", "status"],
        },
        pattern="history_dimension",
    )

    with pytest.raises(LoomError, match=r"metadata/instances/history_dimension.yml"):
        validate_project(tmp_path)


def test_invalid_yaml_reports_path_and_line(tmp_path: Path) -> None:
    path = tmp_path / "metadata" / "patterns" / "dim_customer.yml"
    path.parent.mkdir(parents=True)
    path.write_text("name: [unterminated\n", encoding="utf-8")

    # The failure is a grouped report - the
    # file is named in the headline and the YAML location rides
    # in the problem line.
    with pytest.raises(
        LoomError, match=r"1 problem in metadata/patterns/dim_customer\.yml"
    ) as excinfo:
        validate_project(tmp_path)
    assert "invalid YAML" in str(excinfo.value)
    assert "(line 2, column 1)" in str(excinfo.value)


def test_duplicate_yaml_mapping_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "metadata" / "patterns" / "dim_customer.yml"
    path.parent.mkdir(parents=True)
    path.write_text("name: dim_customer\nname: dim_other\n", encoding="utf-8")

    with pytest.raises(LoomError, match="duplicate key 'name'"):
        validate_project(tmp_path)


def test_scd2_instance_requires_input_models_key_and_tracked_columns(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "scd2_dimension",
        {"description": "Customer history", "owner": "customer-team"},
        pattern="history_dimension",
    )

    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)

    assert "input_models" in str(error.value)
    assert "business_key" in str(error.value)
    assert "tracked_columns" in str(error.value)


def test_symlink_to_file_outside_project_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "outside.yml"
    external.write_text("name: external", encoding="utf-8")
    metadata = project / "metadata"
    metadata.mkdir()
    link = metadata / "external.yml"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")

    with pytest.raises(LoomError, match="escapes project root"):
        validate_project(project)


def test_module_cli_validate_and_search(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "customer-team",
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "tracked_columns": ["email", "status"],
        },
        pattern="history_dimension",
    )

    assert main(["validate", "--project-dir", str(tmp_path)]) == 0
    # The closing line names what was validated - the
    # entries and the authoring documents carrying them -
    # with the singulars right at one.
    assert (
        "Validated 1 entry in 1 authoring document."
        in capsys.readouterr().out
    )

    assert (
        main(
            [
                "search",
                "--project-dir",
                str(tmp_path),
                "column:email",
                "--format",
                "json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["results"][0]["name"] == "dim_customer"
    assert output["results"][0]["path"] == (
        "metadata/instances/history_dimension.yml#instances[dim_customer]"
    )


def test_cli_validation_failure_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "metadata/invalid.yml"
    path.parent.mkdir()
    path.write_text("not: [valid", encoding="utf-8")

    assert main(["validate", "--project-dir", str(tmp_path)]) == 1
    assert "invalid YAML" in capsys.readouterr().err


def test_coverage_includes_only_project_owned_manifest_models(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customers_scd2",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "customer-team",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name"],
        },
        pattern="history_dimension",
    )
    registration = tmp_path / "local" / "registrations" / "stg_customers.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: stg_customers\nclassification: staging\n"
        "description: Authored staging\nowner: analytics\nupstream: raw_customers\n",
        encoding="utf-8",
    )
    manifest = _write_manifest(
        tmp_path,
        {
            "model.pilot.dim_customers_scd2": _manifest_node("dim_customers_scd2"),
            "model.pilot.stg_customers": _manifest_node("stg_customers"),
            "model.package.pkg_model": _manifest_node("pkg_model", package_name="pkg"),
            "seed.pilot.customers": _manifest_node("customers", "seed"),
            "test.pilot.not_a_model": _manifest_node("not_a_model", "test"),
        },
    )

    coverage = check_inventory_coverage(tmp_path)

    assert coverage.ok
    assert coverage.missing_models == []
    assert coverage.orphan_inventory_entries == []
    assert coverage.duplicate_inventory_entries == {}
    assert coverage.duplicate_manifest_models == {}
    assert check_inventory_coverage(tmp_path, manifest).ok


def test_coverage_reports_missing_and_orphan_inventory_names(tmp_path: Path) -> None:
    registration = tmp_path / "local" / "registrations" / "unmatched.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: unmatched\nclassification: staging\n"
        "description: Authored staging\nowner: analytics\n",
        encoding="utf-8",
    )
    write_model_manifest(tmp_path, ["dim_customers_scd2"], "pilot")

    coverage = check_inventory_coverage(tmp_path)

    assert not coverage.ok
    assert coverage.missing_models == ["dim_customers_scd2"]
    assert coverage.orphan_inventory_entries == ["unmatched"]


def test_coverage_reports_duplicate_inventory_and_manifest_models(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "feed", "schema": "raw", "table": "customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="raw_staging",
    )
    registration = tmp_path / "local" / "registrations" / "stg_customers.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: stg_customers\nclassification: staging\n"
        "description: Authored staging\nowner: analytics\n",
        encoding="utf-8",
    )
    _write_manifest(
        tmp_path,
        {
            "model.pilot.stg_customers": _manifest_node("stg_customers"),
            "model.pilot.stg_customers_copy": _manifest_node("stg_customers"),
        },
    )

    coverage = check_inventory_coverage(tmp_path)

    assert coverage.duplicate_inventory_entries == {
        "stg_customers": [
            "local/registrations/stg_customers.yml",
            "metadata/instances/raw_staging.yml#instances[stg_customers]",
        ]
    }
    assert coverage.duplicate_manifest_models == {
        "stg_customers": [
            "model.pilot.stg_customers",
            "model.pilot.stg_customers_copy",
        ]
    }


def test_check_coverage_cli_returns_failure_for_incomplete_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_model_manifest(tmp_path, ["stg_customers"], "pilot")

    assert (
        main(["coverage", "--project-dir", str(tmp_path), "--format", "json"])
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["missing_models"] == ["stg_customers"]
    assert output["ok"] is False


def test_unreadable_inventory_is_not_reported_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    original = os.scandir

    def scan(path: object):
        if Path(path) == metadata:
            raise PermissionError(13, "injected inaccessible directory", str(metadata))
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert main(["validate", "--project-dir", str(tmp_path)]) == 1
    assert "cannot scan project directory" in capsys.readouterr().err


def test_coverage_reports_versioned_models_distinctly(tmp_path: Path) -> None:
    # Same-name nodes carrying distinct dbt `version` values
    # are one versioned model, not a duplicate-name defect - and the
    # check error must name versions as the cause.
    v1 = _manifest_node("dim_customers")
    v1.update(version=1, latest_version=2)
    v2 = _manifest_node("dim_customers")
    v2.update(version=2, latest_version=2)
    _write_manifest(
        tmp_path,
        {
            "model.pilot.dim_customers.v1": v1,
            "model.pilot.dim_customers.v2": v2,
        },
    )

    coverage = check_inventory_coverage(tmp_path)
    assert coverage.duplicate_manifest_models == {}
    assert coverage.versioned_manifest_models == {
        "dim_customers": ["v1", "v2"]
    }
    assert not coverage.ok
    assert coverage.as_dict()["versioned_manifest_models"] == {
        "dim_customers": ["v1", "v2"]
    }

    from loom.check import check_project

    with pytest.raises(LoomError, match=r"multiple dbt versions \(v1, v2\)"):
        check_project(tmp_path)
    with pytest.raises(LoomError, match="does not support dbt model versions"):
        check_project(tmp_path)


def test_malformed_yaml_halts_before_relationship_cascade(
    tmp_path: Path,
) -> None:
    # Session C's malformed staging.yml produced
    # the real parse error plus a cascade of "not in the inventory"
    # errors from healthy files whose only fault was depending on
    # models the broken file defined. Validation now stops at the
    # load failure: the parse error and the halt note are the whole
    # report.
    write_pattern_instance(
        tmp_path,
        "stg_customers",
        "staging",
        {
            "description": "Customers",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers", "schema": "raw"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="staging",
    )
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            "description": "Customer identities",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )
    staging_instances = tmp_path / "metadata" / "instances" / "staging.yml"
    staging_instances.write_text(
        "pattern: staging\ninstances: [unclosed\n", encoding="utf-8"
    )

    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    message = str(excinfo.value)
    assert "metadata/instances/staging.yml" in message
    assert "invalid YAML" in message
    assert "validation stopped" in message
    assert "not in the inventory" not in message
    assert "hub_customer" not in message


def test_validate_zero_inventory_warns_on_stderr_but_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Validating a project
    # with no Loom metadata used to exit 0 with only a bare
    # count line ("Validated 0 inventory entries.") -
    # indistinguishable from a real pass in the wrong
    # directory. It now warns on stderr; the exit code stays
    # 0. The count line itself now names the entries and the
    # authoring documents carrying them.
    (tmp_path / "dbt_project.yml").write_text(
        "name: empty_project\nversion: '1.0.0'\nconfig-version: 2\n"
        "profile: empty_project\n",
        encoding="utf-8",
    )

    assert main(["validate", "--project-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "Validated 0 entries in 0 authoring documents." in captured.out
    assert (
        f"loom: warning: no Loom metadata found under {tmp_path}"
        in captured.err
    )



class TestStep2NamingPrefixValidation:
        def test_hub_name_must_start_with_hub_prefix(self, tmp_path: Path) -> None:
            _register(tmp_path, "stg_customers", "staging")
            write_pattern_instance(
                tmp_path,
                "customer_hub",
                "hub",
                {
                    "description": "Customer hub",
                    "owner": "analytics",
                    "input_models": ["stg_customers"],
                    "business_key": "customer_id",
                },
                pattern="hub",
            )
            message = _validation_error(tmp_path)
            assert "hub name must start with 'hub_'" in message

        def test_link_endpoints_must_be_hubs(self, tmp_path: Path) -> None:
            _register(tmp_path, "stg_orders", "staging")
            write_pattern_instance(
                tmp_path,
                "link_order_customer",
                "link",
                {
                    "description": "Order-customer link",
                    "owner": "analytics",
                    "input_models": ["stg_orders"],
                    "endpoints": [
                        {"hub": "order", "key": "order_id"},
                        {"hub": "hub_customer", "key": "customer_id"},
                    ],
                },
                pattern="link",
            )
            message = _validation_error(tmp_path)
            assert "endpoints.0.hub must start with 'hub_'" in message



class TestStep2ObtValidation:
        def test_obt_empty_joins_rejected(self, tmp_path: Path) -> None:
            _register(tmp_path, "stg_orders", "staging")
            write_pattern_instance(
                tmp_path,
                "obt_orders",
                "obt",
                {
                    "description": "OBT",
                    "owner": "analytics",
                    "input_models": ["stg_orders"],
                    "joins": [],
                },
                pattern="obt",
            )
            message = _validation_error(tmp_path)
            assert "'joins' must be a non-empty list" in message



_STAGING_DEFINITION = """\
name: staging
generator: staging
"""


_STAGING_INSTANCE = """\
pattern: staging
instances:
  - name: stg_yaml_orders
    params:
      description: Orders staging from a .yaml instance file.
      owner: data-platform
      inline_source:
        name: raw
        table: orders
      columns:
        - source: order_id
          alias: order_id
"""


def test_yaml_extension_metadata_is_loaded(tmp_path: Path) -> None:
    patterns = tmp_path / "metadata" / "patterns"
    instances = tmp_path / "metadata" / "instances"
    patterns.mkdir(parents=True)
    instances.mkdir(parents=True)
    (patterns / "staging.yaml").write_text(_STAGING_DEFINITION, encoding="utf-8")
    (instances / "staging.yaml").write_text(_STAGING_INSTANCE, encoding="utf-8")

    entries = validate_project(tmp_path)

    assert [entry.data["name"] for entry in entries] == ["stg_yaml_orders"]
    assert entries[0].path.startswith("metadata/instances/staging.yaml")

def test_duplicate_model_across_extensions_is_an_error(tmp_path: Path) -> None:
    patterns = tmp_path / "metadata" / "patterns"
    instances = tmp_path / "metadata" / "instances"
    patterns.mkdir(parents=True)
    instances.mkdir(parents=True)
    (patterns / "staging.yml").write_text(_STAGING_DEFINITION, encoding="utf-8")
    (instances / "a.yml").write_text(_STAGING_INSTANCE, encoding="utf-8")
    (instances / "b.yaml").write_text(_STAGING_INSTANCE, encoding="utf-8")

    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "stg_yaml_orders" in str(excinfo.value)

def test_format_version_is_not_read(tmp_path: Path) -> None:
    # Documents carry no format version and the loaders do not
    # read one. A document that still carries the retired field -
    # with any value - loads exactly as if it were absent.
    patterns = tmp_path / "metadata" / "patterns"
    instances = tmp_path / "metadata" / "instances"
    patterns.mkdir(parents=True)
    instances.mkdir(parents=True)
    (patterns / "staging.yml").write_text(
        'format_version: "9.9"\n' + _STAGING_DEFINITION, encoding="utf-8"
    )
    (instances / "staging.yml").write_text(
        'format_version: "9.9"\n' + _STAGING_INSTANCE, encoding="utf-8"
    )

    entries = validate_project(tmp_path)

    assert [entry.data["name"] for entry in entries] == ["stg_yaml_orders"]


# Validation helpers.

def _register(project: Path, name: str, classification: str = "external") -> None:
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True, exist_ok=True)
    (registrations / f"{name}.yml").write_text(
        f"name: {name}\n"
        f"classification: {classification}\n"
        "description: Registered input.\n"
        "owner: analytics\n",
        encoding="utf-8",
    )

def _validation_error(project: Path) -> str:
    with pytest.raises(LoomError) as error:
        validate_project(project)
    return str(error.value)


def test_validate_rejects_staging_column_colliding_with_system_column(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"source": "loaded", "alias": "__loaded_at"}],
        },
        pattern="customer_staging",
    )
    error = _validation_error(tmp_path)
    assert "'__loaded_at' collides with the generated system column" in error


def test_validate_staging_columns_unknown_key_names_accepted_keys(
    tmp_path: Path,
) -> None:
    # QOL D-2: the nested columns-item error used to read only
    # "Additional properties", leaving the practitioner to find
    # the accepted keys elsewhere - the recovery for the rejected
    # from:/to: column form depended on it. The error now names
    # the unknown key(s) and the accepted set, matching this
    # validator's other unknown-key errors.
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"from": "id", "to": "customer_id"}],
        },
        pattern="customer_staging",
    )
    error = _validation_error(tmp_path)
    assert "columns[0] has unknown key(s): from, to" in error
    assert (
        "accepted keys are: alias, expression, macro, macro_args, "
        "macro_column_param, source" in error
    )
    assert "Additional properties" not in error


def test_validate_rejects_satellite_attribute_colliding_with_hash_diff(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "sat_customer",
        "satellite",
        {
            "description": "Customer satellite",
            "owner": "analytics",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customer"],
            "attribute_columns": ["__hash_diff"],
            "record_source": "crm",
        },
        pattern="customer_satellite",
    )
    error = _validation_error(tmp_path)
    assert "'__hash_diff' collides with the generated system column" in error


def test_validate_rejects_scd2_tracked_column_colliding_with_valid_from(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "dim_customers_scd2",
        "scd2_dimension",
        {
            "description": "Customer SCD2 dimension",
            "owner": "analytics",
            "input_models": ["stg_customer"],
            "business_key": "customer_id",
            "tracked_columns": ["__valid_from"],
        },
        pattern="customer_scd2",
    )
    error = _validation_error(tmp_path)
    assert "'__valid_from' collides with the generated system column" in error


def test_validate_rejects_hub_business_key_colliding_with_record_source(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            "description": "Customer identities",
            "owner": "analytics",
            "input_models": ["stg_customer"],
            "business_key": "__record_source",
            "record_source": "crm",
            "retention": "persistent",
        },
        pattern="customer_hub",
    )
    error = _validation_error(tmp_path)
    assert "'__record_source' collides with the generated system column" in error


def test_validate_allows_authored_column_that_is_not_a_system_column(
    tmp_path: Path,
) -> None:
    # __valid_from is a system column of scd2_dimension, not of
    # staging: the collision check is per-generator.
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"source": "valid_from", "alias": "__valid_from"}],
        },
        pattern="customer_staging",
    )
    entries = validate_project(tmp_path)
    assert [entry.data["name"] for entry in entries] == ["stg_customer"]


def test_documents_classify_by_path_without_a_type_field(tmp_path: Path) -> None:
    # No document carries a type field: the directories alone
    # classify - a definition in metadata/patterns/, an instance
    # set in metadata/instances/, a registration in
    # local/registrations/.
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="customer_staging",
    )
    registration = tmp_path / "local" / "registrations" / "authored_model.yml"
    registration.parent.mkdir(parents=True, exist_ok=True)
    registration.write_text(
        "name: authored_model\nclassification: staging\n"
        "description: Authored model\nowner: analytics\n",
        encoding="utf-8",
    )
    entries = {entry.data["name"]: entry for entry in validate_project(tmp_path)}
    assert entries["stg_customer"].is_generated
    assert not entries["authored_model"].is_generated


def test_definition_shaped_file_in_instances_directory_fails_as_instance_set(
    tmp_path: Path,
) -> None:
    # A pattern definition's shape in the instances directory is
    # read as an instance set and fails that role's own checks:
    # classification is by path, so the definition's fields count
    # for nothing there and the role's required fields are missing.
    path = tmp_path / "metadata" / "instances" / "customer_staging.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "name: customer_staging\ngenerator: staging\n",
        encoding="utf-8",
    )
    with pytest.raises(LoomError, match="pattern is required"):
        validate_project(tmp_path)
