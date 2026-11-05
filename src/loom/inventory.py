"""The model inventory: loading, coverage and search over Loom metadata.

This module is the facade every consumer imports. The record-level
validators live in ``loom.inventory_records``, pattern loading and
expansion in ``loom.inventory_patterns``, and the cross-record
relationship validators in ``loom.inventory_relationships``;
this module holds the shared entry types, constants and helpers they
build on, plus the project loading, coverage and search entry points.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loom.errors import LoomError
from loom.manifest import read_manifest, validate_model_freshness
from loom.output_layout import expected_model_path, resolve_output_subpath
from loom.contracts import check_generator_wiring
from loom.project_files import (
    _UniqueKeySafeLoader,
    _output_root,
    _project_config,
    _scan_files,
    _yaml_error,
)

# --- Validation error presentation ---------------------------------------
#
# The validators below collect flat ``address: message`` strings.
# Raised raw, they repeated one root cause per line (a pattern
# instance missing three params stated it five times: the
# missing-params line, one required-property line per param, and
# one cascade per param) with no headline. The formatter groups
# the problems by file, then by entry, states each root cause
# once, and leads with the count. The surviving problems keep
# their exact texts and the file#instances[name] addressing.

_ADDRESSED_ERROR = re.compile(r"^(?P<addr>\S+?): (?P<msg>.*)$", re.DOTALL)
_DUPLICATE_ERROR = re.compile(
    r"^duplicate inventory name '(?P<name>.+)': (?P<paths>.+)$"
)
_MISSING_PARAMS = re.compile(
    r"^missing required param\(s\) for pattern '.+': (?P<fields>.+)$"
)
_REQUIRED_PROPERTY = re.compile(r"^'(?P<field>[^']+)' is a required property")


def _split_address(address: str) -> tuple[str, str | None, str | None]:
    """An error address as (file, entry fragment, line:col suffix).

    ``metadata/instances/x.yml#instances[name]`` splits into the
    file and the ``instances[name]`` fragment; a YAML error's
    ``path:9:19`` splits into the file and its ``9:19`` location."""
    location = None
    match = re.search(r":(\d+:\d+)$", address)
    if match and "#" not in address:
        location = match.group(1)
        address = address[: match.start()]
    if "#" in address:
        file, fragment = address.split("#", 1)
        return file, fragment, location
    return address, None, location


def _format_validation_errors(
    errors: list[str], *, trailer: str | None = None
) -> str:
    """Render collected validation problems in the E2 grammar:
    a count headline, problems grouped by file then entry, each
    root cause stated once, and a closing remedy line (or the
    caller's trailer, for the load-failure stop)."""
    # Parse into (file, fragment, location, message) problems,
    # plus the address-less duplicate-name problems, which are
    # resolved against the groups below.
    parsed: list[tuple[str, str | None, str | None, str]] = []
    duplicates: list[tuple[str, list[str]]] = []
    ungrouped: list[str] = []
    for error in errors:
        duplicate = _DUPLICATE_ERROR.match(error)
        if duplicate:
            paths = [p.strip() for p in duplicate.group("paths").split(", ")]
            duplicates.append((duplicate.group("name"), paths))
            continue
        addressed = _ADDRESSED_ERROR.match(error)
        if addressed is None:
            ungrouped.append(error)
            continue
        file, fragment, location = _split_address(addressed.group("addr"))
        parsed.append((file, fragment, location, addressed.group("msg")))

    # Group, preserving first-appearance order of files and of
    # entries within a file.
    files: list[str] = []
    groups: dict[tuple[str, str | None], list[tuple[str | None, str]]] = {}
    for file, fragment, location, message in parsed:
        if file not in files:
            files.append(file)
        groups.setdefault((file, fragment), []).append((location, message))

    # Collapse: within an entry, a missing-params problem names
    # the root cause; per-field complaints about those same
    # fields (required-property lines, shape complaints, and
    # value cascades such as "parent_model None is not an
    # inventoried hub") restate it and are dropped. Identical
    # problems (a duplicated instance expanding twice) state
    # once. A located problem (a YAML error's line:column) keeps
    # its location, folded into its own line.
    problems: dict[tuple[str, str | None], list[str]] = {}
    total = 0
    for key, items in groups.items():
        missing_fields: set[str] = set()
        for _, message in items:
            match = _MISSING_PARAMS.match(message)
            if match:
                missing_fields.update(
                    field.strip()
                    for field in match.group("fields").split(",")
                )
        kept: list[str] = []
        seen: set[str] = set()
        for location, message in items:
            if message in seen:
                continue
            seen.add(message)
            if missing_fields and not _MISSING_PARAMS.match(message):
                required = _REQUIRED_PROPERTY.match(message)
                if required and required.group("field") in missing_fields:
                    continue
                first = message.split(" ", 1)[0].strip("'")
                if first in missing_fields:
                    continue
            if location:
                line, _, column = location.partition(":")
                message = f"{message} (line {line}, column {column})"
            kept.append(message)
        problems[key] = kept
        total += len(kept)

    # A duplicate whose declarations all sit in one entry joins
    # that entry's group, in the design's phrasing; one spanning
    # addresses keeps its full text, ungrouped.
    for name, paths in duplicates:
        targets = {_split_address(path)[:2] for path in paths}
        if len(targets) == 1:
            (file, fragment), = targets
            if file not in files:
                files.append(file)
            times = len(paths)
            problems.setdefault((file, fragment), []).append(
                f"duplicate inventory name {name!r} "
                f"(declared {times} times in this file)"
            )
        else:
            ungrouped.append(
                f"duplicate inventory name {name!r}: " + ", ".join(paths)
            )
        total += 1
    total += len(ungrouped)

    noun = "problem" if total == 1 else "problems"
    if len(files) == 1:
        headline = f"Validation failed: {total} {noun} in {files[0]}"
    elif files:
        headline = f"Validation failed: {total} {noun} in {len(files)} files"
    else:
        headline = f"Validation failed: {total} {noun}"
    lines = [headline]
    single = len(files) == 1
    for file in files:
        if not single:
            lines.append(f"  {file}:")
        file_indent = "" if single else "  "
        fragments = [
            fragment
            for (group_file, fragment) in problems
            if group_file == file
        ]
        ordered = sorted(fragments, key=lambda f: f is not None)
        for fragment in ordered:
            messages = problems.get((file, fragment)) or []
            if not messages:
                continue
            if fragment is not None:
                lines.append(f"{file_indent}  {fragment}:")
                bullet = f"{file_indent}    - "
            else:
                bullet = f"{file_indent}  - "
            for message in messages:
                lines.append(f"{bullet}{message}")
    for message in ungrouped:
        lines.append(f"  - {message}")
    if trailer is not None:
        lines.append(trailer)
    else:
        lines.append("Fix the file(s) above and re-run `loom validate`.")
    return "\n".join(lines)


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_DEFINITIONS_DIR = "metadata/patterns/"
_INSTANCES_DIR = "metadata/instances/"


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    data: dict[str, Any]
    definition: dict[str, Any] | None = None

    @property
    def is_generated(self) -> bool:
        return self.path.startswith("metadata/")

    @property
    def paradigm(self) -> str | None:
        return self.data.get("_paradigm")

    def as_dict(self) -> dict[str, Any]:
        if self.definition is None:
            return {"path": self.path, **self.data}
        return {"path": self.path, **self.data, "pattern_definition": self.definition}


@dataclass(frozen=True)
class InventoryCoverage:
    missing_models: list[str]
    duplicate_inventory_entries: dict[str, list[str]]
    duplicate_manifest_models: dict[str, list[str]]
    orphan_inventory_entries: list[str]
    by_paradigm: dict[str, dict[str, int]] | None = None
    misplaced_generated_models: list[str] | None = None
    # Same-name manifest nodes that are dbt model *versions*
    # of one model (name -> version labels, e.g. ["v1", "v2"]) are
    # reported separately from genuine duplicate names: the cause
    # (and the remedy) is different.
    versioned_manifest_models: dict[str, list[str]] | None = None

    @property
    def ok(self) -> bool:
        return not (
            self.missing_models
            or self.duplicate_inventory_entries
            or self.duplicate_manifest_models
            or self.orphan_inventory_entries
            or self.misplaced_generated_models
            or self.versioned_manifest_models
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "missing_models": self.missing_models,
            "duplicate_inventory_entries": self.duplicate_inventory_entries,
            "duplicate_manifest_models": self.duplicate_manifest_models,
            "orphan_inventory_entries": self.orphan_inventory_entries,
            "misplaced_generated_models": self.misplaced_generated_models or [],
            "versioned_manifest_models": self.versioned_manifest_models or {},
        }
        if self.by_paradigm is not None:
            result["by_paradigm"] = self.by_paradigm
        return result


def authored_source_keys(project_dir: Path) -> set[tuple[str, str]]:
    """(source name, table) pairs declared by authored model YAML files.

    Used by generation to avoid re-emitting declarations that already
    exist. Read errors are ignored here: `validate_project` has
    already reported them before generation runs. The root is resolved
    first, as `_load_project` does: the model-YAML scan compares
    resolved paths against the root, so a relative project dir (`.`)
    would otherwise be misread as an escaping path."""
    root = Path(project_dir).expanduser().resolve()
    errors: list[str] = []
    config = _project_config(root)
    return {
        (table.source_name.casefold(), table.table_name.casefold())
        for table in _native_sources(root, errors, config)
    }


def _load_project(
    project_dir: str | Path,
    *,
    reject_duplicate_names: bool,
    validate_relationships: bool = True,
) -> tuple[list[InventoryEntry], dict[str, list[str]]]:
    # Refuse to load a project at all when the generator wiring
    # is incomplete - every command reaches loading through here.
    check_generator_wiring()
    root = Path(project_dir).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise LoomError(f"{root}: cannot resolve project directory: {error}") from error
    if not root.is_dir():
        raise LoomError(f"{root}: project path is not a directory")

    files_to_load: list[tuple[Path, bool]] = []
    for relative_dir, is_registration in (
        ("metadata", False),
        ("local/registrations", True),
    ):
        files_to_load.extend(
            (path, is_registration) for path in _scan_files(root, relative_dir)
        )
    files_to_load.sort(key=lambda item: item[0].relative_to(root).as_posix())

    errors: list[str] = []
    entries: list[InventoryEntry] = []
    documents: list[tuple[str, Any, bool]] = []
    for path, is_registration in files_to_load:
        display_path = path.relative_to(root).as_posix()
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = yaml.load(stream, Loader=_UniqueKeySafeLoader)
        except yaml.YAMLError as error:
            errors.append(_yaml_error(display_path, error))
            continue
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"{display_path}: cannot read metadata: {error}")
            continue
        documents.append((display_path, data, is_registration))

    if errors:
        # Every error so far is a document that
        # could not be loaded at all. Continuing would expand
        # patterns and validate relationships against a partial
        # inventory, manufacturing "not in the inventory" errors for
        # models whose only fault is being defined in - or depending
        # on - the unloadable file (one malformed YAML file once
        # produced seven such cascade errors across four healthy
        # files). Stop at the load failures: they are the whole
        # story until they are fixed. The stop line stays verbatim,
        # as the trailer under the grouped form.
        raise LoomError(
            _format_validation_errors(
                errors,
                trailer=(
                    "validation stopped: the metadata file(s) above could "
                    "not be loaded, so no further checks were run; fix them "
                    "and re-run"
                ),
            )
        )

    definitions: dict[str, _PatternDefinition | None] = {}
    for display_path, data, is_registration in documents:
        if is_registration or not display_path.startswith(_DEFINITIONS_DIR):
            continue
        definition = _load_pattern_definition(display_path, data, errors)
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str):
            definitions[name] = definition

    for display_path, data, is_registration in documents:
        if not is_registration and display_path.startswith(_DEFINITIONS_DIR):
            continue
        if not is_registration and display_path.startswith(_INSTANCES_DIR):
            if isinstance(data, dict):
                entries.extend(_expand_pattern(display_path, data, definitions, errors))
            else:
                errors.append(
                    f"{display_path}: expected an instance set: "
                    "a mapping with pattern and instances"
                )
            continue
        if not is_registration:
            errors.append(
                f"{display_path}: generated model metadata belongs in "
                "metadata/patterns/ (pattern definitions) or "
                "metadata/instances/ (instance sets); "
                "hand-written models belong in local/registrations/"
            )
            continue
        errors.extend(_validate_record(display_path, data, is_registration=True))
        entries.append(
            InventoryEntry(display_path, data)
            if isinstance(data, dict)
            else InventoryEntry(display_path, {})
        )

    names: dict[str, list[str]] = {}
    for entry in entries:
        name = entry.data.get("name")
        if isinstance(name, str):
            names.setdefault(name, []).append(entry.path)
    duplicates = {}
    for name, paths in sorted(names.items()):
        if len(paths) > 1:
            duplicates[name] = sorted(paths)
            if reject_duplicate_names:
                errors.append(
                    f"duplicate inventory name {name!r}: " + ", ".join(sorted(paths))
                )
    if validate_relationships:
        try:
            _validate_relationships(entries, root, errors)
        except LoomError as error:
            errors.append(str(error))
    if errors:
        raise LoomError(_format_validation_errors(errors))
    return sorted(
        entries, key=lambda entry: (entry.data["name"], entry.path)
    ), duplicates


