import argparse
import json
import os
import shutil
import sys
import textwrap
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from loom import __version__
from loom.dialects import detect_dialect, find_profiles_file
from loom.errors import LoomError
from loom.generator_registry import GENERATOR_REGISTRY
from loom.inventory import (
    InventoryEntry,
    _load_project,
    authored_source_keys,
    build_inventory,
    check_inventory_coverage,
    corpus_from_entries,
    load_project_documents,
    validate_project,
)
from loom.selection import (
    diagnose_empty_branch,
    evaluate_selectors,
    load_named_selectors,
    parse_branch,
    records_from_corpus,
    records_from_manifest_document,
)
from loom.similarity import similar_to
from loom.check import (
    check_project,
    declared_test_problems,
    duplicate_test_warnings,
)
from loom.renderers import RENDERERS, get_renderer
from loom.renderers.base import BaseRenderer, IncrementalSpec

from loom.migration_assessment import (
    _draft_sources,
    _project_patterns,
    _synthesised_pattern_name,
    migration_assessment,
    write_draft_registrations,
    write_drafts,
)
from loom.migration_plan import migration_plan
from loom.output_columns import resolve_output_columns
from loom.output_layout import (
    expected_model_path,
    resolve_contract_enforced,
    resolve_output_subpath,
    resolve_skip_properties_document,
)
from loom.properties import (
    contract_warnings,
    merge_properties,
    properties_optout_warnings,
    model_properties_entry,

    render_schema_document,
)
from loom.declared_unit_tests import (
    UNIT_TEST_EXCLUSIONS,
    dbt_yaml_snippet,
    render_unit_tests,
    scaffold_snippet,
    unit_test_warnings,
)
from loom.declared_generic_tests import (
    apply_carried_to_document,
    carried_generic_tests,
    dbt_yaml_data_test_snippet,
    metadata_data_test_snippet,
)
from loom.inventory_relationships import staging_macro_warnings
from loom.manifest import read_manifest, validate_model_freshness
from loom.equivalence import (
    CANDIDATE_MANIFEST_NAME,
    EQUIVALENCE_DIR,
    static_stage_report,
)
from loom.reporting import (
    BOLD,
    bold,
    colour_enabled,
    mode_phrase,
    paint,
    red,
    render_check_gate_text,
    render_check_shadowed_text,
    render_compare_text,
    render_convert_text,
    render_coverage_shadowed_text,
    render_coverage_text,
    render_inspect_text,
    render_migration_plan_text,
    render_prove_text,
    render_register_text,
    render_search_engine_text,
    render_shadowed_section,
    set_colour_enabled,
    set_terminal_width,
    terminal_width,
    _FOLD_LIMIT,
    _folded_name_lines,
)
from loom.project_files import (
    _configured_paths,
    _output_root,
    _project_config,
)
from loom.source_structure import (
    capture_source_structure,
    compare_source_structure,
    inspect_sources,
)


def _add_project_dir_arg(parser: argparse.ArgumentParser) -> None:
    """Every command carries the same --project-dir, defined once
    here so its help states the default identically on all 13
    command pages (QOL F-10: the default - the current directory -
    used to be stated nowhere in help)."""
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="the dbt project directory (default: the current directory)",
    )


def _add_format_args(parser: argparse.ArgumentParser) -> None:
    """Every command accepts the same output controls: report
    commands render human-readable text by default; --format json
    emits the full structured payload for automation."""
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format: text (default, human-readable) or json "
        "(full structured payload for automation)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include low-level detail in text output (unique IDs, "
        "paths, exhaustive lists)",
    )


# --- The write-mode convention ----------------------------------
#
# Mutating commands (generate, register, convert,
# snapshot-sources) preview by default and write only in write
# mode: the --write flag, or the LOOM_WRITE environment variable
# (set and not 0/false/no, case-insensitive) when no flag is
# given. The flag is idempotent over the variable - there is no
# negative flag in the Alpha; unsetting the variable is how an
# environment opts back out. Every mutating run states its mode
# and the mode's source in its banner (loom.reporting.mode_phrase
# owns the wording).

_WRITE_ENV_VAR = "LOOM_WRITE"
_WRITE_ENV_OFF = frozenset({"0", "false", "no"})


def _write_mode(args: argparse.Namespace) -> tuple[bool, str]:
    """Resolve a mutating command's mode: (write, source), where
    source is "flag", "env" or "default"."""
    if getattr(args, "write", False):
        return True, "flag"
    raw = os.environ.get(_WRITE_ENV_VAR)
    if raw is not None and raw.strip().lower() not in _WRITE_ENV_OFF:
        return True, "env"
    return False, "default"


def _add_write_args(parser: argparse.ArgumentParser, what: str) -> None:
    """The one write flag every mutating command carries."""
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"write {what}; without it the command previews what "
        "would change and writes nothing (dry run). Also enabled "
        f"by the {_WRITE_ENV_VAR} environment variable",
    )


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    """The shared --select / --exclude pair every model-scoped
    command carries (plan 7.6): selectors in the one grammar
    (comma-separated criteria in one selector intersect;
    repeated flags union; --exclude subtracts), evaluated
    strictly - a selector matching nothing in the command's
    pool is an error naming it."""
    parser.add_argument(
        "--select",
        action="append",
        metavar="SELECTOR",
        help=(
            "only act on models matching the selector (the "
            "selector grammar: names, qualifiers such as "
            "generator:hub or key:customer_id, graph forms such "
            "as +name and name+2; commas intersect within one "
            "selector, repeated --select flags union); a "
            "selector that matches nothing here is an error"
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="SELECTOR",
        help="subtract models matching the selector from the acted-on set",
    )


def _named_selectors_for(
    project_dir: Path, texts: list[str]
) -> dict:
    """Load dbt-style named selectors from the project's
    selectors.yml, but only when a selector: criterion is
    actually in play."""
    if any("selector:" in text for text in texts):
        return load_named_selectors(project_dir)
    return {}


def _selection_for(
    args: argparse.Namespace,
    records: list,
    *,
    pool: set[str],
    pool_label: str = "models",
):
    """Evaluate a command's --select / --exclude strictly over
    its records, narrowed to its pool."""
    texts = list(getattr(args, "select", None) or []) + list(
        getattr(args, "exclude", None) or []
    )
    return evaluate_selectors(
        records,
        getattr(args, "select", None) or [],
        getattr(args, "exclude", None) or [],
        mode="select",
        pool=pool,
        named=_named_selectors_for(args.project_dir, texts),
        pool_label=pool_label,
    )


def _apply_hint(argv: list[str]) -> str:
    """The exact command to re-run to apply a preview: the
    invocation as given, with --write inserted after the command
    name. Arguments are joined bare, double-quoted only when one
    contains whitespace - shlex.join is platform-dependent here
    (it single-quotes backslash paths on Windows, which neither
    cmd nor a POSIX shell reads back as the same command), and
    the hint must read identically everywhere."""
    command, rest = argv[0], argv[1:]
    parts = [
        f'"{part}"' if any(ch.isspace() for ch in part) or not part else part
        for part in ["loom", command, "--write", *rest]
    ]
    return " ".join(parts)


# The command families: `loom --help` lists the commands
# grouped by the task they serve, in this order. The subparsers
# are also created in this order, so argparse's own listings
# (the usage line, invalid-choice errors) agree with the help.
_COMMAND_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("author", ("validate", "generate", "create-new", "search")),
    ("adopt", ("register", "convert", "plan")),
    ("verify", ("check", "coverage", "prove")),
    ("sources", ("inspect-sources", "snapshot-sources", "diff-sources")),
)


