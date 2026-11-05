from __future__ import annotations

from pathlib import Path

from loom.dialects import Dialect
from loom.errors import LoomError
from loom.renderers.base import BaseRenderer


class DimensionRenderer(BaseRenderer):
    """Renderer for the dimension generator.

    Produces Kimball dimension table SQL with surrogate key generation
    (ROW_NUMBER), natural key, and hierarchy/attribute columns.

    An optional `incremental` block turns the
    table incremental with `merge` - the only admitted strategy.
    The surrogate is a positional ROW_NUMBER over the full input,
    so a naive incremental batch would number its rows from 1 again
    and collide with (or silently re-point) the surrogates facts
    already reference. Two mechanisms keep existing members'
    surrogates stable: the merge excludes the surrogate column
    from updates (the first-insert mechanism), and in the incremental
    branch new keys are numbered from the target's current maximum
    plus their batch position. `append` and `delete+insert` are
    rejected by name (the inventory validators carry the reason;
    the renderer backstops them). An optional `cursor_column`
    bounds the batch; without one the batch is the full input and
    the saving is write-side only.
    """

    def render(self, params: dict, dialect: Dialect, output_path: Path | None) -> str:
        name = params.get("name", "unknown")
        input_model = (params.get("input_models") or ["unknown"])[0]
        natural_key = params.get("natural_key", "unknown")
        surrogate_key = params.get("surrogate_key", "unknown")
        attributes = params.get("attribute_columns", [])

        spec = self._incremental_spec(
            params, default_key=(natural_key,)
        )
        config = "{{ config(materialized='table') }}"
        # The `filter` parameter (ADR 0038): a verbatim predicate
        # opening the WHERE over the input scan. With an
        # incremental cursor it composes as a single WHERE -
        # filter first, cursor predicate ANDed behind it inside
        # its gate (see the prepend after the spec block). Absent,
        # the render is unchanged, byte for byte.
        row_filter = params.get("filter")
        filter_present = isinstance(row_filter, str) and bool(row_filter.strip())
        filter_sql = ""
        surrogate_expr = (
            f"ROW_NUMBER() OVER (ORDER BY {natural_key})"
        )
        if spec is not None:
            self._check_incremental_strategy(spec, dialect, model_name=name)
            if spec.strategy != "merge":
                raise LoomError(
                    f"dimension {name!r}: incremental strategy "
                    f"{spec.strategy!r} is not supported for "
                    "dimension; use 'merge' (design §3.4 admits "
                    "merge only - append cannot express attribute "
                    "updates, and delete+insert would re-insert "
                    "rows under freshly numbered surrogate keys)"
                )
            if not spec.unique_key:
                raise LoomError(
                    f"dimension {name!r}: incremental strategy "
                    "'merge' requires a unique key - declare "
                    "'unique_key' or a 'natural_key' to default it "
                    "from"
                )
            config = self._incremental_config(
                spec, merge_exclude=(surrogate_key,)
            )
            # Incremental branch only (design §3.4): new keys are
            # numbered from the target's current maximum, so an
            # existing member's surrogate is never re-issued; the
            # first build (is_incremental() false) is exactly the
            # table-mode numbering.
            surrogate_expr = (
                "{% if is_incremental() %}"
                f"(SELECT COALESCE(MAX({surrogate_key}), 0) "
                f"FROM {{{{ this }}}}) + "
                f"ROW_NUMBER() OVER (ORDER BY {natural_key})"
                "{% else %}"
                f"ROW_NUMBER() OVER (ORDER BY {natural_key})"
                "{% endif %}"
            )
            if spec.cursor_column is not None:
                filter_sql = self._cursor_predicate(
                    spec, conjunction=filter_present
                )

        if filter_present:
            filter_sql = f"\nWHERE {row_filter}" + filter_sql

        select_parts = [
            f"    {surrogate_expr} AS {surrogate_key}",
            f"    {natural_key}",
        ]
        for attr in attributes:
            # Name-list items (ADR 0037): a string projects the
            # column; a {source, alias} mapping renames it; an
            # {expression, alias} mapping carries verbatim SQL.
            if isinstance(attr, str):
                select_parts.append(f"    {attr}")
            elif isinstance(attr, dict):
                alias = attr.get("alias")
                if not isinstance(alias, str):
                    continue
                expression = attr.get("expression")
                if isinstance(expression, str) and expression.strip():
                    select_parts.append(f"    {expression} AS {alias}")
                elif isinstance(attr.get("source"), str):
                    select_parts.append(f"    {attr['source']} AS {alias}")
        select_parts.append(f"    {dialect.current_timestamp} AS __loaded_at")

        select_sql = ",\n".join(select_parts)

        sql = (
            f"-- Generated by Loom\n"
            f"-- Generator: dimension\n"
            f"-- Paradigm: kimball\n"
            f"{config}\n"
            f"SELECT\n"
            f"{select_sql}\n"
            f"FROM {{{{ ref('{input_model}') }}}}"
            f"{filter_sql}"
        )
        return sql

    def render_tests(
        self, params: dict, dialect: Dialect, output_path: Path | None
    ) -> str | None:
        natural_key = params.get("natural_key")
        surrogate_key = params.get("surrogate_key")
        name = params.get("name", "unknown")

        # Tests are generated regardless of the model's name: the dim_
        # prefix is a convention, not a gate. The natural key is
        # unique-tested as well as not-null: the renderer assigns
        # surrogate keys by ROW_NUMBER over the natural key, so duplicate
        # natural keys would otherwise produce duplicate dimension rows
        # silently (M-04).
        if not natural_key or not surrogate_key:
            return None

        tests_yaml = (
            f"# Generated by Loom\n"
            f"version: 2\n"
            f"models:\n"
            f"  - name: {name}\n"
            f"    columns:\n"
            f"      - name: {natural_key}\n"
            f"        tests:\n"
            f"          - not_null\n"
            f"          - unique\n"
            f"      - name: {surrogate_key}\n"
            f"        tests:\n"
            f"          - unique\n"
        )
        return tests_yaml
