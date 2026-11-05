"""Merge keys in practitioner dbt YAML:
a schema/sources document using anchors + merge keys must
validate, and the merged definitions must take effect in
source resolution - not merely parse."""
from __future__ import annotations

from pathlib import Path

from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance


def _project(tmp_path: Path) -> Path:
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "x",
            "owner": "analytics",
            "source_ref": {"name": "raw", "table": "customer"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="staging",
    )
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    # The source table's very name arrives only via the merge:
    # if merged pairs did not take effect, the table would have
    # no name and the staging source_ref could not resolve.
    (models / "sources.yml").write_text(
        "version: 2\n"
        "x-table-template: &customer_table\n"
        "  name: customer\n"
        "  description: Composed by a merge key.\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - <<: *customer_table\n"
        "        loaded_at_field: updated_at\n",
        encoding="utf-8",
    )
    # The symptom shape: a schema document using anchors and
    # merge keys anywhere under model-paths must not fail the
    # whole validation with a constructor error.
    (models / "schema.yml").write_text(
        "version: 2\n"
        "x-column-defaults: &column_defaults\n"
        "  description: templated column\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    columns:\n"
        "      - <<: *column_defaults\n"
        "        name: customer_id\n",
        encoding="utf-8",
    )
    return tmp_path


def test_merge_key_yaml_validates_and_takes_effect(tmp_path: Path) -> None:
    project = _project(tmp_path)
    entries = validate_project(project)
    assert [entry.data["name"] for entry in entries] == ["stg_customer"]
