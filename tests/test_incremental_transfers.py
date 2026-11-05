"""Incremental link, bronze and normalized_table.

The transfers - the three generators whose guard/key shapes sit
closest to the hub's. Covers, per design §8.3: exact config-string
and filter-fragment assertions per generator × strategy, the
§1.6(b) cross-field rejections, the generate-time dialect gate
(§1.6(c): rejected pre-write, nothing written on gate failure), and
byte-identical output when no block is declared.

The no-block byte pins and strategy-render
shape pins live in the goldens + parse matrix + the
defaults table and gate matrix in test_incremental_core.py);
this file keeps the transfers' unique scenarios.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.cli import main
from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.inventory_records import _validate_record
from loom.renderers import get_renderer
from metadata_helpers import write_pattern_instance

PATH = "metadata/instances/example.yml"


# ------------------------------------------------------------------
# Renderer shapes
# ------------------------------------------------------------------


class TestBronzeRenderer:
    BASE = {
        "name": "bronze_events",
        "inline_source": {"name": "raw", "table": "events"},
    }

    def _render(self, block: dict | None, dialect: str = "duckdb") -> str:
        params = dict(self.BASE)
        if block is not None:
            params["incremental"] = block
        return get_renderer("bronze", dialect).render(
            params, DIALECTS[dialect], None
        )

    def test_append_composite_key_guard_uses_not_exists(self) -> None:
        # Kept deliberately: sole exerciser
        # of bronze's composite-key anti-join branch (where form);
        # the coverage diff reclassified this pin as keep.
        sql = self._render(
            {"strategy": "append", "unique_key": ["event_id", "event_seq"]}
        )
        assert (
            "  WHERE NOT EXISTS (\n"
            "    SELECT 1 FROM {{ this }} AS target\n"
            "    WHERE target.event_id = event_id "
            "AND target.event_seq = event_seq\n"
            "  )\n" in sql
        )


class TestLinkRenderer:
    BASE = {
        "name": "link_order_customer",
        "input_models": ["stg_orders"],
        "endpoints": [
            {"hub": "hub_order", "key": "order_id"},
            {"hub": "hub_customer", "key": "customer_id"},
        ],
    }

    def _render(self, block: dict | None, dialect: str = "duckdb", **extra) -> str:
        params = dict(self.BASE)
        params.update(extra)
        if block is not None:
            params["incremental"] = block
        return get_renderer("link", dialect).render(
            params, DIALECTS[dialect], None
        )


    def test_block_on_non_historised_variant_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="non_historised"):
            self._render(
                {"strategy": "append"},
                variant="non_historised",
                dependent_child_key="order_item_id",
                payload_columns=["quantity"],
            )


class TestNormalizedTableRenderer:
    BASE = {
        "name": "n_customer",
        "input_models": ["bronze_customers"],
        "entity_columns": ["customer_id", "region_id"],
        "attribute_columns": ["country", "updated_seq"],
    }

    def _render(self, block: dict | None, dialect: str = "duckdb") -> str:
        params = dict(self.BASE)
        if block is not None:
            params["incremental"] = block
        return get_renderer("normalized_table", dialect).render(
            params, DIALECTS[dialect], None
        )


    def test_append_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="key strategies only"):
            self._render({"strategy": "append"})


# ------------------------------------------------------------------
# §1.6(b) cross-field validation
# ------------------------------------------------------------------


def _record(generator: str, block: dict, **overrides) -> list[str]:
    base: dict = {
        "name": "example_model",
        "generator": generator,
        "description": "Example.",
        "owner": "test",
    }
    if generator == "link":
        base.update(
            name="link_order_customer",
            input_models=["stg_orders"],
            endpoints=[
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        )
    elif generator == "bronze":
        base.update(
            name="bronze_events",
            inline_source={"name": "raw", "table": "events"},
        )
    elif generator == "normalized_table":
        base.update(
            name="n_customer",
            input_models=["bronze_customers"],
            entity_columns=["customer_id", "region_id"],
            attribute_columns=["country", "updated_seq"],
        )
    base.update(overrides)
    base["incremental"] = block
    return _validate_record(PATH, base, is_registration=False)


def _incremental_errors(generator: str, block: dict, **overrides) -> list[str]:
    return [
        error
        for error in _record(generator, block, **overrides)
        if "incremental" in error
    ]


class TestLinkCrossFieldRules:

    def test_block_rejected_on_non_historised_variant(self) -> None:
        errors = _incremental_errors(
            "link",
            {"strategy": "append"},
            variant="non_historised",
            dependent_child_key="order_item_id",
            payload_columns=["quantity"],
        )
        assert any("non_historised" in e for e in errors)

    def test_cursor_rejected(self) -> None:
        errors = _incremental_errors(
            "link", {"strategy": "merge", "cursor_column": "placed_at"}
        )
        assert any("cursor_column is not supported for link" in e for e in errors)

    def test_lookback_rejected(self) -> None:
        errors = _incremental_errors(
            "link", {"strategy": "merge", "lookback": 3}
        )
        assert any("cursor_column is not supported for link" in e for e in errors)

    def test_unique_key_must_be_the_link_hash(self) -> None:
        errors = _incremental_errors(
            "link", {"strategy": "merge", "unique_key": "order_id"}
        )
        assert any(
            "must be the derived link hash column 'order_customer_hk'"
            in e
            for e in errors
        )


class TestBronzeCrossFieldRules:


    def test_unguarded_append_rejected(self) -> None:
        errors = _incremental_errors("bronze", {"strategy": "append"})
        assert any("requires a guard" in e for e in errors)

    def test_append_with_both_guards_rejected(self) -> None:
        errors = _incremental_errors(
            "bronze",
            {
                "strategy": "append",
                "unique_key": "event_id",
                "cursor_column": "event_seq",
            },
        )
        assert any("alternative append guards" in e for e in errors)

    def test___loaded_at_cursor_rejected_by_name(self) -> None:
        errors = _incremental_errors(
            "bronze",
            {"strategy": "append", "cursor_column": "__loaded_at"},
        )
        assert any(
            "must not be '__loaded_at'" in e for e in errors
        )

    def test_key_strategy_requires_a_declared_key(self) -> None:
        errors = _incremental_errors("bronze", {"strategy": "merge"})
        assert any("unique_key is required" in e for e in errors)

    def test_lookback_with_append_rejected(self) -> None:
        errors = _incremental_errors(
            "bronze",
            {
                "strategy": "append",
                "cursor_column": "event_seq",
                "lookback": 1,
            },
        )
        assert any(
            "lookback is not accepted with strategy 'append'" in e
            for e in errors
        )

    def test_lookback_without_cursor_rejected(self) -> None:
        errors = _incremental_errors(
            "bronze",
            {"strategy": "merge", "unique_key": "event_id", "lookback": 1},
        )
        assert any("lookback requires 'cursor_column'" in e for e in errors)


class TestNormalizedTableCrossFieldRules:


    def test_append_rejected(self) -> None:
        errors = _incremental_errors(
            "normalized_table", {"strategy": "append"}
        )
        assert any("key strategies" in e for e in errors)

    def test_key_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "normalized_table",
            {"strategy": "merge", "unique_key": "order_id"},
        )
        assert any("does not emit" in e for e in errors)

    def test_cursor_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "normalized_table",
            {"strategy": "merge", "cursor_column": "placed_at"},
        )
        assert any("is not one of the columns" in e for e in errors)

    def test_lookback_without_cursor_rejected(self) -> None:
        errors = _incremental_errors(
            "normalized_table", {"strategy": "merge", "lookback": 2}
        )
        assert any("lookback requires 'cursor_column'" in e for e in errors)


# ------------------------------------------------------------------
# Generate-time dialect gate (§1.6(c))
# ------------------------------------------------------------------


def _gate_project(tmp_path: Path) -> Path:
    write_pattern_instance(
        tmp_path,
        "bronze_events_merge",
        "bronze",
        {
            "inline_source": {"name": "raw", "table": "events"},
            "incremental": {"strategy": "merge", "unique_key": "event_id"},
        },
        pattern="bronze",
    )
    write_pattern_instance(
        tmp_path,
        "bronze_events_append",
        "bronze",
        {
            "inline_source": {"name": "raw", "table": "events"},
            "incremental": {"strategy": "append", "cursor_column": "event_seq"},
        },
        pattern="bronze",
    )
    return tmp_path


def test_gate_failure_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _gate_project(tmp_path)
    # Validation is dialect-free: the same metadata validates.
    assert main(["validate", "--project-dir", str(project)]) == 0
    result = main(
        ["generate", "--write", "--project-dir", str(project), "--dialect", "clickhouse"]
    )
    assert result == 1
    err = capsys.readouterr().err
    assert "bronze_events_merge" in err
    assert "'merge'" in err and "'clickhouse'" in err
    assert "clickhouse supports: append, delete+insert" in err
    assert "nothing was written" in err
    # The gated model is not written - and neither is the model
    # whose strategy ClickHouse does support: the preflight aborts
    # the whole generation before the plan phase.
    assert not list((project / "models").rglob("*.sql"))


def test_gate_passes_on_a_supporting_dialect(tmp_path: Path) -> None:
    project = _gate_project(tmp_path)
    result = main(
        ["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]
    )
    assert result == 0
    written = {
        path.name for path in (project / "models").rglob("*.sql")
    }
    assert "bronze_events_merge.sql" in written
    assert "bronze_events_append.sql" in written
