"""Incremental silver (key-scoped recomputation).

Covers, per design §8.3: exact config-string and fragment
assertions for the admitted strategies, the §1.6(b) cross-field
rejections (append by name, block without a dedup_key, a
unique_key that differs from the dedup_key), the generate-time
dialect gate (§1.6(c): silver merge on ClickHouse - rejected
pre-write, nothing written), and byte-identical output when no
block is declared.

The semantics themselves - a late-arriving older copy must not
evict the held winner, the winner's __loaded_at is carried
never restamped, delete-blindness - are proven at runtime in
tests/test_incremental_silver_runtime.py.

The no-block byte pins and strategy-render
shape pins live in the goldens + parse matrix + the
defaults table and gate matrix in test_incremental_core.py);
this file keeps silver's unique scenarios.
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


class TestSilverRenderer:
    BASE = {
        "name": "silver_orders",
        "input_models": ["bronze_orders"],
        "dedup_key": "order_id",
        "columns": ["order_id", "status"],
    }

    def _render(
        self, block: dict | None, dialect: str = "duckdb", **extra
    ) -> str:
        params = dict(self.BASE)
        params.update(extra)
        if block is not None:
            params["incremental"] = block
        return get_renderer("silver", dialect).render(
            params, DIALECTS[dialect], None
        )


    def test_declared_dedup_order_preserved_verbatim(self) -> None:
        # Q61-5: no tie-break is invented - a declared dedup_order
        # is the dedup's ordering in the incremental branch too.
        sql = self._render(
            {"strategy": "merge"},
            dedup_order="updated_at DESC, row_seq ASC",
        )
        assert (
            "ROW_NUMBER() OVER (PARTITION BY order_id "
            "ORDER BY updated_at DESC, row_seq ASC) AS rn" in sql
        )


    def test_append_rejected_by_renderer(self) -> None:
        with pytest.raises(LoomError, match="not supported for silver"):
            self._render({"strategy": "append"})

    def test_block_without_dedup_key_rejected_by_renderer(self) -> None:
        params = {
            "name": "silver_feed",
            "input_models": ["bronze_feed"],
            "incremental": {"strategy": "merge"},
        }
        with pytest.raises(LoomError, match="requires a 'dedup_key'"):
            get_renderer("silver", "duckdb").render(
                params, DIALECTS["duckdb"], None
            )

    def test_unique_key_mismatch_rejected_by_renderer(self) -> None:
        with pytest.raises(LoomError, match="must equal the dedup_key"):
            self._render(
                {"strategy": "merge", "unique_key": "customer_id"}
            )


# ------------------------------------------------------------------
# §1.6(b) cross-field validation
# ------------------------------------------------------------------


def _record(block: dict, **overrides) -> list[str]:
    base: dict = {
        "name": "silver_orders",
        "generator": "silver",
        "description": "Example.",
        "owner": "test",
        "input_models": ["bronze_orders"],
        "dedup_key": "order_id",
    }
    base.update(overrides)
    base["incremental"] = block
    return _validate_record(PATH, base, is_registration=False)


def _incremental_errors(block: dict, **overrides) -> list[str]:
    return [
        error
        for error in _record(block, **overrides)
        if "incremental" in error
    ]


class TestSilverCrossFieldRules:


    def test_append_rejected_by_name(self) -> None:
        errors = _incremental_errors({"strategy": "append"})
        assert any(
            "strategy 'append' is not accepted for silver" in e
            for e in errors
        ), errors

    def test_unique_key_must_equal_the_dedup_key(self) -> None:
        errors = _incremental_errors(
            {"strategy": "merge", "unique_key": "customer_id"}
        )
        assert any("must equal the dedup_key" in e for e in errors), errors

    def test_composite_unique_key_rejected(self) -> None:
        errors = _incremental_errors(
            {"strategy": "merge", "unique_key": ["order_id", "status"]}
        )
        assert any("must equal the dedup_key" in e for e in errors), errors

    def test_block_without_dedup_key_rejected(self) -> None:
        # An empty dedup_key is the pass-through shape that reaches
        # §1.6(b) through record validation (a missing/null key is
        # stopped earlier by the contract's required check, which
        # names the same problem).
        errors = _incremental_errors({"strategy": "merge"}, dedup_key="")
        assert any("requires a 'dedup_key'" in e for e in errors), errors


# ------------------------------------------------------------------
# Generate-time dialect gate (§1.6(c))
# ------------------------------------------------------------------


def _write_silver_project(project: Path) -> None:
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True, exist_ok=True)
    (registrations / "bronze_orders.yml").write_text(
        "name: bronze_orders\n"
        "classification: bronze\n"
        "description: Hand-written order extracts.\n"
        "owner: test\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "silver_orders",
        "silver",
        {
            "input_models": ["bronze_orders"],
            "dedup_key": "order_id",
            "columns": ["order_id", "status"],
            "incremental": {"strategy": "merge"},
        },
        pattern="silver",
    )


def test_silver_merge_on_clickhouse_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_silver_project(tmp_path)
    # Validation is dialect-free: the same metadata validates.
    assert main(["validate", "--project-dir", str(tmp_path)]) == 0
    result = main(
        [
            "generate",
            "--write", "--project-dir",
            str(tmp_path),
            "--dialect",
            "clickhouse",
        ]
    )
    assert result == 1
    err = capsys.readouterr().err
    assert "silver_orders" in err
    assert "'merge'" in err and "'clickhouse'" in err
    assert "clickhouse supports: append, delete+insert" in err
    assert "nothing was written" in err
    assert not list((tmp_path / "models").rglob("*.sql"))


def test_silver_gate_passes_on_a_supporting_dialect(
    tmp_path: Path,
) -> None:
    _write_silver_project(tmp_path)
    result = main(
        [
            "generate",
            "--write", "--project-dir",
            str(tmp_path),
            "--dialect",
            "duckdb",
        ]
    )
    assert result == 0
    generated = tmp_path / "models" / "silver_orders.sql"
    assert generated.exists()
    assert "incremental_strategy='merge'" in generated.read_text(
        encoding="utf-8"
    )
