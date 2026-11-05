from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from loom.dialects import Dialect
from loom.errors import LoomError
from loom.renderers.base import BaseRenderer


class SilverRenderer(BaseRenderer):
    """Renderer for the silver generator.

    Produces a cleaned/deduplicated model using ROW_NUMBER()
    partitioned by dedup_key, ordered by the declared `dedup_order`
    or, by default, __loaded_at DESC. The internal `rn`
    column never reaches the output.
    """

    def render(self, params: dict, dialect: Dialect, output_path: Path | None) -> str:
        name = params.get("name", "unknown")
        input_model = (params.get("input_models") or ["unknown"])[0]
        dedup_key = params.get("dedup_key")
        dedup_order = params.get("dedup_order") or "__loaded_at DESC"

        # An `incremental` block turns the dedup
        # into key-scoped recomputation - the scan window names the
        # touched keys, the batch recomputes the winner over the
        # input's full history for exactly those keys, and dbt
        # writes the batch under the declared key strategy. The
        # winner's __loaded_at travels with the winning input row
        # and is never restamped (§4.2). No block: the table render
        # below is unchanged, byte for byte.
        spec = self._incremental_spec(
            params,
            default_key=(dedup_key,) if isinstance(dedup_key, str) else (),
        )
        config = "{{ config(materialized='table') }}"
        if spec is not None:
            self._check_incremental_strategy(spec, dialect, model_name=name)
            if spec.strategy == "append":
                raise LoomError(
                    f"silver instance {name!r}: incremental strategy "
                    "'append' is not supported for silver: the dedup "
                    "output is the current winner per key - replacement "
                    "semantics - so an append would duplicate keys on "
                    "every rerun; use 'merge' or 'delete+insert' "
                    ""
                )
            if not dedup_key:
                raise LoomError(
                    f"silver instance {name!r}: 'incremental' requires "
                    "a 'dedup_key': pass-through silver has no key "
                    "whose rows an incremental run could recompute "
                    ""
                )
            if spec.unique_key != (dedup_key,):
                raise LoomError(
                    f"silver instance {name!r}: incremental unique_key "
                    f"{list(spec.unique_key)!r} must equal the "
                    f"dedup_key {dedup_key!r}: the batch computes one "
                    "winner per dedup key, so the strategy must "
                    "reconcile on that same key"
                )
            if spec.cursor_column is None:
                # The cursor's dual life (§4.1): it bounds the scan
                # window that names the touched keys. Silver's input
                # carries the load stamp the dedup orders by, so the
                # cursor defaults to __loaded_at; declare
                # cursor_column when the stamp lives elsewhere.
                spec = replace(spec, cursor_column="__loaded_at")
            config = self._incremental_config(spec)

        # The `filter` parameter (ADR 0038): a verbatim predicate
        # over the input scan. On the dedup path it sits inside the
        # ranking subquery, so rows are filtered BEFORE ROW_NUMBER
        # ranks them - the winner is the best row that passes, never
        # the best row overall with the filter applied to the
        # outcome. With an incremental block the composition is a
        # single WHERE: the filter first, the affected-key
        # restriction ANDed behind it inside its gate (the window
        # subquery still names touched keys over the unfiltered
        # input, so a key whose newest rows fall out of the filter
        # is still recomputed over its surviving history). Absent,
        # both renders below are unchanged, byte for byte.
        row_filter = params.get("filter")
        filter_present = isinstance(row_filter, str) and bool(row_filter.strip())
        restriction = ""
        if filter_present:
            restriction = f"\n  WHERE {row_filter}"
        if spec is not None:
            restriction += self._affected_key_filter(
                spec,
                key_column=dedup_key,
                input_ref=f"{{{{ ref('{input_model}') }}}}",
                conjunction=filter_present,
            )

        if dedup_key:
            # The dedup subquery adds an internal ROW_NUMBER column
            # (`rn`); the outer projection must not leak it into the
            # generated model. Projection shapes, in order:
            # declared `columns` (deterministic on every dialect),
            # the dialect's star-exclusion syntax, or a hard error -
            # never a silent leak.
            declared = params.get("columns")
            if declared is not None:
                if not isinstance(declared, list) or not declared:
                    raise LoomError(
                        f"silver instance {name!r}: 'columns' must be a "
                        "non-empty list of the input model's columns when "
                        "declared"
                    )
                # Items: a string projects the input column; a
                # {source, alias} mapping renames it; an
                # {expression, alias} mapping (ADR 0037) carries
                # verbatim SQL - the dedup subquery passes the
                # input's columns through, so all three forms
                # resolve in the outer projection.
                projected: list[str] = []
                projected_names: list[str] = []
                for item in declared:
                    if isinstance(item, str):
                        projected.append(item)
                        projected_names.append(item)
                    elif isinstance(item, dict):
                        alias = item.get("alias")
                        if not isinstance(alias, str):
                            continue
                        expression = item.get("expression")
                        if isinstance(expression, str) and expression.strip():
                            projected.append(f"{expression} AS {alias}")
                        elif isinstance(item.get("source"), str):
                            projected.append(f"{item['source']} AS {alias}")
                        else:
                            continue
                        projected_names.append(alias)
                if "__loaded_at" not in {c.casefold() for c in projected_names}:
                    projected.append("__loaded_at")
                outer_select = ", ".join(projected)
            elif dialect.star_exclude_template:
                outer_select = dialect.star_exclude_template.format(column="rn")
            else:
                raise LoomError(
                    f"silver instance {name!r}: dialect {dialect.name!r} has "
                    "no star-exclusion syntax, so the internal 'rn' dedup "
                    "column cannot be dropped from the output; declare "
                    "'columns' (the input model's columns) so they can be "
                    "projected explicitly"
                )
            sql = (
                f"-- Generated by Loom\n"
                f"-- Generator: silver\n"
                f"-- Paradigm: medallion\n"
                f"{config}\n"
                f"SELECT {outer_select}\n"
                f"FROM (\n"
                f"  SELECT\n"
                f"    *,\n"
                f"    ROW_NUMBER() OVER (PARTITION BY {dedup_key} ORDER BY {dedup_order}) AS rn\n"
                f"  FROM {{{{ ref('{input_model}') }}}}"
                f"{restriction}\n"
                f")\n"
                f"WHERE rn = 1"
            )
        else:
            # No dedup key: pass the input through unchanged (a
            # declared `filter` still restricts the scan - it is a
            # predicate over the input, with or without a dedup).
            # Medallion inputs (bronze/staging) already carry
            # __loaded_at, so appending a second one here produced
            # a duplicate column.
            pass_filter = f"\nWHERE {row_filter}" if filter_present else ""
            sql = (
                f"-- Generated by Loom\n"
                f"-- Generator: silver\n"
                f"-- Paradigm: medallion\n"
                f"{{{{ config(materialized='table') }}}}\n"
                f"SELECT *\n"
                f"FROM {{{{ ref('{input_model}') }}}}"
                f"{pass_filter}"
            )
        return sql

    def render_tests(
        self, params: dict, dialect: Dialect, output_path: Path | None
    ) -> str | None:
        dedup_key = params.get("dedup_key")
        name = params.get("name", "unknown")
        if not dedup_key:
            return None

        tests_yaml = (
            f"# Generated by Loom\n"
            f"version: 2\n"
            f"models:\n"
            f"  - name: {name}\n"
            f"    columns:\n"
            f"      - name: {dedup_key}\n"
            f"        tests:\n"
            f"          - not_null\n"
            f"          - unique\n"
        )
        return tests_yaml
