"""Generated-SQL parse layer (default suite).

Every generator's rendered SQL - compiled through the same Jinja
stubs as the execution harness - must parse under a dialect's
grammar via sqlglot. This is the structural guard against an early
defect class (syntactically invalid warehouse SQL that no test
executed): it is a *syntax* guarantee only. Parse success is not
execution evidence and must never be reported as dialect support;
execution proof lives in the runtime tests (DuckDB).

One parse test per *distinct rendered output*
per case, parsed in a representative dialect of its own byte-identical
cluster - never another dialect's grammar. Cluster representatives
rotate deterministically across cases (md5 of the case key, so the
choice is stable across processes), and every grammar represents
several clusters across each matrix (asserted by the cover test).
Outputs that are byte-identical in several dialects are therefore
parsed once, not eight times. The accepted losses this creates are
the accepted losses: per-dialect grammar acceptance of
byte-identical outputs in non-representative grammars, timestamp
cells for non-representative dialects, and cross-dialect parse for
the satellite shapes canonicalised out of CASES below (multi-active,
effectivity, n-ary - DuckDB parse + execution for those shapes
lives in the goldens and runtime tests instead).
"""
from __future__ import annotations

from pathlib import Path
import hashlib

import jinja2
import pytest
import sqlglot
from sqlglot import exp

from loom.dialects import DIALECTS
from loom.renderers import get_renderer


# Functions sqlglot does not model for a dialect although the
# warehouse has them (registry gaps, verified against the engines):
# - postgres convert_to: real Postgres function used by the
#   hash template; pinned by exact-string tests there.
# - clickhouse now(): real ClickHouse function (the dialect's
#   current_timestamp).
# - the link guard's raise functions: duckdb error(),
#   bigquery ERROR(), databricks raise_error() and clickhouse
#   intDiv() are each dialect's link_guard_template gate; error()
#   and intDiv() are executed in the §4.5 guard runtime tests, the
#   other two are those engines' documented raise/divide idioms.
# - clickhouse toDateTime(): the real ClickHouse timestamp-literal
# parser used by the PIT ghost sentinel; executed in the
#   quickstart ClickHouse CI leg via the quickstart PIT instance.
# Anything Anonymous outside this list fails the parse layer.
KNOWN_UNMODELED: dict[str, set[str]] = {
    "postgres": {"convert_to"},
    "clickhouse": {"now", "intdiv", "todatetime"},
    "duckdb": {"error"},
    "bigquery": {"error"},
    "databricks": {"raise_error"},
}


def assert_parses(compiled: str, dialect_name: str) -> None:
    """Parse in the target grammar and reject unknown functions.

    sqlglot parses leniently: a function it does not know *for that
    dialect* still parses, as an Anonymous node (e.g. DuckDB's
    sha256() parses under tsql). Loom-rendered SQL must contain no
    Anonymous function nodes beyond KNOWN_UNMODELED. This still is
    not a function-existence proof - sqlglot's registry is
    cross-dialect (SHA2 parses under duckdb, which has no such
    function) - which is why hash SQL is pinned by exact-string
    tests and execution proof is separate.
    """
    parsed = sqlglot.parse(compiled, read=SQLGLOT_DIALECTS[dialect_name])
    assert parsed and all(statement is not None for statement in parsed), (
        f"SQL did not parse as {dialect_name}:\n{compiled}"
    )
    allowed = KNOWN_UNMODELED.get(dialect_name, set())
    anonymous = [
        node.sql(dialect=SQLGLOT_DIALECTS[dialect_name])
        for statement in parsed
        for node in statement.walk()
        if isinstance(node, exp.Anonymous)
        and node.name.lower() not in allowed
    ]
    assert not anonymous, (
        f"unknown function(s) for {dialect_name}: {anonymous}\n{compiled}"
    )

SQLGLOT_DIALECTS = {
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "azuresynapse": "tsql",
    "clickhouse": "clickhouse",
}