class _RootParser(argparse.ArgumentParser):
    """The root parser, whose help groups commands by family.

    Subcommand parsers inherit this class (argparse's default
    parser_class is the root's type); they hold no subcommands,
    so their format_help falls back to the standard rendering -
    only the root's help is grouped.
    """

    def format_help(self) -> str:
        action = next(
            (
                a
                for a in self._actions
                if isinstance(a, argparse._SubParsersAction)
            ),
            None,
        )
        # Help renders before the CLI sets the colour switch, so
        # both help surfaces ask the one colour path directly
        # about the real stdout.
        colour = colour_enabled(sys.stdout)
        if action is None:
            text = super().format_help()
            if not colour:
                return text
            return "\n".join(
                paint(line, BOLD, enabled=True)
                if line.startswith(
                    ("usage:", "options:", "positional arguments:")
                )
                else line
                for line in text.split("\n")
            )
        helps = {
            pseudo.dest: (pseudo.help or "")
            for pseudo in action._choices_actions
        }
        width = shutil.get_terminal_size().columns - 2
        name_width = (
            max(
                len(name)
                for _family, names in _COMMAND_FAMILIES
                for name in names
            )
            + 2
        )

        def heading(text: str) -> str:
            return paint(text, BOLD, enabled=True) if colour else text

        lines = [
            f"usage: {self.prog} [-h] [--version] <command> ...",
            "",
            heading("commands, grouped by task:"),
        ]
        for family, names in _COMMAND_FAMILIES:
            lines.append("")
            lines.append(heading(f"  {family}:"))
            for name in names:
                wrapped = textwrap.wrap(
                    helps.get(name, ""),
                    width=max(20, width - 4 - name_width),
                ) or [""]
                lines.append(f"    {name:<{name_width}}{wrapped[0]}")
                for extra in wrapped[1:]:
                    lines.append(f"    {'':<{name_width}}{extra}")
        lines.append("")
        lines.append(heading("options:"))
        for option in self._actions:
            if isinstance(
                option, (argparse._HelpAction, argparse._VersionAction)
            ):
                lines.append(
                    f"  {', '.join(option.option_strings):<12}"
                    f"{option.help or ''}"
                )
        lines.append("")
        lines.append(
            "Mutating commands (generate, register, convert, "
            "snapshot-sources) preview by"
        )
        lines.append(
            "default and write only with --write (or when LOOM_WRITE is set)."
        )
        lines.append("")
        lines.append("Run 'loom <command> --help' for a command's options.")
        return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = _RootParser(prog="loom")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate",
        help="validate pattern definitions, instance sets and model registrations",
        description=(
            "Validate the project's Loom metadata: pattern definitions, "
            "instance sets (expanded and checked against the generator "
            "contracts) and model registrations, including cross-model "
            "relationships. Read-only: writes nothing."
        ),
        epilog="Example: loom validate --project-dir .",
    )
    _add_project_dir_arg(validate)
    _add_selection_args(validate)
    _add_format_args(validate)

    generate = commands.add_parser(
        "generate",
        help=(
            "render dbt resource files from the inventory "
            "(preview by default; --write writes)"
        ),
        description=(
            "Render the project's generated instances to dbt model SQL, "
            "data-test YAML, SCD2 snapshots and the inline-source "
            "declarations document. Everything is rendered before "
            "anything is written. Preview by default (a dry run: the "
            "output reports what would be written, changed or left "
            "unchanged); --write writes the files, and --check is the "
            "CI drift mode and never writes."
        ),
        epilog="Example: loom generate --write",
    )
    _add_project_dir_arg(generate)
    generate.add_argument(
        "--dialect",
        help="target database dialect (auto-detected from dbt profile by default)",
    )
    _add_write_args(generate, "the generated files")
    _add_selection_args(generate)
    generate.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing files even when they are not Loom-generated",
    )
    generate.add_argument(
        "--check",
        action="store_true",
        help=(
            "verify the generated files on disk match the metadata without "
            "writing anything; exit 1 when any file is missing or differs "
            "(drift check for CI; takes precedence over --write)"
        ),
    )
    _add_format_args(generate)
    parser.add_argument(
        "--version", action="version", version=f"loom {__version__}"
    )

    create_new = commands.add_parser(
        "create-new",
        help="print an authoring snippet for a new artefact on a model",
        description=(
            "Print an authoring snippet for a new artefact on a model: "
            "a unit-test or data-test declaration in the form the "
            "model's state calls for (a metadata snippet for a "
            "generated model, the dbt-YAML form for a hand-written "
            "one). Print-only: writes nothing, and the snippet takes "
            "effect only once pasted into the practitioner's files."
        ),
        epilog="Example: loom create-new unit-test hub_customer",
    )
    create_new.add_argument(
        "kind",
        help="the snippet to print: 'unit-test' or 'data-test'",
    )
    create_new.add_argument(
        "--test",
        help="data-test only: the generic test to declare (a bare "
        "name or package.name; Loom never invents one)",
    )
    create_new.add_argument(
        "--column",
        help="data-test only: the column the test attaches to "
        "(omit for a model-level test)",
    )
    create_new.add_argument(
        "model",
        help="name of the model: a generated model gets the "
        "metadata snippet; a hand-written model (manifest only) "
        "gets the dbt-YAML form",
    )
    _add_project_dir_arg(create_new)
    create_new.add_argument(
        "--manifest",
        type=Path,
        help="dbt manifest path (default: <project-dir>/target/manifest.json); "
        "read for hand-written models",
    )
    _add_format_args(create_new)

    search = commands.add_parser(
        "search",
        help=(
            "search the inventory: models, macros and sources by "
            "name, shape, owner or impact"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Search the inventory - Loom metadata and the dbt manifest\n"
            "read as one corpus - and see what already exists before\n"
            "you build. With no query it prints a project overview:\n"
            "counts by status, generator, owner and paradigm,\n"
            "documentation coverage, and suggested queries drawn from\n"
            "the project's actual state. A query that resolves to\n"
            "exactly one object prints that object's detail view.\n"
            "Search reads the project's metadata and, when one exists,\n"
            "its target/manifest.json; with no manifest it runs over\n"
            "metadata only and says so. Read-only: writes nothing.\n"
            "\n"
            "Selector arguments use the shared selector grammar: bare\n"
            "terms are ranked substring matches (exact name first),\n"
            "while qualifier values match exactly unless globbed with\n"
            "*. Commas inside one argument intersect; separate\n"
            "arguments union. A query carrying two or more shape\n"
            "qualifiers (key:, column:, input:, generator:, paradigm:)\n"
            "also returns close matches by shape overlap, labelled as\n"
            "banded; the same selectors under --select on an action\n"
            "command stay strict. Or search by example:\n"
            "--similar-to <model> ranks every other model by overlap\n"
            "with that model's shape, each result carrying its score\n"
            "and what it shares with, and how it differs from, the\n"
            "reference model."
        ),
        epilog=(
            "Example:\n"
            "  loom search                                  project overview (orientation)\n"
            "  loom search hub_customer                     one model, in detail\n"
            "  loom search \"key:customer_id,column:email\"   duplication check by shape\n"
            "  loom search --similar-to hub_customer        duplication check by example: models shaped like this one\n"
            "  loom search downstream:hub_customer          impact: what this feeds\n"
            "  loom search has:no-owner                     hygiene: unowned models\n"
            "  loom search is:unmanaged                     hygiene: models Loom does not manage\n"
            "  loom search kind:macro                       macros, project and package\n"
            "  loom search slug --names-only                names only, for piping\n"
            "  loom search is:orphaned --count-only          just the count\n"
            "\n"
            "Qualifiers:\n"
            "  generator: pattern: paradigm: classification: owner: tag:\n"
            "  column: key: input: doc: kind: is: has: path: package:\n"
            "  upstream: downstream: (with an optional depth: companion)\n"
            "  selector: (a named selector from selectors.yml)\n"
            "Qualifier values match exactly (case-insensitively) unless\n"
            "they carry a * glob; bare terms are the forgiving form."
        ),
    )
    search.add_argument(
        "query",
        nargs="*",
        metavar="SELECTOR",
        help=(
            "selector arguments to search by (comma-separated "
            "criteria in one argument intersect; separate "
            "arguments union); with no selector, prints the "
            "project overview"
        ),
    )
    _add_project_dir_arg(search)
    search.add_argument(
        "--select",
        action="append",
        metavar="SELECTOR",
        help=(
            "additional selector arguments, equivalent to "
            "supplying them as the query (the flag form scripts "
            "can use on every model-scoped command); repeatable"
        ),
    )
    search.add_argument(
        "--exclude",
        action="append",
        metavar="SELECTOR",
        help="subtract records matching the selector from the results",
    )
    search.add_argument(
        "--similar-to",
        metavar="MODEL",
        help=(
            "search by example: rank every other model by shape "
            "overlap with the named model's fingerprint (keys, "
            "inputs, output columns, generator family); cannot "
            "be combined with selector arguments, --select, "
            "--exclude, --count-only or --names-only"
        ),
    )
    search.add_argument(
        "--fields",
        metavar="FIELD,...",
        help=(
            "json output only: comma-separated record fields to "
            "include in each result, instead of the full record "
            "(for example --fields name,status,owner)"
        ),
    )
    search.add_argument(
        "--count-only",
        action="store_true",
        help=(
            "print only the match count (with --format json, an "
            "object carrying the total, full and close counts)"
        ),
    )
    search.add_argument(
        "--names-only",
        action="store_true",
        help=(
            "print one matching name per line, clean for piping "
            "to other tools (with --format json, an object "
            "carrying the name list)"
        ),
    )
    _add_format_args(search)

    # The migration pathway's operations are flat verbs. A
    # grouped spelling (`loom migration assess`, `loom migration
    # plan`) was retired: "migration" names the pathway, never a
    # command, and the two acts get their own commands over the
    # one assessment engine: `register` (inventory + registration
    # drafts) and `convert` (fit detection + conversion drafts).
    # `plan` phases both acts, so it nests under neither. The
    # retired names (`migration-assessment`, `migration-plan`,
    # the group) are not aliases. The CLI surface is frozen.
    register = commands.add_parser(
        "register",
        help=(
            "register an existing dbt project's models in the Loom "
            "inventory: report every model's state and draft a "
            "registration for each unknown model "
            "(preview by default; --write writes the drafts)"
        ),
        description=(
            "Report every project model's Loom state and draft a "
            "registration for each unknown model. Preview by default "
            "(a dry run: the report shows the drafts and what a write "
            "run would add); --write writes the drafted registrations "
            "into local/registrations/ (never overwrites an existing "
            "file). Registering a model also withdraws it from "
            "convert's drafting pool."
        ),
        epilog="Example: loom register --write",
    )
    _add_project_dir_arg(register)
    register.add_argument(
        "--manifest",
        type=Path,
        help="dbt manifest path (default: <project-dir>/target/manifest.json)",
    )
    _add_write_args(register, "the drafted registrations")
    _add_selection_args(register)
    _add_format_args(register)
    convert = commands.add_parser(
        "convert",
        help=(
            "draft conversions for an existing dbt project: report "
            "generator-fit proposals and authoring drafts for layered "
            "models with no fit (preview by default; --write writes "
            "the drafts)"
        ),
        description=(
            "Report generator-fit proposals and authoring drafts for "
            "an existing dbt project's models. Preview by default "
            "(a dry run: the report shows the drafts and what a write "
            "run would add); --write writes the unified drafts into "
            "local/drafts/ (never overwrites an existing file). A "
            "draft never swaps a model; the playbook's proof gates "
            "the swap."
        ),
        epilog="Example: loom convert --write",
    )
    _add_project_dir_arg(convert)
    convert.add_argument(
        "--manifest",
        type=Path,
        help="dbt manifest path (default: <project-dir>/target/manifest.json)",
    )
    _add_write_args(convert, "the unified drafts")
    _add_selection_args(convert)
    convert.add_argument(
        "--include-registered",
        action="store_true",
        help=(
            "also draft registered (known, not controlled) "
            "models: by default convert drafts only models "
            "unknown to Loom - registering a model withdraws "
            "it from the drafting pool"
        ),
    )
    _add_format_args(convert)
    plan = commands.add_parser(
        "plan",
        help=(
            "plan replacing one model with another: inventory known "
            "dbt dependents and suggest migration phases"
        ),
        description=(
            "Plan replacing one model with another: inventory the "
            "known dbt dependents of the old model (direct and "
            "transitive, plus observed manifest consumers) and "
            "suggest migration phases. Read-only: writes nothing."
        ),
        epilog="Example: loom plan --from dim_customers_scd2 --to hub_customer",
    )
    _add_project_dir_arg(plan)
    plan.add_argument("--from", dest="from_model", required=True, metavar="MODEL")
    plan.add_argument("--to", dest="to_model", required=True, metavar="MODEL")
    plan.add_argument(
        "--manifest",
        type=Path,
        help="dbt manifest path (default: <project-dir>/target/manifest.json)",
    )
    _add_format_args(plan)
    check = commands.add_parser(
        "check",
        help=(
            "run the combined gate: validate the metadata, check "
            "inventory coverage and verify macro references"
        ),
        description=(
            "The combined gate: validate the metadata, check "
            "manifest-backed inventory coverage, and verify generated "
            "instances' staging macro references against the manifest, "
            "in one command. Read-only: writes nothing."
        ),
        epilog="Example: loom check",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help=(
            "fail when a generic_tests declaration is not attested "
            "by the manifest (a warning without this flag)"
        ),
    )
    _add_project_dir_arg(check)
    check.add_argument(
        "--manifest",
        type=Path,
        help="dbt manifest path (default: <project-dir>/target/manifest.json)",
    )
    _add_selection_args(check)
    _add_format_args(check)

    coverage = commands.add_parser(
        "coverage",
        help="check inventory coverage for project-owned dbt models",
        description=(
            "Compare the Loom inventory against the project-owned dbt "
            "models in the manifest, by name and - for generated "
            "models - by expected output path. Read-only: writes "
            "nothing."
        ),
        epilog="Example: loom coverage --format json",
    )
    _add_project_dir_arg(coverage)
    coverage.add_argument(
        "--manifest",
        type=Path,
        help="dbt manifest path (default: <project-dir>/target/manifest.json)",
    )
    _add_selection_args(coverage)
    _add_format_args(coverage)

    prove = commands.add_parser(
        "prove",
        help=(
            "run the static equivalence stage over the compiled "
            "manifests in local/equivalence/"
        ),
        description=(
            "Run the static equivalence stage for conversions: "
            "compare the two practitioner-compiled manifests in "
            "local/equivalence/ and report, per model, whether the "
            "candidate is statically proven equivalent to the "
            "original or the stage is inconclusive. Read-only: "
            "writes nothing."
        ),
        epilog="Example: loom prove --format json",
    )
    _add_project_dir_arg(prove)
    _add_selection_args(prove)
    _add_format_args(prove)

    inspect = commands.add_parser(
        "inspect-sources",
        help="inspect selected physical sources in a local DuckDB file",
        description=(
            "Inspect the physical columns of the local DuckDB sources "
            "that generated staging models map - names, types, "
            "ordinals, and which staging models map each column - "
            "without reading rows. Read-only: writes nothing."
        ),
        epilog="Example: loom inspect-sources --database quickstart.duckdb",
    )
    _add_project_dir_arg(inspect)
    inspect.add_argument(
        "--database",
        required=True,
        type=Path,
        help="existing project-local DuckDB file",
    )
    inspect.add_argument("--manifest", type=Path, default=Path("target/manifest.json"))
    inspect.add_argument(
        "--source", help="dbt source name (default: all mapped sources)"
    )
    inspect.add_argument("--table", help="dbt source table; requires --source")
    _add_format_args(inspect)
    capture = commands.add_parser(
        "snapshot-sources",
        help=(
            "capture an explicit ignored local source-structure "
            "baseline (preview by default; --write captures)"
        ),
        description=(
            "Capture an explicit, create-only baseline of the "
            "physical source structure (the inspect-sources view, "
            "minus mapping detail) so later drift can be reported "
            "against it; the baseline's path must be git-ignored. "
            "Preview by default (a dry run: reports the baseline "
            "that would be captured); --write captures it (never "
            "overwrites an existing baseline)."
        ),
        epilog="Example: loom snapshot-sources --database quickstart.duckdb --write",
    )
    _add_project_dir_arg(capture)
    capture.add_argument("--database", required=True, type=Path)
    capture.add_argument("--manifest", type=Path, default=Path("target/manifest.json"))
    capture.add_argument(
        "--output",
        type=Path,
        default=Path("target/loom/source-structure.json"),
        help="new ignored baseline (default: <project-dir>/target/loom/source-structure.json)",
    )
    _add_write_args(capture, "the baseline")
    _add_format_args(capture)
    compare = commands.add_parser(
        "diff-sources",
        help="report local source-structure changes against a baseline",
        description=(
            "Report physical source-structure changes against a "
            "baseline captured by snapshot-sources: sources and "
            "columns added or removed, and type, ordinal and "
            "location changes. Advisory and read-only: writes "
            "nothing."
        ),
        epilog="Example: loom diff-sources --database quickstart.duckdb",
    )
    _add_project_dir_arg(compare)
    compare.add_argument("--database", required=True, type=Path)
    compare.add_argument("--manifest", type=Path, default=Path("target/manifest.json"))
    compare.add_argument(
        "--baseline",
        type=Path,
        default=Path("target/loom/source-structure.json"),
        help="captured baseline (default: <project-dir>/target/loom/source-structure.json)",
    )
    _add_format_args(compare)
    return parser


def _is_loom_generated_text(text: str) -> bool:
    """Generated files carry a provenance header on their first line.

    That header is Loom's ownership marker: a file at a
    generated path whose first line lacks it is treated as
    practitioner-authored and is never silently overwritten.
    """
    first_line = text.split("\n", 1)[0]
    return first_line.startswith(("-- Generated by Loom", "# Generated by Loom"))


# The consequence line of a `generate` shadowed-models section:
# in preview it states what a write run would bring about, in a
# write report what the run has left behind - the fact is the
# same in both.
_GENERATE_SHADOW_LEAD = (
    "dbt cannot parse the project while a generated model and a "
    "hand-written model share a name, so every dbt command "
    "(parse, seed, build) will fail until the swap is completed"
)


