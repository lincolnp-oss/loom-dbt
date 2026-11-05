"""Pattern loading and expansion for Loom inventory metadata.

Extracted from ``loom.inventory``, which remains the facade consumers
import. This module reads pattern definitions, expands their instances
into inventory entries, and reads the project's native dbt sources and
macros that validation reasons about.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loom.contracts import ENVELOPE_KEYS
from loom.generator_registry import GENERATOR_REGISTRY
from loom.inventory import _DEFINITIONS_DIR, InventoryEntry
from loom.inventory_records import _validate_document_envelope, _validate_record
from loom.project_files import (
    _UniqueKeySafeLoader,
    _configured_paths,
    _model_yaml_files,
    _yaml_error,
)

_IDENTITY_FIELDS = ("generator", "name")


@dataclass(frozen=True)
class _PatternDefinition:
    generator: str
    defaults: dict[str, Any]
    required_params: tuple[str, ...]
    identity: dict[str, Any]


def _load_pattern_definition(
    path: str, data: Any, errors: list[str]
) -> _PatternDefinition | None:
    """Validate a named definition; it is an authoring input, not a inventory entry."""
    if not isinstance(data, dict):
        errors.append(f"{path}: expected a mapping")
        return None
    name = data.get("name")
    if not isinstance(name, str):
        errors.append(f"{path}: missing required field 'name'")
        return None
    if not re.fullmatch(r"^[a-z][a-z0-9_]*$", name):
        errors.append(f"{path}: name must be a lowercase snake_case identifier")
        return None
    expected_stem = f"{_DEFINITIONS_DIR}{name}"
    if path not in (f"{expected_stem}.yml", f"{expected_stem}.yaml"):
        errors.append(
            f"{path}: pattern definition {name!r} must be stored at "
            f"{expected_stem}.yml (or .yaml)"
        )
        return None
    generator = data.get("generator")
    if not isinstance(generator, str):
        errors.append(f"{path}: generator is required")
        return None
    generator_spec = GENERATOR_REGISTRY.get(generator)
    if generator_spec is None:
        errors.append(f"{path}: generator {generator!r} is not a supported generator")
        return None
    problems = _validate_document_envelope(path, data, "pattern_definition")
    # Check identity fields in defaults
    defaults = data.get("defaults")
    if isinstance(defaults, dict):
        problems.extend(
            f"{path}: defaults must not define {field!r}"
            for field in _IDENTITY_FIELDS
            if field in defaults
        )
        # Unit tests are model-specific by definition (they name the
        # model's own upstreams and expected rows), so they are
        # declared per instance, never in pattern defaults.
        if "unit_tests" in defaults:
            problems.append(
                f"{path}: defaults must not define 'unit_tests'; unit "
                "tests are declared on the instance, not in pattern defaults"
            )
        # The same model-specificity rule for carried generic
        # tests: a declaration names the model's own
        # columns, so it is declared per instance.
        if "generic_tests" in defaults:
            problems.append(
                f"{path}: defaults must not define 'generic_tests'; "
                "generic tests are declared on the instance, not in "
                "pattern defaults"
            )
    # Validate required_params
    required_params = data.get("required_params", [])
    if isinstance(required_params, list):
        seen: set[str] = set()
        for field in required_params:
            if not isinstance(field, str):
                continue
            if field in seen:
                problems.append(f"{path}: required_params must be non-unique")
            seen.add(field)
            if field in _IDENTITY_FIELDS:
                problems.append(f"{path}: required_params must not include {field!r}")
            if isinstance(defaults, dict) and field in defaults:
                problems.append(
                    f"{path}: required_params: {field!r} must not also have a default"
                )
    if problems:
        errors.extend(problems)
        return None
    return _PatternDefinition(
        generator,
        dict(defaults) if isinstance(defaults, dict) else {},
        tuple(required_params) if isinstance(required_params, list) else (),
        {
            "name": name,
            "path": path,
            "sha256": hashlib.sha256(
                json.dumps(data, sort_keys=True, ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
        },
    )


def _expand_instance(
    path: str,
    instance: dict[str, Any],
    pattern: str,
    definitions: dict[str, _PatternDefinition | None],
    errors: list[str],
) -> list[InventoryEntry]:
    """Expand a validated instance into its pattern's generator input."""
    params = instance["params"]
    if not isinstance(params, dict):
        errors.append(f"{path}: params must be a mapping")
        return []
    problems = [
        f"{path}: params must not define {field!r}"
        for field in _IDENTITY_FIELDS
        if field in params
    ]
    if problems:
        errors.extend(problems)
        return []

    if pattern not in definitions:
        errors.append(
            f"{path}: pattern {pattern!r} is not defined "
            f"(expected {_DEFINITIONS_DIR}{pattern}.yml or .yaml)"
        )
        return []
    definition = definitions[pattern]
    if definition is None:
        errors.append(f"{path}: pattern {pattern!r} definition is invalid")
        return []

    missing = [field for field in definition.required_params if field not in params]
    if missing:
        errors.append(
            f"{path}: missing required param(s) for pattern {pattern!r}: "
            + ", ".join(missing)
        )
    defaults = definition.defaults if isinstance(definition.defaults, dict) else {}
    # Envelope keys (output_subpath, contract_enforced) are exempt:
    # they are generation-consumed configuration, and their whole
    # point is a pattern-level default with a per-instance override
    # (instance wins in the merge below). Every other field defined
    # in both places remains an error.
    overlaps = (defaults.keys() & params.keys()) - ENVELOPE_KEYS
    if overlaps:
        errors.append(
            f"{path}: fields may not be defined in both pattern defaults and "
            "instance params: "
            + ", ".join(repr(field) for field in sorted(overlaps, key=str))
        )
        return []
    generator_spec = GENERATOR_REGISTRY.get(definition.generator)
    paradigm = generator_spec.paradigm if generator_spec else None
    expanded = _restore_yaml11_boolean_keys(
        {
            "name": instance["name"],
            "generator": definition.generator,
            "_paradigm": paradigm,
            **definition.defaults,
            **params,
        }
    )
    errors.extend(_validate_record(path, expanded, is_registration=False))
    return [InventoryEntry(path, expanded, definition.identity)]