def _compile(rendered: str, *, incremental: bool) -> str:
    environment = jinja2.Environment()
    template = environment.from_string(rendered)
    return template.render(
        config=lambda **kwargs: "",
        ref=lambda name: name,
        source=lambda source_name, table: table,
        is_incremental=lambda: incremental,
        # dbt's `this` relation variable: a bare name, so the
        # incremental fragments' {{ this }} subqueries (anti-joins,
        # MAX bounds) parse as real SQL in the harness.
        this="loom_this",
    )


def compile_sql(rendered: str) -> str:
    """Compile dbt-flavoured Jinja down to plain SQL (harness stubs)."""
    return _compile(rendered, incremental=False)


def compile_sql_incremental(rendered: str) -> str:
    """Compile with is_incremental() true.

    The incremental branches carry the {{ this }} subqueries and the
    dialect date-arithmetic templates; they must parse in the target
    grammar exactly like the first-load branch does.
    """
    return _compile(rendered, incremental=True)


# One representative instance per generator. The SCD2 *view* is
# parsed; the snapshot block is dbt Jinja ({% snapshot %}), which no
# SQL parser accepts - dbt compiles it, and its lifecycle is covered
# by the runtime tests instead.
# Canonicalisation: satellite contributes only its 4
# canonical cases (standard, current-view, non_historised,
# record_tracking); multi-active, effectivity and n-ary satellite
# shapes are parse-proven on DuckDB via the goldens and runtime
# tests, not across dialects here. Link contributes binary, n-ary
# and non_historised (the dependent-child shape is a scenario-test
# concern). Reference_table contributes its default mode only;
# full / snapshot modes are still rendered and executed on DuckDB
# in test_reference_constructs, so only their cross-dialect parse
# is dropped here.
CASES: list[tuple[str, dict, object]] = [
    (
        "staging",
        {
            "name": "stg_customers",
            "source_ref": {"name": "raw", "table": "customers"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "name", "alias": "customer_name"},
            ],
        },
        None,
    ),
    (
        "hub",
        {
            "name": "hub_customer",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        None,
    ),
    (
        "link",
        {
            "name": "link_order_customer",
            "input_models": ["stg_orders"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
        None,
    ),
    (
        "scd2_dimension",
        {
            "name": "dim_customers_scd2",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name"],
        },
        Path("models/dim_customers_scd2.sql"),
    ),
    (
        "scd2_dimension",
        {
            "name": "dim_customers_scd3",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name", "customer_email"],
            "variant": "type_3",
            "previous_value_columns": ["customer_name"],
        },
        Path("models/dim_customers_scd3.sql"),
    ),
    (
        "scd2_dimension",
        {
            "name": "dim_customers_scd6",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["customer_name", "customer_email"],
            "variant": "type_6",
            "previous_value_columns": ["customer_name"],
        },
        Path("models/dim_customers_scd6.sql"),
    ),
    (
        "fact",
        {
            "name": "orders_fact",
            "input_models": ["stg_orders"],
            "dimension_keys": ["customer_id"],
            "measure_columns": ["quantity"],
        },
        None,
    ),
    (
        "fact",
        {
            "name": "orders_periodic_fact",
            "input_models": ["stg_orders"],
            "dimension_keys": ["customer_id", "period_key"],
            "measure_columns": ["quantity"],
            "variant": "periodic_snapshot",
            "period_key": "period_key",
        },
        None,
    ),
    (
        "fact",
        {
            "name": "orders_accumulating_fact",
            "input_models": ["stg_orders"],
            "dimension_keys": ["customer_id", "ordered_key", "shipped_key"],
            "measure_columns": ["quantity"],
            "variant": "accumulating_snapshot",
            "milestone_keys": ["ordered_key", "shipped_key"],
        },
        None,
    ),
    (
        "dimension",
        {
            "name": "dim_customers",
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_name"],
        },
        None,
    ),
    (
        "bronze",
        {
            "name": "bronze_customers",
            "inline_source": {"name": "raw", "table": "customers"},
        },
        None,
    ),
    (
        "silver",
        {
            "name": "silver_customers",
            "input_models": ["bronze_customers"],
            "dedup_key": "customer_id",
            # Declared so the explicit-projection shape is what gets
            # parsed on dialects without star-exclusion syntax;
            # the star-exclusion shape has its own parse test below.
            "columns": ["customer_id", "customer_name"],
        },
        None,
    ),
    (
        "gold",
        {
            "name": "gold_customer_orders",
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
        },
        None,
    ),
    (
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
        None,
    ),
    (
        "normalized_table",
        {
            "name": "n_customer",
            "input_models": ["bronze_customers"],
            "entity_columns": ["customer_id", "customer_name", "email"],
            "attribute_columns": ["country", "segment", "created_at"],
        },
        None,
    ),
    (
        "view",
        {
            "name": "vw_customer_orders",
            "input_models": ["n_customer", "n_order"],
            "joins": [{"model": "n_order", "on": ["customer_id"]}],
        },
        None,
    ),
    (
        "mart",
        {
            "name": "mart_totals",
            "input_models": ["stg_orders"],
            "aggregation": {
                "group_by": [],
                "measures": [
                    {
                        "column": "total",
                        "expression": "SUM(amount)",
                        "alias": "total",
                    }
                ],
            },
        },
        None,
    ),
    (
        "satellite",
        {
            "name": "sat_customer_details",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["customer_name", "customer_email"],
            "record_source": "crm",
        },
        None,
    ),
    (
        "satellite",
        {
            "name": "sat_customer_details",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["customer_name", "customer_email"],
            "record_source": "crm",
        },
        "current",
    ),
    (
        "satellite",
        {
            "name": "sat_order_events",
            "parent": {
                "model": "link_order_customer",
                "endpoints": [
                    {"hub": "hub_order", "key": "order_id"},
                    {"hub": "hub_customer", "key": "customer_id"},
                ],
            },
            "input_models": ["stg_order_items"],
            "variant": "non_historised",
            "attribute_columns": ["product_name", "quantity"],
            "record_source": "pos",
        },
        None,
    ),
    (
        "satellite",
        {
            "name": "sat_customer_tracking",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "variant": "record_tracking",
            "record_source": "crm",
        },
        None,
    ),
    # (ADR 0035) PIT over a hub parent (load calendar +
    # ghost-coalesced latest-row resolution).
    (
        "pit",
        {
            "name": "pit_customer",
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "satellites": ["sat_customer_details", "sat_customer_tracking"],
        },
        None,
    ),
    # (ADR 0034) Reference constructs - hub plus the three
    # reference_table modes.
    (
        "reference_hub",
        {
            "name": "ref_country",
            "input_models": ["stg_country_codes"],
            "business_key": "country_code",
            "record_source": "refdata",
        },
        None,
    ),
    (
        "pit",
        {
            "name": "pit_order_customer",
            "parent": {
                "model": "link_order_customer",
                "endpoints": [
                    {"hub": "hub_order", "key": "order_id"},
                    {"hub": "hub_customer", "key": "customer_id"},
                ],
            },
            "satellites": ["sat_order_customer_details"],
        },
        None,
    ),
    (
        "reference_table",
        {
            "name": "ref_country_table",
            "reference_hub": "ref_country",
            "satellites": ["sat_country_details"],
            "business_key": "country_code",
            "attribute_columns": ["country_name"],
        },
        None,
    ),
    # (ADR 0028) Multi-active and effectivity satellite
    # shapes are canonicalised OUT of this matrix (see the CASES
    # header note); their params live in test_generator_goldens.py
    # and their DuckDB execution in the satellite runtime tests.
    # (ADR 0036) Bridge over a two-step path, plain and
    # with equal weighting.
    (
        "bridge",
        {
            "name": "bridge_customer_products",
            "parent": {"model": "hub_customer"},
            "path": [
                {"link": "link_order_customer", "hub": "hub_order"},
                {"link": "link_order_customer_product", "hub": "hub_product"},
            ],
        },
        None,
    ),
    (
        "bridge",
        {
            "name": "bridge_customer_products",
            "parent": {"model": "hub_customer"},
            "path": [
                {"link": "link_order_customer", "hub": "hub_order"},
                {"link": "link_order_customer_product", "hub": "hub_product"},
            ],
            "weighting": "equal",
        },
        None,
    ),
    # (ADR 0030) The generalised hub/link shapes.
    (
        "hub",
        {
            "name": "hub_customer",
            "input_models": ["stg_customers_crm", "stg_customers_web"],
            "business_key": "customer_id",
            "retention": "persistent",
            "record_source": {
                "stg_customers_crm": "crm",
                "stg_customers_web": "web",
            },
        },
        None,
    ),
    (
        "link",
        {
            "name": "link_order_customer_product",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
                {"hub": "hub_product", "key": "product_name"},
            ],
        },
        None,
    ),
    # Link dependent-child shape canonicalised out (see CASES header
    # note); covered by link scenario tests.
    (
        "link",
        {
            "name": "link_order_item_events",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_product", "key": "product_name"},
            ],
            "variant": "non_historised",
            "dependent_child_key": "order_item_id",
            "payload_columns": ["quantity"],
        },
        None,
    ),
    # N-ary satellite shape canonicalised out (see CASES header
    # note); covered by the multi-source runtime test.
]

