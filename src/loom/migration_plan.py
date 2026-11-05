from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.errors import LoomError
from loom.inventory import validate_project
from loom.manifest import read_manifest, validate_model_freshness


def _inventory_states(project_root: Path) -> dict[str, str]:
    """Loom's three-state view of the project's models.

    The plan annotates manifest models with their inventory state so
    practitioners can see what Loom controls, what it merely knows,
    and what it has never seen. Invalid inventory fails explicitly -
    an unannotated plan presented as state-checked would mislead.
    """
    states: dict[str, str] = {}
    for entry in validate_project(project_root):
        name = entry.data.get("name")
        if isinstance(name, str):
            states[name] = (
                "controlled_by_loom"
                if entry.is_generated
                else "known_not_controlled"
            )
    return states


_CONSUMER_SECTIONS = (
    "nodes",
    "sources",
    "exposures",
    "metrics",
    "semantic_models",
    "saved_queries",
    "unit_tests",
)


@dataclass(frozen=True)
class _ManifestResource:
    section: str
    data: dict[str, Any]
    dependencies: list[str]
    is_owned_model: bool


def _read_manifest(
    project_root: Path, manifest_path: str | Path | None
) -> tuple[Path, dict[str, Any]]:
    manifest = (
        Path(manifest_path)
        if manifest_path is not None
        else Path("target/manifest.json")
    )
    if not manifest.is_absolute():
        manifest = project_root / manifest
    if not manifest.is_file():
        raise LoomError(
            f"{manifest}: dbt manifest is required for migration planning; "
            "run dbt to produce target/manifest.json or supply manifest_path"
        )

    return read_manifest(project_root, manifest_path, sections=("nodes",))


def _dependencies(
    manifest: Path, unique_id: str, resource: dict[str, Any]
) -> list[str]:
    depends_on = resource.get("depends_on")
    if depends_on is None:
        return []
    if not isinstance(depends_on, dict):
        raise LoomError(
            f"{manifest}: manifest resource {unique_id!r} has invalid depends_on"
        )
    nodes = depends_on.get("nodes", [])
    if not isinstance(nodes, list) or any(not isinstance(item, str) for item in nodes):
        raise LoomError(
            f"{manifest}: manifest resource {unique_id!r} has invalid depends_on.nodes"
        )
    return nodes


def _named_model(
    manifest: Path,
    nodes: dict[str, Any],
    project_name: str,
    name: str,
) -> tuple[str, dict[str, Any]]:
    matches = [
        (unique_id, node)
        for unique_id, node in nodes.items()
        if isinstance(unique_id, str)
        and isinstance(node, dict)
        and node.get("resource_type") == "model"
        and node.get("package_name") == project_name
        and node.get("name") == name
    ]
    if not matches:
        raise LoomError(f"{manifest}: project-owned dbt model {name!r} was not found")
    if len(matches) > 1:
        raise LoomError(
            f"{manifest}: project-owned dbt model {name!r} is ambiguous: "
            + ", ".join(sorted(unique_id for unique_id, _ in matches))
        )
    return matches[0]


def _manifest_resources(
    manifest: Path, document: dict[str, Any], project_name: str
) -> dict[str, _ManifestResource]:
    resources_by_id: dict[str, _ManifestResource] = {}
    for section in _CONSUMER_SECTIONS:
        resources = document.get(section, {})
        if not isinstance(resources, dict):
            raise LoomError(f"{manifest}: {section} must be a JSON object")
        for unique_id, resource in resources.items():
            if not isinstance(unique_id, str) or not isinstance(resource, dict):
                raise LoomError(
                    f"{manifest}: {section} must contain JSON objects keyed by strings"
                )
            if unique_id in resources_by_id:
                raise LoomError(
                    f"{manifest}: duplicate manifest resource {unique_id!r}"
                )
            is_owned_model = (
                section == "nodes"
                and resource.get("resource_type") == "model"
                and resource.get("package_name") == project_name
            )
            resources_by_id[unique_id] = _ManifestResource(
                section,
                resource,
                _dependencies(manifest, unique_id, resource),
                is_owned_model,
            )
    return resources_by_id


