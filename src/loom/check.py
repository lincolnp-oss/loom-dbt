from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from loom.declared_generic_tests import (
    carried_generic_tests,
    render_test_entry,
    rendered_test_signature,
)
from loom.errors import LoomError
from loom.inventory import (
    InventoryEntry,
    _inventory_coverage,
    _manifest_models,
    validate_project,
)
from loom.manifest import (
    manifest_macro_index,
    read_manifest,
    verify_macro_reference,
)


def _coverage_errors(
    project_dir: Path,
    manifest_path: str | Path | None,
    entries: list[InventoryEntry],
    *,
    only_models: set[str] | None = None,
) -> list[str]:
    """Report coverage for entries already validated with duplicate names rejected.

    ``only_models`` (a ``--select`` narrowing) restricts the
    manifest side of the comparison to the same names the
    entries were already narrowed to."""
    manifest = (
        Path(manifest_path)
        if manifest_path is not None
        else Path("target/manifest.json")
    )
    if not manifest.is_absolute():
        manifest = project_dir / manifest
    if not manifest.is_file():
        return [
            f"{manifest}: dbt manifest is required for inventory coverage; "
            "run `dbt parse` in the project first "
            "or supply manifest_path"
        ]

    try:
        owned_models, model_file_paths, model_versions = _manifest_models(
            project_dir, manifest
        )
        if only_models is not None:
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
        coverage = _inventory_coverage(
            entries, {}, owned_models, model_file_paths, project_dir, model_versions
        )
    except LoomError as error:
        return [str(error)]
    errors: list[str] = []
    if coverage.missing_models:
        errors.append(
            "inventory coverage: project models missing inventory records: "
            + ", ".join(coverage.missing_models)
        )
    if coverage.orphan_inventory_entries:
        errors.append(
            "inventory coverage: inventory records without project models: "
            + ", ".join(coverage.orphan_inventory_entries)
        )
    if coverage.misplaced_generated_models:
        errors.append(
            "inventory coverage: generated models whose manifest file "
            "is not at the expected output path (layout metadata vs "
            "reality): " + ", ".join(coverage.misplaced_generated_models)
            + " - this usually means an adoption step is still pending: "
            "complete the swap (replace the original model with the "
            "generated file), or run `loom generate --write` and "
            "re-run `dbt parse`, then re-check"
        )
    for name, paths in coverage.duplicate_inventory_entries.items():
        errors.append(
            f"inventory coverage: duplicate inventory name {name!r}: "
            + ", ".join(paths)
        )
    for name, node_ids in coverage.duplicate_manifest_models.items():
        errors.append(
            f"inventory coverage: duplicate project model name {name!r}: "
            + ", ".join(node_ids)
        )
    for name, versions in (coverage.versioned_manifest_models or {}).items():
        errors.append(
            f"inventory coverage: project model {name!r} has multiple "
            f"dbt versions ({', '.join(versions)}); Loom tracks models "
            "by name and does not support dbt model versions, so "
            "coverage cannot pass for this model until its versions "
            "are collapsed to a single model"
        )
    return errors


def _manifest_generic_tests(
    document: dict,
) -> dict[str, set[tuple[str, str | None]]]:
    """Per model name, the (test, column) pairs the manifest's
    generic test nodes attest - keyed on the ATTACHED model
    where the manifest carries one (a relationships test also
    depends on its target, and the declaration belongs to the
    attached model)."""
    by_model: dict[str, set[tuple[str, str | None]]] = {}
    for node in (document.get("nodes") or {}).values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        metadata = node.get("test_metadata")
        if not isinstance(metadata, dict):
            continue
        test_name = metadata.get("name")
        if not isinstance(test_name, str) or not test_name:
            continue
        model_names: set[str] = set()
        attached = node.get("attached_node")
        if isinstance(attached, str) and attached.startswith("model."):
            model_names.add(attached.rsplit(".", 1)[-1])
        else:
            for dep in (node.get("depends_on") or {}).get("nodes") or []:
                if isinstance(dep, str) and dep.startswith("model."):
                    model_names.add(dep.rsplit(".", 1)[-1])
        column = node.get("column_name")
        pair = (
            test_name.casefold(),
            column.casefold() if isinstance(column, str) and column else None,
        )
        for model_name in model_names:
            by_model.setdefault(model_name, set()).add(pair)
    return by_model


