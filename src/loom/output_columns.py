"""The general output-column resolver.

One source of truth for "which columns does a generated model
output, in order" - derived from each renderer's SELECT construction,
never from the model's data. Consumers: the generated model
properties (catalogue) YAML in ``loom.properties``, the fact/dimension
input validation in ``loom.inventory`` (via
``_derivable_output_columns``), and future given/expect validation
for generated unit tests.

Each column also carries the *generic* data type Loom itself fixes
for it, where Loom's SQL determines the type (hash columns are text,
``__loaded_at`` is a timestamp, a dimension surrogate key is an
integer ROW_NUMBER, SCD2 validity columns are timestamps / a
boolean). Columns whose type comes from practitioner data carry
``fixed_type=None``: Loom has never seen that data and does not
guess. Generic type names are the ``column_types`` vocabulary
(``loom.dialects.render_data_type`` renders them per dialect).

``resolve_output_columns`` returns ``None`` when the output set
cannot be determined from metadata - the ``SELECT *`` generators
(bronze; staging without an explicit ``columns`` list) and chains
that pass an undeterminable input through (silver/gold over bronze).
Callers must treat ``None`` as "unknown", never as "no columns".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class OutputColumn:
    """One output column of a generated model, in SELECT order."""

    name: str
    # Generic type vocabulary name when Loom's own SQL fixes the
    # column's type; None when the type is practitioner-data-defined.
    fixed_type: str | None = None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _dedupe(columns: list[OutputColumn]) -> list[OutputColumn]:
    seen: set[str] = set()
    result = []
    for column in columns:
        key = column.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(column)
    return result


def _input_columns(
    data: Mapping[str, Any],
    key: str,
    by_name: Mapping[str, Mapping[str, Any]] | None,
    seen: frozenset[str],
) -> list[OutputColumn] | None:
    """Output columns of the first entry of a list field, or None."""
    declared = data.get(key)
    input_name = (
        declared[0]
        if isinstance(declared, list) and declared
        else None
    )
    if not isinstance(input_name, str) or by_name is None:
        return None
    target = by_name.get(input_name.casefold())
    if target is None:
        return None
    return resolve_output_columns(target, by_name, seen)


def _satellite_driving_column(data: Mapping[str, Any]) -> str | None:
    """The driving endpoint's hash column for an effectivity satellite.

    The satellite's parent block names the parent link's endpoint
    hubs/keys itself, so the driving hash column is
    derivable from the record alone: the endpoint whose key column
    equals `driving_key` contributes `<hub>_hk`.
    """
    driving_key = data.get("driving_key")
    if not isinstance(driving_key, str):
        return None
    endpoints: list[tuple[Any, Any]] = []
    parent_block = data.get("parent")
    declared = (
        parent_block.get("endpoints")
        if isinstance(parent_block, Mapping)
        else None
    )
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, Mapping):
                endpoints.append((item.get("hub"), item.get("key")))
    for hub, key in endpoints:
        if key == driving_key and isinstance(hub, str) and len(hub) > 4:
            return f"{hub[4:]}_hk"
    return None


def resolve_output_columns(
    data: Mapping[str, Any],
    by_name: Mapping[str, Mapping[str, Any]] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> list[OutputColumn] | None:
    """Output columns of one generated model, or None if undeterminable.

    ``data`` is the expanded instance record (generator params merged,
    including ``name`` and ``generator``). ``by_name`` maps casefolded
    model names to their records and is consulted only by the
    pass-through generators (silver, gold pass-through); a name cycle
    or a missing/undeterminable input yields None rather than a guess.
    """
    name = data.get("name")
    if not isinstance(name, str) or name.casefold() in _seen:
        return None
    seen = _seen | {name.casefold()}
    generator = data.get("generator")

    if generator == "staging":
        columns = data.get("columns")
        if not isinstance(columns, list) or not columns:
            # No explicit projection: the renderer falls back to
            # SELECT * over the source, whose columns Loom cannot
            # enumerate.
            return None
        outputs = [
            OutputColumn(col["alias"])
            for col in columns
            if isinstance(col, dict) and isinstance(col.get("alias"), str)
        ]
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return outputs

    if generator == "hub":
        business_key = data.get("business_key")
        if not isinstance(business_key, str):
            return None
        outputs = [
            OutputColumn(f"{name[4:]}_hk", "text"),
            OutputColumn(business_key),
        ]
        # The renderer emits both audit columns exactly when the
        # hub declares record_source (either form).
        if data.get("record_source"):
            outputs.append(OutputColumn("__record_source", "text"))
            outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return outputs

    if generator == "reference_hub":
        # (ADR 0034) Provenance is mandatory, so both audit
        # columns are unconditional here.
        business_key = data.get("business_key")
        if not isinstance(business_key, str):
            return None
        return [
            OutputColumn(f"{name[4:]}_hk", "text"),
            OutputColumn(business_key),
            OutputColumn("__record_source", "text"),
            OutputColumn("__loaded_at", "timestamp"),
        ]

    if generator == "reference_table":
        # The flattened view: the reference hub's hash + code, the
        # declared satellite attributes, then the satellite row's
        # provenance (renderer order).
        ref_name = data.get("reference_hub")
        business_key = data.get("business_key")
        if not isinstance(ref_name, str) or not isinstance(business_key, str):
            return None
        outputs = [
            OutputColumn(f"{ref_name[4:]}_hk", "text"),
            OutputColumn(business_key),
        ]
        outputs.extend(
            OutputColumn(col) for col in _strings(data.get("attribute_columns"))
        )
        outputs.append(OutputColumn("__record_source", "text"))
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return outputs

    if generator == "link":
        declared_endpoints = data.get("endpoints")
        endpoint_hubs = [
            item.get("hub")
            for item in declared_endpoints
            if isinstance(item, dict)
        ] if isinstance(declared_endpoints, list) else []
        if len(endpoint_hubs) < 2 or not all(
            isinstance(hub, str) for hub in endpoint_hubs
        ):
            return None
        outputs = [OutputColumn(f"{name[5:]}_hk", "text")]
        # (ADR 0030) Every endpoint contributes
        # its hub hash column in declared order; a
        # dependent-child key is stored normalized on the link row;
        # a non-historised link carries its payload columns.
        for hub in endpoint_hubs:
            outputs.append(OutputColumn(f"{hub[4:]}_hk", "text"))
        dependent_key = data.get("dependent_child_key")
        if isinstance(dependent_key, str) and dependent_key:
            outputs.append(OutputColumn(dependent_key))
        outputs.extend(
            OutputColumn(column)
            for column in _strings(data.get("payload_columns"))
        )
        return outputs

    if generator == "satellite":
        # The described model is the satellite table (the standard
        # variant's <name>_current view is an auxiliary output, like
        # the SCD2 snapshot). The parent hash column comes from the
        # parent's own naming rule, so the parent record is required.
        parent_block = data.get("parent")
        parent_name = (
            parent_block.get("model")
            if isinstance(parent_block, dict)
            else None
        )
        if not isinstance(parent_name, str) or by_name is None:
            return None
        parent = by_name.get(parent_name.casefold())
        if parent is None:
            return None
        parent_generator = parent.get("generator")
        if parent_generator in ("hub", "reference_hub"):
            outputs = [OutputColumn(f"{parent_name[4:]}_hk", "text")]
        elif parent_generator == "link":
            outputs = [OutputColumn(f"{parent_name[5:]}_hk", "text")]
        else:
            return None
        variant = data.get("variant", "standard")
        if variant == "standard":
            outputs.append(OutputColumn("__hash_diff", "text"))
            outputs.extend(
                OutputColumn(col)
                for col in _strings(data.get("attribute_columns"))
            )
        elif variant == "multi_active":
            outputs.append(OutputColumn("__hash_diff", "text"))
            subsequence = data.get("subsequence_key")
            if not isinstance(subsequence, str):
                return None
            outputs.append(OutputColumn(subsequence))
            outputs.extend(
                OutputColumn(col)
                for col in _strings(data.get("attribute_columns"))
            )
        elif variant == "effectivity":
            driving_column = _satellite_driving_column(data)
            if driving_column is None:
                return None
            outputs.append(OutputColumn(driving_column, "text"))
        elif variant == "non_historised":
            outputs.extend(
                OutputColumn(col)
                for col in _strings(data.get("attribute_columns"))
            )
        elif variant != "record_tracking":
            return None
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        outputs.append(OutputColumn("__record_source", "text"))
        if variant == "record_tracking":
            outputs.append(OutputColumn("__is_deleted", "boolean"))
        return outputs

    if generator == "pit":
        # (ADR 0035) Parent hash, snapshot date, then one
        # __<satellite>_ldts column per declared satellite.
        parent_block = data.get("parent")
        parent_name = (
            parent_block.get("model")
            if isinstance(parent_block, dict)
            else None
        )
        if not isinstance(parent_name, str) or by_name is None:
            return None
        parent = by_name.get(parent_name.casefold())
        if parent is None:
            return None
        parent_generator = parent.get("generator")
        if parent_generator == "hub":
            hash_column = f"{parent_name[4:]}_hk"
        elif parent_generator == "link":
            hash_column = f"{parent_name[5:]}_hk"
        else:
            return None
        outputs = [
            OutputColumn(hash_column, "text"),
            OutputColumn("__snapshot_date", "timestamp"),
        ]
        outputs.extend(
            OutputColumn(f"__{satellite}_ldts", "timestamp")
            for satellite in _strings(data.get("satellites"))
        )
        return outputs

    if generator == "bridge":
        # (ADR 0036) Anchor hash, each reached hub's hash in
        # path order, the optional weighting factor, build stamp.
        parent_block = data.get("parent")
        anchor = (
            parent_block.get("model")
            if isinstance(parent_block, dict)
            else None
        )
        if not isinstance(anchor, str) or not anchor.startswith("hub_"):
            return None
        outputs = [OutputColumn(f"{anchor[4:]}_hk", "text")]
        steps = data.get("path")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict) and isinstance(step.get("hub"), str):
                    hub_name = step["hub"]
                    if hub_name.startswith("hub_"):
                        outputs.append(OutputColumn(f"{hub_name[4:]}_hk", "text"))
        if data.get("weighting") == "equal":
            outputs.append(OutputColumn("__weighting_factor"))
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return outputs

    if generator == "scd2_dimension":
        business_key = data.get("business_key")
        if not isinstance(business_key, str):
            return None
        # The properties record describes the generated view model
        # (the snapshot is not a model): business key, tracked
        # columns (the renderer drops a tracked repeat of the key),
        # then the validity columns renamed from dbt's snapshot
        # columns. The fact variants reshape the view: type_3 holds
        # current rows only (previous_<column> per declared column,
        # no __valid_to); type_6 adds current_<column> for every
        # tracked column and the previous-value columns to the full
        # history shape.
        variant = data.get("variant", "type_2")
        tracked = [
            col
            for col in _strings(data.get("tracked_columns"))
            if col != business_key
        ]
        previous = _strings(data.get("previous_value_columns"))
        outputs = [OutputColumn(business_key)]
        outputs.extend(OutputColumn(col) for col in tracked)
        if variant == "type_6":
            outputs.extend(OutputColumn(f"current_{col}") for col in tracked)
        if variant in ("type_3", "type_6"):
            outputs.extend(OutputColumn(f"previous_{col}") for col in previous)
        outputs.append(OutputColumn("__valid_from", "timestamp"))
        if variant != "type_3":
            outputs.append(OutputColumn("__valid_to", "timestamp"))
        outputs.append(OutputColumn("__is_deleted", "boolean"))
        return outputs

    if generator == "fact":
        outputs = [OutputColumn(key) for key in _strings(data.get("dimension_keys"))]
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        outputs.extend(
            OutputColumn(measure)
            for measure in _name_list_names(data.get("measure_columns"))
        )
        return outputs

    if generator == "dimension":
        natural_key = data.get("natural_key")
        surrogate_key = data.get("surrogate_key")
        if not isinstance(natural_key, str) or not isinstance(surrogate_key, str):
            return None
        outputs = [
            # The surrogate is a rendered ROW_NUMBER(): BIGINT on
            # DuckDB/Postgres (the contract proof failed
            # preflight on INTEGER) - bigint is the truthful generic.
            OutputColumn(surrogate_key, "bigint"),
            OutputColumn(natural_key),
        ]
        outputs.extend(
            OutputColumn(attr)
            for attr in _name_list_names(data.get("attribute_columns"))
        )
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return _dedupe(outputs)

    if generator == "bronze":
        # SELECT * over a source: the source's columns are not
        # declared in Loom metadata.
        return None

    if generator == "normalized_table":
        outputs = [
            OutputColumn(col)
            for col in _strings(data.get("entity_columns"))
            + _strings(data.get("attribute_columns"))
        ]
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return _dedupe(outputs)

    if generator == "view":
        select_columns = data.get("select_columns")
        if not isinstance(select_columns, list) or not select_columns:
            return None
        outputs = []
        for col in select_columns:
            if not isinstance(col, dict):
                return None
            alias = col.get("alias")
            if "expression" in col:
                # The expression item (ADR 0037): its output
                # column is its (required) alias.
                if not isinstance(alias, str):
                    return None
                outputs.append(OutputColumn(alias))
                continue
            from_ref = col.get("source")
            if not isinstance(from_ref, str) or from_ref.strip() == "*":
                return None
            if not isinstance(alias, str):
                alias = from_ref.split(".")[-1]
            outputs.append(OutputColumn(alias))
        return _dedupe(outputs)

    if generator == "obt":
        joins = data.get("joins")
        if not isinstance(joins, list) or not joins:
            return None
        outputs: list[OutputColumn] = []
        seen_names: set[str] = set()
        # The source model's own declared columns project first
        #: the renderer skips a __loaded_at entry (the
        # fresh stamp is appended last) and exact repeats.
        for col in _name_list_names(data.get("columns")):
            if col.casefold() == "__loaded_at" or col in seen_names:
                continue
            seen_names.add(col)
            outputs.append(OutputColumn(col))
        for idx, join in enumerate(joins):
            if not isinstance(join, dict):
                return None
            for col in _strings(join.get("columns")):
                output_name = col
                if output_name in seen_names:
                    # The renderer aliases a repeated column with its
                    # join alias (j<idx>_<column>).
                    output_name = f"j{idx}_{col}"
                seen_names.add(output_name)
                outputs.append(OutputColumn(output_name))
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return outputs

    if generator == "mart":
        aggregation = data.get("aggregation")
        if not isinstance(aggregation, dict):
            aggregation = {}
        outputs = [
            OutputColumn(col) for col in _group_by_names(aggregation)
        ]
        outputs.extend(
            OutputColumn(alias)
            for measure in _measures(aggregation)
            if (alias:= _measure_alias(measure))
        )
        outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return _dedupe(outputs)

    if generator == "silver":
        # With a dedup key and declared `columns`, the renderer
        # projects exactly those columns, appending the
        # input's __loaded_at when the declaration omits it - the
        # output set is then known without resolving the input.
        declared = data.get("columns")
        if data.get("dedup_key") and isinstance(declared, list) and declared:
            outputs = [
                OutputColumn(col) for col in _name_list_names(declared)
            ]
            if not any(c.name.casefold() == "__loaded_at" for c in outputs):
                outputs.append(OutputColumn("__loaded_at", "timestamp"))
            return outputs
        # Otherwise silver's output set is exactly its input model's
        # output set, in input order (normative
        # for this resolver): the dedup renderer's star-exclusion
        # projection keeps the internal ROW_NUMBER column
        # (rn) out of the output, and pass-through mode selects the
        # input unchanged.
        return _input_columns(data, "input_models", by_name, seen)

    if generator == "gold":
        aggregation = data.get("aggregation")
        if not isinstance(aggregation, dict):
            aggregation = {}
        group_by_names = _group_by_names(aggregation)
        measures = _measures(aggregation)
        if group_by_names or measures:
            # Aggregate mode: grain columns, measure
            # aliases, and a fresh __loaded_at; every other input
            # column is dropped by the aggregation select list.
            outputs = [OutputColumn(col) for col in group_by_names]
            outputs.extend(
                OutputColumn(alias)
                for m in measures
                if (alias:= _measure_alias(m))
            )
            outputs.append(OutputColumn("__loaded_at", "timestamp"))
            return _dedupe(outputs)
        # Pass-through mode: the input's full column set
        # plus a fresh __loaded_at. If the input already carries
        # one, the rendered SQL would name it twice; the catalogue
        # lists the column once.
        inputs = _input_columns(data, "input_models", by_name, seen)
        if inputs is None:
            return None
        outputs = list(inputs)
        if not any(c.name.casefold() == "__loaded_at" for c in outputs):
            outputs.append(OutputColumn("__loaded_at", "timestamp"))
        return outputs

    return None


def generated_system_columns(data: Mapping[str, Any]) -> set[str]:
    """The system columns a generated instance will emit.

    System columns are the fixed-name, generator-managed
    bookkeeping columns (the `__` namespace): their names are
    Loom's, never the practitioner's, and they are never renamed
    per project. The set is instance-conditional - a hub emits
    ``__record_source`` only with declared provenance, a bridge
    emits ``__weighting_factor`` only under equal weighting, a
    PIT emits one ``__<satellite>_ldts`` per declared satellite.
    Hash keys and practitioner-declared names are not system
    columns. Validation uses this set to detect a
    practitioner-authored column colliding with a generated
    system column.
    """
    generator = data.get("generator")
    if generator == "staging":
        return {"__loaded_at"}
    if generator == "hub":
        if data.get("record_source"):
            return {"__record_source", "__loaded_at"}
        return set()
    if generator in ("reference_hub", "reference_table"):
        return {"__record_source", "__loaded_at"}
    if generator == "satellite":
        columns = {"__record_source", "__loaded_at"}
        variant = data.get("variant", "standard")
        if variant in ("standard", "multi_active"):
            columns.add("__hash_diff")
        if variant == "record_tracking":
            columns.add("__is_deleted")
        return columns
    if generator == "pit":
        columns = {"__snapshot_date"}
        columns.update(
            f"__{satellite}_ldts"
            for satellite in _strings(data.get("satellites"))
        )
        return columns
    if generator == "bridge":
        columns = {"__loaded_at"}
        if data.get("weighting") == "equal":
            columns.add("__weighting_factor")
        return columns
    if generator == "scd2_dimension":
        columns = {"__valid_from", "__is_deleted"}
        if data.get("variant", "type_2") != "type_3":
            columns.add("__valid_to")
        return columns
    if generator in ("fact", "dimension", "normalized_table", "mart", "gold"):
        return {"__loaded_at"}
    return set()


def _name_list_names(items: Any) -> list[str]:
    """Output names of a name-list surface's items (silver/obt
    `columns`, ADR 0037), in order.

    A string item names itself; a mapping item ({source, alias}
    or {expression, alias}) names its alias.
    """
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("alias"), str):
            names.append(item["alias"])
    return names


def _group_by_names(aggregation: Mapping[str, Any]) -> list[str]:
    """Output names of an aggregation's group_by items, in order.

    A string item names itself; an expression item (ADR 0037)
    names its alias.
    """
    group_by = aggregation.get("group_by")
    if not isinstance(group_by, list):
        return []
    names: list[str] = []
    for item in group_by:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("alias"), str):
            names.append(item["alias"])
    return names


def _measures(aggregation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    measures = aggregation.get("measures")
    if not isinstance(measures, list):
        return []
    return [m for m in measures if isinstance(m, dict)]


def _measure_alias(measure: Mapping[str, Any]) -> str:
    alias = measure.get("alias", measure.get("column", ""))
    return alias if isinstance(alias, str) else ""
