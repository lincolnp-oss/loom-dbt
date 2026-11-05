"""Cross-record relationship validation for the inventory.

Extracted from ``loom.inventory``, which remains the facade consumers
import. Where the record validators check one document at a time,
these validators check an entry against the rest of the project: its
hubs, links, satellites, staging sources and other referenced models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loom.contracts import expression_content_errors
from loom.declared_unit_tests import (
    SOURCE_FED_GENERATORS,
    declared_source,
    declared_upstreams,
    fixture_columns,
)
from loom.inventory import _IDENTIFIER_PATTERN, InventoryEntry
from loom.inventory_patterns import (
    _NativeSourceTable,
    _native_sources,
    _project_macros,
)
from loom.output_columns import resolve_output_columns
from loom.project_files import _project_config

def _validate_relationships(
    entries: list[InventoryEntry],
    root: Path,
    errors: list[str],
    *,
    universe: list[InventoryEntry] | None = None,
) -> None:
    """Validate cross-record and dbt source/macro references, even after record errors.

    ``entries`` are the subjects: the records whose own
    relationships are checked. ``universe`` (scoped validation
    only, plan 7.6) is the wider record set references resolve
    against: a reference from a subject to a model outside the
    subjects is checked for resolvability against the universe
    rather than reported as missing, while the referenced
    record's own relationships are not validated here. Without
    a universe, subjects and universe are the same set."""
    universe_entries = universe if universe is not None else entries
    universe_staging = [
        entry
        for entry in universe_entries
        if entry.is_generated and entry.data.get("generator") == "staging"
    ]
    metadata_staging = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") == "staging"
    ]
    hubs = [
        entry
        for entry in entries
        if entry.is_generated
        and entry.data.get("generator") in ("hub", "reference_hub")
    ]
    links = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") == "link"
    ]
    kimballs = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") in ("fact", "dimension")
    ]
    medallions = [
        entry
        for entry in entries
        if entry.is_generated
        and entry.data.get("generator") in ("bronze", "silver", "gold")
    ]
    obts = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") == "obt"
    ]
    hybrids = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") == "mart"
    ]
    # Every per-family validator below must run regardless of which other
    # generators the project uses: several of them also carry that family's
    # required-field checks (e.g. normalized_table entity/attribute columns),
    # so a composition-dependent early return here silently disables
    # validation for single-paradigm projects.

    config = _project_config(root)
    inline_entries = [
        entry for entry in entries if isinstance(entry.data.get("inline_source"), dict)
    ]
    native_tables = (
        _native_sources(root, errors, config)
        if metadata_staging or inline_entries
        else []
    )
    _validate_staging_macros(metadata_staging, root, config, errors)
    _validate_staging_source_relationships(metadata_staging, native_tables, errors)
    _validate_inline_source_collisions(inline_entries, native_tables, errors)

    staging_by_name = {
        entry.data["name"].casefold(): entry
        for entry in universe_staging
        if isinstance(entry.data.get("name"), str)
    }
    inventory_by_name = {
        entry.data["name"].casefold(): entry
        for entry in universe_entries
        if isinstance(entry.data.get("name"), str)
    }
    metadata_staging_paths = {entry.path for entry in universe_staging}
    _validate_hub_relationships(hubs, inventory_by_name, metadata_staging_paths, errors)
    _validate_link_relationships(
        links, inventory_by_name, metadata_staging_paths, errors
    )
    satellites = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") == "satellite"
    ]
    _validate_satellite_relationships(
        satellites, inventory_by_name, metadata_staging_paths, errors
    )
    _validate_reference_table_relationships(entries, inventory_by_name, errors)
    _validate_scd2_relationships(entries, staging_by_name, errors)
    _validate_kimball_relationships(entries, staging_by_name, inventory_by_name, errors)
    _validate_medallion_relationships(entries, inventory_by_name, errors)
    _validate_obt_relationships(entries, inventory_by_name, errors)
    _validate_pit_relationships(entries, inventory_by_name, errors)
    _validate_bridge_relationships(entries, inventory_by_name, errors)
    _validate_threenf_relationships(entries, inventory_by_name, errors)
    _validate_hybrid_relationships(entries, inventory_by_name, errors)
    _validate_column_documentation(entries, inventory_by_name, errors)
    _validate_unit_test_references(entries, inventory_by_name, errors)
    _validate_contract_completeness(entries, inventory_by_name, errors)


def _validate_contract_completeness(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """The enforced-contract completeness rule.

    dbt requires an enforced contract to declare a data_type for
    EVERY column. Loom checks that at authoring time: an opted-in
    model (the `contract_enforced` envelope key) whose output
    columns are not all typed - by the resolver's Loom-fixed types
    or by `column_types` declarations - fails validation here, in
    seconds, instead of at dbt's build-time preflight. An opted-in
    model whose outputs cannot be derived at all fails too: Loom
    will not emit a contract it cannot enumerate.
    """
    from loom.output_layout import resolve_contract_enforced
    from loom.properties import effective_generic_type

    by_name = {
        name: entry.data for name, entry in inventory_by_name.items()
    }
    for entry in entries:
        if not entry.is_generated:
            continue
        if not resolve_contract_enforced(entry.data):
            continue
        model = entry.data.get("name")
        outputs = resolve_output_columns(entry.data, by_name)
        if outputs is None:
            errors.append(
                f"{entry.path}: contract_enforced model {model!r}: its "
                "output columns cannot be derived, so no complete "
                "contract can be emitted; make the columns derivable "
                "(declare them on the generator) or remove the opt-in"
            )
            continue
        declared = entry.data.get("column_types")
        types_by_key = (
            {
                key.casefold(): value
                for key, value in declared.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if isinstance(declared, dict)
            else {}
        )
        untyped = [
            column.name
            for column in outputs
            if effective_generic_type(column, types_by_key) is None
        ]
        if untyped:
            errors.append(
                f"{entry.path}: contract_enforced model {model!r} has "
                "untyped output column(s): "
                + ", ".join(untyped)
                + "; declare their types in column_types (dbt requires "
                "a data_type for every column of an enforced contract)"
            )


def _validate_unit_test_references(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Cross-record unit-test checks.

    Test names are unique across the project (dbt requires it);
    every `given` input resolves in the inventory and is one of the
    target model's declared upstreams; row columns are checked
    against the resolver's derivable output columns wherever those
    are known (never guessed); a persistent hub's tests must declare
    which load they model via `overrides.macros.is_incremental`.
    """
    by_name = {name: entry.data for name, entry in inventory_by_name.items()}
    seen_names: dict[str, str] = {}
    for entry in entries:
        if not entry.is_generated:
            continue
        tests = entry.data.get("unit_tests")
        if not isinstance(tests, list):
            continue
        model = entry.data.get("name")
        upstreams = {name.casefold() for name in declared_upstreams(entry.data)}
        target_columns = resolve_output_columns(entry.data, by_name)
        target_known = (
            {column.name.casefold() for column in target_columns}
            if target_columns is not None
            else None
        )
        for test in tests:
            if not isinstance(test, dict):
                continue
            test_name = test.get("name")
            label = repr(test_name) if isinstance(test_name, str) else "(unnamed)"
            if isinstance(test_name, str):
                if test_name in seen_names:
                    errors.append(
                        f"{entry.path}: unit test name {test_name!r} is "
                        f"also declared at {seen_names[test_name]}; unit "
                        "test names must be unique across the project"
                    )
                else:
                    seen_names[test_name] = entry.path
            given = test.get("given")
            if isinstance(given, list):
                for given_item in given:
                    if not isinstance(given_item, dict):
                        continue
                    input_name = given_item.get("input")
                    if not isinstance(input_name, str):
                        continue
                    if entry.data.get("generator") in SOURCE_FED_GENERATORS:
                        # A source-fed model's given input names its
                        # own source's table; the emitter renders it
                        # in source form. The reference is checked
                        # against the declared source - a mismatch is
                        # a clear error. Row columns are not checked:
                        # a source's full column set is not derivable
                        # from the inventory (never guessed).
                        source = declared_source(entry.data)
                        if source is None or input_name != source[1]:
                            declared = (
                                f"source {source[0]!r}, table {source[1]!r}"
                                if source is not None
                                else "no declared source"
                            )
                            errors.append(
                                f"{entry.path}: unit test {label}: given "
                                f"input {input_name!r} does not name this "
                                f"model's source table; the model declares "
                                f"{declared}, and a source-fed model's "
                                "unit tests mock its own source"
                            )
                        continue
                    input_entry = inventory_by_name.get(input_name.casefold())
                    if input_entry is None:
                        errors.append(
                            f"{entry.path}: unit test {label}: given input "
                            f"{input_name!r} does not name a model in the "
                            "inventory"
                        )
                        continue
                    if input_name.casefold() not in upstreams:
                        errors.append(
                            f"{entry.path}: unit test {label}: given input "
                            f"{input_name!r} is not a declared upstream of "
                            f"{model!r}"
                        )
                    input_columns = resolve_output_columns(
                        input_entry.data, by_name
                    )
                    if input_columns is not None:
                        known = {
                            column.name.casefold() for column in input_columns
                        }
                        for column in fixture_columns(given_item):
                            if column.casefold() not in known:
                                errors.append(
                                    f"{entry.path}: unit test {label}: "
                                    f"given rows name column {column!r}, "
                                    f"which {input_name!r} does not generate"
                                )
            expect = test.get("expect")
            if isinstance(expect, dict) and target_known is not None:
                for column in fixture_columns(expect):
                    if column.casefold() not in target_known:
                        errors.append(
                            f"{entry.path}: unit test {label}: expect rows "
                            f"name column {column!r}, which {model!r} does "
                            "not generate"
                        )
            if (
                entry.data.get("generator") == "hub"
                and entry.data.get("retention") == "persistent"
            ):
                overrides = test.get("overrides")
                macros = (
                    overrides.get("macros")
                    if isinstance(overrides, dict)
                    else None
                )
                if not (
                    isinstance(macros, dict)
                    and isinstance(macros.get("is_incremental"), bool)
                ):
                    errors.append(
                        f"{entry.path}: unit test {label}: tests on a "
                        "persistent hub must declare "
                        "overrides.macros.is_incremental (true or false) "
                        "so the fixture states which load it models "
                        "(ADR 0029)"
                    )


