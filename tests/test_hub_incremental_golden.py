"""Golden byte-identical fixtures for hub SQL.

The fixtures under ``fixtures/hub_golden/`` were captured from the
pre-refactor hub renderer (main ``0fe558e``) before its incremental
paths moved onto the shared core in ``renderers/base.py``. Every
shape x dialect rendering must stay byte-identical to its fixture;
a diff here means a refactor changed generated output, which
must never happen silently.

Shapes: a rebuilt table (control), single-input persistent hubs in
both incremental branches (append + anti-join everywhere; Databricks
merge, with ``merge_exclude_columns`` when provenance is declared),
and the multi-source forms of the same.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.dialects import DIALECTS
from loom.renderers import get_renderer

FIXTURES = Path(__file__).parent / "fixtures" / "hub_golden"

SHAPES: dict[str, dict] = {
    "table": {
        "name": "hub_customer",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
    },
    "persistent_append": {
        "name": "hub_customer",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
        "retention": "persistent",
    },
    "persistent_provenance": {
        "name": "hub_customer",
        "input_models": ["stg_customers"],
        "business_key": "customer_id",
        "retention": "persistent",
        "record_source": "orders_system",
    },
    "persistent_multi": {
        "name": "hub_customer",
        "input_models": ["stg_customers_crm", "stg_customers_web"],
        "business_key": "customer_id",
        "retention": "persistent",
    },
    "persistent_multi_provenance": {
        "name": "hub_customer",
        "input_models": ["stg_customers_crm", "stg_customers_web"],
        "business_key": "customer_id",
        "retention": "persistent",
        "record_source": {
            "stg_customers_crm": "crm",
            "stg_customers_web": "web",
        },
    },
    "table_multi": {
        "name": "hub_customer",
        "input_models": ["stg_customers_crm", "stg_customers_web"],
        "business_key": "customer_id",
    },
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_hub_sql_byte_identical_to_golden(shape: str, dialect_name: str) -> None:
    renderer = get_renderer("hub", dialect_name)
    sql = renderer.render(
        dict(SHAPES[shape]), DIALECTS[dialect_name], None
    )
    expected = (FIXTURES / f"{shape}__{dialect_name}.sql").read_text(
        encoding="utf-8"
    )
    assert sql == expected
