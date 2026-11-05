from __future__ import annotations

import duckdb
import pytest

from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance
from render_execution import compile_sql, materialize, render
from loom.renderers import (
    NormalizedTableRenderer,
    ViewRenderer,
)


class TestNormalizedTableRenderer:
    """Test NormalizedTableRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> NormalizedTableRenderer:
        return NormalizedTableRenderer(DIALECTS["duckdb"])


    def test_render_normalized_table_produces_tests(self, renderer) -> None:
        params = {
            "name": "n_customer",
            "description": "Test",
            "input_models": ["bronze_customers"],
            "entity_columns": ["customer_id", "email"],
            "attribute_columns": ["country"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "customer_id" in tests
        assert "email" in tests
        assert "country" not in tests
        assert "not_null" in tests
        assert "models:" in tests

    def test_render_normalized_table_key_column_is_unique(
        self, renderer
    ) -> None:
        """The first entity column is the entity key and is the
        only column carrying the generated unique test."""
        import yaml

        params = {
            "name": "n_customer",
            "description": "Test",
            "input_models": ["bronze_customers"],
            "entity_columns": ["customer_id", "customer_name", "email"],
            "attribute_columns": ["country"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        parsed = yaml.safe_load(tests)
        columns = {
            column["name"]: column["tests"]
            for column in parsed["models"][0]["columns"]
        }
        assert columns["customer_id"] == ["not_null", "unique"]
        assert columns["customer_name"] == ["not_null"]
        assert columns["email"] == ["not_null"]

    def test_render_normalized_table_no_tests_on_empty_entity_columns(
        self, renderer
    ) -> None:
        params = {
            "name": "n_test",
            "description": "Test",
            "input_models": ["bronze_test"],
            "entity_columns": [],
            "attribute_columns": ["value"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None


class TestViewRenderer:
    """Test ViewRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> ViewRenderer:
        return ViewRenderer(DIALECTS["duckdb"])


    def test_render_view_multiple_inputs_without_join_keys_raises(
        self, renderer
    ) -> None:
        params = {
            "name": "vw_customer_orders",
            "description": "Test",
            "input_models": ["n_customer", "n_order"],
            "select_columns": [
                {"source": "n_customer.customer_id", "alias": "customer_id"},
            ],
        }
        with pytest.raises(Exception, match="explicit 'joins' entry"):
            renderer.render(params, renderer.dialect, None)

    def test_render_view_no_tests(self, renderer) -> None:
        params = {
            "name": "vw_test",
            "description": "Test",
            "input_models": ["n_test"],
            "select_columns": [
                {"source": "n_test.id", "alias": "id"},
            ],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None




# -- Generated-SQL execution -----------------------------------------
# Data-semantic execution: LEFT JOIN null survival is a row-value
# assertion the conformance chain (execute-only for views) does not
# make.


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def test_view_multi_input_executes(conn) -> None:
    conn.execute(
        "CREATE TABLE n_customer "
        "(customer_id INTEGER, customer_name VARCHAR)"
    )
    conn.execute(
        "INSERT INTO n_customer VALUES (1, 'Ada'), (2, 'Grace')"
    )
    conn.execute(
        "CREATE TABLE n_order (order_id INTEGER, customer_id INTEGER)"
    )
    conn.execute("INSERT INTO n_order VALUES (100, 1)")
    materialize(
        conn,
        "vw_customer_orders",
        render(
            "view",
            {
                "name": "vw_customer_orders",
                "input_models": ["n_customer", "n_order"],
                "joins": [{"model": "n_order", "on": ["customer_id"]}],
                "select_columns": [
                    {"source": "n_customer.customer_id", "alias": "customer_id"},
                    {"source": "n_order.order_id", "alias": "order_id"},
                ],
            },
        ),
    )
    rows = conn.execute(
        "SELECT customer_id, order_id FROM vw_customer_orders "
        "ORDER BY customer_id"
    ).fetchall()
    # LEFT JOIN semantics: Grace has no order and survives with NULL.
    assert rows == [(1, 100), (2, None)]



class TestStep2ViewValidation:
        def _view_params(self, **extra: object) -> dict:
            params: dict = {
                "description": "Reporting view",
                "owner": "analytics",
                "input_models": ["n_customer", "n_order"],
                "select_columns": [
                    {"source": "n_customer.customer_id", "alias": "customer_id"},
                    {"source": "n_order.order_id", "alias": "order_id"},
                ],
            }
            params.update(extra)
            return params

        def test_view_multi_input_without_joins_rejected(
            self, tmp_path: Path
        ) -> None:
            _register(tmp_path, "n_customer", "normalized")
            _register(tmp_path, "n_order", "normalized")
            write_pattern_instance(
                tmp_path, "vw_customer_orders", "view", self._view_params(), pattern="view"
            )
            message = _validation_error(tmp_path)
            assert "explicit 'joins' entry" in message

        def test_view_multi_input_with_joins_validates(self, tmp_path: Path) -> None:
            _register(tmp_path, "n_customer", "normalized")
            _register(tmp_path, "n_order", "normalized")
            write_pattern_instance(
                tmp_path,
                "vw_customer_orders",
                "view",
                self._view_params(joins=[{"model": "n_order", "on": ["customer_id"]}]),
                pattern="view",
            )
            entries = validate_project(tmp_path)
            assert any(
                entry.data.get("name") == "vw_customer_orders" for entry in entries
            )


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
