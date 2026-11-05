from __future__ import annotations

from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.inventory import check_inventory_coverage
from loom.check import check_project
from metadata_helpers import write_model_manifest, write_pattern_instance


def test_check_requires_manifest(tmp_path: Path) -> None:
    with pytest.raises(LoomError, match="dbt manifest is required"):
        check_project(tmp_path)


def test_check_reports_inventory_coverage(
    tmp_path: Path,
) -> None:
    write_model_manifest(tmp_path, ["stg_customers", "missing_model"])
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

    with pytest.raises(
        LoomError,
        match="project models missing inventory records: missing_model",
    ):
        check_project(tmp_path)


def test_check_and_standalone_coverage_keep_validation_order(tmp_path: Path) -> None:
    path = tmp_path / "local" / "registrations" / "invalid.yml"
    path.parent.mkdir(parents=True)
    path.write_text("name: customer\n", encoding="utf-8")

    with pytest.raises(LoomError, match="local/registrations/invalid.yml"):
        check_project(tmp_path)
    # This previously matched "cannot read dbt
    # manifest", the old wrapper that leaked the raw "[Errno 2]"
    # OSError. A missing manifest now reports "dbt manifest not
    # found" with the remedy (see test_manifest.py).
    with pytest.raises(LoomError, match="dbt manifest not found"):
        check_inventory_coverage(tmp_path)


def _write_satellite_project(tmp_path: Path, variant: str | None) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_customers",
        "staging",
        {
            "description": "Customers",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers", "schema": "raw"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "customer_name", "alias": "customer_name"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            "description": "Customer hub",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )
    params: dict = {
        "description": "Customer satellite",
        "owner": "analytics",
        "parent": {"model": "hub_customer", "key": "customer_id"},
        "input_models": ["stg_customers"],
        "record_source": "crm",
    }
    if variant is None:
        params["attribute_columns"] = ["customer_name"]
    else:
        params["variant"] = variant
    write_pattern_instance(
        tmp_path, "sat_customer_details", "satellite", params, pattern="satellite"
    )


def _fix_staging_manifest_path(tmp_path: Path) -> None:
    # write_model_manifest records every model at models/<name>.sql;
    # staging's expected output path is the _generated/staging directory,
    # and the misplaced-models check would (correctly) flag the fake
    # path before coverage of the satellite view is even reached.
    import json

    manifest_path = tmp_path / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for node in manifest["nodes"].values():
        if node["name"] == "stg_customers":
            node["original_file_path"] = "models/_generated/staging/stg_customers.sql"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # The helper's flat staging file must go: the manifest reader
    # requires every model file on disk to be a manifest node, and
    # the node now points at the _generated/staging path.
    (tmp_path / "models" / "stg_customers.sql").unlink()


def test_check_covers_the_standard_satellite_current_view(
    tmp_path: Path,
) -> None:
    # The <name>_current view is an auxiliary output of the
    # standard satellite's own inventory record, not a project model
    # missing a record.
    _write_satellite_project(tmp_path, None)
    # The manifest reader requires each node's file to exist and be
    # unchanged since the manifest was written, so the staging file
    # must be created before the manifest, not patched in afterwards.
    staging_file = tmp_path / "models" / "_generated/staging" / "stg_customers.sql"
    staging_file.parent.mkdir(parents=True, exist_ok=True)
    staging_file.write_text("select 1\n", encoding="utf-8")
    write_model_manifest(
        tmp_path,
        [
            "stg_customers",
            "hub_customer",
            "sat_customer_details",
            "sat_customer_details_current",
        ],
    )
    _fix_staging_manifest_path(tmp_path)
    assert check_project(tmp_path) == []


def test_check_does_not_cover_a_current_view_without_a_standard_satellite(
    tmp_path: Path,
) -> None:
    _write_satellite_project(tmp_path, "record_tracking")
    staging_file = tmp_path / "models" / "_generated/staging" / "stg_customers.sql"
    staging_file.parent.mkdir(parents=True, exist_ok=True)
    staging_file.write_text("select 1\n", encoding="utf-8")
    write_model_manifest(
        tmp_path,
        [
            "stg_customers",
            "hub_customer",
            "sat_customer_details",
            "sat_customer_details_current",
        ],
    )
    _fix_staging_manifest_path(tmp_path)
    with pytest.raises(
        LoomError,
        match="missing inventory records: sat_customer_details_current",
    ):
        check_project(tmp_path)


def test_unrelated_binary_file_does_not_fail_check(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    write_model_manifest(tmp_path, [])
    assert check_project(tmp_path) == []
