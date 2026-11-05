"""Generator wiring fails closed.

A spike proved a registered generator with no contract entry
validated and generated with no parameter or relationship checking at
all: `validate_against_contract` returned no errors for a generator it
had no contract for, and `generate` silently skipped entries whose
generator had no renderer. These tests simulate the unwired states by
removing entries from the wiring tables and require explicit failure
at validation / startup instead.
"""
from __future__ import annotations

import pytest

import loom.cli as cli
import loom.contracts as contracts
from loom.contracts import check_generator_wiring, validate_against_contract
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance

_HUB_RECORD = {
    "name": "hub_customer",
    "generator": "hub",
    "description": "Customer hub.",
    "owner": "analytics",
    "input_models": ["stg_customers"],
    "business_key": "customer_id",
}

_HUB_PARAMS = {
    "description": "Customer hub.",
    "owner": "analytics",
    "input_models": ["stg_customers"],
    "business_key": "customer_id",
}


def test_shipped_wiring_is_complete() -> None:
    check_generator_wiring()  # must not raise
    cli._check_renderer_wiring()  # must not raise


def test_missing_contract_fails_the_wiring_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(contracts.GENERATOR_CONTRACTS, "hub")
    with pytest.raises(LoomError, match="generator 'hub' is registered but has no parameter contract"):
        check_generator_wiring()


def test_unregistered_contract_fails_the_wiring_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        contracts.GENERATOR_CONTRACTS, "ghost", contracts.GENERATOR_CONTRACTS["hub"]
    )
    with pytest.raises(LoomError, match="generator 'ghost' has a parameter contract but is not registered"):
        check_generator_wiring()


def test_record_validation_reports_a_missing_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(contracts.GENERATOR_CONTRACTS, "hub")
    errors = validate_against_contract("p", dict(_HUB_RECORD))
    assert any("no parameter contract" in error for error in errors), errors


def test_unregistered_generator_still_reports_unsupported_not_unwired() -> None:
    # The record validator's "generator is not supported" error owns
    # unknown names; the contract layer stays silent for them.
    record = {**_HUB_RECORD, "generator": "no_such_generator"}
    assert validate_against_contract("p", record) == []


def _write_hub_project(tmp_path) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging.",
            "owner": "analytics",
            "inline_source": {"name": "raw", "table": "customers"},
            "columns": [{"source": "id", "alias": "customer_id"}],
        },
        pattern="raw_staging",
    )
    write_pattern_instance(
        tmp_path, "hub_customer", "hub", _HUB_PARAMS, pattern="current_hub"
    )


def test_validate_project_refuses_a_missing_contract(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_hub_project(tmp_path)
    validate_project(tmp_path)  # wired tree: passes
    monkeypatch.delitem(contracts.GENERATOR_CONTRACTS, "hub")
    with pytest.raises(LoomError, match="generator wiring is incomplete"):
        validate_project(tmp_path)


def test_cli_refuses_a_missing_renderer(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _write_hub_project(tmp_path)
    monkeypatch.delitem(cli.RENDERERS, "hub")
    result = cli.main(["validate", "--project-dir", str(tmp_path)])
    assert result == 1
    assert "generator 'hub' is registered but has no renderer" in capsys.readouterr().err


def test_get_renderer_unknown_generator_raises() -> None:
    # Kept deliberately: the factory's explicit
    # failure for an unknown generator is a wiring behaviour, and
    # deleting it with test_renderers.py dropped the raise branch's
    # coverage (the unknown-dialect branch is still covered by the
    # dialect tests).
    with pytest.raises(LoomError, match="unknown generator 'unknown'"):
        get_renderer("unknown", "duckdb")
