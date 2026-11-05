"""Record-level validation for Loom inventory metadata.

Extracted from ``loom.inventory``, which remains the facade consumers
import. These validators check one metadata document at a time - its
envelope, field shapes and per-generator record rules - without
reference to the rest of the project.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import re
from typing import Any

from loom.contracts import (
    GENERIC_TEST_KEYS,
    UNIT_TEST_EXPECT_KEYS,
    UNIT_TEST_GIVEN_KEYS,
    UNIT_TEST_KEYS,
    UNIT_TEST_MACRO_OVERRIDE_KEYS,
    UNIT_TEST_OVERRIDES_KEYS,
    expression_content_errors,
    validate_against_contract,
)
from loom.declared_unit_tests import UNIT_TEST_EXCLUSIONS
from loom.dialects import GENERIC_DATA_TYPES
from loom.generator_registry import GENERATOR_REGISTRY
from loom.inventory import _IDENTIFIER_PATTERN
from loom.output_columns import _name_list_names, generated_system_columns
from loom.output_layout import validate_output_subpath_template

# A document's kind is its directory, never a field it carries:
# metadata/patterns/ holds pattern definitions, metadata/instances/
# holds instance sets, local/registrations/ holds registrations.
# The envelope checks, for the roles that have one, the document
# name; each role's own required shape is checked by its loader.
def _validate_document_envelope(
    path: str, data: Any, role: str
) -> list[str]:
    """Lightweight structural validation for one authoring role:
    the document name, where the role has one."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"{path}: expected a mapping"]
    # name (an instance set has no document-level name)
    if role != "instance_set":
        name = data.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"^[a-z][a-z0-9_]*$", name):
            errors.append(f"{path}: name must be a lowercase snake_case identifier")
    return errors


def _validate_aggregation_keys(
    path: str, aggregation: Any, errors: list[str]
) -> None:
    """The `aggregation` mapping is a closed shape: `group_by`
    and `measures` are its only keys.

    The mapping's own keys were the one unclosed level in the
    gold/mart parameter tree - a key the renderer does not
    read (a nested `filter`, say) passed validation and was
    silently ignored, where the same word at the top level is
    rejected as an unknown parameter. Unknown keys are
    rejected here, naming the accepted set, in the same form
    as the neighbouring item checks.
    """
    if not isinstance(aggregation, dict):
        return
    unknown = sorted(set(aggregation) - {"group_by", "measures"})
    if unknown:
        errors.append(
            f"{path}: aggregation has unknown key(s): "
            + ", ".join(unknown)
            + "; accepted keys are: group_by, measures"
        )


def _validate_measure_expressions(
    path: str, measures: Any, errors: list[str]
) -> None:
    """Every aggregation measure needs a real expression.

    Without one the renderers used to fall back to the measure's alias as
    a bare column reference, which is not valid aggregation SQL.
    A measure is a closed shape too - `expression`, `alias`
    and `column` are its only keys - so an unknown key is
    rejected by name rather than silently ignored (a
    misspelled key used to surface only indirectly, through
    the missing-expression error below).
    """
    if not isinstance(measures, list):
        return
    for i, measure in enumerate(measures):
        if not isinstance(measure, dict):
            errors.append(f"{path}: measures.{i} must be an object")
            continue
        unknown = sorted(set(measure) - {"alias", "column", "expression"})
        if unknown:
            errors.append(
                f"{path}: measures.{i} has unknown key(s): "
                + ", ".join(unknown)
                + "; accepted keys are: alias, column, expression"
            )
        expression = measure.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            errors.append(
                f"{path}: measures.{i} requires a non-empty 'expression'"
            )
        if "alias" not in measure and "column" not in measure:
            errors.append(
                f"{path}: measures.{i} requires an 'alias' (or 'column')"
            )


def _validate_group_by_items(
    path: str, group_by: Any, errors: list[str]
) -> None:
    """Aggregation group_by items: plain column names, or the
    expression item (ADR 0037).

    A mapping item is ``{expression, alias}``: the renderer
    emits ``expression AS alias`` in the select list and the
    expression in GROUP BY. String items are unchanged.
    """
    if not isinstance(group_by, list):
        return
    for i, item in enumerate(group_by):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            errors.append(
                f"{path}: aggregation.group_by[{i}] must be a string "
                "or an object"
            )
            continue
        unknown = sorted(set(item) - {"expression", "alias"})
        if unknown:
            errors.append(
                f"{path}: aggregation.group_by[{i}] has unknown key(s): "
                + ", ".join(unknown)
                + "; accepted keys are: alias, expression"
            )
        errors.extend(
            expression_content_errors(
                path,
                f"aggregation.group_by[{i}].expression",
                item.get("expression"),
            )
        )
        alias = item.get("alias")
        if not isinstance(alias, str) or not _IDENTIFIER_PATTERN.fullmatch(alias):
            errors.append(
                f"{path}: aggregation.group_by[{i}].alias: must be a "
                "safe SQL identifier (an 'alias' is required with "
                "'expression')"
            )


def _validate_name_list_items(
    path: str, field: str, items: Any, errors: list[str]
) -> None:
    """A name-list surface's items (silver/obt `columns`, ADR 0037).

    An item is a string (a plain input column, unchanged), a
    ``{source, alias}`` mapping (a rename), or an
    ``{expression, alias}`` mapping (the expression item, the
    uniform grammar: verbatim, alias required, mutually
    exclusive with `source`). There is no macro slot on these
    surfaces.
    """
    if not isinstance(items, list):
        return
    for i, item in enumerate(items):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            errors.append(
                f"{path}: {field}[{i}] must be a string or an object"
            )
            continue
        unknown = sorted(set(item) - {"source", "alias", "expression"})
        if unknown:
            errors.append(
                f"{path}: {field}[{i}] has unknown key(s): "
                + ", ".join(unknown)
                + "; accepted keys are: alias, expression, source"
            )
        has_source = "source" in item
        has_expression = "expression" in item
        if has_source == has_expression:
            errors.append(
                f"{path}: {field}[{i}]: exactly one of 'source' or "
                "'expression' is required"
            )
        if has_expression:
            errors.extend(
                expression_content_errors(
                    path, f"{field}[{i}].expression", item.get("expression")
                )
            )
        elif has_source:
            source = item.get("source")
            if not isinstance(source, str) or not source.strip():
                errors.append(
                    f"{path}: {field}[{i}].source must be a non-empty string"
                )
        alias = item.get("alias")
        if not isinstance(alias, str) or not _IDENTIFIER_PATTERN.fullmatch(alias):
            errors.append(
                f"{path}: {field}[{i}].alias: must be a safe SQL "
                "identifier (an 'alias' is required on a mapping item)"
            )


def _validate_explicit_joins(
    path: str, input_models: Any, joins: Any, errors: list[str]
) -> None:
    """Multi-input models need explicit join keys for every extra input.

    `joins` entries are ``{model, on: [key, ...]}``, the same shape the
    OBT generator already uses. Without keys the only possible join is a
    cross join, which Loom does not generate.
    """
    if not isinstance(input_models, list) or len(input_models) < 2:
        return
    join_keys: dict[str, list] = {}
    if joins is not None:
        if not isinstance(joins, list):
            errors.append(f"{path}: 'joins' must be a list")
            return
        for i, join in enumerate(joins):
            if not isinstance(join, dict):
                errors.append(f"{path}: joins.{i} must be an object")
                continue
            unknown = sorted(set(join) - {"model", "on"})
            if unknown:
                errors.append(
                    f"{path}: joins.{i} has unknown key(s): "
                    + ", ".join(unknown)
                    + "; accepted keys are: model, on"
                )
            model = join.get("model")
            if not isinstance(model, str) or not model:
                errors.append(f"{path}: joins.{i} missing 'model'")
                continue
            on = join.get("on")
            if not isinstance(on, list) or not on:
                errors.append(f"{path}: joins.{i}.on must be a non-empty list")
                continue
            join_keys[model] = on
    for model in input_models[1:]:
        if isinstance(model, str) and model not in join_keys:
            errors.append(
                f"{path}: input model {model!r} requires an explicit 'joins' "
                "entry with a non-empty 'on' list of join keys"
            )