def _model_dependents(
    resources: dict[str, _ManifestResource],
    old_model_id: str,
    states: dict[str, str] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, int],
    dict[str, list[str]],
]:
    owned_models = {
        unique_id: resource.data
        for unique_id, resource in resources.items()
        if resource.is_owned_model
    }
    reverse_edges: dict[str, list[str]] = {}
    for unique_id, resource in resources.items():
        for dependency in resource.dependencies:
            reverse_edges.setdefault(dependency, []).append(unique_id)

    distances: dict[str, int] = {}
    paths: dict[str, list[str]] = {}
    queue = deque([(old_model_id, 0, [old_model_id])])
    while queue:
        current, distance, path = queue.popleft()
        for dependent in sorted(reverse_edges.get(current, [])):
            if dependent == old_model_id or dependent in distances:
                continue
            distances[dependent] = distance + 1
            paths[dependent] = [*path, dependent]
            queue.append((dependent, distance + 1, paths[dependent]))

    def detail(unique_id: str) -> dict[str, Any]:
        node = owned_models[unique_id]
        result: dict[str, Any] = {
            "name": node["name"],
            "unique_id": unique_id,
            "distance": distances[unique_id],
            "path": paths[unique_id],
        }
        if states is not None:
            result["state"] = states.get(node["name"], "unknown_to_loom")
        return result

    direct = [
        detail(item)
        for item in sorted(distances)
        if item in owned_models and distances[item] == 1
    ]
    transitive = [
        detail(item)
        for item in sorted(distances)
        if item in owned_models and distances[item] > 1
    ]
    return direct, transitive, distances, paths


def _observed_consumers(
    resources: dict[str, _ManifestResource],
    distances: dict[str, int],
    paths: dict[str, list[str]],
) -> list[dict[str, Any]]:
    consumers = []
    for unique_id, resource in resources.items():
        if unique_id not in distances or resource.is_owned_model:
            continue
        consumers.append(
            {
                "name": str(resource.data.get("name", unique_id)),
                "unique_id": unique_id,
                "resource_type": str(
                    resource.data.get("resource_type", resource.section)
                ),
                "distance": distances[unique_id],
                "path": paths[unique_id],
                "depends_on_resources": sorted(
                    set(resource.dependencies) & distances.keys()
                ),
            }
        )
    return sorted(
        consumers, key=lambda item: (item["resource_type"], item["unique_id"])
    )


def migration_plan(
    project_dir: str | Path,
    from_model: str,
    to_model: str,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a read-only, manifest-backed migration inventory and phased plan."""
    project_root = Path(project_dir).expanduser()
    try:
        project_root = project_root.resolve(strict=True)
    except OSError as error:
        raise LoomError(
            f"{project_root}: cannot resolve project directory: {error}"
        ) from error
    if not project_root.is_dir():
        raise LoomError(f"{project_root}: project path is not a directory")
    if not isinstance(from_model, str) or not from_model.strip():
        raise LoomError("--from must be a non-empty model name")
    if not isinstance(to_model, str) or not to_model.strip():
        raise LoomError("--to must be a non-empty model name")
    if from_model == to_model:
        raise LoomError("--from and --to must identify distinct models")

    manifest, document = _read_manifest(project_root, manifest_path)
    validate_model_freshness(project_root, manifest, document)
    metadata = document["metadata"]
    nodes = document["nodes"]
    old_id, old_node = _named_model(
        manifest, nodes, metadata["project_name"], from_model
    )
    new_id, new_node = _named_model(manifest, nodes, metadata["project_name"], to_model)
    resources = _manifest_resources(manifest, document, metadata["project_name"])
    states = _inventory_states(project_root)
    direct, transitive, distances, paths = _model_dependents(
        resources, old_id, states
    )
    observed = _observed_consumers(resources, distances, paths)
    return {
        # The payload names the CLI operation that produced
        # it - the flat verb `loom plan`.
        "operation": "plan",
        "from": {
            "name": old_node["name"],
            "unique_id": old_id,
            "state": states.get(old_node["name"], "unknown_to_loom"),
        },
        "to": {
            "name": new_node["name"],
            "unique_id": new_id,
            "state": states.get(new_node["name"], "unknown_to_loom"),
        },
        "inventory": {
            "basis": "dbt manifest depends_on.nodes",
            "direct_dependents": direct,
            "transitive_dependents": transitive,
            "observed_manifest_consumers": observed,
            "external_consumers": {
                "status": "unknown",
                "reason": (
                    "The manifest cannot enumerate every downstream system or "
                    "consumer; this inventory is not complete."
                ),
            },
        },
        "migration_phases": [
            {
                "phase": "build_alongside",
                "action": "Build the replacement model alongside the existing model.",
            },
            {
                "phase": "prove_invariants",
                "action": (
                    "Define and validate the required invariants for each model "
                    "and consumer; do not assume equivalence."
                ),
            },
            {
                "phase": "move_dependent_refs_deliberately",
                "action": (
                    "Review each listed dependent and move its reference deliberately "
                    "only after its contract is verified."
                ),
            },
            {
                "phase": "retire_later",
                "action": (
                    "Retire the old model only after all known and external consumers "
                    "have been checked."
                ),
            },
        ],
        "limitations": [
            "No model files or references were edited.",
            "No equivalence between the old and replacement models is asserted.",
            "Do not compare or aggregate row counts across models; their grains may differ.",
            "Manifest-visible dependents do not constitute a complete consumer inventory.",
        ],
    }
