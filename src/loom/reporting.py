"""Human-readable renderers for Loom's report commands.

The report commands historically printed raw JSON, which is
borderline unreadable at quickstart scale and unusable on large
projects. This module renders the same payloads as summary-first
text: a headline, counts before detail, non-empty sections only, and
low-level detail (unique IDs, full dependency paths, exhaustive
column lists) only in verbose mode. JSON remains available in full
via ``--format json`` and is the format automation and AI workflows
should request explicitly.

That grammar is tightened into one shape everywhere: verdict
first; lists longer than ``_FOLD_LIMIT`` fold to a head sample plus
the folded count and the unfold path (``--verbose``, or
``--format json``); homogeneous runs are grouped, not enumerated;
interface flags render as bracketed codes with a legend in default
mode and as their full prose only in verbose mode; warnings are a
headed section, not fine print. The renderers only ever regroup
data the payload already carries - they compute no fact the JSON
lacks and never mutate the shared payload.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from collections import Counter
from typing import Any, Iterable, TextIO

from wcwidth import wcwidth

from loom.migration_assessment import _model_layer

# The fold threshold: no default
# section lists more than this many entries.
_FOLD_LIMIT = 10
_WRAP_WIDTH = 100


# --- The one colour path ---------------------------------------
#
# Every coloured byte Loom emits goes through here: the report
# renderers in this module, the generate summary in the CLI, and
# both help surfaces (root and per-command). Colour is enabled
# only when the destination is a terminal and NO_COLOR is unset
# (any value, per no-color.org); piped output and ``--format
# json`` are never coloured. The CLI decides once per run and
# sets the module switch; the renderers read it through
# ``paint``/``bold``/``red``. Direct library callers (and the
# test suite, whose captured streams are not terminals) get
# plain text by default.

_COLOUR_ENABLED = False

BOLD = "1"
RED = "31"
GREEN = "32"


def colour_enabled(stream: TextIO | None = None) -> bool:
    """Whether colour may be emitted to ``stream`` (stdout by
    default): a terminal destination and no NO_COLOR opt-out."""
    if "NO_COLOR" in os.environ:
        return False
    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, "isatty", None)
    return bool(isatty is not None and isatty())


def set_colour_enabled(enabled: bool) -> None:
    """Set the process-wide colour switch the renderers read.
    The CLI calls this once per run: enabled only for text
    output to a terminal."""
    global _COLOUR_ENABLED
    _COLOUR_ENABLED = enabled


# --- The one terminal-width path -------------------------------
#
# Bounded width adaptation (assessment finding C-2) follows
# the colour pattern: the CLI resolves the width once per run
# and sets the module switch; the search renderers consult it
# through ``_adapt_width``. Only search's primary tables (the
# models table and the kind:-promoted tables), its facet line
# and the overview's census lines adapt - every other surface
# renders exactly as before. Adaptation happens only when the
# destination is a terminal: a pipe gets ``None`` here and the
# unbounded rendering, byte-identical to the pre-adaptation
# output, which the test suite pins. Direct library callers
# (and the suite, whose captured streams are not terminals)
# get ``None`` by default.

_TERMINAL_WIDTH: int | None = None

# Below this many columns the primary tables cannot keep all
# their columns in any form, so adaptation stands down and
# the unbounded layout stands - a stated bound, not a layout
# the renderer mangled trying.
_MIN_ADAPT_WIDTH = 40
# A terminal claiming more columns than this is not believed;
# the conventional fallback stands in for it.
_MAX_BELIEVED_WIDTH = 1000
_FALLBACK_WIDTH = 80


def terminal_width(stream: TextIO | None = None) -> int | None:
    """The width search output may adapt to: the terminal's
    column count when ``stream`` (stdout by default) is a
    terminal, else ``None`` - pipes and files never adapt. A
    terminal reporting a degenerate size (zero, or beyond
    belief) falls back to 80 columns; a terminal narrower
    than the adaptation floor also returns ``None``."""
    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, "isatty", None)
    if not (isatty is not None and isatty()):
        return None
    try:
        columns = shutil.get_terminal_size().columns
    except (OSError, ValueError):
        columns = 0
    if columns <= 0 or columns > _MAX_BELIEVED_WIDTH:
        columns = _FALLBACK_WIDTH
    if columns < _MIN_ADAPT_WIDTH:
        return None
    return columns


def set_terminal_width(width: int | None) -> None:
    """Set the process-wide width switch the search renderers
    read. The CLI calls this once per run, beside the colour
    decision; ``None`` (the default) disables adaptation."""
    global _TERMINAL_WIDTH
    _TERMINAL_WIDTH = width


def _adapt_width() -> int | None:
    return _TERMINAL_WIDTH


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _char_columns(char: str) -> int:
    """The columns one character occupies on a terminal:
    wcwidth's count, with its -1 for control and unprintable
    characters read as zero - such a character advances no
    column, and one stray control character must not poison
    the measurement of a whole line."""
    return max(wcwidth(char), 0)


def _visible_len(text: str) -> int:
    """A line's length as the terminal shows it, in columns:
    the ANSI escapes the colour path emits take no columns,
    and wide characters (CJK, most emoji) take two where a
    code-point count would take one. The count is summed per
    character rather than taken from ``wcwidth.wcswidth``:
    wcswidth returns -1 for a whole string when any one
    character is unprintable, discarding every other
    character's width with it, while the per-character sum
    stays total and monotone, which the truncation and
    padding arithmetic below rely on."""
    return sum(
        _char_columns(char) for char in _ANSI_RE.sub("", text)
    )


def _pad(text: str, columns: int) -> str:
    """``text`` left-aligned in a field ``columns`` terminal
    columns wide. An f-string width spec pads to a code-point
    count, so a cell carrying a wide character would come up
    short of its column and push the rest of the row right;
    padding by the visible count keeps every column where
    the table's arithmetic put it."""
    return text + " " * max(columns - _visible_len(text), 0)


def _truncate(text: str, budget: int) -> str:
    """``text`` fitted to ``budget`` columns: unchanged when
    it fits, else cut and closed with the ellipsis the fold
    grammar already uses. A truncated cell is marked - never
    silently clipped, and never split mid-token across lines
    the way a terminal's own wrap would split it. The cut
    walks whole characters, so a character is never split
    and the result never exceeds the budget: a wide
    character that would straddle the final column is
    dropped whole, and the ellipsis closes the line one
    column short rather than one over."""
    if _visible_len(text) <= budget:
        return text
    if budget <= 0:
        return ""
    if budget == 1:
        return "…"
    kept: list[str] = []
    used = 0
    for char in text:
        char_columns = _char_columns(char)
        if used + char_columns > budget - 1:
            break
        kept.append(char)
        used += char_columns
    return "".join(kept) + "…"


def _wrap_segments(
    text: str, width: int, separators: tuple[str, ...]
) -> list[str]:
    """Greedy-wrap ``text`` to ``width`` columns at the given
    separators. The text is atomised so each atom keeps its
    trailing separator (``"a, b, c"`` at ``", "`` becomes
    ``"a, " | "b, " | "c"``); atoms pack whole onto lines and
    are never split. A single atom longer than the line is
    truncated with the ellipsis marker instead - the one
    place bounded output loses characters, and it says so.
    Used for the free-text payloads whose items arrive
    separator-joined: argument lists, matched annotations,
    facet entries, shares/differs lists."""
    pattern = "|".join(re.escape(sep) for sep in separators)
    parts = re.split(f"({pattern})", text)
    atoms = [
        parts[index] + parts[index + 1]
        for index in range(0, len(parts) - 1, 2)
    ]
    atoms.append(parts[-1])
    lines: list[str] = []
    current = ""
    for atom in atoms:
        if not atom:
            continue
        if current and _visible_len((current + atom).rstrip()) > width:
            lines.append(current.rstrip())
            current = atom
        else:
            current += atom
    if current or not lines:
        lines.append(current.rstrip())
    return [
        _truncate(line, width) if _visible_len(line) > width else line
        for line in lines
    ]


def _wrap_annotated(
    prefix: str, text: str, width: int, separators: tuple[str, ...]
) -> list[str]:
    """``prefix`` on the first line and ``text`` wrapped after
    it at the separators, continuation lines aligned under
    the text - the shape a bounded table's annotation takes
    when it moves beneath its row."""
    budget = max(width - len(prefix), 8)
    segments = _wrap_segments(text, budget, separators)
    indent = " " * len(prefix)
    return [prefix + segments[0], *[indent + seg for seg in segments[1:]]]


def paint(text: str, *codes: str, enabled: bool | None = None) -> str:
    """Wrap ``text`` in the given ANSI codes when colour is on.

    ``enabled`` overrides the module switch - the help surfaces
    use it, because argparse renders help before the CLI has set
    the switch; they pass ``colour_enabled()`` for the real
    stdout instead."""
    if enabled is None:
        enabled = _COLOUR_ENABLED
    if not enabled or not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def bold(text: str) -> str:
    return paint(text, BOLD)


def red(text: str) -> str:
    return paint(text, RED)


def green(text: str) -> str:
    return paint(text, GREEN)


def mode_phrase(mode: str, source: str | None) -> str:
    """The banner wording for a mutating command's mode, stated
    identically on every mutating run: ``writing`` plainly,
    ``writing (LOOM_WRITE)`` when the environment variable set
    the mode (the explicit flag needs no annotation - the
    practitioner typed it), and ``dry run - nothing written``
    for the default preview."""
    if mode == "write":
        return "writing (LOOM_WRITE)" if source == "env" else "writing"
    return "dry run - nothing written"


def mode_suffix(report: dict[str, Any]) -> str:
    """The `` - <mode phrase>`` suffix for a report headline,
    from the ``mode``/``mode_source`` fields the CLI stamps on
    mutating commands' payloads; empty when the payload carries
    no mode (read-only reports, direct renderer calls)."""
    mode = report.get("mode")
    if mode not in ("write", "dry-run"):
        return ""
    return " - " + mode_phrase(mode, report.get("mode_source"))


def _names(items: Iterable[Any]) -> str:
    return ", ".join(str(item) for item in items)


def _section(lines: list[str], title: str, body: list[str]) -> None:
    if not body:
        return
    lines.append("")
    lines.append(bold(title))
    lines.extend(body)


def _wrap(text: str, *, indent: str = "  ", subsequent: str = "    ") -> list[str]:
    """Wrap one paragraph to the report width under an indent."""
    return textwrap.wrap(
        text,
        width=_WRAP_WIDTH,
        initial_indent=indent,
        subsequent_indent=subsequent,
    ) or [indent.rstrip()]


def _folded_name_lines(
    names: list[str], *, verbose: bool, unfold: str = "--verbose, or --format json"
) -> list[str]:
    """A plain name list in the shared fold grammar.

    Verbose lists every name, one per line. Default lists the
    names comma-joined when there are few, and folds past the
    threshold to a head sample plus the folded count and the way
    to unfold."""
    if verbose:
        return [f"  {name}" for name in names]
    if len(names) <= _FOLD_LIMIT:
        return _wrap(_names(names))
    head = _names(names[:_FOLD_LIMIT])
    lines = _wrap(f"First {_FOLD_LIMIT}: {head}")
    lines.append(
        f"  … and {len(names) - _FOLD_LIMIT} more (full list: {unfold})"
    )
    return lines


def _fold_note(shown: int, total: int, *, unfold: str) -> str:
    return f"  … and {total - shown} more (full list: {unfold})"


# --- Interface-flag codes -------------------------------------
#
# The assessment payload carries interface flags as prose sentences
# (``interface_flags`` in the JSON - frozen). The text renderers
# classify each sentence, by its stable generated phrasing, into a
# short code for default output; the prose itself is shown only in
# verbose mode. Classification is presentation only: every flag
# lands in exactly one class, an unrecognised flag lands in
# ``other`` and is still counted and coded, so no flag can silently
# disappear from the text in either mode and the text counts always
# reconcile with the JSON list.

