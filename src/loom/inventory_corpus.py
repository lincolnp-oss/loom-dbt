"""The unioned inventory corpus.

One derived view over the two sources Loom already reads: Loom
metadata (pattern definitions, instance sets and registrations,
loaded through the same ``_load_project`` path the facade's other
consumers use) unioned with the dbt manifest (models, sources,
seeds, snapshots, tests, exposures and macros). Nothing is stored
or synced: every call rebuilds the records from the files on disk.

The record model is the contract later code builds on - the
query engine evaluates selectors over these records, search
renders them, and similarity derives shape fingerprints from
them. Fields that cannot be derived for a record are absent
or ``None``, never fabricated.

Modes: with a manifest present the corpus is in ``full`` mode and
statuses run over generated / registered / unmanaged / orphaned.
With no manifest the corpus is in ``metadata-only`` mode: only
Loom-known objects appear, statuses collapse to what metadata
alone can prove (generated / registered), and the mode is stated
on the corpus so no consumer can mistake the two truth values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loom.errors import LoomError
from loom.inventory import (
    _endpoint_values,
    _inventory_coverage,
    _load_project,
    _parent_values,
    _values,
    InventoryEntry,
)
from loom.inventory_patterns import _native_sources
from loom.manifest import (
    generated_at,
    manifest_macro_index,
    read_manifest,
    validate_model_freshness,
)
from loom.output_columns import resolve_output_columns
from loom.project_files import _project_config

# Provenance values for unified documentation fields (plan 6.6).
PROVENANCE_LOOM = "loom"
PROVENANCE_MANIFEST = "manifest"
PROVENANCE_BOTH = "both-agreeing"

# Where a record's output-column list came from.
COLUMNS_RESOLVER = "resolver"
COLUMNS_MANIFEST = "manifest"
COLUMNS_DECLARED = "declared"


@dataclass(frozen=True)
class CorpusColumn:
    """One output column of a model record: the name, plus the
    documented type and description where a truthful one exists,
    each carrying the provenance of the source that won it."""

    name: str
    data_type: str | None = None
    data_type_provenance: str | None = None
    description: str | None = None
    description_provenance: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "data_type_provenance": self.data_type_provenance,
            "description": self.description,
            "description_provenance": self.description_provenance,
        }


@dataclass(frozen=True)
class ModelRecord:
    """One tier-1 record: a model in the metadata/manifest union."""

    name: str
    status: str  # generated | registered | unmanaged | orphaned
    kind: str = "model"
    unique_id: str | None = None
    # The model's own file (manifest original_file_path); for an
    # orphaned model, its metadata file instead.
    path: str | None = None
    # The inventory entry's path, for Loom-known models.
    metadata_path: str | None = None
    package: str | None = None
    # Loom enrichment (Loom-known models only).
    generator: str | None = None
    pattern: str | None = None
    paradigm: str | None = None
    classification: str | None = None
    owner: str | None = None
    tags: tuple[str, ...] = ()
    # Unified documentation (plan 6.6).
    description: str | None = None
    description_provenance: str | None = None
    documented: bool = False
    columns_documented: int = 0
    # Shape: declared grain keys, declared model inputs, declared
    # source inputs ("name.table"), output columns in order.
    keys: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    source_inputs: tuple[str, ...] = ()
    columns: tuple[CorpusColumn, ...] = ()
    columns_partial: bool = False
    columns_source: str | None = None
    # Graph neighbours by name; declared-only when the model has
    # no manifest entry (or in metadata-only mode).
    upstream: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    graph_declared_only: bool = False
    # Health context: state check and coverage already compute.
    has_owner: bool = False
    has_tests: bool = False
    misplaced: bool = False
    duplicate: bool = False

    @property
    def columns_total(self) -> int:
        return len(self.columns)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "unique_id": self.unique_id,
            "path": self.path,
            "metadata_path": self.metadata_path,
            "package": self.package,
            "generator": self.generator,
            "pattern": self.pattern,
            "paradigm": self.paradigm,
            "classification": self.classification,
            "owner": self.owner,
            "tags": list(self.tags),
            "description": self.description,
            "description_provenance": self.description_provenance,
            "documented": self.documented,
            "columns_documented": self.columns_documented,
            "columns_total": self.columns_total,
            "keys": list(self.keys),
            "inputs": list(self.inputs),
            "source_inputs": list(self.source_inputs),
            "columns": [column.as_dict() for column in self.columns],
            "columns_partial": self.columns_partial,
            "columns_source": self.columns_source,
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
            "graph_declared_only": self.graph_declared_only,
            "has_owner": self.has_owner,
            "has_tests": self.has_tests,
            "misplaced": self.misplaced,
            "duplicate": self.duplicate,
        }


@dataclass(frozen=True)
class MacroArgument:
    """One declared macro argument, in signature order."""

    name: str
    type: str | None = None
    has_default: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "has_default": self.has_default,
        }


@dataclass(frozen=True)
class ObjectRecord:
    """One tier-2 record: a source, seed, snapshot, test,
    exposure or macro (plan 6.4). ``arguments`` and ``used_by``
    are populated for macros only."""

    kind: str  # source | seed | snapshot | test | exposure | macro
    name: str
    unique_id: str | None = None
    package: str | None = None
    provenance: str = "project"  # project | package
    path: str | None = None
    description: str | None = None
    upstream: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    arguments: tuple[MacroArgument, ...] | None = None
    used_by: tuple[str, ...] | None = None

    @property
    def identity(self) -> str:
        """The selection/search identity of the record: the
        bare name, except for macros, whose names live in
        package namespaces - a macro's identity is
        package-qualified so same-name macros from different
        packages coexist in results (M6)."""
        if self.kind == "macro" and self.package:
            return f"{self.package}.{self.name}"
        return self.name

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "unique_id": self.unique_id,
            "package": self.package,
            "provenance": self.provenance,
            "path": self.path,
            "description": self.description,
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
        }
        if self.arguments is not None:
            result["arguments"] = [
                argument.as_dict() for argument in self.arguments
            ]
        if self.used_by is not None:
            result["used_by"] = list(self.used_by)
        return result


@dataclass(frozen=True)
class InventoryCorpus:
    """The whole derived inventory: mode, manifest facts, and
    the tier-1 model records plus tier-2 object records."""

    mode: str  # "full" | "metadata-only"
    project_name: str | None
    manifest_path: str | None
    manifest_generated_at: datetime | None
    models: tuple[ModelRecord, ...]
    objects: tuple[ObjectRecord, ...]

    def model(self, name: str) -> ModelRecord | None:
        """The model record with this exact name, or None."""
        for record in self.models:
            if record.name == name:
                return record
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "project_name": self.project_name,
            "manifest_path": self.manifest_path,
            "manifest_generated_at": (
                self.manifest_generated_at.isoformat()
                if self.manifest_generated_at is not None
                else None
            ),
            "models": [record.as_dict() for record in self.models],
            "objects": [record.as_dict() for record in self.objects],
        }


# --- Small shared helpers ---------------------------------------------------


def _text(value: Any) -> str | None:
    """A non-blank string, or None."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _unify_text(
    loom_text: str | None, manifest_text: str | None, *, loom_wins: bool
) -> tuple[str | None, str | None]:
    """Unify one documentation field from its two sources
    (plan 6.6 rule 1): returns the winning text and its
    provenance. Agreement is one source, not two; on a genuine
    difference the winner is decided by ``loom_wins`` and the
    losing text is dropped, never carried."""
    if loom_text is not None and manifest_text is not None:
        if loom_text.strip() == manifest_text.strip():
            return loom_text, PROVENANCE_BOTH
        if loom_wins:
            return loom_text, PROVENANCE_LOOM
        return manifest_text, PROVENANCE_MANIFEST
    if loom_text is not None:
        return loom_text, PROVENANCE_LOOM
    if manifest_text is not None:
        return manifest_text, PROVENANCE_MANIFEST
    return None, None