def validate_project(project_dir: str | Path) -> list[InventoryEntry]:
    """Load and validate all supported YAML inventory files in a project."""
    entries, _ = _load_project(project_dir, reject_duplicate_names=True)
    return entries


@dataclass
class ScopedValidation:
    """The outcome of a scoped validation (plan 7.6): the
    included documents' entries, validated whole; the models
    those documents contain beyond the selection; and the
    selected models that have no authoring documents."""

    entries: list[InventoryEntry]
    documents: list[str]
    extra_models: list[str]
    without_documents: list[str]
    selected: list[str]


@dataclass
class ProjectDocuments:
    """A project's authoring documents loaded with their
    record-level errors attributed per document rather than
    raised as one set - the substrate for scoped validation.
    Documents that cannot be parsed at all still stop the
    load, exactly as in `_load_project`: which models they
    contain is unknowable, so no honest scoping exists."""

    root: Path
    entries: list[InventoryEntry]
    document_models: dict[str, list[str]]
    _documents: dict[str, Any]
    _document_entries: dict[str, list[InventoryEntry]]
    _document_errors: dict[str, list[str]]
    _definition_documents: dict[str, str]

    def validate(self, selected_names: set[str]) -> ScopedValidation:
        """Validate the authoring documents of the selected
        models, whole-document: the instance and registration
        documents containing a selected model, plus the pattern
        documents defining the patterns those instances use.
        Relationships from included records to models outside
        the included set are checked for resolvability against
        the whole inventory (the `universe`), not deep-validated;
        excluded documents' own errors do not surface."""
        selected = set(selected_names)
        included: set[str] = set()
        for path, names in self.document_models.items():
            if selected.intersection(names):
                included.add(path)
        for path in sorted(included):
            if not path.startswith(_INSTANCES_DIR):
                continue
            data = self._documents.get(path)
            pattern = data.get("pattern") if isinstance(data, dict) else None
            if isinstance(pattern, str) and pattern in self._definition_documents:
                included.add(self._definition_documents[pattern])
        errors: list[str] = []
        for path in sorted(included):
            errors.extend(self._document_errors.get(path, []))
        included_entries = [
            entry
            for path in sorted(included)
            for entry in self._document_entries.get(path, [])
        ]
        names: dict[str, list[str]] = {}
        for entry in included_entries:
            name = entry.data.get("name")
            if isinstance(name, str):
                names.setdefault(name, []).append(entry.path)
        for name, paths in sorted(names.items()):
            if len(paths) > 1:
                errors.append(
                    f"duplicate inventory name {name!r}: "
                    + ", ".join(sorted(paths))
                )
        try:
            _validate_relationships(
                included_entries, self.root, errors, universe=self.entries
            )
        except LoomError as error:
            errors.append(str(error))
        if errors:
            raise LoomError(_format_validation_errors(errors))
        contained = {
            name
            for path in included
            for name in self.document_models.get(path, [])
        }
        all_documented = {
            name for names in self.document_models.values() for name in names
        }
        return ScopedValidation(
            entries=sorted(
                included_entries,
                key=lambda entry: (entry.data.get("name") or "", entry.path),
            ),
            documents=sorted(included),
            extra_models=sorted(contained - selected),
            without_documents=sorted(selected - all_documented),
            selected=sorted(selected),
        )