_TESTS_FLAG = re.compile(r"^(\d+) test\(s\) are attached")
_CONSUMERS_FLAG = re.compile(r"^(\d+) downstream consumer\(s\)")

# class key -> (census label, legend fragment)
_FLAG_CLASSES: dict[str, tuple[str, str]] = {
    "consumers": (
        "consumers",
        "[consumers:N] downstream consumers in the manifest",
    ),
    "tests": (
        "attached tests",
        "[tests:N] attached tests a swap must keep or replace",
    ),
    "semantic": (
        "semantic layer",
        "[semantic] semantic layer reads the model; reachable "
        "metrics and saved queries are named in --verbose",
    ),
    "contract": (
        "enforced contracts",
        "[contract] enforced dbt contract",
    ),
    "access": ("access", "[access] non-default access level"),
    "physical": (
        "physical config",
        "[physical] physical warehouse configuration",
    ),
    "materialisation": (
        "materialisation",
        "[materialized] non-default materialisation",
    ),
    "config": (
        "other config",
        "[config] other declared configuration "
        "(group, deprecation date, grants)",
    ),
    "other": (
        "other flags",
        "[other] further interface flags (prose in --verbose)",
    ),
}
_FLAG_CLASS_ORDER = tuple(_FLAG_CLASSES)


def _classify_flag(flag: str) -> tuple[str, int | None]:
    """The presentation class of one interface-flag sentence, and
    its count where the sentence carries one (tests, consumers)."""
    match = _TESTS_FLAG.match(flag)
    if match:
        return "tests", int(match.group(1))
    match = _CONSUMERS_FLAG.match(flag)
    if match:
        return "consumers", int(match.group(1))
    if flag.startswith("the semantic layer reads this model"):
        return "semantic", None
    if flag.startswith("carries an enforced dbt contract"):
        return "contract", None
    if flag.startswith("access is "):
        return "access", None
    if flag.startswith("carries physical warehouse configuration"):
        return "physical", None
    if flag.startswith("materialized as "):
        return "materialisation", None
    if flag.startswith(
        ("belongs to dbt group", "carries a dbt deprecation date", "carries dbt grants")
    ):
        return "config", None
    return "other", None


def _flag_census(models: Iterable[dict[str, Any]]) -> list[tuple[str, int]]:
    """Flag occurrences by class across the given models, in the
    fixed display order, non-zero classes only. Reconciles with
    the models' ``interface_flags`` lists by construction: every
    flag is classified exactly once."""
    counts: Counter[str] = Counter()
    for model in models:
        for flag in model.get("interface_flags") or []:
            counts[_classify_flag(flag)[0]] += 1
    return [
        (_FLAG_CLASSES[key][0], counts[key])
        for key in _FLAG_CLASS_ORDER
        if counts[key]
    ]


def _flag_codes(model: dict[str, Any]) -> str:
    """The bracketed flag codes for one model, e.g.
    `` [tests:1] [consumers:2]``; empty when the model is
    unflagged."""
    by_class: dict[str, int | None] = {}
    for flag in model.get("interface_flags") or []:
        key, count = _classify_flag(flag)
        if key in ("tests", "consumers"):
            by_class[key] = count
        else:
            by_class.setdefault(key, None)
    codes = []
    for key in _FLAG_CLASS_ORDER:
        if key not in by_class:
            continue
        if key == "tests":
            codes.append(f"[tests:{by_class[key]}]")
        elif key == "consumers":
            codes.append(f"[consumers:{by_class[key]}]")
        elif key == "materialisation":
            codes.append("[materialized]")
        else:
            codes.append(f"[{key}]")
    return " " + " ".join(codes) if codes else ""


def _flag_legend(models: Iterable[dict[str, Any]]) -> list[str]:
    """The one-line legend for the flag codes used by the given
    models; empty when no model is flagged."""
    present = {
        _classify_flag(flag)[0]
        for model in models
        for flag in model.get("interface_flags") or []
    }
    if not present:
        return []
    fragments = [
        _FLAG_CLASSES[key][1] for key in _FLAG_CLASS_ORDER if key in present
    ]
    return _wrap("Flag legend: " + " · ".join(fragments), indent="", subsequent="  ")


def _coverage_totals(payload: dict[str, Any]) -> tuple[int, int] | None:
    """(covered, total) project-model counts for a coverage
    payload, derived from the fields it carries: the models a
    paradigm entry covers, plus the missing list. ``None`` when
    the payload carries no paradigm breakdown and nothing is
    missing (nothing to state)."""
    by_paradigm = payload.get("by_paradigm") or {}
    missing = len(payload.get("missing_models") or [])
    if not by_paradigm and not missing:
        return None
    covered = sum(v["covered"] for v in by_paradigm.values())
    return covered, covered + missing


# The remedy a misplaced-generated-models report must name (QOL
# B-3): a generated model whose manifest file is not at the
# expected output path is most often mid-adoption - the instance
# was promoted but the swap has not happened - and a bare
# PROBLEMS FOUND left the practitioner to guess that. Both the
# check gate and the coverage report carry this line whenever
# they report misplaced models.
_MISPLACED_REMEDY = (
    "a generated model at an unexpected output path usually "
    "means an adoption step is still pending: complete the swap "
    "(replace the original model with the generated file), or "
    "run `loom generate --write` and re-run `dbt parse`, then "
    "re-check"
)


# The remedy a shadowed-models report must name (P5): a
# generated model and a hand-written model sharing a name leave
# dbt unable to parse the project at all, so the only ways out
# are completing the swap (the hand-written original goes) or
# retreating to the playbook's prove-in-a-scratch-copy route,
# where the swap happens last, after proof.
_SHADOWED_REMEDY = (
    "remove or move the hand-written original(s) once the "
    "generated models are proven, or prove the generated models "
    "in a scratch copy and swap last (adoption playbook, "
    "Steps 6-7)"
)


def shadowed_model_lines(
    pairs: list[dict[str, Any]], *, verbose: bool = False
) -> list[str]:
    """One line per shadowed model: its name, the generated
    file, and the hand-written file(s) sharing the name. Folded
    past the shared limit like every other name list."""
    shown = pairs if verbose else pairs[:_FOLD_LIMIT]
    body = [
        f"  {pair['name']}: {pair['generated']} (generated) · "
        + " · ".join(
            f"{path} (hand-written)" for path in pair["hand_written"]
        )
        for pair in shown
    ]
    if not verbose and len(pairs) > _FOLD_LIMIT:
        body.append(
            _fold_note(
                len(shown),
                len(pairs),
                unfold="--verbose, or --format json",
            )
        )
    return body


def render_shadowed_section(
    pairs: list[dict[str, Any]], *, verbose: bool = False, lead: str
) -> list[str]:
    """The shadowed-models section of a `generate` report: the
    pairs, the consequence (every dbt command fails while the
    state lasts), and the way out."""
    lines = [bold(f"Shadowed models ({len(pairs)}):")]
    lines.extend(shadowed_model_lines(pairs, verbose=verbose))
    lines.append(f"  {lead}")
    lines.append(f"  Next: {_SHADOWED_REMEDY}.")
    return lines


def render_coverage_shadowed_text(
    pairs: list[dict[str, Any]], *, verbose: bool = False
) -> str:
    """The `coverage` report for the shadowed state: coverage
    cannot run - the manifest cannot be refreshed while dbt
    cannot parse the project - so the report names the state
    and the way out instead of prescribing the `dbt parse` that
    cannot succeed."""
    lines = [red("Inventory coverage: CANNOT RUN - shadowed models")]
    lines.append(
        "State: dbt cannot parse the project while a generated "
        "model and a hand-written model share a name, so the "
        "manifest cannot be refreshed and coverage cannot be "
        "checked."
    )
    _section(
        lines,
        f"Shadowed models ({len(pairs)}):",
        shadowed_model_lines(pairs, verbose=verbose),
    )
    lines.append("")
    lines.append(f"Next: {_SHADOWED_REMEDY}; then re-run coverage.")
    return "\n".join(lines)


def render_check_shadowed_text(
    pairs: list[dict[str, Any]], *, verbose: bool = False
) -> str:
    """The `check` report for the shadowed state: check cannot
    run - its gate renders from the coverage payload, which
    needs a manifest dbt cannot refresh while it cannot parse
    the project - so the report names the state and the way out
    instead of surfacing the stale-manifest error whose remedy
    is the `dbt parse` that cannot succeed. The verdict line is
    coverage's, verbatim: the state is one state, named the same
    by every command that reports it."""
    lines = [red("Inventory coverage: CANNOT RUN - shadowed models")]
    lines.append(
        "State: dbt cannot parse the project while a generated "
        "model and a hand-written model share a name, so the "
        "manifest cannot be refreshed and check cannot run."
    )
    _section(
        lines,
        f"Shadowed models ({len(pairs)}):",
        shadowed_model_lines(pairs, verbose=verbose),
    )
    lines.append("")
    lines.append(f"Next: {_SHADOWED_REMEDY}; then re-run check.")
    return "\n".join(lines)


def render_check_gate_text(payload: dict[str, Any]) -> str:
    """The `loom check` failure gate: the
    one-screen summary of a failed coverage payload, produced by
    the CLI from the same payload `coverage` renders, so
    the two commands' counts always agree. (``check_project``'s
    raised error remains the library behaviour; this is only the
    CLI's presentation of the failure.)"""
    parts: list[str] = []
    missing = payload.get("missing_models") or []
    if missing:
        totals = _coverage_totals(payload)
        if totals is not None:
            parts.append(
                f"{len(missing)} of {totals[1]} project models "
                "missing from inventory"
            )
        else:
            parts.append(
                f"{len(missing)} project models missing from inventory"
            )
    orphans = payload.get("orphan_inventory_entries") or []
    if orphans:
        parts.append(f"{len(orphans)} orphan inventory entries")
    for key, label in (
        ("duplicate_inventory_entries", "duplicate inventory names"),
        ("duplicate_manifest_models", "duplicate project model names"),
        ("versioned_manifest_models", "versioned project models (unsupported)"),
    ):
        entries = payload.get(key) or {}
        if entries:
            parts.append(f"{len(entries)} {label}")
    misplaced = payload.get("misplaced_generated_models") or []
    if misplaced:
        parts.append(
            f"{len(misplaced)} generated models at unexpected output paths"
        )
    lines = [red("Inventory coverage: PROBLEMS FOUND - " + " · ".join(parts))]
    if missing:
        lines.append(
            "Next: loom register drafts a registration for every "
            "model; loom coverage shows the full report."
        )
    if misplaced:
        lines.append(
            "Next: " + _MISPLACED_REMEDY + "; loom coverage shows "
            "the full report."
        )
    if not missing and not misplaced:
        lines.append("Next: loom coverage shows the full report.")
    return "\n".join(lines)