def declared_test_problems(
    entries: list[InventoryEntry], document: dict | None
) -> list[str]:
    """`generic_tests` declarations the manifest does not attest
    for the model: the declaration generates a test
    dbt has not compiled - typically the manifest predates the
    declaration or the last generate. A warning at check; an
    error under `--strict`. Matching is by test name and column
    on the attached model, case-insensitive."""
    if document is None:
        return []
    attested = _manifest_generic_tests(document)
    problems: list[str] = []
    for entry in entries:
        if entry.path.startswith("local/registrations/"):
            continue
        declared = carried_generic_tests(entry.data)
        if not declared:
            continue
        model_name = entry.data.get("name")
        present = (
            attested.get(model_name, set())
            if isinstance(model_name, str)
            else set()
        )
        for test in declared:
            pair = (
                test["test"].casefold(),
                (test.get("column_name") or "").casefold() or None,
            )
            if pair not in present:
                where = (
                    f" on column '{test['column_name']}'"
                    if test.get("column_name")
                    else ""
                )
                problems.append(
                    f"{entry.path}: generic_tests declares "
                    f"'{test['test']}'{where} for model "
                    f"'{model_name}', but the manifest records no "
                    "such test for that model - re-run "
                    "`loom generate` and `dbt parse` so the "
                    "manifest attests the declaration, or drop it"
                )
    return problems


def _declaration_config_clause(test: Mapping[str, Any]) -> str:
    """A warning clause naming one declaration's `config`
    - its pairs in key order, or its absence. Two
    declarations can duplicate the same emitted test while
    differing only in config (config is no part of the
    test's identity); naming each declaration's config in
    its warning keeps the warnings distinguishable, so the
    practitioner can tell which declaration carries the
    config worth keeping and which to drop."""
    config = test.get("config")
    if not isinstance(config, dict) or not config:
        return " (no config)"
    pairs = []
    for key in sorted(config, key=str):
        value = config[key]
        rendered = (
            value
            if isinstance(value, (str, int, float, bool))
            else json.dumps(value, sort_keys=True, default=str)
        )
        pairs.append(f"{key}: {rendered}")
    return f" (config: {', '.join(pairs)})"


def duplicate_test_warnings(entries: list[InventoryEntry]) -> list[str]:
    """`generic_tests` declarations that duplicate a data
    test the model's own generator already emits: same test
    name, same column, same kwargs - the identity dbt names
    a generic test by. Generation emits such a test once,
    merging any config the declaration carries onto the
    emitted entry, so the declaration's only remaining
    effect is its config; this warning is where the
    redundancy is named, so the practitioner can drop the
    declaration unless that config is its point. Each
    warning names its own declaration's config (or that it
    carries none), so two declarations differing only in
    config warn in distinguishable forms. A declaration
    whose kwargs differ from the emitted test's is a
    distinct test to dbt, is carried in full, and does
    not warn. The emitted set is derived from the generator's
    own rendered tests document (dialect-independent; the
    reference dialect is only the render vehicle)."""
    from loom.renderers import get_renderer

    warnings: list[str] = []
    for entry in entries:
        if not entry.is_generated:
            continue
        declared = carried_generic_tests(entry.data)
        if not declared:
            continue
        generator = entry.data.get("generator")
        try:
            renderer = get_renderer(generator, "duckdb")
            test_yaml = renderer.render_tests(
                entry.data, renderer.dialect, None
            )
        except (LoomError, KeyError, TypeError):
            continue
        if not test_yaml:
            continue
        document = yaml.safe_load(test_yaml) or {}
        emitted: set[tuple[tuple[str, str], str | None]] = set()
        for model in document.get("models") or []:
            for rendered in model.get("tests") or []:
                signature = rendered_test_signature(rendered)
                if signature is not None:
                    emitted.add((signature, None))
            for column in model.get("columns") or []:
                column_name = str(column.get("name") or "").casefold() or None
                for rendered in column.get("tests") or []:
                    signature = rendered_test_signature(rendered)
                    if signature is not None:
                        emitted.add((signature, column_name))
        for test in declared:
            signature = rendered_test_signature(render_test_entry(test))
            pair = (
                signature,
                (test.get("column_name") or "").casefold() or None,
            )
            if signature is not None and pair in emitted:
                where = (
                    f" on column '{test['column_name']}'"
                    if test.get("column_name")
                    else ""
                )
                warnings.append(
                    f"{entry.path}: generic_tests declares "
                    f"'{test['test']}'{where}"
                    f"{_declaration_config_clause(test)}, which "
                    f"the {generator} generator already emits for "
                    f"model '{entry.data.get('name')}' - "
                    "generation emits the test once, merging "
                    "any config the declaration carries onto "
                    "the emitted test; drop the declaration "
                    "from generic_tests unless its config is "
                    "the point"
                )
    return warnings