def load_project_documents(project_dir: str | Path) -> ProjectDocuments:
    """Load a project's authoring documents for scoped
    validation: every document parsed and expanded, with
    record-level errors kept per document instead of raised.
    Mirrors `_load_project`'s scan and its load-failure stop."""
    check_generator_wiring()
    root = Path(project_dir).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise LoomError(f"{root}: cannot resolve project directory: {error}") from error
    if not root.is_dir():
        raise LoomError(f"{root}: project path is not a directory")

    files_to_load: list[tuple[Path, bool]] = []
    for relative_dir, is_registration in (
        ("metadata", False),
        ("local/registrations", True),
    ):
        files_to_load.extend(
            (path, is_registration) for path in _scan_files(root, relative_dir)
        )
    files_to_load.sort(key=lambda item: item[0].relative_to(root).as_posix())

    load_errors: list[str] = []
    documents: list[tuple[str, Any, bool]] = []
    for path, is_registration in files_to_load:
        display_path = path.relative_to(root).as_posix()
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = yaml.load(stream, Loader=_UniqueKeySafeLoader)
        except yaml.YAMLError as error:
            load_errors.append(_yaml_error(display_path, error))
            continue
        except (OSError, UnicodeDecodeError) as error:
            load_errors.append(f"{display_path}: cannot read metadata: {error}")
            continue
        documents.append((display_path, data, is_registration))
    if load_errors:
        raise LoomError(
            _format_validation_errors(
                load_errors,
                trailer=(
                    "validation stopped: the metadata file(s) above could "
                    "not be loaded, so no further checks were run; fix them "
                    "and re-run"
                ),
            )
        )

    definitions: dict[str, _PatternDefinition | None] = {}
    definition_documents: dict[str, str] = {}
    document_errors: dict[str, list[str]] = {}
    documents_by_path: dict[str, Any] = {}
    for display_path, data, is_registration in documents:
        documents_by_path[display_path] = data
        if is_registration or not display_path.startswith(_DEFINITIONS_DIR):
            continue
        errors: list[str] = []
        definition = _load_pattern_definition(display_path, data, errors)
        document_errors[display_path] = errors
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str):
            definitions[name] = definition
            definition_documents[name] = display_path

    document_entries: dict[str, list[InventoryEntry]] = {}
    document_models: dict[str, list[str]] = {}
    entries: list[InventoryEntry] = []
    for display_path, data, is_registration in documents:
        if not is_registration and display_path.startswith(_DEFINITIONS_DIR):
            document_models[display_path] = []
            document_entries[display_path] = []
            continue
        errors = document_errors.setdefault(display_path, [])
        if not is_registration and display_path.startswith(_INSTANCES_DIR):
            raw_names: list[str] = []
            if isinstance(data, dict) and isinstance(data.get("instances"), list):
                raw_names = [
                    instance["name"]
                    for instance in data["instances"]
                    if isinstance(instance, dict)
                    and isinstance(instance.get("name"), str)
                ]
            document_models[display_path] = raw_names
            if isinstance(data, dict):
                expanded = _expand_pattern(display_path, data, definitions, errors)
            else:
                errors.append(
                    f"{display_path}: expected an instance set: "
                    "a mapping with pattern and instances"
                )
                expanded = []
            document_entries[display_path] = expanded
            entries.extend(expanded)
            continue
        if not is_registration:
            errors.append(
                f"{display_path}: generated model metadata belongs in "
                "metadata/patterns/ (pattern definitions) or "
                "metadata/instances/ (instance sets); "
                "hand-written models belong in local/registrations/"
            )
            document_models[display_path] = []
            document_entries[display_path] = []
            continue
        errors.extend(_validate_record(display_path, data, is_registration=True))
        entry = (
            InventoryEntry(display_path, data)
            if isinstance(data, dict)
            else InventoryEntry(display_path, {})
        )
        document_entries[display_path] = [entry]
        entries.append(entry)
        name = data.get("name") if isinstance(data, dict) else None
        document_models[display_path] = [name] if isinstance(name, str) else []

    return ProjectDocuments(
        root=root,
        entries=sorted(
            entries, key=lambda entry: (entry.data.get("name") or "", entry.path)
        ),
        document_models=document_models,
        _documents=documents_by_path,
        _document_entries=document_entries,
        _document_errors=document_errors,
        _definition_documents=definition_documents,
    )


