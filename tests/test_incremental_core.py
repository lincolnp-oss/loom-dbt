"""Unit tests for the shared incremental core.

The core exists and the hub renders through it, but no
generator admits an `incremental` block yet - these tests exercise
the core's API directly, including the generate-time dialect gate.

This file also carries the consolidated pins as
two loop tests -- TestDialectGate (the strategy x dialect
support matrix) and TestIncrementalDefaults (per-generator
default key/cursor facts).
"""
from __future__ import annotations

import pytest

from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.renderers import get_renderer
from loom.renderers.base import IncrementalSpec


def _renderer(dialect_name: str):
    return get_renderer("hub", dialect_name)


class TestIncrementalSpec:
    def test_no_block_resolves_to_none(self) -> None:
        assert _renderer("duckdb")._incremental_spec({}) is None

    def test_full_block_resolution(self) -> None:
        spec = _renderer("duckdb")._incremental_spec(
            {
                "incremental": {
                    "strategy": "merge",
                    "unique_key": ["order_id", "line_no"],
                    "cursor_column": "placed_at",
                    "lookback": 3,
                    "lookback_unit": "day",
                    "full_refresh": False,
                }
            }
        )
        assert spec == IncrementalSpec(
            strategy="merge",
            unique_key=("order_id", "line_no"),
            on_schema_change="fail",
            cursor_column="placed_at",
            lookback=3,
            lookback_unit="day",
            full_refresh=False,
        )

    def test_string_unique_key_becomes_singleton(self) -> None:
        spec = _renderer("duckdb")._incremental_spec(
            {"incremental": {"strategy": "delete+insert", "unique_key": "id"}}
        )
        assert spec is not None
        assert spec.unique_key == ("id",)

    def test_default_key_supplies_the_grain(self) -> None:
        spec = _renderer("duckdb")._incremental_spec(
            {"incremental": {"strategy": "merge"}},
            default_key=("customer_hk",),
        )
        assert spec is not None
        assert spec.unique_key == ("customer_hk",)

    def test_declared_key_beats_default_key(self) -> None:
        spec = _renderer("duckdb")._incremental_spec(
            {"incremental": {"strategy": "merge", "unique_key": "other"}},
            default_key=("customer_hk",),
        )
        assert spec is not None
        assert spec.unique_key == ("other",)


class TestIncrementalConfig:

    def test_append_without_key_or_policy(self) -> None:
        spec = IncrementalSpec(strategy="append", on_schema_change=None)
        assert _renderer("duckdb")._incremental_config(spec) == (
            "{{ config(materialized='incremental', "
            "incremental_strategy='append') }}"
        )


    def test_full_refresh_lock_rendered(self) -> None:
        # Kept deliberately: this direct pin
        # is the sole exerciser of the full_refresh config branch
        # in base.py (the wave files' pass-through pins were
        # demoted; the coverage diff reclassified this one).
        spec = IncrementalSpec(strategy="append", full_refresh=False)
        config = _renderer("duckdb")._incremental_config(spec)
        assert "full_refresh=false" in config

    def test_merge_exclude_columns(self) -> None:
        spec = IncrementalSpec(
            strategy="merge",
            unique_key=("customer_hk",),
            on_schema_change=None,
        )
        config = _renderer("databricks")._incremental_config(
            spec, merge_exclude=("__record_source", "__loaded_at")
        )
        assert config == (
            "{{ config(materialized='incremental', "
            "unique_key='customer_hk', incremental_strategy='merge', "
            "merge_exclude_columns=['__record_source', '__loaded_at']) }}"
        )


class TestAntiJoinWhereForm:
    """Queries with no WHERE of their own (bronze) open the
    clause instead of continuing one; the default AND form - the
    hub's golden shape - is unchanged."""

    def test_single_key_where_form(self) -> None:
        fragment = _renderer("duckdb")._anti_join_filter(
            "event_id", "event_id", where=True
        )
        assert fragment == (
            "\n{% if is_incremental() %}\n"
            "  WHERE event_id NOT IN (\n"
            "    SELECT target.event_id FROM {{ this }} AS target\n"
            "  )\n"
            "{% endif %}"
        )

    def test_multi_key_where_form(self) -> None:
        fragment = _renderer("duckdb")._anti_join_filter_multi(
            [("event_id", "event_id")], where=True
        )
        assert fragment.startswith(
            "\n{% if is_incremental() %}\n  WHERE NOT EXISTS ("
        )


