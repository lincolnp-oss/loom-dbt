"""Generation must honour dbt_project.yml output paths.

Generation previously wrote to hard-coded roots (models/,
models/_generated/staging, models/_generated/tests, snapshots/) while the
reading side honoured model-paths / snapshot-paths, so a project
with renamed paths got generated files where dbt does not scan.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.cli import main
from loom.errors import LoomError
from loom.project_files import _output_root


def _write_project(
    project: Path, *, paths_block: str = "", model_dir: str = "models"
) -> None:
    (project / model_dir).mkdir(parents=True)
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: configured_paths_test\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: configured_paths_test\n"
        f"{paths_block}",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "configured_paths_test:\n"
        "  target: local\n"
        "  outputs:\n"
        "    local:\n"
        "      type: duckdb\n"
        "      path: test.duckdb\n"
        "      schema: main\n",
        encoding="utf-8",
    )
    (project / model_dir / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    patterns = {
        "staging": (
            "generator: staging\n"
            "required_params: [description, source_ref, columns]\n"
        ),
        "hub": (
            "generator: hub\n"
            "required_params: [description, input_models, business_key]\n"
        ),
        "scd2": (
            "generator: scd2_dimension\n"
            "required_params: [description, input_models, business_key, "
            "tracked_columns]\n"
        ),
    }
    for name, body in patterns.items():
        (project / "metadata" / "patterns" / f"{name}.yml").write_text(
            f"name: {name}\n"
            "defaults: {owner: test}\n"
            f"{body}",
            encoding="utf-8",
        )
    instances = {
        "staging": (
            "pattern: staging\n"
            "instances:\n"
            "  - name: stg_customers\n"
            "    params:\n"
            "      description: Customer staging.\n"
            "      source_ref: {name: raw, table: customers}\n"
            "      columns:\n"
            "        - {source: id, alias: customer_id}\n"
            "        - {source: name, alias: customer_name}\n"
        ),
        "hub": (
            "pattern: hub\n"
            "instances:\n"
            "  - name: hub_customer\n"
            "    params:\n"
            "      description: Customer identities.\n"
            "      input_models: [stg_customers]\n"
            "      business_key: customer_id\n"
        ),
        "scd2": (
            "pattern: scd2\n"
            "instances:\n"
            "  - name: dim_customers_scd2\n"
            "    params:\n"
            "      description: Customer history.\n"
            "      input_models: [stg_customers]\n"
            "      business_key: customer_id\n"
            "      tracked_columns: [customer_name]\n"
        ),
    }
    for name, body in instances.items():
        (project / "metadata" / "instances" / f"{name}.yml").write_text(
            f"{body}",
            encoding="utf-8",
        )


def test_generate_writes_under_configured_paths(tmp_path: Path) -> None:
    _write_project(
        tmp_path,
        paths_block="model-paths: ['dbt_models']\nsnapshot-paths: ['dbt_snapshots']\n",
        model_dir="dbt_models",
    )

    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 0

    # Models, staging sources and generated test YAML live under the
    # configured model root; the SCD2 snapshot under the snapshot root.
    assert (tmp_path / "dbt_models" / "_generated/staging" / "stg_customers.sql").is_file()
    assert (tmp_path / "dbt_models" / "hub_customer.sql").is_file()
    assert (tmp_path / "dbt_models" / "dim_customers_scd2.sql").is_file()
    assert (tmp_path / "dbt_models" / "_generated/tests" / "data_hub_customer.yml").is_file()
    assert (tmp_path / "dbt_snapshots" / "dim_customers_scd2.sql").is_file()
    # Nothing is generated under the default roots.
    assert not (tmp_path / "models" / "hub_customer.sql").exists()
    assert not (tmp_path / "snapshots").exists()


def test_generate_uses_first_configured_path(tmp_path: Path) -> None:
    _write_project(
        tmp_path,
        paths_block="model-paths: ['dbt_models', 'also_scanned']\n",
        model_dir="dbt_models",
    )

    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 0
    assert (tmp_path / "dbt_models" / "hub_customer.sql").is_file()
    assert not (tmp_path / "also_scanned").exists()


def test_generate_defaults_unchanged_without_paths_config(tmp_path: Path) -> None:
    _write_project(tmp_path)

    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 0
    assert (tmp_path / "models" / "_generated/staging" / "stg_customers.sql").is_file()
    assert (tmp_path / "models" / "hub_customer.sql").is_file()
    assert (tmp_path / "models" / "_generated/tests" / "data_hub_customer.yml").is_file()
    assert (tmp_path / "snapshots" / "dim_customers_scd2.sql").is_file()


def test_generate_rejects_absolute_configured_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An absolute model-path inside the test sandbox: validation can
    # read sources from it, but generation must refuse to write
    # outside the project-relative layout dbt expects.
    elsewhere = tmp_path / "elsewhere"
    _write_project(
        tmp_path,
        paths_block=f"model-paths: ['{elsewhere}']\n",
        model_dir=str(elsewhere),
    )

    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 1
    # Validation already rejects non-project-relative paths before
    # generation; generation's own root resolution refuses them too
    # (see test_output_root_helper). Either way: nothing is written.
    assert "project-relative" in capsys.readouterr().err
    assert not (elsewhere / "hub_customer.sql").exists()


def test_output_root_helper() -> None:
    assert _output_root({}, "model") == "models"
    assert _output_root({}, "snapshot") == "snapshots"
    assert _output_root({"model-paths": ["dbt_models", "other"]}, "model") == (
        "dbt_models"
    )
    # A native absolute path ("/abs" alone is not absolute on Windows).
    absolute = str(Path(Path(__file__).anchor) / "abs")
    with pytest.raises(LoomError):
        _output_root({"model-paths": [absolute]}, "model")
    with pytest.raises(LoomError):
        _output_root({"model-paths": "models"}, "model")