def render_coverage_text(payload: dict[str, Any], *, verbose: bool = False) -> str:
    verdict = (
        green("Inventory coverage: OK")
        if payload["ok"]
        else red("Inventory coverage: PROBLEMS FOUND")
    )
    lines = [verdict]
    by_paradigm = payload.get("by_paradigm") or {}
    if payload["ok"]:
        if by_paradigm:
            total = sum(v["total"] for v in by_paradigm.values())
            covered = sum(v["covered"] for v in by_paradigm.values())
            lines.append(
                f"Inventory entries: {total} ({covered} cover a project model)"
            )
    else:
        totals = _coverage_totals(payload)
        orphans = len(payload.get("orphan_inventory_entries") or [])
        if totals is not None:
            lines.append(
                f"State: {totals[0]} of {totals[1]} project models "
                f"covered · {orphans} orphan entries"
            )
    problems = {
        "Missing from inventory": payload["missing_models"],
        "Orphan inventory entries": payload["orphan_inventory_entries"],
    }
    for title, values in problems.items():
        _section(
            lines,
            f"{title} ({len(values)}):",
            _folded_name_lines(list(values), verbose=verbose),
        )
    misplaced = payload.get("misplaced_generated_models") or []
    if misplaced:
        _section(
            lines,
            f"Misplaced generated models ({len(misplaced)}):",
            _folded_name_lines(list(misplaced), verbose=verbose),
        )
    for title, key in (
        ("Duplicate inventory entries", "duplicate_inventory_entries"),
        ("Duplicate manifest models", "duplicate_manifest_models"),
        ("Versioned manifest models (unsupported)", "versioned_manifest_models"),
    ):
        entries = payload.get(key) or {}
        items = sorted(entries.items())
        shown = items if verbose else items[:_FOLD_LIMIT]
        body = [f"  {name}: {_names(paths)}" for name, paths in shown]
        if not verbose and len(items) > _FOLD_LIMIT:
            body.append(_fold_note(len(shown), len(items), unfold="--verbose, or --format json"))
        _section(lines, f"{title} ({len(entries)}):", body)
    if by_paradigm:
        body = [
            f"  {name}: {values['covered']}/{values['total']} covered"
            for name, values in sorted(by_paradigm.items())
        ]
        _section(lines, "By paradigm:", body)
    if not payload["ok"]:
        lines.append("")
        if payload.get("missing_models"):
            lines.append(
                "Next: loom register --write, then review "
                "the drafted registrations."
            )
        elif not misplaced:
            lines.append(
                "Next: register missing models (see `loom register`) "
                "or remove orphan entries, then re-run."
            )
        if misplaced:
            lines.append("Next: " + _MISPLACED_REMEDY + ".")
    return "\n".join(lines)


def render_prove_text(payload: dict[str, Any], *, verbose: bool = False) -> str:
    """The static equivalence stage's text report: verdict
    counts first, then the per-model verdicts with their bases -
    proven models may skip the warehouse comparison, inconclusive
    models proceed to it, and a not-run stage changes nothing."""
    if payload.get("stage") == "not_run":
        missing = payload.get("missing") or []
        lines = [
            bold(
                "Static equivalence: not run - "
                + ", ".join(missing)
                + (" is missing." if len(missing) == 1 else " are missing.")
            )
        ]
        lines.append(
            "Next: compile both projects (`dbt compile`) and place the "
            "manifests at the fixed names above (adoption playbook, "
            "Step 6), or proceed to the warehouse comparison without "
            "the static stage."
        )
        return "\n".join(lines)
    summary = payload.get("summary") or {}
    lines = [
        bold(
            f"Static equivalence: {summary.get('proven', 0)} proven, "
            f"{summary.get('inconclusive', 0)} inconclusive "
            f"of {summary.get('subjects', 0)} models."
        )
    ]
    for model in payload.get("models") or []:
        lines.append(
            f"  {model['name']}: {model['verdict']} - {model['basis']}"
        )
    partial = payload.get("documentation_partial") or []
    if partial:
        shown = partial[:10]
        names = ", ".join(shown)
        if len(partial) > len(shown):
            names += (
                f", and {len(partial) - len(shown)} more "
                "(see --format json)"
            )
        lines.append(
            "Note: the original manifest's documentation is partial "
            f"for {len(partial)} of {summary.get('subjects', 0)} "
            f"compared models ({names}): the counterpart check sees "
            "documented columns only, so verdicts on those models "
            "may reflect documentation gaps rather than real "
            "differences - the warehouse comparison decides."
        )
    non_subjects = payload.get("non_subjects") or []
    if non_subjects:
        lines.append(
            "Candidate-only models (not equivalence subjects): "
            + ", ".join(non_subjects)
            + "."
        )
    if summary.get("inconclusive"):
        lines.append(
            "Next: warehouse-prove the inconclusive models (adoption "
            "playbook, Step 6); the proven models may skip the "
            "comparison - record this report with the conversion."
        )
    else:
        lines.append(
            "Next: record this report with the conversion; every "
            "subject was statically proven."
        )
    return "\n".join(lines)


# --- Search rendering ------------------------------------
#
# The search payload the CLI builds over the inventory corpus
# renders as: a headline naming the corpus mode, a facet line
# over the returned set, the model table (name, status,
# generator, owner, matched), tier-2 objects in grouped
# sections beneath the models, a detail view when the query
# resolves to exactly one object, and the project overview
# when there is no query. Status is the primary colour
# carrier in the tables: the status token is painted, through
# the one colour path, when colour is enabled.

_STATUS_CODES = {
    "generated": GREEN,
    "registered": "36",
    "unmanaged": "33",
    "orphaned": RED,
}

_TIER2_KIND_TITLES = {
    "macro": "Macros",
    "source": "Sources",
    "seed": "Seeds",
    "snapshot": "Snapshots",
    "test": "Tests",
    "exposure": "Exposures",
}

_TIER2_KIND_ORDER = ("macro", "source", "seed", "snapshot", "test", "exposure")


def _corpus_mode_note(corpus: dict[str, Any]) -> str:
    return (
        "metadata only"
        if corpus.get("mode") == "metadata-only"
        else "metadata + manifest"
    )


def _manifest_generated_line(corpus: dict[str, Any]) -> str | None:
    """The manifest age label search's contract promises in its
    output: when the manifest was generated, and how long ago.
    Search never refuses over a stale manifest — this line is
    how the practitioner judges the answers' currency. Shown on
    the overview and on result lists alike; metadata-only mode
    has no manifest to label."""
    if corpus.get("mode") == "metadata-only":
        return None
    if not corpus.get("manifest_generated_at"):
        return None
    generated = f"Manifest: generated {corpus['manifest_generated_at']}"
    if corpus.get("manifest_age"):
        generated += f" ({corpus['manifest_age']} ago)"
    return generated


def _facet_groups(facets: dict[str, Any]) -> list[str]:
    parts = []
    for key in ("status", "kind", "generator", "owner", "paradigm", "provenance"):
        counts = facets.get(key) or {}
        if not counts:
            continue
        rendered = ", ".join(
            f"{value} {count}" for value, count in counts.items()
        )
        parts.append(f"{key}: {rendered}")
    return parts


def _facet_line(facets: dict[str, Any]) -> str | None:
    parts = _facet_groups(facets)
    if not parts:
        return None
    return "Facets: " + " · ".join(parts)


def _facet_lines(facets: dict[str, Any], width: int) -> list[str]:
    """The facet line bounded to ``width``: groups pack onto
    lines whole, joined by the same `` · `` as the unbounded
    line, under a hanging indent aligned with the first
    group. A group too long for any line wraps at its entry
    boundaries (``, ``) under the same indent - a facet entry
    (``value count``) is never split."""
    groups = _facet_groups(facets)
    prefix = "Facets: "
    indent = " " * len(prefix)
    avail = max(width - len(prefix), 8)
    packed: list[str] = []
    current: str | None = None
    for group in groups:
        if _visible_len(group) > avail:
            if current is not None:
                packed.append(current)
                current = None
            packed.extend(_wrap_segments(group, avail, (", ",)))
            continue
        candidate = group if current is None else f"{current} · {group}"
        if current is not None and _visible_len(candidate) > avail:
            packed.append(current)
            current = group
        else:
            current = candidate
    if current is not None:
        packed.append(current)
    if not packed:
        return []
    return [prefix + packed[0], *[indent + line for line in packed[1:]]]


def _matched_text(item: dict[str, Any]) -> str:
    parts = []
    if item.get("matched"):
        parts.append(f"matched: {', '.join(item['matched'])}")
    if item.get("missing"):
        parts.append(f"differs: {', '.join(item['missing'])}")
    similarity = item.get("similarity") or {}
    if similarity:
        parts.append(
            f"shape overlap {round(similarity['score'] * 100)}%"
        )
    return " · ".join(parts)


def _model_table(items: list[dict[str, Any]], *, verbose: bool) -> list[str]:
    rows = []
    for item in items:
        rows.append(
            (
                item,
                item.get("status") or "-",
                item.get("generator") or item.get("classification") or "-",
                item.get("owner") or "-",
            )
        )
    name_w = max(_visible_len(item["name"]) for item, *_ in rows)
    status_w = max(_visible_len(status) for _, status, *_ in rows)
    label_w = max(_visible_len(label) for _, _, label, _ in rows)
    owner_w = max(_visible_len(owner) for *_, owner in rows)
    lines = ["Models:"]
    for item, status, label, owner in rows:
        status_cell = paint(
            _pad(status, status_w), _STATUS_CODES.get(status, BOLD)
        )
        line = (
            f"  {_pad(item['name'], name_w)}  {status_cell}  "
            f"{_pad(label, label_w)}  {_pad(owner, owner_w)}"
        )
        matched = _matched_text(item)
        if matched:
            line += f"  {matched}"
        lines.append(line.rstrip())
        similarity = item.get("similarity") or {}
        if similarity:
            if similarity.get("shares"):
                lines.append(
                    f"    shares: {'; '.join(similarity['shares'])}"
                )
            if similarity.get("differs"):
                lines.append(
                    f"    differs: {'; '.join(similarity['differs'])}"
                )
        if verbose:
            if item.get("path"):
                lines.append(f"    path: {item['path']}")
            if item.get("description"):
                lines.append(f"    description: {item['description']}")
    width = _adapt_width()
    if width is None or all(
        _visible_len(line) <= width for line in lines
    ):
        return lines
    return _model_table_bounded(items, verbose=verbose, width=width)


def _model_table_bounded(
    items: list[dict[str, Any]], *, verbose: bool, width: int
) -> list[str]:
    """The models table bounded to ``width`` columns. The
    four columns all stay: when the fixed columns alone are
    too wide they shrink - owner first, then the generator
    label, then the name, each closed with the ellipsis
    marker - while the status column, the verdict the table
    is scanned by and the shortest of the four, is never
    truncated. The matched annotation, which the unbounded
    table appends to the row, moves beneath it and wraps at
    its own separators; similarity shares/differs lists wrap
    the same way under their labels. Verbose path/description
    lines are not reflowed - they are detail prose, not the
    table."""
    rows = []
    for item in items:
        rows.append(
            (
                item,
                item.get("status") or "-",
                item.get("generator") or item.get("classification") or "-",
                item.get("owner") or "-",
            )
        )
    status_w = max(_visible_len(status) for _, status, *_ in rows)
    budgets = [
        max(_visible_len(item["name"]) for item, *_ in rows),
        max(_visible_len(label) for _, _, label, _ in rows),
        max(_visible_len(owner) for *_, owner in rows),
    ]
    floors = [8, 4, 4]

    def total(b: list[int]) -> int:
        return 2 + b[0] + 2 + status_w + 2 + b[1] + 2 + b[2]

    while total(budgets) > width:
        for index in (2, 1, 0):
            if budgets[index] > floors[index]:
                budgets[index] -= 1
                break
        else:
            break
    name_b, label_b, owner_b = budgets
    lines = ["Models:"]
    for item, status, label, owner in rows:
        status_cell = paint(
            _pad(status, status_w), _STATUS_CODES.get(status, BOLD)
        )
        line = (
            f"  {_pad(_truncate(item['name'], name_b), name_b)}  "
            f"{status_cell}  "
            f"{_pad(_truncate(label, label_b), label_b)}  "
            f"{_pad(_truncate(owner, owner_b), owner_b)}"
        )
        lines.append(line.rstrip())
        matched = _matched_text(item)
        if matched:
            lines.extend(
                _wrap_annotated("    ", matched, width, (" · ", ", "))
            )
        similarity = item.get("similarity") or {}
        if similarity:
            if similarity.get("shares"):
                lines.extend(
                    _wrap_annotated(
                        "    shares: ",
                        "; ".join(similarity["shares"]),
                        width,
                        ("; ",),
                    )
                )
            if similarity.get("differs"):
                lines.extend(
                    _wrap_annotated(
                        "    differs: ",
                        "; ".join(similarity["differs"]),
                        width,
                        ("; ",),
                    )
                )
        if verbose:
            if item.get("path"):
                lines.append(f"    path: {item['path']}")
            if item.get("description"):
                lines.append(f"    description: {item['description']}")
    return lines


def _arguments_summary(item: dict[str, Any]) -> str:
    arguments = item.get("arguments") or []
    names = [
        str(argument["name"])
        for argument in arguments
        if isinstance(argument, dict) and argument.get("name")
    ]
    return ", ".join(names) if names else "-"


