from __future__ import annotations

import duckdb
import pytest

from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance
from render_execution import compile_sql, materialize, render
from loom.renderers import (
    BronzeRenderer,
    GoldRenderer,
    SilverRenderer,
)


class TestBronzeRenderer:
    """Test BronzeRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> BronzeRenderer:
        return BronzeRenderer(DIALECTS["duckdb"])


    def test_render_bronze_no_tests(self, renderer) -> None:
        params = {
            "name": "bronze_customers",
            "description": "Raw data",
            "inline_source": {"name": "raw", "table": "customers"},
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None


class TestSilverRenderer:
    """Test SilverRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> SilverRenderer:
        return SilverRenderer(DIALECTS["duckdb"])


    def test_render_silver_uses_declared_dedup_order(self, renderer) -> None:
        # A declared dedup_order replaces the default
        # __loaded_at ordering, so a migrated dedup keeps the
        # tie-break that decides which row survives.
        params = {
            "name": "silver_customers",
            "description": "Cleaned customer data",
            "input_models": ["bronze_customers"],
            "dedup_key": "customer_id",
            "dedup_order": "record_updated_at DESC, id ASC",
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert (
            "PARTITION BY customer_id ORDER BY record_updated_at DESC, id ASC"
            in sql
        )
        assert "ORDER BY __loaded_at DESC" not in sql


    def test_render_silver_excludes_rn_via_duckdb_star_exclusion(
        self, renderer
    ) -> None:
        # DuckDB drops the internal rn column with * EXCLUDE.
        params = {
            "name": "silver_customers",
            "description": "Cleaned data",
            "input_models": ["bronze_customers"],
            "dedup_key": "customer_id",
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert "SELECT * EXCLUDE (rn)" in sql
        assert "WHERE rn = 1" in sql


    def test_render_silver_without_star_exclusion_dialect_raises(self) -> None:
        # Postgres has no star-exclusion syntax: without declared
        # columns the renderer refuses rather than leaking rn.
        renderer = SilverRenderer(DIALECTS["postgres"])
        params = {
            "name": "silver_customers",
            "description": "Cleaned data",
            "input_models": ["bronze_customers"],
            "dedup_key": "customer_id",
        }
        with pytest.raises(Exception, match="declare 'columns'"):
            renderer.render(params, renderer.dialect, None)

    def test_render_silver_empty_columns_raises(self, renderer) -> None:
        params = {
            "name": "silver_customers",
            "description": "Cleaned data",
            "input_models": ["bronze_customers"],
            "dedup_key": "customer_id",
            "columns": [],
        }
        with pytest.raises(Exception, match="non-empty list"):
            renderer.render(params, renderer.dialect, None)

    def test_render_silver_generates_tests(self, renderer) -> None:
        params = {
            "name": "silver_customers",
            "description": "Cleaned data",
            "input_models": ["bronze_customers"],
            "dedup_key": "customer_id",
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "not_null" in tests
        assert "unique" in tests
        assert "customer_id" in tests

    def test_render_silver_no_tests_without_dedup_key(self, renderer) -> None:
        params = {
            "name": "silver_customers",
            "description": "Cleaned data",
            "input_models": ["bronze_customers"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None


class TestGoldRenderer:
    """Test GoldRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> GoldRenderer:
        return GoldRenderer(DIALECTS["duckdb"])


    def test_render_gold_multi_input(self, renderer) -> None:
        params = {
            "name": "gold_customer_orders",
            "description": "Customer order aggregates",
            "input_models": ["silver_customers", "silver_orders"],
            "joins": [{"model": "silver_orders", "on": ["customer_id"]}],
            "aggregation": {
                "group_by": ["customer_id"],
                "measures": [
                    {
                        "column": "order_count",
                        "expression": "COUNT(*)",
                        "alias": "order_count",
                    },
                    {
                        "column": "total_amount",
                        "expression": "SUM(total_amount)",
                        "alias": "total_amount",
                    },
                ],
            },
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert "JOIN" in sql
        assert "ref('silver_customers')" in sql
        assert "ref('silver_orders')" in sql
        # Inputs are aliased by model name; the join uses the explicit
        # key and the group-by column is qualified with the first
        # input's alias, so the shared key is never ambiguous.
        assert "AS silver_customers" in sql
        assert "AS silver_orders" in sql
        assert "ON silver_customers.customer_id = silver_orders.customer_id" in sql
        assert "GROUP BY silver_customers.customer_id" in sql

    def test_render_gold_multi_input_joins_are_left_joins(
        self, renderer
    ) -> None:
        # The join type is fixed LEFT across the generators that
        # take `joins` entries (gold, obt, view): a driving row
        # whose key is unmatched in a joined input survives the
        # aggregation with nulls instead of being dropped. Every
        # entry renders LEFT JOIN; no entry renders a plain JOIN.
        params = {
            "name": "gold_customer_orders",
            "description": "Customer order aggregates",
            "input_models": [
                "silver_customers",
                "silver_orders",
                "silver_returns",
            ],
            "joins": [
                {"model": "silver_orders", "on": ["customer_id"]},
                {"model": "silver_returns", "on": ["customer_id"]},
            ],
            "aggregation": {
                "group_by": ["customer_id"],
                "measures": [
                    {
                        "column": "order_count",
                        "expression": "COUNT(*)",
                        "alias": "order_count",
                    },
                ],
            },
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert (
            "LEFT JOIN {{ ref('silver_orders') }} AS silver_orders "
            "ON silver_customers.customer_id = silver_orders.customer_id"
        ) in sql
        assert (
            "LEFT JOIN {{ ref('silver_returns') }} AS silver_returns "
            "ON silver_customers.customer_id = silver_returns.customer_id"
        ) in sql
        assert "\nJOIN " not in sql

    def test_render_gold_multi_input_without_join_keys_raises(
        self, renderer
    ) -> None:
        params = {
            "name": "gold_customer_orders",
            "description": "Customer order aggregates",
            "input_models": ["silver_customers", "silver_orders"],
            "aggregation": {
                "group_by": ["customer_id"],
                "measures": [
                    {"column": "c", "expression": "COUNT(*)", "alias": "c"},
                ],
            },
        }
        with pytest.raises(Exception, match="explicit 'joins' entry"):
            renderer.render(params, renderer.dialect, None)

    def test_render_gold_multi_input_no_group_by_is_grand_total(
        self, renderer
    ) -> None:
        # This combination used to crash with an IndexError.
        params = {
            "name": "gold_totals",
            "description": "Grand totals",
            "input_models": ["silver_customers", "silver_orders"],
            "joins": [{"model": "silver_orders", "on": ["customer_id"]}],
            "aggregation": {
                "measures": [
                    {"column": "c", "expression": "COUNT(*)", "alias": "c"},
                ],
            },
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert "COUNT(*)" in sql
        assert "GROUP BY" not in sql

    def test_render_gold_single_input_no_group_by_is_grand_total(
        self, renderer
    ) -> None:
        # This combination used to emit a trailing "GROUP BY ".
        params = {
            "name": "gold_totals",
            "description": "Grand totals",
            "input_models": ["silver_customers"],
            "aggregation": {
                "measures": [
                    {"column": "c", "expression": "COUNT(*)", "alias": "c"},
                ],
            },
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert "COUNT(*)" in sql
        assert "GROUP BY" not in sql

    def test_render_gold_measure_without_expression_raises(
        self, renderer
    ) -> None:
        params = {
            "name": "gold_bad",
            "description": "Missing expression",
            "input_models": ["silver_customers"],
            "aggregation": {
                "group_by": ["customer_id"],
                "measures": [{"column": "c", "alias": "c"}],
            },
        }
        with pytest.raises(Exception, match="non-empty 'expression'"):
            renderer.render(params, renderer.dialect, None)

    def test_render_gold_pass_through_rejects_multiple_inputs(
        self, renderer
    ) -> None:
        params = {
            "name": "gold_bad",
            "description": "Pass-through with two inputs",
            "input_models": ["silver_customers", "silver_orders"],
        }
        with pytest.raises(Exception, match="exactly one input model"):
            renderer.render(params, renderer.dialect, None)


    def test_render_gold_generates_tests(self, renderer) -> None:
        params = {
            "name": "gold_metrics",
            "description": "Metrics",
            "input_models": ["silver_data"],
            "aggregation": {
                "group_by": ["id"],
                "measures": [
                    {"column": "count", "expression": "COUNT(*)", "alias": "count"},
                    {"column": "total", "expression": "SUM(value)", "alias": "total"},
                ],
            },
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "count" in tests
        assert "total" in tests
        assert "not_null" in tests

    def test_render_gold_no_tests_without_measures(self, renderer) -> None:
        params = {
            "name": "gold_empty",
            "description": "Empty",
            "input_models": ["silver_data"],
            "aggregation": {"group_by": ["id"], "measures": []},
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None

    def test_render_gold_no_aggregation(self, renderer) -> None:
        params = {
            "name": "gold_order_totals",
            "description": "Aggregated order totals",
            "input_models": ["silver_order_items"],
        }
        sql = renderer.render(params, renderer.dialect, None)
        assert "SELECT" in sql
        assert "*" in sql
        assert "__loaded_at" in sql
        assert "ref('silver_order_items')" in sql
        assert "GROUP BY" not in sql




# -- Generated-SQL execution -----------------------------------------
# Data-semantic execution: these assert executed rows/columns, not
# render strings, so the conformance chain (which only executes the
# canonical renders and asserts hub/link/fact counts) does not cover
# them.


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def test_bronze_and_silver_dedup_execute(conn) -> None:
    conn.execute(
        "CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR)"
    )
    conn.execute("INSERT INTO customers VALUES (1, 'Ada'), (2, 'Grace')")
    materialize(
        conn,
        "bronze_customers",
        render(
            "bronze",
            {
                "name": "bronze_customers",
                "inline_source": {"name": "raw", "table": "customers"},
            },
        ),
    )
    rows = conn.execute(
        "SELECT customer_id, customer_name FROM bronze_customers "
        "ORDER BY customer_id"
    ).fetchall()
    assert rows == [(1, "Ada"), (2, "Grace")]

    # A second, later bronze load of customer 1 must win the dedup.
    conn.execute(
        "INSERT INTO bronze_customers "
        "SELECT 1, 'Ada Lovelace', CURRENT_TIMESTAMP + INTERVAL 1 DAY"
    )
    materialize(
        conn,
        "silver_customers",
        render(
            "silver",
            {
                "name": "silver_customers",
                "input_models": ["bronze_customers"],
                "dedup_key": "customer_id",
            },
        ),
    )
    rows = conn.execute(
        "SELECT customer_id, customer_name FROM silver_customers "
        "ORDER BY customer_id"
    ).fetchall()
    assert rows == [(1, "Ada Lovelace"), (2, "Grace")]
    # The internal dedup column must not leak into the output.
    output_columns = [
        row[0] for row in conn.execute("DESCRIBE silver_customers").fetchall()
    ]
    assert "rn" not in output_columns


def test_silver_declared_columns_execute_without_rn(conn) -> None:
    # The explicit-projection shape (declared `columns`) is what
    # dialects without star-exclusion syntax generate.
    conn.execute(
        "CREATE TABLE bronze_orders "
        "(order_id INTEGER, amount DECIMAL(10, 2), __loaded_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO bronze_orders VALUES "
        "(1, 10.00, CURRENT_TIMESTAMP), (1, 12.50, CURRENT_TIMESTAMP + INTERVAL 1 DAY)"
    )
    materialize(
        conn,
        "silver_orders",
        render(
            "silver",
            {
                "name": "silver_orders",
                "input_models": ["bronze_orders"],
                "dedup_key": "order_id",
                "columns": ["order_id", "amount"],
            },
        ),
    )
    output_columns = [
        row[0] for row in conn.execute("DESCRIBE silver_orders").fetchall()
    ]
    assert output_columns == ["order_id", "amount", "__loaded_at"]
    rows = conn.execute("SELECT order_id, amount FROM silver_orders").fetchall()
    assert rows == [(1, 12.50)]


def test_silver_pass_through_preserves_single_loaded_at(conn) -> None:
    conn.execute(
        "CREATE TABLE bronze_x (id INTEGER, __loaded_at TIMESTAMP)"
    )
    conn.execute("INSERT INTO bronze_x VALUES (1, CURRENT_TIMESTAMP)")
    materialize(
        conn,
        "silver_x",
        render("silver", {"name": "silver_x", "input_models": ["bronze_x"]}),
    )
    columns = [
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'silver_x'"
        ).fetchall()
    ]
    # Before the fallback fix this CREATE failed: the fallback
    # appended a second __loaded_at over the input's own column.
    assert columns == ["id", "__loaded_at"]



class TestStep2GoldValidation:
        def test_gold_multi_input_without_joins_rejected(self, tmp_path: Path) -> None:
            _register(tmp_path, "silver_customers", "silver")
            _register(tmp_path, "silver_orders", "silver")
            write_pattern_instance(
                tmp_path,
                "gold_customer_orders",
                "gold",
                {
                    "description": "Aggregates",
                    "owner": "analytics",
                    "input_models": ["silver_customers", "silver_orders"],
                    "aggregation": {
                        "group_by": ["customer_id"],
                        "measures": [
                            {"column": "c", "expression": "COUNT(*)", "alias": "c"}
                        ],
                    },
                },
                pattern="gold",
            )
            message = _validation_error(tmp_path)
            assert "explicit 'joins' entry" in message

        def test_gold_multi_input_with_joins_validates(self, tmp_path: Path) -> None:
            _register(tmp_path, "silver_customers", "silver")
            _register(tmp_path, "silver_orders", "silver")
            write_pattern_instance(
                tmp_path,
                "gold_customer_orders",
                "gold",
                {
                    "description": "Aggregates",
                    "owner": "analytics",
                    "input_models": ["silver_customers", "silver_orders"],
                    "joins": [{"model": "silver_orders", "on": ["customer_id"]}],
                    "aggregation": {
                        "group_by": ["customer_id"],
                        "measures": [
                            {"column": "c", "expression": "COUNT(*)", "alias": "c"}
                        ],
                    },
                },
                pattern="gold",
            )
            entries = validate_project(tmp_path)
            assert any(
                entry.data.get("name") == "gold_customer_orders" for entry in entries
            )

        def test_gold_measure_without_expression_rejected(
            self, tmp_path: Path
        ) -> None:
            _register(tmp_path, "silver_customers", "silver")
            write_pattern_instance(
                tmp_path,
                "gold_bad",
                "gold",
                {
                    "description": "Bad measure",
                    "owner": "analytics",
                    "input_models": ["silver_customers"],
                    "aggregation": {
                        "group_by": ["customer_id"],
                        "measures": [{"column": "c", "alias": "c"}],
                    },
                },
                pattern="gold",
            )
            message = _validation_error(tmp_path)
            assert "requires a non-empty 'expression'" in message

        def test_gold_without_any_input_rejected(self, tmp_path: Path) -> None:
            write_pattern_instance(
                tmp_path,
                "gold_bad",
                "gold",
                {"description": "No input", "owner": "analytics"},
                pattern="gold",
            )
            message = _validation_error(tmp_path)
            assert "a non-empty 'input_models' list is a required property" in message

        def test_mart_measure_without_expression_rejected(
            self, tmp_path: Path
        ) -> None:
            _register(tmp_path, "stg_orders", "staging")
            write_pattern_instance(
                tmp_path,
                "mart_bad",
                "mart",
                {
                    "description": "Bad measure",
                    "owner": "analytics",
                    "input_models": ["stg_orders"],
                    "aggregation": {
                        "group_by": ["customer_id"],
                        "measures": [{"column": "c", "alias": "c"}],
                    },
                },
                pattern="mart",
            )
            message = _validation_error(tmp_path)
            assert "requires a non-empty 'expression'" in message



class TestStep2BronzeDegenerateGuards:
        def test_bronze_missing_source_table_rejected(self, tmp_path: Path) -> None:
            write_pattern_instance(
                tmp_path,
                "bronze_customers",
                "bronze",
                {
                    "description": "Bronze",
                    "owner": "analytics",
                    "inline_source": {"name": "raw"},
                },
                pattern="bronze",
            )
            message = _validation_error(tmp_path)
            assert "inline_source.table" in message

        def test_bronze_without_source_raises(self) -> None:
            renderer = BronzeRenderer(DIALECTS["duckdb"])
            with pytest.raises(LoomError, match="inline_source"):
                renderer.render({"name": "bronze_x"}, renderer.dialect, None)

        def test_silver_without_dedup_key_does_not_duplicate_loaded_at(self) -> None:
            renderer = SilverRenderer(DIALECTS["duckdb"])
            sql = renderer.render(
                {"name": "silver_x", "input_models": ["bronze_x"]},
                renderer.dialect,
                None,
            )
            assert "AS __loaded_at" not in sql
            assert "SELECT *" in sql


# Validation helpers.

def _register(project: Path, name: str, classification: str = "external") -> None:
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True, exist_ok=True)
    (registrations / f"{name}.yml").write_text(
        f"name: {name}\n"
        f"classification: {classification}\n"
        "description: Registered input.\n"
        "owner: analytics\n",
        encoding="utf-8",
    )

def _validation_error(project: Path) -> str:
    with pytest.raises(LoomError) as error:
        validate_project(project)
    return str(error.value)
