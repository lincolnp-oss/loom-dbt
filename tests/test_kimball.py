from __future__ import annotations


import pytest

from loom.dialects import DIALECTS
from loom.renderers import (
    DimensionRenderer,
    FactRenderer,
)


class TestFactRenderer:
    """Test FactRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> FactRenderer:
        return FactRenderer(DIALECTS["duckdb"])


    def test_render_tests_not_null_on_dim_keys(self, renderer) -> None:
        params = {
            "name": "fct_orders",
            "input_models": ["stg_orders"],
            "measure_columns": ["quantity"],
            "dimension_keys": ["customer_id", "product_id"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "not_null" in tests
        assert "customer_id" in tests
        assert "product_id" in tests

    def test_render_tests_not_null_on_measures(self, renderer) -> None:
        params = {
            "name": "fct_orders",
            "input_models": ["stg_orders"],
            "measure_columns": ["quantity", "unit_price"],
            "dimension_keys": ["customer_id"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "quantity" in tests
        assert "unit_price" in tests

    def test_render_tests_generated_for_non_fct_name(self, renderer) -> None:
        # The fct_ prefix is a convention, not a gate - tests are
        # generated whatever the fact model is named.
        params = {
            "name": "orders_fact",
            "input_models": ["stg_customers"],
            "measure_columns": ["quantity"],
            "dimension_keys": ["customer_id"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "quantity" in tests
        assert "customer_id" in tests

    def test_render_tests_empty_columns(self, renderer) -> None:
        params = {
            "name": "fct_orders",
            "input_models": ["stg_orders"],
            "measure_columns": [],
            "dimension_keys": [],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None


class TestDimensionRenderer:
    """Test DimensionRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> DimensionRenderer:
        return DimensionRenderer(DIALECTS["duckdb"])


    def test_render_tests(self, renderer) -> None:
        params = {
            "name": "dim_customers",
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_name"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "not_null" in tests
        assert "unique" in tests
        assert "customer_id" in tests
        assert "customer_sk" in tests

    def test_render_tests_generated_for_non_dim_name(self, renderer) -> None:
        # The dim_ prefix is a convention, not a gate.
        params = {
            "name": "customers_dimension",
            "input_models": ["stg_orders"],
            "natural_key": "order_id",
            "surrogate_key": "order_sk",
            "attribute_columns": ["total"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        assert "order_id" in tests

    def test_render_tests_unique_on_natural_key(self, renderer) -> None:
        # M-04: duplicate natural keys must fail a generated test rather
        # than silently produce duplicate dimension rows.
        params = {
            "name": "dim_customers",
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": [],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is not None
        natural_key_block = tests.split("customer_sk")[0]
        assert "- name: customer_id" in natural_key_block
        assert "unique" in natural_key_block

    def test_render_tests_missing_keys(self, renderer) -> None:
        params = {
            "name": "dim_customers",
            "input_models": ["stg_customers"],
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None


