from __future__ import annotations

from pathlib import Path

from loom.dialects import Dialect
from loom.errors import LoomError
from loom.renderers.base import BaseRenderer


_VARIANTS = ("transaction", "periodic_snapshot", "accumulating_snapshot")


class FactRenderer(BaseRenderer):
    """Renderer for the fact generator.

    Produces Kimball fact table SQL with measure columns,
    foreign keys, and a __loaded_at provenance column.

    Variants (ADR 0031): `transaction` (the default) is the
    original projection. `periodic_snapshot` declares a period grain
    via `period_key`; `accumulating_snapshot` declares the process
    milestones via `milestone_keys`, whose generated tests drop the
    not_null assertion - a milestone key is legitimately null until
    the milestone occurs.

    An optional `incremental` block turns any
    variant incremental in one of two modes. Key mode (`merge` /
    `delete+insert`) reconciles on a declared `unique_key` - the
    fact's grain, which has no default: `dimension_keys` are
    foreign keys, and two fact rows routinely share every one of
    them - with an optional cursor window bounding the scan. Append
    mode requires a `cursor_column` and reads only rows strictly
    after the target's current maximum: a row restated at an
    already-loaded cursor value is not captured, so append mode is
    for append-only event facts. Append is not admitted for
    `accumulating_snapshot`: a milestone completes by updating the
    grain row already loaded (the update-in-place load), which
    only a key strategy expresses. Without the
    block every variant is a full rebuild, as before. The
    cross-field rules live in the inventory validators; the
    renderer composes the shared core's fragments.
    """

    def render(self, params: dict, dialect: Dialect, output_path: Path | None) -> str:
        name = params.get("name", "unknown")
        input_model = (params.get("input_models") or ["unknown"])[0]
        measures = params.get("measure_columns", [])
        dim_keys = params.get("dimension_keys", [])
        variant = params.get("variant", "transaction")
        if variant not in _VARIANTS:
            raise LoomError(
                f"fact variant {variant!r} is not supported; "
                f"expected one of: {', '.join(_VARIANTS)}"
            )

        select_parts = []
        for dk in dim_keys:
            select_parts.append(f"    {dk}")
        select_parts.append(f"    {dialect.current_timestamp} AS __loaded_at")
        for measure in measures:
            # Name-list items (ADR 0037): a string projects the
            # column; a {source, alias} mapping renames it; an
            # {expression, alias} mapping carries verbatim SQL -
            # in the one projection every variant shares, so all
            # three variants honour the forms uniformly.
            if isinstance(measure, str):
                select_parts.append(f"    {measure}")
            elif isinstance(measure, dict):
                alias = measure.get("alias")
                if not isinstance(alias, str):
                    continue
                expression = measure.get("expression")
                if isinstance(expression, str) and expression.strip():
                    select_parts.append(f"    {expression} AS {alias}")
                elif isinstance(measure.get("source"), str):
                    select_parts.append(f"    {measure['source']} AS {alias}")

        select_sql = ",\n".join(select_parts)

        variant_line = ""
        if variant != "transaction":
            variant_line = f"-- Variant: {variant}\n"

        spec = self._incremental_spec(params)
        config = "{{ config(materialized='table') }}"
        # The `filter` parameter (ADR 0038): a verbatim predicate
        # opening the WHERE over the input scan. With an
        # incremental cursor it composes as a single WHERE -
        # filter first, cursor predicate ANDed behind it inside
        # its gate, in both the append (strict) and key-strategy
        # forms (see the prepend after the spec block). Absent,
        # the render is unchanged, byte for byte.
        row_filter = params.get("filter")
        filter_present = isinstance(row_filter, str) and bool(row_filter.strip())
        filter_sql = ""
        if spec is not None:
            self._check_incremental_strategy(spec, dialect, model_name=name)
            if spec.strategy == "append":
                if variant == "accumulating_snapshot":
                    raise LoomError(
                        f"fact {name!r}: incremental strategy "
                        "'append' is not accepted for the "
                        "accumulating_snapshot variant: a milestone "
                        "completes by updating the grain row already "
                        "loaded, and an append would load a second "
                        "copy of the row instead; use 'merge' or "
                        "'delete+insert' (design §3.1)"
                    )
                if spec.unique_key:
                    raise LoomError(
                        f"fact {name!r}: 'unique_key' is not accepted "
                        "with incremental strategy 'append'; append "
                        "mode is guarded by its strict cursor window "
                        "alone (design §3.1)"
                    )
                if spec.cursor_column is None:
                    raise LoomError(
                        f"fact {name!r}: incremental strategy "
                        "'append' requires a 'cursor_column'; append "
                        "mode reads only rows after the target's "
                        "current maximum cursor value (design §3.1)"
                    )
                # Append-guard shapes emit no unique_key in the
                # config (design §2.2): the window, not dbt,
                # bounds what lands.
                config = self._incremental_config(spec)
                filter_sql = self._cursor_predicate(
                    spec, strict=True, conjunction=filter_present
                )
            else:
                if not spec.unique_key:
                    raise LoomError(
                        f"fact {name!r}: incremental strategy "
                        f"{spec.strategy!r} requires a declared "
                        "'unique_key' - dimension keys are foreign "
                        "keys, not a grain, so the block must declare "
                        "the column(s) that identify a fact row "
                        "(design §3.1)"
                    )
                config = self._incremental_config(spec)
                if spec.cursor_column is not None:
                    # Key strategies: the cursor only bounds the
                    # scan; dbt reconciles by key, boundary rows
                    # included.
                    filter_sql = self._cursor_predicate(
                        spec, conjunction=filter_present
                    )

        if filter_present:
            filter_sql = f"\nWHERE {row_filter}" + filter_sql

        sql = (
            f"-- Generated by Loom\n"
            f"-- Generator: fact\n"
            f"-- Paradigm: kimball\n"
            f"{variant_line}"
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
        dim_keys = params.get("dimension_keys", [])
        measures = params.get("measure_columns", [])
        name = params.get("name", "unknown")
        variant = params.get("variant", "transaction")
        milestone_keys = (
            {key.casefold() for key in params.get("milestone_keys", [])}
            if variant == "accumulating_snapshot"
            else set()
        )

        # Tests are generated regardless of the model's name: the fct_
        # prefix is a convention, not a gate.
        col_lines = []
        for fk in dim_keys:
            if fk.casefold() in milestone_keys:
                continue
            col_lines.append(f"      - name: {fk}")
            col_lines.append(f"        tests:")
            col_lines.append(f"          - not_null")
        for measure in measures:
            # The test names the OUTPUT column: a string names
            # itself, a mapping item names its alias (ADR 0037).
            measure_name = (
                measure
                if isinstance(measure, str)
                else measure.get("alias")
                if isinstance(measure, dict)
                else None
            )
            if not isinstance(measure_name, str):
                continue
            col_lines.append(f"      - name: {measure_name}")
            col_lines.append(f"        tests:")
            col_lines.append(f"          - not_null")

        if not col_lines:
            return None

        tests_yaml = (
            "# Generated by Loom\nversion: 2\nmodels:\n"
            f"  - name: {name}\n    columns:\n"
            + "\n".join(col_lines) + "\n"
        )
        return tests_yaml