GENERATORS = sorted({generator for generator, _, _ in CASES})


def _clusters(outputs: dict[str, str]) -> list[list[str]]:
    """Group dialects by byte-identical rendered output.

    Clusters are ordered by their first (sorted) dialect so the
    parametrisation is stable across runs.
    """
    by_output: dict[str, list[str]] = {}
    for dialect_name in sorted(outputs):
        by_output.setdefault(outputs[dialect_name], []).append(dialect_name)
    return [by_output[sql] for sql in sorted(by_output, key=by_output.get)]


def _representative(cluster: list[str], case_key: str, cluster_index: int) -> str:
    """Pick one dialect from a cluster, rotating across cases.

    md5 (not hash()) so the choice is identical in every process;
    the case key + cluster index rotate the pick, so no single
    grammar represents every cluster of a case.
    """
    digest = hashlib.md5(f"{case_key}:{cluster_index}".encode()).hexdigest()
    return sorted(cluster)[int(digest, 16) % len(cluster)]


def _render_all(
    generator: str, params: dict, output_path: object
) -> dict[str, str]:
    return {
        dialect_name: compile_sql(
            get_renderer(generator, dialect_name).render(
                params, DIALECTS[dialect_name], output_path
            )
        )
        for dialect_name in sorted(DIALECTS)
    }