def _validate_incremental(path: str, data: dict, errors: list[str]) -> None:
    """Cross-field rules for the `incremental` block.

    The contract layer (``loom.contracts``) has already
    checked the block's shape - keys, scalar types, the strategy
    enum and the reserved microbatch / insert_overwrite rejections -
    so this validator assumes a well-shaped block and checks what
    the block means for the generator that declares it: key
    resolvability and membership where the emitted column set is
    knowable from the contract, the append-shape rules, and the
    lookback rule (a lookback requires a cursor and a key strategy:
    with plain append a widened window re-reads rows an append has
    no key to reconcile). Dialect capability is NOT checked here -
    validation is dialect-free; the generate-time gate (§1.6(c))
    owns it. Only the generators whose wave has landed are handled;
    every other generator's contract still rejects the parameter.
    """
    block = data.get("incremental")
    if not isinstance(block, dict):
        return  # the contract layer reports the shape
    strategy = block.get("strategy")
    if strategy not in ("append", "delete+insert", "merge"):
        return  # §1.6(a) already names the strategy problem
    generator = data.get("generator")
    if generator not in (
        "link",
        "bronze",
        "normalized_table",
        "fact",
        "dimension",
        "silver",
        "gold",
    ):
        return
    prefix = f"{path}: incremental"
    raw_key = block.get("unique_key")
    if isinstance(raw_key, str):
        keys = [raw_key]
    elif isinstance(raw_key, list):
        keys = [key for key in raw_key if isinstance(key, str)]
    else:
        keys = []
    cursor = block.get("cursor_column")
    if not isinstance(cursor, str):
        cursor = None
    lookback = block.get("lookback")
    if not isinstance(lookback, int) or isinstance(lookback, bool):
        lookback = 0

    if generator == "link":
        name = data.get("name")
        if data.get("variant", "standard") == "non_historised":
            errors.append(
                f"{prefix} is not accepted for the non_historised "
                "link variant: that variant already loads "
                "incrementally (append, insert-once), so the block "
                "would be a second switch for the same behaviour; "
                "remove the block"
            )
        if cursor is not None or lookback > 0:
            errors.append(
                f"{prefix}.cursor_column is not supported for link: "
                "the link's incremental shapes are the append "
                "anti-join guard and key reconciliation, and the "
                "the link guard scans the full input on every run in "
                "either shape"
            )
        if isinstance(name, str) and keys:
            link_hash = f"{name[5:]}_hk"
            if keys != [link_hash]:
                errors.append(
                    f"{prefix}.unique_key must be the derived link "
                    f"hash column {link_hash!r} when declared; the "
                    "link hash is the link's only key"
                )
        return

    if generator == "bronze":
        # §3.2's structural rule: bronze declares no key and its
        # source columns are unknowable, so membership is unchecked
        # - but the block must carry a guard, and the one column
        # Loom *does* know (bronze's own load stamp, absent from
        # the source) can never be the cursor.
        if cursor == "__loaded_at":
            errors.append(
                f"{prefix}.cursor_column must not be '__loaded_at': "
                "that is bronze's own load stamp, written at insert; "
                "the source table has no such column to bound the "
                "window against"
            )
        if strategy == "append":
            if keys and cursor is not None:
                errors.append(
                    f"{prefix} declares both 'unique_key' and "
                    "'cursor_column' with strategy 'append': the two "
                    "are alternative append guards (the key anti-join "
                    "or the cursor window); declare one"
                )
            elif not keys and cursor is None:
                errors.append(
                    f"{prefix} with strategy 'append' requires a "
                    "guard: declare 'unique_key' (anti-join guard) or "
                    "'cursor_column' (cursor window); an unguarded "
                    "append would duplicate rows on every rerun"
                )
            if lookback > 0:
                errors.append(
                    f"{prefix}.lookback is not accepted with strategy "
                    "'append': a widened window re-reads rows an "
                    "append has no key to reconcile; use a key "
                    "strategy (merge / delete+insert) for "
                    "late-arriving data"
                )
        else:
            if not keys:
                errors.append(
                    f"{prefix}.unique_key is required for strategy "
                    f"{strategy!r}: bronze declares no key of its "
                    "own, so the block must declare the source "
                    "column(s) that identify a row"
                )
            if lookback > 0 and cursor is None:
                errors.append(
                    f"{prefix}.lookback requires 'cursor_column': "
                    "the lookback widens the cursor window, and "
                    "there is no window without a cursor"
                )
        return

    if generator == "fact":
        # §3.1: fact has no default key - dimension_keys are foreign
        # keys, not a grain - so the two modes carry distinct rules.
        # Key mode requires a declared unique_key from the emitted
        # set; append mode requires a cursor on an emitted column,
        # takes no key (the strict window is its guard) and no
        # lookback. The fact variants share the emitted set
        # (period_key and milestone_keys are dimension_keys members),
        # so they follow the same rules with one exception, checked
        # first: an accumulating snapshot's milestone completes by
        # updating the grain row already loaded, which append cannot
        # express.
        variant = data.get("variant", "transaction")
        if variant == "accumulating_snapshot" and strategy == "append":
            errors.append(
                f"{prefix}.strategy 'append' is not accepted for "
                "the accumulating_snapshot fact variant: a milestone "
                "completes by updating the grain row already loaded, "
                "and an append would load a second copy of the row "
                "instead; use a key strategy (merge / delete+insert)"
            )
            return
        dim_keys = data.get("dimension_keys")
        measures = data.get("measure_columns")
        emitted = [
            column
            for column in (dim_keys if isinstance(dim_keys, list) else [])
            if isinstance(column, str)
        ] + _name_list_names(measures)
        if strategy == "append":
            if keys:
                errors.append(
                    f"{prefix}.unique_key is not accepted with "
                    "strategy 'append' for fact: append mode is "
                    "guarded by its strict cursor window alone - a "
                    "key belongs to the key strategies (merge / "
                    "delete+insert)"
                )
            if cursor is None:
                errors.append(
                    f"{prefix} with strategy 'append' requires "
                    "'cursor_column': append mode reads only rows "
                    "after the target's current maximum cursor "
                    "value, and there is no window without a cursor"
                )
            elif cursor not in emitted + ["__loaded_at"]:
                errors.append(
                    f"{prefix}.cursor_column {cursor!r} is not one "
                    "of the columns this generator emits "
                    "(dimension_keys, measure_columns, __loaded_at)"
                )
            if lookback > 0:
                errors.append(
                    f"{prefix}.lookback is not accepted with strategy "
                    "'append': a widened window re-reads rows an "
                    "append has no key to reconcile; use a key "
                    "strategy (merge / delete+insert) for "
                    "late-arriving data"
                )
        else:
            if not keys:
                errors.append(
                    f"{prefix}.unique_key is required for strategy "
                    f"{strategy!r}: dimension keys are foreign keys, "
                    "not a grain - two fact rows routinely share "
                    "every dimension key - so declare the column(s) "
                    "that identify a fact row (the grain)"
                )
            else:
                unknown = [key for key in keys if key not in emitted]
                if unknown:
                    errors.append(
                        f"{prefix}.unique_key names column(s) the "
                        "generator does not emit: "
                        + ", ".join(unknown)
                        + "; the key must come from dimension_keys / "
                        "measure_columns"
                    )
            if cursor is not None and cursor not in emitted + [
                "__loaded_at"
            ]:
                errors.append(
                    f"{prefix}.cursor_column {cursor!r} is not one "
                    "of the columns this generator emits "
                    "(dimension_keys, measure_columns, __loaded_at)"
                )
            if lookback > 0 and cursor is None:
                errors.append(
                    f"{prefix}.lookback requires 'cursor_column': "
                    "the lookback widens the cursor window, and "
                    "there is no window without a cursor"
                )
        return

    if generator == "dimension":
        # §3.4: merge only. The surrogate is a positional
        # ROW_NUMBER over the full input, so append cannot express
        # attribute updates and delete+insert would re-insert rows
        # under freshly numbered surrogates, silently re-pointing
        # the facts that reference them; merge is the one strategy
        # whose update path can exclude the surrogate. The key
        # defaults to the natural key.
        if strategy != "merge":
            errors.append(
                f"{prefix}.strategy {strategy!r} is not accepted "
                "for dimension: design §3.4 admits 'merge' only - "
                "append cannot express attribute updates, and "
                "delete+insert would re-insert rows under freshly "
                "numbered surrogate keys, silently re-pointing the "
                "facts that reference them"
            )
            return
        natural_key = data.get("natural_key")
        surrogate_key = data.get("surrogate_key")
        attributes = data.get("attribute_columns")
        members = [
            column
            for column in [natural_key, surrogate_key]
            if isinstance(column, str)
        ] + _name_list_names(attributes)
        resolved = keys or (
            [natural_key] if isinstance(natural_key, str) else []
        )
        if not resolved:
            errors.append(
                f"{prefix}.unique_key is required for strategy "
                "'merge': declare 'unique_key' or a 'natural_key' "
                "to default the key from"
            )
        else:
            unknown = [key for key in resolved if key not in members]
            if unknown:
                errors.append(
                    f"{prefix}.unique_key names column(s) the "
                    "generator does not emit: "
                    + ", ".join(unknown)
                    + "; the key must come from the natural key, "
                    "the surrogate key or attributes"
                )
        if cursor is not None and cursor not in members + [
            "__loaded_at"
        ]:
            errors.append(
                f"{prefix}.cursor_column {cursor!r} is not one of "
                "the columns this generator emits (natural_key, "
                "surrogate_key, attributes, __loaded_at)"
            )
        if lookback > 0 and cursor is None:
            errors.append(
                f"{prefix}.lookback requires 'cursor_column': the "
                "lookback widens the cursor window, and there is no "
                "window without a cursor"
            )
        return

    if generator == "silver":
        # §4: key-scoped recomputation. The batch computes one
        # winner per dedup_key, so the strategy must reconcile on
        # that same identity; append is rejected by name (the
        # output is replacement semantics - one current winner per
        # key - so an append would duplicate keys on every rerun).
        # The cursor names the touched keys rather than loading
        # rows itself, and it defaults to __loaded_at, so no
        # lookback-requires-cursor rule applies (the §1.6(b)
        # medallion exception); the input's columns are unknowable
        # from metadata, so no key/cursor membership is checked.
        dedup_key = data.get("dedup_key")
        if not isinstance(dedup_key, str) or not dedup_key:
            errors.append(
                f"{prefix} requires a 'dedup_key': incremental "
                "silver recomputes the winner per dedup key, and "
                "pass-through silver (no dedup_key) has no key "
                "whose rows an incremental run could recompute "
                ""
            )
            return
        if strategy == "append":
            errors.append(
                f"{prefix}.strategy 'append' is not accepted for "
                "silver: design §4.3 admits key strategies only "
                "(merge / delete+insert) - the dedup output is the "
                "current winner per key, replacement semantics, so "
                "an append would duplicate keys on every rerun"
            )
            return
        if keys and keys != [dedup_key]:
            errors.append(
                f"{prefix}.unique_key must equal the dedup_key "
                f"{dedup_key!r} when declared: the batch computes "
                "one winner per dedup key, so the strategy must "
                "reconcile on that same key"
            )
        return

    if generator == "gold":
        # §5: grain-period recomputation, aggregate mode only. The
        # grain is the aggregation's group_by tuple; the cursor
        # over the driving (first) input is required - it names
        # the touched grains - and, per §1.1's cursor principle,
        # it must be a column the model emits, because the window
        # bound is the target's maximum cursor value (gold's
        # emitted set is knowable: group_by ∪ measure aliases ∪
        # __loaded_at). A declared unique_key must equal the
        # group_by set: the batch computes one row per grain, so
        # the write strategy must reconcile on that same identity.
        # Append is rejected by name: a recomputed grain replaces
        # the stored grain row, which only a key strategy
        # expresses. No lookback rule is needed - the cursor is
        # mandatory in this mode, and the lookback widens its
        # window.
        aggregation = data.get("aggregation")
        if not isinstance(aggregation, dict):
            aggregation = {}
        raw_grain = aggregation.get("group_by")
        grain = (
            [column for column in raw_grain if isinstance(column, str)]
            if isinstance(raw_grain, list)
            else []
        )
        raw_measures = aggregation.get("measures")
        measures = (
            [measure for measure in raw_measures if isinstance(measure, dict)]
            if isinstance(raw_measures, list)
            else []
        )
        if not grain:
            if measures:
                errors.append(
                    f"{prefix} is not accepted for a grand total "
                    "(measures with no aggregation.group_by): the "
                    "grain would be the whole table, so every run "
                    "would recompute everything and 'incremental' "
                    "would be a label on a full rebuild; keep table "
                    "mode, which is honest about the cost"
                )
            else:
                errors.append(
                    f"{prefix} is not accepted for pass-through "
                    "gold (no aggregation): pass-through has no "
                    "grain and no key - its output is the input's "
                    "column set - so there is no grain an "
                    "incremental run could recompute; add an "
                    "aggregation with a group_by to declare the "
                    "grain"
                )
            return
        if strategy == "append":
            errors.append(
                f"{prefix}.strategy 'append' is not accepted for "
                "gold: design §5.3 admits the key strategies only "
                "(merge / delete+insert) - a recomputed grain "
                "replaces the stored grain row, and an append "
                "would load a second copy of every touched grain "
                "on each rerun"
            )
            return
        if keys and set(keys) != set(grain):
            errors.append(
                f"{prefix}.unique_key must equal the aggregation "
                "group_by set ("
                + ", ".join(grain)
                + ", in any order) when declared: the batch "
                "computes one row per grain, so the strategy must "
                "reconcile on that same grain"
            )
        if cursor is None:
            errors.append(
                f"{prefix} requires 'cursor_column': the cursor "
                "over the driving input names the grains each run "
                "recomputes, and gold has no default cursor - "
                "declare the driving input's event or load column "
                ""
            )
        else:
            aliases = [
                alias
                for measure in measures
                for alias in [measure.get("alias", measure.get("column"))]
                if isinstance(alias, str)
            ]
            if cursor not in grain + aliases + ["__loaded_at"]:
                errors.append(
                    f"{prefix}.cursor_column {cursor!r} is not one "
                    "of the columns this generator emits "
                    "(group_by, measure aliases, __loaded_at): "
                    "the window bound is the target's maximum "
                    "cursor value, so the cursor must be a column "
                    "of the target"
                )
        return

    # normalized_table (§3.5): key strategies only, key resolved
    # against the declared column set.
    entity = data.get("entity_columns")
    attribute = data.get("attribute_columns")
    declared = [
        column
        for column in (
            (entity if isinstance(entity, list) else [])
            + (attribute if isinstance(attribute, list) else [])
        )
        if isinstance(column, str)
    ]
    if strategy == "append":
        errors.append(
            f"{prefix}.strategy 'append' is not accepted for "
            "normalized_table: design §3.5 admits key strategies "
            "only (merge / delete+insert) - an append cannot apply "
            "attribute corrections to an existing entity row"
        )
        return
    resolved = keys or declared[:1]
    if not resolved:
        errors.append(
            f"{prefix}.unique_key is required for strategy "
            f"{strategy!r}: declare 'unique_key' or at least one "
            "entity column to default the key from"
        )
    else:
        unknown = [key for key in resolved if key not in declared]
        if unknown:
            errors.append(
                f"{prefix}.unique_key names column(s) the generator "
                "does not emit: "
                + ", ".join(unknown)
                + "; the key must come from entity_columns / "
                "attribute_columns"
            )
    if cursor is not None and cursor not in declared + ["__loaded_at"]:
        errors.append(
            f"{prefix}.cursor_column {cursor!r} is not one of the "
            "columns this generator emits (entity_columns, "
            "attribute_columns, __loaded_at)"
        )
    if lookback > 0 and cursor is None:
        errors.append(
            f"{prefix}.lookback requires 'cursor_column': the "
            "lookback widens the cursor window, and there is no "
            "window without a cursor"
        )