def check_inventory_coverage(
    project_dir: str | Path,
    manifest_path: str | Path | None = None,
    *,
    only_models: set[str] | None = None,
) -> InventoryCoverage:
    """Compare project-owned dbt manifest models with inventory records by name.

    ``only_models`` (a ``--select`` narrowing) restricts both
    sides of the comparison to the named models, so the scoped
    verdict describes the selection's coverage, not the whole
    project's."""
    try:
        project_root = Path(project_dir).expanduser().resolve(strict=True)
    except OSError as error:
        raise LoomError(
            f"{project_dir}: cannot resolve project directory: {error}"
        ) from error
    if not project_root.is_dir():
        raise LoomError(f"{project_root}: project path is not a directory")
    owned_models, model_file_paths, model_versions = _manifest_models(
        project_root, manifest_path
    )
    entries, duplicates = _load_project(
        project_root, reject_duplicate_names=False, validate_relationships=False
    )
    if only_models is not None:
        entries = [
            entry
            for entry in entries
            if entry.data.get("name") in only_models
        ]
        duplicates = {
            name: paths
            for name, paths in duplicates.items()
            if name in only_models
        }
        owned_models = {
            name: ids
            for name, ids in owned_models.items()
            if name in only_models
        }
        model_file_paths = {
            name: path
            for name, path in model_file_paths.items()
            if name in only_models
        }
        model_versions = {
            name: versions
            for name, versions in model_versions.items()
            if name in only_models
        }
    return _inventory_coverage(
        entries,
        duplicates,
        owned_models,
        model_file_paths,
        project_root,
        model_versions,
    )