# One cell per distinct rendered output per case: (generator,
# params, output_path, representative dialect, cluster dialects).
# Computed at import by rendering every case in every dialect -
# the same render work the old 8-dialect matrix did per test, done
# once here instead of once per node.
def _parse_cells() -> list[tuple[str, dict, object, str, tuple[str, ...]]]:
    cells: list[tuple[str, dict, object, str, tuple[str, ...]]] = []
    for index, (generator, params, output_path) in enumerate(CASES):
        case_key = f"{index}:{generator}:{params.get('name')}:{output_path!r}"
        for cluster_index, cluster in enumerate(
            _clusters(_render_all(generator, params, output_path))
        ):
            cells.append(
                (
                    generator,
                    params,
                    output_path,
                    _representative(cluster, case_key, cluster_index),
                    tuple(cluster),
                )
            )
    return cells


PARSE_CELLS = _parse_cells()


@pytest.mark.parametrize(
    "generator,params,output_path,dialect_name,cluster",
    PARSE_CELLS,
    ids=[
        f"{index:03d}-{generator}-{params['name']}-{dialect_name}"
        for index, (generator, params, _, dialect_name, _) in enumerate(PARSE_CELLS)
    ],
)
def test_generated_sql_parses_in_target_dialect(
    generator: str,
    params: dict,
    output_path: object,
    dialect_name: str,
    cluster: tuple[str, ...],
) -> None:
    # The representative's grammar parses the cluster's bytes; the
    # other cluster members render byte-identical SQL, so parsing
    # them again would prove nothing new about the rendered output
    # (the accepted loss is their own grammar's acceptance).
    renderer = get_renderer(generator, dialect_name)
    rendered = renderer.render(params, DIALECTS[dialect_name], output_path)
    assert_parses(compile_sql(rendered), dialect_name)
    assert dialect_name in cluster


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_persistent_hub_sql_parses(dialect_name: str) -> None:
    # Persistent hubs render for every dialect: Databricks uses the
    # merge form, everything else the guarded append form (§4.6).
    renderer = get_renderer("hub", dialect_name)
    rendered = renderer.render(
        {
            "name": "hub_customer",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "retention": "persistent",
            "record_source": "crm",
        },
        DIALECTS[dialect_name],
        None,
    )
    assert_parses(compile_sql(rendered), dialect_name)


