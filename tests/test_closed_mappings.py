"""Closed sub-mappings: unknown keys are rejected, never
silently ignored.

The `aggregation` mapping's own keys were the found hole - a
`filter` nested inside it passed validation and the renderer
ignored it, where the same word at the top level is an
unknown parameter. This module pins the closure of
`aggregation` (gold and mart) and of the sibling sub-mappings
the accompanying sweep audited: measure items, `parent`
blocks, bridge's parent, `joins` entries and view
`select_columns` items.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from generator_conformance import CANONICAL_INSTANCES
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.inventory_records import _validate_record
from metadata_helpers import write_pattern_instance

PATH = "metadata/instances/x.yml"


def _record(generator: str) -> dict:
    for name, gen, params in CANONICAL_INSTANCES:
        if gen == generator:
            return {"name": name, "generator": gen, **deepcopy(params)}
    raise AssertionError(f"no canonical instance for {generator}")


def _errors(data: dict) -> list[str]:
    return _validate_record(PATH, data, is_registration=False)


def _assert_fragment(data: dict, fragment: str) -> None:
    errors = _errors(data)
    assert any(fragment in error for error in errors), errors


# --- aggregation (the found hole) ----------------------------


@pytest.mark.parametrize("generator", ["gold", "mart"])
def test_aggregation_unknown_key_rejected(generator: str) -> None:
    data = _record(generator)
    assert _errors(data) == []
    data["aggregation"]["filter"] = "region = 'EU'"
    _assert_fragment(
        data,
        "aggregation has unknown key(s): filter; "
        "accepted keys are: group_by, measures",
    )


@pytest.mark.parametrize("generator", ["gold", "mart"])
def test_aggregation_measures_item_unknown_key_rejected(
    generator: str,
) -> None:
    data = _record(generator)
    data["aggregation"]["measures"][0]["bogus"] = 1
    _assert_fragment(
        data,
        "measures.0 has unknown key(s): bogus; "
        "accepted keys are: alias, column, expression",
    )


# --- parent blocks --------------------------------------------


@pytest.mark.parametrize("generator", ["satellite", "pit"])
def test_parent_block_unknown_key_rejected(generator: str) -> None:
    data = _record(generator)
    assert _errors(data) == []
    data["parent"]["bogus"] = 1
    _assert_fragment(
        data,
        "'parent' has unknown key(s): bogus; accepted keys are: "
        "dependent_child_key, endpoints, key, model",
    )


def test_bridge_parent_unknown_key_rejected() -> None:
    # The bridge parent names its anchor hub only; the
    # satellite family's `key` shape is silently ignored
    # there today.
    data = _record("bridge")
    assert _errors(data) == []
    data["parent"]["key"] = "customer_id"
    _assert_fragment(
        data, "'parent' has unknown key(s): key; accepted keys are: model"
    )


# --- joins entries ---------------------------------------------


def test_obt_joins_entry_unknown_key_rejected() -> None:
    data = _record("obt")
    assert _errors(data) == []
    data["joins"][0]["bogus"] = 1
    _assert_fragment(
        data,
        "joins.0 has unknown key(s): bogus; "
        "accepted keys are: columns, model, on",
    )


@pytest.mark.parametrize("generator", ["gold", "view"])
def test_explicit_joins_entry_unknown_key_rejected(
    generator: str,
) -> None:
    data = _record(generator)
    if generator == "gold":
        # The canonical gold has one input; explicit joins
        # are validated for the multi-input shape.
        data["input_models"] = ["stg_orders", "stg_customers"]
        data["joins"] = [
            {"model": "stg_customers", "on": ["customer_id"]}
        ]
    assert _errors(data) == []
    data["joins"][0]["bogus"] = 1
    _assert_fragment(
        data,
        "joins.0 has unknown key(s): bogus; "
        "accepted keys are: model, on",
    )


# --- view select_columns (the relationship phase) --------------


def test_view_select_columns_unknown_key_rejected(
    tmp_path: Path,
) -> None:
    write_pattern_instance(
        tmp_path,
        "n_customers",
        "normalized_table",
        {
            "description": "x",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "entity_columns": ["customer_id"],
            "attribute_columns": ["customer_name"],
        },
        pattern="normalized_table",
    )
    write_pattern_instance(
        tmp_path,
        "vw_customers",
        "view",
        {
            "description": "x",
            "owner": "analytics",
            "input_models": ["n_customers"],
            "select_columns": [
                {
                    "source": "n_customers.customer_id",
                    "alias": "customer_id",
                    "bogus": 1,
                }
            ],
        },
        pattern="view",
    )
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert (
        "select_columns[0] has unknown key(s): bogus; "
        "accepted keys are: alias, expression, source"
        in str(error.value)
    )