def _shadowed_models(
    project_dir: str | Path,
    entries: list[InventoryEntry],
    *,
    planned_paths: Iterable[Path] = (),
) -> list[dict[str, Any]]:
    """The project's shadowed models (P5): generated models
    whose name a hand-written model file still carries.

    A generated model under its expected output path and a
    hand-written model of the same name elsewhere in the model
    paths leave dbt unable to parse the project at all - every
    dbt command fails on the duplicate name, so the manifest
    can never be refreshed out of the state. Detection is
    filesystem-only by necessity: in this state the manifest is
    stale and unrefreshable, so nothing here reads it. The
    generated side of each pair is resolved with the one layout
    resolver (``expected_model_path``, the same path coverage's
    misplaced check compares against); the hand-written side is
    every other model file under the configured model paths
    carrying the same file stem. ``planned_paths`` (a generate
    run's plan) lets a preview name pairs whose generated file
    the run itself would write.

    Returns one ``{"name", "generated", "hand_written"}`` dict
    per shadowed name, sorted by name, with project-relative
    POSIX paths; empty when the project is not shadowed. Any
    layout problem (unreadable project config, an unresolvable
    output subpath) narrows detection rather than failing the
    caller's command over it."""
    try:
        config = _project_config(Path(project_dir))
        model_paths = _configured_paths(config, "model")
        model_root = _output_root(config, "model")
    except LoomError:
        return []
    root = Path(project_dir).expanduser().resolve()

    def display(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    by_stem: dict[str, list[Path]] = {}
    for relative in model_paths:
        base = root / relative
        if not base.is_dir():
            continue
        for candidate in sorted(base.rglob("*")):
            if candidate.is_file() and candidate.suffix.lower() in (
                ".sql",
                ".py",
            ):
                by_stem.setdefault(candidate.stem, []).append(candidate)
    planned = {Path(path).resolve() for path in planned_paths}
    candidates: list[tuple[str, Path]] = []
    for entry in entries:
        if not entry.is_generated:
            continue
        name = entry.data.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            expected = expected_model_path(model_root, entry.data)
        except LoomError:
            continue
        generated = root / expected
        candidates.append((name, generated))
        if entry.data.get("generator") == "satellite":
            # A satellite's auxiliary view is a second generated
            # model beside the satellite's own file (the same
            # derived names coverage's auxiliary set knows).
            variant = entry.data.get("variant", "standard")
            auxiliary = None
            if variant in ("standard", "multi_active"):
                auxiliary = f"{name}_current"
            elif variant == "effectivity":
                auxiliary = f"{name}_windows"
            if auxiliary is not None:
                candidates.append(
                    (auxiliary, generated.parent / f"{auxiliary}.sql")
                )
    generated_paths = {path.resolve() for _name, path in candidates}
    pairs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, generated in candidates:
        if name in seen:
            continue
        if not generated.is_file() and generated.resolve() not in planned:
            continue
        twins = [
            candidate
            for candidate in by_stem.get(name, [])
            if candidate.resolve() not in generated_paths
        ]
        if not twins:
            continue
        seen.add(name)
        pairs.append(
            {
                "name": name,
                "generated": display(generated),
                "hand_written": sorted(display(twin) for twin in twins),
            }
        )
    return sorted(pairs, key=lambda pair: pair["name"])


def _project_shadowed_models(
    project_dir: str | Path,
) -> list[dict[str, Any]]:
    """``_shadowed_models`` over the project's own inventory,
    loaded the way coverage loads it. A project whose metadata
    cannot be loaded reports no pairs: the caller's normal path
    surfaces the load error instead."""
    try:
        entries, _duplicates = _load_project(
            Path(project_dir),
            reject_duplicate_names=False,
            validate_relationships=False,
        )
    except LoomError:
        return []
    return _shadowed_models(project_dir, entries)


def _emit_snippet(args: argparse.Namespace, snippet: str) -> int:
    """Print a create-new snippet: raw in text mode (it is meant
    to be pasted), as a structured payload in json mode."""
    if getattr(args, "format", "text") == "json":
        print(
            json.dumps(
                {
                    "command": "create-new",
                    "kind": args.kind,
                    "model": args.model,
                    "snippet": snippet,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(snippet, end="")
    return 0


def _cmd_create_new(args: argparse.Namespace) -> int:
    """Handle create-new: print an authoring snippet.

    Writes nothing - the snippet belongs in the practitioner's own
    files, theirs to fill in; nothing is generated from it until
    the sentinel values are replaced. Each kind has two forms, by
    the model's state: a generated model gets the metadata
    snippet (it pastes into the model's instance params); a
    hand-written model - in the manifest but not generated
    inventory - gets the dbt-YAML form dbt itself reads. The
    data-test kind declares one generic test (--test, optional
    --column): the metadata form is a `generic_tests:` carriage
    declaration; a test whose arguments Loom cannot know
    carries the `__TODO__` kwargs sentinel the carriage
    validator rejects, so the snippet cannot validate unfinished.
    """
    if args.kind not in ("unit-test", "data-test"):
        print(
            f"loom: unknown kind {args.kind!r} - create-new "
            "supports 'unit-test' and 'data-test'",
            file=sys.stderr,
        )
        return 1
    if args.kind == "unit-test" and (
        args.test is not None or args.column is not None
    ):
        print(
            "loom: --test and --column belong to the 'data-test' "
            "kind",
            file=sys.stderr,
        )
        return 1
    if args.kind == "data-test":
        # The declaration's shape rules are the carriage
        # validator's: a bare or package-qualified test
        # reference, a safe-identifier column. Loom never invents
        # the test - --test is required.
        from loom.inventory_records import (
            _IDENTIFIER_PATTERN,
            _MACRO_REFERENCE_PATTERN,
        )

        if args.test is None:
            print(
                "loom: the 'data-test' kind needs --test <name>: "
                "the generic test to declare",
                file=sys.stderr,
            )
            return 1
        if not _MACRO_REFERENCE_PATTERN.fullmatch(args.test):
            print(
                f"loom: --test {args.test!r} is not a safe test "
                "reference - a bare name or package.name",
                file=sys.stderr,
            )
            return 1
        if args.column is not None and not _IDENTIFIER_PATTERN.fullmatch(
            args.column
        ):
            print(
                f"loom: --column {args.column!r} must be a safe "
                "SQL identifier",
                file=sys.stderr,
            )
            return 1
    try:
        entries = validate_project(args.project_dir)
    except LoomError as e:
        print(f"loom: {e}", file=sys.stderr)
        return 1
    entry = next(
        (
            entry
            for entry in entries
            if entry.is_generated and entry.data.get("name") == args.model
        ),
        None,
    )
    if entry is not None:
        if args.kind == "data-test":
            by_name = {
                entry.data["name"].casefold(): entry.data
                for entry in entries
                if isinstance(entry.data.get("name"), str)
            }
            column_unverified = False
            if args.column is not None:
                resolved = resolve_output_columns(entry.data, by_name)
                if resolved is not None:
                    names = [column.name for column in resolved]
                    if args.column.casefold() not in {
                        name.casefold() for name in names
                    }:
                        print(
                            f"loom: model {args.model!r} has no "
                            f"output column {args.column!r} - its "
                            "derivable output columns are: "
                            + ", ".join(names),
                            file=sys.stderr,
                        )
                        return 1
                else:
                    column_unverified = True
            if resolve_skip_properties_document(entry.data):
                print(
                    f"loom: warning: model {args.model!r} declares "
                    "skip_properties_document - a generic_tests "
                    "declaration conflicts with that opt-out "
                    "(carriage needs the properties document); "
                    "drop the opt-out, or declare the test in "
                    "your own schema YAML instead",
                    file=sys.stderr,
                )
            return _emit_snippet(
                args,
                metadata_data_test_snippet(
                    args.test,
                    args.column,
                    column_unverified=column_unverified,
                ),
            )
        generator = entry.data.get("generator")
        if generator in UNIT_TEST_EXCLUSIONS:
            print(
                f"loom: unit tests are not supported for generator "
                f"{generator!r} in v1: {UNIT_TEST_EXCLUSIONS[generator]}",
                file=sys.stderr,
            )
            return 1
        by_name = {
            entry.data["name"].casefold(): entry.data
            for entry in entries
            if isinstance(entry.data.get("name"), str)
        }
        return _emit_snippet(args, scaffold_snippet(entry.data, by_name))
    # Hand-written model: derive the dbt-YAML form from the
    # manifest - given inputs from the node's depends_on (models
    # as ref('…'), sources as source('…', '…')), expect columns
    # from the node's documented columns, with the same honest
    # degradation as the metadata snippet where documentation is
    # absent. The freshness guard is the one every manifest
    # reader here applies.
    try:
        manifest_file, document = read_manifest(
            args.project_dir,
            args.manifest,
            sections=("nodes", "sources"),
        )
        validate_model_freshness(args.project_dir, manifest_file, document)
    except LoomError as e:
        print(f"loom: {e}", file=sys.stderr)
        return 1
    nodes = document.get("nodes")
    nodes = nodes if isinstance(nodes, dict) else {}
    sources = document.get("sources")
    sources = sources if isinstance(sources, dict) else {}
    project_name = (_project_config(args.project_dir) or {}).get("name")
    candidates = [
        node
        for node in nodes.values()
        if isinstance(node, dict)
        and node.get("resource_type") == "model"
        and node.get("name") == args.model
    ]
    node = next(
        (n for n in candidates if n.get("package_name") == project_name),
        candidates[0] if candidates else None,
    )
    if node is None:
        print(
            f"loom: no model named {args.model!r} in the inventory "
            "or the manifest",
            file=sys.stderr,
        )
        return 1

    def documented_columns(holder: Any) -> list[str] | None:
        columns = holder.get("columns") if isinstance(holder, dict) else None
        if isinstance(columns, dict) and columns:
            return [str(name) for name in columns]
        return None

    depends_on = node.get("depends_on")
    dep_ids = depends_on.get("nodes") if isinstance(depends_on, dict) else None
    given: list[tuple[str, list[str] | None]] = []
    for dep_id in dep_ids if isinstance(dep_ids, list) else []:
        if not isinstance(dep_id, str):
            continue
        if dep_id.startswith("source."):
            source = sources.get(dep_id)
            if isinstance(source, dict):
                expression = (
                    f"source({source.get('source_name')!r}, "
                    f"{source.get('name')!r})"
                )
                given.append((expression, documented_columns(source)))
            else:
                given.append((f"source({dep_id.rsplit('.', 2)[-2]!r}, {dep_id.rsplit('.', 1)[-1]!r})", None))
            continue
        target = nodes.get(dep_id)
        dep_name = (
            target.get("name")
            if isinstance(target, dict) and isinstance(target.get("name"), str)
            else dep_id.rsplit(".", 1)[-1]
        )
        given.append((f"ref({dep_name!r})", documented_columns(target)))
    if args.kind == "data-test":
        column_unverified = False
        if args.column is not None:
            documented = documented_columns(node)
            if documented is not None:
                if args.column.casefold() not in {
                    name.casefold() for name in documented
                }:
                    print(
                        f"loom: model {args.model!r} documents no "
                        f"column {args.column!r} in the manifest "
                        "- its documented columns are: "
                        + ", ".join(documented),
                        file=sys.stderr,
                    )
                    return 1
            else:
                column_unverified = True
        return _emit_snippet(
            args,
            dbt_yaml_data_test_snippet(
                args.model,
                args.test,
                args.column,
                column_unverified=column_unverified,
            ),
        )
    return _emit_snippet(
        args,
        dbt_yaml_snippet(args.model, given, documented_columns(node)),
    )


def _unit_test_floor_warning(
    project_dir: Path, entries: list
) -> str | None:
    """The conditional dbt >= 1.8 floor for unit tests.

    Warn-not-fail: the only version evidence available is the
    project's existing manifest, which may be stale or produced in
    another environment, so an old dbt_version warns and generation
    continues.
    """
    declares = any(
        entry.is_generated
        and isinstance(entry.data.get("unit_tests"), list)
        and bool(entry.data.get("unit_tests"))
        for entry in entries
    )
    if not declares:
        return None
    manifest_path = project_dir / "target" / "manifest.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    metadata = document.get("metadata") if isinstance(document, dict) else None
    version = metadata.get("dbt_version") if isinstance(metadata, dict) else None
    if not isinstance(version, str):
        return None
    parts = version.split(".")
    try:
        major_minor = (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None
    if major_minor >= (1, 8):
        return None
    return (
        f"the project declares unit tests, which require dbt >= 1.8; "
        f"the manifest at target/manifest.json was produced by dbt "
        f"{version}. The manifest may be stale - generation continues."
    )


def _cmd_generate(
    args: argparse.Namespace, apply_hint: str | None = None
) -> int:
    """Handle the generate command.

    Three modes share the one plan phase (everything renders
    before anything is written): --check (the CI drift mode,
    which takes precedence and never writes), write mode
    (--write or LOOM_WRITE), and the default preview, which
    classifies every planned file as would-write, would-change,
    unchanged or blocked and ends with the apply hint.
    """
    project_dir = args.project_dir
    check_mode = getattr(args, "check", False)
    write_mode, mode_source = _write_mode(args)
    json_mode = getattr(args, "format", "text") == "json"
    verbose = getattr(args, "verbose", False)
    if not args.dialect and find_profiles_file(project_dir) is None:
        print(
            "loom: warning: no profiles.yml found in the project directory, "
            "$DBT_PROFILES_DIR, or ~/.dbt; defaulting to the DuckDB dialect. "
            "Pass --dialect to generate for a different warehouse.",
            file=sys.stderr,
        )
    dialect_name = args.dialect or detect_dialect(project_dir)

    # Validate dialect
    try:
        from loom.dialects import get_dialect

        dialect = get_dialect(dialect_name)
    except LoomError as e:
        print(f"loom: {e}", file=sys.stderr)
        return 1

    # Validate project first
    try:
        entries = validate_project(project_dir)
    except LoomError as e:
        print(f"loom: {e}", file=sys.stderr)
        return 1
    for warning in unit_test_warnings(entries):
        print(f"loom: warning: {warning}", file=sys.stderr)
    for warning in contract_warnings(entries):
        print(f"loom: warning: {warning}", file=sys.stderr)
    if not dialect.contract_proven and any(
        entry.is_generated and resolve_contract_enforced(entry.data)
        for entry in entries
    ):
        print(
            "loom: warning: contract enforcement is unproven on "
            f"{dialect_name}; declared types for opted-in models are "
            "rendered from adapter documentation and may fail dbt "
            "preflight",
            file=sys.stderr,
        )
    floor_warning = _unit_test_floor_warning(project_dir, entries)
    if floor_warning:
        print(f"loom: warning: {floor_warning}", file=sys.stderr)

    # Generation writes under the project's configured output roots
    #: the first model-paths / snapshot-paths entry, or the
    # conventional defaults. Registry output_roots are expressed
    # against those defaults and are remapped onto them here.
    try:
        project_config = _project_config(project_dir)
        model_root = _output_root(project_config, "model")
        snapshot_root = _output_root(project_config, "snapshot")
    except LoomError as e:
        print(f"loom: {e}", file=sys.stderr)
        return 1

    def resolve_root(root: str) -> str:
        if root == "models":
            return model_root
        if root.startswith("models/"):
            return f"{model_root}/{root.removeprefix('models/')}"
        if root == "snapshots":
            return snapshot_root
        return root

    # Selection: the shared selector engine over the metadata
    # corpus, narrowed to the generatable pool. Commas within
    # one --select intersect (the dbt reading; generate's old
    # ad hoc comma-union reading is retired with this change),
    # repeated flags union, --exclude subtracts, and a selector
    # matching nothing in the pool is an error naming it.
    generated_entries = [
        entry
        for entry in entries
        if entry.is_generated and entry.data.get("generator") in RENDERERS
    ]
    selected_entries = generated_entries
    if getattr(args, "select", None) or getattr(args, "exclude", None):
        try:
            corpus = corpus_from_entries(project_dir, entries)
            selection = _selection_for(
                args,
                records_from_corpus(corpus),
                pool={
                    entry.data["name"]
                    for entry in generated_entries
                    if isinstance(entry.data.get("name"), str)
                },
                pool_label="generated models",
            )
        except LoomError as e:
            print(f"loom: {e}", file=sys.stderr)
            return 1
        keep = set(selection.names)
        selected_entries = [
            entry
            for entry in generated_entries
            if entry.data.get("name") in keep
        ]

    # The generate-time dialect gate, run as a
    # preflight over every selected instance before anything is
    # rendered or written. Validation is dialect-free, so this is
    # the first point dialect truth can judge a declared
    # `incremental` strategy; a strategy the target dialect does not
    # support aborts the whole generation with nothing written (the
    # plan phase below would otherwise write the models that did
    # render and strand the gated one).
    gate_dialect = get_dialect(dialect_name)
    gate_failures: list[str] = []
    for entry in selected_entries:
        block = entry.data.get("incremental")
        if not isinstance(block, dict):
            continue
        strategy = block.get("strategy")
        if not isinstance(strategy, str):
            continue  # the contract layer already reported the shape
        try:
            BaseRenderer._check_incremental_strategy(
                IncrementalSpec(strategy=strategy),
                gate_dialect,
                model_name=str(entry.data.get("name")),
            )
        except LoomError as error:
            gate_failures.append(str(error))
    if gate_failures:
        for failure in gate_failures:
            print(f"loom: {failure}", file=sys.stderr)
        print(
            "Generation aborted: an incremental strategy is not "
            "supported by the target dialect; nothing was written.",
            file=sys.stderr,
        )
        return 1

    # Plan phase: render everything before writing anything, so a
    # failure or an ownership conflict cannot leave a half-generated
    # project behind.
    plan: list[tuple[Path, str]] = []
    written = 0
    tests_written = 0
    properties_written = 0
    unit_files_written = 0
    unit_test_details: list[str] = []
    properties_by_name = {
        entry.data["name"].casefold(): entry.data
        for entry in entries
        if isinstance(entry.data.get("name"), str)
    }
    failed_models = 0
    test_details: list[str] = []
    create_only_paths: set[Path] = set()
    # Create-only files that already exist are left out of the
    # plan, so without this count a re-run's headline drops even
    # when a model was just added (23 -> 22 on a re-run). The
    # headline states the split instead.
    create_only_current = 0
    # The properties-document opt-out, collected during
    # the plan loop and named in the report: models whose
    # properties entry was skipped, models whose merged data-test
    # document was skipped with it, and Loom-headed documents
    # left on disk from before the opt-out (never deleted -
    # reported, like stale SQL).
    properties_skipped: list[str] = []
    data_documents_skipped: list[str] = []
    properties_leftovers: list[tuple[str, Path]] = []
    for entry in selected_entries:
        data = entry.data
        generator = data.get("generator")
        name = data.get("name")
        try:
            renderer = get_renderer(generator, dialect_name)
            params = {k: v for k, v in data.items() if k != "_paradigm"}
            opted_out = resolve_skip_properties_document(data)
            # Determine output paths from generator spec, remapped
            # onto the configured roots
            spec = GENERATOR_REGISTRY.get(generator)
            if spec:
                raw_roots = spec.output_roots if spec.output_roots else ("models",)
            else:
                raw_roots = ("models",)
            output_roots = tuple(resolve_root(root) for root in raw_roots)
            # The practitioner's optional nested layout. One
            # resolver; flat when no output_subpath is declared.
            subpath = resolve_output_subpath(data)

            def _out(root: str, filename: str) -> Path:
                base = project_dir / root
                return base / subpath / filename if subpath else base / filename

            entry_plan: list[tuple[Path, str]] = []
            # scd2_dimension outputs snapshot to first root, view to second
            # root, and keeps its create-only semantics: existing snapshot
            # and view files are left untouched.
            if generator == "scd2_dimension":
                snapshot_sql = renderer.render(params, renderer.dialect, None)
                if len(output_roots) > 0:
                    snapshot_path = _out(output_roots[0], f"{name}.sql")
                    if check_mode:
                        # Drift check must compare create-only files
                        # too: the normal plan omits them when they
                        # exist, which would hide exactly the drift a
                        # metadata edit creates.
                        entry_plan.append((snapshot_path, snapshot_sql))
                        create_only_paths.add(snapshot_path)
                    elif not snapshot_path.exists():
                        entry_plan.append((snapshot_path, snapshot_sql))
                    else:
                        create_only_current += 1
                view_sql = renderer.render(params, renderer.dialect, "view")
                if len(output_roots) > 1:
                    view_path = _out(output_roots[1], f"{name}.sql")
                    if check_mode:
                        entry_plan.append((view_path, view_sql))
                        create_only_paths.add(view_path)
                    elif not view_path.exists():
                        entry_plan.append((view_path, view_sql))
                    else:
                        create_only_current += 1
            elif generator == "satellite":
                # Satellite outputs the historised table to the first
                # root; the standard variant also outputs its
                # current-state view (ADR 0028) under a derived name.
                # Unlike scd2_dimension's outputs these are not
                # create-only: they regenerate from metadata.
                sql = renderer.render(params, renderer.dialect, None)
                entry_plan.append((_out(output_roots[0], f"{name}.sql"), sql))
                satellite_variant = params.get("variant", "standard")
                if satellite_variant in ("standard", "multi_active"):
                    view_sql = renderer.render(
                        params, renderer.dialect, "current"
                    )
                    entry_plan.append(
                        (_out(output_roots[-1], f"{name}_current.sql"), view_sql)
                    )
                elif satellite_variant == "effectivity":
                    view_sql = renderer.render(
                        params, renderer.dialect, "windows"
                    )
                    entry_plan.append(
                        (_out(output_roots[-1], f"{name}_windows.sql"), view_sql)
                    )
            else:
                sql = renderer.render(params, renderer.dialect, None)
                output_path = _out(output_roots[-1], f"{name}.sql")
                entry_plan.append((output_path, sql))

            # Generate data tests if the renderer produces them
            entry_tests = 0
            entry_not_null: frozenset[str] = frozenset()
            test_yaml = renderer.render_tests(params, renderer.dialect, None)
            had_test_yaml = bool(test_yaml)
            if test_yaml and opted_out:
                # The opt-out skips the merged document wholesale:
                # the data-test YAML is the properties entry's
                # vehicle when tests exist (one schema file per
                # model in dbt), so the generated data tests go
                # with it. Named in the report below.
                data_documents_skipped.append(name)
                test_yaml = None
            if test_yaml:
                test_path = _out(
                    f"{model_root}/_generated/tests", f"data_{name}.yml"
                )
                import yaml as _yaml

                try:
                    test_data = _yaml.safe_load(test_yaml)
                except _yaml.YAMLError as error:
                    raise LoomError(
                        "renderer produced invalid data-test YAML"
                    ) from error

                entry_plan.append((test_path, test_yaml))
                entry_tests = 1
                # Extract tested columns from the YAML for reporting
                if isinstance(test_data, dict):
                    not_null: set[str] = set()
                    for model in test_data.get("models") or []:
                        if not isinstance(model, dict):
                            continue
                        for column in model.get("columns") or []:
                            if isinstance(column, dict):
                                test_details.append(
                                    f"  {name}: {column.get('name', '')}"
                                )
                                # The data-test matrix is the source
                                # of truth for not_null contract
                                # constraints: a column
                                # earns the constraint exactly when a
                                # generated data test guarantees it.
                                tests = (
                                    column.get("tests")
                                    or column.get("data_tests")
                                    or []
                                )
                                if "not_null" in tests and isinstance(
                                    column.get("name"), str
                                ):
                                    not_null.add(column["name"].casefold())
                    entry_not_null = frozenset(not_null)
            # Generate model properties (catalogue) YAML:
            # descriptive only - description, derivable columns, and
            # types Loom knows or the practitioner declared. dbt
            # rejects a model described by two schema files, so the
            # properties live in the model's single Loom schema
            # document: merged into the data-test YAML when the
            # generator emits tests, otherwise a properties-only
            # document beside it. Either way the file joins the same
            # plan, so ownership preflight and --check cover it.
            entry_properties = 0
            properties_entry = model_properties_entry(
                params,
                renderer.dialect,
                properties_by_name,
                not_null_columns=entry_not_null,
            )
            if properties_entry is not None and opted_out:
                # The entry existed to skip: name it in the
                # report and emit nothing.
                properties_skipped.append(name)
                properties_entry = None
            # Carried generic tests: practitioner
            # declarations from the instance's `generic_tests`,
            # merged into the model's single schema document at
            # the document level - in whichever vehicle the
            # model has: the merged data-test document, the
            # properties-only document, or a document the
            # carriage alone justifies (a model with nothing to
            # publish but tests to carry still gets its document).
            # The opt-out and carriage cannot coexist (validation
            # rejects the combination), so an opted-out model
            # never reaches the merge.
            carried_tests = (
                [] if opted_out else carried_generic_tests(params)
            )
            if properties_entry is not None or carried_tests:
                if test_yaml:
                    document = (
                        merge_properties(test_data, properties_entry)
                        if properties_entry is not None
                        else dict(test_data)
                    )
                    if carried_tests:
                        document = apply_carried_to_document(
                            document, name, carried_tests
                        )
                    merged_yaml = render_schema_document(document)
                    entry_plan = [
                        (
                            planned_path,
                            merged_yaml if planned_path == test_path else content,
                        )
                        for planned_path, content in entry_plan
                    ]
                    if properties_entry is not None:
                        entry_properties = 1
                else:
                    document = (
                        {"version": 2, "models": [properties_entry]}
                        if properties_entry is not None
                        else {"version": 2, "models": [{"name": name}]}
                    )
                    if carried_tests:
                        document = apply_carried_to_document(
                            document, name, carried_tests
                        )
                    properties_path = _out(
                        f"{model_root}/_generated/tests", f"properties_{name}.yml"
                    )
                    entry_plan.append(
                        (properties_path, render_schema_document(document))
                    )
                    entry_properties = 1

            if opted_out:
                # A Loom-headed document from before the opt-out
                # still sits where dbt reads it; the duplicate-
                # description hazard persists until it is removed.
                # Report it; never delete it.
                vehicle = (
                    _out(
                        f"{model_root}/_generated/tests", f"data_{name}.yml"
                    )
                    if had_test_yaml
                    else _out(
                        f"{model_root}/_generated/tests",
                        f"properties_{name}.yml",
                    )
                )
                try:
                    if vehicle.exists() and _is_loom_generated_text(
                        vehicle.read_text(encoding="utf-8")
                    ):
                        properties_leftovers.append((name, vehicle))
                except (OSError, UnicodeDecodeError):
                    pass

            # Declared unit tests: emitted centrally from
            # the metadata declaration - one file per model holding
            # all its declared tests, joining the same plan (header
            # ownership, --check, subpath all inherited).
            entry_unit_file = 0
            unit_yaml = render_unit_tests(params)
            if unit_yaml:
                unit_path = _out(
                    f"{model_root}/_generated/tests", f"unit_{name}.yml"
                )
                import yaml as _yaml

                try:
                    unit_data = _yaml.safe_load(unit_yaml)
                except _yaml.YAMLError as error:
                    raise LoomError(
                        "unit-test emitter produced invalid YAML"
                    ) from error
                entry_plan.append((unit_path, unit_yaml))
                entry_unit_file = 1
                if isinstance(unit_data, dict):
                    for unit_test in unit_data.get("unit_tests") or []:
                        if isinstance(unit_test, dict):
                            unit_test_details.append(
                                f"  {name}: {unit_test.get('name', '')}"
                            )

            plan.extend(entry_plan)
            # "Models" counts generated SQL files; a merged schema
            # document is both the entry's test file and its
            # properties entry, so it must not be subtracted twice.
            written += sum(
                1 for planned_path, _ in entry_plan if planned_path.suffix == ".sql"
            )
            tests_written += entry_tests
            properties_written += entry_properties
            unit_files_written += entry_unit_file
        except (LoomError, OSError, ValueError) as e:
            failed_models += 1
            print(
                f"loom: failed to generate {name} ({generator}): {e}", file=sys.stderr
            )

    # Inline sources document: staging/bronze
    # instances may declare their source inline; generation emits the
    # matching dbt source declarations, grouped by source name, into a
    # Loom-owned file so such a project actually builds. The document
    # is derived from ALL entries, not the current selection - a
    # --select regeneration must never shrink it and strand the models
    # that were not regenerated. It joins the plan like any other
    # output, so ownership preflight and --check cover it too.
    authored = authored_source_keys(project_dir)
    inline_sources: dict[str, dict[str, object]] = {}
    for entry in entries:
        source = entry.data.get("inline_source")
        if not isinstance(source, dict):
            continue
        src_name, src_table = source.get("name"), source.get("table")
        if not isinstance(src_name, str) or not isinstance(src_table, str):
            continue
        if (src_name.casefold(), src_table.casefold()) in authored:
            continue  # the authored declaration governs; do not re-emit
        slot = inline_sources.setdefault(
            src_name, {"schema": None, "tables": {}}
        )
        if slot["schema"] is None and isinstance(source.get("schema"), str):
            slot["schema"] = source["schema"]
        tables = slot["tables"]  # type: ignore[assignment]
        table_slot = tables.setdefault(  # type: ignore[union-attr]
            src_table, {"loaded_at_field": None, "freshness": None}
        )
        # Freshness declarations for one (source, table) merge
        # across the entries that declare it; two different
        # declarations for the same table cannot both govern.
        for field in ("loaded_at_field", "freshness"):
            declared = source.get(field)
            if declared is None:
                continue
            existing = table_slot[field]
            if existing is not None and existing != declared:
                raise LoomError(
                    f"{entry.path}: inline_source {src_name!r} table "
                    f"{src_table!r} declares {field} {declared!r} but "
                    f"another entry declares {existing!r} for the same "
                    "source table; one (source, table) has one "
                    "freshness declaration in the generated sources "
                    "document"
                )
            table_slot[field] = declared
    if inline_sources:
        lines = [
            "# Generated by Loom",
            "# Source declarations for the inline sources declared by",
            "# staging/bronze metadata. Regenerate with `loom generate`.",
            "version: 2",
            "sources:",
        ]
        for src_name in sorted(inline_sources):
            slot = inline_sources[src_name]
            lines.append(f"  - name: {src_name}")
            if isinstance(slot["schema"], str) and slot["schema"].strip():
                lines.append(f"    schema: {slot['schema']}")
            lines.append("    tables:")
            tables = slot["tables"]  # type: ignore[assignment]
            for table in sorted(tables):  # type: ignore[union-attr]
                lines.append(f"      - name: {table}")
                info = tables[table]  # type: ignore[index]
                if isinstance(info["loaded_at_field"], str):
                    lines.append(
                        f"        loaded_at_field: {info['loaded_at_field']}"
                    )
                freshness = info["freshness"]
                if isinstance(freshness, dict):
                    lines.append("        freshness:")
                    for threshold in ("warn_after", "error_after"):
                        spec = freshness.get(threshold)
                        if isinstance(spec, dict):
                            lines.append(
                                f"          {threshold}: "
                                f"{{count: {spec['count']}, "
                                f"period: {spec['period']}}}"
                            )
        sources_path = (
            project_dir / model_root / "_generated/staging" / "sources.yml"
        )
        plan.append((sources_path, "\n".join(lines) + "\n"))

    # Collision check: two outputs resolving to one file -
    # possible once layout templates exist - fail the plan phase
    # before anything is written or compared.
    seen_paths: set[Path] = set()
    collisions: list[Path] = []
    for planned_path, _content in plan:
        if planned_path in seen_paths:
            collisions.append(planned_path)
        seen_paths.add(planned_path)
    if collisions:
        for planned_path in collisions:
            print(
                f"loom: two generated outputs resolve to the same "
                f"file: {planned_path.relative_to(project_dir).as_posix()}",
                file=sys.stderr,
            )
        print(
            "Generation aborted: output path collision; nothing was written.",
            file=sys.stderr,
        )
        return 1

    # The opt-out notices print ahead of the mode's own output in
    # text mode; in json mode they ride the payload instead.
    notices: list[str] = []
    if properties_skipped:
        notices.append(
            f"Properties documents skipped ({len(properties_skipped)}): "
            + ", ".join(properties_skipped)
            + " - declared skip_properties_document"
        )
    if data_documents_skipped:
        notices.append(
            f"Merged data-test documents skipped "
            f"({len(data_documents_skipped)}): "
            + ", ".join(data_documents_skipped)
            + " - skip_properties_document skips the merged document "
            "(properties entry and generated data tests)"
        )
    if not json_mode:
        for notice in notices:
            print(notice)

    # Drift check: compare the plan against disk and write
    # nothing. This is the CI gate for "someone edited a generated
    # file" and "someone edited metadata but did not regenerate".
    if check_mode:
        drift: list[str] = []
        current = 0
        for path, content in sorted(plan):
            # POSIX separators on every platform: the report is read
            # by humans and CI logs, not by the filesystem.
            relative = path.relative_to(project_dir).as_posix()
            if not path.exists():
                drift.append(f"  missing: {relative}")
                continue
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                drift.append(f"  unreadable: {relative}")
                continue
            if existing == content:
                current += 1
                continue
            note = ""
            if path in create_only_paths:
                note = (
                    " (SCD2 create-only output: generation will not "
                    "update it; delete the file and regenerate to apply "
                    "the metadata)"
                )
            elif not _is_loom_generated_text(existing):
                note = " (not Loom-generated; generation would refuse without --force)"
            drift.append(f"  differs: {relative}{note}")
        # A Loom-headed SQL file for a selected model at a
        # location the current layout no longer plans is a stale
        # leftover from a layout change. Report it; never delete it.
        stale: list[str] = []
        planned_paths = {path for path, _content in plan}
        selected_names = {
            entry.data.get("name") for entry in selected_entries
        }
        for root_name in {model_root, snapshot_root}:
            root_dir = project_dir / root_name
            if not root_dir.is_dir():
                continue
            for candidate in sorted(root_dir.rglob("*.sql")):
                if candidate in planned_paths:
                    continue
                if candidate.stem not in selected_names:
                    continue
                try:
                    text = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if _is_loom_generated_text(text):
                    stale.append(
                        "  stale: "
                        + candidate.relative_to(project_dir).as_posix()
                        + f" (Loom-generated file for {candidate.stem} at "
                        "a previous location; remove it explicitly)"
                    )
        for leftover_name, leftover_path in properties_leftovers:
            stale.append(
                "  stale: "
                + leftover_path.relative_to(project_dir).as_posix()
                + f" (properties document no longer generated for "
                f"{leftover_name}: it declares skip_properties_document; "
                "remove it explicitly)"
            )
        if failed_models:
            print(
                f"loom: {failed_models} model(s) failed to render; "
                "drift cannot be verified for them.",
                file=sys.stderr,
            )
        drifted = bool(drift or stale or failed_models)
        message = ""
        if drifted:
            message = (
                f"Drift detected: {len(drift)} of {len(plan)} generated "
                f"file(s) are not current for dialect: {dialect_name}. "
                "Run `loom generate --write` to update them."
            )
            if stale:
                message += (
                    f" {len(stale)} stale file(s) remain at previous "
                    "locations; remove them explicitly."
                )
        if json_mode:
            print(
                json.dumps(
                    {
                        "command": "generate",
                        "mode": "check",
                        "project": project_config.get("name")
                        or Path(project_dir).name,
                        "dialect": dialect_name,
                        "ok": not drifted,
                        "current": current,
                        "total": len(plan),
                        "drift": [line.strip() for line in drift],
                        "stale": [line.strip() for line in stale],
                        "notices": notices,
                        "failed_models": failed_models,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1 if drifted else 0
        for line in drift:
            print(line)
        for line in stale:
            print(line)
        if drifted:
            print(red(message))
            return 1
        print(
            f"All {current} generated files are current for dialect: "
            f"{dialect_name}"
        )
        return 0

    # Classify every planned file against disk - would write
    # (absent), would change (present and different), unchanged
    # (present and identical). The preview reports this
    # classification as a forecast; a write run classifies the
    # same plan before writing and reports it as what actually
    # changed, so the two runs' counts agree by construction
    # (QOL FR symmetry). A differing file without the Loom header
    # is classified blocked: a write run would refuse it without
    # --force. Create-only files already on disk are not in the
    # plan; they are reported as their own line in both modes.
    def _classify_plan() -> tuple[dict[str, int], list[dict[str, str]], int]:
        files: list[dict[str, str]] = []
        counts = {"would_write": 0, "would_change": 0, "unchanged": 0}
        blocked = 0
        for path, content in sorted(plan):
            relative = path.relative_to(project_dir).as_posix()
            if not path.exists():
                action = "would_write"
            else:
                try:
                    existing: str | None = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    existing = None
                if existing == content:
                    action = "unchanged"
                elif (
                    existing is not None
                    and not _is_loom_generated_text(existing)
                    and not getattr(args, "force", False)
                ):
                    action = "blocked"
                else:
                    action = "would_change"
            if action == "blocked":
                blocked += 1
                counts["would_change"] += 1
            else:
                counts[action] += 1
            files.append({"path": relative, "action": action})
        return counts, files, blocked

    # Preview (the default mode): report the classification and
    # write nothing.
    if not write_mode:
        project_name = project_config.get("name") or Path(project_dir).name
        counts, files, blocked = _classify_plan()
        # The shadowed state (P5), forecast: pairs whose
        # generated file this run would write count alongside
        # pairs already on disk, so the preview warns before the
        # write leaves dbt unable to parse the project.
        shadowed = _shadowed_models(
            project_dir,
            entries,
            planned_paths={path for path, _content in plan},
        )
        if failed_models:
            print(
                f"loom: {failed_models} model(s) failed to render; "
                "the preview covers the models that did.",
                file=sys.stderr,
            )
        if json_mode:
            payload: dict[str, Any] = {
                "command": "generate",
                "mode": "dry-run",
                "mode_source": mode_source,
                "project": project_name,
                "dialect": dialect_name,
                "models": len(selected_entries),
                "counts": {**counts, "blocked": blocked},
                "create_only_current": create_only_current,
                "files": files,
                "notices": notices,
                "left_in_place": [
                    leftover_path.relative_to(project_dir).as_posix()
                    for _name, leftover_path in properties_leftovers
                ],
                "failed_models": failed_models,
                "apply_hint": apply_hint,
            }
            if shadowed:
                payload["shadowed_models"] = shadowed
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1 if failed_models else 0
        print(
            bold(f"Generate: {project_name} (dialect {dialect_name})")
            + f" - {mode_phrase('dry-run', mode_source)}"
        )
        print(
            f"Would write: {counts['would_write']} file(s) · "
            f"Would change: {counts['would_change']} file(s) · "
            f"Unchanged: {counts['unchanged']} file(s)"
        )
        if create_only_current:
            print(
                f"  ({create_only_current} create-only file(s) already "
                "on disk - left untouched)"
            )
        if verbose:
            labels = {
                "would_write": "would write",
                "would_change": "would change",
                "unchanged": "unchanged",
                "blocked": (
                    "blocked (not Loom-generated; a write run "
                    "would refuse without --force)"
                ),
            }
            for planned_file in files:
                print(
                    f"  {labels[planned_file['action']]}: "
                    f"{planned_file['path']}"
                )
        elif files:
            print("  (per-file detail: --verbose)")
        if blocked:
            print(
                f"  {blocked} planned file(s) are not Loom-generated; "
                "a write run would refuse without --force"
            )
        for leftover_name, leftover_path in properties_leftovers:
            print(
                "  left in place: "
                + leftover_path.relative_to(project_dir).as_posix()
                + f" (no longer generated for {leftover_name}: it declares "
                "skip_properties_document; remove it explicitly)"
            )
        if shadowed:
            print()
            for line in render_shadowed_section(
                shadowed, verbose=verbose, lead=_GENERATE_SHADOW_LEAD
            ):
                print(line)
        if apply_hint:
            print(f"Re-run with --write to apply: {apply_hint}")
        return 1 if failed_models else 0

    # Ownership preflight: an existing file at a planned output
    # path that does not carry the Loom provenance header - and whose
    # content differs from what would be written - is practitioner
    # work. Refuse to overwrite any of them, before writing anything,
    # unless --force was given.
    if not getattr(args, "force", False):
        conflicts: list[Path] = []
        for path, content in plan:
            if not path.exists():
                continue
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                conflicts.append(path)
                continue
            if existing != content and not _is_loom_generated_text(existing):
                conflicts.append(path)
        if conflicts:
            for path in conflicts:
                print(
                    f"loom: refusing to overwrite {path}: the existing file "
                    "is not Loom-generated. Move it, or re-run with --force "
                    "to replace it.",
                    file=sys.stderr,
                )
            print(
                f"Generation aborted: {len(conflicts)} existing file(s) "
                "would be overwritten; nothing was written.",
                file=sys.stderr,
            )
            return 1

    # What the write will actually change, classified against
    # disk before anything is written (the preview's own
    # classification, so the summary below is symmetric with the
    # preview's forecast rather than a restatement of the
    # in-scope totals).
    change_counts, _change_files, _change_blocked = _classify_plan()

    # Write phase
    for path, content in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    project_name = project_config.get("name") or Path(project_dir).name
    if failed_models:
        print(
            f"Generation incomplete: wrote {written} resource files, "
            f"{tests_written} data-test files, {properties_written} "
            f"properties entries and {unit_files_written} unit-test "
            f"files; {failed_models} models failed.",
            file=sys.stderr,
        )
        if json_mode:
            print(
                json.dumps(
                    {
                        "command": "generate",
                        "mode": "write",
                        "mode_source": mode_source,
                        "project": project_name,
                        "dialect": dialect_name,
                        "counts": {
                            "models": written,
                            "properties": properties_written,
                            "unit_test_files": unit_files_written,
                            "test_files": tests_written,
                            "create_only_current": create_only_current,
                        },
                        "file_changes": {
                            "written": change_counts["would_write"],
                            "changed": change_counts["would_change"],
                            "unchanged": change_counts["unchanged"],
                        },
                        "files": [
                            path.relative_to(project_dir).as_posix()
                            for path, _content in plan
                        ],
                        "notices": notices,
                        "failed_models": failed_models,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        return 1

    # The shadowed state (P5), as the write left it: detected
    # from disk now that the generated files exist, so a run
    # that completes the generate-before-remove window names it
    # instead of leaving the practitioner to discover it from
    # dbt's duplicate-name error.
    shadowed = _shadowed_models(project_dir, entries)

    # The summary leads with the counts in one line; the
    # per-file lists are emission detail and move behind
    # --verbose. The create-only split stays in the headline
    # segment it belongs to, so a run that adds a model never
    # reads as one that lost some.
    if json_mode:
        payload = {
            "command": "generate",
            "mode": "write",
            "mode_source": mode_source,
            "project": project_name,
            "dialect": dialect_name,
            "counts": {
                "models": written,
                "properties": properties_written,
                "unit_test_files": unit_files_written,
                "test_files": tests_written,
                "create_only_current": create_only_current,
            },
            "file_changes": {
                "written": change_counts["would_write"],
                "changed": change_counts["would_change"],
                "unchanged": change_counts["unchanged"],
            },
            "files": [
                path.relative_to(project_dir).as_posix()
                for path, _content in plan
            ],
            "notices": notices,
            "left_in_place": [
                leftover_path.relative_to(project_dir).as_posix()
                for _name, leftover_path in properties_leftovers
            ],
            "failed_models": failed_models,
        }
        if shadowed:
            payload["shadowed_models"] = shadowed
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(
        bold(f"Generate: {project_name} (dialect {dialect_name})")
        + f" - {mode_phrase('write', mode_source)}"
    )
    # The headline reports what actually changed, in the
    # preview's own classification (QOL FR symmetry: the old
    # headline restated the in-scope totals - "Wrote: 4 models"
    # on a run whose preview said "Would change: 1 file(s)").
    # The in-scope breakdown follows as the scope line, where
    # the create-only split still rides, so a run that adds a
    # model never reads as one that lost some.
    print(
        f"Wrote: {change_counts['would_write']} file(s) · "
        f"Changed: {change_counts['would_change']} file(s) · "
        f"Unchanged: {change_counts['unchanged']} file(s)"
    )
    segments = [f"{written} models in scope"]
    if create_only_current:
        segments[0] += (
            f" ({create_only_current} create-only file(s) already current)"
        )
    if properties_written:
        segments.append(f"properties for {properties_written} models")
    if unit_files_written:
        segments.append(
            f"{unit_files_written} unit-test file"
            f"{'' if unit_files_written == 1 else 's'}"
        )
    if tests_written:
        segments.append(
            f"{tests_written} test file{'' if tests_written == 1 else 's'}"
        )
    print("Scope: " + " · ".join(segments))
    if verbose:
        if unit_test_details:
            print(f"Unit-test files ({unit_files_written}):")
            for detail in unit_test_details:
                print(detail)
        if test_details:
            print(f"Test files ({tests_written}):")
            for detail in test_details:
                print(detail)
    elif unit_test_details or test_details:
        print("  (per-file detail: --verbose)")
    for leftover_name, leftover_path in properties_leftovers:
        print(
            "  left in place: "
            + leftover_path.relative_to(project_dir).as_posix()
            + f" (no longer generated for {leftover_name}: it declares "
            "skip_properties_document; remove it explicitly)"
        )
    if shadowed:
        print()
        for line in render_shadowed_section(
            shadowed, verbose=verbose, lead=_GENERATE_SHADOW_LEAD
        ):
            print(line)
    return 0


def _register_preview(
    project_dir: Path, report: dict[str, Any]
) -> dict[str, Any]:
    """The register preview: the registration files a write run
    would create, counted without writing. Mirrors
    ``write_draft_registrations``' gating exactly (one file per
    model carrying a drafted registration). Registrations are
    create-only - an existing file is never overwritten - so an
    existing file counts as unchanged and would_change is 0."""
    target_dir = Path(project_dir) / "local" / "registrations"
    files = []
    for model in report["models"]:
        if not model.get("draft_registration"):
            continue
        target = target_dir / f"{model['name']}.yml"
        files.append(
            {
                "model": model["name"],
                "file": f"local/registrations/{model['name']}.yml",
                "action": "unchanged" if target.exists() else "would_write",
            }
        )
    return {
        "would_write": sum(
            1 for entry in files if entry["action"] == "would_write"
        ),
        "would_change": 0,
        "unchanged": sum(
            1 for entry in files if entry["action"] == "unchanged"
        ),
        "files": files,
    }


def _convert_preview(
    project_dir: Path, report: dict[str, Any], *, include_registered: bool
) -> dict[str, Any]:
    """The convert preview: the draft files a write run would
    create, counted without writing. Mirrors ``write_drafts``'
    gating and file layout (a record and an instance document
    per drafted model, plus one synthesised pattern per
    generator the project has no pattern for). Drafts are
    create-only - an existing file is never overwritten, and a
    write run reports divergence instead of repairing it - so an
    existing file counts as unchanged and would_change is 0."""
    entries = validate_project(project_dir)
    patterns_by_generator = _project_patterns(project_dir, entries)
    all_pattern_names = {
        name
        for names in patterns_by_generator.values()
        for name in names
    }
    target_dir = Path(project_dir) / "local" / "drafts"
    files: list[dict[str, str]] = []
    synthesised: dict[str, str] = {}

    def classify(relative: str, kind: str) -> None:
        files.append(
            {
                "file": relative,
                "kind": kind,
                "action": (
                    "unchanged"
                    if (Path(project_dir) / relative).exists()
                    else "would_write"
                ),
            }
        )

    for model in report["models"]:
        source = _draft_sources(model)
        if source is None:
            continue
        if model.get("state") not in (
            "unknown_to_loom",
            "known_not_controlled",
        ):
            continue
        if (
            model["state"] == "known_not_controlled"
            and not include_registered
        ):
            continue
        generator = source["generator"]
        classify(f"local/drafts/{model['name']}.yml", "record")
        classify(f"local/drafts/instances/{model['name']}.yml", "instance")
        existing = sorted(patterns_by_generator.get(generator, set()))
        if not existing and generator not in synthesised:
            pattern_name = _synthesised_pattern_name(
                generator, all_pattern_names
            )
            synthesised[generator] = pattern_name
            all_pattern_names.add(pattern_name)
            classify(f"local/drafts/patterns/{pattern_name}.yml", "pattern")
    return {
        "would_write": sum(
            1 for entry in files if entry["action"] == "would_write"
        ),
        "would_change": 0,
        "unchanged": sum(
            1 for entry in files if entry["action"] == "unchanged"
        ),
        "files": files,
    }


def _print_preview_tail(
    preview: dict[str, Any], *, verbose: bool, apply_hint: str
) -> None:
    """The uniform end of a register/convert preview: optional
    per-file detail, the three counts in the shared shape, and
    the apply hint as the last line."""
    files = preview["files"]
    if verbose:
        for entry in files:
            label = (
                "would write" if entry["action"] == "would_write" else "unchanged"
            )
            print(f"  {label}: {entry['file']}")
    elif files:
        print("  (per-file detail: --verbose)")
    print(
        f"Would write: {preview['would_write']} file(s) · "
        f"Would change: {preview['would_change']} file(s) · "
        f"Unchanged: {preview['unchanged']} file(s)"
    )
    print(f"Re-run with --write to apply: {apply_hint}")


def _scoped_name_list(
    names: list[str], *, verbose: bool
) -> tuple[str, list[str]]:
    """Present a scoped-validate model list as (inline,
    following lines): while the list fits the shared fold
    limit the names ride inside the sentence, in
    parentheses, as they always have; past it the sentence
    carries the count alone and the list follows in the
    shared fold grammar (first ten, the folded count, and
    the way to unfold), so a wide selection never prints as
    one unwrapped line. The JSON form is untouched and
    always carries the full lists."""
    if len(names) <= _FOLD_LIMIT:
        return (f" ({', '.join(names)})" if names else ""), []
    return "", _folded_name_lines(names, verbose=verbose)


def _validated_summary(entries: int, documents: int) -> str:
    """The closing line of a validate run. What was validated
    is named as the project's own objects - the entries and
    the authoring documents carrying them - rather than as
    "inventory entries", the wording the search redesign
    retired in favour of naming project objects; the plurals
    are right at one. Both validate paths pass the count of
    documents carrying the validated entries, so the figure
    means the same thing scoped and unscoped."""
    entry_noun = "entry" if entries == 1 else "entries"
    document_noun = "document" if documents == 1 else "documents"
    return (
        f"Validated {entries} {entry_noun} in "
        f"{documents} authoring {document_noun}."
    )


def _cmd_validate_scoped(args: argparse.Namespace) -> int:
    """`loom validate --select/--exclude`: scoped validation
    (plan 7.6). The selector is evaluated over the inventory
    model records - the project's authoring documents plus,
    when target/manifest.json exists, the manifest's project
    models, so an unmanaged model can be selected and reported
    as having no authoring documents. The manifest is read
    without the freshness guard: unscoped validate never reads
    one, and staleness does not change which documents a model
    lives in."""
    project_dir = args.project_dir
    documents = load_project_documents(project_dir)
    records = records_from_corpus(
        corpus_from_entries(project_dir, documents.entries)
    )
    known = {record.name for record in records}
    if (project_dir / "target" / "manifest.json").is_file():
        try:
            _manifest_file, document = read_manifest(
                project_dir, None, sections=("nodes",)
            )
        except LoomError:
            document = None
        if document is not None:
            for record in records_from_manifest_document(document):
                if (
                    record.kind == "model"
                    and record.provenance == "project"
                    and record.name not in known
                ):
                    records.append(replace(record, status="unmanaged"))
                    known.add(record.name)
    # The pool is the model records only (plan 7.6: the
    # selector is evaluated over inventory model records).
    # A tier-2 record whose identity is its bare name - the
    # sources this corpus carries - is not in the pool, so
    # naming one errors as an unmatched model selection
    # instead of reporting it as a selected model with no
    # authoring documents.
    selection = _selection_for(
        args,
        records,
        pool={
            record.name for record in records if record.kind == "model"
        },
    )
    result = documents.validate(set(selection.names))
    for warning in unit_test_warnings(result.entries):
        print(f"loom: warning: {warning}", file=sys.stderr)
    for warning in contract_warnings(result.entries):
        print(f"loom: warning: {warning}", file=sys.stderr)
    for warning in staging_macro_warnings(result.entries):
        print(f"loom: warning: {warning}", file=sys.stderr)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "command": "validate",
                    "entries": len(result.entries),
                    "ok": True,
                    "scoped": True,
                    "selection": {
                        "selected": result.selected,
                        "documents": result.documents,
                        "extra_models": result.extra_models,
                        "without_documents": result.without_documents,
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if not result.documents:
        suffix, folded = _scoped_name_list(
            result.selected, verbose=args.verbose
        )
        print(
            "Scoped validation: the selection matched "
            f"{len(result.selected)} model(s){suffix}, and none of "
            "them has authoring documents, so there was nothing "
            "to validate."
        )
        for line in folded:
            print(line)
        return 0
    suffix, folded = _scoped_name_list(
        result.selected, verbose=args.verbose
    )
    print(
        f"Scoped validation: {len(result.documents)} authoring "
        f"document(s) included for {len(result.selected)} selected "
        f"model(s){suffix}."
    )
    for line in folded:
        print(line)
    if result.extra_models:
        suffix, folded = _scoped_name_list(
            result.extra_models, verbose=args.verbose
        )
        print(
            "The included documents also contain "
            f"{len(result.extra_models)} model(s) beyond the selection"
            f"{suffix}, validated whole."
        )
        for line in folded:
            print(line)
    if result.without_documents:
        if len(result.without_documents) <= _FOLD_LIMIT:
            print(
                "Selected models without authoring documents (nothing "
                f"to validate for them): "
                f"{', '.join(result.without_documents)}."
            )
        else:
            print(
                "Selected models without authoring documents (nothing "
                "to validate for them):"
            )
            for line in _folded_name_lines(
                result.without_documents, verbose=args.verbose
            ):
                print(line)
    if args.verbose:
        for entry in result.entries:
            name = entry.data.get("name")
            if isinstance(name, str):
                print(f"  {name} ({entry.path})")
            else:
                print(f"  {entry.path}")
    print(
        _validated_summary(
            len(result.entries),
            len({entry.path for entry in result.entries}),
        )
    )
    return 0


# --- Search ------------------------------------------------
#
# Search is the selector engine over the inventory corpus, presented:
# no query is the project overview; a query renders the faceted
# table (or the detail view for a single hit); --fields chooses
# the JSON record fields; --count-only and --names-only are the
# scripting forms. The legacy nine-flag filter interface is
# retired (a clean break): selector arguments are the
# one interface.

_SEARCH_FIELDS = frozenset(
    {
        "name",
        "kind",
        "status",
        "unique_id",
        "path",
        "metadata_path",
        "package",
        "provenance",
        "generator",
        "pattern",
        "paradigm",
        "classification",
        "owner",
        "tags",
        "description",
        "description_provenance",
        "documented",
        "columns_documented",
        "columns_total",
        "keys",
        "inputs",
        "source_inputs",
        "columns",
        "columns_partial",
        "columns_source",
        "upstream",
        "downstream",
        "graph_declared_only",
        "has_owner",
        "has_tests",
        "misplaced",
        "duplicate",
        "arguments",
        "used_by",
        "output_path",
        "matched",
        "missing",
        "band",
    }
)


def _parse_search_fields(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    fields = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [field for field in fields if field not in _SEARCH_FIELDS]
    if unknown:
        raise LoomError(
            f"search: unknown --fields value(s): {', '.join(unknown)}; "
            f"available fields: {', '.join(sorted(_SEARCH_FIELDS))}"
        )
    return fields


def _manifest_age(generated_at: Any) -> str | None:
    if generated_at is None:
        return None
    try:
        seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
    except TypeError:
        return None
    if seconds < 60:
        return "under a minute"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _search_output_paths(project_dir: Path) -> dict[str, str]:
    """Expected generated output paths for generated models, so
    search results name where a model's file belongs (the one
    layout resolver, as before). Best-effort: a project whose
    metadata does not load still searches over the corpus, just
    without this field."""
    try:
        entries, _duplicates = _load_project(
            project_dir,
            reject_duplicate_names=False,
            validate_relationships=False,
        )
        model_root = _output_root(_project_config(project_dir), "model")
    except LoomError:
        return {}
    paths: dict[str, str] = {}
    for entry in entries:
        if not entry.is_generated:
            continue
        name = entry.data.get("name")
        if not isinstance(name, str):
            continue
        try:
            paths[name] = expected_model_path(model_root, entry.data)
        except LoomError:
            continue
    return paths


def _search_records_by_name(
    corpus: Any, output_paths: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """The rich corpus record dicts behind the engine's
    identities: models win a name collision with a tier-2
    object, mirroring the engine's record order. Macros key by
    their package-qualified identity (their bare names are not
    unique across packages)."""
    records: dict[str, dict[str, Any]] = {}
    for obj in corpus.objects:
        records[obj.identity] = obj.as_dict()
    for model in corpus.models:
        record = model.as_dict()
        record["provenance"] = (
            "package"
            if model.package is not None
            and corpus.project_name is not None
            and model.package != corpus.project_name
            else "project"
        )
        if model.name in output_paths:
            record["output_path"] = output_paths[model.name]
        records[model.name] = record
    return records


def _sorted_counts(counter: Counter) -> dict[str, int]:
    return {
        key: counter[key]
        for key in sorted(counter, key=lambda k: (-counter[k], k.casefold()))
    }


def _search_facets(items: list[dict[str, Any]]) -> dict[str, Any]:
    def counts(key: str) -> dict[str, int]:
        counter: Counter = Counter()
        for item in items:
            value = item.get(key)
            if value:
                counter[str(value)] += 1
        return _sorted_counts(counter)

    owner_counter: Counter = Counter()
    for item in items:
        if "owner" in item:
            owner_counter[item.get("owner") or "(none)"] += 1
    return {
        "status": counts("status"),
        "kind": counts("kind"),
        "generator": counts("generator"),
        "owner": _sorted_counts(owner_counter),
        "paradigm": counts("paradigm"),
        "provenance": counts("provenance"),
    }


def _search_overview(corpus: Any) -> dict[str, Any]:
    """The no-query overview facts (plan 8.1), computed from
    the corpus the search already built."""
    models = list(corpus.models)
    objects = list(corpus.objects)
    status_order = ("generated", "registered", "unmanaged", "orphaned")
    status_counter: Counter = Counter(model.status for model in models)
    by_status = {
        status: status_counter[status]
        for status in status_order
        if status_counter.get(status)
    }
    generator_counter: Counter = Counter(
        model.generator for model in models if model.generator
    )
    paradigm_counter: Counter = Counter(
        model.paradigm for model in models if model.paradigm
    )
    owner_counter: Counter = Counter(
        model.owner for model in models if model.owner
    )
    top_owners = [
        [name, count]
        for name, count in sorted(
            owner_counter.items(), key=lambda kv: (-kv[1], kv[0].casefold())
        )[:5]
    ]
    unowned = sum(1 for model in models if not model.has_owner)
    macros = [obj for obj in objects if obj.kind == "macro"]
    macro_packages: Counter = Counter(
        obj.package
        for obj in macros
        if obj.provenance == "package" and obj.package
    )
    kind_counter: Counter = Counter(obj.kind for obj in objects)
    suggestions: list[dict[str, Any]] = []

    def suggest(query: str, count: int, singular: str, plural: str) -> None:
        # The label is a counted noun phrase: at one, the
        # singular is the truthful form ("1 orphaned model",
        # not "1 orphaned models").
        if count:
            suggestions.append(
                {
                    "query": query,
                    "count": count,
                    "label": singular if count == 1 else plural,
                }
            )

    suggest(
        "is:unmanaged",
        by_status.get("unmanaged", 0),
        "unmanaged model",
        "unmanaged models",
    )
    suggest(
        "is:orphaned",
        by_status.get("orphaned", 0),
        "orphaned model",
        "orphaned models",
    )
    suggest(
        "has:no-owner", unowned, "model with no owner", "models with no owner"
    )
    if corpus.mode == "full":
        # Test presence is a manifest fact; in metadata-only
        # mode it is unknowable and must not be implied.
        suggest(
            "has:no-tests",
            sum(1 for model in models if not model.has_tests),
            "model with no tests",
            "models with no tests",
        )
    suggest(
        "has:no-description",
        sum(1 for model in models if not model.documented),
        "model with no description",
        "models with no description",
    )
    suggest(
        "has:undocumented-columns",
        sum(
            1
            for model in models
            if model.columns_total > 0
            and model.columns_documented < model.columns_total
        ),
        "model with undocumented columns",
        "models with undocumented columns",
    )
    return {
        "project": corpus.project_name,
        "total_objects": len(models) + len(objects),
        "models": {"total": len(models), "by_status": by_status},
        "by_generator": _sorted_counts(generator_counter),
        "by_paradigm": _sorted_counts(paradigm_counter),
        "owners": {"top": top_owners, "unowned": unowned},
        "documentation": {
            "models_documented": sum(
                1 for model in models if model.documented
            ),
            "models_total": len(models),
            "columns_documented": sum(
                model.columns_documented for model in models
            ),
            "columns_total": sum(model.columns_total for model in models),
        },
        "macros": {
            "total": len(macros),
            "project": sum(
                1 for obj in macros if obj.provenance != "package"
            ),
            "package": sum(
                1 for obj in macros if obj.provenance == "package"
            ),
            "by_package": _sorted_counts(macro_packages),
        },
        "objects_by_kind": _sorted_counts(kind_counter),
        "suggested_queries": suggestions,
    }


def _cmd_search_similar(
    args: argparse.Namespace,
    corpus: Any,
    records: list[Any],
    corpus_info: dict[str, Any],
    fields: list[str] | None,
) -> int:
    """Search by example (plan 7.7): ``--similar-to <model>``
    expands the named model's fingerprint and ranks every
    other model against it through the same weighted overlap
    as shape banding, with a score and a shares / differs
    account per result. The named model is the reference
    header, never a result. The payload carries the rich
    corpus record dicts, as the engine search does."""
    reference, target, ranked, omitted = similar_to(
        records, args.similar_to
    )
    by_name = _search_records_by_name(
        corpus, _search_output_paths(args.project_dir)
    )

    def record_for(item: Any) -> dict[str, Any]:
        record = dict(
            by_name.get(item.name) or {"name": item.name, "kind": "model"}
        )
        record.update(
            {
                "matched": list(item.shares),
                "band": "similar",
                "similarity": item.as_dict(),
            }
        )
        return record

    results = [record_for(item) for item in ranked]
    payload: dict[str, Any] = {
        "command": "search",
        "query": f"--similar-to {reference.name}",
        "corpus": corpus_info,
        "banded": True,
        "similar_to": {
            "name": reference.name,
            "fingerprint": target.as_dict(),
            "omitted_no_overlap": omitted,
        },
        "facets": _search_facets(results),
        "results": results,
        "close_matches": [],
    }
    if args.format == "json":
        if fields is not None:
            payload = {
                **payload,
                "results": [
                    {
                        key: item[key]
                        for key in fields
                        if key in item
                    }
                    for item in results
                ],
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_search_engine_text(payload, verbose=args.verbose))
    return 0


def _asks_unmanaged(query_texts: list[str]) -> bool:
    """Whether any query branch carries an ``is:unmanaged``
    criterion, read through the selection parser itself. A
    named-selector reference does not unfold here - the
    metadata-only restatement guards the direct form."""
    for text in query_texts:
        try:
            criteria = parse_branch(text)
        except LoomError:
            continue
        for criterion in criteria:
            if (
                criterion.key == "is"
                and (criterion.value or "").casefold() == "unmanaged"
            ):
                return True
    return False


def _cmd_search_engine(args: argparse.Namespace) -> int:
    """Search on the selector engine over the inventory corpus:
    positional selector arguments plus any --select branches
    (equivalent forms), --exclude subtracting. The
    corpus is built without the freshness guard - search is a
    read, and the payload labels the corpus mode rather than
    refusing to answer over a stale manifest."""
    corpus = build_inventory(args.project_dir, check_freshness=False)
    records = records_from_corpus(corpus)
    query_texts = list(args.query or []) + list(args.select or [])
    exclude_texts = list(args.exclude or [])
    fields = _parse_search_fields(args.fields)
    corpus_info = {
        "mode": corpus.mode,
        "project": corpus.project_name,
        "manifest_generated_at": (
            corpus.manifest_generated_at.isoformat()
            if corpus.manifest_generated_at is not None
            else None
        ),
        "manifest_age": _manifest_age(corpus.manifest_generated_at),
    }
    if args.similar_to is not None:
        if (
            query_texts
            or exclude_texts
            or args.count_only
            or args.names_only
        ):
            raise LoomError(
                "--similar-to cannot be combined with selector "
                "arguments, --select, --exclude, --count-only "
                "or --names-only: it expands the named model's "
                "own shape (keys, inputs, output columns, "
                "generator family) into the comparison; run it "
                "on its own, for example "
                "'loom search --similar-to hub_customer'"
            )
        return _cmd_search_similar(
            args, corpus, records, corpus_info, fields
        )
    if not query_texts and not exclude_texts and not (
        args.count_only or args.names_only
    ):
        payload: dict[str, Any] = {
            "command": "search",
            "query": "",
            "corpus": corpus_info,
            "overview": _search_overview(corpus),
        }
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(render_search_engine_text(payload, verbose=args.verbose))
        return 0
    result = evaluate_selectors(
        records,
        query_texts,
        exclude_texts,
        mode="search",
        named=_named_selectors_for(
            args.project_dir, query_texts + exclude_texts
        ),
    )
    by_name = _search_records_by_name(
        corpus, _search_output_paths(args.project_dir)
    )

    def record_for(
        name: str, extra: dict[str, Any]
    ) -> dict[str, Any]:
        record = dict(by_name.get(name) or {"name": name, "kind": "model"})
        record.update(extra)
        return record

    results = [
        record_for(name, {"matched": result.full.get(name, []), "band": "full"})
        for name in result.names
    ]
    close = [
        record_for(
            match.name,
            {
                "matched": match.satisfied,
                "missing": match.missing,
                "band": "close",
                "similarity": {
                    "score": round(match.score, 6),
                    "gate": match.gate,
                    "shares": match.shares,
                    "differs": match.differs,
                },
            },
        )
        for match in result.close
    ]
    if args.count_only:
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "command": "search",
                        "count": len(results) + len(close),
                        "full": len(results),
                        "close": len(close),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif result.banded:
            # Under banding the text form distinguishes the
            # two counts with the headline's own labels, as
            # the JSON form already does; a single merged
            # number would read as a strict match count.
            print(
                f"{len(results)} full matches, "
                f"{len(close)} close matches"
            )
        else:
            print(len(results) + len(close))
        return 0
    if args.names_only:
        # Result identities, not display names: a macro's
        # identity is package-qualified, so two same-name
        # macros print as two distinct lines.
        names = list(result.names) + [match.name for match in result.close]
        if args.format == "json":
            print(
                json.dumps(
                    {"command": "search", "names": names},
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif names:
            print("\n".join(names))
        return 0
    payload = {
        "command": "search",
        "query": " ".join(query_texts),
        "corpus": corpus_info,
        "banded": result.banded,
        "facets": _search_facets(results + close),
        "results": results,
        "close_matches": close,
    }
    if not result.names and not result.close:
        payload["diagnosis"] = [
            {
                "branch": diagnosis.branch,
                "culprit": diagnosis.culprit,
                "facets": diagnosis.facets,
                "suggestions": diagnosis.suggestions,
            }
            for diagnosis in (
                diagnose_empty_branch(records, text)
                for text in dict.fromkeys(query_texts)
            )
        ]
        if corpus.mode == "metadata-only" and _asks_unmanaged(
            query_texts
        ):
            # The empty set here is a property of the mode,
            # not of the project: with no manifest read, no
            # model can carry the unmanaged status at all.
            # The renderer restates that on the zero-result
            # path so the empty set is never read as a finding.
            payload["unmanaged_unknowable"] = True
    if args.format == "json":
        if fields is not None:
            payload = {
                **payload,
                "results": [
                    {
                        key: item[key]
                        for key in fields
                        if key in item
                    }
                    for item in results
                ],
                "close_matches": [
                    {
                        key: item[key]
                        for key in fields
                        if key in item
                    }
                    for item in close
                ],
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_search_engine_text(payload, verbose=args.verbose))
    return 0


def _prove_selection(
    args: argparse.Namespace, payload: dict[str, Any]
) -> dict[str, Any]:
    """Narrow a prove payload to the selected subjects. The
    pool is the stage's own subjects - the verdict models plus
    the candidate-only non-subjects - and the records the
    selectors evaluate over come from the candidate manifest,
    whose graph the graph forms traverse."""
    pool = {model["name"] for model in payload.get("models") or []}
    pool |= set(payload.get("non_subjects") or [])
    records = []
    candidate_path = (
        args.project_dir / EQUIVALENCE_DIR / CANDIDATE_MANIFEST_NAME
    )
    if candidate_path.is_file():
        try:
            document = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict):
            records = records_from_manifest_document(document)
    selection = _selection_for(args, records, pool=pool)
    keep = set(selection.names)
    models = [
        model for model in payload.get("models") or [] if model["name"] in keep
    ]
    proven = sum(1 for model in models if model["verdict"] == "proven")
    return {
        **payload,
        "models": models,
        "non_subjects": [
            name for name in payload.get("non_subjects") or [] if name in keep
        ],
        "summary": {
            "subjects": len(models),
            "proven": proven,
            "inconclusive": len(models) - proven,
        },
        "selection": {
            "select": list(args.select or []),
            "exclude": list(args.exclude or []),
            "matched": len(keep & pool),
        },
    }


def _ensure_utf8_streams() -> None:
    """Make the process's text output encoding-safe.

    Windows consoles default to a legacy code page (cp1252),
    which cannot encode parts of Loom's report vocabulary - the
    scaffold summary table's and the readiness transitions' `→`
    among them - so printing a report crashed with
    UnicodeEncodeError AFTER the command's writes had succeeded,
    and an automated consumer read a completed run as a failure
    (finding Q1). Where stdout/stderr are text streams whose
    encoding is not UTF-8, reconfigure them to UTF-8 with
    errors="replace": the report vocabulary encodes exactly, and
    anything still unencodable (a lone surrogate echoed from a
    filesystem name, say) degrades to a replacement character
    rather than an exception. Streams that cannot be reconfigured
    (test capture, embedded interpreters) are left untouched: a
    failure here degrades to the previous behaviour, never to a
    new crash, so every step is guarded.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            encoding = getattr(stream, "encoding", None)
            if not isinstance(encoding, str):
                continue
            if encoding.replace("_", "-").casefold() in ("utf-8", "utf8"):
                continue
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is None:
                continue
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            continue


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_streams()
    argv = sys.argv[1:] if argv is None else argv
    args = _parser().parse_args(argv)
    # The one colour decision per run: text output to a terminal
    # colours; json and pipes never do.
    set_colour_enabled(
        colour_enabled(sys.stdout)
        and getattr(args, "format", "text") != "json"
    )
    # The one width decision per run, beside the colour
    # decision: text output to a terminal lets search's
    # tables, facet line and overview census lines adapt to
    # the terminal width; json and pipes never adapt, so
    # piped output stays byte-identical.
    set_terminal_width(
        terminal_width(sys.stdout)
        if getattr(args, "format", "text") != "json"
        else None
    )
    try:
        # Refuse every command when the generator wiring is
        # incomplete, before any project work happens.
        _check_renderer_wiring()
        if args.command == "validate":
            if getattr(args, "select", None) or getattr(args, "exclude", None):
                return _cmd_validate_scoped(args)
            entries = validate_project(args.project_dir)
            if not entries:
                # A bare
                # "Validated 0 entries ..." line reads as success
                # in a directory that simply has no Loom metadata
                # (wrong directory, metadata not yet written). Warn
                # on stderr; the exit code stays 0 - an empty-but-
                # valid project is a legitimate intermediate state.
                print(
                    f"loom: warning: no Loom metadata found under "
                    f"{args.project_dir}",
                    file=sys.stderr,
                )
            for warning in unit_test_warnings(entries):
                print(f"loom: warning: {warning}", file=sys.stderr)
            for warning in contract_warnings(entries):
                print(f"loom: warning: {warning}", file=sys.stderr)
            for warning in staging_macro_warnings(entries):
                print(f"loom: warning: {warning}", file=sys.stderr)
            if args.format == "json":
                print(
                    json.dumps(
                        {
                            "command": "validate",
                            "entries": len(entries),
                            "ok": True,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0
            if args.verbose:
                for entry in entries:
                    name = entry.data.get("name")
                    if isinstance(name, str):
                        print(f"  {name} ({entry.path})")
                    else:
                        print(f"  {entry.path}")
            print(
                _validated_summary(
                    len(entries), len({entry.path for entry in entries})
                )
            )
            return 0

        if args.command == "create-new":
            return _cmd_create_new(args)

        if args.command == "coverage":
            # The shadowed state first (P5): while a generated
            # model and a hand-written model share a name, dbt
            # cannot parse the project, so the manifest can
            # never be refreshed and the normal path can only
            # prescribe the `dbt parse` that cannot succeed.
            # Detection is filesystem-only (no manifest read),
            # and the report names the state and the way out
            # instead. Any shadow blocks coverage project-wide:
            # the unrefreshable manifest is a project fact, not
            # a per-model one, so --select does not narrow this.
            shadowed = _project_shadowed_models(args.project_dir)
            if shadowed:
                if args.format == "json":
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "state": "shadowed",
                                "shadowed_models": shadowed,
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                else:
                    print(
                        render_coverage_shadowed_text(
                            shadowed, verbose=args.verbose
                        )
                    )
                return 1
            only_models = None
            if getattr(args, "select", None) or getattr(args, "exclude", None):
                corpus = build_inventory(args.project_dir, args.manifest)
                records = records_from_corpus(corpus)
                selection = _selection_for(
                    args,
                    records,
                    pool={
                        record.name
                        for record in records
                        if record.kind == "model"
                    },
                )
                only_models = set(selection.names)
            coverage = check_inventory_coverage(
                args.project_dir, args.manifest, only_models=only_models
            )
            payload = coverage.as_dict()
            if only_models is not None:
                payload = {**payload, "selection": {"matched": len(only_models)}}
            if args.format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(render_coverage_text(payload, verbose=args.verbose))
            return 0 if coverage.ok else 1

        if args.command == "prove":
            payload = static_stage_report(args.project_dir)
            if getattr(args, "select", None) or getattr(args, "exclude", None):
                payload = _prove_selection(args, payload)
            if args.format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(render_prove_text(payload, verbose=args.verbose))
            return 0

        if args.command == "check":
            # The shadowed state first (P5, as in coverage):
            # check's gate renders from the coverage payload,
            # which needs a manifest dbt cannot refresh while a
            # generated model and a hand-written model share a
            # name - the normal path can only surface the
            # stale-manifest error prescribing the `dbt parse`
            # that cannot succeed. Detection is filesystem-only
            # (no manifest read), and the report names the state
            # and the way out instead. Any shadow blocks check
            # project-wide: the unrefreshable manifest is a
            # project fact, not a per-model one, so --select
            # does not narrow this.
            shadowed = _project_shadowed_models(args.project_dir)
            if shadowed:
                if args.format == "json":
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "state": "shadowed",
                                "shadowed_models": shadowed,
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                else:
                    print(
                        render_check_shadowed_text(
                            shadowed, verbose=args.verbose
                        )
                    )
                return 1
            only_models: set[str] | None = None
            if getattr(args, "select", None) or getattr(args, "exclude", None):
                corpus = build_inventory(args.project_dir, args.manifest)
                records = records_from_corpus(corpus)
                selection = _selection_for(
                    args,
                    records,
                    pool={
                        record.name
                        for record in records
                        if record.kind == "model"
                    },
                )
                only_models = set(selection.names)
            failure: LoomError | None = None
            try:
                check_project(
                    args.project_dir,
                    args.manifest,
                    strict=args.strict,
                    only_models=only_models,
                )
            except LoomError as error:
                failure = error
            if failure is None:
                # The properties-opt-out safety net:
                # warn-not-fail when an opted-out model has no
                # practitioner description in the manifest.
                try:
                    entries = validate_project(args.project_dir)
                    if only_models is not None:
                        entries = [
                            entry
                            for entry in entries
                            if entry.data.get("name") in only_models
                        ]
                    _manifest_file, document = read_manifest(
                        args.project_dir,
                        args.manifest,
                        sections=("nodes",),
                    )
                except LoomError:
                    entries, document = [], None
                warnings = properties_optout_warnings(entries, document)
                # Tests carriage: exact duplicates of
                # generator-emitted tests, and declarations the
                # manifest does not attest - warnings here;
                # --strict promotes the attestation rule inside
                # check_project, so it never reaches this point
                # as a mere warning under --strict.
                warnings += duplicate_test_warnings(entries)
                warnings += declared_test_problems(entries, document)
                for warning in warnings:
                    print(f"loom: warning: {warning}", file=sys.stderr)
                if args.format == "json":
                    payload = check_inventory_coverage(
                        args.project_dir, args.manifest, only_models=only_models
                    ).as_dict()
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                else:
                    print("Inventory coverage is current.")
                return 0
            # The gate's failure is
            # rendered here, at the CLI point, from the same
            # coverage payload `coverage` renders - a short
            # summary instead of the coverage error's full name
            # lists (one 24k-character line on a fresh 625-model
            # project). `check_project`'s raised LoomError remains
            # the library behaviour and is what surfaces whenever
            # the failure is not a coverage failure the payload
            # can summarise (invalid metadata, relationship
            # errors): the counts in the gate form come from the
            # payload, so they agree with `coverage` by
            # construction.
            try:
                coverage = check_inventory_coverage(
                    args.project_dir, args.manifest, only_models=only_models
                )
            except LoomError:
                raise failure
            if coverage.ok:
                raise failure
            if args.format == "json":
                print(
                    json.dumps(
                        coverage.as_dict(), indent=2, ensure_ascii=False
                    )
                )
            else:
                print(render_check_gate_text(coverage.as_dict()))
            return 1

        if args.command == "generate":
            return _cmd_generate(args, apply_hint=_apply_hint(argv))

        if args.command == "search":
            return _cmd_search_engine(args)

        if args.command == "plan":
            plan = migration_plan(
                args.project_dir,
                args.from_model,
                args.to_model,
                args.manifest,
            )
            if args.format == "json":
                print(json.dumps(plan, indent=2, ensure_ascii=False))
            else:
                print(render_migration_plan_text(plan, verbose=args.verbose))
            return 0

        # `register` and `convert` are two commands over the
        # one assessment engine. Each presents its own half of the
        # report, and --write writes only its own drafts -
        # registrations for register, the unified drafts for
        # convert; without --write (and without LOOM_WRITE) the
        # run is a preview: the same report, a mode stamp, the
        # draft-file counts and the apply hint. The JSON payload
        # is the shared report either way (its summary carries
        # the readiness distribution), plus the acting command's
        # own `*_written` list on a write run or `drafts_preview`
        # on a preview run.
        if args.command == "register":
            only_models = None
            selection_info = None
            if args.select or args.exclude:
                corpus = build_inventory(args.project_dir, args.manifest)
                records = records_from_corpus(corpus)
                selection = _selection_for(
                    args,
                    records,
                    pool={
                        record.name
                        for record in records
                        if record.kind == "model"
                    },
                )
                only_models = set(selection.names)
                selection_info = {
                    "select": list(args.select or []),
                    "exclude": list(args.exclude or []),
                    "matched_models": len(only_models),
                }
            report = migration_assessment(
                args.project_dir, args.manifest, only_models=only_models
            )
            if selection_info is not None:
                report = {**report, "selection": selection_info}
            write_mode, mode_source = _write_mode(args)
            report = {
                **report,
                "mode": "write" if write_mode else "dry-run",
                "mode_source": mode_source,
            }
            preview = None
            if write_mode:
                written = write_draft_registrations(args.project_dir, report)
                report = {**report, "registrations_written": written}
                for path in written:
                    print(f"Wrote registration draft: {path}", file=sys.stderr)
            else:
                preview = _register_preview(args.project_dir, report)
                report = {
                    **report,
                    "drafts_preview": preview,
                    "apply_hint": _apply_hint(argv),
                }
            if args.format == "json":
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print(render_register_text(report, verbose=args.verbose))
                if preview is not None:
                    _print_preview_tail(
                        preview,
                        verbose=args.verbose,
                        apply_hint=_apply_hint(argv),
                    )
            return 0

        if args.command == "convert":
            only_models = None
            selection_info = None
            if args.select or args.exclude:
                corpus = build_inventory(args.project_dir, args.manifest)
                records = records_from_corpus(corpus)
                selection = _selection_for(
                    args,
                    records,
                    pool={
                        record.name
                        for record in records
                        if record.kind == "model"
                    },
                )
                only_models = set(selection.names)
                selection_info = {
                    "select": list(args.select or []),
                    "exclude": list(args.exclude or []),
                    "matched_models": len(only_models),
                }
            report = migration_assessment(
                args.project_dir, args.manifest, only_models=only_models
            )
            if selection_info is not None:
                report = {**report, "selection": selection_info}
            write_mode, mode_source = _write_mode(args)
            report = {
                **report,
                "mode": "write" if write_mode else "dry-run",
                "mode_source": mode_source,
            }
            preview = None
            if write_mode:
                # One draft class: convert's --write
                # writes the unified drafts (fit-derived and
                # authoring alike); register's registrations-only
                # purity is unchanged.
                written_drafts = write_drafts(
                    args.project_dir,
                    report,
                    include_registered=args.include_registered,
                )
                report = {
                    **report,
                    "drafts_written": written_drafts,
                }
                # The written-drafts account carries the
                # provenance annotation on a build-produced
                # manifest, so a pipeline consumer sees it at the
                # point of writing. Annotate, do not gate: the
                # drafts are written exactly as on any other
                # manifest, and a parse-produced report carries
                # no such key.
                if report.get("manifest_provenance") == "build-produced":
                    report["drafts_provenance"] = "build-produced"
                for path in written_drafts:
                    print(f"Wrote draft: {path}", file=sys.stderr)
                for entry in report.get("draft_divergences") or []:
                    print(
                        "Existing draft differs from today's draft "
                        f"(kept): {entry['file']}",
                        file=sys.stderr,
                    )
            else:
                preview = _convert_preview(
                    args.project_dir,
                    report,
                    include_registered=args.include_registered,
                )
                report = {
                    **report,
                    "drafts_preview": preview,
                    "apply_hint": _apply_hint(argv),
                }
            if args.format == "json":
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print(render_convert_text(report, verbose=args.verbose))
                if preview is not None:
                    _print_preview_tail(
                        preview,
                        verbose=args.verbose,
                        apply_hint=_apply_hint(argv),
                    )
            return 0

        if args.command == "inspect-sources":
            result = inspect_sources(
                args.project_dir,
                args.database,
                args.manifest,
                source=args.source,
                table=args.table,
            )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(render_inspect_text(result, verbose=args.verbose))
            return 0

        if args.command == "snapshot-sources":
            write_mode, mode_source = _write_mode(args)
            if not write_mode:
                # Preview: report the baseline that would be
                # captured, from the same inspection the capture
                # itself runs. Nothing is written.
                inspection = inspect_sources(
                    args.project_dir, args.database, args.manifest
                )
                tables = inspection["sources"]
                source_names = {entry["source"] for entry in tables}
                output = args.output
                candidate = (
                    output
                    if output.is_absolute()
                    else Path(args.project_dir) / output
                )
                exists = candidate.exists()
                if args.format == "json":
                    print(
                        json.dumps(
                            {
                                "command": "snapshot-sources",
                                "mode": "dry-run",
                                "mode_source": mode_source,
                                "output": str(candidate),
                                "exists": exists,
                                "sources": len(source_names),
                                "tables": len(tables),
                                "apply_hint": _apply_hint(argv),
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                    return 0
                print(
                    bold("Snapshot-sources")
                    + f" - {mode_phrase('dry-run', mode_source)}"
                )
                print(
                    f"Would capture: {candidate} "
                    f"({len(tables)} source table(s) from "
                    f"{len(source_names)} source(s))"
                )
                if exists:
                    print(
                        "  a baseline already exists there; capture "
                        "would refuse - choose a new --output or "
                        "remove it explicitly"
                    )
                if args.verbose:
                    for entry in tables:
                        print(f"  {entry['source']}.{entry['table']}")
                print(f"Re-run with --write to apply: {_apply_hint(argv)}")
                return 0
            path = capture_source_structure(
                args.project_dir, args.database, args.manifest, args.output
            )
            if args.format == "json":
                print(
                    json.dumps(
                        {
                            "command": "snapshot-sources",
                            "mode": "write",
                            "mode_source": mode_source,
                            "written": str(path),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0
            print(
                bold("Snapshot-sources")
                + f" - {mode_phrase('write', mode_source)}"
            )
            print(f"Captured source-structure baseline: {path}")
            return 0

        if args.command == "diff-sources":
            report = compare_source_structure(
                args.project_dir, args.database, args.manifest, args.baseline
            )
            if args.format == "json":
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print(render_compare_text(report, verbose=args.verbose))
            return 0

        raise LoomError(f"unsupported command {args.command!r}; see loom --help")
    except LoomError as error:
        print(f"loom: {error}", file=sys.stderr)
        return 1


def _check_renderer_wiring() -> None:
    """Every registered generator must have a renderer.

    Before this check, `generate` silently skipped a generated entry
    whose generator had no renderer (the entry filter treated the
    renderer table as the generator list). The registry/contract half
    of the wiring is checked in `loom.contracts.check_generator_wiring`
    on project load; this half can only be checked here, where the
    renderer table lives.
    """
    registered = set(GENERATOR_REGISTRY)
    rendered = set(RENDERERS)
    problems = [
        f"generator {name!r} is registered but has no renderer"
        for name in sorted(registered - rendered)
    ] + [
        f"renderer {name!r} exists for an unregistered generator"
        for name in sorted(rendered - registered)
    ]
    if problems:
        raise LoomError(
            "generator wiring is incomplete: " + "; ".join(problems)
        )
