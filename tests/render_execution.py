"""In-process DuckDB execution helpers.

Shared by the data-semantic execution tests re-homed here from
``test_generated_sql_execution.py`` when that file was deleted:
render a generator's SQL, compile the dbt Jinja with plain stubs,
and execute the result in DuckDB. Each destination file defines
its own ``conn`` fixture (a fresh in-memory DuckDB connection);
the compile/render helpers live here so the re-homed tests do not
each duplicate them.
"""
from __future__ import annotations

import jinja2

from loom.dialects import DIALECTS
from loom.renderers import get_renderer

DUCKDB = DIALECTS["duckdb"]


def compile_sql(rendered: str) -> str:
    """Compile dbt-flavoured Jinja down to plain DuckDB SQL."""
    environment = jinja2.Environment()
    template = environment.from_string(rendered)
    return template.render(
        config=lambda **kwargs: "",
        ref=lambda name: name,
        source=lambda source_name, table: table,
        is_incremental=lambda: False,
    )


def render(generator: str, params: dict, output_path: object = None) -> str:
    """Render ``generator`` for DuckDB (the execution dialect)."""
    renderer = get_renderer(generator, "duckdb")
    return renderer.render(params, DUCKDB, output_path)  # type: ignore[arg-type]


def materialize(conn, name: str, rendered: str) -> None:
    """Create ``name`` in DuckDB from rendered generator SQL."""
    conn.execute(f"CREATE OR REPLACE TABLE {name} AS {compile_sql(rendered)}")
