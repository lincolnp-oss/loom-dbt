"""Contract-layer validation of the `incremental` block.

The shape validation exists independently of admission: a
generator's contract admits the parameter only in the same change
that teaches its renderer to honour it (contract-admission rule).
Link, bronze and normalized_table admitted it first, then fact
and dimension, then silver; every other generator still rejects the
block as an unknown parameter - the tests assert both halves of
that state.
"""
from __future__ import annotations

import pytest

from loom.contracts import (
    GENERATOR_CONTRACTS,
    _validate_incremental_block,
    accepted_params,
    validate_against_contract,
)

PATH = "metadata/instances/example.yml"


def _errors(block: object) -> list[str]:
    return _validate_incremental_block(PATH, block)


class TestIncrementalBlockShape:
    def test_minimal_valid_block(self) -> None:
        assert _errors({"strategy": "append"}) == []

    def test_full_valid_block(self) -> None:
        block = {
            "strategy": "merge",
            "unique_key": ["order_id", "line_no"],
            "on_schema_change": "sync_all_columns",
            "cursor_column": "placed_at",
            "lookback": 3,
            "lookback_unit": "day",
            "full_refresh": False,
        }
        assert _errors(block) == []

    def test_non_mapping_block(self) -> None:
        errors = _errors("merge")
        assert len(errors) == 1
        assert "must be a mapping" in errors[0]

    def test_unknown_key_names_the_accepted_set(self) -> None:
        errors = _errors({"strategy": "append", "concurrent_batches": True})
        assert any("unknown key 'concurrent_batches'" in e for e in errors)
        assert any("accepted keys are:" in e for e in errors)

    def test_missing_strategy_is_required(self) -> None:
        errors = _errors({"unique_key": "id"})
        assert any("strategy is required" in e for e in errors)

    def test_strategy_must_be_a_string(self) -> None:
        errors = _errors({"strategy": 3})
        assert any("strategy must be a string" in e for e in errors)

    def test_unknown_strategy_names_the_enum(self) -> None:
        errors = _errors({"strategy": "upsert"})
        assert any(
            "must be one of: append, delete+insert, merge" in e
            for e in errors
        )

    def test_microbatch_strategy_is_reserved_by_name(self) -> None:
        errors = _errors({"strategy": "microbatch", "event_time": "placed_at"})
        assert any(
            "'microbatch' is reserved" in e and "fast-follow" in e
            for e in errors
        )

    def test_insert_overwrite_names_the_partition_gap(self) -> None:
        errors = _errors({"strategy": "insert_overwrite"})
        assert any(
            "'insert_overwrite' is not accepted" in e
            and "partition metadata" in e
            for e in errors
        )

    @pytest.mark.parametrize("key", ["event_time", "begin", "batch_size"])
    def test_microbatch_keys_are_reserved_by_name(self, key: str) -> None:
        block: dict = {"strategy": "append", key: "x"}
        errors = _errors(block)
        assert any(
            f"incremental.{key} is reserved" in e for e in errors
        ), errors

    @pytest.mark.parametrize("bad", [42, [], ["a", 1], {"a": 1}])
    def test_unique_key_types(self, bad: object) -> None:
        errors = _errors({"strategy": "merge", "unique_key": bad})
        assert any(
            "unique_key must be a string or a list of strings" in e
            for e in errors
        )

    def test_on_schema_change_enum(self) -> None:
        errors = _errors({"strategy": "merge", "on_schema_change": "merge"})
        assert any(
            "on_schema_change must be one of: ignore, fail, "
            "append_new_columns, sync_all_columns" in e
            for e in errors
        )

    @pytest.mark.parametrize("bad", [True, -1, 1.5, "3"])
    def test_lookback_must_be_a_non_negative_integer(self, bad: object) -> None:
        errors = _errors({"strategy": "append", "lookback": bad})
        assert any("lookback must be an integer >= 0" in e for e in errors)

    def test_lookback_unit_enum(self) -> None:
        errors = _errors(
            {"strategy": "append", "lookback": 1, "lookback_unit": "week"}
        )
        assert any(
            "lookback_unit must be one of: hour, day, month, year" in e
            for e in errors
        )

    def test_lookback_unit_must_be_a_string(self) -> None:
        errors = _errors({"strategy": "append", "lookback_unit": 3})
        assert any("lookback_unit must be a string" in e for e in errors)

    def test_cursor_column_must_be_a_string(self) -> None:
        errors = _errors({"strategy": "append", "cursor_column": 5})
        assert any("cursor_column must be a string" in e for e in errors)

    def test_full_refresh_must_be_a_boolean(self) -> None:
        errors = _errors({"strategy": "append", "full_refresh": "false"})
        assert any("full_refresh must be a boolean" in e for e in errors)


# Generators that admit the block (contract-admission
# rule): the transfers first; then the Kimball pair;
# then silver; then gold. The remaining generators - hub (its own `retention`
# switch), staging, view, scd2_dimension, obt, mart - still
# reject the parameter as unknown.
ADMITTED = {
    "link",
    "bronze",
    "normalized_table",
    "fact",
    "dimension",
    "silver",
    "gold",
}
NOT_ADMITTED = sorted(set(GENERATOR_CONTRACTS) - ADMITTED)


class TestContractAdmission:
    """Admission follows the waves; rejection everywhere else."""

    @pytest.mark.parametrize("generator", sorted(ADMITTED))
    def test_admitted_generators_accept_the_parameter(
        self, generator: str
    ) -> None:
        assert "incremental" in accepted_params(generator)
        data = {
            "generator": generator,
            "incremental": {"strategy": "merge", "unique_key": "id"},
        }
        errors = validate_against_contract(PATH, data)
        assert not any(
            "unknown parameter 'incremental'" in e for e in errors
        ), errors

    @pytest.mark.parametrize("generator", NOT_ADMITTED)
    def test_incremental_not_in_other_contracts(self, generator: str) -> None:
        assert "incremental" not in accepted_params(generator)

    @pytest.mark.parametrize("generator", NOT_ADMITTED)
    def test_block_rejected_as_unknown_parameter(self, generator: str) -> None:
        data = {
            "generator": generator,
            "incremental": {"strategy": "merge", "unique_key": "id"},
        }
        errors = validate_against_contract(PATH, data)
        assert any(
            "unknown parameter 'incremental'" in e for e in errors
        ), errors

    def test_block_shape_errors_surface_alongside_admission(self) -> None:
        # The unknown-parameter error fires (no contract admits the
        # block), and the §1.6(a) errors still name the precise
        # problem with the block itself.
        data = {
            "generator": "hub",
            "incremental": {"strategy": "microbatch", "event_time": "ts"},
        }
        errors = validate_against_contract(PATH, data)
        assert any("unknown parameter 'incremental'" in e for e in errors)
        assert any("'microbatch' is reserved" in e for e in errors)
        assert any("incremental.event_time is reserved" in e for e in errors)