def _manifest_models(
    project_root: Path, manifest_path: str | Path | None
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, list[Any]]]:
    """Load coverage's manifest model names after identity and freshness checks.

    Also returns each model's manifest ``original_file_path`` (first
    node wins) for the misplaced-output comparison, and each
    name's per-node dbt ``version`` values (``None`` for
    unversioned nodes) so coverage can tell version nodes of one
    model from genuine same-name duplicates."""
    manifest, dbt_manifest = read_manifest(
        project_root, manifest_path, sections=("nodes",)
    )
    validate_model_freshness(project_root, manifest, dbt_manifest)
    metadata = dbt_manifest["metadata"]
    nodes = dbt_manifest["nodes"]
    project_name = metadata["project_name"]

    owned_models: dict[str, list[str]] = {}
    model_file_paths: dict[str, str] = {}
    model_versions: dict[str, list[Any]] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        if node.get("package_name") != project_name:
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name:
            raise LoomError(
                f"{manifest}: project-owned model {unique_id!r} has no valid name"
            )
        owned_models.setdefault(name, []).append(str(unique_id))
        model_versions.setdefault(name, []).append(node.get("version"))
        original = node.get("original_file_path")
        if isinstance(original, str) and original:
            model_file_paths.setdefault(name, original.replace("\\", "/"))
    return owned_models, model_file_paths, model_versions


