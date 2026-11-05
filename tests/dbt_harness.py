"""Shared dbt subprocess harness for the runtime test modules.

The runtime-proof modules each carried a copy
of the same plumbing - dbt discovery, the subprocess runner with
the telemetry opt-out env, and the DuckDB profile scaffold. The
copies were identical in semantics (and near-identical in text);
they live here now so the telemetry rule and the failure-message
shape each have exactly one home.

Everything here is stateless: helpers take the dbt executable and
the project directory as arguments and keep no module-level state,
so the harness is safe under pytest-xdist workers.

What deliberately stays in the individual modules: each test's
project content, metadata, seeds, and every assertion. A few
modules whose runner genuinely differs keep
their own runner and adopt only `dbt_env` or nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def dbt_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a dbt subprocess: the current
    environment, plus any per-call extras. The harness sets no
    variables of its own (the shipped tree imposes no
    dbt telemetry opt-outs; a caller's shell may export what it
    likes, and that passes through untouched).
    """
    env = dict(os.environ)
    if extra:
        env.update(extra)
    return env


def dbt_or_skip() -> str:
    """The dbt executable on PATH, or skip the calling test."""
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")
    return dbt


def run_dbt_result(
    dbt: str, project: Path, command: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run one dbt command in `project` and return the result.

    The canonical invocation shape shared by the runtime modules:
    `--profiles-dir.` (the scaffolded project carries its own
    profiles.yml), the project directory as cwd, output captured.
    """
    return subprocess.run(
        [dbt, command, "--profiles-dir", ".", *args],
        cwd=project,
        env=dbt_env(),
        text=True,
        capture_output=True,
        check=False,
    )


def run_dbt(dbt: str, project: Path, command: str, *args: str) -> None:
    """Run one dbt command, asserting success with full output."""
    result = run_dbt_result(dbt, project, command, *args)
    assert result.returncode == 0, (
        f"dbt {command} failed ({result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def duckdb_profiles_yml(
    profile_name: str, db_filename: str, *, target: str = "local"
) -> str:
    """The canonical local DuckDB profiles.yml for a scaffolded
    runtime project."""
    return (
        f"{profile_name}:\n"
        f"  target: {target}\n"
        "  outputs:\n"
        f"    {target}:\n"
        "      type: duckdb\n"
        f"      path: {db_filename}\n"
        "      schema: main\n"
        "      threads: 1\n"
    )