def check_project(
    project_dir: str | Path,
    manifest_path: str | Path | None = None,
    *,
    strict: bool = False,
    only_models: set[str] | None = None,
) -> list[str]:
    """Check manifest-backed inventory coverage without mutation.

    ``only_models`` narrows every check to the named models
    (the ``--select`` pool): coverage compares only those
    models, macro references are verified only on their
    entries, and the strict declared-test rule reads only
    their declarations. Validation itself stays whole-project:
    scoping validation is `validate --select`'s semantics."""
    root = Path(project_dir).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise LoomError(f"{root}: cannot resolve project directory: {error}") from error
    if not root.is_dir():
        raise LoomError(f"{root}: project path is not a directory")

    entries = validate_project(root)
    if only_models is not None:
        entries = [
            entry
            for entry in entries
            if entry.data.get("name") in only_models
        ]
    errors = _coverage_errors(root, manifest_path, entries, only_models=only_models)
    errors.extend(_macro_errors(root, manifest_path, entries))
    if errors:
        raise LoomError("\n".join(errors))
    if strict:
        # --strict promotes the declared-test attestation rule
        # from warning to error; the duplicate rule stays a
        # warning under --strict too.
        from loom.manifest import read_manifest

        _manifest_file, document = read_manifest(
            root, manifest_path, sections=("nodes",)
        )
        problems = declared_test_problems(entries, document)
        if problems:
            raise LoomError("\n".join(problems))
    return []


def _macro_errors(
    project_dir: Path,
    manifest_path: str | Path | None,
    entries: list[InventoryEntry],
) -> list[str]:
    """Verify generated instances' staging macro references
    against the manifest (Tier 1): every `macro:`
    reference - bare (project) or dotted (package) - must exist
    in the manifest's macro index, and the call's supplied
    argument count (the column plus `macro_args`) must fit the
    manifest signature's arity including defaults. A manifest
    that cannot be read adds nothing here: the coverage check
    already reports manifest problems."""
    manifest = (
        Path(manifest_path)
        if manifest_path is not None
        else Path("target/manifest.json")
    )
    if not manifest.is_absolute():
        manifest = project_dir / manifest
    if not manifest.is_file():
        return []
    try:
        _, document = read_manifest(
            project_dir, manifest, sections=("nodes",)
        )
    except LoomError:
        return []
    index = manifest_macro_index(document)
    metadata = document.get("metadata")
    project_name = (
        metadata.get("project_name") if isinstance(metadata, dict) else None
    )
    errors: list[str] = []
    for entry in entries:
        if not entry.is_generated:
            continue
        columns = entry.data.get("columns")
        if not isinstance(columns, list):
            continue
        for position, column in enumerate(columns):
            if not isinstance(column, dict):
                continue
            macro = column.get("macro")
            if not isinstance(macro, str):
                continue
            macro_args = column.get("macro_args")
            supplied = 1 + (
                len(macro_args) if isinstance(macro_args, list) else 0
            )
            problem = verify_macro_reference(
                index, macro, supplied, project_name=project_name
            )
            if problem is not None:
                errors.append(
                    f"{entry.path}: columns[{position}].macro: {problem}"
                )
    return errors