def _validate_column_documentation(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Check column_docs/column_types keys against derivable outputs.

    Where the general resolver (loom.output_columns) can determine a
    generated model's output columns, every documented/typed column
    must be one of them - a typo'd name would otherwise publish a
    catalogue entry for a column that does not exist. Where the
    output set cannot be determined (SELECT * generators and chains
    over them), the declaration is accepted unchecked: the
    practitioner is documenting source columns Loom cannot see.
    """
    by_name = {
        name: entry.data for name, entry in inventory_by_name.items()
    }
    for entry in entries:
        if not entry.is_generated:
            continue
        declared: dict[str, Any] = {}
        for field in ("column_docs", "column_types"):
            value = entry.data.get(field)
            if isinstance(value, dict):
                declared[field] = value
        if not declared:
            continue
        columns = resolve_output_columns(entry.data, by_name)
        if columns is None:
            continue
        known = {column.name.casefold() for column in columns}
        for field, mapping in declared.items():
            unknown = sorted(
                (key for key in mapping if key.casefold() not in known),
                key=str.casefold,
            )
            if unknown:
                errors.append(
                    f"{entry.path}: {field} names column(s) that "
                    f"{entry.data.get('name')!r} does not generate: "
                    + ", ".join(unknown)
                )


def _validate_staging_macros(
    entries: list[InventoryEntry], root: Path, config: dict[str, Any], errors: list[str]
) -> None:
    macro_uses = [
        (entry.path, index, column["macro"])
        for entry in entries
        if isinstance(entry.data.get("columns"), list)
        for index, column in enumerate(entry.data["columns"])
        if isinstance(column, dict) and isinstance(column.get("macro"), str)
    ]
    if macro_uses:
        macro_paths, project_macros = _project_macros(root, errors, config)
        for entry_path, index, macro in macro_uses:
            if "." in macro:
                # A dotted reference names a package macro
                # (Tier 1). Validation is deliberately manifest-
                # free, so it cannot verify the reference; it
                # warns instead (staging_macro_warnings) and the
                # manifest-holding commands (check, convert)
                # verify it. Never an error here.
                continue
            if macro not in project_macros:
                errors.append(
                    f"{entry_path}: columns[{index}].macro: {macro!r} is not defined "
                    "by a project-authored macro under macro-paths ("
                    + ", ".join(macro_paths)
                    + ")"
                )


def staging_macro_warnings(entries: list[InventoryEntry]) -> list[str]:
    """Warn (never block) for each dotted - package - macro
    reference in the staging instances: `loom validate` has no
    manifest, so a package macro is unverified at validation time
    by design. `loom check` and `loom convert` verify package
    macros against the manifest; `loom generate` renders the
    invocation as declared."""
    warnings: list[str] = []
    for entry in entries:
        columns = entry.data.get("columns")
        if not isinstance(columns, list):
            continue
        for index, column in enumerate(columns):
            if not isinstance(column, dict):
                continue
            macro = column.get("macro")
            if isinstance(macro, str) and "." in macro:
                warnings.append(
                    f"{entry.path}: columns[{index}].macro: {macro!r} is an "
                    "unverified package macro - validate has no manifest "
                    "to verify it against; `loom check` and `loom convert` "
                    "verify package macros against the manifest"
                )
    return warnings


def _validate_inline_source_collisions(
    inline_entries: list[InventoryEntry],
    native_tables: list[_NativeSourceTable],
    errors: list[str],
) -> None:
    """Inline sources are completed by generation: `loom generate`
    emits dbt source declarations for the inline sources that have no
    authored declaration, grouped by source name. An inline source that
    matches an authored declaration is fine - the authored declaration
    governs and generation does not re-emit it (the quickstart's bronze
    models read the same authored sources its staging models use).
    What cannot be grouped is two inline declarations of one
    not-authored source name carrying different schemas."""
    authored_names = {table.source_name.casefold() for table in native_tables}
    authored_pairs = {
        (table.source_name.casefold(), table.table_name.casefold())
        for table in native_tables
    }
    first_schema: dict[str, tuple[str, str]] = {}
    # Freshness is declared per (source, table); two entries
    # declaring different freshness for one table cannot both govern
    # the generated declaration.
    first_freshness: dict[tuple[str, str], tuple[tuple, str]] = {}
    for entry in inline_entries:
        source = entry.data["inline_source"]
        name = source.get("name")
        if not isinstance(name, str) or name.casefold() in authored_names:
            continue
        schema = source.get("schema")
        if isinstance(schema, str) and schema.strip():
            known = first_schema.setdefault(name.casefold(), (schema, entry.path))
            if known[0] != schema:
                errors.append(
                    f"{entry.path}: inline_source {name!r} declares schema "
                    f"{schema!r} but {known[1]} declares {known[0]!r} for "
                    "the same source name; one source name has one schema "
                    "in the generated sources document"
                )
        table = source.get("table")
        if not isinstance(table, str):
            continue
        pair = (name.casefold(), table.casefold())
        if pair in authored_pairs:
            continue  # the authored declaration governs
        declared = (source.get("loaded_at_field"), source.get("freshness"))
        if declared == (None, None):
            continue
        frozen = (
            declared[0],
            json.dumps(declared[1], sort_keys=True)
            if isinstance(declared[1], dict)
            else declared[1],
        )
        known_freshness = first_freshness.setdefault(pair, (frozen, entry.path))  # type: ignore[arg-type]
        if known_freshness[0] != frozen:
            errors.append(
                f"{entry.path}: inline_source {name!r} table {table!r} "
                "declares a different loaded_at_field/freshness than "
                f"{known_freshness[1]} declares for the same source "
                "table; one (source, table) has one freshness "
                "declaration in the generated sources document"
            )


def _validate_staging_source_relationships(
    entries: list[InventoryEntry],
    native_tables: list[_NativeSourceTable],
    errors: list[str],
) -> None:
    for entry in entries:
        data = entry.data
        source = data.get("source_ref") or data.get("inline_source")
        if not isinstance(source, dict):
            continue
        source_name = source.get("name")
        table_name = source.get("table")
        if not isinstance(source_name, str) or not isinstance(table_name, str):
            continue
        if not _IDENTIFIER_PATTERN.fullmatch(
            source_name
        ) or not _IDENTIFIER_PATTERN.fullmatch(table_name):
            continue

        if "inline_source" in data:
            collisions = [
                native
                for native in native_tables
                if native.source_name.casefold() == source_name.casefold()
            ]
            if collisions:
                locations = ", ".join(sorted({item.path for item in collisions}))
                errors.append(
                    f"{entry.path}: inline_source name {source_name!r} collides "
                    f"with authored dbt source in {locations}"
                )
            continue

        source_definitions = {
            native.source_definition
            for native in native_tables
            if native.source_name.casefold() == source_name.casefold()
        }
        if len(source_definitions) > 1:
            locations = ", ".join(
                sorted(
                    {
                        native.path
                        for native in native_tables
                        if native.source_name.casefold() == source_name.casefold()
                    }
                )
            )
            errors.append(
                f"{entry.path}: source_ref source {source_name!r} is ambiguous "
                f"in dbt native source YAML: {locations}"
            )
            continue

        matches = [
            native
            for native in native_tables
            if native.source_name.casefold() == source_name.casefold()
            and native.table_name.casefold() == table_name.casefold()
        ]
        if not matches:
            errors.append(
                f"{entry.path}: source_ref {source_name}.{table_name} was not found "
                "in dbt native source YAML under model-paths"
            )
            continue
        if len(matches) > 1:
            locations = ", ".join(sorted({item.path for item in matches}))
            errors.append(
                f"{entry.path}: source_ref {source_name}.{table_name} is ambiguous "
                f"in dbt native source YAML: {locations}"
            )
            continue

        declared_columns = matches[0].columns
        if declared_columns is None:
            continue
        declared_folded = {column.casefold() for column in declared_columns}
        mappings = data.get("columns")
        if not isinstance(mappings, list):
            continue
        missing = sorted(
            {
                mapping["source"]
                for mapping in mappings
                if isinstance(mapping, dict)
                and isinstance(mapping.get("source"), str)
                and mapping["source"].casefold() not in declared_folded
            },
            key=str.casefold,
        )
        if missing:
            errors.append(
                f"{entry.path}: source_ref {source_name}.{table_name} does not "
                f"declare mapped raw column(s): {', '.join(missing)}"
            )


def _validate_hub_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    metadata_staging_paths: set[str],
    errors: list[str],
) -> None:
    for entry in entries:
        # A hub reads its inputs from input_models; every input
        # gets the same staging discipline, and the business key
        # must be an alias of every generated input.
        source_names: list[str] = []
        multi = entry.data.get("input_models")
        if isinstance(multi, list):
            source_names.extend(
                model for model in multi if isinstance(model, str)
            )
        for source_name in source_names:
            stage = inventory_by_name.get(source_name.casefold())
            if stage is None or (
                stage.data.get("generator") != "staging"
                and stage.data.get("classification") != "staging"
            ):
                errors.append(
                    f"{entry.path}: input_models staging model {source_name!r} is not in the inventory"
                )
                continue
            if stage.path not in metadata_staging_paths:
                continue
            stage_columns = stage.data.get("columns")
            if not isinstance(stage_columns, list):
                continue
            aliases = {
                column["alias"].casefold()
                for column in stage_columns
                if isinstance(column, dict) and isinstance(column.get("alias"), str)
            }
            key = entry.data.get("business_key")
            if isinstance(key, str) and key.casefold() not in aliases:
                errors.append(
                    f"{entry.path}: input_models staging model {source_name!r} does not "
                    f"generate required hub column alias: {key}"
                )


def _validate_link_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    metadata_staging_paths: set[str],
    errors: list[str],
) -> None:
    for entry in entries:
        input_models = entry.data.get("input_models")
        source_name = (
            input_models[0]
            if isinstance(input_models, list) and input_models
            else None
        )
        if not isinstance(source_name, str):
            continue
        stage = inventory_by_name.get(source_name.casefold())
        if stage is None or (
            stage.data.get("generator") != "staging"
            and stage.data.get("classification") != "staging"
        ):
            errors.append(
                f"{entry.path}: input_models staging model {source_name!r} is not in the inventory"
            )
        elif stage.path not in metadata_staging_paths:
            errors.append(
                f"{entry.path}: input_models staging model {source_name!r} must be "
                "generated from metadata so link aliases can be validated"
            )
        else:
            stage_columns = stage.data.get("columns")
            if isinstance(stage_columns, list):
                aliases = {
                    column["alias"].casefold()
                    for column in stage_columns
                    if isinstance(column, dict) and isinstance(column.get("alias"), str)
                }
                # The staging input must carry every
                # endpoint key, the dependent-child key and the
                # payload columns as declared aliases.
                endpoints = entry.data.get("endpoints")
                required_aliases: list = [
                    item.get("key")
                    for item in endpoints
                    if isinstance(item, dict)
                ] if isinstance(endpoints, list) else []
                required_aliases.append(entry.data.get("dependent_child_key"))
                payload_columns = entry.data.get("payload_columns")
                if isinstance(payload_columns, list):
                    required_aliases.extend(payload_columns)
                missing_aliases = sorted(
                    {
                        key
                        for key in required_aliases
                        if isinstance(key, str) and key.casefold() not in aliases
                    },
                    key=str.casefold,
                )
                if missing_aliases:
                    errors.append(
                        f"{entry.path}: input_models staging model {source_name!r} does not "
                        "generate required link column alias(es): "
                        + ", ".join(missing_aliases)
                    )

        endpoints = entry.data.get("endpoints")
        if isinstance(endpoints, list):
            for i, item in enumerate(endpoints):
                if not isinstance(item, dict):
                    continue
                hub_name = item.get("hub")
                link_key = item.get("key")
                if not isinstance(hub_name, str):
                    continue
                hub = inventory_by_name.get(hub_name.casefold())
                if hub is None or hub.data.get("generator") != "hub":
                    errors.append(
                        f"{entry.path}: endpoints.{i}.hub "
                        f"{hub_name!r} is not an inventoried hub"
                    )
                    continue
                hub_key = hub.data.get("business_key")
                if (
                    isinstance(link_key, str)
                    and isinstance(hub_key, str)
                    and link_key.casefold() != hub_key.casefold()
                ):
                    errors.append(
                        f"{entry.path}: endpoints.{i}.key "
                        f"{link_key!r} must match {hub_name!r} "
                        f"business_key {hub_key!r}"
                    )


def _validate_satellite_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    metadata_staging_paths: set[str],
    errors: list[str],
) -> None:
    for entry in entries:
        # Input must be generated staging, so the key and payload
        # columns can be checked against its declared aliases (the
        # link discipline).
        input_models = entry.data.get("input_models")
        source_name = (
            input_models[0]
            if isinstance(input_models, list) and input_models
            else None
        )
        stage = None
        if isinstance(source_name, str):
            stage = inventory_by_name.get(source_name.casefold())
            if stage is None or (
                stage.data.get("generator") != "staging"
                and stage.data.get("classification") != "staging"
            ):
                errors.append(
                    f"{entry.path}: input_models staging model {source_name!r} "
                    "is not in the inventory"
                )
                stage = None
            elif stage.path not in metadata_staging_paths:
                errors.append(
                    f"{entry.path}: input_models staging model {source_name!r} "
                    "must be generated from metadata so satellite columns "
                    "can be validated"
                )
                stage = None
        parent_block = entry.data.get("parent")
        parent_endpoints = (
            parent_block.get("endpoints")
            if isinstance(parent_block, dict)
            else None
        )
        parent_endpoint_keys = (
            [
                item.get("key")
                for item in parent_endpoints
                if isinstance(item, dict)
            ]
            if isinstance(parent_endpoints, list)
            else []
        )
        if stage is not None:
            stage_columns = stage.data.get("columns")
            if isinstance(stage_columns, list):
                aliases = {
                    column["alias"].casefold()
                    for column in stage_columns
                    if isinstance(column, dict)
                    and isinstance(column.get("alias"), str)
                }
                required = [
                    parent_block.get("key")
                    if isinstance(parent_block, dict)
                    else None,
                    *parent_endpoint_keys,
                    parent_block.get("dependent_child_key")
                    if isinstance(parent_block, dict)
                    else None,
                    entry.data.get("driving_key"),
                    entry.data.get("subsequence_key"),
                    *(
                        entry.data.get("attribute_columns")
                        if isinstance(entry.data.get("attribute_columns"), list)
                        else []
                    ),
                ]
                missing_aliases = sorted(
                    {
                        item
                        for item in required
                        if isinstance(item, str) and item.casefold() not in aliases
                    },
                    key=str.casefold,
                )
                if missing_aliases:
                    errors.append(
                        f"{entry.path}: input_models staging model "
                        f"{source_name!r} does not generate required "
                        "satellite column alias(es): "
                        + ", ".join(missing_aliases)
                    )

        parent_name = (
            parent_block.get("model")
            if isinstance(parent_block, dict)
            else None
        )
        if not isinstance(parent_name, str):
            continue
        parent = inventory_by_name.get(parent_name.casefold())
        if parent is None or parent.data.get("generator") not in (
            "hub",
            "link",
            "reference_hub",
        ):
            errors.append(
                f"{entry.path}: parent model {parent_name!r} is not an "
                "inventoried hub, link or reference hub"
            )
            continue
        if parent.data.get("generator") in ("hub", "reference_hub"):
            hub_key = parent.data.get("business_key")
            parent_key = parent_block.get("key")
            if (
                isinstance(parent_key, str)
                and isinstance(hub_key, str)
                and parent_key.casefold() != hub_key.casefold()
            ):
                errors.append(
                    f"{entry.path}: parent key {parent_key!r} must match "
                    f"{parent_name!r} business_key {hub_key!r}"
                )
        else:
            # (ADR 0030) The satellite's parent block must mirror
            # the parent link's endpoints (hub and key, in declared
            # order - the hash composes in that order) and its
            # dependent-child key.
            def _endpoint_pairs(value: Any) -> list[tuple[str, str]]:
                if not isinstance(value, list):
                    return []
                return [
                    (str(item.get("hub", "")).casefold(),
                     str(item.get("key", "")).casefold())
                    for item in value
                    if isinstance(item, dict)
                ]

            declared_endpoints = _endpoint_pairs(parent_endpoints)
            actual_endpoints = _endpoint_pairs(parent.data.get("endpoints"))
            if declared_endpoints != actual_endpoints:
                errors.append(
                    f"{entry.path}: parent endpoints must "
                    f"match {parent_name!r} endpoints "
                    "(hub and key, in declared order)"
                )
            declared_dep = parent_block.get("dependent_child_key")
            actual_dep = parent.data.get("dependent_child_key")
            declared_dep = declared_dep if isinstance(declared_dep, str) else None
            actual_dep = actual_dep if isinstance(actual_dep, str) else None
            if (declared_dep or None) is None and (actual_dep or None) is not None:
                errors.append(
                    f"{entry.path}: parent link {parent_name!r} declares "
                    f"dependent_child_key {actual_dep!r}; the satellite "
                    "must declare it in its parent block"
                )
            elif (actual_dep or None) is None and (declared_dep or None) is not None:
                errors.append(
                    f"{entry.path}: the parent block's "
                    f"'dependent_child_key' {declared_dep!r} is declared "
                    f"but parent link {parent_name!r} has no "
                    "dependent_child_key"
                )
            elif (
                declared_dep is not None
                and actual_dep is not None
                and declared_dep.casefold() != actual_dep.casefold()
            ):
                errors.append(
                    f"{entry.path}: the parent block's "
                    f"dependent_child_key {declared_dep!r} must match "
                    f"{parent_name!r} dependent_child_key {actual_dep!r}"
                )


def _validate_scd2_relationships(
    entries: list[InventoryEntry],
    staging_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    for entry in entries:
        if entry.data.get("generator") != "scd2_dimension":
            continue
        source_name = (
            entry.data.get("input_models")[0]
            if isinstance(entry.data.get("input_models"), list)
            and entry.data.get("input_models")
            else None
        )
        stage = (
            staging_by_name.get(source_name.casefold())
            if isinstance(source_name, str)
            else None
        )
        if stage is None:
            continue
        stage_columns = stage.data.get("columns")
        if not isinstance(stage_columns, list):
            continue
        aliases = {
            column["alias"].casefold()
            for column in stage_columns
            if isinstance(column, dict) and isinstance(column.get("alias"), str)
        }
        required = [
            item
            for item in [
                entry.data.get("business_key"),
                *(
                    entry.data.get("tracked_columns", [])
                    if isinstance(entry.data.get("tracked_columns"), list)
                    else []
                ),
            ]
            if isinstance(item, str)
        ]
        missing_aliases = sorted(
            {item for item in required if item.casefold() not in aliases},
            key=str.casefold,
        )
        if missing_aliases:
            errors.append(
                f"{entry.path}: input_models staging model {stage.data.get('name')!r} "
                "does not generate required SCD2 column alias(es): "
                + ", ".join(missing_aliases)
            )


def _derivable_output_columns(
    entry: InventoryEntry,
    staging_by_name: dict[str, InventoryEntry],
) -> set[str] | None:
    """Output columns Loom can determine for a generated model, or None.

    Thin set-valued wrapper over the general resolver in
    ``loom.output_columns``, restricted to the three
    generators this function's consumers accept as fact/dimension
    inputs - staging (its column aliases), hub and link. The hub and
    link column sets follow the renderers exactly: a hub
    outputs __record_source and __loaded_at only when it declares
    provenance (record_source, in either of its forms); a link outputs its own and all endpoint hash columns, any
    dependent-child key and payload columns, and no audit columns.
    Other generators return None here
    even where the general resolver can determine their outputs,
    because the fact/dimension input contract accepts only these
    three as inputs.
    """
    if entry.data.get("generator") not in ("staging", "hub", "link"):
        return None
    columns = resolve_output_columns(entry.data)
    if columns is None:
        return None
    return {column.name.casefold() for column in columns}


def _validate_kimball_relationships(
    entries: list[InventoryEntry],
    staging_by_name: dict[str, InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Validate Kimball fact and dimension input relationships.

    The input may be a staging model (columns are its aliases) or a
    Data Vault hub/link whose output columns Loom derives - the
    vault-core-to-marts composition. Referenced dimension keys,
    measures, natural keys and attributes must be among the input's
    columns either way.
    """
    for entry in entries:
        gen = entry.data.get("generator")
        if gen not in ("fact", "dimension"):
            continue
        source_name = (
            entry.data.get("input_models")[0]
            if isinstance(entry.data.get("input_models"), list)
            and entry.data.get("input_models")
            else None
        )
        if not isinstance(source_name, str):
            continue
        source = inventory_by_name.get(source_name.casefold())
        if source is None:
            errors.append(
                f"{entry.path}: input_models {source_name!r} not found in "
                "model inventory (must reference a staging, hub or link instance)"
            )
            continue
        aliases = _derivable_output_columns(source, staging_by_name)
        if aliases is None:
            errors.append(
                f"{entry.path}: input_models {source_name!r} is a "
                f"{source.data.get('generator')!r} model; fact and dimension "
                "inputs must be staging, hub or link models, whose output "
                "columns Loom can determine"
            )
            continue
        required: list[str] = []
        if gen == "fact":
            dim_keys = entry.data.get("dimension_keys", [])
            measures = entry.data.get("measure_columns", [])
            if isinstance(dim_keys, list):
                required.extend(k for k in dim_keys if isinstance(k, str))
            if isinstance(measures, list):
                required.extend(m for m in measures if isinstance(m, str))
                # Rename items (ADR 0037) reference their source
                # column, which must exist like any string item;
                # expression items skip - their identifiers are
                # practitioner-asserted.
                required.extend(
                    m["source"]
                    for m in measures
                    if isinstance(m, dict) and isinstance(m.get("source"), str)
                )
        elif gen == "dimension":
            nk = entry.data.get("natural_key")
            attrs = entry.data.get("attribute_columns", [])
            if isinstance(nk, str):
                required.append(nk)
            if isinstance(attrs, list):
                required.extend(a for a in attrs if isinstance(a, str))
                required.extend(
                    a["source"]
                    for a in attrs
                    if isinstance(a, dict) and isinstance(a.get("source"), str)
                )
        missing_aliases = sorted(
            {item for item in required if item.casefold() not in aliases},
            key=str.casefold,
        )
        if missing_aliases:
            errors.append(
                f"{entry.path}: input_models {source.data.get('name')!r} "
                "does not generate required column(s): " + ", ".join(missing_aliases)
            )


def _validate_medallion_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Validate Medallion bronze/silver/gold cross-record relationships."""
    for entry in entries:
        gen = entry.data.get("generator")
        if gen == "silver":
            input_model = (
            entry.data.get("input_models")[0]
            if isinstance(entry.data.get("input_models"), list)
            and entry.data.get("input_models")
            else None
        )
            if isinstance(input_model, str):
                if input_model.casefold() not in inventory_by_name:
                    errors.append(
                        f"{entry.path}: input_models {input_model!r} not found in "
                        "model inventory"
                    )
        elif gen == "gold":
            # Check input_models (list)
            input_models = entry.data.get("input_models")
            if isinstance(input_models, list):
                for model in input_models:
                    if (
                        isinstance(model, str)
                        and model.casefold() not in inventory_by_name
                    ):
                        errors.append(
                            f"{entry.path}: input_models {model!r} not found in "
                            "model inventory"
                        )


def _validate_obt_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Validate OBT cross-record relationships (input and join references)."""
    for entry in entries:
        if entry.data.get("generator") != "obt":
            continue
        source_model = (
            entry.data.get("input_models")[0]
            if isinstance(entry.data.get("input_models"), list)
            and entry.data.get("input_models")
            else None
        )
        if isinstance(source_model, str):
            if source_model.casefold() not in inventory_by_name:
                errors.append(
                    f"{entry.path}: input_models {source_model!r} not found in "
                    "model inventory"
                )
        for i, join in enumerate(entry.data.get("joins", [])):
            if not isinstance(join, dict):
                continue
            model = join.get("model")
            if isinstance(model, str) and model.casefold() not in inventory_by_name:
                errors.append(
                    f"{entry.path}: joins.{i}.model {model!r} not found in "
                    "model inventory"
                )


def _validate_threenf_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Validate 3NF cross-record relationships."""
    for entry in entries:
        generator = entry.data.get("generator")
        if generator not in ("normalized_table", "view"):
            continue
        if generator == "normalized_table":
            input_model = (
            entry.data.get("input_models")[0]
            if isinstance(entry.data.get("input_models"), list)
            and entry.data.get("input_models")
            else None
        )
            if isinstance(input_model, str):
                if input_model.casefold() not in inventory_by_name:
                    errors.append(
                        f"{entry.path}: input_models {input_model!r} not found in "
                        "model inventory"
                    )
            entity_columns = entry.data.get("entity_columns")
            if not isinstance(entity_columns, list) or len(entity_columns) == 0:
                errors.append(f"{entry.path}: entity_columns must be a non-empty list")
            attribute_columns = entry.data.get("attribute_columns")
            if not isinstance(attribute_columns, list) or len(attribute_columns) == 0:
                errors.append(
                    f"{entry.path}: attribute_columns must be a non-empty list"
                )
        elif generator == "view":
            input_models = entry.data.get("input_models")
            if not isinstance(input_models, list) or len(input_models) == 0:
                errors.append(f"{entry.path}: input_models must be a non-empty list")
            else:
                for model in input_models:
                    if (
                        isinstance(model, str)
                        and model.casefold() not in inventory_by_name
                    ):
                        errors.append(
                            f"{entry.path}: input_models entry {model!r} not "
                            "found in model inventory"
                        )
            select_columns = entry.data.get("select_columns")
            if not isinstance(select_columns, list) or len(select_columns) == 0:
                errors.append(f"{entry.path}: select_columns must be a non-empty list")
            else:
                for i, col in enumerate(select_columns):
                    if not isinstance(col, dict):
                        continue
                    unknown = sorted(
                        set(col) - {"alias", "expression", "source"}
                    )
                    if unknown:
                        errors.append(
                            f"{entry.path}: select_columns[{i}] has "
                            "unknown key(s): "
                            + ", ".join(unknown)
                            + "; accepted keys are: alias, "
                            "expression, source"
                        )
                    if "expression" in col:
                        # The expression item (ADR 0037): an
                        # alternative to 'source', never both.
                        if "source" in col:
                            errors.append(
                                f"{entry.path}: select_columns[{i}]: exactly "
                                "one of 'source' or 'expression' is required"
                            )
                        errors.extend(
                            expression_content_errors(
                                entry.path,
                                f"select_columns[{i}].expression",
                                col.get("expression"),
                            )
                        )
                    elif "source" not in col:
                        errors.append(
                            f"{entry.path}: select_columns[{i}] missing 'source'"
                        )
                    if "alias" not in col:
                        errors.append(
                            f"{entry.path}: select_columns[{i}] missing 'alias'"
                        )


def _validate_hybrid_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Validate Hybrid mart cross-record relationships."""
    for entry in entries:
        if entry.data.get("generator") != "mart":
            continue
        input_model = (
            entry.data.get("input_models")[0]
            if isinstance(entry.data.get("input_models"), list)
            and entry.data.get("input_models")
            else None
        )
        if isinstance(input_model, str):
            if input_model.casefold() not in inventory_by_name:
                errors.append(
                    f"{entry.path}: input_models {input_model!r} not found in "
                    "model inventory"
                )


def _validate_pit_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """A PIT's parent must be an inventoried hub or (ADR 0035)
    link whose key fields the PIT mirrors (the satellite family's
    discipline), and every declared satellite must be a satellite
    of that same parent - never an effectivity satellite, whose
    point-in-time answer is its windows view.
    """
    for entry in entries:
        if not entry.is_generated or entry.data.get("generator") != "pit":
            continue
        parent_block = entry.data.get("parent")
        parent_name = (
            parent_block.get("model")
            if isinstance(parent_block, dict)
            else None
        )
        parent = (
            inventory_by_name.get(parent_name.casefold())
            if isinstance(parent_name, str)
            else None
        )
        if parent is None or parent.data.get("generator") not in ("hub", "link"):
            errors.append(
                f"{entry.path}: parent model {parent_name!r} is not an "
                "inventoried hub or link"
            )
            parent = None
        if parent is not None and parent.data.get("generator") == "hub":
            hub_key = parent.data.get("business_key")
            parent_key = parent_block.get("key")
            if (
                isinstance(parent_key, str)
                and isinstance(hub_key, str)
                and parent_key.casefold() != hub_key.casefold()
            ):
                errors.append(
                    f"{entry.path}: parent key {parent_key!r} must match "
                    f"{parent_name!r} business_key {hub_key!r}"
                )
        if parent is not None and parent.data.get("generator") == "link":
            def _pairs(value):
                if not isinstance(value, list):
                    return []
                return [
                    (str(item.get("hub", "")).casefold(),
                     str(item.get("key", "")).casefold())
                    for item in value
                    if isinstance(item, dict)
                ]

            declared = _pairs(
                parent_block.get("endpoints")
                if isinstance(parent_block, dict)
                else None
            )
            actual = _pairs(parent.data.get("endpoints"))
            if declared != actual:
                errors.append(
                    f"{entry.path}: parent endpoints must match "
                    f"{parent_name!r} endpoints (hub and key, in "
                    "declared order)"
                )
        satellites = entry.data.get("satellites")
        if not isinstance(satellites, list):
            continue
        for sat_name in satellites:
            if not isinstance(sat_name, str):
                continue
            satellite = inventory_by_name.get(sat_name.casefold())
            if satellite is None or satellite.data.get("generator") != "satellite":
                errors.append(
                    f"{entry.path}: satellites entry {sat_name!r} is "
                    "not an inventoried satellite"
                )
                continue
            sat_parent_block = satellite.data.get("parent")
            sat_parent = (
                sat_parent_block.get("model")
                if isinstance(sat_parent_block, dict)
                else None
            )
            if (
                isinstance(sat_parent, str)
                and isinstance(parent_name, str)
                and sat_parent.casefold() != parent_name.casefold()
            ):
                errors.append(
                    f"{entry.path}: satellite {sat_name!r} has parent "
                    f"{sat_parent!r}, not {parent_name!r}"
                )
            if satellite.data.get("variant", "standard") == "effectivity":
                errors.append(
                    f"{entry.path}: satellite {sat_name!r} is an "
                    "effectivity satellite; its point-in-time answer "
                    "is its windows view, not a PIT column"
                )


def _validate_reference_table_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """A reference table flattens ONE reference hub (ADR 0034)
    with ONE of its satellites (the one-entry `satellites` list).
    The declared business key mirrors the
    reference hub's key; the declared attributes must be the
    satellite's own attribute columns; latest/snapshot variants need
    the satellite's standard variant (its stored hash diff is the
    latest-row tie-break), full variant also accepts non-historised.
    """
    for entry in entries:
        if not entry.is_generated or entry.data.get("generator") != "reference_table":
            continue
        ref_name = entry.data.get("reference_hub")
        ref_hub = (
            inventory_by_name.get(ref_name.casefold())
            if isinstance(ref_name, str)
            else None
        )
        if ref_hub is None or ref_hub.data.get("generator") != "reference_hub":
            errors.append(
                f"{entry.path}: reference_hub {ref_name!r} is not an "
                "inventoried reference_hub"
            )
            ref_hub = None
        if ref_hub is not None:
            hub_key = ref_hub.data.get("business_key")
            declared_key = entry.data.get("business_key")
            if (
                isinstance(declared_key, str)
                and isinstance(hub_key, str)
                and declared_key.casefold() != hub_key.casefold()
            ):
                errors.append(
                    f"{entry.path}: business_key {declared_key!r} must "
                    f"match reference hub {ref_name!r} business_key "
                    f"{hub_key!r}"
                )
        declared_satellites = entry.data.get("satellites")
        sat_name = (
            declared_satellites[0]
            if isinstance(declared_satellites, list)
            and len(declared_satellites) == 1
            else None
        )
        satellite = (
            inventory_by_name.get(sat_name.casefold())
            if isinstance(sat_name, str)
            else None
        )
        if satellite is None or satellite.data.get("generator") != "satellite":
            errors.append(
                f"{entry.path}: satellites entry {sat_name!r} is not an "
                "inventoried satellite"
            )
            continue
        sat_parent_block = satellite.data.get("parent")
        sat_parent = (
            sat_parent_block.get("model")
            if isinstance(sat_parent_block, dict)
            else None
        )
        if (
            isinstance(sat_parent, str)
            and isinstance(ref_name, str)
            and sat_parent.casefold() != ref_name.casefold()
        ):
            errors.append(
                f"{entry.path}: satellite {sat_name!r} has parent "
                f"{sat_parent!r}, not reference_hub {ref_name!r}"
            )
        sat_variant = satellite.data.get("variant", "standard")
        mode = entry.data.get("variant", "latest")
        if mode in ("latest", "snapshot") and sat_variant != "standard":
            errors.append(
                f"{entry.path}: variant {mode!r} requires satellite "
                f"{sat_name!r} to be a standard satellite (its hash "
                f"diff is the latest-row tie-break); it is "
                f"{sat_variant!r}"
            )
        if mode == "full" and sat_variant not in ("standard", "non_historised"):
            errors.append(
                f"{entry.path}: variant 'full' requires satellite "
                f"{sat_name!r} to be a standard or non_historised "
                f"satellite; it is {sat_variant!r}"
            )
        sat_attributes = satellite.data.get("attribute_columns")
        declared = entry.data.get("attribute_columns")
        if isinstance(declared, list) and isinstance(sat_attributes, list):
            unknown = [
                column
                for column in declared
                if isinstance(column, str) and column not in sat_attributes
            ]
            if unknown:
                errors.append(
                    f"{entry.path}: attribute_columns not carried by "
                    f"satellite {sat_name!r}: " + ", ".join(unknown)
                )


def _validate_bridge_relationships(
    entries: list[InventoryEntry],
    inventory_by_name: dict[str, InventoryEntry],
    errors: list[str],
) -> None:
    """Walk each bridge path over the link (ADR 0036)
    records. A step's link must connect the hub reached so far to
    the step's hub, every other endpoint of the link must already
    be reached (so n-ary links traverse deterministically), and a
    step never revisits a hub.
    """
    for entry in entries:
        if not entry.is_generated or entry.data.get("generator") != "bridge":
            continue
        parent_block = entry.data.get("parent")
        anchor_name = (
            parent_block.get("model")
            if isinstance(parent_block, dict)
            else None
        )
        anchor = (
            inventory_by_name.get(anchor_name.casefold())
            if isinstance(anchor_name, str)
            else None
        )
        if anchor is None or anchor.data.get("generator") != "hub":
            errors.append(
                f"{entry.path}: parent model {anchor_name!r} is not an "
                "inventoried hub"
            )
            continue
        reached = {anchor_name.casefold()}
        current = anchor_name
        steps = entry.data.get("path")
        if not isinstance(steps, list):
            continue
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            link_name = step.get("link")
            hub_name = step.get("hub")
            if not isinstance(link_name, str) or not isinstance(hub_name, str):
                continue
            link = inventory_by_name.get(link_name.casefold())
            if link is None or link.data.get("generator") != "link":
                errors.append(
                    f"{entry.path}: path.{i} link {link_name!r} is not "
                    "an inventoried link"
                )
                break
            hub = inventory_by_name.get(hub_name.casefold())
            if hub is None or hub.data.get("generator") != "hub":
                errors.append(
                    f"{entry.path}: path.{i} hub {hub_name!r} is not "
                    "an inventoried hub"
                )
                break
            declared_endpoints = link.data.get("endpoints")
            endpoints = {
                item.get("hub")
                for item in declared_endpoints
                if isinstance(item, dict)
            } if isinstance(declared_endpoints, list) else set()
            endpoints = {
                name for name in endpoints if isinstance(name, str)
            }
            endpoint_keys = {name.casefold() for name in endpoints}
            if current.casefold() not in endpoint_keys:
                errors.append(
                    f"{entry.path}: path.{i} link {link_name!r} does "
                    f"not connect the reached hub {current!r}"
                )
                break
            if hub_name.casefold() not in endpoint_keys:
                errors.append(
                    f"{entry.path}: path.{i} hub {hub_name!r} is not "
                    f"an endpoint of link {link_name!r}"
                )
                break
            if hub_name.casefold() in reached:
                errors.append(
                    f"{entry.path}: path.{i} hub {hub_name!r} is "
                    "already reached by this path (a bridge path "
                    "never revisits a hub)"
                )
                break
            unreached = endpoint_keys - reached - {hub_name.casefold()}
            if unreached:
                errors.append(
                    f"{entry.path}: path.{i} link {link_name!r} has "
                    "endpoint hub(s) not yet reached by this path: "
                    + ", ".join(sorted(unreached))
                )
                break
            reached.add(hub_name.casefold())
            current = hub_name
