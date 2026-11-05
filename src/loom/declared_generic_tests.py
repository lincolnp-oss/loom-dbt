"""Declared generic (data) tests, carried by Loom.

Practitioners declare generic tests in instance metadata
(`generic_tests`); Loom carries them verbatim into the model's
single generated schema document, merged with the properties
entry and any generator-emitted data tests. Carriage exists
because dbt's single-description rule leaves the declaration
nowhere else to live for a generated model: Loom
hosts practitioner declarations where it must, and originates
none of them. Loom therefore never validates test names (dbt
resolves them from installed packages at build) and never
examines kwargs or config for meaning.

One merge rule applies where carriage meets the generator's
own emitted tests: a carried declaration that duplicates an
emitted test - same test name, same column, same kwargs -
is emitted once, not twice, because dbt names a generic test
from exactly those three and a second entry with the same
name fails `dbt parse` outright. A config the carried
declaration carries is merged onto the single emitted entry,
never silently dropped (a declared `severity: warn` is the
practitioner's deliberate softening of the emitted test).
Declarations whose kwargs differ from the emitted test's are
distinct tests to dbt and are carried as declared. `loom
check` warns about the declarations generation will collapse
this way, so the redundancy is still named where the
practitioner can act on it.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping


def carried_generic_tests(
    data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The normalised carriage declarations on an instance
    record: `{test, column_name?, kwargs?, config?}` entries, in
    declaration order. Defensive about shape - validation has
    already run by the time generation reads these."""
    declared = data.get("generic_tests")
    if not isinstance(declared, list):
        return []
    tests: list[dict[str, Any]] = []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        name = entry.get("test")
        if not isinstance(name, str) or not name:
            continue
        normalised: dict[str, Any] = {"test": name}
        column = entry.get("column_name")
        if isinstance(column, str) and column:
            normalised["column_name"] = column
        for key in ("kwargs", "config"):
            value = entry.get(key)
            if isinstance(value, dict) and value:
                normalised[key] = deepcopy(value)
        tests.append(normalised)
    return tests


def render_test_entry(entry: Mapping[str, Any]) -> Any:
    """One declaration as a dbt `tests:` list entry: the bare
    test name when it carries no arguments or config, else the
    mapping form `{name: {**kwargs, config: …}}` - dbt's own
    spelling for a parameterised generic test."""
    name = entry["test"]
    kwargs = entry.get("kwargs") or {}
    config = entry.get("config") or {}
    if not kwargs and not config:
        return name
    body: dict[str, Any] = dict(kwargs)
    if config:
        body["config"] = dict(config)
    return {name: body}