def _inventory_coverage(
    entries: list[InventoryEntry],
    duplicate_inventory_entries: dict[str, list[str]],
    owned_models: dict[str, list[str]],
    model_file_paths: dict[str, str] | None = None,
    project_root: Path | None = None,
    model_versions: dict[str, list[Any]] | None = None,
) -> InventoryCoverage:
    """Compare loaded inventory entries with validated manifest model names."""
    inventory_names = {entry.data["name"] for entry in entries}
    model_names = set(owned_models)
    # (ADR 0028) A standard satellite's current-state view is
    # an auxiliary generated model of the satellite instance itself,
    # so it is covered by the satellite's inventory record rather
    # than needing one of its own. (Drift in the view's generated
    # file is `generate --check`'s concern, not coverage's.)
    auxiliary_models = {
        f"{entry.data['name']}_current"
        for entry in entries
        if entry.is_generated
        and entry.data.get("generator") == "satellite"
        and entry.data.get("variant", "standard")
        in ("standard", "multi_active")
        and isinstance(entry.data.get("name"), str)
    } | {
        # (ADR 0028) An effectivity satellite's windows view
        # is its auxiliary derived model, covered the same way.
        f"{entry.data['name']}_windows"
        for entry in entries
        if entry.is_generated
        and entry.data.get("generator") == "satellite"
        and entry.data.get("variant") == "effectivity"
        and isinstance(entry.data.get("name"), str)
    }
    # A controlled model whose manifest file sits somewhere
    # other than its expected output path (layout metadata vs reality)
    # is reported distinctly. Registered models are exempt.
    misplaced: list[str] = []
    if model_file_paths and project_root is not None:
        model_root = _output_root(_project_config(project_root), "model")
        for entry in entries:
            if not entry.is_generated:
                continue
            name = entry.data["name"]
            actual = model_file_paths.get(name)
            if actual is None:
                continue
            try:
                expected = expected_model_path(model_root, entry.data)
            except LoomError:
                continue
            if actual != expected:
                misplaced.append(name)
    # Per-paradigm stats
    by_paradigm: dict[str, dict[str, int]] = {}
    for entry in entries:
        p = entry.paradigm or "unspecified"
        if p not in by_paradigm:
            by_paradigm[p] = {"total": 0, "covered": 0}
        by_paradigm[p]["total"] += 1
        if entry.data["name"] in model_names:
            by_paradigm[p]["covered"] += 1
    missing_by_paradigm: dict[str, int] = {}
    for p in by_paradigm:
        missing_by_paradigm[p] = by_paradigm[p]["total"] - by_paradigm[p]["covered"]
    # A same-name group whose every node carries a distinct
    # dbt `version` is one versioned model, not a duplicate-name
    # defect; report it separately so the error can name the cause.
    duplicate_manifest_models: dict[str, list[str]] = {}
    versioned_manifest_models: dict[str, list[str]] = {}
    for name, ids in sorted(owned_models.items()):
        if len(ids) <= 1:
            continue
        versions = (model_versions or {}).get(name) or []
        if (
            len(versions) == len(ids)
            and all(version is not None for version in versions)
            and len({str(version) for version in versions}) == len(ids)
        ):
            ordered = sorted(
                versions,
                key=lambda v: (0, v, "")
                if isinstance(v, (int, float))
                else (1, 0, str(v)),
            )
            versioned_manifest_models[name] = [f"v{v}" for v in ordered]
        else:
            duplicate_manifest_models[name] = sorted(ids)
    return InventoryCoverage(
        missing_models=sorted(model_names - inventory_names - auxiliary_models),
        duplicate_inventory_entries=duplicate_inventory_entries,
        duplicate_manifest_models=duplicate_manifest_models,
        versioned_manifest_models=versioned_manifest_models,
        orphan_inventory_entries=sorted(inventory_names - model_names),
        misplaced_generated_models=sorted(misplaced),
        by_paradigm={
            p: {**by_paradigm[p], "missing": missing_by_paradigm.get(p, 0)}
            for p in sorted(by_paradigm)
        },
    )