def _restore_yaml11_boolean_keys(value: Any) -> Any:
    """Restore the `on` key in joins entries parsed by YAML 1.1.

    PyYAML implements YAML 1.1, where an unquoted `on:` mapping key
    parses as the boolean True. A hand-written joins entry -
    ``- {model: x, on: [key]}`` - therefore arrives with True where
    the author wrote `on`, and every consumer looking for "on" would
    report it missing. Restore the intended key in any mapping that
    carries a boolean True key alongside a "model" entry (the joins
    shape), recursively. Quoted 'on' keys are unaffected.
    """
    if isinstance(value, dict):
        restored = {
            key: _restore_yaml11_boolean_keys(item) for key, item in value.items()
        }
        if restored.get("model") is not None and True in restored:
            restored["on"] = restored.pop(True)
        return restored
    if isinstance(value, list):
        return [_restore_yaml11_boolean_keys(item) for item in value]
    return value


def _expand_pattern(
    path: str,
    data: Any,
    definitions: dict[str, _PatternDefinition | None],
    errors: list[str],
) -> list[InventoryEntry]:
    problems = _validate_document_envelope(path, data, "instance_set")
    if problems:
        errors.extend(problems)
        return []
    assert isinstance(data, dict)
    # pattern and instances are required
    if not isinstance(data.get("pattern"), str):
        errors.append(f"{path}: pattern is required")
        return []
    if not isinstance(data.get("instances"), list):
        errors.append(f"{path}: instances is required")
        return []
    if len(data["instances"]) == 0:
        errors.append(f"{path}: instances must be non-empty")
        return []
    entries: list[InventoryEntry] = []
    for instance in data["instances"]:
        instance_path = f"{path}#instances[{instance['name']}]"
        entries.extend(
            _expand_instance(
                instance_path,
                instance,
                data["pattern"],
                definitions,
                errors,
            )
        )
    return entries