def _ordered_unique(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _manifest_columns(
    node: dict[str, Any] | None,
) -> list[tuple[str, str | None, str | None]]:
    """A manifest node's documented columns, in manifest order:
    (name, data_type, description)."""
    if node is None:
        return []
    columns = node.get("columns")
    if not isinstance(columns, dict):
        return []
    result: list[tuple[str, str | None, str | None]] = []
    for key, column in columns.items():
        if not isinstance(column, dict):
            continue
        name = _text(column.get("name")) or _text(key)
        if name is None:
            continue
        result.append(
            (name, _text(column.get("data_type")), _text(column.get("description")))
        )
    return result


def _declared_column_names(data: dict[str, Any]) -> list[str]:
    """Column names a record declares without the resolver: a
    staging-style ``columns`` list's aliases, then
    ``tracked_columns`` (registrations, satellites)."""
    names: list[str] = []
    columns = data.get("columns")
    if isinstance(columns, list):
        for item in columns:
            if isinstance(item, dict):
                alias = _text(item.get("alias"))
                if alias is not None:
                    names.append(alias)
    names.extend(_values(data, "tracked_columns"))
    return list(_ordered_unique(names))


# --- Record assembly --------------------------------------------------------


def _model_columns(
    entry: InventoryEntry | None,
    node: dict[str, Any] | None,
    *,
    status: str,
    by_name: dict[str, dict[str, Any]],
) -> tuple[tuple[CorpusColumn, ...], bool, str | None]:
    """The unified output-column list for one model record:
    (columns, partial, source). The resolver is authoritative
    where it can determine a generated model's outputs; its
    partial cases fall back to the manifest's documented columns,
    then to names the record itself declares - both marked
    partial, per the resolver's existing honesty rules."""
    manifest_columns = _manifest_columns(node)
    manifest_by_name = {name: (dtype, desc) for name, dtype, desc in manifest_columns}
    loom_wins = status == "generated"
    data = entry.data if entry is not None else {}
    column_docs = data.get("column_docs")
    if not isinstance(column_docs, dict):
        column_docs = {}
    column_types = data.get("column_types")
    if not isinstance(column_types, dict):
        column_types = {}

    def unify_column(
        name: str,
        loom_type: str | None,
        loom_description: str | None,
    ) -> CorpusColumn:
        manifest_type, manifest_description = manifest_by_name.get(
            name, (None, None)
        )
        description, description_provenance = _unify_text(
            loom_description, manifest_description, loom_wins=loom_wins
        )
        if loom_type is not None:
            data_type, data_type_provenance = loom_type, PROVENANCE_LOOM
        elif manifest_type is not None:
            data_type, data_type_provenance = manifest_type, PROVENANCE_MANIFEST
        else:
            data_type, data_type_provenance = None, None
        return CorpusColumn(
            name=name,
            data_type=data_type,
            data_type_provenance=data_type_provenance,
            description=description,
            description_provenance=description_provenance,
        )

    if entry is not None and entry.is_generated:
        resolved = resolve_output_columns(data, by_name)
        if resolved is not None:
            columns = tuple(
                unify_column(
                    column.name,
                    column.fixed_type or _text(column_types.get(column.name)),
                    _text(column_docs.get(column.name)),
                )
                for column in resolved
            )
            return columns, False, COLUMNS_RESOLVER
    if manifest_columns:
        columns = tuple(
            unify_column(name, _text(column_types.get(name)), _text(column_docs.get(name)))
            for name, _, _ in manifest_columns
        )
        return columns, True, COLUMNS_MANIFEST
    declared = _declared_column_names(data)
    if declared:
        columns = tuple(
            unify_column(name, _text(column_types.get(name)), _text(column_docs.get(name)))
            for name in declared
        )
        return columns, True, COLUMNS_DECLARED
    # Nothing derivable and nothing declared: the output set is
    # unknown - an empty list marked partial, never "no columns".
    return (), True, None


class _ManifestView:
    """The manifest facts the corpus derives from, computed once."""

    def __init__(self, document: dict[str, Any], project_name: str | None):
        self.document = document
        self.project_name = project_name
        nodes = document.get("nodes")
        self.nodes: dict[str, Any] = nodes if isinstance(nodes, dict) else {}
        # Model nodes by name, project package preferred where a
        # name exists in several packages.
        self.model_nodes: dict[str, dict[str, Any]] = {}
        self.model_unique_ids: dict[str, str] = {}
        for unique_id, node in self.nodes.items():
            if not isinstance(node, dict) or node.get("resource_type") != "model":
                continue
            name = node.get("name")
            if not isinstance(name, str) or not name:
                continue
            current = self.model_nodes.get(name)
            if current is None or (
                node.get("package_name") == project_name
                and current.get("package_name") != project_name
            ):
                self.model_nodes[name] = node
                self.model_unique_ids[name] = unique_id
        # Reverse depends_on edges across every section, computed
        # once: target unique_id -> dependent unique_ids.
        self._downstream_ids: dict[str, list[str]] = {}
        for dependent_id, item in self._all_items():
            depends_on = item.get("depends_on")
            if not isinstance(depends_on, dict):
                continue
            targets = list(depends_on.get("nodes") or []) + list(
                depends_on.get("macros") or []
            )
            for target in targets:
                if isinstance(target, str):
                    self._downstream_ids.setdefault(target, []).append(
                        dependent_id
                    )
        # unique_id -> display name for every manifest object, for
        # edge translation.
        self.display_names: dict[str, str] = {}
        for unique_id, node in self.nodes.items():
            if isinstance(node, dict) and isinstance(node.get("name"), str):
                self.display_names[unique_id] = node["name"]
        for section in ("sources", "exposures", "macros"):
            entries = document.get(section)
            if not isinstance(entries, dict):
                continue
            for unique_id, item in entries.items():
                if not isinstance(item, dict):
                    continue
                if section == "sources":
                    self.display_names[unique_id] = (
                        f"{item.get('source_name')}.{item.get('name')}"
                        if isinstance(item.get("source_name"), str)
                        and isinstance(item.get("name"), str)
                        else unique_id
                    )
                elif isinstance(item.get("name"), str):
                    self.display_names[unique_id] = item["name"]
        # Model unique_ids carrying manifest test nodes.
        self.tested_model_ids: set[str] = set()
        for node in self.nodes.values():
            if not isinstance(node, dict):
                continue
            if node.get("resource_type") not in ("test", "unit_test"):
                continue
            depends_on = node.get("depends_on")
            if isinstance(depends_on, dict):
                for target in depends_on.get("nodes") or []:
                    if isinstance(target, str):
                        self.tested_model_ids.add(target)

    def model_adjacency(self) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        """Model-graph neighbours by model name, over the
        model/seed/snapshot nodes: (upstream, downstream) per
        model name."""
        graph_kinds = ("model", "seed", "snapshot")
        upstream_by_name: dict[str, list[str]] = {}
        for unique_id, node in self.nodes.items():
            if not isinstance(node, dict):
                continue
            if node.get("resource_type") not in graph_kinds:
                continue
            name = node.get("name")
            if not isinstance(name, str):
                continue
            depends_on = node.get("depends_on")
            neighbours: list[str] = []
            if isinstance(depends_on, dict):
                for target in depends_on.get("nodes") or []:
                    target_node = self.nodes.get(target)
                    if (
                        isinstance(target_node, dict)
                        and target_node.get("resource_type") in graph_kinds
                        and isinstance(target_node.get("name"), str)
                    ):
                        neighbours.append(target_node["name"])
            upstream_by_name[name] = list(_ordered_unique(neighbours))
        downstream_by_name: dict[str, list[str]] = {
            name: [] for name in upstream_by_name
        }
        for name, neighbours in upstream_by_name.items():
            for neighbour in neighbours:
                downstream_by_name.setdefault(neighbour, []).append(name)
        return {
            name: (
                tuple(neighbours),
                tuple(sorted(downstream_by_name.get(name, []))),
            )
            for name, neighbours in upstream_by_name.items()
        }

    def object_edges(
        self, unique_id: str, item: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(upstream, downstream) display names for one manifest
        object, over depends_on edges across all sections."""
        depends_on = item.get("depends_on")
        upstream: list[str] = []
        if isinstance(depends_on, dict):
            for target in depends_on.get("nodes") or []:
                if isinstance(target, str) and target in self.display_names:
                    upstream.append(self.display_names[target])
            for target in depends_on.get("macros") or []:
                if isinstance(target, str) and target in self.display_names:
                    upstream.append(self.display_names[target])
        downstream = [
            self.display_names[dependent_id]
            for dependent_id in self._downstream_ids.get(unique_id, [])
            if dependent_id in self.display_names
        ]
        return (
            tuple(_ordered_unique(upstream)),
            tuple(sorted(set(downstream))),
        )

    def _all_items(self) -> list[tuple[str, dict[str, Any]]]:
        items: list[tuple[str, dict[str, Any]]] = []
        for unique_id, node in self.nodes.items():
            if isinstance(node, dict):
                items.append((unique_id, node))
        for section in ("sources", "exposures", "macros"):
            entries = self.document.get(section)
            if isinstance(entries, dict):
                for unique_id, item in entries.items():
                    if isinstance(item, dict):
                        items.append((unique_id, item))
        return items

    def macro_used_by(self) -> dict[str, tuple[str, ...]]:
        """Macro unique_id -> sorted display names of the models
        and macros that call it (depends_on.macros edges)."""
        used_by: dict[str, set[str]] = {}
        for unique_id, item in self._all_items():
            depends_on = item.get("depends_on")
            if not isinstance(depends_on, dict):
                continue
            caller = self.display_names.get(unique_id)
            if caller is None:
                continue
            for target in depends_on.get("macros") or []:
                if isinstance(target, str):
                    used_by.setdefault(target, set()).add(caller)
        return {
            macro_id: tuple(sorted(callers))
            for macro_id, callers in used_by.items()
        }


def _declared_inputs(data: dict[str, Any]) -> tuple[str, ...]:
    """A Loom-known record's declared model inputs: instance
    inputs, the parent block's model and endpoint hubs, and a
    registration's depends_on / upstream names."""
    values = (
        _values(data, "input_models")
        + _values(data, "depends_on")
        + _values(data, "upstream")
        + _endpoint_values(data, "hub")
        + _parent_values(data, "model")
        + _parent_values(data, "hub")
    )
    return _ordered_unique(values)


def _declared_source_inputs(data: dict[str, Any]) -> tuple[str, ...]:
    reference = data.get("source_ref") or data.get("inline_source")
    if not isinstance(reference, dict):
        return ()
    name = _text(reference.get("name"))
    table = _text(reference.get("table"))
    if name is None or table is None:
        return ()
    return (f"{name}.{table}",)


def _declared_keys(data: dict[str, Any]) -> tuple[str, ...]:
    values = (
        _values(data, "business_key")
        + _values(data, "keys")
        + _endpoint_values(data, "key")
        + _parent_values(data, "key")
    )
    return _ordered_unique(values)


def _pattern_name(entry: InventoryEntry) -> str | None:
    if entry.definition is not None:
        name = entry.definition.get("name")
        if isinstance(name, str) and name:
            return name
    # The instance path carries the pattern file's stem:
    # metadata/instances/<pattern>.yml#instances[<name>].
    path = entry.path.split("#", 1)[0]
    stem = Path(path).stem
    return stem or None


# --- The builder ------------------------------------------------------------


def build_inventory(
    project_dir: str | Path,
    manifest_path: str | Path | None = None,
    *,
    check_freshness: bool = True,
) -> InventoryCorpus:
    """Build the unioned inventory corpus for a project.

    ``manifest_path`` defaults to the project's
    ``target/manifest.json``; when no manifest path is given and
    that file does not exist, the corpus is built in
    metadata-only mode. A manifest that is present is read with
    the established freshness guard (the same one check and
    coverage apply) unless ``check_freshness`` is false - the
    corpus still carries the manifest's generated-at time either
    way, so a caller that tolerates staleness can label it.

    Metadata loading mirrors coverage's: duplicate names are a
    coverage fact on the records, not a load failure here."""
    try:
        root = Path(project_dir).expanduser().resolve(strict=True)
    except OSError as error:
        raise LoomError(
            f"{project_dir}: cannot resolve project directory: {error}"
        ) from error
    if not root.is_dir():
        raise LoomError(f"{root}: project path is not a directory")

    entries, duplicates = _load_project(
        root, reject_duplicate_names=False, validate_relationships=False
    )
    entries_by_name: dict[str, InventoryEntry] = {}
    for entry in entries:
        name = entry.data.get("name")
        if isinstance(name, str) and name not in entries_by_name:
            entries_by_name[name] = entry

    manifest_file: Path | None = None
    document: dict[str, Any] | None = None
    resolved_manifest_path: str | None = None
    manifest_generated: datetime | None = None
    if manifest_path is not None:
        manifest_file, document = read_manifest(
            root, manifest_path, sections=("nodes",)
        )
    elif (root / "target" / "manifest.json").is_file():
        manifest_file, document = read_manifest(
            root, None, sections=("nodes",)
        )
    if document is not None and manifest_file is not None:
        if check_freshness:
            validate_model_freshness(root, manifest_file, document)
        resolved_manifest_path = str(manifest_file)
        manifest_generated = generated_at(manifest_file, document)

    if document is None:
        return _metadata_only_corpus(root, entries, entries_by_name)

    metadata = document.get("metadata")
    project_name = (
        metadata.get("project_name") if isinstance(metadata, dict) else None
    )
    if not isinstance(project_name, str):
        project_name = None
    view = _ManifestView(document, project_name)

    # Coverage state, computed by the existing machinery over
    # the same inputs check and coverage use.
    owned_models: dict[str, list[str]] = {}
    model_file_paths: dict[str, str] = {}
    model_versions: dict[str, list[Any]] = {}
    for unique_id, node in view.nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        if node.get("package_name") != project_name:
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        owned_models.setdefault(name, []).append(str(unique_id))
        model_versions.setdefault(name, []).append(node.get("version"))
        original = node.get("original_file_path")
        if isinstance(original, str) and original:
            model_file_paths.setdefault(name, original.replace("\\", "/"))
    coverage = _inventory_coverage(
        entries, duplicates, owned_models, model_file_paths, root, model_versions
    )
    misplaced = set(coverage.misplaced_generated_models or [])
    duplicate_names = set(coverage.duplicate_inventory_entries) | set(
        coverage.duplicate_manifest_models
    )

    adjacency = view.model_adjacency()
    by_name = {
        entry.data["name"].casefold(): entry.data
        for entry in entries
        if entry.is_generated and isinstance(entry.data.get("name"), str)
    }

    records: list[ModelRecord] = []
    declared_upstream: dict[str, tuple[str, ...]] = {}
    for name, entry in entries_by_name.items():
        declared_upstream[name] = _declared_inputs(entry.data)
    declared_downstream: dict[str, list[str]] = {}
    for name, inputs in declared_upstream.items():
        for target in inputs:
            declared_downstream.setdefault(target, []).append(name)

    for name in sorted(set(entries_by_name) | set(view.model_nodes)):
        entry = entries_by_name.get(name)
        node = view.model_nodes.get(name)
        records.append(
            _full_mode_record(
                name,
                entry,
                node,
                view=view,
                adjacency=adjacency,
                declared_downstream=declared_downstream,
                by_name=by_name,
                misplaced=misplaced,
                duplicate_names=duplicate_names,
            )
        )

    objects = _full_mode_objects(root, document, view, entries)
    return InventoryCorpus(
        mode="full",
        project_name=project_name or _project_config(root).get("name"),
        manifest_path=resolved_manifest_path,
        manifest_generated_at=manifest_generated,
        models=tuple(records),
        objects=tuple(objects),
    )


def _full_mode_record(
    name: str,
    entry: InventoryEntry | None,
    node: dict[str, Any] | None,
    *,
    view: _ManifestView,
    adjacency: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
    declared_downstream: dict[str, list[str]],
    by_name: dict[str, dict[str, Any]],
    misplaced: set[str],
    duplicate_names: set[str],
) -> ModelRecord:
    if entry is not None and node is not None:
        status = "generated" if entry.is_generated else "registered"
    elif entry is not None:
        status = "orphaned"
    else:
        status = "unmanaged"

    data = entry.data if entry is not None else {}
    inputs = _declared_inputs(data) if entry is not None else ()
    columns, columns_partial, columns_source = _model_columns(
        entry, node, status=status, by_name=by_name
    )

    loom_description = _text(data.get("description")) if entry is not None else None
    manifest_description = _text(node.get("description")) if node else None
    description, description_provenance = _unify_text(
        loom_description,
        manifest_description,
        loom_wins=status == "generated",
    )

    if node is not None and name in adjacency:
        upstream, downstream = adjacency[name]
        graph_declared_only = False
    else:
        # No manifest entry: the graph is what the metadata
        # declares - this model's inputs, and the Loom-known
        # models that declare it as an input.
        upstream = inputs
        downstream = tuple(sorted(declared_downstream.get(name, [])))
        graph_declared_only = True

    owner = _text(data.get("owner")) if entry is not None else None
    if owner is None and node is not None:
        meta = node.get("meta")
        if isinstance(meta, dict):
            owner = _text(meta.get("owner"))
    tags = _values(data, "tags") if entry is not None else []
    if not tags and node is not None:
        tags = _values(node, "tags")

    unique_id = view.model_unique_ids.get(name) if node is not None else None
    path: str | None = None
    if node is not None:
        original = node.get("original_file_path")
        if isinstance(original, str) and original:
            path = original.replace("\\", "/")
    if path is None and entry is not None:
        path = entry.path

    return ModelRecord(
        name=name,
        status=status,
        unique_id=unique_id,
        path=path,
        metadata_path=entry.path if entry is not None else None,
        package=(
            node.get("package_name")
            if node is not None and isinstance(node.get("package_name"), str)
            else None
        ),
        generator=(
            _text(data.get("generator"))
            if entry is not None and entry.is_generated
            else None
        ),
        pattern=_pattern_name(entry) if entry is not None and entry.is_generated else None,
        paradigm=entry.paradigm if entry is not None else None,
        classification=(
            _text(data.get("classification"))
            if entry is not None and not entry.is_generated
            else None
        ),
        owner=owner,
        tags=tuple(tags),
        description=description,
        description_provenance=description_provenance,
        documented=description is not None,
        columns_documented=sum(
            1 for column in columns if column.description is not None
        ),
        keys=_declared_keys(data) if entry is not None else (),
        inputs=inputs,
        source_inputs=_declared_source_inputs(data) if entry is not None else (),
        columns=columns,
        columns_partial=columns_partial,
        columns_source=columns_source,
        upstream=upstream,
        downstream=downstream,
        graph_declared_only=graph_declared_only,
        has_owner=owner is not None,
        has_tests=unique_id in view.tested_model_ids if unique_id else False,
        misplaced=name in misplaced,
        duplicate=name in duplicate_names,
    )


def _provenance(package_name: Any, project_name: str | None) -> str:
    if isinstance(package_name, str) and package_name != project_name:
        return "package"
    return "project"


def _full_mode_objects(
    root: Path,
    document: dict[str, Any],
    view: _ManifestView,
    entries: list[InventoryEntry],
) -> list[ObjectRecord]:
    project_name = view.project_name
    records: list[ObjectRecord] = []
    macro_index = manifest_macro_index(document)
    used_by = view.macro_used_by()

    sources = document.get("sources")
    manifest_source_keys: set[tuple[str, str]] = set()
    if isinstance(sources, dict):
        for unique_id, item in sources.items():
            if not isinstance(item, dict):
                continue
            source_name = item.get("source_name")
            table_name = item.get("name")
            if not isinstance(source_name, str) or not isinstance(table_name, str):
                continue
            manifest_source_keys.add((source_name, table_name))
            upstream, downstream = view.object_edges(unique_id, item)
            records.append(
                ObjectRecord(
                    kind="source",
                    name=f"{source_name}.{table_name}",
                    unique_id=unique_id,
                    package=(
                        item.get("package_name")
                        if isinstance(item.get("package_name"), str)
                        else None
                    ),
                    provenance=_provenance(item.get("package_name"), project_name),
                    path=_text(item.get("original_file_path")),
                    description=_text(item.get("description")),
                    upstream=upstream,
                    downstream=downstream,
                )
            )
    # Sources Loom metadata declares (staging source_ref /
    # inline_source) that the manifest does not carry.
    declared_sources: set[tuple[str, str]] = set()
    for entry in entries:
        for identity in _declared_source_inputs(entry.data):
            source_name, _, table_name = identity.partition(".")
            declared_sources.add((source_name, table_name))
    for source_name, table_name in sorted(declared_sources - manifest_source_keys):
        records.append(
            ObjectRecord(
                kind="source",
                name=f"{source_name}.{table_name}",
                provenance="project",
            )
        )

    kind_by_resource = {"seed": "seed", "snapshot": "snapshot", "test": "test"}
    for unique_id, node in view.nodes.items():
        resource_type = node.get("resource_type")
        if resource_type == "unit_test":
            kind = "test"
        elif resource_type in kind_by_resource:
            kind = kind_by_resource[resource_type]
        else:
            continue
        upstream, downstream = view.object_edges(unique_id, node)
        records.append(
            ObjectRecord(
                kind=kind,
                name=node["name"],
                unique_id=unique_id,
                package=(
                    node.get("package_name")
                    if isinstance(node.get("package_name"), str)
                    else None
                ),
                provenance=_provenance(node.get("package_name"), project_name),
                path=_text(node.get("original_file_path")),
                description=_text(node.get("description")),
                upstream=upstream,
                downstream=downstream,
            )
        )

    exposures = document.get("exposures")
    if isinstance(exposures, dict):
        for unique_id, item in exposures.items():
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            upstream, downstream = view.object_edges(unique_id, item)
            records.append(
                ObjectRecord(
                    kind="exposure",
                    name=item["name"],
                    unique_id=unique_id,
                    package=(
                        item.get("package_name")
                        if isinstance(item.get("package_name"), str)
                        else None
                    ),
                    provenance=_provenance(item.get("package_name"), project_name),
                    path=_text(item.get("original_file_path")),
                    description=_text(item.get("description")),
                    upstream=upstream,
                    downstream=downstream,
                )
            )

    macros = document.get("macros")
    if isinstance(macros, dict):
        for unique_id, item in macros.items():
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            package_name = item.get("package_name")
            indexed = macro_index.get((package_name, item["name"]), [])
            indexed_by_name = {arg["name"]: arg for arg in indexed}
            arguments: list[MacroArgument] = []
            declared_arguments = item.get("arguments")
            if isinstance(declared_arguments, list):
                for argument in declared_arguments:
                    if not isinstance(argument, dict) or not isinstance(
                        argument.get("name"), str
                    ):
                        continue
                    arguments.append(
                        MacroArgument(
                            name=argument["name"],
                            type=_text(argument.get("type")),
                            has_default=bool(
                                indexed_by_name.get(argument["name"], {}).get(
                                    "has_default"
                                )
                            ),
                        )
                    )
            if not arguments:
                arguments = [
                    MacroArgument(
                        name=arg["name"], has_default=bool(arg["has_default"])
                    )
                    for arg in indexed
                ]
            upstream, downstream = view.object_edges(unique_id, item)
            records.append(
                ObjectRecord(
                    kind="macro",
                    name=item["name"],
                    unique_id=unique_id,
                    package=package_name if isinstance(package_name, str) else None,
                    provenance=_provenance(package_name, project_name),
                    path=_text(item.get("original_file_path")),
                    description=_text(item.get("description")),
                    upstream=upstream,
                    downstream=downstream,
                    arguments=tuple(arguments),
                    used_by=used_by.get(unique_id, ()),
                )
            )

    records.sort(key=lambda record: (record.kind, record.name))
    return records


def corpus_from_entries(
    project_dir: str | Path, entries: list[InventoryEntry]
) -> InventoryCorpus:
    """The metadata-only corpus over entries a caller has
    already loaded and validated (generate's and scoped
    validate's pools): the same derivation
    :func:`build_inventory` performs in metadata-only mode,
    without a second project load or any manifest read."""
    root = Path(project_dir).expanduser().resolve()
    entries_by_name: dict[str, InventoryEntry] = {}
    for entry in entries:
        name = entry.data.get("name")
        if isinstance(name, str) and name not in entries_by_name:
            entries_by_name[name] = entry
    return _metadata_only_corpus(root, list(entries), entries_by_name)


def _metadata_only_corpus(
    root: Path,
    entries: list[InventoryEntry],
    entries_by_name: dict[str, InventoryEntry],
) -> InventoryCorpus:
    """The metadata-only corpus: Loom-known objects only, with
    statuses collapsed to what metadata alone can prove and the
    graph read from declarations."""
    declared_upstream: dict[str, tuple[str, ...]] = {}
    for name, entry in entries_by_name.items():
        declared_upstream[name] = _declared_inputs(entry.data)
    declared_downstream: dict[str, list[str]] = {}
    for name, inputs in declared_upstream.items():
        for target in inputs:
            declared_downstream.setdefault(target, []).append(name)

    by_name = {
        entry.data["name"].casefold(): entry.data
        for entry in entries
        if entry.is_generated and isinstance(entry.data.get("name"), str)
    }
    records: list[ModelRecord] = []
    for name in sorted(entries_by_name):
        entry = entries_by_name[name]
        status = "generated" if entry.is_generated else "registered"
        columns, columns_partial, columns_source = _model_columns(
            entry, None, status=status, by_name=by_name
        )
        description = _text(entry.data.get("description"))
        records.append(
            ModelRecord(
                name=name,
                status=status,
                path=entry.path,
                metadata_path=entry.path,
                generator=(
                    _text(entry.data.get("generator")) if entry.is_generated else None
                ),
                pattern=_pattern_name(entry) if entry.is_generated else None,
                paradigm=entry.paradigm,
                classification=(
                    _text(entry.data.get("classification"))
                    if not entry.is_generated
                    else None
                ),
                owner=_text(entry.data.get("owner")),
                tags=tuple(_values(entry.data, "tags")),
                description=description,
                description_provenance=(
                    PROVENANCE_LOOM if description is not None else None
                ),
                documented=description is not None,
                columns_documented=sum(
                    1 for column in columns if column.description is not None
                ),
                keys=_declared_keys(entry.data),
                inputs=declared_upstream[name],
                source_inputs=_declared_source_inputs(entry.data),
                columns=columns,
                columns_partial=columns_partial,
                columns_source=columns_source,
                upstream=declared_upstream[name],
                downstream=tuple(sorted(declared_downstream.get(name, []))),
                graph_declared_only=True,
                has_owner=_text(entry.data.get("owner")) is not None,
            )
        )

    # Tier 2 in metadata-only mode: the sources Loom metadata
    # declares - instance source declarations and the project's
    # authored source YAML.
    source_identities: set[tuple[str, str]] = set()
    for entry in entries:
        for identity in _declared_source_inputs(entry.data):
            source_name, _, table_name = identity.partition(".")
            source_identities.add((source_name, table_name))
    native_paths: dict[tuple[str, str], str] = {}
    config = _project_config(root)
    for table in _native_sources(root, [], config):
        identity = (table.source_name, table.table_name)
        source_identities.add(identity)
        native_paths[identity] = table.path
    objects = [
        ObjectRecord(
            kind="source",
            name=f"{source_name}.{table_name}",
            provenance="project",
            path=native_paths.get((source_name, table_name)),
        )
        for source_name, table_name in sorted(source_identities)
    ]
    return InventoryCorpus(
        mode="metadata-only",
        project_name=config.get("name"),
        manifest_path=None,
        manifest_generated_at=None,
        models=tuple(records),
        objects=tuple(objects),
    )
