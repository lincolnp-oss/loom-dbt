"""Metadata-declared dbt unit tests (ADR 0029).

Practitioners declare unit tests in instance metadata (`unit_tests`);
`loom generate` emits them centrally - declaration-driven and
generator-agnostic, unlike the per-renderer data tests - to
`models/_generated/tests[/<subpath>]/unit_<model>.yml`. Fixtures are
`dict` or `csv` rows only in v1: partial-column rows are what make
`__loaded_at`-stamped models testable (expect rows omit the
run-time stamp). Validation of the declarations lives in
`loom.inventory`; this module owns the upstream derivation, the
emitter, the scaffold snippet, and the warning pass.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from loom.output_columns import resolve_output_columns
from loom.properties import render_schema_document

# Generators on which `unit_tests` is a validation error in v1, with
# the reason the error names (proof-stack design §1.5). Only
# scd2_dimension remains: dbt unit tests target models, and the
# model under test there is a snapshot. Staging and bronze were
# excluded here until the 2026-11-04 extension ruling - the stated
# reasons described the emitter (given inputs were rendered only
# as ref() of inventory models), not a dbt limit; the jaffle
# evidence (a practitioner unit test mocking the staging model's
# own source, macro transform included, passing dbt build)
# displaced them, and satellite's absence from the contracts was
# a wiring gap closed by the same ruling (ADR 0029, amended).
UNIT_TEST_EXCLUSIONS: dict[str, str] = {
    "scd2_dimension": (
        "the model under test is a dbt snapshot, and a unit fixture "
        "must not imply it proves the snapshot change/delete "
        "lifecycle (ADR 0029)"
    ),
}

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def declared_upstreams(data: Mapping[str, Any]) -> list[str]:
    """The model's declared upstream model names, in declaration order.

    Every model the generated SQL can `ref()`: its input models,
    joined models (obt/gold/view), and a link's endpoint hubs.
    """
    upstreams: list[str] = []

    def add(name: Any) -> None:
        if isinstance(name, str) and name not in upstreams:
            upstreams.append(name)

    input_models = data.get("input_models")
    if isinstance(input_models, list):
        for name in input_models:
            add(name)
    joins = data.get("joins")
    if isinstance(joins, list):
        for join in joins:
            if isinstance(join, dict):
                add(join.get("model"))
    endpoints = data.get("endpoints")
    if isinstance(endpoints, list):
        for item in endpoints:
            if isinstance(item, dict):
                add(item.get("hub"))
    return upstreams


# Generators whose models read a dbt source rather than an
# inventory model: their declared tests mock the model's own
# source, rendered in dbt's source form.
SOURCE_FED_GENERATORS = ("staging", "bronze")


def declared_source(data: Mapping[str, Any]) -> tuple[str, str] | None:
    """The (source name, table) a source-fed model declares, or None.

    Resolved from `source_ref` (staging) or `inline_source`
    (staging or bronze) on the expanded record.
    """
    for key in ("source_ref", "inline_source"):
        ref = data.get(key)
        if isinstance(ref, dict):
            name, table = ref.get("name"), ref.get("table")
            if isinstance(name, str) and isinstance(table, str):
                return name, table
    return None


def render_unit_tests(data: Mapping[str, Any]) -> str | None:
    """The unit-test YAML document for one model, or None.

    None when the model declares no unit tests. The document shape is
    dbt's: a top-level `unit_tests:` list whose entries name the
    model and carry given/expect fixtures; `given` inputs render as
    `ref('<name>')` - or, for a source-fed generator (staging,
    bronze), as `source('<name>', '<table>')` resolved from the
    model's own declared source. Serialisation goes through the
    same schema document renderer as the data-test/properties
    YAML.
    """
    tests = data.get("unit_tests")
    if not isinstance(tests, list) or not tests:
        return None
    name = data.get("name")
    if not isinstance(name, str):
        return None
    source = (
        declared_source(data)
        if data.get("generator") in SOURCE_FED_GENERATORS
        else None
    )
    entries: list[dict[str, Any]] = []
    for test in tests:
        if not isinstance(test, dict):
            continue
        entry: dict[str, Any] = {"name": test.get("name")}
        if isinstance(test.get("description"), str):
            entry["description"] = test["description"]
        entry["model"] = name
        given_entries = []
        given = test.get("given")
        if isinstance(given, list):
            for given_item in given:
                if not isinstance(given_item, dict):
                    continue
                rendered: dict[str, Any] = {
                    "input": (
                        f"source('{source[0]}', '{source[1]}')"
                        if source is not None
                        else f"ref('{given_item.get('input')}')"
                    )
                }
                if given_item.get("format") == "csv":
                    rendered["format"] = "csv"
                rows = given_item.get("rows")
                rendered["rows"] = rows.strip() if isinstance(rows, str) else rows
                given_entries.append(rendered)
        entry["given"] = given_entries
        expect = test.get("expect")
        expect_entry: dict[str, Any] = {}
        if isinstance(expect, dict):
            if expect.get("format") == "csv":
                expect_entry["format"] = "csv"
            rows = expect.get("rows")
            expect_entry["rows"] = rows.strip() if isinstance(rows, str) else rows
        entry["expect"] = expect_entry
        overrides = test.get("overrides")
        if isinstance(overrides, dict):
            entry["overrides"] = overrides
        entries.append(entry)
    if not entries:
        return None
    return render_schema_document({"version": 2, "unit_tests": entries})


def fixture_columns(block: Mapping[str, Any]) -> list[str]:
    """Column names a given/expect block names, in first-seen order."""
    columns: list[str] = []
    rows = block.get("rows")
    if block.get("format") == "csv":
        if isinstance(rows, str) and rows.strip():
            header = rows.splitlines()[0]
            for cell in header.split(","):
                cell = cell.strip().strip('"')
                if cell and cell not in columns:
                    columns.append(cell)
        return columns
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                for key in row:
                    if isinstance(key, str) and key not in columns:
                        columns.append(key)
    return columns


def unit_test_warnings(entries: list) -> list[str]:
    """Non-fatal unit-test warnings for validate/generate output.

    Two warn-not-fail rules from the design: a test name that does
    not start with its model name (convention is
    `<model>__<scenario>`), and an expect block that pins
    `__loaded_at` (a run-time stamp - the test will be brittle).
    """
    warnings: list[str] = []
    for entry in entries:
        if not entry.is_generated:
            continue
        tests = entry.data.get("unit_tests")
        if not isinstance(tests, list):
            continue
        model = entry.data.get("name")
        for test in tests:
            if not isinstance(test, dict):
                continue
            test_name = test.get("name")
            if not isinstance(test_name, str):
                continue
            if isinstance(model, str) and not test_name.startswith(model):
                warnings.append(
                    f"{entry.path}: unit test {test_name!r} does not start "
                    f"with the model name {model!r}; by convention test "
                    "names read '<model>__<scenario>'"
                )
            expect = test.get("expect")
            if isinstance(expect, dict) and any(
                col.casefold() == "__loaded_at"
                for col in fixture_columns(expect)
            ):
                warnings.append(
                    f"{entry.path}: unit test {test_name!r}: expect pins "
                    "'__loaded_at', a run-time timestamp; the test "
                    "will be brittle"
                )
    return warnings


def _flow_row(columns: list[str]) -> str:
    # One key per column, first-seen order: a column named twice
    # would emit a duplicate YAML key and the row would not parse
    # as the mapping it reads as. Duplicates arise honestly - one
    # source column feeding both a plain mapping and a macro
    # mapping is two declarations of the same given column.
    parts = []
    seen: set[str] = set()
    for column in columns:
        if column in seen:
            continue
        seen.add(column)
        key = column if _IDENT.match(column) else json.dumps(column)
        parts.append(f'{key}: "__TODO__"')
    return "- {" + ", ".join(parts) + "}"


def scaffold_snippet(
    data: Mapping[str, Any],
    by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """A `unit_tests:` metadata snippet for one model (design §1.7).

    One stub test; a given entry per declared upstream with a single
    sentinel row keyed by the upstream's derivable output columns;
    an expect row keyed by the model's derivable deterministic
    columns (the run-time stamp excluded). Where columns cannot be
    derived, the snippet degrades honestly: empty rows plus a
    comment, never invented columns. The validator rejects any row
    still containing the `__TODO__` sentinel.
    """
    name = data.get("name")
    lines = ["unit_tests:", f"  - name: {name}__todo_scenario", "    given:"]
    upstreams = declared_upstreams(data)
    given_written = False
    if not upstreams and data.get("generator") in SOURCE_FED_GENERATORS:
        source = declared_source(data)
        if source is not None:
            # A source-fed model mocks its own source: the given
            # input names the source table. Staging's mapped `from`
            # columns are derivable; bronze's source columns are
            # not in the inventory, so its rows degrade honestly.
            lines.append(f"      - input: {source[1]}")
            columns = None
            if data.get("generator") == "staging":
                mapping = data.get("columns")
                if isinstance(mapping, list):
                    # First-seen order, deduped: one source column
                    # may feed both a plain mapping and a macro
                    # mapping, and the given row keys it once.
                    columns = list(
                        dict.fromkeys(
                            item["source"]
                            for item in mapping
                            if isinstance(item, dict)
                            and isinstance(item.get("source"), str)
                        )
                    ) or None
            if columns:
                lines.append("        rows:")
                lines.append(f"          {_flow_row(columns)}")
            else:
                lines.append(
                    f"        # columns for source {source[1]!r} could not "
                    "be derived; write the row keys by hand"
                )
                lines.append("        rows: []")
            # The given block for this input is now written; the
            # generic upstream loop below must not emit a second
            # block for the same input.
            given_written = True
    if not upstreams and not given_written:
        lines.append("      # this model declares no upstreams; add given entries by hand")
        lines.append("      []")
    if not given_written:
        for upstream in upstreams:
            lines.append(f"      - input: {upstream}")
            columns = None
            if by_name is not None:
                target = by_name.get(upstream.casefold())
                if target is not None:
                    resolved = resolve_output_columns(target, by_name)
                    if resolved is not None:
                        columns = [column.name for column in resolved]
            if columns:
                lines.append("        rows:")
                lines.append(f"          {_flow_row(columns)}")
            else:
                lines.append(
                    f"        # columns for {upstream!r} could not be derived; "
                    "write the row keys by hand"
                )
                lines.append("        rows: []")
    lines.append("    expect:")
    resolved = resolve_output_columns(data, by_name)
    if resolved is None:
        lines.append("      # output columns could not be derived; write the row keys by hand")
        lines.append("      rows: []")
    else:
        stamped = any(c.name.casefold() == "__loaded_at" for c in resolved)
        deterministic = [
            c.name for c in resolved if c.name.casefold() != "__loaded_at"
        ]
        if stamped:
            lines.append("      # __loaded_at omitted - run-time stamp")
        lines.append("      rows:")
        lines.append(f"        {_flow_row(deterministic)}")
    return "\n".join(lines) + "\n"


def dbt_yaml_snippet(
    name: str,
    given: list[tuple[str, list[str] | None]],
    expect_columns: list[str] | None,
) -> str:
    """A dbt-YAML `unit_tests:` document for a hand-written model.

    The form dbt itself reads in a model's properties YAML - the
    counterpart of `scaffold_snippet`'s metadata form, for models
    Loom does not generate. `given` carries one (input expression,
    documented columns) pair per upstream in manifest order: the
    expression is the `ref('…')` / `source('…', '…')` call dbt
    expects, and the columns are the input's documented columns,
    or None where the manifest documents none. Rows are sentinel
    rows keyed by those columns; where columns cannot be derived
    the snippet degrades honestly - empty rows plus a comment,
    never invented columns - the same discipline as the metadata
    snippet. Nothing here is Loom metadata: the practitioner
    pastes the document into their own model YAML.
    """
    lines = [
        "unit_tests:",
        f"  - name: {name}__todo_scenario",
        f"    model: {name}",
        "    given:",
    ]
    if not given:
        lines.append(
            "      # this model depends on no upstream models or "
            "sources; add given entries by hand"
        )
        lines.append("      []")
    for expression, columns in given:
        lines.append(f"      - input: {expression}")
        if columns:
            lines.append("        rows:")
            lines.append(f"          {_flow_row(columns)}")
        else:
            lines.append(
                f"        # columns for {expression} are not "
                "documented in the manifest; write the row keys "
                "by hand"
            )
            lines.append("        rows: []")
    lines.append("    expect:")
    if expect_columns:
        lines.append("      rows:")
        lines.append(f"        {_flow_row(expect_columns)}")
    else:
        lines.append(
            "      # output columns are not documented in the "
            "manifest; write the row keys by hand"
        )
        lines.append("      rows: []")
    return "\n".join(lines) + "\n"
