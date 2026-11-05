"""Human-readable CLI reporting with JSON preserved on request.

Text is the default for the report commands; --format json emits the
full structured payload unchanged. These tests pin the text shapes,
the JSON equivalence, and behaviour on a large (250-model) project
where controlled models must collapse to a summary line.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.cli import main
from loom.reporting import (
    render_compare_text,
    render_inspect_text,
    render_search_engine_text,
    set_terminal_width,
    terminal_width,
)


def _write_registration(project: Path, name: str, classification: str = "mart") -> None:
    directory = project / "local" / "registrations"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yml").write_text(
        f"name: {name}\nclassification: {classification}\n"
        "description: A hand-written model.\nowner: analytics\n",
        encoding="utf-8",
    )


def _write_manifest(project: Path, names: list[str]) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    models_dir = project / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    nodes = {}
    for name in names:
        model_file = models_dir / f"{name}.sql"
        model_file.write_text("select 1\n", encoding="utf-8")
        nodes[f"model.sample.{name}"] = {
            "name": name,
            "resource_type": "model",
            "package_name": "sample",
            "original_file_path": f"models/{name}.sql",
            "depends_on": {"nodes": []},
            "raw_code": "select 1\n",
            "config": {},
            "columns": {},
        }
    # dbt_project.yml must predate the manifest or the freshness
    # check (correctly) reports the manifest as stale.
    (project / "dbt_project.yml").write_text(
        "name: sample\nprofile: sample\nmodel-paths: ['models']\n",
        encoding="utf-8",
    )
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "sample",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                "nodes": nodes,
                "sources": {},
                "exposures": {},
            }
        ),
        encoding="utf-8",
    )


def test_search_no_query_is_the_overview_in_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_registration(tmp_path, "customer_report")
    assert main(["search", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    # No query is the project overview (plan 8.1), not a dump
    # of every record: counts and coverage, honestly labelled
    # as metadata-only here.
    assert "overview (metadata only)" in text
    assert "Models: 1 (registered 1)" in text
    assert "Owners: analytics 1 · unowned 0" in text
    assert not text.lstrip().startswith("{")

    assert (
        main(["search", "--project-dir", str(tmp_path), "--format", "json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["corpus"]["mode"] == "metadata-only"
    assert payload["overview"]["models"] == {
        "total": 1,
        "by_status": {"registered": 1},
    }
    assert payload["overview"]["owners"] == {
        "top": [["analytics", 1]],
        "unowned": 0,
    }


def test_check_coverage_text_headline_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_registration(tmp_path, "customer_report")
    _write_manifest(tmp_path, ["customer_report", "unregistered_model"])
    assert main(["coverage", "--project-dir", str(tmp_path)]) == 1
    text = capsys.readouterr().out
    assert text.startswith("Inventory coverage: PROBLEMS FOUND")
    assert "unregistered_model" in text

    assert (
        main(
            [
                "coverage",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["missing_models"] == ["unregistered_model"]


def test_migration_plan_text_groups_dependents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_manifest(tmp_path, ["old_model", "new_model"])
    assert (
        main(
            [
                "plan",
                "--project-dir",
                str(tmp_path),
                "--from",
                "old_model",
                "--to",
                "new_model",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    # State codes become plain glosses in the plan title;
    # the payload's phases render verbatim under "Phases:".
    assert text.startswith(
        "Plan: replace old_model (unknown to Loom) with "
        "new_model (unknown to Loom)"
    )
    assert "Phases:" in text
    assert "unique_id" not in text


def test_register_text_summarises_and_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_registration(tmp_path, "known_model")
    _write_manifest(tmp_path, ["known_model", "mystery_model"])
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("Register: sample")
    assert "Unknown to Loom - action needed (1):" in text
    assert "mystery_model" in text
    assert "1 known, not controlled" in text
    # The headline leads with the readiness distribution
    # Neither model here is draftable (no fit, no
    # layer), registered or not.
    assert "Readiness: 0 ready to promote" in text
    assert "2 not draftable" in text


def test_large_project_text_stays_grouped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = [f"model_{index:03d}" for index in range(250)]
    _write_manifest(tmp_path, names)
    assert main(["register", "--project-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "State: 250 project models" in text
    assert "250 unknown to Loom" in text
    assert "Readiness: 0 ready to promote · 0 needs authoring" in text
    assert "250 not draftable" in text
    # The unknown list folds past ten - the full 250-name
    # enumeration no longer appears in default text.
    assert "… and 240 more (full list: --verbose, or --format json)" in text
    assert "model_249" not in text
    # No low-level payload noise in the default text rendering.
    assert "unique_id" not in text
    assert "pattern_definition" not in text

    assert (
        main(
            [
                "register",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["models"]) == 250
    assert payload["summary"]["readiness"] == {
        "ready_to_promote": 0,
        "needs_authoring": 0,
        "not_draftable": 250,
    }


def test_inspect_and_compare_text_renderers() -> None:
    inspect_payload = {
        "project": "sample",
        "sources": [
            {
                "source": "raw",
                "table": "orders",
                "database": "local",
                "schema": "main",
                "identifier": "orders",
                "mapped_columns": [{"column": "id", "models": ["stg_orders"]}],
                "columns": [
                    {
                        "name": "id",
                        "type": "INTEGER",
                        "ordinal": 1,
                        "mapped_by": ["stg_orders"],
                    }
                ],
            }
        ],
    }
    text = render_inspect_text(inspect_payload)
    assert "raw.orders" in text and "1 columns, 1 mapped" in text
    verbose = render_inspect_text(inspect_payload, verbose=True)
    assert "1. id INTEGER <- stg_orders" in verbose

    compare_payload = {
        "project": "sample",
        "changes": [
            {
                "source": "raw",
                "table": "orders",
                "change": "column_added",
                "column": "total",
                "mapped_by": [],
            }
        ],
    }
    text = render_compare_text(compare_payload)
    assert "1 change(s)" in text and "column_added total" in text
    assert render_compare_text({"project": "sample", "changes": []}).endswith(
        "no changes"
    )


def _search_payload(results: list[dict], query: str) -> dict:
    return {
        "command": "search",
        "query": query,
        "corpus": {"mode": "full"},
        "banded": False,
        "facets": {},
        "results": results,
        "close_matches": [],
    }


def test_search_promotion_needs_exactly_one_model_with_the_name() -> None:
    # M8's trigger is exact-one: a payload carrying two
    # model records with the query's name (not producible
    # from one corpus, whose models are unique by name, but
    # the renderer's guard is what the behaviour promises)
    # renders the ordinary list, with no detail promotion.
    model = {
        "name": "dup_model",
        "kind": "model",
        "status": "unmanaged",
        "matched": ["dup_model"],
    }
    test = {
        "name": "unique_dup_model",
        "kind": "test",
        "matched": ["dup_model"],
    }
    out = render_search_engine_text(
        _search_payload([dict(model), dict(model), test], "dup_model")
    )
    assert "Models:" in out
    assert "Other matches" not in out

    # A qualified query never promotes, however many
    # records it returns: the ordinary grouped list stands.
    out = render_search_engine_text(
        _search_payload([dict(model), test], "kind:model,dup_model")
    )
    assert "Models:" in out
    assert "Other matches" not in out


def test_search_promoted_kind_table_folds_past_the_fold_limit() -> None:
    # The kind:-promoted table folds at ten like the grouped
    # tier-2 sections (plan 8.2's same capping rule): the
    # first ten rows, the folded count, and the
    # comma-intersected narrowing query. This supersedes the
    # F-12 behaviour (print in full, end with a
    # --names-only/--count-only hint).
    def macros(count: int) -> list[dict]:
        return [
            {
                "name": f"macro_{index:02d}",
                "kind": "macro",
                "provenance": "project",
                "package": "sample",
                "arguments": [],
                "used_by": [],
                "matched": ["kind:macro"],
            }
            for index in range(count)
        ]

    # At the fold limit nothing folds and nothing is said.
    out = render_search_engine_text(
        _search_payload(macros(10), "kind:macro")
    )
    assert "macro_09" in out
    assert "more - narrow with" not in out
    # One row past it: ten rows print, the eleventh is folded
    # behind the count and the narrowing query. Every item's
    # provenance is "project" despite the "sample" package
    # field, so the narrowing query is the project form.
    out = render_search_engine_text(
        _search_payload(macros(11), "kind:macro")
    )
    assert "macro_09" in out
    assert "macro_10" not in out
    assert out.rstrip().endswith(
        '  … and 1 more - narrow with: '
        'loom search "kind:macro,is:project"'
    )
    # The folded rows are hidden, never dropped from the
    # payload: --verbose unfolds, as in the grouped sections.
    out = render_search_engine_text(
        _search_payload(macros(11), "kind:macro"), verbose=True
    )
    assert "macro_10" in out
    assert "more - narrow with" not in out


def test_search_promoted_kind_table_narrows_to_its_largest_group() -> None:
    # A promoted table narrows to its largest provenance
    # group - here the dbt_utils package macros outnumber
    # the project ones.
    items = [
        {
            "name": f"pkg_macro_{index:02d}",
            "kind": "macro",
            "provenance": "package",
            "package": "dbt_utils",
            "arguments": [],
            "used_by": [],
            "matched": ["kind:macro"],
        }
        for index in range(8)
    ] + [
        {
            "name": f"own_macro_{index:02d}",
            "kind": "macro",
            "provenance": "project",
            "package": "sample",
            "arguments": [],
            "used_by": [],
            "matched": ["kind:macro"],
        }
        for index in range(5)
    ]
    out = render_search_engine_text(
        _search_payload(items, "kind:macro")
    )
    assert out.rstrip().endswith(
        '  … and 3 more - narrow with: '
        'loom search "kind:macro,package:dbt_utils"'
    )


# --- Bounded terminal width (assessment finding C-2) --------------
#
# Adaptation is a terminal-only rendering: the CLI resolves
# the width once per run into the module switch, and a pipe
# (or any direct renderer call, as in this suite) gets None
# and the unbounded rendering. These tests pin both halves -
# piped output byte-identical to the pre-adaptation shapes,
# and the bounded shapes at 80 columns: no line over the
# width, no token split, every column still present.


@pytest.fixture
def adapt_width():
    """Set the width switch for one test and always clear
    it afterwards - the switch is process-wide, and a leaked
    width would rewrite every later rendering in the run."""
    yield set_terminal_width
    set_terminal_width(None)


def _small_models_payload() -> dict:
    return _search_payload(
        [
            {
                "name": "dim_customer",
                "kind": "model",
                "status": "generated",
                "generator": "dimension",
                "owner": "analytics",
            },
            {
                "name": "fct_orders",
                "kind": "model",
                "status": "unmanaged",
                "generator": "fact",
                "owner": None,
            },
        ],
        "customer",
    )


def _small_macros_payload() -> dict:
    return _search_payload(
        [
            {
                "name": "cents",
                "kind": "macro",
                "provenance": "project",
                "package": "sample",
                "arguments": [{"name": "amount"}],
                "used_by": ["x"],
            },
            {
                "name": "trim",
                "kind": "macro",
                "provenance": "package",
                "package": "dbt_utils",
                "arguments": [],
                "used_by": [],
            },
        ],
        "kind:macro",
    )


def _small_overview_payload() -> dict:
    return {
        "command": "search",
        "query": "",
        "corpus": {"mode": "full"},
        "results": [],
        "overview": {
            "project": "sample",
            # 2 models + 2 macros + 4 tests, as the census
            # below breaks down.
            "total_objects": 8,
            "models": {
                "total": 2,
                "by_status": {"generated": 1, "registered": 1},
            },
            "by_generator": {"dimension": 1, "fact": 1},
            "by_paradigm": {"kimball": 2},
            "owners": {"top": [["analytics", 1]], "unowned": 1},
            "documentation": {
                "models_documented": 1,
                "models_total": 2,
                "columns_documented": 3,
                "columns_total": 5,
            },
            "macros": {
                "total": 2,
                "project": 1,
                "package": 1,
                "by_package": {"sample": 1, "dbt_utils": 1},
            },
            "objects_by_kind": {"test": 4},
            "suggested_queries": [
                {"query": "is:unmanaged", "count": 1, "label": "unmanaged models"}
            ],
        },
    }


def test_search_width_piped_output_is_byte_identical() -> None:
    # The hard requirement: with no width resolved (a pipe),
    # the rendering is the pre-adaptation output, byte for
    # byte - overview, models table and macros table alike.
    assert render_search_engine_text(_small_models_payload()) == (
        "Search: 2 matches (metadata + manifest)\n"
        "Query: customer\n"
        "Models:\n"
        "  dim_customer  generated  dimension  analytics\n"
        "  fct_orders    unmanaged  fact       -"
    )
    assert render_search_engine_text(_small_macros_payload()) == (
        "Search: 2 matches (metadata + manifest)\n"
        "Query: kind:macro\n"
        "Macros:\n"
        "  cents  project  sample     amount  used by: 1\n"
        "  trim   package  dbt_utils  -       used by: 0"
    )
    assert render_search_engine_text(_small_overview_payload()) == (
        "Search: sample overview (metadata + manifest)\n"
        "Objects: 8 total\n"
        "Models: 2 (generated 1, registered 1)\n"
        "Generators: dimension 1, fact 1\n"
        "Paradigms: kimball 2\n"
        "Owners: analytics 1 · unowned 1\n"
        "Documentation: 1 of 2 models described; "
        "3 of 5 output columns documented\n"
        "Macros: 2 (1 project, 1 package - sample 1, dbt_utils 1)\n"
        "Other objects: tests 4\n"
        "Suggested queries:\n"
        "  loom search is:unmanaged - 1 unmanaged models"
    )


def _wide_models_payload() -> dict:
    payload = _search_payload(
        [
            {
                "name": "fct_order_line_revenue_recognition",
                "kind": "model",
                "status": "unmanaged",
                "generator": "fact",
                "owner": "analytics-platform",
                "matched": ["column:customer_id", "column:order_id"],
            },
            {
                "name": "dim_customer",
                "kind": "model",
                "status": "generated",
                "generator": "dimension",
                "owner": "analytics",
                "matched": ["column:customer_id"],
            },
        ],
        "column:customer_id,column:order_id",
    )
    payload["facets"] = {
        "status": {"unmanaged": 1, "generated": 1},
        "kind": {"model": 2},
        "generator": {"fact": 1, "dimension": 1},
        "owner": {"analytics-platform": 1, "analytics": 1},
        "provenance": {"project": 2},
    }
    return payload


def _wide_macros_payload() -> dict:
    return _search_payload(
        [
            {
                "name": "aggregate_revenue_by_period",
                "kind": "macro",
                "provenance": "project",
                "package": "jaffle_shop",
                "arguments": [
                    {"name": name}
                    for name in (
                        "period_start",
                        "period_end",
                        "grain",
                        "currency",
                        "refunds",
                        "rounding",
                    )
                ],
                "used_by": ["a", "b", "c"],
                "matched": ["kind:macro"],
            },
            {
                "name": "cents_to_dollars",
                "kind": "macro",
                "provenance": "package",
                "package": "dbt_utils",
                "arguments": [],
                "used_by": [],
                "matched": ["kind:macro"],
            },
        ],
        "kind:macro",
    )


def _wide_overview_payload() -> dict:
    payload = _small_overview_payload()
    overview = payload["overview"]
    overview["project"] = "loom_quickstart"
    # 36 models + the small payload's 2 macros and 4 tests.
    overview["total_objects"] = 42
    overview["models"] = {
        "total": 36,
        "by_status": {"generated": 32, "registered": 1, "unmanaged": 3},
    }
    overview["by_generator"] = {
        "satellite": 4,
        "staging": 4,
        "bronze": 3,
        "hub": 3,
        "link": 3,
        "silver": 3,
        "normalized_table": 2,
        "scd2_dimension": 2,
        "bridge": 1,
        "dimension": 1,
        "fact": 1,
        "gold": 1,
        "mart": 1,
        "obt": 1,
        "pit": 1,
        "view": 1,
    }
    overview["suggested_queries"] = [
        {
            "query": "has:undocumented-columns",
            "count": 208,
            "label": "models with undocumented columns",
        }
    ]
    return payload


def test_search_width_generous_width_matches_unbounded(adapt_width) -> None:
    # Adaptation only ever fires past the resolved width:
    # at a width every line already fits, the bounded path
    # hands back the unbounded rendering unchanged.
    for payload in (
        _wide_models_payload(),
        _wide_macros_payload(),
        _wide_overview_payload(),
    ):
        unbounded = render_search_engine_text(payload)
        adapt_width(200)
        assert render_search_engine_text(payload) == unbounded
        set_terminal_width(None)


def test_search_width_models_table_at_80(adapt_width) -> None:
    adapt_width(80)
    out = render_search_engine_text(_wide_models_payload())
    lines = out.splitlines()
    assert all(len(line) <= 80 for line in lines)
    # Every column survives on the row line; the matched
    # annotation moves beneath it, whole.
    row = next(
        line for line in lines if "fct_order_line_revenue_recognition" in line
    )
    assert "unmanaged" in row and "fact" in row
    assert "analytics-platform" in row
    assert "matched:" not in row
    assert (
        "    matched: column:customer_id, column:order_id" in lines
    )
    # The facet line wrapped at group boundaries under the
    # hanging indent: no line over the width, entries whole.
    facet_lines = [
        line for line in lines if "Facets:" in line or "provenance:" in line
    ]
    assert len(facet_lines) > 1
    assert "analytics-platform 1" in out
    assert facet_lines[1].startswith("        ")


def test_search_width_macros_table_at_80(adapt_width) -> None:
    adapt_width(80)
    out = render_search_engine_text(_wide_macros_payload())
    lines = out.splitlines()
    assert all(len(line) <= 80 for line in lines)
    assert "Macros:" in lines
    row = next(
        line for line in lines if "aggregate_revenue_by_period" in line
    )
    # Name, provenance, package, first arguments and the
    # used-by column all on the row line.
    assert "project" in row and "jaffle_shop" in row
    assert "period_start" in row and "used by: 3" in row
    # The argument list wraps at entry boundaries, aligned
    # under the arguments column; every parameter survives
    # whole - nothing is split mid-token.
    for name in ("period_end", "grain", "currency", "refunds", "rounding"):
        assert name in out
    args_column = row.index("period_start")
    continuation = next(line for line in lines if "currency," in line)
    assert continuation.startswith(" " * args_column)
    # The short row is untouched in shape.
    assert any(
        "cents_to_dollars" in line and "used by: 0" in line
        for line in lines
    )


def test_search_width_macro_row_truncated_name_args_used_by_at_80(
    adapt_width,
) -> None:
    # The gate's review shape, pinned: a macro row combining
    # a name long enough to truncate, an argument list and a
    # used-by suffix, in a table whose suffix column is set
    # by another row's two-digit count. The bound is in
    # terminal columns - len() here, because every glyph the
    # renderer emits (the truncation marker and the census
    # separator included) occupies exactly one column, so a
    # string's code-point length is its column count. (A
    # byte count would overstate such rows: the marker is
    # three UTF-8 bytes for its one column.)
    adapt_width(80)
    payload = _search_payload(
        [
            {
                "name": "_ensure_all_pks_are_in_columns_for_every_model",
                "kind": "macro",
                "provenance": "package",
                "package": "audit_helper",
                "arguments": [
                    {"name": "primary_key"},
                    {"name": "relation"},
                    {"name": "exclude_columns"},
                ],
                "used_by": ["a", "b"],
                "matched": ["kind:macro"],
            },
            {
                "name": "cents_to_dollars",
                "kind": "macro",
                "provenance": "package",
                "package": "dbt_utils",
                "arguments": [],
                "used_by": [str(i) for i in range(12)],
                "matched": ["kind:macro"],
            },
        ],
        "kind:macro",
    )
    out = render_search_engine_text(payload)
    lines = out.splitlines()
    assert all(len(line) <= 80 for line in lines)
    # The row itself: truncated name (marker shown), every
    # fixed column present, first argument and the used-by
    # suffix on the row line - at 79 of the 80 columns.
    row = (
        "  _ensure_all_pks_are_in_colu…  package  audit_helper  "
        "primary_key,  used by: 2"
    )
    assert len(row) == 79
    assert row in lines
    # The remaining arguments wrap beneath, aligned under
    # the arguments column; the atom too long for the column
    # is truncated with the marker, never split.
    args_column = row.index("primary_key")
    assert " " * args_column + "relation," in lines
    assert " " * args_column + "exclude_col…" in lines
    # The row that sets the suffix width lands exactly on
    # the bound.
    widest = next(line for line in lines if "used by: 12" in line)
    assert len(widest) == 80


def test_search_width_truncates_an_overlong_token_with_a_marker(
    adapt_width,
) -> None:
    adapt_width(80)
    payload = _search_payload(
        [
            {
                "name": "short_name",
                "kind": "macro",
                "provenance": "project",
                "package": "sample",
                "arguments": [
                    {"name": "a_parameter_name_far_too_long_for_the_column"}
                ],
                "used_by": ["x"],
                "matched": ["kind:macro"],
            },
            {
                "name": "other_macro",
                "kind": "macro",
                "provenance": "package",
                "package": "dbt_utils",
                "arguments": [],
                "used_by": [],
                "matched": ["kind:macro"],
            },
        ],
        "kind:macro",
    )
    out = render_search_engine_text(payload)
    assert all(len(line) <= 80 for line in out.splitlines())
    # The single token cannot wrap, so it is truncated and
    # the cut is marked - never split, never silent.
    assert "a_parameter_name_far_too_long_for_the_column" not in out
    assert "a_parameter_name" in out and "…" in out

    # The same rule on the models table's name column.
    payload = _search_payload(
        [
            {
                "name": "m" * 90,
                "kind": "model",
                "status": "unmanaged",
                "generator": "fact",
                "owner": "analytics",
            },
            {
                "name": "dim_customer",
                "kind": "model",
                "status": "generated",
                "generator": "dimension",
                "owner": "analytics",
            },
        ],
        "customer",
    )
    out = render_search_engine_text(payload)
    assert all(len(line) <= 80 for line in out.splitlines())
    assert "m" * 90 not in out and "…" in out
    # Unbounded, the full name stands.
    set_terminal_width(None)
    assert "m" * 90 in render_search_engine_text(payload)


def test_search_width_kind_table_fact_wraps_at_80(adapt_width) -> None:
    adapt_width(80)
    payload = _search_payload(
        [
            {
                "name": "not_null_fct_orders_order_id",
                "kind": "test",
                "provenance": "project",
                "package": "sample",
                "upstream": [
                    "fct_orders",
                    "fct_order_lines",
                    "fct_payments",
                    "fct_shipments",
                    "fct_returns",
                    "fct_refunds",
                ],
                "matched": ["kind:test"],
            },
            {
                "name": "unique_fct_orders_order_id",
                "kind": "test",
                "provenance": "project",
                "package": "sample",
                "upstream": ["fct_orders"],
                "matched": ["kind:test"],
            },
        ],
        "kind:test",
    )
    out = render_search_engine_text(payload)
    lines = out.splitlines()
    assert all(len(line) <= 80 for line in lines)
    # The graph fact wraps at its entry boundaries; every
    # upstream name survives whole.
    for name in (
        "fct_orders",
        "fct_order_lines",
        "fct_payments",
        "fct_shipments",
        "fct_returns",
        "fct_refunds",
    ):
        assert name in out
    assert "on: fct_orders" in out


def test_search_width_overview_census_at_80(adapt_width) -> None:
    adapt_width(80)
    out = render_search_engine_text(_wide_overview_payload())
    lines = out.splitlines()
    assert all(len(line) <= 80 for line in lines)
    # The census enumeration wraps at entry boundaries under
    # the label's hanging indent; entries stay whole.
    assert "normalized_table 2" in out
    assert "scd2_dimension 2" in out
    generators = [
        line for line in lines if line.startswith(("Generators:", "    "))
    ]
    assert len(generators) > 1
    # Suggested queries are commands, not census: they are
    # never wrapped, even past the width.
    assert (
        "  loom search has:undocumented-columns - 208 "
        "models with undocumented columns" in lines
    )


# --- Wide characters (wcwidth measurement) -------------------------
#
# The width arithmetic above counts terminal columns, and a
# code-point count is not a column count: CJK characters and
# most emoji occupy two columns. These tests measure with
# wcwidth's own wcswidth - an independent count, not the
# renderer's - so a line the renderer believes fits is
# checked against the terminal's arithmetic, not its own.


def _true_columns(text: str) -> int:
    from wcwidth import wcswidth

    return wcswidth(text)


def test_visible_len_measures_terminal_columns() -> None:
    from loom.reporting import _visible_len, paint

    assert _visible_len("") == 0
    assert _visible_len("plain") == 5
    # Wide characters count two columns, not one code point.
    assert _visible_len("模型") == 4
    assert _visible_len("🎉") == 2
    assert _visible_len("a模b") == 4
    # The colour path's escapes take no columns.
    assert _visible_len(paint("模型", "1", enabled=True)) == 4
    # A control character advances no column and does not
    # poison the rest of the line's measurement.
    assert _visible_len("a\x07b") == 2


def test_truncate_fits_columns_and_never_splits_a_character() -> None:
    from loom.reporting import _truncate, _visible_len

    # ASCII semantics unchanged: budget-1 characters + marker.
    assert _truncate("abcdef", 4) == "abc…"
    assert _truncate("abc", 4) == "abc"
    # Two wide characters in a five-column budget: both fit
    # whole, the marker takes the fifth column.
    assert _truncate("模型模型", 5) == "模型…"
    # A wide character that would straddle the final column
    # is dropped whole - the line closes one column short
    # rather than one over, and no character is split.
    truncated = _truncate("模型模型", 4)
    assert truncated == "模…"
    assert _visible_len(truncated) <= 4
    assert _truncate("模型", 1) == "…"
    assert _truncate("模型", 0) == ""


def test_pad_aligns_on_columns_not_code_points() -> None:
    from loom.reporting import _pad, _visible_len

    assert _pad("ab", 5) == "ab   "
    padded = _pad("模型", 6)
    assert _visible_len(padded) == 6
    assert padded == "模型  "


def _wide_name_models_payload() -> dict:
    return _search_payload(
        [
            {
                "name": "顧客モデル",
                "kind": "model",
                "status": "generated",
                "generator": "dimension",
                "owner": "analytics",
            },
            {
                "name": "plain_name",
                "kind": "model",
                "status": "generated",
                "generator": "dimension",
                "owner": "analytics",
            },
            {
                "name": "orders_🎉🎉",
                "kind": "model",
                "status": "unmanaged",
                "generator": "fact",
                "owner": None,
            },
        ],
        "customer",
    )


def test_search_width_wide_names_align_and_hold_the_bound(
    adapt_width,
) -> None:
    payload = _wide_name_models_payload()
    # Unbounded: the status column starts at the same
    # terminal column on every row - padding is by columns,
    # so the wide name takes exactly its own width of field.
    lines = render_search_engine_text(payload).splitlines()
    rows = [line for line in lines if "generated" in line]
    assert len(rows) == 2
    starts = {
        _true_columns(line[: line.index("generated")]) for line in rows
    }
    assert len(starts) == 1
    # Bounded at 40: every line's true width holds the bound.
    adapt_width(40)
    out = render_search_engine_text(payload)
    assert all(
        _true_columns(line) <= 40 for line in out.splitlines()
    )
    assert "顧客モデル" in out or "顧…" in out


def test_search_width_bound_guard_sees_wide_characters(
    adapt_width,
) -> None:
    # A row whose code-point length fits 80 but whose true
    # width does not: the pre-wcwidth guard measured code
    # points, passed the row, and the terminal overflowed.
    payload = _search_payload(
        [
            {
                "name": "モ" * 25,
                "kind": "model",
                "status": "unmanaged",
                "generator": "fact",
                "owner": "analytics-platform",
            },
            {
                "name": "plain",
                "kind": "model",
                "status": "generated",
                "generator": "fact",
                "owner": "analytics-platform",
            },
        ],
        "wide",
    )
    unbounded = render_search_engine_text(payload)
    row = next(
        line for line in unbounded.splitlines() if "unmanaged" in line
    )
    assert len(row) <= 80 < _true_columns(row)
    adapt_width(80)
    out = render_search_engine_text(payload)
    assert all(
        _true_columns(line) <= 80 for line in out.splitlines()
    )
    bounded_row = next(
        line for line in out.splitlines() if "unmanaged" in line
    )
    # Every column survives the squeeze, as in the ASCII
    # bounded table: the owner column shrinks first and
    # closes with the ellipsis marker.
    assert "fact" in bounded_row and "analytic…" in bounded_row


def test_search_width_wide_macro_names_hold_the_bound(
    adapt_width,
) -> None:
    payload = _search_payload(
        [
            {
                "name": "集計_🎉",
                "kind": "macro",
                "provenance": "project",
                "package": "sample",
                "arguments": [{"name": "amount"}],
                "used_by": ["x"],
            },
            {
                "name": "cents",
                "kind": "macro",
                "provenance": "package",
                "package": "dbt_utils",
                "arguments": [],
                "used_by": [],
            },
        ],
        "kind:macro",
    )
    lines = render_search_engine_text(payload).splitlines()
    rows = [line for line in lines if "used by:" in line]
    starts = {
        _true_columns(line[: line.index("used by:")]) for line in rows
    }
    assert len(starts) == 1
    adapt_width(80)
    out = render_search_engine_text(payload)
    assert all(
        _true_columns(line) <= 80 for line in out.splitlines()
    )


def test_search_width_facet_line_with_wide_values_holds_the_bound(
    adapt_width,
) -> None:
    payload = _wide_name_models_payload()
    payload["facets"] = {
        "owner": {
            "データチーム": 2,
            "分析プラットフォーム部門": 1,
            "analytics": 4,
        }
    }
    adapt_width(60)
    out = render_search_engine_text(payload)
    assert all(
        _true_columns(line) <= 60 for line in out.splitlines()
    )
    # Facet entries stay whole - a wide value is never split.
    assert "分析プラットフォーム部門 1" in out


class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_terminal_width_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pipe never adapts.
    assert terminal_width(_FakeStream(False)) is None
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda: os.terminal_size((120, 24))
    )
    assert terminal_width(_FakeStream(True)) == 120
    # Below the adaptation floor the unbounded layout stands.
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda: os.terminal_size((30, 24))
    )
    assert terminal_width(_FakeStream(True)) is None
    # A degenerate report falls back to the conventional 80.
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda: os.terminal_size((0, 24))
    )
    assert terminal_width(_FakeStream(True)) == 80


def test_main_sets_the_terminal_width_once_per_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr("loom.cli.set_terminal_width", seen.append)
    _write_registration(tmp_path, "customer_report")
    assert main(["search", "--project-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    # Captured stdout is not a terminal, so the one decision
    # resolves to None: adaptation off, the piped shape safe.
    assert seen == [None]