def _values(data: dict[str, Any], field: str) -> list[str]:
    value = data.get(field)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _contains(values: list[str], term: str) -> bool:
    folded = term.casefold()
    return any(folded in value.casefold() for value in values)


def _search_columns(data: dict[str, Any]) -> list[str]:
    columns = _values(data, "tracked_columns")
    for mapping in data.get("columns", []):
        if isinstance(mapping, dict):
            columns.extend(_values(mapping, "source") + _values(mapping, "alias"))
    return columns


def _endpoint_values(data: dict[str, Any], field: str) -> list[str]:
    """One field across a record's `endpoints` mappings (links)."""
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    return [
        item[field]
        for item in endpoints
        if isinstance(item, dict) and isinstance(item.get(field), str)
    ]


def _parent_values(data: dict[str, Any], field: str) -> list[str]:
    """Values from a record's `parent` block: its model, its key,
    or one field across its endpoint mappings."""
    parent = data.get("parent")
    if not isinstance(parent, dict):
        return []
    if field in ("model", "key"):
        value = parent.get(field)
        return [value] if isinstance(value, str) else []
    endpoints = parent.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    return [
        item[field]
        for item in endpoints
        if isinstance(item, dict) and isinstance(item.get(field), str)
    ]


def _source_values(data: dict[str, Any]) -> list[str]:
    reference = data.get("source_ref") or data.get("inline_source")
    if not isinstance(reference, dict):
        return []
    values = [
        reference[field]
        for field in ("name", "table", "schema", "database")
        if isinstance(reference.get(field), str)
    ]
    if isinstance(reference.get("name"), str) and isinstance(
        reference.get("table"), str
    ):
        values.append(f"{reference['name']}.{reference['table']}")
    return values


