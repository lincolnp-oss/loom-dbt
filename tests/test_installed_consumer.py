"""Optional installed-wheel and dbt-DuckDB release smoke test."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import venv
import zipfile
from pathlib import Path

from quickstart_expectations import QUICKSTART_GENERATED_FILES

import pytest

from metadata_helpers import write_pattern_instance


def _check_package_layout(paths: set[str]) -> None:
    assert {
        "loom/source_structure.py",
        "loom/generator_registry.py",
        "loom/migration_plan.py",
    } <= paths
    for name in (
        "catalog",
        "kind_registry",
        "schemas",
        "unit_tests",
        "source_discovery",
        "generators",
        "migration",
        "json_schemas",
    ):
        assert f"loom/{name}" not in paths
        assert f"loom/{name}.py" not in paths
        assert f"loom/{name}.pyc" not in paths
        assert not any(
            path.startswith((f"loom/{name}/", f"loom/__pycache__/{name}."))
            for path in paths
        )


def _run(
    command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, (
        f"{command} failed ({result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _check_quickstart(
    root: Path,
    temporary: Path,
    loom: Path,
    dbt: Path,
    runtime_python: str,
    env: dict[str, str],
) -> None:
    project = temporary / "quickstart"

    def ignore_quickstart(directory: str, names: list[str]) -> list[str]:
        ignored = {
            name
            for name in names
            if name in {"target", "logs", "dbt_packages", ".user.yml"}
            or name.endswith((".duckdb", ".duckdb.wal"))
        }
        if Path(directory).name == "models":
            ignored.update(
                name
                for name in names
                if name
                not in {
                    "sources.yml",
                    "fct_order_totals.sql",
                    "fct_order_totals.yml",
                    "exposures.yml",
                }
            )
        if Path(directory).name == "snapshots":
            ignored.update(names)
        return list(ignored)

    shutil.copytree(
        root / "examples" / "quickstart",
        project,
        ignore=ignore_quickstart,
    )
    authored = {
        path.relative_to(project).as_posix()
        for path in project.rglob("*")
        if path.is_file()
    }
    git_env = env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    _run(["git", "init", "--quiet"], project, git_env)
    _run([str(loom), "validate"], project, env)
    _run([str(loom), "generate", "--write"], project, env)
    generated = QUICKSTART_GENERATED_FILES
    generated_snapshots = {
        "snapshots/vault/dim_customers_scd2.sql",
        "snapshots/vault/dim_customers_scd6.sql",
    }
    model_files = {
        path.relative_to(project).as_posix()
        for path in (project / "models").rglob("*")
        if path.is_file()
    }
    expected_model_files = generated | {
        "models/sources.yml",
        "models/fct_order_totals.sql",
        "models/fct_order_totals.yml",
        "models/exposures.yml",
    }
    assert model_files == expected_model_files, (
        f"unexpected quickstart models: {sorted(model_files ^ expected_model_files)}"
    )
    snapshot_files = {
        path.relative_to(project).as_posix()
        for path in (project / "snapshots").rglob("*")
        if path.is_file()
    }
    assert snapshot_files == generated_snapshots

    # The quickstart's staging models read dbt sources backed by seed
    # tables, and dbt creates no seed-to-model edge through a source, so
    # the seeds must be loaded before the build (the documented
    # quickstart sequence). `dbt build` alone fails on a fresh checkout
    # with "Table with name customers does not exist".
    _run([str(dbt), "seed", "--profiles-dir", ".", "--quiet"], project, env)
    _run([str(dbt), "build", "--profiles-dir", ".", "--quiet"], project, env)
    _run(
        [
            runtime_python,
            "-c",
            "import duckdb; "
            "connection = duckdb.connect('quickstart.duckdb', read_only=True); "
            "assert connection.execute("
            "'select order_id, total_quantity from main.fct_order_totals order by order_id'"
            ").fetchall() == [(1, 3), (2, 3), (3, 1), (4, 2)]; "
            "connection.close()",
        ],
        project,
        env,
    )
    manifest = json.loads((project / "target" / "manifest.json").read_text())
    nodes = list(manifest["nodes"].values())
    # The expected model count is derived, not hard-coded:
    # every .sql file in the expected model file set asserted above
    # is a dbt model, and that set is built from the quickstart's
    # generated and authored artefacts - never from the manifest
    # itself, so a generated model missing from the manifest (or an
    # unexpected extra one) still fails here, while adding or
    # removing a quickstart model needs no edit to this test.
    expected_model_count = sum(
        path.endswith(".sql") for path in expected_model_files
    )
    model_count = sum(node["resource_type"] == "model" for node in nodes)
    assert model_count == expected_model_count, (
        f"manifest has {model_count} models, expected "
        f"{expected_model_count} from the quickstart's model files"
    )
    assert sum(node["resource_type"] == "test" for node in nodes) >= 10
    assert sum(node["resource_type"] == "snapshot" for node in nodes) == 2
    _run([str(loom), "check"], project, env)

    ignored = generated | {"quickstart.duckdb", "target/manifest.json", "logs/dbt.log"}
    result = _run(
        ["git", "check-ignore", "--no-index", *sorted(ignored)], project, git_env
    )
    assert set(result.stdout.splitlines()) == ignored
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], project, git_env
    )
    assert set(untracked.stdout.splitlines()) == authored


@pytest.mark.integration
def test_sdist_rebuilt_wheel_builds_external_dbt_consumer(tmp_path: Path) -> None:
    duckdb = pytest.importorskip(
        "duckdb", reason="requires the integration dependency group"
    )
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the installed-wheel test"
    root = Path(__file__).resolve().parents[1]
    exported_constraints = tmp_path / "locked-dependencies.txt"
    _run(
        [
            uv,
            "export",
            "--frozen",
            "--offline",
            "--group",
            "integration",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(exported_constraints),
        ],
        root,
    )

    sdist_dir = tmp_path / "sdist"
    sdist_dir.mkdir()
    _run(
        [
            uv,
            "build",
            "--sdist",
            "--no-sources",
            "--offline",
            "--out-dir",
            str(sdist_dir),
        ],
        root,
    )
    (sdist,) = sdist_dir.glob("loom_dbt-*.tar.gz")
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.isfile() or member.isdir() for member in members)
        assert all(
            not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            for member in members
        )
        _check_package_layout(
            {
                member.name.partition("/")[2].removeprefix("src/")
                for member in members
                if member.name.partition("/")[2].startswith("src/loom/")
            }
        )
        archive.extractall(extracted)
    (source_root,) = extracted.iterdir()
    wheelhouse = tmp_path / "rebuilt-wheels"
    wheelhouse.mkdir()
    _run(
        [
            uv,
            "build",
            "--wheel",
            "--no-sources",
            "--offline",
            "--out-dir",
            str(wheelhouse),
            str(source_root),
        ],
        tmp_path,
    )
    (wheel,) = wheelhouse.glob("loom_dbt-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        _check_package_layout(set(archive.namelist()))

    cli_dir = tmp_path / "cli"
    runtime_dir = tmp_path / "dbt-runtime"
    venv.EnvBuilder(with_pip=False).create(cli_dir)
    venv.EnvBuilder(with_pip=False).create(runtime_dir)
    scripts = "Scripts" if os.name == "nt" else "bin"
    python = "python.exe" if os.name == "nt" else "python"
    loom = cli_dir / scripts / ("loom.exe" if os.name == "nt" else "loom")
    dbt = runtime_dir / scripts / ("dbt.exe" if os.name == "nt" else "dbt")
    cli_python = str(cli_dir / scripts / python)
    runtime_python = str(runtime_dir / scripts / python)
    offline_install = ["--offline", "--constraints", str(exported_constraints)]
    _run(
        [uv, "pip", "install", *offline_install, "--python", cli_python, str(wheel)],
        tmp_path,
    )
    _run(
        [uv, "pip", "install", *offline_install, "--python", cli_python, "duckdb"],
        tmp_path,
    )
    _run(
        [
            uv,
            "pip",
            "install",
            *offline_install,
            "--python",
            runtime_python,
            "dbt-duckdb",
        ],
        tmp_path,
    )

    project = tmp_path / "consumer"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: consumer\nversion: '1.0.0'\nconfig-version: 2\nprofile: consumer\n"
        "model-paths: ['models']\nsnapshot-paths: ['snapshots']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "consumer:\n  target: local\n  outputs:\n    local:\n      type: duckdb\n"
        "      path: \"{{ env_var('LOOM_CONSUMER_DB_PATH') }}\"\n"
        "      schema: consumer_data\n      threads: 1\n",
        encoding="utf-8",
    )
    (project / "seeds" / "raw_customers.csv").write_text(
        "id,name\n7,Ada\n", encoding="utf-8"
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\nsources:\n"
        "  - name: ecom\n"
        "    schema: consumer_data\n"
        "    tables:\n"
        "      - name: raw_customers\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "source_ref": {
                "name": "ecom",
                "table": "raw_customers",
            },
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "name", "alias": "customer_name"},
            ],
        },
        pattern="raw_staging",
    )
    write_pattern_instance(
        project,
        "dim_customers_scd2",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name"],
        },
        pattern="history_dimension",
    )

    cli_env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        cli_env.pop(name, None)
    cli_env["PYTHONNOUSERSITE"] = "1"
    dbt_env = cli_env | {"LOOM_CONSUMER_DB_PATH": str(project / "consumer.duckdb")}
    assert not workdir.is_relative_to(root)
    _run(
        [
            str(runtime_dir / scripts / python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('loom') is None",
        ],
        workdir,
        cli_env,
    )
    _run(
        [
            cli_python,
            "-c",
            "import pathlib, sys, loom; "
            "assert pathlib.Path(loom.__file__).is_relative_to(pathlib.Path(sys.prefix))",
        ],
        workdir,
        cli_env,
    )
    _check_quickstart(root, tmp_path, loom, dbt, runtime_python, cli_env)

    def loom_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return _run([str(loom), *args, "--project-dir", str(project)], workdir, cli_env)

    def dbt_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                str(dbt),
                *args,
                "--project-dir",
                str(project),
                "--profiles-dir",
                str(project),
                "--no-use-colors",
            ],
            workdir,
            dbt_env,
        )

    loom_cli("validate")
    loom_cli("generate", "--write")
    assert (project / "models" / "_generated/staging" / "stg_customers.sql").is_file()
    assert (project / "models" / "sources.yml").is_file()
    dbt_cli("seed", "--full-refresh")
    dbt_cli("build", "--select", "+dim_customers_scd2")
    manifest = json.loads(
        (project / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    nodes = manifest["nodes"]
    staging = nodes["model.consumer.stg_customers"]
    snapshot = nodes["snapshot.consumer.dim_customers_scd2_history"]
    dimension = nodes["model.consumer.dim_customers_scd2"]
    assert "source.consumer.ecom.raw_customers" in staging["depends_on"]["nodes"]
    assert staging["unique_id"] in snapshot["depends_on"]["nodes"]
    assert snapshot["unique_id"] in dimension["depends_on"]["nodes"]
    loom_cli("check")

    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    (project / ".gitignore").write_text("target/\n", encoding="utf-8")
    inspection = loom_cli(
        "inspect-sources", "--database", "consumer.duckdb", "--source", "ecom",
        "--format", "json"
    )
    assert json.loads(inspection.stdout)["sources"] == [
        {
            "source": "ecom",
            "table": "raw_customers",
            "database": "consumer",
            "schema": "consumer_data",
            "identifier": "raw_customers",
            "mapped_columns": [
                {"column": "id", "models": ["stg_customers"]},
                {"column": "name", "models": ["stg_customers"]},
            ],
            "columns": [
                {
                    "name": "id",
                    "type": "INTEGER",
                    "ordinal": 1,
                    "mapped_by": ["stg_customers"],
                },
                {
                    "name": "name",
                    "type": "VARCHAR",
                    "ordinal": 2,
                    "mapped_by": ["stg_customers"],
                },
            ],
        }
    ]
    loom_cli("snapshot-sources", "--database", "consumer.duckdb", "--write")
    baseline_path = project / "target" / "loom" / "source-structure.json"
    baseline = baseline_path.read_bytes()
    unchanged = loom_cli(
        "diff-sources", "--database", "consumer.duckdb",
        "--format", "json"
    )
    assert json.loads(unchanged.stdout) == {"project": "consumer", "changes": []}
    assert baseline_path.read_bytes() == baseline

    with duckdb.connect(dbt_env["LOOM_CONSUMER_DB_PATH"]) as conn:
        conn.execute("alter table consumer_data.raw_customers add column email varchar")
    changed = loom_cli(
        "diff-sources", "--database", "consumer.duckdb",
        "--format", "json"
    )
    assert json.loads(changed.stdout) == {
        "project": "consumer",
        "changes": [
            {
                "source": "ecom",
                "table": "raw_customers",
                "change": "column_added",
                "column": "email",
                "mapped_by": [],
            }
        ],
    }
    assert baseline_path.read_bytes() == baseline

    with duckdb.connect(dbt_env["LOOM_CONSUMER_DB_PATH"]) as conn:
        conn.execute(
            "update consumer_data.raw_customers set name = 'Adelaide' where id = 7"
        )
    dbt_cli("build", "--select", "+dim_customers_scd2")
    with duckdb.connect(dbt_env["LOOM_CONSUMER_DB_PATH"], read_only=True) as conn:
        assert conn.execute(
            "select customer_name, __is_deleted, __valid_to is null "
            "from consumer_data.dim_customers_scd2 order by __valid_from"
        ).fetchall() == [("Ada", False, False), ("Adelaide", False, True)]

    with duckdb.connect(dbt_env["LOOM_CONSUMER_DB_PATH"]) as conn:
        conn.execute("delete from consumer_data.raw_customers where id = 7")
    dbt_cli("build", "--select", "+dim_customers_scd2")
    with duckdb.connect(dbt_env["LOOM_CONSUMER_DB_PATH"], read_only=True) as conn:
        assert conn.execute(
            "select customer_name, __is_deleted from consumer_data.dim_customers_scd2 "
            "where customer_id = 7 and __valid_to is null"
        ).fetchall() == [("Adelaide", True)]
