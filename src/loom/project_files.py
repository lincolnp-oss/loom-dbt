"""Shared project-file parsing and traversal, independent of Loom metadata semantics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from loom.errors import LoomError


# Metadata parsing is on every command's path and dominates at
# scale, so prefer libyaml's C loader when the installed PyYAML provides
# it; the pure-Python SafeLoader is the fallback for builds without
# libyaml. The two are behaviour-identical for Loom's inputs (same
# resolver, same safe tag set); the duplicate-key constructor below is
# registered on the chosen base either way.
_UniqueKeyLoaderBase = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _UniqueKeySafeLoader(_UniqueKeyLoaderBase):  # type: ignore[valid-type,misc]
    pass


def _construct_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    # Merge keys (`<<: *template`): PyYAML expands them inside
    # SafeConstructor.construct_mapping via flatten_mapping -
    # the step this override originally dropped, which made the
    # loader reject every merge key with "could not determine a
    # constructor for the tag 'tag:yaml.org,2002:merge'".
    # Flatten with PyYAML's own routine (the one dbt's parsing
    # runs), so merge semantics are the reference semantics by
    # construction: merged pairs are spliced before the node's
    # own pairs, precedence-ordered, and explicit keys override
    # merged ones. Duplicate detection therefore scopes to the
    # node's OWN key nodes, snapshotted before flattening: an
    # own key repeated is a duplicate; an own key overriding a
    # merged key is the merge idiom working, not a duplicate.
    # (A merged source's own internal duplicates are checked
    # when that mapping is constructed in its own right - at its
    # definition site, for anchored templates; a mapping that
    # exists only as an inline merge-source literal is the one
    # blind spot.)
    own_key_nodes = {
        id(key_node)
        for key_node, _value_node in node.value
        if key_node.tag != "tag:yaml.org,2002:merge"
    }
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    own_keys: set[Any] = set()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if id(key_node) in own_key_nodes:
            if key in own_keys:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            own_keys.add(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _yaml_error(path: str, error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    location = f":{mark.line + 1}:{mark.column + 1}" if mark else ""
    problem = getattr(error, "problem", None) or str(error).splitlines()[0]
    return f"{path}{location}: invalid YAML: {problem}"


def _scan_error(error: OSError) -> None:
    raise LoomError(
        f"{error.filename}: cannot scan project directory: {error}"
    ) from error


def _project_config(project_root: Path) -> dict[str, Any]:
    project_file = project_root / "dbt_project.yml"
    if not project_file.exists():
        return {}
    try:
        document = yaml.load(
            project_file.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader
        )
    except yaml.YAMLError as error:
        raise LoomError(_yaml_error("dbt_project.yml", error)) from error
    except (OSError, UnicodeDecodeError) as error:
        raise LoomError(f"dbt_project.yml: cannot read dbt project: {error}") from error
    if not isinstance(document, dict):
        raise LoomError("dbt_project.yml: expected a YAML mapping")
    return document


def _configured_paths(config: dict[str, Any], resource_type: str) -> list[str]:
    paths = config.get(f"{resource_type}-paths", [f"{resource_type}s"])
    if not isinstance(paths, list) or any(
        not isinstance(path, str) or not path for path in paths
    ):
        raise LoomError(
            f"dbt_project.yml: {resource_type}-paths must be a list of non-empty paths"
        )
    return paths


def _output_root(config: dict[str, Any], resource_type: str) -> str:
    """The single directory generation writes to for a resource type.

    dbt scans every entry of ``<resource_type>-paths``; generation
    needs one deterministic home, so it uses the first configured
    path, or the conventional default (``models`` / ``snapshots``)
    when none is configured. Entries must be relative to the
    project root, as dbt requires.
    """
    root = _configured_paths(config, resource_type)[0]
    if Path(root).is_absolute():
        raise LoomError(
            f"dbt_project.yml: {resource_type}-paths entries must be "
            f"relative to the project root: {root!r}"
        )
    return root


def _scan_files(project_root: Path, relative_dir: str) -> list[Path]:
    scan_root = project_root / relative_dir
    if not scan_root.exists():
        return []
    if scan_root.is_symlink():
        resolved = scan_root.resolve()
        if not resolved.is_relative_to(project_root):
            raise LoomError(f"{scan_root}: inventory directory escapes project root")
    if not scan_root.is_dir():
        raise LoomError(f"{scan_root}: expected a directory")

    paths: list[Path] = []
    for current, directories, filenames in os.walk(
        scan_root, followlinks=False, onerror=_scan_error
    ):
        current_path = Path(current)
        directories.sort()
        filenames.sort()
        for directory in list(directories):
            candidate = current_path / directory
            if candidate.is_symlink():
                resolved = candidate.resolve()
                if not resolved.is_relative_to(project_root):
                    raise LoomError(
                        f"{candidate}: directory symlink escapes project root"
                    )
                directories.remove(directory)
        for filename in filenames:
            # Loom metadata is YAML under either conventional extension;
            # matching only .yml silently ignored .yaml files.
            if not filename.endswith((".yml", ".yaml")):
                continue
            candidate = current_path / filename
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise LoomError(
                    f"{candidate}: cannot resolve inventory file: {error}"
                ) from error
            if not resolved.is_relative_to(project_root):
                raise LoomError(f"{candidate}: file symlink escapes project root")
            if not resolved.is_file():
                raise LoomError(f"{candidate}: expected a regular file")
            paths.append(candidate)
    return sorted(paths, key=lambda path: path.relative_to(project_root).as_posix())


def _model_yaml_files(
    project_root: Path,
    model_paths: list[str],
    *,
    suffixes: frozenset[str] = frozenset({".yml", ".yaml"}),
    path_label: str = "model",
    file_label: str = "dbt YAML file",
) -> list[Path]:
    files: list[Path] = []
    for model_path in model_paths:
        configured_path = Path(model_path)
        if configured_path.is_absolute():
            raise LoomError(
                f"dbt_project.yml: {path_label} path {model_path!r} must be project-relative"
            )
        directory = project_root / configured_path
        try:
            resolved_directory = directory.resolve()
        except OSError as error:
            raise LoomError(
                f"{directory}: cannot resolve dbt {path_label} path: {error}"
            ) from error
        if not resolved_directory.is_relative_to(project_root):
            raise LoomError(
                f"dbt_project.yml: {path_label} path {model_path!r} escapes project root"
            )
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise LoomError(f"{directory}: dbt {path_label} path is not a directory")

        for current, directories, filenames in os.walk(
            directory, followlinks=False, onerror=_scan_error
        ):
            current_path = Path(current)
            directories.sort()
            filenames.sort()
            for child in list(directories):
                candidate = current_path / child
                if candidate.is_symlink():
                    try:
                        resolved = candidate.resolve(strict=True)
                    except OSError as error:
                        raise LoomError(
                            f"{candidate}: cannot resolve dbt {path_label} directory: {error}"
                        ) from error
                    if not resolved.is_relative_to(project_root):
                        raise LoomError(
                            f"{candidate}: dbt {path_label} directory symlink escapes project root"
                        )
                    directories.remove(child)
            for filename in filenames:
                if Path(filename).suffix.lower() not in suffixes:
                    continue
                candidate = current_path / filename
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as error:
                    raise LoomError(
                        f"{candidate}: cannot resolve {file_label}: {error}"
                    ) from error
                if not resolved.is_relative_to(project_root):
                    raise LoomError(
                        f"{candidate}: {file_label} symlink escapes project root"
                    )
                if resolved.is_file():
                    files.append(candidate)
    return sorted(
        set(files), key=lambda path: path.relative_to(project_root).as_posix()
    )