def search_inventory(
    project_dir: str | Path,
    *,
    text: str | None = None,
    generator: str | None = None,
    classification: str | None = None,
    owner: str | None = None,
    tag: str | None = None,
    column: str | None = None,
    key: str | None = None,
    dependency: str | None = None,
    paradigm: str | None = None,
) -> list[InventoryEntry]:
    """Return validated inventory records matching all supplied filters."""
    if generator is not None and classification is not None:
        raise LoomError(
            "generator and classification filters are mutually exclusive; "
            "use generator for generated instances or classification for model registrations"
        )
    entries = validate_project(project_dir)
    matches = []
    for entry in entries:
        data = entry.data
        dependencies = (
            _values(data, "depends_on")
            + _values(data, "input_models")
            + _values(data, "upstream")
            + _endpoint_values(data, "hub")
            + _parent_values(data, "model")
            + _parent_values(data, "hub")
            + _source_values(data)
        )
        keys = (
            _values(data, "business_key")
            + _values(data, "keys")
            + _endpoint_values(data, "key")
            + _parent_values(data, "key")
        )
        haystack = [
            entry.path,
            data.get("name", ""),
            data.get("generator", ""),
            data.get("classification", ""),
            data.get("description", ""),
            data.get("owner", ""),
            *_values(data, "tags"),
            *_search_columns(data),
            *keys,
            *dependencies,
        ]
        if text and not _contains(haystack, text):
            continue
        if generator is not None and (
            not entry.is_generated
            or not _contains(_values(data, "generator"), generator)
        ):
            continue
        if classification is not None and (
            entry.is_generated
            or not _contains(_values(data, "classification"), classification)
        ):
            continue
        if owner and not _contains(_values(data, "owner"), owner):
            continue
        if tag and not _contains(_values(data, "tags"), tag):
            continue
        if column and not _contains(_search_columns(data), column):
            continue
        if key and not _contains(keys, key):
            continue
        if dependency and not _contains(dependencies, dependency):
            continue
        if paradigm is not None:
            entry_paradigm = entry.paradigm or "unspecified"
            if entry_paradigm != paradigm:
                continue
        matches.append(entry)
    return matches

# Extracted implementation modules. These imports sit at the
# bottom of the facade on purpose: the extracted modules import the
# shared names defined above from here, so the facade must define them
# before the extracted modules load. Every pre-extraction name remains
# importable from ``loom.inventory``.
from loom.inventory_patterns import (  # noqa: E402
    _expand_pattern,
    _load_pattern_definition,
    _native_sources,
)
from loom.inventory_records import _validate_record  # noqa: E402
from loom.inventory_relationships import _validate_relationships  # noqa: E402
from loom.inventory_corpus import (  # noqa: E402
    CorpusColumn,
    InventoryCorpus,
    MacroArgument,
    ModelRecord,
    ObjectRecord,
    build_inventory,
    corpus_from_entries,
)