def _tier2_row(item: dict[str, Any], *, verbose: bool) -> list[str]:
    line = f"  {item['name']}"
    matched = _matched_text(item)
    if matched:
        line += f" · {matched}"
    lines = [line]
    if verbose:
        if item.get("description"):
            lines.append(f"    description: {item['description']}")
        if item.get("path"):
            lines.append(f"    path: {item['path']}")
        if item.get("kind") == "macro":
            lines.append(f"    arguments: {_arguments_summary(item)}")
            used_by = item.get("used_by") or []
            lines.append(f"    used by: {len(used_by)}")
    return lines


def _narrowing_query(kind: str, label: str, query: str) -> str:
    tokens = query.split()
    if len(tokens) == 1 and ":" not in tokens[0] and not tokens[0].startswith(
        ("+", "@")
    ):
        return f"kind:{kind},{tokens[0]}"
    if label == "project":
        return f"kind:{kind},is:project"
    return f"kind:{kind},package:{label}"


def _promoted_label(items: list[dict[str, Any]]) -> str:
    """The provenance label a folded kind:-promoted table
    narrows to. The grouped sections fold per provenance
    subgroup and narrow to that subgroup; a promoted table
    is one table, so it narrows to its largest provenance
    group - the bulk of what the fold hides - with ties in
    the group order the sections use (project first, then
    package name)."""
    counts: dict[str, int] = {}
    for item in items:
        if item.get("provenance") == "package":
            label = str(item.get("package") or "package")
        else:
            label = "project"
        counts[label] = counts.get(label, 0) + 1
    return sorted(
        counts,
        key=lambda label: (-counts[label], label != "project", label),
    )[0]


def _tier2_groups(
    items: list[dict[str, Any]], *, verbose: bool, query: str
) -> list[str]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_kind.setdefault(item.get("kind") or "object", []).append(item)
    kinds = [kind for kind in _TIER2_KIND_ORDER if kind in by_kind]
    kinds += sorted(kind for kind in by_kind if kind not in _TIER2_KIND_ORDER)
    lines: list[str] = []
    for kind in kinds:
        group = by_kind[kind]
        subgroups: list[tuple[str, list[dict[str, Any]]]] = []
        project = [i for i in group if i.get("provenance") != "package"]
        if project:
            subgroups.append(("project", project))
        packages = sorted(
            {
                str(i.get("package") or "package")
                for i in group
                if i.get("provenance") == "package"
            }
        )
        for package in packages:
            subgroups.append(
                (
                    package,
                    [
                        i
                        for i in group
                        if i.get("provenance") == "package"
                        and str(i.get("package") or "package") == package
                    ],
                )
            )
        title = _TIER2_KIND_TITLES.get(kind, f"{kind}s")
        for label, subgroup in subgroups:
            if label == "project":
                lines.append(f"{title} - project ({len(subgroup)}):")
            else:
                lines.append(f"{title} - package {label} ({len(subgroup)}):")
            shown = subgroup if verbose else subgroup[:_FOLD_LIMIT]
            for item in shown:
                lines.extend(_tier2_row(item, verbose=verbose))
            if len(subgroup) > len(shown):
                lines.append(
                    f"  … and {len(subgroup) - len(shown)} more - "
                    f"narrow with: loom search "
                    f"\"{_narrowing_query(kind, label, query)}\""
                )
    return lines


def _kind_fact(kind: str, item: dict[str, Any]) -> str:
    """The one graph fact a promoted tier-2 table is read by for
    each kind: what consumes a source or a seed, the model a
    snapshot snapshots and the model a test tests (named - the
    attachment is the fact, and the set is small by
    construction), and how much an exposure depends on."""
    upstream = [str(name) for name in item.get("upstream") or []]
    downstream = item.get("downstream") or []
    if kind in ("source", "seed"):
        return f"downstream: {len(downstream)}"
    if kind == "snapshot":
        return f"of: {', '.join(upstream) if upstream else '-'}"
    if kind == "test":
        return f"on: {', '.join(upstream) if upstream else '-'}"
    if kind == "exposure":
        return f"depends on: {len(upstream)}"
    return "-"


def _kind_table_unbounded(
    kind: str, items: list[dict[str, Any]], *, verbose: bool
) -> list[str]:
    title = _TIER2_KIND_TITLES.get(kind, f"{kind}s")
    lines = [f"{title}:"]
    if kind == "macro":
        rows = [
            (
                item,
                item.get("provenance") or "-",
                item.get("package") or "-",
                _arguments_summary(item),
                str(len(item.get("used_by") or [])),
            )
            for item in items
        ]
        name_w = max(_visible_len(item["name"]) for item, *_ in rows)
        prov_w = max(_visible_len(prov) for _, prov, *_ in rows)
        pkg_w = max(_visible_len(pkg) for _, _, pkg, _args, _used in rows)
        args_w = max(_visible_len(args) for *_, args, _used in rows)
        for item, prov, pkg, args, used in rows:
            line = (
                f"  {_pad(item['name'], name_w)}  {_pad(prov, prov_w)}  "
                f"{_pad(pkg, pkg_w)}  {_pad(args, args_w)}  used by: {used}"
            )
            matched = _matched_text(item)
            if matched:
                line += f"  {matched}"
            lines.append(line.rstrip())
            if verbose and item.get("description"):
                lines.append(f"    description: {item['description']}")
        return lines
    rows = [
        (
            item,
            item.get("provenance") or "-",
            item.get("package") or "-",
            _kind_fact(kind, item),
        )
        for item in items
    ]
    name_w = max(_visible_len(item["name"]) for item, *_ in rows)
    prov_w = max(_visible_len(prov) for _, prov, *_ in rows)
    pkg_w = max(_visible_len(pkg) for _, _, pkg, _fact in rows)
    fact_w = max(_visible_len(fact) for *_, fact in rows)
    for item, prov, pkg, fact in rows:
        line = (
            f"  {_pad(item['name'], name_w)}  {_pad(prov, prov_w)}  "
            f"{_pad(pkg, pkg_w)}  {_pad(fact, fact_w)}"
        )
        matched = _matched_text(item)
        if matched:
            line += f"  {matched}"
        lines.append(line.rstrip())
        if verbose and item.get("description"):
            lines.append(f"    description: {item['description']}")
    return lines


def _kind_table(
    kind: str, items: list[dict[str, Any]], *, verbose: bool
) -> list[str]:
    lines = _kind_table_unbounded(kind, items, verbose=verbose)
    width = _adapt_width()
    if width is None or all(
        _visible_len(line) <= width for line in lines
    ):
        return lines
    return _kind_table_bounded(kind, items, verbose=verbose, width=width)


# The narrowest the free-text column of a bounded kind table
# may be squeezed to before the name column gives way instead.
_MIN_TEXT_COLUMN = 12


def _kind_table_bounded(
    kind: str, items: list[dict[str, Any]], *, verbose: bool, width: int
) -> list[str]:
    """A kind:-promoted table bounded to ``width`` columns.
    Every column stays. The free-text column - a macro's
    argument list, or another kind's graph fact - wraps at
    its entry boundaries into the space the fixed columns
    leave, continuation lines aligned under the column; the
    fixed columns keep their natural widths unless the name
    alone would starve the text column below its floor, in
    which case the name truncates with the ellipsis marker.
    The matched annotation moves beneath its row, as in the
    bounded models table."""
    title = _TIER2_KIND_TITLES.get(kind, f"{kind}s")
    lines = [f"{title}:"]
    if kind == "macro":
        rows = [
            (
                item,
                item.get("provenance") or "-",
                item.get("package") or "-",
                _arguments_summary(item),
                str(len(item.get("used_by") or [])),
            )
            for item in items
        ]
        name_w = max(_visible_len(item["name"]) for item, *_ in rows)
        prov_w = max(_visible_len(prov) for _, prov, *_ in rows)
        pkg_w = max(_visible_len(pkg) for _, _, pkg, _args, _used in rows)
        used_w = max(_visible_len(used) for *_, used in rows)
        suffix_w = len("used by: ") + used_w
        fixed = 2 + name_w + 2 + prov_w + 2 + pkg_w + 2 + 2 + suffix_w
        if width - fixed < _MIN_TEXT_COLUMN and name_w > 8:
            name_w = max(8, name_w - (_MIN_TEXT_COLUMN - (width - fixed)))
            fixed = 2 + name_w + 2 + prov_w + 2 + pkg_w + 2 + 2 + suffix_w
        args_budget = max(width - fixed, 1)
        args_indent = " " * (2 + name_w + 2 + prov_w + 2 + pkg_w + 2)
        for item, prov, pkg, args, used in rows:
            segments = _wrap_segments(args, args_budget, (", ",))
            line = (
                f"  {_pad(_truncate(item['name'], name_w), name_w)}  "
                f"{_pad(prov, prov_w)}  {_pad(pkg, pkg_w)}  "
                f"{_pad(segments[0], args_budget)}  used by: {used}"
            )
            lines.append(line.rstrip())
            for segment in segments[1:]:
                lines.append(args_indent + segment)
            matched = _matched_text(item)
            if matched:
                lines.extend(
                    _wrap_annotated("    ", matched, width, (" · ", ", "))
                )
            if verbose and item.get("description"):
                lines.append(f"    description: {item['description']}")
        return lines
    rows = [
        (
            item,
            item.get("provenance") or "-",
            item.get("package") or "-",
            _kind_fact(kind, item),
        )
        for item in items
    ]
    name_w = max(_visible_len(item["name"]) for item, *_ in rows)
    prov_w = max(_visible_len(prov) for _, prov, *_ in rows)
    pkg_w = max(_visible_len(pkg) for _, _, pkg, _fact in rows)
    fixed = 2 + name_w + 2 + prov_w + 2 + pkg_w + 2
    if width - fixed < _MIN_TEXT_COLUMN and name_w > 8:
        name_w = max(8, name_w - (_MIN_TEXT_COLUMN - (width - fixed)))
        fixed = 2 + name_w + 2 + prov_w + 2 + pkg_w + 2
    fact_budget = max(width - fixed, 1)
    fact_indent = " " * fixed
    for item, prov, pkg, fact in rows:
        segments = _wrap_segments(fact, fact_budget, (", ",))
        line = (
            f"  {_pad(_truncate(item['name'], name_w), name_w)}  "
            f"{_pad(prov, prov_w)}  {_pad(pkg, pkg_w)}  {segments[0]}"
        )
        lines.append(line.rstrip())
        for segment in segments[1:]:
            lines.append(fact_indent + segment)
        matched = _matched_text(item)
        if matched:
            lines.extend(
                _wrap_annotated("    ", matched, width, (" · ", ", "))
            )
        if verbose and item.get("description"):
            lines.append(f"    description: {item['description']}")
    return lines


def _results_block(
    items: list[dict[str, Any]], *, verbose: bool, query: str
) -> list[str]:
    kinds = {item.get("kind") or "model" for item in items}
    if len(items) > 1 and len(kinds) == 1 and "model" not in kinds:
        kind = next(iter(kinds))
        if verbose or len(items) <= _FOLD_LIMIT:
            return _kind_table(kind, items, verbose=verbose)
        # Past the fold limit the promoted table folds like
        # the grouped tier-2 sections (plan 8.2: the same
        # capping rule) - the first ten rows, the folded
        # count, and the comma-intersected narrowing query -
        # instead of printing in full, which at real scale
        # (hundreds of package macros) runs to hundreds of
        # lines. --verbose remains the unfold path, as in
        # the grouped sections.
        lines = _kind_table(kind, items[:_FOLD_LIMIT], verbose=verbose)
        lines.append(
            f"  … and {len(items) - _FOLD_LIMIT} more - "
            f"narrow with: loom search "
            f"\"{_narrowing_query(kind, _promoted_label(items), query)}\""
        )
        return lines
    models = [i for i in items if (i.get("kind") or "model") == "model"]
    others = [i for i in items if (i.get("kind") or "model") != "model"]
    lines: list[str] = []
    if models:
        lines.extend(_model_table(models, verbose=verbose))
    if others:
        if lines:
            lines.append("")
        lines.extend(_tier2_groups(others, verbose=verbose, query=query))
    return lines