_GENERATED_SOURCE_MARKER = "# GENERATED FILE. Source:"


@dataclass(frozen=True)
class _NativeSourceTable:
    source_name: str
    table_name: str
    columns: set[str] | None
    source_definition: str
    path: str


def _native_sources(
    project_root: Path, errors: list[str], config: dict[str, Any]
) -> list[_NativeSourceTable]:
    model_paths = _configured_paths(config, "model")

    native_tables: list[_NativeSourceTable] = []
    for path in _model_yaml_files(project_root, model_paths):
        display_path = path.relative_to(project_root).as_posix()
        try:
            with path.open("r", encoding="utf-8") as stream:
                text = stream.read()
            stripped = text.lstrip("\ufeff \t\r\n")
            if stripped.startswith(_GENERATED_SOURCE_MARKER) or stripped.startswith(
                "# Generated by Loom"
            ):
                # Not practitioner-authored: a foreign generator's file,
                # or Loom's own generated sources document - its
                # declarations must never count as authored sources,
                # or every inline source would collide with itself.
                continue
            document = yaml.load(text, Loader=_UniqueKeySafeLoader)
        except yaml.YAMLError as error:
            errors.append(_yaml_error(display_path, error))
            continue
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"{display_path}: cannot read dbt YAML: {error}")
            continue

        if not isinstance(document, dict) or "sources" not in document:
            continue
        sources = document["sources"]
        if not isinstance(sources, list):
            errors.append(f"{display_path}: sources must be a list")
            continue
        for source_index, source in enumerate(sources):
            source_location = f"{display_path}: sources[{source_index}]"
            if not isinstance(source, dict) or not isinstance(source.get("name"), str):
                errors.append(f"{source_location}: source must have a string name")
                continue
            source_name = source["name"]
            tables = source.get("tables")
            if not isinstance(tables, list):
                errors.append(f"{source_location}: tables must be a list")
                continue
            for table_index, table in enumerate(tables):
                table_location = f"{source_location}.tables[{table_index}]"
                if not isinstance(table, dict) or not isinstance(
                    table.get("name"), str
                ):
                    errors.append(f"{table_location}: table must have a string name")
                    continue
                raw_columns = table.get("columns")
                columns: set[str] | None = None
                if "columns" in table:
                    if not isinstance(raw_columns, list):
                        errors.append(f"{table_location}: columns must be a list")
                        continue
                    if any(
                        not isinstance(column, dict)
                        or not isinstance(column.get("name"), str)
                        for column in raw_columns
                    ):
                        errors.append(
                            f"{table_location}: each declared column must have a string name"
                        )
                        continue
                    columns = {column["name"] for column in raw_columns}
                native_tables.append(
                    _NativeSourceTable(
                        source_name,
                        table["name"],
                        columns,
                        source_location,
                        display_path,
                    )
                )
    return native_tables


_MACRO_DEFINITION = re.compile(r"\{%-?\s*macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _project_macros(
    project_root: Path, errors: list[str], config: dict[str, Any]
) -> tuple[list[str], set[str]]:
    """Return configured macro-paths and names of macros authored in them."""
    macro_paths = _configured_paths(config, "macro")

    names: set[str] = set()
    for path in _model_yaml_files(
        project_root,
        macro_paths,
        suffixes=frozenset({".sql"}),
        path_label="macro",
        file_label="dbt macro file",
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(
                f"{path.relative_to(project_root).as_posix()}: cannot read dbt macro: {error}"
            )
            continue
        names.update(_MACRO_DEFINITION.findall(text))
    return macro_paths, names