SILVER_EXCLUSION_PARAMS = {
    "name": "silver_customers",
    "input_models": ["bronze_customers"],
    "dedup_key": "customer_id",
}


def _silver_cells() -> list[tuple[str, tuple[str, ...]]]:
    """One cell per distinct star-exclusion spelling (EXCLUDE vs
    EXCEPT), parsed in a rotating representative of its cluster."""
    outputs = {
        dialect_name: compile_sql(
            get_renderer("silver", dialect_name).render(
                SILVER_EXCLUSION_PARAMS, DIALECTS[dialect_name], None
            )
        )
        for dialect_name in sorted(DIALECTS)
        if DIALECTS[dialect_name].star_exclude_template is not None
    }
    return [
        (
            _representative(cluster, "silver-exclusion", index),
            tuple(cluster),
        )
        for index, cluster in enumerate(_clusters(outputs))
    ]


SILVER_CELLS = _silver_cells()


@pytest.mark.parametrize(
    "dialect_name,cluster",
    SILVER_CELLS,
    ids=[dialect_name for dialect_name, _ in SILVER_CELLS],
)
def test_silver_star_exclusion_sql_parses(
    dialect_name: str, cluster: tuple[str, ...]
) -> None:
    # With no declared `columns`, silver drops its internal rn
    # column via the dialect's star-exclusion syntax; that shape must
    # parse in its target dialect (the CASES fixture declares columns,
    # so it exercises the explicit-projection shape instead). The
    # five template dialects collapse to two spellings, so two
    # representative parses replace five.
    renderer = get_renderer("silver", dialect_name)
    rendered = renderer.render(
        SILVER_EXCLUSION_PARAMS,
        DIALECTS[dialect_name],
        None,
    )
    assert "rn" in rendered
    assert_parses(compile_sql(rendered), dialect_name)
    assert dialect_name in cluster


def test_parse_layer_covers_all_generators_and_dialects() -> None:
    assert len(GENERATORS) == 18
    assert set(SQLGLOT_DIALECTS) == set(DIALECTS)
    assert {generator for generator, _, _, _, _ in PARSE_CELLS} == set(GENERATORS)
    # Rotation must actually use every grammar somewhere in each
    # matrix; otherwise a dialect's grammar would go unexercised.
    assert {cell[3] for cell in PARSE_CELLS} == set(DIALECTS)
    assert {cell[2] for cell in INCREMENTAL_CELLS} == set(DIALECTS)


