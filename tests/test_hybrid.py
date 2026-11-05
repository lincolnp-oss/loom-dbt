from __future__ import annotations

import pytest

from loom.dialects import DIALECTS
from loom.renderers import (
    MartRenderer,
)


class TestMartRenderer:
    """Test MartRenderer SQL generation."""

    @pytest.fixture
    def renderer(self) -> MartRenderer:
        return MartRenderer(DIALECTS["duckdb"])


    def test_render_mart_generates_tests(self, renderer) -> None:
        params = {
            "name": "mart_metrics",
            "description": "Metrics",
            "input_models": ["stg_data"],
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

    def test_render_mart_no_tests_without_measures(self, renderer) -> None:
        params = {
            "name": "mart_empty",
            "description": "Empty",
            "input_models": ["stg_data"],
            "aggregation": {"group_by": ["id"], "measures": []},
        }
        tests = renderer.render_tests(params, renderer.dialect, None)
        assert tests is None


    def test_render_mart_no_group_by(self, renderer) -> None:
        params = {
            "name": "mart_totals",
            "description": "Global totals",
            "input_models": ["stg_orders"],
            "aggregation": {
                "group_by": [],
                "measures": [
                    {"column": "total", "expression": "SUM(amount)", "alias": "total"},
                ],
            },
        }
        sql = renderer.render(params, renderer.dialect, None)
        # An empty group_by is a grand total: no GROUP BY clause at all.
        # (GROUP BY 1 grouped by the first measure expression.)
        assert "GROUP BY" not in sql
        assert "SUM(amount)" in sql


