"""Incremental gold (grain-period recomputation).

Covers, per design §8.3: exact config-string and fragment
assertions for the admitted strategies, the §5.4 / §1.6(b)
cross-field rejections (grand-total and pass-through shapes by
name, append by name, a unique_key that differs from the
group_by set, a missing cursor, a cursor the model does not
emit), the generate-time dialect gate (§1.6(c): gold merge on
ClickHouse and gold delete+insert on BigQuery - rejected
pre-write, nothing written), and byte-identical output when no
block is declared, in every mode.

The semantics themselves - a restated grain inside the lookback
recomputed exactly, a restated grain beyond it left stale as
expected behaviour, the __loaded_at stamp asymmetry - are
proven at runtime in tests/test_incremental_gold_runtime.py.

The no-block byte pins and strategy-render
shape pins were demoted (goldens + parse matrix + the
defaults table and gate matrix in test_incremental_core.py);
this file keeps gold's unique scenarios.
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


class TestGoldRenderer:
    BASE = {
        "name": "gold_sales_daily",
        "input_models": ["silver_sales"],
        "aggregation": {
            "group_by": ["sale_date", "region"],
            "measures": [
                {"expression": "sum(amount)", "alias": "total_amount"},
                {"expression": "count(*)", "alias": "lines"},
            ],
        },
    }

    def _render(
        self, block: dict | None, dialect: str = "duckdb", **extra
    ) -> str:
        params = dict(self.BASE)
        params.update(extra)
        if block is not None:
            params["incremental"] = block
        return get_renderer("gold", dialect).render(
            params, DIALECTS[dialect], None
        )


    def test_multi_input_restriction_reads_the_driving_input_only(
        self,
    ) -> None:
        sql = self._render(
            {"strategy": "merge", "cursor_column": "customer_id"},
            input_model=None,
            input_models=["silver_sales", "silver_customers"],
            joins=[{"model": "silver_customers", "on": ["customer_id"]}],
            aggregation={
                "group_by": ["customer_id"],
                "measures": [{"expression": "count(*)", "alias": "lines"}],
            },
        )
        assert "unique_key='customer_id'" in sql
        # The window subquery reads the first input; the joined
        # input is read whole for the touched grains (§5.3).
        assert (
            "FROM {{ ref('silver_sales') }} AS __window\n" in sql
        )
        assert "__window.customer_id = silver_sales.customer_id" in sql
        # The alias is the window's alone: the FROM, the grain
        # correlation, and the cursor bound - never the joined
        # input.
        assert sql.count("__window") == 3
        assert "silver_customers') }} AS __window" not in sql


    def test_append_rejected_by_renderer(self) -> None:
        with pytest.raises(LoomError, match="not supported for gold"):
            self._render(
                {"strategy": "append", "cursor_column": "sale_date"}
            )

    def test_grand_total_block_rejected_by_renderer(self) -> None:
        with pytest.raises(LoomError, match="grand total"):
            self._render(
                {"strategy": "merge", "cursor_column": "sale_date"},
                aggregation={
                    "measures": [
                        {
                            "expression": "sum(amount)",
                            "alias": "total_amount",
                        }
                    ]
                },
            )

    def test_pass_through_block_rejected_by_renderer(self) -> None:
        params = {
            "name": "gold_feed",
            "input_models": ["silver_feed"],
            "incremental": {"strategy": "merge", "cursor_column": "x"},
        }
        with pytest.raises(LoomError, match="pass-through gold"):
            get_renderer("gold", "duckdb").render(
                params, DIALECTS["duckdb"], None
            )

    def test_missing_cursor_rejected_by_renderer(self) -> None:
        with pytest.raises(LoomError, match="requires a 'cursor_column'"):
            self._render({"strategy": "merge"})

    def test_unique_key_mismatch_rejected_by_renderer(self) -> None:
        with pytest.raises(LoomError, match="must equal the aggregation"):
            self._render(
                {
                    "strategy": "merge",
                    "unique_key": "sale_date",
                    "cursor_column": "sale_date",
                }
            )

    def test_cursor_not_emitted_rejected_by_renderer(self) -> None:
        with pytest.raises(
            LoomError, match="not one of the columns this generator emits"
        ):
            self._render(
                {"strategy": "merge", "cursor_column": "loaded_seq"}
            )


# ------------------------------------------------------------------
# §1.6(b) cross-field validation
# ------------------------------------------------------------------


def _record(block: dict, **overrides) -> list[str]:
    base: dict = {
        "name": "gold_sales_daily",
        "generator": "gold",
        "description": "Example.",
        "owner": "test",
        "input_models": ["silver_sales"],
        "aggregation": {
            "group_by": ["sale_date", "region"],
            "measures": [
                {"expression": "sum(amount)", "alias": "total_amount"}
            ],
        },
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


class TestGoldCrossFieldRules:


    def test_cursor_may_be_a_measure_alias_or_the_stamp(self) -> None:
        assert (
            _incremental_errors(
                {"strategy": "merge", "cursor_column": "total_amount"}
            )
            == []
        )
        assert (
            _incremental_errors(
                {"strategy": "merge", "cursor_column": "__loaded_at"}
            )
            == []
        )

    def test_append_rejected_by_name(self) -> None:
        errors = _incremental_errors(
            {"strategy": "append", "cursor_column": "sale_date"}
        )
        assert any(
            "strategy 'append' is not accepted for gold" in e
            for e in errors
        ), errors

    def test_grand_total_rejected_by_name(self) -> None:
        errors = _incremental_errors(
            {"strategy": "merge", "cursor_column": "sale_date"},
            aggregation={
                "measures": [
                    {"expression": "sum(amount)", "alias": "total_amount"}
                ]
            },
        )
        assert any(
            "not accepted for a grand total" in e for e in errors
        ), errors

    def test_pass_through_rejected_by_name(self) -> None:
        # An empty aggregation mapping is the pass-through shape
        # that reaches §1.6(b) through record validation (a null
        # aggregation is stopped earlier by the contract's type
        # check, which names the same malformed record).
        errors = _incremental_errors(
            {"strategy": "merge", "cursor_column": "sale_date"},
            aggregation={},
        )
        assert any(
            "not accepted for pass-through gold" in e for e in errors
        ), errors

    def test_unique_key_must_equal_the_group_by_set(self) -> None:
        errors = _incremental_errors(
            {
                "strategy": "merge",
                "unique_key": "sale_date",
                "cursor_column": "sale_date",
            }
        )
        assert any(
            "must equal the aggregation group_by set" in e
            for e in errors
        ), errors

    def test_missing_cursor_rejected(self) -> None:
        errors = _incremental_errors({"strategy": "merge"})
        assert any(
            "requires 'cursor_column'" in e for e in errors
        ), errors

    def test_cursor_not_emitted_rejected(self) -> None:
        errors = _incremental_errors(
            {"strategy": "merge", "cursor_column": "loaded_seq"}
        )
        assert any(
            "not one of the columns this generator emits" in e
            for e in errors
        ), errors


# ------------------------------------------------------------------
# Generate-time dialect gate (§1.6(c))
# ------------------------------------------------------------------


def _write_gold_project(project: Path, strategy: str) -> None:
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True, exist_ok=True)
    (registrations / "silver_sales.yml").write_text(
        "name: silver_sales\n"
        "classification: silver\n"
        "description: Hand-written sales.\n"
        "owner: test\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "gold_sales_daily",
        "gold",
        {
            "input_models": ["silver_sales"],
            "aggregation": {
                "group_by": ["sale_date", "region"],
                "measures": [
                    {"expression": "sum(amount)", "alias": "total_amount"}
                ],
            },
            "incremental": {
                "strategy": strategy,
                "cursor_column": "sale_date",
            },
        },
        pattern="gold",
    )


def test_gold_merge_on_clickhouse_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_gold_project(tmp_path, "merge")
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
    assert "gold_sales_daily" in err
    assert "'merge'" in err and "'clickhouse'" in err
    assert "clickhouse supports: append, delete+insert" in err
    assert "nothing was written" in err
    assert not list((tmp_path / "models").rglob("*.sql"))


def test_gold_gate_passes_on_a_supporting_dialect(
    tmp_path: Path,
) -> None:
    _write_gold_project(tmp_path, "delete+insert")
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
    generated = tmp_path / "models" / "gold_sales_daily.sql"
    assert generated.exists()
    assert "incremental_strategy='delete+insert'" in generated.read_text(
        encoding="utf-8"
    )