def _neighbour_lines(item: dict[str, Any]) -> list[str]:
    lines = []
    for key in ("upstream", "downstream"):
        neighbours = item.get(key) or []
        if not neighbours:
            continue
        shown = neighbours[:_FOLD_LIMIT]
        text = f"{key} ({len(neighbours)}): {', '.join(shown)}"
        if len(neighbours) > len(shown):
            text += f", … and {len(neighbours) - len(shown)} more"
        lines.append(f"  {text}")
    return lines


def _model_detail(item: dict[str, Any], *, verbose: bool) -> list[str]:
    status = item.get("status") or "model"
    status_text = paint(status, _STATUS_CODES.get(status, BOLD))
    lines = [bold(f"{item['name']} ({status_text})")]
    identity = []
    if item.get("generator"):
        identity.append(f"generator: {item['generator']}")
    if item.get("pattern"):
        identity.append(f"pattern: {item['pattern']}")
    if item.get("paradigm"):
        identity.append(f"paradigm: {item['paradigm']}")
    if item.get("classification"):
        identity.append(f"classification: {item['classification']}")
    if identity:
        lines.append("  " + " · ".join(identity))
    owner_line = f"owner: {item.get('owner') or '(none)'}"
    if item.get("tags"):
        owner_line += f" · tags: {', '.join(item['tags'])}"
    lines.append(f"  {owner_line}")
    if item.get("description"):
        lines.append(f"  description: {item['description']}")
    if item.get("path"):
        lines.append(f"  path: {item['path']}")
    if item.get("output_path"):
        lines.append(f"  output: {item['output_path']}")
    if verbose and item.get("metadata_path"):
        lines.append(f"  metadata: {item['metadata_path']}")
    if item.get("keys"):
        lines.append(f"  keys: {', '.join(item['keys'])}")
    if item.get("inputs"):
        lines.append(f"  inputs: {', '.join(item['inputs'])}")
    if item.get("source_inputs"):
        lines.append(f"  sources: {', '.join(item['source_inputs'])}")
    columns = item.get("columns") or []
    if columns:
        heading = f"columns ({len(columns)}"
        if item.get("columns_partial"):
            heading += ", partial"
        lines.append(f"  {heading}):")
        for column in columns:
            if isinstance(column, dict):
                text = f"    - {column.get('name')}"
                if column.get("data_type"):
                    text += f" {column['data_type']}"
                if column.get("description"):
                    text += f" - {column['description']}"
            else:
                text = f"    - {column}"
            lines.append(text)
        total = item.get("columns_total", len(columns))
        documented = item.get("columns_documented", 0)
        lines.append(
            f"  documentation: {documented} of {total} columns documented"
        )
    elif item.get("columns_partial"):
        lines.append("  columns: not derivable (partial record)")
    upstream = item.get("upstream") or []
    downstream = item.get("downstream") or []
    lines.append(
        f"  graph: {len(upstream)} upstream, {len(downstream)} downstream"
    )
    lines.extend(_neighbour_lines(item))
    lines.append(
        "  tests: present" if item.get("has_tests") else "  tests: none recorded"
    )
    return lines


def _macro_slot_lines(arguments: list[Any]) -> list[str]:
    """The slot-compatibility account in a macro's detail view,
    derived from the declared signature alone. The one generator
    slot a macro can fill is the staging column mapping: its
    call passes the mapped column as an argument at position 1
    by default (``macro_column_param`` pins another position)
    and ``macro_args`` fill the remaining positions in signature
    order, so a call is arity-valid when the supplied count
    reaches every argument that carries no default and exceeds
    none of them - the same rule manifest verification applies
    to a declared call. The note states facts about the slot,
    never a judgement about the macro."""
    names = [
        str(argument["name"])
        for argument in arguments
        if isinstance(argument, dict) and argument.get("name")
    ]
    if not names:
        return [
            "  slots: this macro takes no arguments, so it cannot "
            "fill the staging column mapping slot - the slot "
            "passes the mapped column as an argument."
        ]
    lines = [
        "  slots: staging column mapping - the column fills "
        f"'{names[0]}' at position 1 by default; "
        "macro_column_param can place it at another position."
    ]
    required_positions = [
        index + 1
        for index, argument in enumerate(arguments)
        if isinstance(argument, dict) and not argument.get("has_default")
    ]
    reach = max(required_positions) if required_positions else 1
    if reach > 1:
        lines.append(
            f"  a mapping must supply at least {reach - 1} "
            f"macro_args alongside the column: the call has to "
            f"reach '{names[reach - 1]}' (position {reach}), the "
            "last argument without a default."
        )
    elif len(names) > 1:
        lines.append(
            "  the column alone is a complete call - every other "
            "argument carries a default."
        )
    return lines


def _macro_detail(item: dict[str, Any], *, verbose: bool) -> list[str]:
    provenance = item.get("provenance") or "project"
    heading = f"{item['name']} (macro, {provenance}"
    if item.get("package") and provenance == "package":
        heading += f" {item['package']}"
    heading += ")"
    lines = [bold(heading)]
    arguments = item.get("arguments") or []
    names = [
        str(argument["name"])
        for argument in arguments
        if isinstance(argument, dict) and argument.get("name")
    ]
    lines.append(f"  signature: {item['name']}({', '.join(names)})")
    if arguments:
        rendered = []
        for argument in arguments:
            if not isinstance(argument, dict):
                continue
            text = str(argument.get("name"))
            if argument.get("type"):
                text += f" ({argument['type']}"
                if argument.get("has_default"):
                    text += ", has default"
                text += ")"
            elif argument.get("has_default"):
                text += " (has default)"
            rendered.append(text)
        if rendered:
            lines.append(f"  arguments: {', '.join(rendered)}")
    if item.get("description"):
        lines.append(f"  description: {item['description']}")
    if item.get("path"):
        lines.append(f"  path: {item['path']}")
    used_by = item.get("used_by") or []
    lines.append(f"  used by ({len(used_by)}):")
    shown = used_by[:_FOLD_LIMIT]
    for name in shown:
        lines.append(f"    - {name}")
    if len(used_by) > len(shown):
        lines.append(f"    … and {len(used_by) - len(shown)} more")
    lines.extend(_macro_slot_lines(arguments))
    return lines


def _object_detail(item: dict[str, Any], *, verbose: bool) -> list[str]:
    kind = item.get("kind") or "object"
    provenance = item.get("provenance") or "project"
    heading = f"{item['name']} ({kind}, {provenance}"
    if item.get("package") and provenance == "package":
        heading += f" {item['package']}"
    heading += ")"
    lines = [bold(heading)]
    if item.get("description"):
        lines.append(f"  description: {item['description']}")
    if item.get("path"):
        lines.append(f"  path: {item['path']}")
    lines.extend(_neighbour_lines(item))
    return lines


def _detail_lines(item: dict[str, Any], *, verbose: bool) -> list[str]:
    kind = item.get("kind") or "model"
    if kind == "model":
        return _model_detail(item, verbose=verbose)
    if kind == "macro":
        return _macro_detail(item, verbose=verbose)
    return _object_detail(item, verbose=verbose)


def _overview_lines(payload: dict[str, Any]) -> list[str]:
    overview = payload.get("overview") or {}
    corpus = payload.get("corpus") or {}
    mode_note = _corpus_mode_note(corpus)
    project = overview.get("project") or "project"
    lines = [bold(f"Search: {project} overview ({mode_note})")]
    if corpus.get("mode") == "metadata-only":
        lines.append(
            "Corpus: metadata only - no manifest found, so "
            "unmanaged and orphaned models are unknowable here; "
            "run dbt parse to widen the corpus to every model "
            "in the project."
        )
    elif corpus.get("manifest_generated_at"):
        lines.append(_manifest_generated_line(corpus))
    total_objects = overview.get("total_objects")
    if total_objects is not None:
        # The one total the per-category census below breaks
        # down (plan 8.1: total object counts) - computed by
        # the overview builder and carried in the JSON all
        # along; the text form shows it ahead of the census.
        lines.append(f"Objects: {total_objects} total")
    models = overview.get("models") or {}
    by_status = models.get("by_status") or {}
    status_text = ", ".join(
        f"{status} {count}" for status, count in by_status.items()
    )
    lines.append(
        f"Models: {models.get('total', 0)}"
        + (f" ({status_text})" if status_text else "")
    )
    by_generator = overview.get("by_generator") or {}
    if by_generator:
        lines.append(
            "Generators: "
            + ", ".join(f"{name} {count}" for name, count in by_generator.items())
        )
    by_paradigm = overview.get("by_paradigm") or {}
    if by_paradigm:
        lines.append(
            "Paradigms: "
            + ", ".join(f"{name} {count}" for name, count in by_paradigm.items())
        )
    owners = overview.get("owners") or {}
    top = owners.get("top") or []
    owner_text = ", ".join(f"{name} {count}" for name, count in top)
    lines.append(
        "Owners: "
        + (owner_text + " · " if owner_text else "")
        + f"unowned {owners.get('unowned', 0)}"
    )
    documentation = overview.get("documentation") or {}
    lines.append(
        f"Documentation: {documentation.get('models_documented', 0)} of "
        f"{documentation.get('models_total', 0)} models described; "
        f"{documentation.get('columns_documented', 0)} of "
        f"{documentation.get('columns_total', 0)} output columns documented"
    )
    macros = overview.get("macros") or {}
    if macros.get("total"):
        macro_line = (
            f"Macros: {macros['total']} ({macros.get('project', 0)} project, "
            f"{macros.get('package', 0)} package"
        )
        by_package = macros.get("by_package") or {}
        if by_package:
            macro_line += " - " + ", ".join(
                f"{name} {count}" for name, count in by_package.items()
            )
        lines.append(macro_line + ")")
    by_kind = overview.get("objects_by_kind") or {}
    others = {
        kind: count
        for kind, count in by_kind.items()
        if kind != "macro" and count
    }
    if others:
        lines.append(
            "Other objects: "
            + ", ".join(f"{kind}s {count}" for kind, count in others.items())
        )
    suggestions = overview.get("suggested_queries") or []
    if suggestions:
        lines.append("Suggested queries:")
        for suggestion in suggestions:
            lines.append(
                f"  loom search {suggestion['query']} - "
                f"{suggestion['count']} {suggestion['label']}"
            )
    width = _adapt_width()
    if width is None:
        return lines
    wrapped = lines[:1]
    in_suggestions = False
    for line in lines[1:]:
        if line == "Suggested queries:":
            in_suggestions = True
        if in_suggestions or _visible_len(line) <= width:
            wrapped.append(line)
        else:
            wrapped.extend(_wrap_census_line(line, width))
    return wrapped


_CENSUS_LABELS = frozenset(
    {"Models", "Generators", "Paradigms", "Owners", "Macros", "Other objects"}
)


