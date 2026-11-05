from __future__ import annotations

from pathlib import Path

from loom.dialects import Dialect
from loom.renderers.base import BaseRenderer, IncrementalSpec


class HubRenderer(BaseRenderer):
    """Renderer for the hub generator.

    Produces hash-based identity SQL with normalization, deduplication,
    and optional retention/provenance columns.

    A hub reads its staging inputs from `input_models`
    (ADR 0030): with several inputs the multi-source form unions
    the per-input key selects and collapses them to one row per hash,
    the earliest declared input winning a contested key. Provenance
    is the single `record_source` parameter: a string labels every
    input uniformly, a mapping keyed by input model labels each
    feed. The single-input SQL is unchanged from the original
    renderer.
    """

    def render(self, params: dict, dialect: Dialect, output_path: Path | None) -> str:
        business_key = params.get("business_key", "unknown")
        name = params.get("name", "unknown")
        retention = params.get("retention", "current")
        record_source = params.get("record_source")

        hash_column = f"{name[4:]}_hk"  # e.g., hub_customer -> customer_hk

        declared = params.get("input_models")
        input_models = (
            [str(model) for model in declared]
            if isinstance(declared, list)
            else []
        )
        if len(input_models) > 1:
            labels: dict[str, str] = {}
            if isinstance(record_source, dict):
                labels = {
                    str(model): str(label)
                    for model, label in record_source.items()
                }
            elif isinstance(record_source, str):
                labels = {model: record_source for model in input_models}
            return self._render_multi_source(
                input_models,
                business_key,
                hash_column,
                retention,
                labels,
                dialect,
            )

        input_model = input_models[0] if input_models else "unknown"
        if isinstance(record_source, dict):
            record_source = record_source.get(input_model)
        if retention == "persistent":
            return self._render_incremental(
                input_model,
                business_key,
                hash_column,
                record_source,
                dialect,
            )
        else:
            return self._render_rebuilt(
                input_model,
                business_key,
                hash_column,
                record_source,
                dialect,
            )

    def _render_multi_source(
        self,
        input_models: list[str],
        business_key: str,
        hash_column: str,
        retention: str,
        record_sources: dict[str, str],
        dialect: Dialect,
    ) -> str:
        """Union the inputs and collapse to one row per hash.

        Each input contributes a DISTINCT select of (hash, key) plus,
        when the resolved `record_source` labels it, its provenance
        literal and a source ordinal. The combined rows are ranked
        by ordinal within each hash, so the earliest declared input
        wins a contested key deterministically (ADR 0030).
        """
        labels = record_sources
        provenance = bool(labels)
        normalized = self._normalize_key(dialect, business_key)
        hash_expr = self._hash_expression(normalized, dialect)

        selects: list[str] = []
        for ordinal, input_model in enumerate(input_models):
            columns = (
                f"            {hash_expr} AS {hash_column},\n"
                f"            {normalized} AS {business_key}"
            )
            if provenance:
                columns += (
                    f",\n            '{labels[input_model]}' AS __record_source"
                    f",\n            {dialect.current_timestamp} AS __loaded_at"
                )
            columns += f",\n            {ordinal} AS __source_ordinal"
            selects.append(
                f"        SELECT DISTINCT\n{columns}\n"
                f"        FROM {{{{ ref('{input_model}') }}}}\n"
                f"        WHERE {business_key} IS NOT NULL"
            )
        union = "\n        UNION ALL\n".join(selects)

        ranked_provenance = ""
        outer_provenance = ""
        if provenance:
            ranked_provenance = ",\n        __record_source,\n        __loaded_at"
            outer_provenance = ",\n    __record_source,\n    __loaded_at"
        ranked = (
            f"    SELECT\n"
            f"        {hash_column},\n"
            f"        {business_key}"
            f"{ranked_provenance},\n"
            f"        ROW_NUMBER() OVER (\n"
            f"            PARTITION BY {hash_column}\n"
            f"            ORDER BY __source_ordinal\n"
            f"        ) AS __rn\n"
            f"    FROM (\n{union}\n    ) AS combined"
        )
        body = (
            f"SELECT\n"
            f"    {hash_column},\n"
            f"    {business_key}"
            f"{outer_provenance}\n"
            f"FROM (\n{ranked}\n) AS ranked\n"
            f"WHERE __rn = 1"
        )

        header = (
            f"-- Generated by Loom\n"
            f"-- Generator: hub\n"
            f"-- Hash function: {dialect.hash_function}\n"
        )
        if retention != "persistent":
            return (
                header
                + "{{ config(materialized='table') }}\n"
                + body
            )
        if dialect.name == "databricks":
            # As the single-input persistent hub: merge on the
            # hash, provenance columns written on first insert only.
            # The spec carries on_schema_change=None so the emitted
            # config stays byte-identical to the original form
            # (the golden byte-identity gate).
            merge_exclude = (
                ("__record_source", "__loaded_at") if provenance else ()
            )
            spec = IncrementalSpec(
                strategy="merge",
                unique_key=(hash_column,),
                on_schema_change=None,
            )
            config = (
                self._incremental_config(spec, merge_exclude=merge_exclude)
                + "\n"
            )
            return header + config + body
        append_spec = IncrementalSpec(strategy="append", on_schema_change=None)
        return (
            header
            + self._incremental_config(append_spec)
            + "\n"
            + body
            + self._anti_join_filter(hash_column, hash_column)
        )

    def render_tests(
        self, params: dict, dialect: Dialect, output_path: Path | None
    ) -> str | None:
        name = params.get("name", "unknown")
        if not name.startswith("hub_"):
            return None
        hash_column = f"{name[4:]}_hk"
        tests_yaml = (
            f"# Generated by Loom\n"
            f"version: 2\n"
            f"models:\n"
            f"  - name: {name}\n"
            f"    columns:\n"
            f"      - name: {hash_column}\n"
            f"        tests:\n"
            f"          - unique\n"
            f"          - not_null\n"
        )
        return tests_yaml

    def _build_body(
        self,
        input_model: str,
        business_key: str,
        hash_column: str,
        record_source: str | None,
        dialect: Dialect,
        select_clause: str,
        incremental_filter: str = "",
    ) -> str:
        """Build the common SQL body for hub rendering."""
        normalized = self._normalize_key(dialect, business_key)
        hash_expr = self._hash_expression(normalized, dialect)

        extra_columns = ""
        if record_source:
            extra_columns = (
                f",\n    '{record_source}' AS __record_source,"
                f"\n    {dialect.current_timestamp} AS __loaded_at"
            )

        return (
            f"-- Generated by Loom\n"
            f"-- Generator: hub\n"
            f"-- Hash function: {dialect.hash_function}\n"
            f"{select_clause}\n"
            f"    {hash_expr} AS {hash_column},\n"
            f"    {normalized} AS {business_key}"
            f"{extra_columns}\n"
            f"FROM {{{{ ref('{input_model}') }}}}\n"
            f"WHERE {business_key} IS NOT NULL"
            f"{incremental_filter}"
        )

    def _render_rebuilt(
        self,
        input_model: str,
        business_key: str,
        hash_column: str,
        record_source: str | None,
        dialect: Dialect,
    ) -> str:
        return self._build_body(
            input_model,
            business_key,
            hash_column,
            record_source,
            dialect,
            "{{ config(materialized='table') }}\nSELECT DISTINCT",
        )

    def _render_incremental(
        self,
        input_model: str,
        business_key: str,
        hash_column: str,
        record_source: str | None,
        dialect: Dialect,
    ) -> str:
        normalized = self._normalize_key(dialect, business_key)
        hash_expr = self._hash_expression(normalized, dialect)
        if dialect.name == "databricks":
            # Provenance columns are written on first insert only: exclude
            # them from the merge update so a re-observed identity keeps its
            # original __record_source and __loaded_at. The spec
            # carries on_schema_change=None so the emitted config stays
            # byte-identical to the original form (the golden
            # byte-identity gate).
            merge_exclude = (
                ("__record_source", "__loaded_at") if record_source else ()
            )
            spec = IncrementalSpec(
                strategy="merge",
                unique_key=(hash_column,),
                on_schema_change=None,
            )
            select_clause = (
                self._incremental_config(spec, merge_exclude=merge_exclude)
                + "\nSELECT DISTINCT"
            )
            incremental_filter = ""
        else:
            # Every other dialect uses the append form: an incremental
            # append guarded by an is_incremental() anti-join on the
            # hash, so a re-observed identity is never duplicated.
            # Append is the one incremental strategy every supported
            # adapter implements; the form is execution-proven on
            # DuckDB, PostgreSQL and ClickHouse.
            spec = IncrementalSpec(strategy="append", on_schema_change=None)
            select_clause = (
                self._incremental_config(spec) + "\nSELECT DISTINCT"
            )
            incremental_filter = self._anti_join_filter(hash_expr, hash_column)

        return self._build_body(
            input_model,
            business_key,
            hash_column,
            record_source,
            dialect,
            select_clause,
            incremental_filter,
        )
