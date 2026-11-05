"""Unit tests for the shared runtime-test dbt harness.

These run in the default suite: they pin the harness's contract -
the subprocess environment passes through without the harness
imposing dbt telemetry opt-outs (the shipped tree sets
none; the pin below keeps that state self-enforcing), the
canonical argv shape, the profiles shape, and the assert wrapper's
failure message - without invoking dbt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import dbt_harness


def test_dbt_env_does_not_set_telemetry_opt_outs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOME_MARKER", "kept")
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("DBT_SEND_ANONYMOUS_USAGE_STATS", raising=False)
    env = dbt_harness.dbt_env()
    assert "DO_NOT_TRACK" not in env
    assert "DBT_SEND_ANONYMOUS_USAGE_STATS" not in env
    assert env["SOME_MARKER"] == "kept"


def test_dbt_env_extra_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dbt_harness.dbt_env({"EXTRA_FLAG": "on"})
    assert env["EXTRA_FLAG"] == "on"


def test_run_dbt_result_uses_canonical_argv_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(dbt_harness.subprocess, "run", fake_run)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("DBT_SEND_ANONYMOUS_USAGE_STATS", raising=False)
    result = dbt_harness.run_dbt_result("dbt-bin", tmp_path, "build", "--select", "x")
    assert result.returncode == 0
    assert captured["argv"] == [
        "dbt-bin",
        "build",
        "--profiles-dir",
        ".",
        "--select",
        "x",
    ]
    assert captured["kwargs"]["cwd"] == tmp_path
    env = captured["kwargs"]["env"]
    assert "DO_NOT_TRACK" not in env
    assert "DBT_SEND_ANONYMOUS_USAGE_STATS" not in env


def test_run_dbt_assert_message_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 3, stdout="out-text", stderr="err-text")

    monkeypatch.setattr(dbt_harness.subprocess, "run", fake_run)
    with pytest.raises(AssertionError) as excinfo:
        dbt_harness.run_dbt("dbt-bin", tmp_path, "seed")
    message = str(excinfo.value)
    assert "dbt seed failed (3)" in message
    assert "out-text" in message
    assert "err-text" in message


def test_duckdb_profiles_yml_shape() -> None:
    text = dbt_harness.duckdb_profiles_yml("my_profile", "my.duckdb")
    assert text.startswith("my_profile:\n  target: local\n")
    assert "      type: duckdb\n" in text
    assert "      path: my.duckdb\n" in text
    assert "send_anonymous_usage_stats" not in text
    assert "config:" not in text
