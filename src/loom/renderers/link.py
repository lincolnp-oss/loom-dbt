from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from loom.dialects import Dialect
from loom.errors import LoomError
from loom.renderers.base import BaseRenderer

_VARIANTS = ("standard", "non_historised")


def _endpoints(params: dict) -> list[tuple[str, str]]:
    """The link's (hub, key) endpoints in declared order (ADR 0030).

    The `endpoints` list carries every endpoint as a {hub, key}
    mapping. The order is load-bearing: the link hash composes the
    endpoint hashes in exactly this order.
    """
    endpoints = []
    declared = params.get("endpoints")
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, dict):
                endpoints.append((item.get("hub"), item.get("key")))
    return [(str(hub), str(key)) for hub, key in endpoints]


def _input_model(params: dict) -> str:
    models = params.get("input_models")
    if isinstance(models, list) and models:
        return str(models[0])
    return "unknown"


def _alias(index: int) -> str:
    """Join alias for an endpoint: l, r, then e3, e4, ...."""
    if index == 0:
        return "l"
    if index == 1:
        return "r"
    return f"e{index + 1}"


class LinkRenderer(BaseRenderer):
    """Renderer for the link generator.

    Produces concatenated hash SQL for relationships between two or
    more hubs (ADR 0030): the `endpoints` list names every
    endpoint as a {hub, key} mapping; the
    link hash composes the endpoint hashes in declared order. An
    optional `dependent_child_key` - a staging column in the link's
    grain that belongs to no hub - joins the hash composition in
    normalized form and is stored normalized on the link row.

    Variants: `standard` (the default) is the rebuilt current-state
    table of ADR 0030. `non_historised` is the
    immutable-event link: it requires a dependent-child key and
    carries `payload_columns` from staging, loading as an
    append-only incremental model that inserts each event once.

    The standard variant accepts an optional `incremental` block:
    `append` guards the load with the shared
    core's anti-join on the link hash; `merge`/`delete+insert`
    reconcile on the link hash as the unique key (the §1.2 default -
    the hash is a deterministic function of the endpoint hashes, so
    it is a stable key at any endpoint arity, dependent-child key
    included). The block is rejected on the non_historised variant:
    that variant already *is* an incremental append load, and a
    second switch for one behaviour is the two-truths shape the
    hub rules exclude.

    The generated model carries the ADR 0030 fail-fast guard:
    a one-row ``__link_guard`` CTE counts input keys
    that are null, blank (empty after key normalization) or
    unmatched in any endpoint hub, and the dialect's
    ``link_guard_template`` gate turns a non-zero count into a
    build failure instead of silently filtering/dropping rows. The
    guard row is the query's driving row (LEFT JOIN to the link
    rows), so the gate is evaluated even when every input row is
    invalid and the link itself would be empty - the case that
    once failed silently. The unmatched check treats an empty hash column
    as unmatched as well as NULL, because ClickHouse fills LEFT
    JOIN misses with default values rather than NULLs. Hash
    expressions in the guard deliberately mirror the join/hash
    expressions of the link query itself.
    """

    def render(self, params: dict, dialect: Dialect, output_path: Path | None) -> str:
        variant = params.get("variant", "standard")
        if variant not in _VARIANTS:
            raise LoomError(
                f"link variant {variant!r} is not supported; "
                f"expected one of: {', '.join(_VARIANTS)}"
            )
        input_model = _input_model(params)
        name = params.get("name", "unknown")
        if variant == "non_historised" and params.get("incremental") is not None:
            raise LoomError(
                f"link {name!r}: the non_historised variant already "
                "loads incrementally (append, insert-once); the "
                "'incremental' block does not apply to it"
            )
        endpoints = _endpoints(params)
        dependent_key = params.get("dependent_child_key")
        payload = [
            str(column) for column in (params.get("payload_columns") or [])
        ]

        link_hash_column = (
            f"{name[5:]}_hk"  # e.g., link_order_customer -> order_customer_hk
        )
        fk_columns = [f"{hub[4:]}_hk" for hub, _ in endpoints]

        # Per-endpoint hashes over the input (alias t), mirroring the
        # hub hash rule; the guard recomputes them over the
        # deduplicated key subquery (alias d).
        hashes = [
            self._hash_expression(
                self._normalize_key(dialect, f"t.{key}"), dialect
            )
            for _, key in endpoints
        ]
        guard_hashes = [
            self._hash_expression(
                self._normalize_key(dialect, f"d.{key}"), dialect
            )
            for _, key in endpoints
        ]

        # The link hash is the dialect hash of the concatenated
        # component hashes, in declared order, with the normalized
        # dependent-child key (when declared) as the final
        # component. The component hashes are fixed-length
        # 64-character hex, so the concatenation is unambiguous
        # (ADR 0030).
        components = list(hashes)
        if isinstance(dependent_key, str) and dependent_key:
            components.append(
                self._normalize_key(dialect, f"t.{dependent_key}")
            )
        link_hash_expr = self._hash_expression(
            self._concat_expression(components, dialect), dialect
        )

        gate = dialect.link_guard_template.replace(
            "{violations}", "g.__invalid_keys + g.__unmatched_keys"
        )

        guard = self._render_guard(
            params, dialect, endpoints, fk_columns, guard_hashes
        )

        # The link rows subquery (alias lnk).
        select_items = [f"{link_hash_expr} AS {link_hash_column}"]
        select_items += [
            f"{hash_expr} AS {fk}"
            for hash_expr, fk in zip(hashes, fk_columns)
        ]
        if isinstance(dependent_key, str) and dependent_key:
            select_items.append(
                f"{self._normalize_key(dialect, f't.{dependent_key}')} "
                f"AS {dependent_key}"
            )
        select_items += [f"t.{column}" for column in payload]
        joins = "".join(
            f"\n            JOIN {{{{ ref('{hub}') }}}} AS {_alias(i)}"
            f"\n                ON {hashes[i]} = {_alias(i)}.{fk_columns[i]}"
            for i, (hub, _) in enumerate(endpoints)
        )
        not_null = [f"t.{key} IS NOT NULL" for _, key in endpoints]
        if isinstance(dependent_key, str) and dependent_key:
            not_null.append(f"t.{dependent_key} IS NOT NULL")
        where = "        WHERE " + not_null[0]
        for condition in not_null[1:]:
            where += f"\n          AND {condition}"
        if variant == "non_historised":
            where += (
                "\n        {% if is_incremental() %}"
                f"\n          AND {link_hash_expr} NOT IN ("
                f"\n            SELECT target.{link_hash_column} "
                f"FROM {{{{ this }}}} AS target"
                "\n          )"
                "\n        {% endif %}"
            )

        # The standard variant's incremental block. The
        # link hash is the default (and only accepted) key; append
        # splices the core's anti-join guard into the link subquery
        # beside the generalised form above, key strategies reconcile
        # through the config alone.
        spec = self._incremental_spec(
            params, default_key=(link_hash_column,)
        )
        if spec is not None:
            self._check_incremental_strategy(spec, dialect, model_name=name)
            if spec.strategy == "append":
                where += self._anti_join_filter(
                    link_hash_expr, link_hash_column
                )
        lnk = (
            "        SELECT DISTINCT\n            "
            + ",\n            ".join(select_items)
            + f"\n        FROM {{{{ ref('{input_model}') }}}} AS t"
            + joins
            + f"\n{where}"
        )

        output_columns = [link_hash_column, *fk_columns]
        if isinstance(dependent_key, str) and dependent_key:
            output_columns.append(dependent_key)
        output_columns += payload
        projected = ",\n        ".join(
            f"lnk.{column} AS {column}" for column in output_columns
        )

        variant_line = ""
        config = "{{ config(materialized='table') }}"
        if spec is not None:
            # Append-guard shapes emit no unique_key in the config
            # (design §2.2): the guard, not dbt, enforces identity.
            config_spec = (
                replace(spec, unique_key=())
                if spec.strategy == "append"
                else spec
            )
            config = self._incremental_config(config_spec)
        if variant == "non_historised":
            variant_line = "-- Variant: non_historised\n"
            config = (
                "{{ config(materialized='incremental', "
                "incremental_strategy='append') }}"
            )

        sql = (
            f"-- Generated by Loom\n"
            f"-- Generator: link\n"
            f"{variant_line}"
            f"-- Hash function: {dialect.hash_function}\n"
            f"-- Link guard (ADR 0030): this model fails at build time when\n"
            f"-- the input contains null, blank or hub-unmatched keys,\n"
            f"-- instead of silently dropping them. On dialects without a\n"
            f"-- message-bearing raise, the failure surfaces as a\n"
            f"-- division-by-zero error from the __link_guard gate.\n"
            f"{config}\n"
            f"{guard}\n"
            f"SELECT {', '.join(output_columns)}\n"
            f"FROM (\n"
            f"    SELECT\n"
            f"        {gate} AS __guard,\n"
            f"        {projected}\n"
            f"    FROM __link_guard AS g\n"
            f"    LEFT JOIN (\n"
            f"{lnk}\n"
            f"    ) AS lnk ON 1 = 1\n"
            f") AS s\n"
            f"WHERE CASE WHEN s.__guard = 1"
            f" THEN s.{link_hash_column} IS NOT NULL"
            f" AND s.{link_hash_column} <> '' ELSE FALSE END"
        )
        return sql

    def _render_guard(
        self,
        params: dict,
        dialect: Dialect,
        endpoints: list[tuple[str, str]],
        fk_columns: list[str],
        guard_hashes: list[str],
    ) -> str:
        input_model = _input_model(params)
        dependent_key = params.get("dependent_child_key")

        invalid = [
            f"t.{key} IS NULL OR "
            f"{self._normalize_key(dialect, f't.{key}')} = ''"
            for _, key in endpoints
        ]
        if isinstance(dependent_key, str) and dependent_key:
            invalid.append(
                f"t.{dependent_key} IS NULL OR "
                f"{self._normalize_key(dialect, f't.{dependent_key}')} = ''"
            )
        invalid_sql = "\n            OR ".join(invalid)

        distinct_keys = ", ".join(
            f"t.{key} AS {key}" for _, key in endpoints
        )
        not_null = " AND ".join(
            f"t.{key} IS NOT NULL" for _, key in endpoints
        )
        guard_joins = "".join(
            f"\n         LEFT JOIN {{{{ ref('{hub}') }}}} AS {_alias(i)}"
            f"\n             ON {guard_hashes[i]} = "
            f"{_alias(i)}.{fk_columns[i]}"
            for i, (hub, _) in enumerate(endpoints)
        )
        unmatched = "\n            OR ".join(
            f"({_alias(i)}.{fk_columns[i]} IS NULL"
            f" OR {_alias(i)}.{fk_columns[i]} = '')"
            for i in range(len(endpoints))
        )
        return (
            f"WITH __link_guard AS (\n"
            f"    SELECT\n"
            f"        (SELECT COUNT(*) FROM {{{{ ref('{input_model}') }}}} AS t\n"
            f"         WHERE {invalid_sql}) AS __invalid_keys,\n"
            f"        (SELECT COUNT(*) FROM (\n"
            f"            SELECT DISTINCT {distinct_keys}\n"
            f"            FROM {{{{ ref('{input_model}') }}}} AS t\n"
            f"            WHERE {not_null}\n"
            f"        ) AS d"
            f"{guard_joins}\n"
            f"         WHERE {unmatched}) AS __unmatched_keys\n"
            f")"
        )

    def render_tests(
        self, params: dict, dialect: Dialect, output_path: Path | None
    ) -> str | None:
        name = params.get("name", "unknown")
        endpoints = _endpoints(params)

        if not name.startswith("link_") or any(
            not hub.startswith("hub_") for hub, _ in endpoints
        ):
            return None

        link_hash = f"{name[5:]}_hk"
        lines = [
            "# Generated by Loom",
            "version: 2",
            "models:",
            f"  - name: {name}",
            "    columns:",
            f"      - name: {link_hash}",
            "        tests:",
            "          - unique",
            "          - not_null",
        ]
        for hub, _ in endpoints:
            fk = f"{hub[4:]}_hk"
            lines += [
                f"      - name: {fk}",
                "        tests:",
                "          - relationships:",
                "              arguments:",
                f"                to: ref('{hub}')",
                f"                field: {fk}",
            ]
        return "\n".join(lines) + "\n"