class TestCursorPredicate:
    def test_no_lookback(self) -> None:
        spec = IncrementalSpec(strategy="append", cursor_column="placed_at")
        assert _renderer("duckdb")._cursor_predicate(spec) == (
            "\n{% if is_incremental() %}\n"
            "  WHERE placed_at >= "
            "(SELECT MAX(placed_at) FROM {{ this }})\n"
            "{% endif %}"
        )

    def test_source_alias_prefixes_the_column(self) -> None:
        spec = IncrementalSpec(strategy="append", cursor_column="placed_at")
        fragment = _renderer("duckdb")._cursor_predicate(
            spec, source_alias="s"
        )
        assert "WHERE s.placed_at >= (SELECT MAX(placed_at)" in fragment

    def test_numeric_lookback_without_unit(self) -> None:
        spec = IncrementalSpec(
            strategy="append", cursor_column="batch_seq", lookback=7
        )
        fragment = _renderer("duckdb")._cursor_predicate(spec)
        assert ">= (SELECT MAX(batch_seq) FROM {{ this }}) - 7" in fragment

    def test_date_lookback_interval_family(self) -> None:
        spec = IncrementalSpec(
            strategy="append",
            cursor_column="placed_at",
            lookback=3,
            lookback_unit="day",
        )
        fragment = _renderer("duckdb")._cursor_predicate(spec)
        assert (
            ">= (SELECT MAX(placed_at) FROM {{ this }}) "
            "- INTERVAL '3' DAY" in fragment
        )

    def test_date_lookback_dateadd_family(self) -> None:
        spec = IncrementalSpec(
            strategy="append",
            cursor_column="placed_at",
            lookback=3,
            lookback_unit="day",
        )
        fragment = _renderer("snowflake")._cursor_predicate(spec)
        assert (
            ">= DATEADD(DAY, -3, (SELECT MAX(placed_at) FROM {{ this }}))"
            in fragment
        )

    def test_date_lookback_clickhouse(self) -> None:
        spec = IncrementalSpec(
            strategy="append",
            cursor_column="placed_at",
            lookback=3,
            lookback_unit="day",
        )
        fragment = _renderer("clickhouse")._cursor_predicate(spec)
        assert (
            ">= date_add((SELECT MAX(placed_at) FROM {{ this }}), "
            "INTERVAL -3 DAY)" in fragment
        )

    def test_missing_cursor_column_fails_explicitly(self) -> None:
        spec = IncrementalSpec(strategy="append")
        with pytest.raises(LoomError, match="cursor_column"):
            _renderer("duckdb")._cursor_predicate(spec)

    def test_strict_form_is_the_append_window(self) -> None:
        # Bronze append: strictly
        # after the target maximum, so a rerun never re-appends the
        # boundary batch an append cannot reconcile.
        spec = IncrementalSpec(strategy="append", cursor_column="event_seq")
        assert _renderer("duckdb")._cursor_predicate(spec, strict=True) == (
            "\n{% if is_incremental() %}\n"
            "  WHERE event_seq > "
            "(SELECT MAX(event_seq) FROM {{ this }})\n"
            "{% endif %}"
        )

    def test_strict_form_rejects_a_lookback(self) -> None:
        spec = IncrementalSpec(
            strategy="append", cursor_column="event_seq", lookback=1
        )
        with pytest.raises(LoomError, match="strict"):
            _renderer("duckdb")._cursor_predicate(spec, strict=True)


class TestDialectGate:
    """One gate matrix over (strategy x dialect support).

    The per-generator gate repetitions in
    the generator files (``*_rejected_on_clickhouse``,
    ``append_rejected_on_bigquery``, ...) are folded into this single
    matrix over the admitted strategies. Support is a
    dialect-record fact, so the expected table is hardcoded here:
    flipping a dialect's strategy support fails this test (and
    ``test_dialects.py``'s capabilities pin). Per-generator gate
    *invocation* stays proven by the wave files' CLI
    writes-nothing tests and the parse matrix.
    """

    SUPPORTED = {
        "duckdb": {"append", "delete+insert", "merge"},
        "postgres": {"append", "delete+insert", "merge"},
        "snowflake": {"append", "delete+insert", "merge"},
        "redshift": {"append", "delete+insert", "merge"},
        "databricks": {"append", "delete+insert", "merge"},
        "azuresynapse": {"append", "delete+insert", "merge"},
        "clickhouse": {"append", "delete+insert"},
        "bigquery": {"merge"},
    }
    SUPPORT_TEXT = {
        "clickhouse": "append, delete+insert",
        "bigquery": "merge",
    }
    STRATEGIES = ("append", "delete+insert", "merge")

    def test_gate_matrix_covers_every_dialect(self) -> None:
        assert set(self.SUPPORTED) == set(DIALECTS)

    def test_rejected_pairs_raise(self) -> None:
        failures: list[str] = []
        for dialect_name, supported in self.SUPPORTED.items():
            for strategy in self.STRATEGIES:
                if strategy in supported:
                    continue
                spec = IncrementalSpec(
                    strategy=strategy, unique_key=("id",)
                )
                try:
                    _renderer(dialect_name)._check_incremental_strategy(
                        spec,
                        DIALECTS[dialect_name],
                        model_name="fact_orders",
                    )
                    failures.append(
                        f"{strategy} on {dialect_name} did not raise"
                    )
                except LoomError as exc:
                    message = str(exc)
                    expected = (
                        f"{dialect_name} supports: "
                        f"{self.SUPPORT_TEXT[dialect_name]}"
                    )
                    if expected not in message:
                        failures.append(
                            f"{strategy} on {dialect_name}: message "
                            f"{message!r} lacks {expected!r}"
                        )
        assert not failures, failures

    def test_supported_pairs_pass(self) -> None:
        failures: list[str] = []
        for dialect_name, supported in self.SUPPORTED.items():
            for strategy in sorted(supported):
                spec = IncrementalSpec(
                    strategy=strategy, unique_key=("id",)
                )
                try:
                    _renderer(dialect_name)._check_incremental_strategy(
                        spec, DIALECTS[dialect_name], model_name="m"
                    )
                except LoomError as exc:
                    failures.append(
                        f"{strategy} on {dialect_name} raised: {exc}"
                    )
        assert not failures, failures


