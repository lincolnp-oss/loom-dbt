"""Satellite second-wave runtime proof on DuckDB (ADR 0028).

Multi-active: several attribute rows per key per load, with change
detection partitioned by the sub-sequence - one phone number
changing must not disturb the others. Effectivity: a presence log
over three loads (relationship present, ended, returned) whose
windows view derives the correct date windows, including a closed
window whose end equals the next relationship's start batch.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dbt_harness import duckdb_profiles_yml, run_dbt
from loom.cli import main

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def _write_project(project: Path) -> None:
    (project / "models").mkdir(parents=True)
    (project / "seeds").mkdir()
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: sat_wave2\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: sat_wave2\n"
        "model-paths: ['models']\n"
        "seed-paths: ['seeds']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        duckdb_profiles_yml('sat_wave2', 'sat_wave2.duckdb'),
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: customer_phones\n"
        "      - name: orders\n",
        encoding="utf-8",
    )
    patterns = {
        "staging.yml": ("staging", "staging", "[description, source_ref, columns]"),
        "hub.yml": ("hub", "hub", "[description, input_models, business_key]"),
        "link.yml": (
            "link",
            "link",
            "[description, input_models, endpoints]",
        ),
        "sat.yml": (
            "sat",
            "satellite",
            "[description, parent, input_models, record_source]",
        ),
    }
    for filename, (name, generator, required) in patterns.items():
        (project / "metadata" / "patterns" / filename).write_text(
            f"name: {name}\n"
            f"generator: {generator}\n"
            "defaults: {owner: test}\n"
            f"required_params: {required}\n",
            encoding="utf-8",
        )
    (project / "metadata" / "instances" / "staging.yml").write_text(
        "pattern: staging\n"
        "instances:\n"
        "  - name: stg_customers\n"
        "    params:\n"
        "      description: Customers.\n"
        "      source_ref: {name: raw, table: customers}\n"
        "      columns:\n"
        "        - {source: id, alias: customer_id}\n"
        "        - {source: name, alias: customer_name}\n"
        "  - name: stg_customer_phones\n"
        "    params:\n"
        "      description: Customer phones.\n"
        "      source_ref: {name: raw, table: customer_phones}\n"
        "      columns:\n"
        "        - {source: customer_id, alias: customer_id}\n"
        "        - {source: phone_type, alias: phone_type}\n"
        "        - {source: phone_number, alias: phone_number}\n"
        "  - name: stg_orders\n"
        "    params:\n"
        "      description: Orders.\n"
        "      source_ref: {name: raw, table: orders}\n"
        "      columns:\n"
        "        - {source: order_id, alias: order_id}\n"
        "        - {source: customer_id, alias: customer_id}\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "vault.yml").write_text(
        "pattern: hub\n"
        "instances:\n"
        "  - name: hub_customer\n"
        "    params:\n"
        "      description: Customer hub.\n"
        "      input_models: [stg_customers]\n"
        "      business_key: customer_id\n"
        "  - name: hub_order\n"
        "    params:\n"
        "      description: Order hub.\n"
        "      input_models: [stg_orders]\n"
        "      business_key: order_id\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "links.yml").write_text(
        "pattern: link\n"
        "instances:\n"
        "  - name: link_order_customer\n"
        "    params:\n"
        "      description: Order-customer link.\n"
        "      input_models: [stg_orders]\n"
        "      endpoints:\n"
        "        - hub: hub_order\n"
        "          key: order_id\n"
        "        - hub: hub_customer\n"
        "          key: customer_id\n",
        encoding="utf-8",
    )
    (project / "metadata" / "instances" / "satellites.yml").write_text(
        "pattern: sat\n"
        "instances:\n"
        "  - name: sat_customer_phones\n"
        "    params:\n"
        "      description: Customer phone numbers (multi-active).\n"
        "      parent: {model: hub_customer, key: customer_id}\n"
        "      input_models: [stg_customer_phones]\n"
        "      record_source: crm\n"
        "      variant: multi_active\n"
        "      subsequence_key: phone_type\n"
        "      attribute_columns: [phone_number]\n"
        "  - name: sat_order_customer_effectivity\n"
        "    params:\n"
        "      description: Order-customer effectivity.\n"
        "      parent:\n"
        "        model: link_order_customer\n"
        "        endpoints:\n"
        "          - hub: hub_order\n"
        "            key: order_id\n"
        "          - hub: hub_customer\n"
        "            key: customer_id\n"
        "      input_models: [stg_orders]\n"
        "      record_source: oms\n"
        "      variant: effectivity\n"
        "      driving_key: order_id\n",
        encoding="utf-8",
    )

@pytest.mark.integration
def test_multi_active_and_effectivity_lifecycles_on_duckdb(
    tmp_path: Path,
) -> None:
    duckdb = pytest.importorskip("duckdb", reason="requires the integration group")
    dbt = shutil.which("dbt")
    if dbt is None:
        pytest.skip("dbt-duckdb is required for the integration group")

    project = tmp_path / "sat_wave2"
    _write_project(project)

    def load(phones: list[tuple], orders: list[tuple[int, int]]) -> None:
        (project / "seeds" / "customers.csv").write_text(
            "id,name\n7,Ada\n9,Grace\n", encoding="utf-8"
        )
        (project / "seeds" / "customer_phones.csv").write_text(
            "customer_id,phone_type,phone_number\n"
            + "".join(f"{c},{t},{n}\n" for c, t, n in phones),
            encoding="utf-8",
        )
        (project / "seeds" / "orders.csv").write_text(
            "order_id,customer_id\n" + "".join(f"{o},{c}\n" for o, c in orders),
            encoding="utf-8",
        )
        run_dbt(dbt, project, "seed", "--full-refresh")
        run_dbt(dbt, project, "build")

    def query(sql: str) -> list[tuple]:
        connection = duckdb.connect(str(project / "sat_wave2.duckdb"), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    assert main(["validate", "--project-dir", str(project)]) == 0
    assert main(["generate", "--write", "--project-dir", str(project), "--dialect", "duckdb"]) == 0

    # -- Load 1: two phone rows for Ada, orders 101 (Ada) + 102 (Grace).
    load(
        [(7, "home", "111"), (7, "work", "222"), (9, "home", "999")],
        [(101, 7), (102, 9)],
    )
    assert len(query("select * from main.sat_customer_phones")) == 3
    assert len(query("select * from main.sat_order_customer_effectivity")) == 2
    windows = query(
        "select order_hk, __valid_to is null from main.sat_order_customer_effectivity_windows"
    )
    assert {(r[0], r[1]) for r in windows} == {
        (_sha256("101"), True),
        (_sha256("102"), True),
    }

    # -- Load 2: Ada's home number changes (work untouched); order
    # 101's relationship ends, 103 (Ada) appears.
    load(
        [(7, "home", "333"), (7, "work", "222"), (9, "home", "999")],
        [(102, 9), (103, 7)],
    )
    # Multi-active: exactly one new row (Ada home), nothing else.
    assert len(query("select * from main.sat_customer_phones")) == 4
    current = query(
        "select phone_type, cast(phone_number as varchar) "
        "from main.sat_customer_phones_current "
        f"where customer_hk = '{_sha256('7')}' order by phone_type"
    )
    assert current == [("home", "333"), ("work", "222")]
    # Effectivity: presence log grows by the two present pairs; 101's
    # window closes at the batch where 103's window opens.
    assert len(query("select * from main.sat_order_customer_effectivity")) == 4
    rows = query(
        "select order_hk, __valid_from, __valid_to "
        "from main.sat_order_customer_effectivity_windows order by order_hk, __valid_from"
    )
    by_order: dict[str, list[tuple]] = {}
    for order_hk, valid_from, valid_to in rows:
        by_order.setdefault(order_hk, []).append((valid_from, valid_to))
    assert len(by_order[_sha256("101")]) == 1
    assert by_order[_sha256("101")][0][1] is not None  # closed
    assert by_order[_sha256("102")][0][1] is None  # still effective
    assert by_order[_sha256("103")][0][1] is None
    # The closing batch of 101 is the opening batch of 103.
    assert by_order[_sha256("101")][0][1] == by_order[_sha256("103")][0][0]

    # -- Load 3: order 101 returns - a second, open window opens.
    load(
        [(7, "home", "333"), (7, "work", "222"), (9, "home", "999")],
        [(101, 7), (102, 9), (103, 7)],
    )
    rows = query(
        "select __valid_from, __valid_to "
        "from main.sat_order_customer_effectivity_windows "
        f"where order_hk = '{_sha256('101')}' order by __valid_from"
    )
    assert len(rows) == 2
    assert rows[0][1] is not None and rows[0][1] < rows[1][0]
    assert rows[1][1] is None