def _split_rendered(
    rendered: Any,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """One rendered `tests:` entry as (name, kwargs, config),
    or None when the entry is not a recognisable test entry.

    The bare-string form carries neither kwargs nor config.
    The mapping form's body is the kwargs, plus a `config`
    mapping when present; a body consisting of a single
    `arguments` mapping is dbt's wrapped spelling of the same
    kwargs and is unwrapped, so the two spellings compare
    equal - dbt resolves them to the same test."""
    if isinstance(rendered, str):
        return rendered, {}, {}
    if isinstance(rendered, dict) and len(rendered) == 1:
        name, body = next(iter(rendered.items()))
        if not isinstance(name, str) or not name:
            return None
        if body is None:
            return name, {}, {}
        if not isinstance(body, dict):
            return None
        body = dict(body)
        config = body.pop("config", None)
        if not isinstance(config, dict):
            config = {}
        kwargs = body
        if set(kwargs) == {"arguments"} and isinstance(
            kwargs["arguments"], dict
        ):
            kwargs = dict(kwargs["arguments"])
        return name, kwargs, config
    return None


def rendered_test_signature(rendered: Any) -> tuple[str, str] | None:
    """A rendered test entry's identity for duplicate
    purposes: (test name, kwargs), the name case-folded and
    the kwargs canonicalised. dbt names a generic test from
    its name, its attachment and its arguments, so two
    entries sharing a signature on the same attachment would
    be given the same name - and `dbt parse` rejects the
    second. Config is not part of the signature: it changes
    how the test runs, not what it is called."""
    split = _split_rendered(rendered)
    if split is None:
        return None
    name, kwargs, _config = split
    return (
        name.casefold(),
        json.dumps(kwargs, sort_keys=True, default=str),
    )


def _merge_config(rendered: Any, carried_config: Mapping[str, Any]) -> Any:
    """A rendered test entry with a carried declaration's
    config merged onto it: the declaration's keys win (the
    declaration is the deliberate act; the emitted entry is
    the generator's default). An entry without config is
    returned unchanged."""
    if not carried_config:
        return rendered
    split = _split_rendered(rendered)
    if split is None:
        return rendered
    name, _kwargs, config = split
    merged = {**config, **carried_config}
    if merged == config:
        return rendered
    if isinstance(rendered, str):
        return {name: {"config": merged}}
    raw_body = next(iter(rendered.values()))
    if not isinstance(raw_body, dict):
        return {name: {"config": merged}}
    body = dict(raw_body)
    body["config"] = merged
    return {name: body}


def _append_or_merge(
    existing: list, entry: Mapping[str, Any]
) -> None:
    """Append one carried declaration's rendered entry to a
    `tests:` list, or - when the list already carries an
    entry with the same signature - merge the declaration's
    config onto that entry and append nothing: the test is
    emitted once."""
    rendered = render_test_entry(entry)
    signature = rendered_test_signature(rendered)
    if signature is not None:
        for index, candidate in enumerate(existing):
            if rendered_test_signature(candidate) == signature:
                existing[index] = _merge_config(
                    candidate, entry.get("config") or {}
                )
                return
    existing.append(rendered)


def apply_carried_tests(
    model_entry: Mapping[str, Any], tests: list[dict[str, Any]]
) -> dict[str, Any]:
    """A copy of one model entry with the carried tests merged
    into its `tests:` lists: model-level declarations (no
    `column_name`) onto the model's own list; column
    declarations onto the matching column's list (matched
    case-insensitively), appending a `{name, tests}` column
    entry when the entry does not list the column - a carried
    test is never dropped for want of a properties column.
    A declaration duplicating a test already in the list
    (same name, same kwargs) is not appended again; its
    config, if any, is merged onto the entry that stands."""
    combined: dict[str, Any] = deepcopy(dict(model_entry))
    columns: list[dict[str, Any]] = [
        column
        for column in combined.get("columns") or []
        if isinstance(column, dict)
    ]
    by_key: dict[str, dict[str, Any]] = {}
    for column in columns:
        name = column.get("name")
        if isinstance(name, str):
            by_key.setdefault(name.casefold(), column)
    for entry in tests:
        column_name = entry.get("column_name")
        if column_name is None:
            _append_or_merge(combined.setdefault("tests", []), entry)
            continue
        column = by_key.get(column_name.casefold())
        if column is None:
            column = {"name": column_name, "tests": []}
            columns.append(column)
            by_key[column_name.casefold()] = column
        _append_or_merge(column.setdefault("tests", []), entry)
    if columns:
        combined["columns"] = columns
    return combined


def apply_carried_to_document(
    document: Mapping[str, Any],
    model_name: str,
    tests: list[dict[str, Any]],
) -> dict[str, Any]:
    """A copy of a schema document whose entry for `model_name`
    carries the tests - a fresh bare entry is appended when the
    document does not describe the model yet."""
    combined: dict[str, Any] = deepcopy(dict(document))
    models: list[dict[str, Any]] = []
    found = False
    for model in combined.get("models") or []:
        if isinstance(model, dict) and model.get("name") == model_name:
            models.append(apply_carried_tests(model, tests))
            found = True
        else:
            models.append(model)
    if not found:
        models.append(
            apply_carried_tests({"name": model_name}, tests)
        )
    combined["models"] = models
    if "version" not in combined:
        combined = {"version": 2, **combined}
    return combined


# --- create-new snippets ------------------------------------

#: The generic tests dbt ships that take no arguments beyond
#: their attachment. Everything else - dbt's other built-ins
#: (`accepted_values` needs `values`, `relationships` needs
#: `to`/`field`) and every package test, whose signature Loom
#: does not presume to know - is scaffolded with the kwargs
#: sentinel: Loom never invents a test's arguments.
NO_ARGUMENT_TESTS = frozenset({"unique", "not_null"})

_SENTINEL_GUIDANCE = "replace with the test's arguments"


def _test_needs_arguments(test: str) -> bool:
    return test.rsplit(".", 1)[-1].casefold() not in NO_ARGUMENT_TESTS


def metadata_data_test_snippet(
    test: str, column: str | None, *, column_unverified: bool = False
) -> str:
    """A `generic_tests:` metadata snippet for one declaration
    on a generated model: it pastes into the model's
    instance params. A test whose arguments Loom cannot know
    carries the `__TODO__` kwargs sentinel - the declaration is
    rejected by validation until the practitioner replaces it,
    so a pasted-and-forgotten snippet can never validate."""
    lines: list[str] = []
    if column_unverified and column is not None:
        lines.append(
            f"# column '{column}' could not be verified - the "
            "model's output columns could not be derived; check "
            "the column name before relying on the declaration"
        )
    lines.append("generic_tests:")
    lines.append(f"  - test: {test}")
    if column is not None:
        lines.append(f"    column_name: {column}")
    if _test_needs_arguments(test):
        lines.append(
            f'    kwargs: {{__TODO__: "{_SENTINEL_GUIDANCE}"}}'
        )
    return "\n".join(lines) + "\n"


def dbt_yaml_data_test_snippet(
    model: str, test: str, column: str | None, *, column_unverified: bool = False
) -> str:
    """A dbt-YAML `models:` fragment declaring one generic test
    on a hand-written model - the form dbt itself reads
    in the model's properties YAML, the counterpart of the
    metadata snippet. Nothing here is Loom metadata."""
    lines = ["models:", f"  - name: {model}"]
    if column_unverified and column is not None:
        lines.append(
            f"    # column '{column}' could not be verified - "
            "the manifest documents no columns for this model; "
            "check the column name before relying on the "
            "declaration"
        )
    if column is not None:
        lines.append("    columns:")
        lines.append(f"      - name: {column}")
        lines.append("        tests:")
        indent = "          "
    else:
        lines.append("    tests:")
        indent = "      "
    if _test_needs_arguments(test):
        lines.append(f"{indent}- {test}:")
        lines.append(f'{indent}    __TODO__: "{_SENTINEL_GUIDANCE}"')
    else:
        lines.append(f"{indent}- {test}")
    return "\n".join(lines) + "\n"