class TestIncrementalDefaults:
    """Per-generator default key/cursor facts.

    The per-generator strategy-render pins were consolidated on the basis
    that the machinery is shared (pinned in this file and the
    parse matrix) and the only per-generator facts are the
    defaults below. Each case renders the generator's wave
    instance with a bare strategy block and asserts the resolved
    config facts, so flipping a generator's default key or
    cursor fails here.
    """

    CASES = [
        # (generator, params, block, expected fragments, forbidden)
        (
            "bronze",
            {
                "name": "bronze_events",
                "inline_source": {"name": "raw", "table": "events"},
            },
            {"strategy": "merge"},
            ["incremental_strategy='merge'"],
            # Bronze has no default key: key strategies require a
            # declared unique_key (validation-layer rule).
            ["unique_key"],
        ),
        (
            "link",
            {
                "name": "link_order_customer",
                "input_models": ["stg_orders"],
                "endpoints": [
                    {"hub": "hub_order", "key": "order_id"},
                    {"hub": "hub_customer", "key": "customer_id"},
                ],
            },
            {"strategy": "merge"},
            ["unique_key='order_customer_hk'"],
            [],
        ),
        (
            "normalized_table",
            {
                "name": "n_customer",
                "input_models": ["bronze_customers"],
                "entity_columns": ["customer_id", "region_id"],
                "attribute_columns": ["country", "updated_seq"],
            },
            {"strategy": "merge"},
            # The default key is the first entity column.
            ["unique_key='customer_id'"],
            [],
        ),
        (
            "dimension",
            {
                "name": "dim_customer",
                "input_models": ["stg_customers"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["customer_name", "updated_seq"],
            },
            {"strategy": "merge"},
            [
                "unique_key='customer_id'",
                "merge_exclude_columns=['customer_sk']",
            ],
            [],
        ),
        (
            "silver",
            {
                "name": "silver_orders",
                "input_models": ["bronze_orders"],
                "dedup_key": "order_id",
                "columns": ["order_id", "status"],
            },
            {"strategy": "merge"},
            [
                # Key defaults to the dedup_key; the cursor to
                # __loaded_at, the stamp the dedup orders by.
                "unique_key='order_id'",
                "WHERE __loaded_at >= "
                "(SELECT MAX(__loaded_at) FROM {{ this }})",
            ],
            [],
        ),
        (
            "gold",
            {
                "name": "gold_sales_daily",
                "input_models": ["silver_sales"],
                "aggregation": {
                    "group_by": ["sale_date", "region"],
                    "measures": [
                        {
                            "expression": "sum(amount)",
                            "alias": "total_amount",
                        },
                    ],
                },
            },
            {"strategy": "delete+insert", "cursor_column": "sale_date"},
            # The default key is the aggregation grain.
            ["unique_key=['sale_date', 'region']"],
            [],
        ),
    ]

    def test_generator_default_keys_and_cursors(self) -> None:
        failures: list[str] = []
        for generator, params, block, expected, forbidden in self.CASES:
            merged = dict(params)
            merged["incremental"] = block
            sql = get_renderer(generator, "duckdb").render(
                merged, DIALECTS["duckdb"], None
            )
            for fragment in expected:
                if fragment not in sql:
                    failures.append(
                        f"{generator}: expected {fragment!r} in config"
                    )
            for fragment in forbidden:
                if fragment in sql:
                    failures.append(
                        f"{generator}: forbidden {fragment!r} present"
                    )
        assert not failures, failures
