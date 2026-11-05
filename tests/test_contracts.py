"""Authoritative generator contracts drive validation.

Before contracts, a misspelled or invented parameter was silently
ignored (the pre-contract validator even carried the comment "no
additionalProperties check"), so authoring errors surfaced - if at
all - as confusing renderer failures. These tests pin the contract
behaviour: unknown parameters are errors naming the accepted set,
required parameters and top-level types are enforced for every
generator, and the architecture document's per-generator parameter
lines are checked against the contracts so docs cannot drift.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from loom.contracts import GENERATOR_CONTRACTS, validate_against_contract
from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance

ROOT = Path(__file__).parents[1]

_HUB_PARAMS = {
    "description": "Customer hub.",
    "owner": "analytics",
    "input_models": ["stg_customers"],
    "business_key": "customer_id",
}


def test_unknown_parameter_is_rejected_and_names_the_accepted_set(
    tmp_path: Path,
) -> None:
    params = {**_HUB_PARAMS, "retension": "persistent"}  # typo
    write_pattern_instance(
        tmp_path, "hub_customer", "hub", params, pattern="current_hub"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    message = str(excinfo.value)
    assert "unknown parameter 'retension'" in message
    assert "retention" in message  # the accepted set is named


def test_wrong_top_level_type_is_rejected(tmp_path: Path) -> None:
    params = {
        "description": "Orders staging.",
        "owner": "analytics",
        "inline_source": {"name": "raw", "table": "orders"},
        "columns": "order_id",  # must be a list
    }
    write_pattern_instance(
        tmp_path, "stg_orders", "staging", params, pattern="raw_staging"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "'columns' must be a list" in str(excinfo.value)


def test_missing_required_parameter_is_rejected(tmp_path: Path) -> None:
    # The model silver reads from, so relationship validation passes
    # and only the contract can fail the second project state.
    write_pattern_instance(
        tmp_path,
        "bronze_customers",
        "bronze",
        {"inline_source": {"name": "raw", "table": "customers"}},
        pattern="medallion_bronze",
    )
    params = {"input_models": ["bronze_customers"], "dedup_key": "customer_id"}
    write_pattern_instance(
        tmp_path, "silver_customers", "silver", params, pattern="medallion_silver"
    )
    validate_project(tmp_path)
    params.pop("dedup_key")
    write_pattern_instance(
        tmp_path, "silver_customers", "silver", params, pattern="medallion_silver"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "'dedup_key' is a required property" in str(excinfo.value)


def test_unknown_key_inside_inline_source_is_rejected(tmp_path: Path) -> None:
    params = {
        "description": "Orders staging.",
        "owner": "analytics",
        "inline_source": {"name": "raw", "tabel": "orders"},
        "columns": [{"source": "id", "alias": "id"}],
    }
    write_pattern_instance(
        tmp_path, "stg_orders", "staging", params, pattern="raw_staging"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "inline_source has unknown key 'tabel'" in str(excinfo.value)


def test_every_generator_has_a_contract() -> None:
    from loom.generator_registry import GENERATOR_REGISTRY

    assert set(GENERATOR_CONTRACTS) == set(GENERATOR_REGISTRY)


def test_contract_check_is_pure_and_complete() -> None:
    # A record carrying every accepted hub parameter validates clean.
    data = {
        "name": "hub_customer",
        "generator": "hub",
        "_paradigm": "dv",
        **_HUB_PARAMS,
        "retention": "persistent",
        "record_source": "crm",
        "tags": ["core"],
    }
    assert validate_against_contract("p", data) == []


def test_source_mappings_require_name_and_table() -> None:
    # Pi-review P2: a source_ref or inline_source without `table`
    # (or without `name`) used to validate clean and generate
    # source('name', 'unknown'). Both keys are required in both
    # mappings; `schema` keeps its real default and stays
    # optional.
    base = {
        "name": "stg_customers",
        "generator": "staging",
        "description": "Clean customer source",
        "owner": "analytics",
        "columns": [{"source": "raw_id", "alias": "customer_id"}],
    }
    for mapping_name in ("source_ref", "inline_source"):
        for missing in ("name", "table"):
            present = {"name": "ecom", "table": "raw_customers"}
            del present[missing]
            errors = validate_against_contract("p", {**base, mapping_name: present})
            assert (
                f"p: {mapping_name}.{missing} is a required property" in errors
            ), (mapping_name, missing, errors)
        complete = {"name": "ecom", "table": "raw_customers"}
        assert validate_against_contract("p", {**base, mapping_name: complete}) == []


_HEADING_TO_GENERATOR = {
    "Staging": "staging",
    "Hubs": "hub",
    "Links": "link",
    "Satellites": "satellite",
    "SCD2 dimensions": "scd2_dimension",
    "Kimball facts": "fact",
    "Kimball dimensions": "dimension",
    "Medallion bronze": "bronze",
    "Medallion silver": "silver",
    "Medallion gold": "gold",
    "OBT (One Big Table)": "obt",
    "3NF normalized tables": "normalized_table",
    "3NF reporting views": "view",
    "Hybrid marts": "mart",
}


def test_architecture_parameter_lines_match_the_contracts() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    sections = re.split(r"^### ", text, flags=re.MULTILINE)[1:]
    checked = 0
    for section in sections:
        heading = section.split("\n", 1)[0].strip()
        generator = _HEADING_TO_GENERATOR.get(heading)
        if generator is None:
            continue
        match = re.search(
            r"Parameters \(checked against the generator contract[^)]*\): "
            r"([^.]+)\. Required: ([^.]+)\.",
            section,
        )
        assert match, f"architecture section {heading!r} lacks a contract line"
        documented = re.findall(r"`([a-z_0-9]+)`", match.group(1))
        documented_required = re.findall(r"`([a-z_0-9]+)`", match.group(2))
        contract = GENERATOR_CONTRACTS[generator]
        assert documented == [p.name for p in contract], heading
        assert documented_required == [
            p.name for p in contract if p.required
        ], heading
        checked += 1
    assert checked == len(_HEADING_TO_GENERATOR)


def test_explicit_null_required_parameters_are_rejected_for_every_generator() -> None:
    # An explicit null is not a supplied value. Every required
    # parameter of every generator contract must reject it.
    for generator, contract in GENERATOR_CONTRACTS.items():
        required = [p for p in contract if p.required]
        data = {
            "name": "x_model",
            "generator": generator,
            **{p.name: None for p in contract},
        }
        errors = validate_against_contract("p", data)
        # Every parameter rejects an explicit null: required ones with
        # the required-value error, optional ones with a type error.
        # (Gold's contract is all-optional; its cross-field requirement
        # lives in the dedicated validators.)
        for param in contract:
            assert any(
                f"'{param.name}'" in error and "null" in error
                for error in errors
            ), (generator, param.name, errors)
        for param in required:
            assert any(
                f"'{param.name}'" in error and "must not be null" in error
                for error in errors
            ), (generator, param.name, errors)


def test_explicit_null_optional_parameter_is_a_type_error() -> None:
    data = {
        "name": "hub_customer",
        "generator": "hub",
        **_HUB_PARAMS,
        "retention": None,
    }
    errors = validate_against_contract("p", data)
    assert any("'retention'" in e and "not null" in e for e in errors)


def test_hub_with_all_required_fields_null_fails_project_validation(
    tmp_path: Path,
) -> None:
    # Regression: the reproduced all-null hub record validated
    # clean before the fix.
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            "description": None,
            "owner": None,
            "input_models": [None],
            "business_key": None,
        },
        pattern="current_hub",
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "must not be null" in str(excinfo.value)


def test_null_inside_source_mapping_is_rejected() -> None:
    data = {
        "name": "stg_orders",
        "generator": "staging",
        "description": "Orders staging.",
        "owner": "analytics",
        "source_ref": {"name": None, "table": "orders"},
        "columns": [{"source": "id", "alias": "id"}],
    }
    errors = validate_against_contract("p", data)
    assert any("source_ref.name must be a string" in e for e in errors)


def test_null_from_pattern_default_is_rejected(tmp_path: Path) -> None:
    # A pattern default that expands to null reaches the same contract
    # check on the expanded record.
    import yaml

    patterns = tmp_path / "metadata" / "patterns"
    instances = tmp_path / "metadata" / "instances"
    patterns.mkdir(parents=True)
    instances.mkdir(parents=True)
    (patterns / "current_hub.yml").write_text(
        yaml.safe_dump(
            {
                "name": "current_hub",
                "generator": "hub",
                "defaults": {"owner": None},
            }
        ),
        encoding="utf-8",
    )
    (instances / "current_hub.yml").write_text(
        yaml.safe_dump(
            {
                "pattern": "current_hub",
                "instances": [
                    {
                        "name": "hub_customer",
                        "params": {
                            "description": "Customer hub.",
                            "input_models": ["stg_customers"],
                            "business_key": "customer_id",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "'owner'" in str(excinfo.value) and "must not be null" in str(
        excinfo.value
    )


# --- Inline-source freshness contract --------------------------------


def _staging_params(inline_source: dict) -> dict:
    return {
        "description": "Orders staging.",
        "owner": "analytics",
        "inline_source": inline_source,
        "columns": [{"source": "id", "alias": "id"}],
    }


def test_valid_freshness_is_accepted(tmp_path: Path) -> None:
    params = _staging_params(
        {
            "name": "raw",
            "table": "orders",
            "loaded_at_field": "updated_at",
            "freshness": {
                "warn_after": {"count": 12, "period": "hour"},
                "error_after": {"count": 1, "period": "day"},
            },
        }
    )
    write_pattern_instance(
        tmp_path, "stg_orders", "staging", params, pattern="raw_staging"
    )
    validate_project(tmp_path)  # must not raise


@pytest.mark.parametrize(
    ("freshness", "fragment"),
    [
        (
            {"warn_after": {"count": 12, "period": "week"}},
            "freshness.warn_after.period must be one of: minute, hour, day",
        ),
        (
            {"warn_after": {"count": 0, "period": "hour"}},
            "freshness.warn_after.count must be a positive integer",
        ),
        (
            {"warn_after": {"count": "12", "period": "hour"}},
            "freshness.warn_after.count must be a positive integer",
        ),
        (
            {"warn_after": {"count": True, "period": "hour"}},
            "freshness.warn_after.count must be a positive integer",
        ),
        (
            {"warn_after": {"count": 12}},
            "freshness.warn_after.period must be one of",
        ),
        (
            {"sometimes": {"count": 12, "period": "hour"}},
            "freshness has unknown key 'sometimes'",
        ),
        ({}, "freshness must declare at least one of"),
        ("daily", "freshness must be a mapping"),
    ],
)
def test_invalid_freshness_is_rejected_with_named_errors(
    tmp_path: Path, freshness: object, fragment: str
) -> None:
    params = _staging_params(
        {"name": "raw", "table": "orders", "freshness": freshness}
    )
    write_pattern_instance(
        tmp_path, "stg_orders", "staging", params, pattern="raw_staging"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert fragment in str(excinfo.value)


def test_freshness_inside_source_ref_is_rejected(tmp_path: Path) -> None:
    params = {
        "description": "Orders staging.",
        "owner": "analytics",
        "source_ref": {
            "name": "raw",
            "table": "orders",
            "freshness": {"warn_after": {"count": 12, "period": "hour"}},
        },
        "columns": [{"source": "id", "alias": "id"}],
    }
    write_pattern_instance(
        tmp_path, "stg_orders", "staging", params, pattern="raw_staging"
    )
    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    assert "source_ref has unknown key 'freshness'" in str(excinfo.value)