def _wrap_census_line(line: str, width: int) -> list[str]:
    """One overview census line bounded to ``width``. The
    census enumerations (``Models``/``Generators``/
    ``Paradigms``/``Owners``/``Macros``/``Other objects``)
    wrap at their entry boundaries - wrapping moves whole
    ``name count`` entries, never splits one - under a
    hanging indent aligned with the content after the label;
    the overview's prose lines (the corpus note, the manifest
    stamp, the documentation sentence) wrap at word
    boundaries the same way. An entry longer than the whole
    available width takes the ellipsis marker, as in the
    bounded tables."""
    label, sep, rest = line.partition(": ")
    indent = " " * (len(label) + 2) if sep else "  "
    if sep and label in _CENSUS_LABELS and ", " in rest:
        segments = _wrap_segments(rest, max(width - len(indent), 8), (", ",))
        return [
            f"{label}: {segments[0]}",
            *[indent + segment for segment in segments[1:]],
        ]
    return textwrap.wrap(
        line,
        width=width,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [line]


def _similar_header_lines(
    payload: dict[str, Any], *, mode_note: str
) -> list[str]:
    """The search-by-example header (plan 7.7): the reference
    model and its shape - the fingerprint the ranking compares
    against - with any components that could not be derived
    named and excluded from scoring, and the weights the
    ranking uses, stated."""
    similar = payload.get("similar_to") or {}
    fingerprint = similar.get("fingerprint") or {}
    results = payload.get("results") or []
    lines = [
        bold(
            f"Similar to {similar['name']}: {len(results)} close "
            f"matches by shape overlap ({mode_note})"
        )
    ]
    shape_parts: list[str] = []
    if fingerprint.get("keys"):
        shape_parts.append(f"keys: {', '.join(fingerprint['keys'])}")
    if fingerprint.get("inputs"):
        shape_parts.append(
            f"inputs: {', '.join(fingerprint['inputs'])}"
        )
    if fingerprint.get("columns"):
        columns = f"columns: {', '.join(fingerprint['columns'])}"
        if fingerprint.get("columns_partial"):
            columns += " (partial list)"
        shape_parts.append(columns)
    family = fingerprint.get("family") or fingerprint.get("generator")
    if family:
        shape_parts.append(f"generator family: {family}")
    if shape_parts:
        lines.append(f"Reference shape: {'; '.join(shape_parts)}")
    partial = fingerprint.get("partial_components") or []
    if partial:
        lines.append(
            "The reference shape is partial: "
            f"{', '.join(partial)} could not be derived, so the "
            "scores cover only the derivable components."
        )
    lines.append(
        "Ranked by weighted shape overlap (keys 5, inputs 4, "
        "columns 3, generator family 1; a candidate sharing "
        "none of the reference keys ranks below every "
        "candidate that shares the grain). Similarity shows "
        "close matches; it never asserts duplication."
    )
    return lines


def _exact_name_promotion(
    payload: dict[str, Any],
    results: list[dict[str, Any]],
    close: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """A bare-term query that names exactly one model. dbt
    test names embed their model's name, so a bare known
    name otherwise lands on a list the practitioner must
    scan for the one record they meant. When the query is a
    single bare term (no qualifier, no comma) and exactly
    one model in the whole result set carries that name,
    that model is promoted to its detail view and the
    remaining matches are listed compactly beneath it. Any
    other shape - zero or two-plus same-named models, a
    qualified or multi-term query - returns None and the
    ordinary list rendering stands. Detection reads only
    the payload, so selection, banding, facet counts and
    the JSON shape are untouched: the rendering decides,
    never the matching."""
    query = payload.get("query") or ""
    tokens = query.split()
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if ":" in token or "," in token:
        return None
    everyone = [*results, *close]
    if len(everyone) <= 1:
        return None
    named = [
        item
        for item in everyone
        if (item.get("kind") or "model") == "model"
        and item["name"].casefold() == token.casefold()
    ]
    if len(named) != 1:
        return None
    return named[0]


def _compact_match_lines(items: list[dict[str, Any]]) -> list[str]:
    """One line per match - name, kind and what it matched -
    for the matches listed beneath a promoted detail view."""
    lines = []
    for item in items:
        kind = item.get("kind") or "model"
        line = f"  {item['name']} ({kind})"
        matched = _matched_text(item)
        if matched:
            line += f" · {matched}"
        lines.append(line)
    return lines


def render_search_engine_text(
    payload: dict[str, Any], *, verbose: bool = False
) -> str:
    """Text rendering for selector-engine search: the
    no-query overview, the faceted result table with grouped
    tier-2 sections, the single-hit detail view, the
    bare-name detail promotion (exactly one model in the
    result set carries the query term as its name), the
    mandatory banding label when shape banding is active,
    the search-by-example header when the payload carries a
    similar_to reference, and the
    zero-result diagnosis (which criterion emptied the set,
    the facet values that do exist, and did-you-mean
    suggestions)."""
    results = payload.get("results") or []
    if payload.get("overview") is not None and not results:
        return "\n".join(_overview_lines(payload))
    close = payload.get("close_matches") or []
    corpus = payload.get("corpus") or {}
    mode_note = _corpus_mode_note(corpus)
    lines: list[str] = []
    similar = payload.get("similar_to")
    if similar:
        lines.extend(_similar_header_lines(payload, mode_note=mode_note))
    elif payload.get("banded"):
        lines.append(
            bold(
                f"Search: {len(results)} full matches, "
                f"{len(close)} close matches ({mode_note})"
            )
        )
        lines.append(
            "Shape banding is active: the query carries two or more "
            "shape qualifiers, so close matches - records satisfying "
            "some but not all of them - are shown below the full "
            "matches, ranked by shape overlap. The same selector "
            "under --select on an action command takes only the "
            "full matches."
        )
    else:
        total = len(results)
        noun = "match" if total == 1 else "matches"
        lines.append(bold(f"Search: {total} {noun} ({mode_note})"))
    generated_line = _manifest_generated_line(corpus)
    if generated_line is not None:
        lines.append(generated_line)
    if payload.get("query") and not similar:
        lines.append(f"Query: {payload['query']}")
    if len(results) == 1 and not close and not similar:
        lines.append("")
        lines.extend(_detail_lines(results[0], verbose=verbose))
        return "\n".join(lines)
    promoted = (
        None if similar else _exact_name_promotion(payload, results, close)
    )
    if promoted is not None:
        others = [
            item for item in [*results, *close] if item is not promoted
        ]
        lines.append("")
        lines.extend(_detail_lines(promoted, verbose=verbose))
        lines.append("")
        lines.append(f"Other matches ({len(others)}):")
        lines.extend(_compact_match_lines(others))
        return "\n".join(lines)
    facet = _facet_line(payload.get("facets") or {})
    if facet and (results or close):
        width = _adapt_width()
        if width is not None and _visible_len(facet) > width:
            lines.extend(_facet_lines(payload.get("facets") or {}, width))
        else:
            lines.append(facet)
    if results:
        lines.extend(
            _results_block(
                results, verbose=verbose, query=payload.get("query") or ""
            )
        )
    if close:
        lines.append("Close matches (by shape overlap):")
        lines.extend(
            _results_block(
                close, verbose=verbose, query=payload.get("query") or ""
            )
        )
    if similar:
        omitted = similar.get("omitted_no_overlap") or 0
        if omitted:
            lines.append(
                f"{omitted} further model(s) share no shape "
                f"overlap with {similar['name']} and are omitted."
            )
    for diagnosis in payload.get("diagnosis") or []:
        culprit = diagnosis.get("culprit")
        if culprit:
            lines.append(
                f"No matches: the criterion {culprit!r} in "
                f"{diagnosis.get('branch')!r} emptied the set - "
                "drop it or widen it."
            )
        else:
            lines.append(f"No matches for {diagnosis.get('branch')!r}.")
        for key, values in (diagnosis.get("facets") or {}).items():
            lines.append(f"  {key}: values present: {', '.join(values)}")
        if diagnosis.get("suggestions"):
            lines.append(
                f"  Did you mean: {', '.join(diagnosis['suggestions'])}?"
            )
    if payload.get("unmanaged_unknowable"):
        lines.append(
            "Note: unmanaged is unknowable in metadata-only "
            "mode - no manifest was read, so a model Loom has "
            "no record of cannot appear here either way; run "
            "dbt parse to widen the corpus before reading "
            "this empty set as 'there are no unmanaged models'."
        )
    return "\n".join(lines)


_PLAN_STATE_GLOSSES = {
    "controlled_by_loom": "Loom-generated",
    "known_not_controlled": "hand-written",
    "unknown_to_loom": "unknown to Loom",
}

_CONSUMER_PLURALS = {
    "exposure": "exposures",
    "test": "tests",
    "metric": "metrics",
    "semantic_model": "semantic models",
    "saved_query": "saved queries",
    "model": "models",
    "snapshot": "snapshots",
}


def render_migration_plan_text(plan: dict[str, Any], *, verbose: bool = False) -> str:
    inventory = plan["inventory"]
    direct = inventory["direct_dependents"]
    transitive = inventory["transitive_dependents"]
    consumers = inventory["observed_manifest_consumers"]
    def labelled(endpoint: dict[str, Any]) -> str:
        # The state codes become plain glosses in the
        # title; the codes themselves remain in the JSON payload.
        state = endpoint.get("state")
        gloss = _PLAN_STATE_GLOSSES.get(state) if state else None
        return f"{endpoint['name']} ({gloss})" if gloss else endpoint["name"]

    lines = [
        f"Plan: replace {labelled(plan['from'])} with {labelled(plan['to'])}",
        f"Impact: {len(direct)} direct dependents · {len(transitive)} "
        f"transitive · {len(consumers)} manifest consumers observed",
    ]
    if verbose:
        lines.append(f"  from unique_id: {plan['from']['unique_id']}")
        lines.append(f"  to unique_id: {plan['to']['unique_id']}")

    def dependent_line(item: dict[str, Any]) -> str:
        line = f"  {item['name']}"
        if item.get("state"):
            line += f" [{item['state']}]"
        if verbose:
            line += f" ({item['unique_id']}) via {' -> '.join(item['path'])}"
        return line

    _section(
        lines,
        f"Direct dependents ({len(direct)}):",
        [dependent_line(item) for item in direct],
    )
    by_distance: dict[int, list[dict[str, Any]]] = {}
    for item in transitive:
        by_distance.setdefault(item["distance"], []).append(item)
    body = []
    for distance in sorted(by_distance):
        group = by_distance[distance]
        if verbose:
            body.extend(dependent_line(item) for item in group)
        else:
            body.extend(
                _wrap(
                    f"distance {distance}: {_names(i['name'] for i in group)}"
                )
            )
    _section(lines, f"Transitive dependents ({len(transitive)}):", body)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in consumers:
        by_type.setdefault(item["resource_type"], []).append(item)
    body = []
    for resource_type in sorted(by_type):
        group = by_type[resource_type]
        plural = _CONSUMER_PLURALS.get(
            resource_type, resource_type.replace("_", " ") + "s"
        )
        body.extend(
            _wrap(
                f"{plural} ({len(group)}): {_names(i['name'] for i in group)}"
            )
        )
        if verbose:
            body.extend(
                f"    {i['name']} ({i['unique_id']})" for i in group
            )
    _section(lines, f"Manifest consumers ({len(consumers)}):", body)
    external = inventory["external_consumers"]
    lines.append("")
    lines.append(f"External consumers: {external['status']} - {external['reason']}")
    phases = [
        f"  {index}. {phase['action']}"
        for index, phase in enumerate(plan["migration_phases"], start=1)
    ]
    _section(lines, "Phases:", phases)
    _section(
        lines,
        "Limitations:",
        [f"  - {item}" for item in plan["limitations"]],
    )
    return "\n".join(lines)


def _assessment_state(model: dict[str, Any]) -> str:
    if "state" in model:
        return model["state"]
    if model.get("status") == "known to Loom":
        kind = (model.get("inventory") or {}).get("kind")
        return (
            "controlled_by_loom"
            if kind == "generated instance"
            else "known_not_controlled"
        )
    return "unknown_to_loom"


def _assessment_name(model: dict[str, Any]) -> str:
    # Version nodes of one model are listed distinctly.
    if model.get("version") is not None:
        return f"{model['name']} (v{model['version']})"
    return model["name"]


def _assessment_detail_lines(
    model: dict[str, Any], *, verbose: bool
) -> list[str]:
    """The below-the-entry detail: fit evidence and the interface
    flags in their full prose. Verbose only - in default
    mode the flags ride on the entry line as codes (with the
    report's legend) and in the header census."""
    body = []
    if not verbose:
        return body
    fit = model.get("fit") or {}
    if fit.get("evidence"):
        body.extend(f"    evidence: {e}" for e in fit["evidence"])
    for flag in model.get("interface_flags") or []:
        body.append(f"    flag: {flag}")
    readiness = model.get("readiness") or {}
    if readiness:
        holes = readiness.get("holes") or []
        state = readiness.get("state")
        if state == "ready_to_promote":
            body.append("    readiness: ready to promote")
        elif state == "needs_authoring":
            body.append(
                f"    readiness: needs authoring ({len(holes)} hole(s))"
            )
        else:
            body.append("    readiness: not draftable")
        for hole in holes:
            body.append(f"    hole ({hole.get('kind')}): {hole.get('detail')}")
        for blocker in readiness.get("blockers") or []:
            body.append(f"    blocker: {blocker}")
    return body


_ADAPTER_WARNING_PROFILE = re.compile(
    r"project's profile declares the '([^']+)' adapter"
)


def _warning_headline(warning: str) -> tuple[str, str]:
    """Split a report warning into its headline and detail body.

    The two warning families the assessment generates (adapter
    mismatch, manifest provenance) carry stable prefixes;
    the headline names the family and the body keeps the full
    generated text, wrapped - the same string the JSON payload
    carries, never paraphrased. An unrecognised warning renders
    whole under a generic headline."""
    for prefix, headline in (
        ("manifest adapter mismatch: ", "Adapter mismatch"),
        ("manifest provenance: ", "Build-produced manifest"),
    ):
        if warning.startswith(prefix):
            return headline, warning[len(prefix):]
    return "Warning", warning


def _assessment_header(
    report: dict[str, Any],
    *,
    title: str,
    headline: str,
    groups: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """The context lines both assessment renderings share.

    The report leads with the readiness distribution -
    how many of the project's non-controlled models are ready
    to promote, how many need authoring, and how many are not
    draftable. It replaces the three yields the header once led
    with (registered / scaffolded / conversion-drafted): the
    yields counted operations, and "migration" conflated them -
    on Cal-ITP the register yield was 625 of 625 and the
    convert yield 10 of 625 - while saying nothing about the
    work standing between a draft and its promotion.

    The header states the distribution and the state counts
    once each (they were previously restated across four lines),
    declares the manifest's provenance as a header field
    rather than a warning-shaped line, and gives the report
    warnings a headed section of their own.
    """
    summary = report["summary"]
    lines = [
        bold(f"{title}: {report['project']}") + mode_suffix(report),
        headline,
        f"State: {summary['project_models']} project models - "
        f"{len(groups['controlled'])} controlled · "
        f"{len(groups['known'])} known, not controlled · "
        f"{len(groups['unknown'])} unknown to Loom",
    ]
    warnings = report.get("warnings") or []
    # The manifest's compilation provenance is declared in
    # the header of both assessment renderings, parse-produced or
    # build-produced alike (a build-produced manifest also earns
    # a warning in the section below).
    provenance = report.get("manifest_provenance")
    if provenance:
        manifest_line = f"Manifest: {provenance}"
        adapter = next(
            (
                match.group(1)
                for warning in warnings
                for match in [_ADAPTER_WARNING_PROFILE.search(warning)]
                if match
            ),
            None,
        )
        if adapter is not None:
            manifest_line += f" · project adapter {adapter} (see Warnings)"
        elif warnings:
            manifest_line += " (see Warnings)"
        lines.append(manifest_line)
    seen_but_not_assessed = summary.get("seen_but_not_assessed") or {}
    if seen_but_not_assessed:
        rendered = ", ".join(
            f"{count} {resource_type}"
            for resource_type, count in sorted(seen_but_not_assessed.items())
        )
        total = sum(seen_but_not_assessed.values())
        lines.append(f"Non-model resources seen: {total} ({rendered})")
    if warnings:
        lines.append("")
        lines.append(f"Warnings ({len(warnings)}):")
        for warning in warnings:
            headline, body = _warning_headline(warning)
            lines.append(f"  ! {headline}")
            lines.extend(_wrap(body, indent="    ", subsequent="    "))
    return lines


def _model_layer_label(model: dict[str, Any]) -> str:
    """The dbt layer a report model sits in, for grouping: the
    scaffold record's layer where one exists, else the same
    layout/prefix derivation the assessment itself uses, computed
    from fields the record already carries."""
    scaffold = model.get("scaffold") or {}
    if scaffold.get("layer"):
        return str(scaffold["layer"])
    return _model_layer(model["name"], model.get("model_path")) or "unlayered"


def _flag_census_line(models: list[dict[str, Any]]) -> str | None:
    """The report-wide flag summary: how many models are flagged,
    and the flag occurrences by class. ``None`` when nothing is
    flagged."""
    flagged = sum(1 for m in models if m.get("interface_flags"))
    if not flagged:
        return None
    census = " · ".join(
        f"{label} {count}" for label, count in _flag_census(models)
    )
    return (
        f"  Flagged ({flagged} of {len(models)}): {census} "
        "(flag occurrences; per-model flags: --verbose, or JSON)"
    )


def _readiness_label(
    report: dict[str, Any], summary: dict[str, Any]
) -> str:
    """The readiness distribution as named in both commands'
    headlines.

    On a build-produced manifest the headline names the
    provenance at the point of use - the headline is the line
    a skim reads, while the provenance warning sits in the
    header above it. A parse-produced headline renders with
    no annotation."""
    distribution = summary.get("readiness") or {}
    label = (
        f"Readiness: {distribution.get('ready_to_promote', 0)} "
        f"ready to promote · "
        f"{distribution.get('needs_authoring', 0)} needs authoring · "
        f"{distribution.get('not_draftable', 0)} not draftable"
    )
    if report.get("manifest_provenance") == "build-produced":
        label += " (build-produced manifest)"
    return label


def _drafts_provenance_suffix(report: dict[str, Any]) -> str:
    """The annotation on a `--write` account.

    The CLI adds a `drafts_provenance` key to a convert report
    whose drafts were written from a build-produced manifest;
    the written sections carry the same naming here, so the
    account reads annotated at the point of writing. Empty for
    every other report."""
    provenance = report.get("drafts_provenance")
    return f" [{provenance} manifest]" if provenance else ""


def _assessment_groups(
    report: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    models = report["models"]
    return {
        "controlled": [
            m for m in models if _assessment_state(m) == "controlled_by_loom"
        ],
        "known": [
            m for m in models if _assessment_state(m) == "known_not_controlled"
        ],
        "unknown": [
            m for m in models if _assessment_state(m) == "unknown_to_loom"
        ],
        "fits": [m for m in models if "generator" in (m.get("fit") or {})],
        "scaffolded": [
            m
            for m in models
            if (m.get("scaffold") or {}).get("state")
            in ("untouched", "deleted", "missing")
        ],
        "scaffolds_promoted": [
            m
            for m in models
            if (m.get("scaffold") or {}).get("state") == "promoted"
        ],
    }


def _scaffold_inputs_label(scaffold: dict[str, Any]) -> str:
    """A one-line rendering of a scaffold's pre-wired inputs."""
    inputs = scaffold.get("inputs") or {}
    parts = list(inputs.get("models") or [])
    parts.extend(
        f"{source['source_name']}.{source['name']}"
        for source in inputs.get("sources") or []
    )
    return ", ".join(parts) if parts else "no inputs in the manifest graph"


def _controlled_section(
    lines: list[str],
    controlled: list[dict[str, Any]],
    *,
    verbose: bool,
) -> None:
    if verbose:
        body = []
        for model in controlled:
            inventory = model.get("inventory") or {}
            label = inventory.get("generator") or ""
            body.append(f"  {_assessment_name(model)} ({label})")
            if inventory.get("path"):
                body.append(f"    inventory: {inventory['path']}")
            body.extend(_assessment_detail_lines(model, verbose=verbose))
        _section(lines, f"Controlled by Loom ({len(controlled)}):", body)
    elif controlled:
        lines.append("")
        lines.append(
            f"Controlled by Loom: {len(controlled)} model(s) already "
            "generated from Loom metadata (details: --verbose or "
            "--format json)."
        )


def render_register_text(report: dict[str, Any], *, verbose: bool = False) -> str:
    """The register half of the assessment report.

    The inventory view: every model's state, the drafted
    registrations for the unknown models, and the registrations a
    `--write` run wrote. Fit and scaffold detail belongs to
    `render_convert_text`; the readiness distribution is named
    in the headline so the register story never reads as the whole
    story.
    """
    summary = report["summary"]
    groups = _assessment_groups(report)
    unknown, known, controlled = (
        groups["unknown"],
        groups["known"],
        groups["controlled"],
    )
    fits = groups["fits"]
    scaffolded = groups["scaffolded"]
    lines = _assessment_header(
        report,
        title="Register",
        headline=_readiness_label(report, summary),
        groups=groups,
    )

    census_line = _flag_census_line(report["models"])
    body = []
    if unknown:
        if verbose or len(unknown) > _FOLD_LIMIT:
            layers = Counter(_model_layer_label(m) for m in unknown)
            rendered = " · ".join(
                f"{layer} {layers[layer]}"
                for layer in ("staging", "intermediate", "mart", "unlayered")
                if layers[layer]
            )
            body.append(f"  By layer: {rendered}")
        if verbose:
            for model in unknown:
                draft = model.get("draft_registration") or {}
                label = draft.get("classification", "model")
                body.append(f"  {_assessment_name(model)} ({label})")
                if draft.get("evidence"):
                    body.append(
                        f"    classification evidence: {draft['evidence']}"
                    )
                body.extend(_assessment_detail_lines(model, verbose=True))
        elif len(unknown) > _FOLD_LIMIT:
            body.extend(
                _folded_name_lines(
                    [_assessment_name(m) for m in unknown], verbose=False
                )
            )
        else:
            for model in unknown:
                draft = model.get("draft_registration") or {}
                label = draft.get("classification", "model")
                body.append(f"  {_assessment_name(model)} ({label})")
        if census_line is not None:
            body.append(census_line)
            census_line = None
    _section(
        lines,
        f"Unknown to Loom - action needed ({len(unknown)}):",
        body,
    )

    body = []
    if verbose:
        for model in known:
            inventory = model.get("inventory") or {}
            label = inventory.get("generator") or inventory.get("kind") or ""
            body.append(f"  {_assessment_name(model)} ({label})")
            if inventory.get("path"):
                body.append(f"    inventory: {inventory['path']}")
            body.extend(_assessment_detail_lines(model, verbose=True))
    elif len(known) > _FOLD_LIMIT:
        body.extend(
            _folded_name_lines([_assessment_name(m) for m in known], verbose=False)
        )
    else:
        for model in known:
            inventory = model.get("inventory") or {}
            label = inventory.get("generator") or inventory.get("kind") or ""
            body.append(f"  {_assessment_name(model)} ({label})")
    if census_line is not None:
        body.append(census_line)
        census_line = None
    _section(lines, f"Known to Loom, not controlled ({len(known)}):", body)

    _controlled_section(lines, controlled, verbose=verbose)
    if census_line is not None:
        lines.append(census_line)

    written = report.get("registrations_written")
    if written:
        body = [f"  {path}" for path in written[:_FOLD_LIMIT]]
        if not verbose and len(written) > _FOLD_LIMIT:
            body.append(
                _fold_note(
                    _FOLD_LIMIT, len(written), unfold="--format json"
                )
            )
        elif verbose:
            body = [f"  {path}" for path in written]
        _section(
            lines,
            f"Registration drafts written ({len(written)}):",
            body,
        )
    next_parts = []
    if unknown:
        if written:
            # A write run just wrote these drafts: the closing
            # line must point forward, not repeat the instruction
            # the run already carried out (QOL FR; the convert
            # write output's forward-pointing Next is the model).
            next_parts.append(
                "register - the drafted registrations are written: "
                "review them under local/registrations/, then run "
                "`loom validate` to confirm the inventory reads "
                "them"
            )
        else:
            next_parts.append(
                "register - review the drafted registrations, then run "
                "`loom register --write` to write them"
            )
    if scaffolded:
        next_parts.append(
            f"scaffold - {len(scaffolded)} model(s) in the "
            "hand-authored layers have scaffold drafts (starting "
            "instances with the transform as the named hole): run "
            "`loom convert --write` to write them"
        )
    if fits:
        next_parts.append(
            f"convert - {len(fits)} model(s) have a generator fit: run "
            "`loom convert` for their conversion drafts, and follow "
            "the equivalence proof in docs/adoption-playbook.md "
            "before swapping any model"
        )
    if next_parts:
        lines.append("")
        lines.append("Next: " + "; ".join(next_parts) + ".")
    return "\n".join(lines)


def render_convert_text(report: dict[str, Any], *, verbose: bool = False) -> str:
    """The convert half of the assessment report.

    Fits first: fits were once interleaved through the state
    sections - on Cal-ITP the
    first fit sat at line 902 of 1,326 and a practitioner triaging
    conversions had to read the whole register list to find them.
    The scaffold sections follow (starting instances for
    the layered models no rule matched, then any scaffolds a
    re-assessment finds promoted), then the no-fit record with
    each remaining model's named reason; the register detail
    belongs to `render_register_text`.
    """
    summary = report["summary"]
    groups = _assessment_groups(report)
    controlled = groups["controlled"]
    fits = groups["fits"]
    scaffolded = groups["scaffolded"]
    promoted = groups["scaffolds_promoted"]
    fit_ids = {id(m) for m in fits}
    scaffold_ids = {id(m) for m in scaffolded} | {id(m) for m in promoted}
    no_fits = [
        m
        for m in report["models"]
        if _assessment_state(m) != "controlled_by_loom"
        and id(m) not in fit_ids
        and id(m) not in scaffold_ids
    ]
    lines = _assessment_header(
        report,
        title="Convert",
        headline=_readiness_label(report, summary),
        groups=groups,
    )

    if not verbose:
        legend = _flag_legend([*fits, *no_fits])
        if legend:
            lines.append("")
            lines.extend(legend)

    def fit_line(model: dict[str, Any]) -> str:
        fit = model.get("fit") or {}
        if verbose:
            line = (
                f"  {_assessment_name(model)} ({_assessment_state(model)}) "
                f"- convert: {fit['generator']}"
            )
        else:
            line = (
                f"  {_assessment_name(model)} "
                f"- convert: {fit['generator']}"
            )
        # A fit proposed on a build-produced manifest is
        # marked at its own entry, rendered from the same per-fit
        # field the JSON record carries, so the entry is
        # distinguishable without returning to the header warning.
        if fit.get("manifest_provenance"):
            line += f" [{fit['manifest_provenance']} manifest]"
        if not verbose:
            line += _flag_codes(model)
        return line

    body = []
    shown_fits = fits if verbose else fits[:_FOLD_LIMIT]
    for model in shown_fits:
        body.append(fit_line(model))
        body.extend(_assessment_detail_lines(model, verbose=verbose))
    if not verbose and len(fits) > _FOLD_LIMIT:
        body.append(
            _fold_note(len(shown_fits), len(fits), unfold="--verbose, or --format json")
        )
    _section(
        lines,
        f"Conversion drafts proposed ({len(fits)}):",
        body,
    )

    body = []
    if verbose:
        for model in scaffolded:
            scaffold = model.get("scaffold") or {}
            line = (
                f"  {_assessment_name(model)} ({_assessment_state(model)}) "
                f"- scaffold: {scaffold.get('layer')} layer, target "
                f"{scaffold.get('target_generator')}; inputs: "
                f"{_scaffold_inputs_label(scaffold)}"
            )
            # A draft set that was deleted by hand, in whole
            # or in part, is named as such rather than listed as
            # though the draft still sat on disk untouched.
            if scaffold.get("state") == "deleted":
                line += (
                    " - scaffold draft deleted: part of its draft set "
                    "is missing on disk"
                )
            elif scaffold.get("state") == "missing":
                line += (
                    " - scaffold draft missing: no draft set on disk "
                    "for this model"
                )
            body.append(line)
            body.extend(_assessment_detail_lines(model, verbose=True))
    elif scaffolded:
        # The scaffold section's information is the layer ×
        # target table; per-model detail lives in --verbose / JSON.
        table: Counter[tuple[str, str]] = Counter()
        for model in scaffolded:
            scaffold = model.get("scaffold") or {}
            table[(str(scaffold.get("layer")), str(scaffold.get("target_generator")))] += 1
        rendered = " · ".join(
            f"{layer} → {target}: {count}"
            for (layer, target), count in sorted(
                table.items(), key=lambda item: (-item[1], item[0])
            )
        )
        body.extend(_wrap(rendered))
        flagged = sum(1 for m in scaffolded if m.get("interface_flags"))
        if flagged:
            body.append(
                f"  Flagged scaffolds: {flagged} of {len(scaffolded)} "
                "(per-model detail: --verbose, or --format json)"
            )
        names = [_assessment_name(m) for m in scaffolded]
        sample = ", ".join(names[:5])
        if len(names) > 5:
            sample = f"First 5: {sample}"
        body.extend(_wrap(sample))
        # State honesty survives the grouping: draft sets a
        # hand deleted, in whole or in part, are still named.
        for state, label in (
            ("deleted", "Deleted draft sets"),
            ("missing", "Missing draft sets"),
        ):
            named = [
                _assessment_name(m)
                for m in scaffolded
                if (m.get("scaffold") or {}).get("state") == state
            ]
            if not named:
                continue
            if len(named) > _FOLD_LIMIT:
                body.append(f"  {label} ({len(named)}):")
                body.extend(_folded_name_lines(named, verbose=False))
            else:
                body.extend(_wrap(f"{label} ({len(named)}): {_names(named)}"))
    _section(
        lines,
        f"Scaffolds drafted ({len(scaffolded)}):",
        body,
    )

    body = []
    if verbose:
        for model in promoted:
            body.append(
                f"  {_assessment_name(model)} - scaffold promoted: the "
                "model is now controlled by Loom"
            )
            body.extend(_assessment_detail_lines(model, verbose=True))
    elif len(promoted) > _FOLD_LIMIT:
        body.extend(
            _folded_name_lines(
                [_assessment_name(m) for m in promoted], verbose=False
            )
        )
    else:
        for model in promoted:
            body.append(
                f"  {_assessment_name(model)} - scaffold promoted: the "
                "model is now controlled by Loom"
            )
    _section(lines, f"Scaffolds promoted ({len(promoted)}):", body)

    body = []
    shown_no_fits = no_fits if verbose else no_fits[:_FOLD_LIMIT]
    for model in shown_no_fits:
        fit = model.get("fit") or {}
        reason = fit.get("no_fit")
        suffix = f" - no fit: {reason}" if reason else ""
        if verbose:
            line = (
                f"  {_assessment_name(model)} ({_assessment_state(model)})"
                f"{suffix}"
            )
        else:
            line = f"  {_assessment_name(model)}{suffix}{_flag_codes(model)}"
        body.append(line)
        body.extend(_assessment_detail_lines(model, verbose=verbose))
    if not verbose and len(no_fits) > _FOLD_LIMIT:
        body.append(
            _fold_note(
                len(shown_no_fits), len(no_fits), unfold="--verbose, or --format json"
            )
        )
    _section(lines, f"No fit, no layer ({len(no_fits)}):", body)

    _controlled_section(lines, controlled, verbose=verbose)

    drafts_written = report.get("drafts_written")
    if drafts_written:
        body = [f"  {path}" for path in drafts_written[:_FOLD_LIMIT]]
        if verbose:
            body = [f"  {path}" for path in drafts_written]
        elif len(drafts_written) > _FOLD_LIMIT:
            body.append(
                _fold_note(
                    _FOLD_LIMIT, len(drafts_written), unfold="--format json"
                )
            )
        _section(
            lines,
            f"Drafts written ({len(drafts_written)})"
            f"{_drafts_provenance_suffix(report)}:",
            body,
        )
    divergences = report.get("draft_divergences")
    if divergences:
        body = [
            f"  {entry['model']}: {entry['file']} - existing draft "
            "kept; it differs from the draft this assessment would "
            "write today (review it, or delete it and re-run "
            "--write to refresh)"
            for entry in divergences[:_FOLD_LIMIT]
        ]
        if verbose:
            body = [
                f"  {entry['model']}: {entry['file']} - existing "
                "draft kept; it differs from the draft this "
                "assessment would write today (review it, or "
                "delete it and re-run --write to refresh)"
                for entry in divergences
            ]
        elif len(divergences) > _FOLD_LIMIT:
            body.append(
                _fold_note(
                    _FOLD_LIMIT, len(divergences), unfold="--format json"
                )
            )
        _section(
            lines, f"Draft divergences ({len(divergences)}):", body
        )
    transitioned = [
        m for m in report["models"] if m.get("readiness_transition")
    ]
    if transitioned:
        state_words = {
            "ready_to_promote": "ready to promote",
            "needs_authoring": "needs authoring",
            "not_draftable": "not draftable",
        }
        body = []
        for model in transitioned:
            transition = model["readiness_transition"]
            before, after = transition["from"], transition["to"]
            body.append(
                f"  {_assessment_name(model)}: "
                f"{state_words.get(before['state'], before['state'])} "
                f"({before['holes']} hole(s)) → "
                f"{state_words.get(after['state'], after['state'])} "
                f"({after['holes']} hole(s))"
            )
        _section(
            lines,
            f"Readiness transitions ({len(transitioned)}):",
            body,
        )
    next_parts = []
    if fits:
        next_parts.append(
            "convert - follow the equivalence proof in "
            "docs/adoption-playbook.md before swapping any model"
        )
    if scaffolded:
        next_parts.append(
            "scaffold - author each scaffold's transform against "
            "its declared interface and prove it before promoting "
            "(the scaffold pathway in docs/adoption-playbook.md)"
        )
    if groups["unknown"]:
        next_parts.append(
            "register - registering a model withdraws it from "
            "convert's drafting pool: register the models you "
            "intend to keep hand-written with "
            "`loom register --write`"
        )
    if next_parts:
        lines.append("")
        lines.append("Next: " + "; ".join(next_parts) + ".")
    return "\n".join(lines)


def render_inspect_text(result: dict[str, Any], *, verbose: bool = False) -> str:
    sources = result["sources"]
    lines = [
        bold(
            f"Source inspection: {result['project']} - {len(sources)} source(s)"
        )
    ]
    shown = sources if verbose else sources[:_FOLD_LIMIT]
    for source in shown:
        models = sorted(
            {m for item in source["mapped_columns"] for m in item["models"]}
        )
        lines.append(
            f"  {source['source']}.{source['table']} "
            f"({source['database']}.{source['schema']}.{source['identifier']}): "
            f"{len(source['columns'])} columns, "
            f"{len(source['mapped_columns'])} mapped"
            + (f", used by {_names(models)}" if models else "")
        )
        if verbose:
            for column in source["columns"]:
                mapped = (
                    f" <- {_names(column['mapped_by'])}" if column["mapped_by"] else ""
                )
                lines.append(
                    f"    {column['ordinal']}. {column['name']} "
                    f"{column['type']}{mapped}"
                )
    if not verbose and len(sources) > _FOLD_LIMIT:
        lines.append(
            _fold_note(len(shown), len(sources), unfold="--verbose, or --format json")
        )
    return "\n".join(lines)


def render_compare_text(report: dict[str, Any], *, verbose: bool = False) -> str:
    changes = report["changes"]
    if not changes:
        return bold(f"Source structure: {report['project']} - no changes")
    lines = [
        bold(f"Source structure: {report['project']} - {len(changes)} change(s)")
    ]
    by_table: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for change in changes:
        by_table.setdefault((change["source"], change["table"]), []).append(change)
    for (source, table), group in sorted(by_table.items()):
        lines.append(f"  {source}.{table}:")
        for change in group:
            label = change["change"]
            subject = change.get("column") or ""
            detail = ""
            if "from" in change and "to" in change:
                detail = f" ({change['from']} -> {change['to']})"
            lines.append(f"    {label} {subject}{detail}".rstrip())
            if verbose and change.get("mapped_by"):
                lines.append(f"      mapped by: {_names(change['mapped_by'])}")
    return "\n".join(lines)