_DECIMAL_TYPE_RE = re.compile(r"^decimal\(\s*\d+\s*,\s*\d+\s*\)$")


def _validate_column_documentation_shapes(path: str, data: Any) -> list[str]:
    """Record-level shape checks for column_docs / column_types.

    The contracts layer checks only the top-level kind (mapping);
    the contents are column-name -> string mappings, and column_types
    values must come from the generic type vocabulary
    (loom.dialects). Whether the named columns actually exist on the
    model is the cross-record check in
    _validate_column_documentation.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return errors
    for field in ("column_docs", "column_types"):
        mapping = data.get(field)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            continue  # the contract layer already reports the kind
        for key, value in mapping.items():
            if not isinstance(key, str):
                errors.append(
                    f"{path}: {field} keys must be column names (strings)"
                )
                continue
            if field == "column_docs":
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{path}: column_docs.{key} must be a non-empty string"
                    )
            else:
                if not isinstance(value, str):
                    errors.append(
                        f"{path}: column_types.{key} must be a string naming "
                        "a generic data type"
                    )
                    continue
                normalised = value.strip().lower()
                if normalised in GENERIC_DATA_TYPES and normalised != "decimal":
                    continue
                if _DECIMAL_TYPE_RE.match(normalised):
                    continue
                accepted = ", ".join(sorted(GENERIC_DATA_TYPES))
                errors.append(
                    f"{path}: column_types.{key} {value!r} is not a generic "
                    f"data type; accepted types are: {accepted} "
                    "(decimal must be written decimal(precision,scale))"
                )
    return errors


def _unit_test_rows_errors(
    path: str, label: str, where: str, fmt: Any, rows: Any
) -> list[str]:
    """Shape checks for one given/expect `rows` value.

    dict rows: a list of mappings whose values are YAML scalars
    (dates must be quoted strings - an unquoted YAML date parses to
    a date object, and the emitter will not guess a rendering).
    csv rows: one string whose header parses and whose rows share
    the header's field count. The scaffold sentinel `__TODO__` is
    rejected wherever it survives.
    """
    errors: list[str] = []
    if fmt not in ("dict", "csv"):
        return [
            f"{path}: {where}.format {fmt!r} is not supported; "
            "accepted formats are: csv, dict"
        ]
    if fmt == "csv":
        if not isinstance(rows, str):
            return [
                f"{path}: {where}.rows must be a single CSV string with "
                "a header line when format is 'csv'"
            ]
        if "__TODO__" in rows:
            errors.append(
                f"{path}: unit test {label} still contains the scaffold "
                f"sentinel '__TODO__' in {where}.rows; replace it with "
                "real fixture values (or delete the row)"
            )
        parsed = [row for row in csv.reader(io.StringIO(rows)) if row]
        if not parsed or not any(cell.strip() for cell in parsed[0]):
            errors.append(
                f"{path}: {where}.rows must start with a header line "
                "naming the columns"
            )
        elif any(len(row) != len(parsed[0]) for row in parsed[1:]):
            errors.append(
                f"{path}: {where}.rows must give every row the same "
                f"number of fields as the header ({len(parsed[0])})"
            )
        return errors
    if not isinstance(rows, list):
        return [f"{path}: {where}.rows must be a list of row mappings"]
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(
                f"{path}: {where}.rows[{index}] must be a mapping of "
                "column names to scalar values"
            )
            continue
        for key, value in row.items():
            if not isinstance(key, str):
                errors.append(
                    f"{path}: {where}.rows[{index}] keys must be column "
                    "names (strings); quote keys that YAML would parse "
                    "as another type"
                )
                continue
            if isinstance(value, str) and value == "__TODO__":
                errors.append(
                    f"{path}: unit test {label} still contains the "
                    f"scaffold sentinel '__TODO__' at {where}.rows[{index}]"
                    f".{key}; replace it with a real fixture value "
                    "(or delete the row)"
                )
            elif isinstance(value, (dict, list)):
                errors.append(
                    f"{path}: {where}.rows[{index}].{key} must be a scalar "
                    "value; nested lists and mappings are not supported "
                    "in unit-test fixtures"
                )
            elif isinstance(value, (datetime.datetime, datetime.date)):
                errors.append(
                    f"{path}: {where}.rows[{index}].{key} is an unquoted "
                    "date/timestamp; quote it as an ISO string so the "
                    "fixture means the same value on every adapter"
                )
            elif value is not None and not isinstance(
                value, (str, int, float, bool)
            ):
                errors.append(
                    f"{path}: {where}.rows[{index}].{key} must be a scalar "
                    "value (string, number, boolean or null)"
                )
    return errors


def _reserved_model_kwarg_errors(
    path: str, where: str, kwargs: dict
) -> list[str]:
    """The reserved-`model` rejection for declared test kwargs.

    dbt injects the model reference into every generic test's
    arguments itself and reserves the name: a declaration that
    supplies `model` (directly, or inside the `arguments`
    wrapper) renders into the generated properties and fails
    `dbt parse` with 'Test arguments include "model", which
    is a reserved argument'. Convert's carriage strips the
    key from manifest copies for this reason; a hand
    declaration is rejected here instead, so the mistake is
    named at validate time rather than at parse time. Only
    `model` is rejected: `column_name` in kwargs is accepted
    by dbt parse (it is redundant beside the declaration's
    own column, not reserved), and every other kwarg passes
    through unexamined, as carriage discipline requires.
    """
    errors: list[str] = []
    places = [("kwargs", kwargs)]
    arguments = kwargs.get("arguments")
    if isinstance(arguments, dict):
        places.append(("kwargs.arguments", arguments))
    for place, mapping in places:
        if "model" in mapping:
            errors.append(
                f"{path}: {where}.{place} declares 'model', "
                "which dbt reserves as a test argument: dbt "
                "supplies the model reference itself and fails "
                "the build when a declaration supplies it "
                "('Test arguments include \"model\", which is "
                "a reserved argument') - remove the 'model' "
                "key; the test attaches to this instance's "
                "model and its declared column as written"
            )
    return errors


def _validate_generic_tests(path: str, data: Any) -> list[str]:
    """Record-level shape validation for `generic_tests`.

    Carriage discipline: Loom validates the declaration's SHAPE
    only - the test's name form, its optional column, and that
    kwargs/config are mappings. The test name is never checked
    against any registry (dbt resolves test names from installed
    packages at build), and kwargs/config contents pass through
    unexamined, verbatim. Two content rules stand: the
    create-new sentinel (a `__TODO__` kwargs key is the
    unfinished snippet's marker and is rejected, so a snippet
    can never validate half-written) and the reserved `model`
    kwarg (dbt rejects it at parse; see
    `_reserved_model_kwarg_errors`).
    """
    errors: list[str] = []
    tests = data.get("generic_tests") if isinstance(data, dict) else None
    if tests is None:
        return errors
    if not isinstance(tests, list):
        return errors  # the contract layer already reports the kind
    if not tests:
        return [
            f"{path}: 'generic_tests' must be a non-empty list when declared"
        ]
    seen: dict[tuple, int] = {}
    for index, test in enumerate(tests):
        where = f"generic_tests[{index}]"
        if not isinstance(test, dict):
            errors.append(f"{path}: {where} must be a mapping")
            continue
        unknown = sorted(set(test) - GENERIC_TEST_KEYS)
        if unknown:
            errors.append(
                f"{path}: {where} has unknown key(s): "
                + ", ".join(unknown)
                + "; accepted keys are: "
                + ", ".join(sorted(GENERIC_TEST_KEYS))
            )
        name = test.get("test")
        if not isinstance(name, str) or not name:
            errors.append(f"{path}: {where}.test is required (a string)")
        elif not _MACRO_REFERENCE_PATTERN.fullmatch(name):
            errors.append(
                f"{path}: {where}.test {name!r} is not a safe test "
                "reference (a test name, or package.name)"
            )
        column = test.get("column_name")
        if column is not None and (
            not isinstance(column, str)
            or not _IDENTIFIER_PATTERN.fullmatch(column)
        ):
            errors.append(
                f"{path}: {where}.column_name {column!r} must be a "
                "safe SQL identifier"
            )
        kwargs = test.get("kwargs")
        if kwargs is not None:
            if not isinstance(kwargs, dict):
                errors.append(f"{path}: {where}.kwargs must be a mapping")
            elif "__TODO__" in kwargs:
                errors.append(
                    f"{path}: {where}.kwargs carries the '__TODO__' "
                    "sentinel - replace it with the test's real "
                    "arguments, as its definition declares them"
                )
            if isinstance(kwargs, dict):
                errors.extend(
                    _reserved_model_kwarg_errors(path, where, kwargs)
                )
        config = test.get("config")
        if config is not None and not isinstance(config, dict):
            errors.append(f"{path}: {where}.config must be a mapping")
        if isinstance(name, str) and name:
            key = (
                name.casefold(),
                column.casefold() if isinstance(column, str) else None,
                json.dumps(kwargs, sort_keys=True, default=str),
                json.dumps(config, sort_keys=True, default=str),
            )
            if key in seen:
                errors.append(
                    f"{path}: {where} exactly duplicates "
                    f"generic_tests[{seen[key]}] (same test, column, "
                    "kwargs and config) - a duplicated declaration "
                    "can only be a copy-paste mistake; differing "
                    "kwargs on the same test and column are fine"
                )
            else:
                seen[key] = index
    return errors


def _validate_unit_tests(path: str, data: Any) -> list[str]:
    """Record-level shape validation for `unit_tests`.

    The contracts layer checks only the top-level kind (list). The
    cross-record checks (name uniqueness, upstream resolution,
    column subsets, the persistent-hub declaration) live in
    `_validate_unit_test_references`, in the relationship phase.
    """
    errors: list[str] = []
    tests = data.get("unit_tests") if isinstance(data, dict) else None
    if tests is None:
        return errors
    if not isinstance(tests, list):
        return errors  # the contract layer already reports the kind
    if not tests:
        return [
            f"{path}: 'unit_tests' must be a non-empty list when declared"
        ]
    for index, test in enumerate(tests):
        where = f"unit_tests[{index}]"
        if not isinstance(test, dict):
            errors.append(f"{path}: {where} must be a mapping")
            continue
        unknown = sorted(set(test) - UNIT_TEST_KEYS)
        if unknown:
            errors.append(
                f"{path}: {where} has unknown key(s): "
                + ", ".join(unknown)
                + "; accepted keys are: "
                + ", ".join(sorted(UNIT_TEST_KEYS))
            )
        name = test.get("name")
        label = repr(name) if isinstance(name, str) else f"at {where}"
        if not isinstance(name, str) or not name:
            errors.append(f"{path}: {where}.name is required (a string)")
        elif not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            errors.append(
                f"{path}: {where}.name {name!r} must be a lowercase "
                "snake_case identifier"
            )
        description = test.get("description")
        if description is not None and not isinstance(description, str):
            errors.append(f"{path}: {where}.description must be a string")
        given = test.get("given")
        if given is None:
            errors.append(f"{path}: {where} requires 'given'")
        elif not isinstance(given, list):
            errors.append(f"{path}: {where}.given must be a list")
        elif not given:
            errors.append(f"{path}: {where}.given must be a non-empty list")
        else:
            for g_index, given_item in enumerate(given):
                g_where = f"{where}.given[{g_index}]"
                if not isinstance(given_item, dict):
                    errors.append(f"{path}: {g_where} must be a mapping")
                    continue
                g_unknown = sorted(set(given_item) - UNIT_TEST_GIVEN_KEYS)
                if g_unknown:
                    errors.append(
                        f"{path}: {g_where} has unknown key(s): "
                        + ", ".join(g_unknown)
                        + "; accepted keys are: "
                        + ", ".join(sorted(UNIT_TEST_GIVEN_KEYS))
                    )
                if not isinstance(given_item.get("input"), str) or not given_item.get(
                    "input"
                ):
                    hint = (
                        "the name of the model's source table"
                        if data.get("generator") in ("staging", "bronze")
                        else "the name of an upstream model in the inventory"
                    )
                    errors.append(
                        f"{path}: {g_where}.input is required ({hint})"
                    )
                if "rows" not in given_item:
                    errors.append(f"{path}: {g_where} requires 'rows'")
                else:
                    errors.extend(
                        _unit_test_rows_errors(
                            path,
                            label,
                            g_where,
                            given_item.get("format", "dict"),
                            given_item.get("rows"),
                        )
                    )
        expect = test.get("expect")
        if expect is None:
            errors.append(f"{path}: {where} requires 'expect'")
        elif not isinstance(expect, dict):
            errors.append(f"{path}: {where}.expect must be a mapping")
        else:
            e_unknown = sorted(set(expect) - UNIT_TEST_EXPECT_KEYS)
            if e_unknown:
                errors.append(
                    f"{path}: {where}.expect has unknown key(s): "
                    + ", ".join(e_unknown)
                    + "; accepted keys are: "
                    + ", ".join(sorted(UNIT_TEST_EXPECT_KEYS))
                )
            if "rows" not in expect:
                errors.append(f"{path}: {where}.expect requires 'rows'")
            else:
                errors.extend(
                    _unit_test_rows_errors(
                        path,
                        label,
                        f"{where}.expect",
                        expect.get("format", "dict"),
                        expect.get("rows"),
                    )
                )
        overrides = test.get("overrides")
        if overrides is not None:
            if not isinstance(overrides, dict):
                errors.append(f"{path}: {where}.overrides must be a mapping")
            else:
                o_unknown = sorted(set(overrides) - UNIT_TEST_OVERRIDES_KEYS)
                if o_unknown:
                    errors.append(
                        f"{path}: {where}.overrides has unknown key(s): "
                        + ", ".join(o_unknown)
                        + "; accepted keys are: "
                        + ", ".join(sorted(UNIT_TEST_OVERRIDES_KEYS))
                    )
                macros = overrides.get("macros")
                if macros is not None:
                    if not isinstance(macros, dict):
                        errors.append(
                            f"{path}: {where}.overrides.macros must be a mapping"
                        )
                    else:
                        m_unknown = sorted(
                            set(macros) - UNIT_TEST_MACRO_OVERRIDE_KEYS
                        )
                        if m_unknown:
                            errors.append(
                                f"{path}: {where}.overrides.macros has "
                                "unknown key(s): "
                                + ", ".join(m_unknown)
                                + "; the only macro override supported in "
                                "v1 is: is_incremental"
                            )
                        if "is_incremental" in macros and not isinstance(
                            macros["is_incremental"], bool
                        ):
                            errors.append(
                                f"{path}: {where}.overrides.macros."
                                "is_incremental must be true or false"
                            )
    return errors


def _authored_output_names(data: dict) -> list[str]:
    """Practitioner-authored names that become output columns.

    Only the declaration fields whose values CREATE output
    columns on the model itself. Consumer generators are out of
    scope: a dimension/fact/reference_table declaration naming
    an upstream system column is a reference to that column,
    not an authored namesake (the resolver dedupes the carried
    column - pinned behaviour for vault-core consumers).
    Derived names (hash keys, `previous_<col>`) and
    pass-through declarations the renderers treat as carry
    markers (silver/obt `__loaded_at`) are likewise not
    authored names for collision purposes.
    """
    generator = data.get("generator")
    names: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            names.append(value)

    def add_all(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)

    if generator == "staging":
        columns = data.get("columns")
        if isinstance(columns, list):
            for column in columns:
                if isinstance(column, dict):
                    add(column.get("alias"))
    elif generator in ("hub", "reference_hub"):
        add(data.get("business_key"))
    elif generator == "satellite":
        add_all(data.get("attribute_columns"))
        add(data.get("subsequence_key"))
    elif generator == "scd2_dimension":
        add(data.get("business_key"))
        add_all(data.get("tracked_columns"))
    elif generator == "normalized_table":
        add_all(data.get("entity_columns"))
        add_all(data.get("attribute_columns"))
    elif generator == "link":
        add(data.get("dependent_child_key"))
        add_all(data.get("payload_columns"))
    elif generator in ("gold", "mart"):
        aggregation = data.get("aggregation")
        if isinstance(aggregation, dict):
            add_all(aggregation.get("group_by"))
            # Expression group_by items author their alias as
            # an output column (ADR 0037).
            group_by = aggregation.get("group_by")
            if isinstance(group_by, list):
                for item in group_by:
                    if isinstance(item, dict):
                        add(item.get("alias"))
            measures = aggregation.get("measures")
            if isinstance(measures, list):
                for measure in measures:
                    if isinstance(measure, dict):
                        add(measure.get("alias", measure.get("column")))
    return names


def _system_column_collision_errors(path: str, data: Any) -> list[str]:
    """One named error per practitioner column that collides
    with a generated system column of the same model.

    System columns are fixed: detection is Loom's, renaming is
    the practitioner's, and Loom never renames its system
    columns per project.
    """
    if not isinstance(data, dict):
        return []
    system = generated_system_columns(data)
    if not system:
        return []
    collisions = sorted(
        {name for name in _authored_output_names(data) if name in system}
    )
    return [
        f"{path}: '{name}' collides with the generated system "
        f"column '{name}' - rename the declared column; system "
        "columns are fixed and are never renamed per project"
        for name in collisions
    ]


# Input cardinality under the consolidated `input_models` list
# (parameter consolidation, 2026-11-04): exactly one entry for the
# single-input generators; one or more for hub, gold and view.
_SINGLE_INPUT_GENERATORS = frozenset(
    {
        "link",
        "satellite",
        "reference_hub",
        "dimension",
        "fact",
        "scd2_dimension",
        "silver",
        "normalized_table",
        "mart",
        "obt",
    }
)
_MULTI_INPUT_GENERATORS = frozenset({"hub", "gold", "view"})


def _endpoint_items_errors(
    path: str, label: str, value: Any
) -> list[str]:
    """Shape errors for a list of {hub, key} endpoint mappings."""
    errors: list[str] = []
    if not isinstance(value, list) or not value:
        return [
            f"{path}: {label!r} must be a non-empty list of "
            "{hub, key} mappings"
        ]
    for i, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or set(item) != {"hub", "key"}
            or not isinstance(item.get("hub"), str)
            or not isinstance(item.get("key"), str)
        ):
            errors.append(
                f"{path}: {label}.{i} must be a mapping with exactly "
                "'hub' and 'key' string values"
            )
        elif not item["hub"].startswith("hub_"):
            errors.append(
                f"{path}: {label}.{i}.hub must start with 'hub_'"
            )
    return errors


def _parent_block_errors(path: str, data: dict) -> list[str]:
    """Shape errors for the consolidated `parent` block.

    One block, two shapes: a hub parent is {model, key}; a link
    parent is {model, endpoints[, dependent_child_key]}. Exactly
    one shape must stand.
    """
    parent = data.get("parent")
    if not isinstance(parent, dict):
        return []  # the contract kind check reports a non-mapping
    errors: list[str] = []
    unknown = sorted(
        set(parent) - {"model", "key", "endpoints", "dependent_child_key"}
    )
    if unknown:
        errors.append(
            f"{path}: 'parent' has unknown key(s): "
            + ", ".join(unknown)
            + "; accepted keys are: dependent_child_key, "
            "endpoints, key, model"
        )
    model = parent.get("model")
    if not isinstance(model, str) or not model:
        errors.append(f"{path}: the 'parent' block requires a 'model'")
    if "endpoints" in parent:
        errors.extend(
            _endpoint_items_errors(path, "parent.endpoints", parent.get("endpoints"))
        )
        if "key" in parent:
            errors.append(
                f"{path}: 'key' is not accepted in a link 'parent' "
                "block (the endpoints carry the keys)"
            )
        dependent = parent.get("dependent_child_key")
        if dependent is not None and (
            not isinstance(dependent, str) or not dependent
        ):
            errors.append(
                f"{path}: 'parent.dependent_child_key' must be a "
                "non-empty string"
            )
    elif "key" in parent:
        key = parent.get("key")
        if not isinstance(key, str) or not key:
            errors.append(
                f"{path}: the 'parent' block requires a non-empty "
                "'key' for a hub parent"
            )
        if "dependent_child_key" in parent:
            errors.append(
                f"{path}: 'parent.dependent_child_key' is only "
                "accepted with a link parent ('endpoints')"
            )
    else:
        errors.append(
            f"{path}: the 'parent' block requires a 'key' (hub "
            "parent) or 'endpoints' (link parent)"
        )
    return errors


def _validate_record(path: str, data: Any, is_registration: bool) -> list[str]:
    generator = data.get("generator") if isinstance(data, dict) else None
    spec = (
        GENERATOR_REGISTRY.get(generator)
        if isinstance(generator, str) and not is_registration
        else None
    )
    if not is_registration and spec is None:
        return [f"{path}: generator {generator!r} is not supported"]
    if is_registration:
        errors = _validate_document_envelope(path, data, "model_registration")
    else:
        # Generator records: check name and required fields
        errors = []
        if not isinstance(data, dict):
            return [f"{path}: expected a mapping"]
        name = data.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"^[a-z][a-z0-9_]*$", name):
            errors.append(f"{path}: name must be a lowercase snake_case identifier")
        # Authoritative parameter contract: unknown
        # parameters, missing required parameters and top-level type
        # mismatches. The per-generator branches below keep the deeper
        # semantic rules (prefixes, non-empty lists, cross-field rules).
        errors.extend(validate_against_contract(path, data))
        if "output_subpath" in data:
            errors.extend(
                validate_output_subpath_template(path, data["output_subpath"])
            )
        if "contract_enforced" in data and not isinstance(
            data["contract_enforced"], bool
        ):
            errors.append(
                f"{path}: 'contract_enforced' must be true or false"
            )
        if "skip_properties_document" in data and not isinstance(
            data["skip_properties_document"], bool
        ):
            errors.append(
                f"{path}: 'skip_properties_document' must be true or false"
            )
        errors.extend(_validate_column_documentation_shapes(path, data))
        errors.extend(_system_column_collision_errors(path, data))
        if "unit_tests" in data:
            generator = data.get("generator")
            if generator in UNIT_TEST_EXCLUSIONS:
                errors.append(
                    f"{path}: 'unit_tests' is not supported for generator "
                    f"{generator!r} in v1: {UNIT_TEST_EXCLUSIONS[generator]}"
                )
            else:
                errors.extend(_validate_unit_tests(path, data))
        if "generic_tests" in data:
            # Carriage is permitted on every generator -
            # there is no exclusions list.
            errors.extend(_validate_generic_tests(path, data))
            # The interaction, resolved as a validation
            # error: carriage emits the tests in the model's
            # properties document, which the opt-out suppresses
            # - the two declarations cannot both stand. Checked
            # on the expanded record, so a pattern-default
            # opt-out against an instance carriage (or the
            # reverse) is caught as surely as both on one
            # instance. Silently overriding either would hide a
            # deliberate practitioner act.
            if (
                not is_registration
                and data.get("skip_properties_document") is True
                and isinstance(data.get("generic_tests"), list)
                and data["generic_tests"]
            ):
                errors.append(
                    f"{path}: 'generic_tests' cannot be declared "
                    "while 'skip_properties_document' is true: "
                    "carriage emits the tests in the model's "
                    "properties document, which the opt-out "
                    "suppresses - remove the opt-out, or move the "
                    "tests into your own schema YAML and remove "
                    "'generic_tests'"
                )
        gen = data.get("generator")
        input_models = data.get("input_models")
        if gen in _SINGLE_INPUT_GENERATORS and isinstance(input_models, list):
            if len(input_models) != 1:
                errors.append(
                    f"{path}: 'input_models' must name exactly one "
                    f"input model for generator {gen!r}"
                )
        elif gen in _MULTI_INPUT_GENERATORS and isinstance(input_models, list):
            if not input_models:
                errors.append(
                    f"{path}: 'input_models' must be a non-empty list"
                )
        if gen == "staging":
            if isinstance(data.get("columns"), list) and not data["columns"]:
                errors.append(f"{path}: 'columns' must be a non-empty list")
        elif gen == "hub":
            # The hub renderer derives the hash column name from the
            # hub_ prefix, so the prefix is structural, not cosmetic
            #.
            if isinstance(name, str) and not name.startswith("hub_"):
                errors.append(f"{path}: hub name must start with 'hub_'")
        elif gen == "reference_hub":
            # As hubs: the renderer's hash-column derivation strips
            # the ref_ prefix, so it is structural (ADR 0034).
            if isinstance(name, str) and not name.startswith("ref_"):
                errors.append(
                    f"{path}: reference_hub name must start with 'ref_'"
                )
        elif gen == "reference_table":
            satellites = data.get("satellites")
            if (
                not isinstance(satellites, list)
                or len(satellites) != 1
                or not isinstance(satellites[0], str)
                or not satellites[0]
            ):
                errors.append(
                    f"{path}: 'satellites' must be a list naming "
                    "exactly one satellite"
                )
            mode = data.get("variant", "latest")
            if mode not in ("latest", "full", "snapshot"):
                errors.append(
                    f"{path}: 'variant' must be one of 'latest', "
                    "'full', 'snapshot'"
                )
            timestamp = data.get("snapshot_timestamp")
            if mode == "snapshot":
                if not isinstance(timestamp, str) or not timestamp:
                    errors.append(
                        f"{path}: 'snapshot_timestamp' is required "
                        "for the snapshot variant"
                    )
            elif timestamp is not None:
                errors.append(
                    f"{path}: 'snapshot_timestamp' is only accepted "
                    "for the snapshot variant"
                )
            attributes = data.get("attribute_columns")
            if not isinstance(attributes, list) or not attributes:
                errors.append(
                    f"{path}: 'attribute_columns' must be a non-empty list"
                )
        elif gen == "link":
            if isinstance(name, str) and not name.startswith("link_"):
                errors.append(f"{path}: link name must start with 'link_'")
            errors.extend(
                _endpoint_items_errors(path, "endpoints", data.get("endpoints"))
                if "endpoints" in data
                else []
            )
        elif gen == "scd2_dimension":
            if isinstance(data.get("tracked_columns"), list) and not data[
                "tracked_columns"
            ]:
                errors.append(
                    f"{path}: 'tracked_columns' must be a non-empty list"
                )
            variant = data.get("variant", "type_2")
            if variant not in ("type_2", "type_3", "type_6"):
                errors.append(
                    f"{path}: 'variant' must be one of 'type_2', "
                    "'type_3', 'type_6'"
                )
        elif gen == "bridge":
            # (ADR 0036) Structural prefix; the path is a list
            # of {link, hub} steps whose traversal is validated
            # against the link records in the relationship pass.
            if isinstance(name, str) and not name.startswith("bridge_"):
                errors.append(f"{path}: bridge name must start with 'bridge_'")
            parent = data.get("parent")
            if isinstance(parent, dict):
                # The bridge parent names its anchor hub and
                # nothing else - the renderer reads `model`
                # only, so any other key would be silently
                # ignored.
                unknown = sorted(set(parent) - {"model"})
                if unknown:
                    errors.append(
                        f"{path}: 'parent' has unknown key(s): "
                        + ", ".join(unknown)
                        + "; accepted keys are: model"
                    )
            steps = data.get("path")
            if not isinstance(steps, list) or not steps:
                errors.append(
                    f"{path}: 'path' must be a non-empty list of "
                    "{{link, hub}} steps"
                )
            else:
                for i, step in enumerate(steps):
                    if (
                        not isinstance(step, dict)
                        or set(step) != {"link", "hub"}
                        or not isinstance(step.get("link"), str)
                        or not isinstance(step.get("hub"), str)
                    ):
                        errors.append(
                            f"{path}: path.{i} must be a mapping with "
                            "exactly 'link' and 'hub' string values"
                        )
                        continue
                    if not step["link"].startswith("link_"):
                        errors.append(
                            f"{path}: path.{i}.link must start with 'link_'"
                        )
                    if not step["hub"].startswith("hub_"):
                        errors.append(
                            f"{path}: path.{i}.hub must start with 'hub_'"
                        )
            weighting = data.get("weighting")
            if weighting is not None and weighting != "equal":
                errors.append(
                    f"{path}: 'weighting' must be 'equal' when declared"
                )
        elif gen == "satellite":
            # The current-state view name is derived from the sat_
            # prefix, so the prefix is structural.
            if isinstance(name, str) and not name.startswith("sat_"):
                errors.append(f"{path}: satellite name must start with 'sat_'")
            variant = data.get("variant", "standard")
            if variant not in (
                "standard",
                "non_historised",
                "record_tracking",
                "multi_active",
                "effectivity",
            ):
                errors.append(
                    f"{path}: 'variant' must be one of 'standard', "
                    "'non_historised', 'record_tracking', 'multi_active', "
                    "'effectivity'"
                )
            attributes = data.get("attribute_columns")
            if variant == "record_tracking":
                if attributes is not None:
                    errors.append(
                        f"{path}: 'attribute_columns' is not accepted for "
                        "the record_tracking variant (it tracks appearance "
                        "and absence, not payload)"
                    )
            elif variant == "effectivity":
                if attributes is not None:
                    errors.append(
                        f"{path}: 'attribute_columns' is not accepted for "
                        "the effectivity variant (it tracks relationship "
                        "presence, not payload)"
                    )
            elif not isinstance(attributes, list) or not attributes:
                errors.append(
                    f"{path}: 'attribute_columns' must be a non-empty list "
                    "for the standard, non_historised and multi_active "
                    "variants"
                )
            # Variant fields (ADR 0028): each belongs to exactly
            # one variant and is rejected elsewhere.
            subsequence = data.get("subsequence_key")
            if variant == "multi_active":
                if not isinstance(subsequence, str) or not subsequence:
                    errors.append(
                        f"{path}: the multi_active variant requires a "
                        "'subsequence_key'"
                    )
                elif isinstance(attributes, list) and subsequence in attributes:
                    errors.append(
                        f"{path}: 'subsequence_key' {subsequence!r} must "
                        "not also be an attribute column"
                    )
            elif subsequence is not None:
                errors.append(
                    f"{path}: 'subsequence_key' is only accepted for "
                    "the multi_active variant"
                )
            driving = data.get("driving_key")
            if variant == "effectivity":
                if not isinstance(driving, str) or not driving:
                    errors.append(
                        f"{path}: the effectivity variant requires a "
                        "'driving_key'"
                    )
                else:
                    parent_block = data.get("parent")
                    declared_endpoints = (
                        parent_block.get("endpoints")
                        if isinstance(parent_block, dict)
                        else None
                    )
                    endpoint_keys = {
                        item.get("key")
                        for item in declared_endpoints
                        if isinstance(item, dict)
                    } if isinstance(declared_endpoints, list) else set()
                    if driving not in endpoint_keys:
                        errors.append(
                            f"{path}: 'driving_key' {driving!r} must be "
                            "one of the parent link's endpoint keys"
                        )
                parent_block = data.get("parent")
                if not (
                    isinstance(parent_block, dict)
                    and isinstance(parent_block.get("endpoints"), list)
                    and parent_block.get("endpoints")
                ):
                    errors.append(
                        f"{path}: the effectivity variant requires a "
                        "link parent (a 'parent' block with "
                        "'endpoints')"
                    )
            elif driving is not None:
                errors.append(
                    f"{path}: 'driving_key' is only accepted for the "
                    "effectivity variant"
                )
            # Exactly one parent shape (parameter consolidation,
            # 2026-11-04): the `parent` block is {model, key} for
            # a hub parent or {model, endpoints[, dependent_child_key]}
            # for a link parent.
            errors.extend(_parent_block_errors(path, data))
        elif gen == "pit":
            # (ADR 0035) The pit_ prefix is structural in the
            # family sense; the parent shape mirrors the satellite
            # family's (one hub parent via parent_key, or one link
            # parent via the parent_left_*/parent_right_* fields).
            if isinstance(name, str) and not name.startswith("pit_"):
                errors.append(f"{path}: pit name must start with 'pit_'")
            satellites = data.get("satellites")
            if (
                not isinstance(satellites, list)
                or not satellites
                or not all(
                    isinstance(item, str) and item for item in satellites
                )
            ):
                errors.append(
                    f"{path}: 'satellites' must be a non-empty list of "
                    "satellite model names"
                )
            elif len(set(satellites)) != len(satellites):
                errors.append(f"{path}: 'satellites' must not repeat a satellite")
            # The parent shape mirrors the satellite family's:
            # one `parent` block, {model, key} or {model,
            # endpoints[, dependent_child_key]} (ADR 0035).
            errors.extend(_parent_block_errors(path, data))
        elif gen == "dimension":
            _validate_name_list_items(
                path, "attribute_columns", data.get("attribute_columns"), errors
            )
        elif gen == "fact":
            _validate_name_list_items(
                path, "measure_columns", data.get("measure_columns"), errors
            )
        elif gen == "silver":
            if isinstance(data.get("columns"), list) and not data["columns"]:
                errors.append(
                    f"{path}: 'columns' must be a non-empty list when declared"
                )
            _validate_name_list_items(path, "columns", data.get("columns"), errors)
        elif gen == "obt":
            if isinstance(data.get("columns"), list) and not data["columns"]:
                errors.append(f"{path}: 'columns' must be a non-empty list")
            _validate_name_list_items(path, "columns", data.get("columns"), errors)
            if isinstance(data.get("joins"), list) and not data["joins"]:
                errors.append(f"{path}: 'joins' must be a non-empty list")
            if "joins" in data and isinstance(data["joins"], list):
                for i, join in enumerate(data["joins"]):
                    if not isinstance(join, dict):
                        errors.append(f"{path}: joins.{i} must be an object")
                        continue
                    unknown = sorted(set(join) - {"columns", "model", "on"})
                    if unknown:
                        errors.append(
                            f"{path}: joins.{i} has unknown key(s): "
                            + ", ".join(unknown)
                            + "; accepted keys are: columns, model, on"
                        )
                    for jfield in ("model", "on", "columns"):
                        if jfield not in join:
                            errors.append(f"{path}: joins.{i} missing '{jfield}'")
                    if isinstance(join.get("on"), list) and not join["on"]:
                        errors.append(f"{path}: joins.{i}.on must be non-empty")
                    if isinstance(join.get("columns"), list) and not join["columns"]:
                        errors.append(f"{path}: joins.{i}.columns must be non-empty")
        elif gen == "mart":
            if "aggregation" in data and isinstance(data["aggregation"], dict):
                agg = data["aggregation"]
                _validate_aggregation_keys(path, agg, errors)
                if "group_by" not in agg:
                    errors.append(f"{path}: aggregation missing 'group_by'")
                elif not isinstance(agg["group_by"], list):
                    errors.append(f"{path}: aggregation 'group_by' must be a list")
                if "measures" not in agg:
                    errors.append(f"{path}: aggregation missing 'measures'")
                elif not isinstance(agg["measures"], list) or not agg["measures"]:
                    errors.append(f"{path}: 'measures' must be a non-empty list")
                else:
                    _validate_measure_expressions(path, agg["measures"], errors)
                _validate_group_by_items(path, agg.get("group_by"), errors)
        elif gen == "gold":
            input_models = data.get("input_models")
            if not isinstance(input_models, list) or not input_models:
                errors.append(
                    f"{path}: a non-empty 'input_models' list is a "
                    "required property"
                )
            aggregation = data.get("aggregation")
            if aggregation is None:
                if isinstance(input_models, list) and len(input_models) > 1:
                    errors.append(
                        f"{path}: pass-through gold (no aggregation) "
                        "requires exactly one input model"
                    )
            elif not isinstance(aggregation, dict):
                errors.append(f"{path}: 'aggregation' must be an object")
            elif isinstance(aggregation, dict):
                _validate_aggregation_keys(path, aggregation, errors)
                group_by = aggregation.get("group_by", [])
                measures = aggregation.get("measures", [])
                if not isinstance(group_by, list):
                    errors.append(f"{path}: aggregation 'group_by' must be a list")
                    group_by = []
                if not isinstance(measures, list):
                    errors.append(f"{path}: aggregation 'measures' must be a list")
                    measures = []
                _validate_measure_expressions(path, measures, errors)
                _validate_group_by_items(path, group_by, errors)
                if not group_by and not measures:
                    if isinstance(input_models, list) and len(input_models) > 1:
                        errors.append(
                            f"{path}: pass-through gold (no aggregation) "
                            "requires exactly one input model"
                        )
                else:
                    _validate_explicit_joins(
                        path, input_models, data.get("joins"), errors
                    )
        elif gen == "view":
            _validate_explicit_joins(
                path, data.get("input_models"), data.get("joins"), errors
            )
        elif gen == "bronze":
            source = data.get("inline_source")
            if not isinstance(source, dict):
                errors.append(f"{path}: 'inline_source' is a required property")
            else:
                for source_field in ("name", "table"):
                    value = source.get(source_field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(
                            f"{path}: inline_source.{source_field} must be a "
                            "non-empty string"
                        )

    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "staging"
        and (("source_ref" in data) == ("inline_source" in data))
    ):
        errors.append(f"{path}: exactly one of source_ref or inline_source is required")
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "staging"
        and (("source_ref" in data) != ("inline_source" in data))
    ):
        # The chosen source mapping's name/table must be
        # non-empty strings, mirroring bronze's inline_source
        # rule above (pi-review P2). Missing keys and non-string
        # values are already reported by the contract layer, so
        # only the present-but-empty case is named here.
        mapping_name = "source_ref" if "source_ref" in data else "inline_source"
        source = data.get(mapping_name)
        if isinstance(source, dict):
            for source_field in ("name", "table"):
                value = source.get(source_field)
                if isinstance(value, str) and not value.strip():
                    errors.append(
                        f"{path}: {mapping_name}.{source_field} must be a "
                        "non-empty string"
                    )
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "hub"
        and "record_source" in data
        and data.get("retention") != "persistent"
    ):
        errors.append(f"{path}: record_source requires retention 'persistent'")
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "scd2_dimension"
        and isinstance(data.get("input_models"), list)
        and len(data["input_models"]) == 1
        and isinstance(data["input_models"][0], str)
        and not re.fullmatch(r"stg_[a-z][a-z0-9_]*", data["input_models"][0])
    ):
        errors.append(
            f"{path}: the scd2 input model must start with 'stg_' "
            "and be a safe SQL identifier"
        )
    if errors:
        return errors

    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "staging"
        and isinstance(data.get("columns"), list)
    ):
        seen_to: dict[str, int] = {}
        # A raw column may be read more than once only as distinct
        # transformations: at most one plain projection, and no repeated
        # identical macro application.
        seen_from: dict[tuple[str, str | None, str], int] = {}
        for index, column in enumerate(data["columns"]):
            if not isinstance(column, dict):
                errors.append(
                    f"{path}: columns.{index}: {column!r} is not of type 'object'"
                )
                continue
            # additionalProperties: false on columns. The error
            # names the offending key(s) and the accepted set, the
            # same standard as this validator's other unknown-key
            # errors - a bare "Additional properties" left the
            # practitioner to find the accepted keys elsewhere.
            allowed_column_keys = {
                "source",
                "alias",
                "macro",
                "macro_args",
                "macro_column_param",
                "expression",
            }
            unknown_column_keys = sorted(
                key for key in column if key not in allowed_column_keys
            )
            if unknown_column_keys:
                errors.append(
                    f"{path}: columns[{index}] has unknown key(s): "
                    + ", ".join(unknown_column_keys)
                    + "; accepted keys are: "
                    + ", ".join(sorted(allowed_column_keys))
                )
            if "expression" in column:
                # The expression item (ADR 0037): a verbatim SQL
                # string under its own alias, mutually exclusive
                # with `source`; the macro slot transforms a
                # source column, so it cannot combine with an
                # expression either.
                errors.extend(
                    expression_content_errors(
                        path,
                        f"columns[{index}].expression",
                        column.get("expression"),
                    )
                )
                if "source" in column:
                    errors.append(
                        f"{path}: columns[{index}]: exactly one of 'source' "
                        "or 'expression' is required"
                    )
                if any(
                    key in column
                    for key in ("macro", "macro_args", "macro_column_param")
                ):
                    errors.append(
                        f"{path}: columns[{index}]: the macro slot requires "
                        "'source' and cannot combine with 'expression'"
                    )
                if not isinstance(column.get("alias"), str):
                    errors.append(
                        f"{path}: columns[{index}]: 'alias' is required "
                        "with 'expression'"
                    )
            # macro_args requires macro
            if "macro_args" in column and "macro" not in column:
                errors.append(
                    f"{path}: columns[{index}]: 'macro' is a dependency of 'macro_args'"
                )
            # macro_column_param requires macro
            if "macro_column_param" in column and "macro" not in column:
                errors.append(
                    f"{path}: columns[{index}]: 'macro' is a dependency of "
                    "'macro_column_param'"
                )
            target = column.get("alias")
            if isinstance(target, str) and not _IDENTIFIER_PATTERN.fullmatch(target):
                errors.append(
                    f"{path}: columns[{index}].alias: must be a safe SQL identifier"
                )
            if target == "__TODO__":
                # The conversion draft's unresolved-placeholder
                # sentinel: reported as a placeholder (once per
                # column, never as a duplicate `alias`), and it must
                # be replaced before the instance can be used.
                errors.append(
                    f"{path}: columns[{index}].alias: unresolved placeholder "
                    "'__TODO__'; replace it with the output column name "
                    "before this instance can be used"
                )
            elif isinstance(target, str):
                folded = target.casefold()
                if folded in seen_to:
                    errors.append(
                        f"{path}: columns[{index}].alias: duplicate alias "
                        f"identifier {target!r} "
                        f"(also used by columns[{seen_to[folded]}])"
                    )
                else:
                    seen_to[folded] = index
            if "expression" in column:
                # No source column: the identifier, placeholder
                # and duplicate-source checks below are about
                # `source`, and the alias checks above have
                # already covered this item's output name.
                continue
            source = column.get("source")
            if not isinstance(source, str) or not _IDENTIFIER_PATTERN.fullmatch(source):
                errors.append(
                    f"{path}: columns[{index}].source: must be a safe SQL identifier"
                )
                continue
            if source == "__TODO__":
                # The unresolved-placeholder sentinel (see the
                # `alias` check above): each occurrence is reported
                # as a placeholder naming the output it blocks, and
                # it never enters the duplicate-`source` tracking, so
                # several placeholders read as several placeholders,
                # not as duplicates of each other.
                if isinstance(target, str):
                    replacement = (
                        f"the source column that produces {target!r}"
                    )
                else:
                    replacement = "a real source column"
                errors.append(
                    f"{path}: columns[{index}].source: unresolved placeholder "
                    f"'__TODO__'; replace it with {replacement} before "
                    "this instance can be used"
                )
                continue
            macro = column.get("macro")
            macro_args = column.get("macro_args")
            try:
                args_key = json.dumps(
                    {
                        "args": macro_args,
                        "column_param": column.get("macro_column_param"),
                    },
                    sort_keys=True,
                    default=str,
                )
            except (TypeError, ValueError):
                args_key = repr(macro_args)
            key = (
                source.casefold(),
                macro if isinstance(macro, str) else None,
                args_key,
            )
            if key in seen_from:
                transformation = (
                    f"with the same macro {macro!r}"
                    if isinstance(macro, str)
                    else "as a plain projection"
                )
                errors.append(
                    f"{path}: columns[{index}].source: duplicate source "
                    f"identifier "
                    f"{source!r} {transformation} (also used by "
                    f"columns[{seen_from[key]}])"
                )
            else:
                seen_from[key] = index
        for index, column in enumerate(data["columns"]):
            if not isinstance(column, dict):
                continue
            if "expression" in column:
                # The macro checks below are about the macro
                # slot, which an expression item cannot carry
                # (the combination is already reported above).
                continue
            macro = column.get("macro")
            if isinstance(macro, str) and _MACRO_REFERENCE_PATTERN.fullmatch(macro):
                # A reference is a bare project-macro name or a
                # dotted package.name (Tier 1); the reserved
                # check applies to the macro-name segment in both
                # forms (the package segment is a namespace - dbt
                # itself is a package name and a reserved bare
                # name).
                if macro.rsplit(".", 1)[-1].casefold() in _RESERVED_MACRO_NAMES:
                    errors.append(
                        f"{path}: columns[{index}].macro: {macro!r} is a reserved "
                        "dbt/Jinja name; call a project-authored macro instead"
                    )
            elif isinstance(macro, str):
                errors.append(
                    f"{path}: columns[{index}].macro: {macro!r} is not a safe "
                    "macro identifier"
                )
            arguments = column.get("macro_args")
            if isinstance(arguments, list):
                if len(arguments) == 0:
                    errors.append(
                        f"{path}: columns[{index}].macro_args: minimum 1 item"
                    )
                for arg_index, argument in enumerate(arguments):
                    if not isinstance(argument, (str, int, bool)):
                        errors.append(
                            f"{path}: columns[{index}].macro_args[{arg_index}]: "
                            "must be a string, integer, or boolean"
                        )
                    elif isinstance(argument, str) and not _MACRO_STRING_ARG.fullmatch(
                        argument
                    ):
                        errors.append(
                            f"{path}: columns[{index}].macro_args[{arg_index}]: "
                            "string literal contains unsupported characters"
                        )
            if "macro_column_param" in column:
                # The 1-based argument position the source column
                # fills (Tier 2); absent means position 1, the
                # macro: slot's column-first convention. The bound
                # against the declared call is manifest-free: the
                # position cannot exceed the arguments the call
                # itself supplies. The bound against the macro's
                # real signature belongs to the manifest-holding
                # commands (check, convert).
                column_param = column.get("macro_column_param")
                supplied = (
                    len(arguments) + 1 if isinstance(arguments, list) else 1
                )
                if (
                    isinstance(column_param, bool)
                    or not isinstance(column_param, int)
                    or column_param < 1
                ):
                    errors.append(
                        f"{path}: columns[{index}].macro_column_param: must be "
                        "a positive integer (the 1-based argument position "
                        "the source column fills)"
                    )
                elif column_param > supplied:
                    errors.append(
                        f"{path}: columns[{index}].macro_column_param: position "
                        f"{column_param} exceeds the {supplied} argument(s) "
                        "the call supplies (the source column plus macro_args)"
                    )

    if is_registration and isinstance(data, dict):
        classification = data.get("classification")
        if not isinstance(classification, str):
            errors.append(f"{path}: classification must be a string")
        elif classification in {
            "scd2_dimension",
            "hub",
            "link",
        }:
            errors.append(
                f"{path}: classification {classification!r} is reserved for generated "
                "instances in metadata/, not local/registrations/"
            )
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "hub"
    ):
        name = data.get("name")
        key = data.get("business_key")
        if isinstance(name, str) and not re.fullmatch(r"^hub_[a-z][a-z0-9_]*$", name):
            errors.append(f"{path}: name must match pattern 'hub_[a-z][a-z0-9_]*'")
        if isinstance(name, str) and name.startswith("hub_") and isinstance(key, str):
            hash_column = f"{name[4:]}_hk"
            if key.casefold() == hash_column.casefold():
                errors.append(
                    f"{path}: business_key {key!r} collides with generated hash column {hash_column!r}"
                )
        retention = data.get("retention")
        if isinstance(retention, str) and retention not in {"current", "persistent"}:
            errors.append(f"{path}: retention must be 'current' or 'persistent'")
        if "record_source" in data:
            if data.get("retention") != "persistent":
                errors.append(f"{path}: record_source requires retention 'persistent'")
            rs = data.get("record_source")
            if isinstance(rs, str):
                if not rs.strip():
                    errors.append(f"{path}: record_source must be a non-empty string")
                elif rs != rs.strip():
                    errors.append(
                        f"{path}: record_source must not have leading or trailing whitespace"
                    )
                elif len(rs) > 128:
                    errors.append(
                        f"{path}: record_source must be 128 characters or fewer"
                    )
                elif "--" in rs or "{{" in rs or "}}" in rs:
                    errors.append(
                        f"{path}: record_source contains unsupported characters"
                    )
            elif not isinstance(rs, dict):
                errors.append(
                    f"{path}: record_source must be a string or a "
                    "mapping keyed by input model"
                )
            if isinstance(key, str) and key.casefold() in _AUDIT_COLUMNS:
                errors.append(
                    f"{path}: business_key {key!r} collides with generated audit column"
                )
        # (ADR 0030, consolidated 2026-11-04) One input list,
        # `input_models`, naming at least one distinct staging
        # model; provenance is the one `record_source` parameter -
        # a string for uniform provenance, or a mapping naming
        # exactly the input models for per-feed labels.
        input_models = data.get("input_models")
        if isinstance(input_models, list):
            if not input_models:
                errors.append(
                    f"{path}: 'input_models' must be a non-empty list "
                    "of staging model names"
                )
            elif not all(
                isinstance(model, str) and model for model in input_models
            ):
                errors.append(
                    f"{path}: 'input_models' entries must be non-empty strings"
                )
            elif len({model.casefold() for model in input_models}) != len(
                input_models
            ):
                errors.append(f"{path}: 'input_models' entries must be distinct")
            if (
                len(input_models) > 1
                and isinstance(key, str)
                and key.casefold()
                in {
                    "__rn",
                    "__source_ordinal",
                }
            ):
                errors.append(
                    f"{path}: business_key {key!r} collides with generated "
                    "multi-source hub columns"
                )
        sources = data.get("record_source")
        if isinstance(sources, dict):
            if isinstance(input_models, list):
                declared = {
                    model for model in input_models if isinstance(model, str)
                }
                if set(sources) != declared:
                    errors.append(
                        f"{path}: a mapping 'record_source' must name "
                        "exactly the hub's input_models (provenance is "
                        "all-or-nothing per source)"
                    )
            for model, label in sources.items():
                errors.extend(
                    _record_source_label_errors(
                        path, f"record_source.{model}", label
                    )
                )
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "link"
    ):
        name = data.get("name")
        if isinstance(name, str) and not re.fullmatch(r"^link_[a-z][a-z0-9_]*$", name):
            errors.append(f"{path}: name must match pattern 'link_[a-z][a-z0-9_]*'")
        # Endpoints in declared order (ADR 0030): the one
        # `endpoints` list of {hub, key} mappings, at least two.
        endpoints: list[tuple[str, str]] = []
        declared_endpoints = data.get("endpoints")
        if declared_endpoints is not None:
            if not isinstance(declared_endpoints, list) or len(
                declared_endpoints
            ) < 2:
                errors.append(
                    f"{path}: 'endpoints' must be a list of at least "
                    "two {hub, key} mappings"
                )
            else:
                for item in declared_endpoints:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("hub"), str)
                        and isinstance(item.get("key"), str)
                    ):
                        endpoints.append((item["hub"], item["key"]))
        if isinstance(name, str) and endpoints:
            output_columns = [f"{name[5:]}_hk"] + [
                f"{hub[4:]}_hk" for hub, _ in endpoints
            ]
            if len({column.casefold() for column in output_columns}) != len(
                output_columns
            ):
                errors.append(
                    f"{path}: generated link and hub hash columns must be distinct: "
                    + ", ".join(output_columns)
                )
        # A hub may appear at most once among the endpoints:
        # hierarchical links are rejected explicitly (ADR 0030).
        seen_hubs: set[str] = set()
        for hub, _ in endpoints:
            if hub.casefold() in seen_hubs:
                errors.append(
                    f"{path}: hub {hub!r} appears more than once among "
                    "the link's endpoints; hierarchical links (a hub "
                    "linked to itself) are not supported (ADR 0030)"
                )
            seen_hubs.add(hub.casefold())
        endpoint_keys = [key for _, key in endpoints]
        folded_keys = [key.casefold() for key in endpoint_keys]
        if len(set(folded_keys)) != len(folded_keys):
            errors.append(
                f"{path}: endpoint keys must be distinct across "
                "the link's endpoints"
            )
        internal_columns = {
            *(f"{hub[4:]}_hk".casefold() for hub, _ in endpoints),
            "missing_left_hub",
            "missing_right_hub",
        }
        conflicting_keys = sorted(
            {
                value
                for value in endpoint_keys
                if value.casefold() in internal_columns
            }
        )
        if conflicting_keys:
            errors.append(
                f"{path}: link keys collide with generated intermediate columns: "
                + ", ".join(conflicting_keys)
            )
        # Variant, dependent-child key and payload (ADR 0030).
        variant = data.get("variant", "standard")
        if variant not in ("standard", "non_historised"):
            errors.append(
                f"{path}: 'variant' must be one of 'standard', 'non_historised'"
            )
        dependent_key = data.get("dependent_child_key")
        generated_columns = (
            {
                column.casefold()
                for column in [f"{name[5:]}_hk"]
                + [f"{hub[4:]}_hk" for hub, _ in endpoints]
            }
            if isinstance(name, str)
            else set()
        )
        if dependent_key is not None:
            if not isinstance(dependent_key, str) or not dependent_key:
                errors.append(
                    f"{path}: 'dependent_child_key' must be a non-empty string"
                )
            else:
                if dependent_key.casefold() in {
                    key.casefold() for key in endpoint_keys
                }:
                    errors.append(
                        f"{path}: 'dependent_child_key' must be distinct "
                        "from the endpoint keys"
                    )
                if dependent_key.casefold() in generated_columns:
                    errors.append(
                        f"{path}: 'dependent_child_key' {dependent_key!r} "
                        "collides with a generated link column"
                    )
        payload = data.get("payload_columns")
        if variant == "non_historised":
            if not isinstance(dependent_key, str) or not dependent_key:
                errors.append(
                    f"{path}: the non_historised link variant requires "
                    "a 'dependent_child_key' (the event discriminator)"
                )
            if not isinstance(payload, list) or not payload:
                errors.append(
                    f"{path}: the non_historised link variant requires "
                    "a non-empty 'payload_columns' list"
                )
        elif payload is not None:
            errors.append(
                f"{path}: 'payload_columns' is only accepted for the "
                "non_historised link variant (attributes on a standard "
                "link belong in a satellite)"
            )
        if isinstance(payload, list):
            reserved = set(generated_columns)
            if isinstance(dependent_key, str):
                reserved.add(dependent_key.casefold())
            folded_payload: list[str] = []
            for i, column in enumerate(payload):
                if not isinstance(column, str) or not column:
                    errors.append(
                        f"{path}: payload_columns.{i} must be a non-empty string"
                    )
                else:
                    folded_payload.append(column.casefold())
            if len(set(folded_payload)) != len(folded_payload):
                errors.append(
                    f"{path}: 'payload_columns' entries must be distinct"
                )
            collisions = sorted(set(folded_payload) & reserved)
            if collisions:
                errors.append(
                    f"{path}: payload_columns collide with generated link columns: "
                    + ", ".join(collisions)
                )
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "fact"
    ):
        # Fact flavours (ADR 0031): the variant is a grain
        # contract - periodic_snapshot names its period key,
        # accumulating_snapshot names its milestone keys; both fields
        # are drawn from dimension_keys and rejected elsewhere.
        variant = data.get("variant", "transaction")
        if variant not in (
            "transaction",
            "periodic_snapshot",
            "accumulating_snapshot",
        ):
            errors.append(
                f"{path}: 'variant' must be one of 'transaction', "
                "'periodic_snapshot', 'accumulating_snapshot'"
            )
        dimension_keys = data.get("dimension_keys")
        key_set = (
            {key.casefold() for key in dimension_keys if isinstance(key, str)}
            if isinstance(dimension_keys, list)
            else set()
        )
        period_key = data.get("period_key")
        if variant == "periodic_snapshot":
            if not isinstance(period_key, str) or not period_key:
                errors.append(
                    f"{path}: the periodic_snapshot fact variant requires "
                    "a 'period_key'"
                )
            elif isinstance(dimension_keys, list) and (
                period_key.casefold() not in key_set
            ):
                errors.append(
                    f"{path}: 'period_key' {period_key!r} must be one of "
                    "the fact's dimension_keys"
                )
        elif period_key is not None:
            errors.append(
                f"{path}: 'period_key' is only accepted for the "
                "periodic_snapshot fact variant"
            )
        milestone_keys = data.get("milestone_keys")
        if variant == "accumulating_snapshot":
            if not isinstance(milestone_keys, list) or not milestone_keys:
                errors.append(
                    f"{path}: the accumulating_snapshot fact variant "
                    "requires a non-empty 'milestone_keys' list"
                )
            elif isinstance(dimension_keys, list):
                unknown = sorted(
                    {
                        key
                        for key in milestone_keys
                        if isinstance(key, str) and key.casefold() not in key_set
                    },
                    key=str.casefold,
                )
                if unknown:
                    errors.append(
                        f"{path}: milestone_keys must be drawn from the "
                        "fact's dimension_keys: " + ", ".join(unknown)
                    )
        elif milestone_keys is not None:
            errors.append(
                f"{path}: 'milestone_keys' is only accepted for the "
                "accumulating_snapshot fact variant"
            )
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "satellite"
    ):
        # Satellite attributes stay name-only (ADR 0037 extension,
        # 2026-11-05): they feed the hashdiff, so mapping items are
        # rejected by shape, mirroring scd2's tracked_columns.
        attributes = data.get("attribute_columns", [])
        if isinstance(attributes, list):
            for idx, item in enumerate(attributes):
                if not isinstance(item, str):
                    errors.append(
                        f"{path}: attribute_columns.{idx}: {item!r} "
                        "is not of type 'string'"
                    )
    if (
        not is_registration
        and isinstance(data, dict)
        and data.get("generator") == "scd2_dimension"
    ):
        tracked = data.get("tracked_columns", [])
        if isinstance(tracked, list):
            for idx, item in enumerate(tracked):
                if not isinstance(item, str):
                    errors.append(
                        f"{path}: tracked_columns.{idx}: {item!r} is not of type 'string'"
                    )
        columns = [data["business_key"], *tracked]
        if all(isinstance(column, str) for column in columns):
            folded = [column.casefold() for column in columns]
            if len(set(folded)) != len(folded):
                errors.append(
                    f"{path}: tracked_columns must not include the business_key "
                    "or duplicate SQL identifiers"
                )
            reserved = {
                "__valid_from",
                "__valid_to",
                "__is_deleted",
                "dbt_scd_id",
                "dbt_updated_at",
                "dbt_valid_from",
                "dbt_valid_to",
                "dbt_is_deleted",
                "dbt_unique_key",
                "dbt_change_type",
            }
            collisions = sorted(set(folded) & reserved)
            if collisions:
                errors.append(
                    f"{path}: columns collide with generated SCD2 columns: "
                    + ", ".join(collisions)
                )
        # SCD variant previous-value columns (ADR 0031).
        variant = data.get("variant", "type_2")
        previous = data.get("previous_value_columns")
        if variant in ("type_3", "type_6"):
            if not isinstance(previous, list) or not previous:
                errors.append(
                    f"{path}: the {variant} variant requires a non-empty "
                    "'previous_value_columns' list"
                )
        elif previous is not None:
            errors.append(
                f"{path}: 'previous_value_columns' is only accepted for "
                "the type_3 and type_6 variants"
            )
        if isinstance(previous, list):
            for idx, item in enumerate(previous):
                if not isinstance(item, str):
                    errors.append(
                        f"{path}: previous_value_columns.{idx}: {item!r} "
                        "is not of type 'string'"
                    )
            previous_strings = [
                item for item in previous if isinstance(item, str)
            ]
            folded_previous = [item.casefold() for item in previous_strings]
            if len(set(folded_previous)) != len(folded_previous):
                errors.append(
                    f"{path}: 'previous_value_columns' must not contain "
                    "duplicate SQL identifiers"
                )
            tracked_strings = (
                [item for item in tracked if isinstance(item, str)]
                if isinstance(tracked, list)
                else []
            )
            unknown = sorted(
                {
                    item
                    for item in previous_strings
                    if item.casefold()
                    not in {item.casefold() for item in tracked_strings}
                },
                key=str.casefold,
            )
            if unknown:
                errors.append(
                    f"{path}: previous_value_columns must be drawn from "
                    "tracked_columns: " + ", ".join(unknown)
                )
            business_key = data.get("business_key")
            if isinstance(business_key, str) and (
                business_key.casefold() in set(folded_previous)
            ):
                errors.append(
                    f"{path}: 'previous_value_columns' must not include "
                    "the business_key"
                )
            generated = {f"previous_{item}".casefold() for item in previous_strings}
            if variant == "type_6":
                generated |= {
                    f"current_{item}".casefold() for item in tracked_strings
                }
            taken = {
                column.casefold()
                for column in [business_key, *tracked_strings]
                if isinstance(column, str)
            } | {"__valid_from", "__valid_to", "__is_deleted"}
            collisions = sorted(generated & taken)
            if collisions:
                errors.append(
                    f"{path}: generated previous/current columns collide "
                    "with declared SCD2 columns: " + ", ".join(collisions)
                )
    if not is_registration and isinstance(data, dict) and "incremental" in data:
        _validate_incremental(path, data, errors)
    return errors


_AUDIT_COLUMNS = frozenset({"__loaded_at", "__record_source"})


def _record_source_label_errors(path: str, field: str, value: Any) -> list[str]:
    """Label hygiene for one record-source value (mapping form).

    Mirrors the singular hub `record_source` checks (ADR 0010): a
    restricted nonblank literal label, safe to embed in generated
    SQL as a quoted literal.
    """
    if not isinstance(value, str):
        return [f"{path}: {field} must be a string"]
    if not value.strip():
        return [f"{path}: {field} must be a non-empty string"]
    if value != value.strip():
        return [f"{path}: {field} must not have leading or trailing whitespace"]
    if len(value) > 128:
        return [f"{path}: {field} must be 128 characters or fewer"]
    if "--" in value or "{{" in value or "}}" in value:
        return [f"{path}: {field} contains unsupported characters"]
    return []


_MACRO_STRING_ARG = re.compile(r"[A-Za-z0-9 _.,:/@-]{0,200}")

# A staging macro reference (Tier 1): a bare project-macro
# name, or a package macro as package.name - the two forms dbt
# itself writes (compiled SQL and Jinja calls are dotted for
# package macros, bare for project macros, and the manifest keys
# its macro entries macro.<package>.<name>). Anything else -
# extra dots, empty or non-identifier segments - is unsafe and
# rejected, so the no-raw-SQL property of the slot survives.
_MACRO_REFERENCE_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
)
# dbt context members and Jinja names that are never valid mapping macros, even
# if a project shadows them: metadata must only invoke project-authored macros.
_RESERVED_MACRO_NAMES = frozenset(
    {
        "adapter",
        "api",
        "builtins",
        "caller",
        "config",
        "context",
        "cycler",
        "database",
        "dbt",
        "debug",
        "dict",
        "dispatch",
        "doc",
        "env",
        "env_var",
        "exceptions",
        "execute",
        "false",
        "flags",
        "fromjson",
        "fromyaml",
        "graph",
        "invocation_id",
        "is_incremental",
        "joiner",
        "kwargs",
        "lipsum",
        "load_relation",
        "load_result",
        "local_md5",
        "log",
        "loop",
        "model",
        "modules",
        "namespace",
        "none",
        "print",
        "project_name",
        "range",
        "ref",
        "return",
        "run_query",
        "run_started_at",
        "schema",
        "selected_resources",
        "self",
        "set",
        "set_strict",
        "source",
        "sql",
        "statement",
        "store_raw_result",
        "store_result",
        "super",
        "target",
        "this",
        "thread_id",
        "tojson",
        "toyaml",
        "true",
        "try_or_compiler_error",
        "var",
        "varargs",
        "write",
        "zip",
        "zip_strict",
    }
)