# Incremental shapes (link / bronze / normalized_table;
# fact / dimension; silver; gold). Every
# shape is compiled in BOTH is_incremental() branches:
# the false branch is the first load, the true branch carries the
# guard / cursor fragments - for dimension, the true branch also
# carries the max-plus-offset surrogate expression; for silver and
# gold, the affected-key / affected-grain restriction over {{ this }}.
# Collapse: one node per distinct rendered template per case,
# over gate-supported dialects only, parsed in a rotating
# representative grammar. The old gate-skip nodes are deleted
# outright - a skip asserted nothing; gate support itself is
# asserted in the contracts layer.
INCREMENTAL_CASES: list[tuple[str, dict]] = [
    (
        "bronze",
        {
            "name": "bronze_events",
            "inline_source": {"name": "raw", "table": "events"},
            "incremental": {"strategy": "append", "cursor_column": "event_seq"},
        },
    ),
    (
        "bronze",
        {
            "name": "bronze_events",
            "inline_source": {"name": "raw", "table": "events"},
            "incremental": {"strategy": "append", "unique_key": "event_id"},
        },
    ),
    (
        "bronze",
        {
            "name": "bronze_events",
            "inline_source": {"name": "raw", "table": "events"},
            "incremental": {
                "strategy": "merge",
                "unique_key": "event_id",
                "cursor_column": "loaded_seq",
                "lookback": 3,
                "lookback_unit": "day",
            },
        },
    ),
    (
        "link",
        {
            "name": "link_order_customer",
            "input_models": ["stg_orders"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
            "incremental": {"strategy": "append"},
        },
    ),
    (
        "link",
        {
            "name": "link_order_customer",
            "input_models": ["stg_orders"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
            "incremental": {"strategy": "merge"},
        },
    ),
    (
        "link",
        {
            "name": "link_order_customer_product",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
                {"hub": "hub_product", "key": "product_name"},
            ],
            "dependent_child_key": "order_item_id",
            "incremental": {"strategy": "append"},
        },
    ),
    (
        "normalized_table",
        {
            "name": "n_customer",
            "input_models": ["bronze_customers"],
            "entity_columns": ["customer_id", "customer_name", "email"],
            "attribute_columns": ["country", "segment", "created_at"],
            "incremental": {"strategy": "merge"},
        },
    ),
    (
        "normalized_table",
        {
            "name": "n_customer",
            "input_models": ["bronze_customers"],
            "entity_columns": ["customer_id", "customer_name", "email"],
            "attribute_columns": ["country", "segment", "created_at"],
            "incremental": {
                "strategy": "delete+insert",
                "cursor_column": "created_at",
                "lookback": 1,
                "lookback_unit": "day",
            },
        },
    ),
    (
        "fact",
        {
            "name": "fct_orders",
            "input_models": ["stg_orders"],
            "dimension_keys": ["order_id", "order_date"],
            "measure_columns": ["quantity", "amount"],
            "incremental": {"strategy": "append", "cursor_column": "order_date"},
        },
    ),
    (
        "fact",
        {
            "name": "fct_orders",
            "input_models": ["stg_orders"],
            "dimension_keys": ["order_id", "order_date"],
            "measure_columns": ["quantity", "amount"],
            "incremental": {
                "strategy": "merge",
                "unique_key": "order_id",
                "cursor_column": "order_date",
                "lookback": 3,
                "lookback_unit": "day",
            },
        },
    ),
    (
        "fact",
        {
            "name": "fct_orders",
            "input_models": ["stg_orders"],
            "dimension_keys": ["order_id", "order_date"],
            "measure_columns": ["quantity", "amount"],
            "incremental": {
                "strategy": "delete+insert",
                "unique_key": ["order_id", "order_date"],
            },
        },
    ),
    (
        "fact",
        {
            "name": "fct_shipments",
            "input_models": ["stg_shipments"],
            "dimension_keys": ["order_id", "ordered_date", "shipped_date"],
            "measure_columns": ["amount"],
            "variant": "accumulating_snapshot",
            "milestone_keys": ["shipped_date"],
            "incremental": {"strategy": "merge", "unique_key": "order_id"},
        },
    ),
    (
        "fact",
        {
            "name": "fct_daily_sales",
            "input_models": ["stg_daily_sales"],
            "dimension_keys": ["sale_date", "store_id"],
            "measure_columns": ["amount"],
            "variant": "periodic_snapshot",
            "period_key": "sale_date",
            "incremental": {
                "strategy": "merge",
                "unique_key": ["sale_date", "store_id"],
            },
        },
    ),
    (
        "dimension",
        {
            "name": "dim_customer",
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_name", "updated_seq"],
            "incremental": {"strategy": "merge"},
        },
    ),
    (
        "dimension",
        {
            "name": "dim_customer",
            "input_models": ["stg_customers"],
            "natural_key": "customer_id",
            "surrogate_key": "customer_sk",
            "attribute_columns": ["customer_name", "updated_seq"],
            "incremental": {
                "strategy": "merge",
                "cursor_column": "updated_seq",
                "lookback": 1,
            },
        },
    ),
    (
        "silver",
        {
            "name": "silver_orders",
            "input_models": ["bronze_orders"],
            "dedup_key": "order_id",
            "columns": ["order_id", "status"],
            "incremental": {"strategy": "merge"},
        },
    ),
    (
        "silver",
        {
            "name": "silver_orders",
            "input_models": ["bronze_orders"],
            "dedup_key": "order_id",
            "columns": ["order_id", "status"],
            "incremental": {
                "strategy": "delete+insert",
                "lookback": 2,
                "lookback_unit": "day",
            },
        },
    ),
    (
        "silver",
        {
            "name": "silver_orders",
            "input_models": ["bronze_orders"],
            "dedup_key": "order_id",
            "dedup_order": "record_updated_at DESC",
            "columns": ["order_id", "status"],
            "incremental": {
                "strategy": "merge",
                "cursor_column": "record_updated_at",
            },
        },
    ),
    (
        "gold",
        {
            "name": "gold_sales_daily",
            "input_models": ["silver_sales"],
            "aggregation": {
                "group_by": ["sale_date", "region"],
                "measures": [
                    {"expression": "sum(amount)", "alias": "total_amount"},
                    {"expression": "count(*)", "alias": "lines"},
                ],
            },
            "incremental": {
                "strategy": "delete+insert",
                "cursor_column": "sale_date",
                "lookback": 3,
                "lookback_unit": "day",
            },
        },
    ),
    (
        "gold",
        {
            "name": "gold_regional",
            "input_models": ["silver_sales"],
            "aggregation": {
                "group_by": ["region"],
                "measures": [
                    {"expression": "sum(amount)", "alias": "total_amount"}
                ],
            },
            "incremental": {
                "strategy": "merge",
                "cursor_column": "__loaded_at",
            },
        },
    ),
    (
        "gold",
        {
            "name": "gold_customer_lines",
            "input_models": ["silver_sales", "silver_customers"],
            "joins": [{"model": "silver_customers", "on": ["customer_id"]}],
            "aggregation": {
                "group_by": ["customer_id"],
                "measures": [{"expression": "count(*)", "alias": "lines"}],
            },
            "incremental": {
                "strategy": "merge",
                "cursor_column": "customer_id",
            },
        },
    ),
]


def _incremental_cells() -> list[tuple[str, dict, str, tuple[str, ...]]]:
    """One cell per distinct rendered template per incremental case.

    Clusters are over the rendered Jinja template (both branches
    compile from the same template with the same stubs, so a
    template cluster is a compiled-output cluster in both branches),
    restricted to dialects whose strategy gate supports the case.
    """
    cells: list[tuple[str, dict, str, tuple[str, ...]]] = []
    for index, (generator, params) in enumerate(INCREMENTAL_CASES):
        strategy = params["incremental"]["strategy"]
        outputs = {
            dialect_name: get_renderer(generator, dialect_name).render(
                params, DIALECTS[dialect_name], None
            )
            for dialect_name in sorted(DIALECTS)
            if strategy in DIALECTS[dialect_name].incremental_strategies
        }
        case_key = f"{index}:{generator}:{params['name']}:{strategy}"
        for cluster_index, cluster in enumerate(_clusters(outputs)):
            cells.append(
                (
                    generator,
                    params,
                    _representative(cluster, case_key, cluster_index),
                    tuple(cluster),
                )
            )
    return cells


INCREMENTAL_CELLS = _incremental_cells()


@pytest.mark.parametrize(
    "generator,params,dialect_name,cluster",
    INCREMENTAL_CELLS,
    ids=[
        f"{index:03d}-{generator}-{params['incremental']['strategy']}-{dialect_name}"
        for index, (generator, params, dialect_name, _) in enumerate(
            INCREMENTAL_CELLS
        )
    ],
)
def test_incremental_sql_parses_in_both_branches(
    generator: str, params: dict, dialect_name: str, cluster: tuple[str, ...]
) -> None:
    renderer = get_renderer(generator, dialect_name)
    rendered = renderer.render(params, DIALECTS[dialect_name], None)
    assert_parses(compile_sql(rendered), dialect_name)
    assert_parses(compile_sql_incremental(rendered), dialect_name)
    assert dialect_name in cluster
