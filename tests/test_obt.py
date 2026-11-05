from __future__ import annotations

import duckdb
import pytest

from loom.dialects import DIALECTS
from render_execution import compile_sql, materialize, render
from loom.renderers import OBTRenderer


class TestOBTRenderer:
    """Test OBTRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> OBTRenderer:
        return OBTRenderer(DIALECTS["duckdb"])


    def test_render_obt_no_joins(self, renderer) -> None:
        # An OBT without joins would be a timestamp-only model; the
        # renderer refuses it instead of generating one (M-02).
        params = {
            "name": "obt_basic",
            "input_models": ["stg_orders"],
            "columns": ["order_id"],
            "joins": [],
        }
        with pytest.raises(Exception, match="non-empty 'joins'"):
            renderer.render(params, renderer.dialect, None)

    def test_render_tests_returns_none(self, renderer) -> None:
        params = {
            "name": "obt_customer_orders",
            "input_models": ["stg_orders"],
            "columns": ["order_id"],
            "joins": [],
        }
        assert renderer.render_tests(params, renderer.dialect, None) is None


class TestOBTSourceColumnsFI53:
    """The source model's own declared columns are projected."""

    @pytest.fixture
    def renderer(self) -> OBTRenderer:
        return OBTRenderer(DIALECTS["duckdb"])

    def _params(self, **overrides):
        params = {
            "name": "obt_orders",
            "input_models": ["stg_orders"],
            "columns": ["order_id", "amount"],
            "joins": [
                {
                    "model": "stg_customers",
                    "on": ["customer_id"],
                    "columns": ["customer_name"],
                },
            ],
        }
        params.update(overrides)
        return params

    def test_source_columns_projected_first(self, renderer) -> None:
        sql = renderer.render(self._params(), renderer.dialect, None)
        assert "t.order_id AS order_id" in sql
        assert "t.amount AS amount" in sql
        # Source columns precede joined columns in the SELECT list.
        assert sql.index("t.order_id") < sql.index("j0.customer_name")

    def test_join_column_colliding_with_source_column_is_aliased(
        self, renderer
    ) -> None:
        params = self._params(
            columns=["customer_id"],
            joins=[
                {
                    "model": "stg_customers",
                    "on": ["customer_id"],
                    "columns": ["customer_id", "customer_name"],
                }
            ],
        )
        sql = renderer.render(params, renderer.dialect, None)
        assert "t.customer_id AS customer_id" in sql
        assert "j0.customer_id AS j0_customer_id" in sql

    def test_missing_columns_raises(self, renderer) -> None:
        params = self._params()
        del params["columns"]
        with pytest.raises(Exception, match="non-empty 'columns'"):
            renderer.render(params, renderer.dialect, None)

    def test_empty_columns_raises(self, renderer) -> None:
        with pytest.raises(Exception, match="non-empty 'columns'"):
            renderer.render(self._params(columns=[]), renderer.dialect, None)

    def test_declared_loaded_at_is_not_duplicated(self, renderer) -> None:
        sql = renderer.render(
            self._params(columns=["order_id", "__loaded_at"]),
            renderer.dialect,
            None,
        )
        assert sql.count("__loaded_at") == 1
        assert "t.__loaded_at" not in sql


# -- Generated-SQL execution -----------------------------------------
# Data-semantic execution: join aliasing must resolve to the right
# values in execution, not just the right strings in the render.


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def test_obt_with_overlapping_join_columns_executes(conn) -> None:
    conn.execute("CREATE TABLE stg_orders (order_id INTEGER, a_id INTEGER, b_id INTEGER)")
    conn.execute("INSERT INTO stg_orders VALUES (1, 10, 20)")
    conn.execute("CREATE TABLE stg_a (a_id INTEGER, name VARCHAR)")
    conn.execute("INSERT INTO stg_a VALUES (10, 'from-a')")
    conn.execute("CREATE TABLE stg_b (b_id INTEGER, name VARCHAR)")
    conn.execute("INSERT INTO stg_b VALUES (20, 'from-b')")
    materialize(
        conn,
        "obt_orders",
        render(
            "obt",
            {
                "name": "obt_orders",
                "input_models": ["stg_orders"],
                "columns": ["order_id"],
                "joins": [
                    {"model": "stg_a", "on": ["a_id"], "columns": ["name"]},
                    {"model": "stg_b", "on": ["b_id"], "columns": ["name"]},
                ],
            },
        ),
    )
    rows = conn.execute(
        "SELECT order_id, name, j1_name FROM obt_orders"
    ).fetchall()
    assert rows == [(1, "from-a", "from-b")]



class TestStep2ObtDegenerateGuards:
        def test_obt_duplicate_join_columns_get_unique_aliases(self) -> None:
            renderer = OBTRenderer(DIALECTS["duckdb"])
            sql = renderer.render(
                {
                    "name": "obt_x",
                    "input_models": ["stg_orders"],
                    "columns": ["id"],
                    "joins": [
                        {"model": "stg_a", "on": ["id"], "columns": ["name"]},
                        {"model": "stg_b", "on": ["id"], "columns": ["name"]},
                    ],
                },
                renderer.dialect,
                None,
            )
            assert "j0.name AS name" in sql
            assert "j1.name AS j1_name" in sql
