"""Incremental fact and dimension (the Kimball pair).

Covers, per design §8.3: exact config-string and filter-fragment
assertions per generator × admitted strategy, the §1.6(b)
cross-field rejections (fact's keyless key mode, dimension's
delete+insert/append rejections by name), the generate-time
dialect gate for dimension on ClickHouse (§1.6(c): dimension is
merge-only and ClickHouse has no merge - rejected pre-write,
nothing written), and byte-identical output when no block is
declared.

The no-block byte pins and strategy-render
shape pins live in the goldens + parse matrix + the
defaults table and gate matrix in test_incremental_core.py);
this file keeps the Kimball pair's unique scenarios.
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


class TestFactRenderer:
    BASE = {
        "name": "fct_orders",
        "input_models": ["stg_orders"],
        "dimension_keys": ["order_id", "order_date"],
        "measure_columns": ["quantity", "amount"],
    }

    def _render(
        self, block: dict | None, dialect: str = "duckdb", **extra
    ) -> str:
        params = dict(self.BASE)
        params.update(extra)
        if block is not None:
            params["incremental"] = block
        return get_renderer("fact", dialect).render(
            params, DIALECTS[dialect], None
        )


    def test_key_mode_without_a_key_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="dimension keys are foreign keys"):
            self._render({"strategy": "merge"})

    def test_append_without_a_cursor_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="requires a 'cursor_column'"):
            self._render({"strategy": "append"})

    def test_append_with_a_key_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="not accepted"):
            self._render(
                {
                    "strategy": "append",
                    "unique_key": "order_id",
                    "cursor_column": "order_date",
                }
            )


    def test_accumulating_snapshot_append_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="accumulating_snapshot"):
            self._render(
                {"strategy": "append", "cursor_column": "order_date"},
                variant="accumulating_snapshot",
                dimension_keys=["order_id", "ordered_date", "shipped_date"],
                milestone_keys=["shipped_date"],
            )


class TestDimensionRenderer:
    BASE = {
        "name": "dim_customer",
        "input_models": ["stg_customers"],
        "natural_key": "customer_id",
        "surrogate_key": "customer_sk",
        "attribute_columns": ["customer_name", "updated_seq"],
    }

    def _render(self, block: dict | None, dialect: str = "duckdb") -> str:
        params = dict(self.BASE)
        if block is not None:
            params["incremental"] = block
        return get_renderer("dimension", dialect).render(
            params, DIALECTS[dialect], None
        )


    def test_merge_numbers_new_keys_from_the_target_maximum(self) -> None:
        sql = self._render({"strategy": "merge"})
        assert (
            "{% if is_incremental() %}"
            "(SELECT COALESCE(MAX(customer_sk), 0) FROM {{ this }}) "
            "+ ROW_NUMBER() OVER (ORDER BY customer_id)"
            "{% else %}"
            "ROW_NUMBER() OVER (ORDER BY customer_id)"
            "{% endif %} AS customer_sk" in sql
        )


    def test_delete_insert_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="admits merge only"):
            self._render({"strategy": "delete+insert"})

    def test_append_fails_explicitly(self) -> None:
        with pytest.raises(LoomError, match="admits merge only"):
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
    if generator == "fact":
        base.update(
            name="fct_orders",
            input_models=["stg_orders"],
            dimension_keys=["order_id", "order_date"],
            measure_columns=["quantity", "amount"],
        )
    elif generator == "dimension":
        base.update(
            name="dim_customer",
            input_models=["stg_customers"],
            natural_key="customer_id",
            surrogate_key="customer_sk",
            attribute_columns=["customer_name", "updated_seq"],
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


class TestFactCrossFieldRules:


    def test_keyless_key_mode_rejected_naming_the_reason(self) -> None:
        errors = _incremental_errors("fact", {"strategy": "merge"})
        assert any(
            "dimension keys are foreign keys, not a grain" in e
            for e in errors
        )

    def test_key_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "fact", {"strategy": "merge", "unique_key": "product_id"}
        )
        assert any("does not emit" in e for e in errors)

    def test_cursor_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "fact",
            {
                "strategy": "merge",
                "unique_key": "order_id",
                "cursor_column": "shipped_at",
            },
        )
        assert any("is not one of the columns" in e for e in errors)

    def test_append_requires_a_cursor(self) -> None:
        errors = _incremental_errors("fact", {"strategy": "append"})
        assert any("requires 'cursor_column'" in e for e in errors)

    def test_append_cursor_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "fact", {"strategy": "append", "cursor_column": "shipped_at"}
        )
        assert any("is not one of the columns" in e for e in errors)

    def test_append_with_a_key_rejected(self) -> None:
        errors = _incremental_errors(
            "fact",
            {
                "strategy": "append",
                "unique_key": "order_id",
                "cursor_column": "order_date",
            },
        )
        assert any(
            "unique_key is not accepted with strategy 'append'" in e
            for e in errors
        )

    def test_append_with_lookback_rejected(self) -> None:
        errors = _incremental_errors(
            "fact",
            {
                "strategy": "append",
                "cursor_column": "order_date",
                "lookback": 1,
            },
        )
        assert any(
            "lookback is not accepted with strategy 'append'" in e
            for e in errors
        )

    def test_lookback_without_cursor_rejected(self) -> None:
        errors = _incremental_errors(
            "fact",
            {"strategy": "merge", "unique_key": "order_id", "lookback": 1},
        )
        assert any("lookback requires 'cursor_column'" in e for e in errors)


    def test_accumulating_snapshot_append_rejected_by_name(self) -> None:
        errors = _incremental_errors(
            "fact",
            {"strategy": "append", "cursor_column": "ordered_date"},
            variant="accumulating_snapshot",
            dimension_keys=["order_id", "ordered_date", "shipped_date"],
            milestone_keys=["shipped_date"],
        )
        assert any(
            "'append' is not accepted for the accumulating_snapshot"
            in e
            for e in errors
        )


class TestDimensionCrossFieldRules:


    def test_delete_insert_rejected_by_name(self) -> None:
        errors = _incremental_errors(
            "dimension", {"strategy": "delete+insert"}
        )
        assert any(
            "'delete+insert' is not accepted for dimension" in e
            and "merge" in e
            for e in errors
        )

    def test_append_rejected_by_name(self) -> None:
        errors = _incremental_errors("dimension", {"strategy": "append"})
        assert any(
            "'append' is not accepted for dimension" in e for e in errors
        )

    def test_key_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "dimension", {"strategy": "merge", "unique_key": "order_id"}
        )
        assert any("does not emit" in e for e in errors)

    def test_cursor_outside_the_emitted_set_rejected(self) -> None:
        errors = _incremental_errors(
            "dimension", {"strategy": "merge", "cursor_column": "placed_at"}
        )
        assert any("is not one of the columns" in e for e in errors)

    def test_lookback_without_cursor_rejected(self) -> None:
        errors = _incremental_errors(
            "dimension", {"strategy": "merge", "lookback": 2}
        )
        assert any("lookback requires 'cursor_column'" in e for e in errors)


# ------------------------------------------------------------------
# Generate-time dialect gate (§1.6(c))
# ------------------------------------------------------------------


def _write_kimball_inputs(project: Path) -> None:
    # Fact/dimension relationship validation requires their input
    # models to exist in the inventory as staging instances whose
    # columns cover the declared keys and measures, and staging's
    # source_ref must resolve against a native sources document.
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: orders\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging.",
            "owner": "test",
            "source_ref": {"name": "raw", "table": "customers"},
            "columns": [
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "customer_name", "alias": "customer_name"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "stg_orders",
        "staging",
        {
            "description": "Order staging.",
            "owner": "test",
            "source_ref": {"name": "raw", "table": "orders"},
            "columns": [
                {"source": "order_id", "alias": "order_id"},
                {"source": "amount", "alias": "amount"},
            ],
        },
        pattern="staging",
    )


def test_dimension_merge_on_clickhouse_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_kimball_inputs(tmp_path)
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "dimension",
        {
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_name"],
            "incremental": {"strategy": "merge"},
        },
        pattern="dimension",
    )
    write_pattern_instance(
        tmp_path,
        "fct_orders",
        "fact",
        {
            "input_models": ["stg_orders"],
            "dimension_keys": ["order_id"],
            "measure_columns": ["amount"],
            "incremental": {"strategy": "delete+insert", "unique_key": "order_id"},
        },
        pattern="fact",
    )
    project = tmp_path
    # Validation is dialect-free: the same metadata validates.
    assert main(["validate", "--project-dir", str(project)]) == 0
    result = main(
        ["generate", "--write", "--project-dir", str(project), "--dialect", "clickhouse"]
    )
    assert result == 1
    err = capsys.readouterr().err
    # The gate names the model, the strategy, the dialect and the
    # strategies ClickHouse does support: dimension's merge-only
    # rule leaves it no v1 strategy on this dialect.
    assert "dim_customer" in err
    assert "'merge'" in err and "'clickhouse'" in err
    assert "clickhouse supports: append, delete+insert" in err
    assert "nothing was written" in err
    # The gated model is not written - and neither is the fact
    # whose strategy ClickHouse does support: the preflight aborts
    # the whole generation before the plan phase.
    assert not list((project / "models").rglob("*.sql"))


def test_kimball_gate_passes_on_a_supporting_dialect(tmp_path: Path) -> None:
    _write_kimball_inputs(tmp_path)
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "dimension",
        {
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_name"],
            "incremental": {"strategy": "merge"},
        },
        pattern="dimension",
    )
    result = main(
        ["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"]
    )
    assert result == 0
    written = {path.name for path in (tmp_path / "models").rglob("*.sql")}
    assert "dim_customer.sql" in written
