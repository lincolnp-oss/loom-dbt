"""Migration assessment for existing dbt projects (ADR 0025).

Reads a project's dbt manifest and its current Loom inventory and
reports, per project-owned model: what Loom already knows, a drafted
registration for what it does not, a generator-fit proposal (a
heuristic match against the model's output shape, with recorded
evidence and drafted parameters) where a crisp shape matches, and
the interface commitments a conversion would put at risk. Every model carries an explicit state:
``controlled_by_loom`` (a generated instance exists),
``known_not_controlled`` (a practitioner registration exists), or
``unknown_to_loom`` (manifest only). Fit proposals are computed for
registered models too, and the report counts them - but
registration is the conversion gate: ``loom convert``
drafts only unknown models by default, because registering a
model is how a practitioner withdraws it from conversion, and
deleting a registration returns it to the drafting pool;
``--include-registered`` restores the pre-gate drafting reach
exactly. (This reverses the stance this module once documented
- that a registration proved inventory knowledge only, not that
a model should stay hand-written.) The report writes
nothing; drafted registrations and conversion drafts are
written only on explicit request, and conversions are gated on the
equivalence proof in docs/adoption-playbook.md. The flags
reach beyond the original four: materialization/incremental
configuration, group membership, deprecation dates, grants and
physical warehouse configuration are all flagged; consumer/test
counts include the semantic-layer and unit-test manifest
sections, and the consumer count is decomposed by kind;
non-model resources are reported as seen-but-not-assessed
counts, the semantic-layer sections among them; the metrics
and saved queries reachable from each model via its semantic
models are named in its record and flags; dbt model version
nodes are
distinguished in the records; and documented descriptions are
carried into conversion drafts. The fit rules are tightened to
the model's output shape: signals are evaluated on what the
model outputs, not on tokens anywhere in the file. The report
also carries a provenance warning when the manifest's recorded
adapter does not match the adapter the project's profile
declares - the signature of a substitute-adapter parse.
Declared facts are copied verbatim into the drafts instead of
leaving them to be retyped or flagged as losable: the
manifest's model description and ``meta.owner`` into
registrations and conversion drafts, documented column
descriptions and types into the drafted ``column_docs`` /
``column_types`` parameters, a drafted model's declared
incremental configuration into its ``incremental`` block, and
all of the source model's SQL comments - leading and inline,
in source order - into the draft as a source-rationale
note (all of them, not the leading block alone: real rationale
lives inline, and selecting among comments would be
interpretation, which is out of scope here). Copies are never
summarised or interpreted; a fact the project does not declare
stays a TODO. Convert emits ONE draft class: every
assessed model that earns a draft - a generator fit, or a
layered model no fit rule matched - gets a single unified
draft under ``local/drafts/``, outside the load path: a draft
record (the per-parameter evidence basis for a fit; for a
no-fit model, the no-fit reason, the layer, the pre-wired
input set and the transform as the single named authoring
hole), the declared interface copied verbatim as standard
furniture of every draft (an enforced contract's
columns/types/constraints, dbt unit tests' given/expect
fixtures, generic tests, semantic models, and model config -
the copy scaffolds once carried alone, now attached to fits
too), a final-form instance document, and, when the project
has no pattern for the generator, one synthesised pattern
definition per generator for the run under its FINAL name
(``<generator>_pattern``). Drafts are authored in final form:
sections map 1:1 to destination files, instances
reference the pattern's final name from the start, and
promotion is a file move plus registration cleanup - no
content edits, and no rename step, at promotion time. Project
reporting is the readiness distribution, replacing the
three yields (``registered`` / ``scaffolded`` /
``conversion_drafted``) the summary once headlined: the yields
counted operations and differed by orders of magnitude on real
projects (Cal-ITP: 625 registered, 10 conversion-drafted), but
said nothing about the work standing between a draft and
promotion. Every non-controlled model carries a derived
readiness account - ``ready_to_promote`` (zero holes, zero
blockers, strict), ``needs_authoring`` (the holes are named,
each an *authoring* hole only the practitioner can supply or
an *evidence* hole Loom could not verify from the project's
declared evidence), or ``not_draftable`` (no draft, or a named
blocker a generator cannot express even once authored) - and
the summary reports the distribution across the three states.
Readiness is derived, not stored: it lives in the draft record
and in convert's JSON, never inside the instance, and every
run re-derives it for unpromoted drafts from their current
content plus current evidence, reporting transitions against
the account the on-disk record carries; an evidence hole
closes from either side, by editing the draft or by improving
the project's evidence. Promotion itself is unpoliced - the
validator's rejection of unfilled ``__TODO__`` sentinels is
the hard backstop. An UNNEST counts as a join
signal, and a bronze pass-through claim carries a caveat when
the manifest documents no source columns to verify it against.
The CLI over this engine is split into the flat verbs ``loom
register`` (this report's inventory half; ``--write``
writes registrations only) and ``loom convert`` (the draft
half; ``--write`` writes unified drafts only); there is
no separate migration command group, and the report payload
itself is unchanged by the split. A re-assessment reports each
no-fit draft candidate's state - ``untouched`` or
``promoted`` - derived locally from the drafts directory and
the load path; nothing is persisted or sent anywhere for
this. The fit rules are
hardened in the abstain-only direction - no rule reaches
further than it did: the report declares the manifest's
compilation provenance (``manifest_provenance``:
``parse-produced`` when no model node carries compiled SQL -
``dbt parse`` does not compile model SQL - and
``build-produced`` otherwise), warning when a build-produced
manifest is assessed because fit calibration assumes a
parse-produced one; a silver candidate whose dedup QUALIFY
predicate carries an OR escape abstains, since the escape
retains rows beyond the dedup winner that the silver contract
cannot express; a single-source candidate whose SELECT lists
extract dotted struct fields abstains instead of drafting the
extractions as flat identity renames; a single-source candidate
carrying a row filter (WHERE), a plain-SQL column expression or
a LIMIT abstains with the construct named, since a fit draft
cannot carry any of the three - the staging generator can
express a row filter as authored vocabulary (ADR 0038), but
convert does not draft one, and bronze cannot express one - and
a draft ignoring them would silently drop them; and the
documented-columns caveat is generalised - every generator's
draft evidence says explicitly when the manifest documents no
source (or input) columns, or only some of them, for the
columns a draft assumes. A staging draft never guesses a
column mapping it cannot pair: outputs that match no documented
source column (beyond the single guarded rename inference) are
drafted as unresolved placeholders (`source: __TODO__`), the
evidence names the unresolved outputs and the candidate source
columns, and the validator rejects the sentinel, so a
placeholder cannot survive promotion. The declared-interface copy is
complete: every generic test a project declares -
project-defined kinds included - is copied verbatim into the
scaffold interface (an unreadable one is marked ``uncopied``,
never dropped silently). Provenance is carried to the point of
use on build-produced manifests (annotate, do not gate):
per-fit ``manifest_provenance`` in JSON, the provenance named
in the text conversion yield and on each per-fit entry, and
``drafts_provenance`` on a ``--write`` report.
"""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from loom.dialects import canonical_adapter_name, detect_profile_adapter
from loom.errors import LoomError
from loom.inventory import validate_project
from loom.inventory_records import _validate_record
from loom.manifest import (
    manifest_macro_index,
    read_manifest,
    validate_model_freshness,
    verify_macro_reference,
)
from loom.output_layout import (
    expected_model_path,
    primary_model_root,
    resolve_output_subpath,
)
from loom.project_files import _configured_paths, _project_config

_AGGREGATION = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)
_JOIN = re.compile(r"\bjoin\b", re.IGNORECASE)
# UNNEST flattens an array column into rows - a
# join in effect, whatever the FROM clause calls it. A
# re-run found staging evidence claiming "no join signals" over
# UNNEST-flatten models whose output grain the flattening changes.
_UNNEST = re.compile(r"\bunnest\s*\(", re.IGNORECASE)
_WINDOW = re.compile(r"\bover\s*\(", re.IGNORECASE)
_DISTINCT = re.compile(r"select\s+distinct\b", re.IGNORECASE)
_ROW_NUMBER_CALL = re.compile(r"row_number\s*\(\s*\)\s*over\s*\(", re.IGNORECASE)
# A plain column reference, optionally qualified (`a.account_id`,
# `"a"."account_id"`, `` `t`.`col` ``, `[t].[col]`).
_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w]*)'
_PLAIN_COLUMN = re.compile(rf"^{_IDENT}(?:\s*\.\s*{_IDENT})*$")
# A SELECT-list item a staging column mapping can express: a plain
# column (or a star) with an optional rename alias. Anything else
# in the list - a function call, an operator expression, a CASE, a
# literal - computes the column's value in plain SQL, which the
# documented-columns drafting cannot see and a mapping cannot
# express (the expression signal).
_PLAIN_PROJECTION = re.compile(
    rf"^(?:\*|{_IDENT}\s*\.\s*\*|{_IDENT}(?:\s*\.\s*{_IDENT})*)"
    rf"(?:\s+(?:as\s+)?{_IDENT})?$",
    re.IGNORECASE,
)
_ROW_FILTER = re.compile(r"\bwhere\b", re.IGNORECASE)
_LIMIT_CLAUSE = re.compile(r"\blimit\b|\bfetch\s+first\b", re.IGNORECASE)
# Words that end a SELECT list or a FROM clause at the same nesting
# depth.
_CLAUSE_KEYWORDS = frozenset(
    {
        "from",
        "where",
        "group",
        "having",
        "qualify",
        "order",
        "limit",
        "offset",
        "window",
        "union",
        "intersect",
        "except",
        "fetch",
    }
)
# Words that may follow a window construct without being its alias.
_NOT_AN_ALIAS = _CLAUSE_KEYWORDS | {"as", "select", "on", "case", "when"}

_CLASSIFICATION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("stg_", "staging"),
    ("hub_", "data-vault hub"),
    ("link_", "data-vault link"),
    ("sat_", "data-vault satellite"),
    ("dim_", "kimball dimension"),
    ("fct_", "kimball fact"),
    ("fact_", "kimball fact"),
    ("bronze_", "medallion bronze"),
    ("silver_", "medallion silver"),
    ("gold_", "medallion gold"),
    ("obt_", "one big table"),
    ("mart_", "mart"),
)


def _classification(name: str, materialized: str | None) -> tuple[str, str]:
    for prefix, label in _CLASSIFICATION_PREFIXES:
        if name.startswith(prefix):
            return label, f"name prefix {prefix!r} matches the {label} convention"
    if materialized:
        return "model", (
            f"no naming-convention prefix; dbt materialization is "
            f"{materialized!r}"
        )
    return "model", "no naming-convention prefix and no materialization recorded"


_BENIGN_JINJA = re.compile(
    r"\{\{\s*(ref|source|config)\b.*?\}\}|\{%\s*config\b.*?%\}",
    re.IGNORECASE | re.DOTALL,
)


def _signals(sql: str) -> list[str]:
    """Shape signals in a model's SQL. Benign Jinja (ref/source/config
    calls, which every generated model also uses) is stripped first;
    anything Jinja-shaped that remains is a real macro signal."""
    cleaned = _BENIGN_JINJA.sub("", sql)
    found = []
    if _AGGREGATION.search(cleaned):
        found.append("aggregation (GROUP BY)")
    if _JOIN.search(cleaned):
        found.append("a JOIN")
    if _UNNEST.search(cleaned):
        found.append("an UNNEST")
    if _WINDOW.search(cleaned):
        found.append("a window function")
    if "{{" in cleaned or "{%" in cleaned:
        found.append("Jinja/macro usage beyond ref/source/config")
    return found


# --- Output-shape analysis -------------------------------------
#
# The fit rules used to read tokens anywhere in
# a file (a final `SELECT *` for bronze, a `DISTINCT`/`ROW_NUMBER`
# token for silver), so the ubiquitous CTE-wrap idiom - real work in
# the CTEs, `SELECT *` at the end - produced semantic false
# positives. The helpers below analyse what a model *outputs*: the
# statement chain that feeds the final SELECT, every SELECT list in
# the model, and whether a dedup construct's filter binds that final
# output. The analysis is deliberately textual (balanced-paren and
# top-level-keyword scans), like the rest of this module.


def _matching_paren(sql: str, open_index: int) -> int:
    """Index just past the paren matching ``sql[open_index]``."""
    depth = 0
    for index in range(open_index, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(sql)


def _top_level_keyword(sql: str, keyword: str) -> int | None:
    """Offset of ``keyword`` at paren depth 0, or None."""
    depth = 0
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "(":
            depth += 1
            index += 1
        elif char == ")":
            depth -= 1
            index += 1
        elif depth == 0 and (char.isalpha() or char == "_"):
            end = index
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            if sql[index:end].lower() == keyword:
                return index
            index = end
        else:
            index += 1
    return None


def _split_top_level(text: str) -> list[str]:
    """Split a comma list on commas at paren depth 0."""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _select_lists(sql: str) -> list[str]:
    """Every SELECT list in the SQL, at any nesting depth, in order."""
    positions: list[tuple[int, int]] = []
    depth = 0
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "(":
            depth += 1
            index += 1
        elif char == ")":
            depth -= 1
            index += 1
        elif char.isalpha() or char == "_":
            end = index
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            if sql[index:end].lower() == "select":
                positions.append((end, depth))
            index = end
        else:
            index += 1
    lists: list[str] = []
    for start, base in positions:
        depth = base
        index = start
        stop = len(sql)
        while index < len(sql):
            char = sql[index]
            if char == "(":
                depth += 1
                index += 1
            elif char == ")":
                if depth == base:
                    stop = index
                    break
                depth -= 1
                index += 1
            elif char == ";":
                stop = index
                break
            elif depth == base and (char.isalpha() or char == "_"):
                end = index
                while end < len(sql) and (
                    sql[end].isalnum() or sql[end] == "_"
                ):
                    end += 1
                if sql[index:end].lower() in _CLAUSE_KEYWORDS:
                    stop = index
                    break
                index = end
            else:
                index += 1
        lists.append(sql[start:stop])
    return lists


def _list_is_passthrough(list_text: str, *, allow_window_item: bool = False) -> bool:
    """A SELECT list of only `*` / qualified stars / plain columns.

    Anything else - a function call, a CASE, an `expr AS alias`
    rename, arithmetic - computes or renames a column, so the list
    is not a pass-through. ``allow_window_item`` additionally permits
    the ROW_NUMBER dedup item itself (for the statement that defines
    it).
    """
    text = re.sub(r"^(distinct|all)\b", "", list_text.strip(), flags=re.IGNORECASE)
    items = _split_top_level(text)
    if not items:
        return False
    for item in items:
        if item == "*" or item.endswith(".*"):
            continue
        if allow_window_item and _ROW_NUMBER_CALL.search(item):
            continue
        if _PLAIN_COLUMN.match(item):
            continue
        return False
    return True


def _split_ctes(sql: str) -> tuple[dict[str, str], str]:
    """Split WITH-prefixed SQL into (CTE bodies by name, final statement)."""
    text = sql.strip()
    if not re.match(r"with\b", text, flags=re.IGNORECASE):
        return {}, text
    ctes: dict[str, str] = {}
    index = len("with")
    definition = re.compile(rf"\s*,?\s*({_IDENT})\s+as\s*\(", re.IGNORECASE)
    while True:
        match = definition.match(text, index)
        if not match:
            break
        open_index = match.end() - 1
        close = _matching_paren(text, open_index)
        name = match.group(1).strip('"`[]').lower()
        ctes[name] = text[open_index + 1: close - 1]
        index = close
    return ctes, text[index:].strip()


def _from_target(statement: str) -> str | None:
    """The bare-name top-level FROM target of a statement, if any."""
    index = _top_level_keyword(statement, "from")
    if index is None:
        return None
    rest = statement[index + len("from"):].lstrip()
    if rest.startswith("("):
        return None
    match = re.match(rf"({_IDENT})", rest)
    if not match:
        return None
    return match.group(1).strip('"`[]').lower()


def _output_chain(sql: str) -> list[str]:
    """Statements feeding the model's output, final statement first.

    Walks from the final statement backwards through single bare-name
    FROM targets that resolve to CTEs. A statement whose FROM is a
    subquery, a ref/source (stripped Jinja), or a join ends the walk -
    statements not on this chain cannot shape the model's output.
    """
    ctes, final = _split_ctes(sql)
    chain = [final]
    seen: set[str] = set()
    current = final
    while True:
        target = _from_target(current)
        if target is None or target in seen or target not in ctes:
            return chain
        seen.add(target)
        chain.append(ctes[target])
        current = ctes[target]


def _final_is_passthrough(sql: str) -> bool:
    """The model's final SELECT lists only unchanged columns and
    reads a single FROM target with no join."""
    _, final = _split_ctes(sql)
    lists = _select_lists(final)
    if not lists or not _list_is_passthrough(lists[0]):
        return False
    if _top_level_keyword(final, "join") is not None:
        return False
    from_index = _top_level_keyword(final, "from")
    if from_index is None:
        return False
    tail = final[from_index:]
    depth = 0
    for char in tail:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            return False
    return True


# A plain column reference followed by its rename alias (with or
# without AS) - the alias group names the item's output.
_ALIASED_PROJECTION = re.compile(
    rf"^(?:{_IDENT}(?:\s*\.\s*{_IDENT})*)\s+(?:as\s+)?({_IDENT})$",
    re.IGNORECASE,
)


def _projection_output_name(item: str) -> str | None:
    """The output name one final-projection SELECT item produces,
    or None when the item does not name its output plainly.

    A plain column names itself (unqualified); a renamed column
    names its alias; a macro invocation names its alias. Anything
    else - an expression, a literal, an unaliased macro call -
    does not name an output a column mapping can account for
    mechanically."""
    item = item.strip()
    if "{{" in item or "{%" in item:
        match = _MACRO_ITEM.match(item)
        if match is None or not match.group(3):
            return None
        return match.group(3).strip('"`[]')
    if _PLAIN_COLUMN.match(item):
        return _unqualified_column(item)
    match = _ALIASED_PROJECTION.match(item)
    if match is not None:
        return match.group(1).strip('"`[]')
    return None


def _projected_outputs(sql: str) -> list[str] | None:
    """The output column names the model's final projection
    produces, in projection order - or None when the projection's
    shape cannot be enumerated mechanically.

    The walk follows the output chain: the final statement's
    SELECT list names the outputs unless it is a bare star, in
    which case the statement it reads (a CTE on the chain) names
    them, and so on. Enumeration fails (None) when a star is
    mixed with explicit items, when the chain ends at a star
    over a source/ref/subquery (the outputs are then whatever
    the read relation carries - not enumerable from the SQL),
    or when an item does not name its output plainly. Callers
    treat None as a bound: the draft falls back to a named
    evidence hole rather than claiming a completeness it
    cannot verify."""
    cleaned = _strip_comments(_BENIGN_JINJA.sub("", sql))
    for statement in _output_chain(cleaned):
        lists = _select_lists(statement)
        if not lists:
            return None
        text = re.sub(
            r"^(distinct|all)\b", "", lists[0].strip(), flags=re.IGNORECASE
        )
        items = [item.strip() for item in _split_top_level(text)]
        items = [item for item in items if item]
        if not items:
            return None
        if all(item == "*" or item.endswith(".*") for item in items):
            continue
        outputs: list[str] = []
        for item in items:
            if item == "*" or item.endswith(".*"):
                return None
            name = _projection_output_name(item)
            if name is None:
                return None
            if name not in outputs:
                outputs.append(name)
        return outputs
    return None


def _bronze_blockers(sql: str, signals: list[str]) -> list[str]:
    """Why a single-source model is not a plain bronze pass-through.

    Checked across the *whole* model: a transform, rename,
    DISTINCT, UNION or window anywhere - typically inside a CTE whose
    final statement is a bare `SELECT *` - means the output is not
    the source unchanged, whatever the final statement looks like.
    """
    cleaned = _BENIGN_JINJA.sub("", sql)
    blockers = list(signals)
    if _DISTINCT.search(cleaned):
        blockers.append("SELECT DISTINCT")
    if re.search(r"\bunion\b", cleaned, flags=re.IGNORECASE):
        blockers.append("a UNION")
    if any(not _list_is_passthrough(lst) for lst in _select_lists(cleaned)):
        blockers.append("a column transform or rename in a SELECT list")
    if not _final_is_passthrough(cleaned):
        blockers.append("the final SELECT is not a plain pass-through")
    seen: set[str] = set()
    unique: list[str] = []
    for blocker in blockers:
        if blocker not in seen:
            seen.add(blocker)
            unique.append(blocker)
    return unique


def _bronze_output_matches(
    sql: str, columns: list[str], source_columns: list[str]
) -> bool:
    """Whether the final SELECT's list is the source unchanged:
    a star, or explicit columns the manifest documents identically
    on the model and its source."""
    cleaned = _BENIGN_JINJA.sub("", sql)
    _, final = _split_ctes(cleaned)
    lists = _select_lists(final)
    if not lists:
        return False
    items = _split_top_level(
        re.sub(r"^(distinct|all)\b", "", lists[0].strip(), flags=re.IGNORECASE)
    )
    if any(item == "*" or item.endswith(".*") for item in items):
        return True
    return bool(columns) and sorted(columns) == sorted(source_columns)


def _unqualified_column(item: str) -> str | None:
    """The bare column name of a possibly-qualified reference.

    `a.account_id` -> `account_id` (the qualifier is a table alias,
    never part of the key - a `dedup_key: a` defect came from
    capturing the qualifier). Returns None when the item is not a
    plain column reference and cannot be drafted mechanically.
    """
    item = item.strip()
    if not _PLAIN_COLUMN.match(item):
        return None
    return item.split(".")[-1].strip('"`[]')


def _draft_order_by(text: str) -> str:
    """Draft an ORDER BY body, stripping qualifier prefixes from
    plain column terms (expressions are kept verbatim)."""
    terms: list[str] = []
    for term in _split_top_level(text):
        match = re.match(
            r"^(?P<expr>.*?)(?P<dir>\s+(?:asc|desc))?$",
            term,
            flags=re.IGNORECASE | re.DOTALL,
        )
        expr = (match.group("expr") if match else term).strip()
        direction = ""
        if match and match.group("dir"):
            direction = " " + match.group("dir").strip().upper()
        column = _unqualified_column(expr)
        rendered = column if column is not None else " ".join(expr.split())
        terms.append(rendered + direction)
    return ", ".join(terms)


def _parse_over_body(body: str) -> tuple[list[str], str | None] | None:
    """Parse a ROW_NUMBER OVER body into (partition columns, drafted
    ORDER BY or None). Qualifier prefixes are stripped from the
    partition columns; a non-column partition term returns None."""
    match = re.match(r"\s*partition\s+by\s+", body, flags=re.IGNORECASE)
    if not match:
        return None
    rest = body[match.end():]
    order_text: str | None = None
    order_index = _top_level_keyword(rest, "order")
    if order_index is not None and re.match(
        r"order\s+by\b", rest[order_index:], flags=re.IGNORECASE
    ):
        order_text = rest[order_index + len("order"):]
        order_text = re.sub(r"^\s*by\b", "", order_text, flags=re.IGNORECASE)
        rest = rest[:order_index]
    keys: list[str] = []
    for item in _split_top_level(rest):
        column = _unqualified_column(item)
        if column is None:
            return None
        keys.append(column)
    if not keys:
        return None
    order = _draft_order_by(order_text) if order_text is not None else None
    return keys, order


def _dedup_constructs(statement: str) -> list[dict[str, Any]]:
    """ROW_NUMBER dedup constructs in one statement: partition keys,
    drafted ORDER BY, the alias the row number is assigned (if any),
    and whether the construct sits inline in a QUALIFY clause."""
    constructs: list[dict[str, Any]] = []
    for match in _ROW_NUMBER_CALL.finditer(statement):
        open_index = match.end() - 1
        close = _matching_paren(statement, open_index)
        parsed = _parse_over_body(statement[open_index + 1: close - 1])
        if parsed is None:
            continue
        keys, order = parsed
        alias: str | None = None
        tail = re.match(r"\s+(?:as\s+)?([A-Za-z_][\w]*)", statement[close:])
        if tail and tail.group(1).lower() not in _NOT_AN_ALIAS:
            alias = tail.group(1)
        constructs.append(
            {
                "keys": keys,
                "order": order,
                "alias": alias,
                "qualify_inline": bool(
                    re.search(
                        r"qualify\s*$",
                        statement[max(0, match.start() - 16): match.start()],
                        flags=re.IGNORECASE,
                    )
                ),
            }
        )
    return constructs


def _dedup_binds_output(sql: str) -> dict[str, Any] | None:
    """The dedup construct whose filter binds the model's final
    output, or None.

    A construct binds the output when it sits on the output chain
    (the final statement and the CTEs it reads, transitively) and is
    filtered there: either inline in a QUALIFY, or via an
    `<alias> = 1` predicate in a WHERE/QUALIFY of a chain statement
    at or downstream of the construct's statement. Every chain
    statement's SELECT list must be a pass-through (the dedup item
    itself excepted), so a dedup buried in a CTE whose result is
    then transformed or aggregated does not count.
    """
    cleaned = _BENIGN_JINJA.sub("", sql)
    chain = _output_chain(cleaned)
    for position, statement in enumerate(chain):
        for construct in _dedup_constructs(statement):
            lists = _select_lists(statement)
            if not lists or not _list_is_passthrough(
                lists[0], allow_window_item=True
            ):
                continue
            if construct["qualify_inline"]:
                return construct
            alias = construct["alias"]
            if alias is None:
                continue
            filter_pattern = re.compile(
                rf"\b(?:where|qualify)\b[\s\S]*?\b{re.escape(alias)}\s*=\s*1\b",
                flags=re.IGNORECASE,
            )
            # Chain order is final-first: statements at or downstream
            # of the construct are chain[0..position].
            if any(filter_pattern.search(s) for s in chain[: position + 1]):
                return construct
    return None


# Clauses that may follow a QUALIFY predicate at the same level;
# the dedup predicate ends where one of them begins.
_QUALIFY_TAIL_CLAUSES = (
    "order",
    "limit",
    "offset",
    "window",
    "union",
    "intersect",
    "except",
    "fetch",
)


def _qualify_or_escape(sql: str) -> str | None:
    """The OR escape in a dedup QUALIFY predicate, or None.

    The witnessed shape (Cal-ITP's
    ``int_payments__transactions_deduped_enghouse``) is
    ``QUALIFY ROW_NUMBER() OVER (...) = 1 OR <col> IS NULL``: the
    predicate keeps every row the escape admits - null-key rows,
    in the witnessed case - in addition to the dedup winner. The
    silver contract (``dedup_key`` + ``dedup_order`` over one
    input) has no way to express that retention, so a fit would
    silently drop the escaped rows; worse, an equivalence proof
    built on data without null keys would pass. Detection is
    bounded to the witnessed class: a top-level OR in the QUALIFY
    predicate of a statement on the output chain whose pre-OR
    part is the ``ROW_NUMBER()... = 1`` dedup filter. Returns
    the escape text (whitespace-collapsed) for the no-fit reason.
    """
    cleaned = _BENIGN_JINJA.sub("", sql)
    for statement in _output_chain(cleaned):
        index = _top_level_keyword(statement, "qualify")
        if index is None:
            continue
        tail = statement[index + len("qualify"):].strip()
        # A predicate wrapped in one pair of parentheses hides its
        # OR from the top-level scan; unwrap a whole-predicate pair.
        if tail.startswith("(") and _matching_paren(tail, 0) == len(tail):
            tail = tail[1:-1].strip()
        for clause in _QUALIFY_TAIL_CLAUSES:
            cut = _top_level_keyword(tail, clause)
            if cut is not None:
                tail = tail[:cut].rstrip()
        or_index = _top_level_keyword(tail, "or")
        if or_index is None:
            continue
        head = tail[:or_index]
        if not _ROW_NUMBER_CALL.search(head):
            continue
        if not re.search(r"=\s*1\b", head):
            continue
        return " ".join(tail[or_index:].split())[:120]
    return None


# A dotted path of three or more segments (`metadata.extract_config
#.name`): field extraction from a struct/record column.
_STRUCT_PATH = re.compile(rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{2,}}")


def _struct_extractions(sql: str) -> list[str]:
    """Dotted struct-field extractions in the model's SELECT
    lists.

    The witnessed shapes (Cal-ITP's GTFS staging models) project
    fields out of struct columns - ``metadata.extract_ts``,
    ``metadata.extract_config.extracted_at``, ``alert.cause`` -
    and the staging draft rendered them as flat identity renames
    of columns the source does not have under those names. A
    two-segment reference is an ordinary qualified column and is
    not an extraction; three or more segments is a path into a
    struct/record value, which no flat column mapping expresses.
    Detection is bounded to that shape class: multi-segment
    dotted references appearing in SELECT lists. Returns the
    distinct paths found, in order.
    """
    cleaned = _BENIGN_JINJA.sub("", sql)
    found: list[str] = []
    for list_text in _select_lists(cleaned):
        text = re.sub(
            r"^(distinct|all)\b", "", list_text.strip(), flags=re.IGNORECASE
        )
        for item in _split_top_level(text):
            for match in _STRUCT_PATH.finditer(item):
                path = "".join(match.group(0).split())
                if path not in found:
                    found.append(path)
    return found


def _expression_items(sql: str) -> list[str]:
    """Plain-SQL expression items in the model's SELECT lists.

    A staging draft is drafted from documented columns, so a
    SELECT item that computes its value in plain SQL - a function
    call, an operator expression, a CASE, a literal - is invisible
    to the drafting: the draft would carry the documented output
    name as if it were a plain rename and silently drop the
    computation (the expression case). Items a column mapping
    *can* express (plain columns and stars, with or without a
    rename alias) are not expressions. Jinja/macro items are
    skipped - macro usage is its own signal - and window items
    are skipped - a window function is its own signal. Returns
    the distinct offending items, whitespace-normalised, in
    order."""
    cleaned = _strip_comments(_BENIGN_JINJA.sub("", sql))
    found: list[str] = []
    for list_text in _select_lists(cleaned):
        text = re.sub(
            r"^(distinct|all)\b", "", list_text.strip(), flags=re.IGNORECASE
        )
        for item in _split_top_level(text):
            if "{{" in item or "{%" in item:
                continue
            if _WINDOW.search(item):
                continue
            if _PLAIN_PROJECTION.match(item):
                continue
            normalised = " ".join(item.split())[:80]
            if normalised and normalised not in found:
                found.append(normalised)
    return found


_JINJA_SIGNAL = "Jinja/macro usage beyond ref/source/config"

_MACRO_ITEM = re.compile(
    r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"\s*\((.*)\)\s*\}\}\s*(?:as\s+)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?$",
    re.DOTALL | re.IGNORECASE,
)

_EMBEDDED_MACRO = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\("
)

_MACRO_ARG_STRING = re.compile(r"[A-Za-z0-9 _.,:/@-]{0,200}")


def _split_macro_args(text: str) -> list[str]:
    """Split a macro call's argument list on top-level commas
    (commas inside nested brackets or quotes do not split)."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _macro_literal(token: str) -> tuple[bool, Any]:
    """Parse one macro argument as a slot literal: a quoted
    string within the macro_args charset, an integer, or a
    boolean - the values a drafted `macro_args` list may carry.
    Returns (is_literal, value)."""
    token = token.strip()
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        content = token[1:-1]
        if token[0] in content or "\\" in content:
            return False, None
        if not _MACRO_ARG_STRING.fullmatch(content):
            return False, None
        return True, content
    if re.fullmatch(r"-?\d+", token):
        return True, int(token)
    if token.lower() == "true":
        return True, True
    if token.lower() == "false":
        return True, False
    return False, None


def _macro_select_items(sql: str) -> list[dict[str, Any]]:
    """The Jinja invocations among the model's SELECT items
    (Tier 1). Each item is classified: ``slot`` when it is
    exactly a name-style macro call in the staging slot's
    calling convention - the source column as the first, quoted
    argument, then fixed literal arguments - with an output
    alias; ``tier2`` when every argument is a fixed literal and
    a quoted identifier-shaped argument sits in a non-first
    position (a macro_column_param candidate, resolved by the
    fit against the source's documented columns); ``other``
    otherwise, with the reason it cannot draft.
    Jinja that is not a select item at all (call blocks, set
    statements, calls in other clauses) is not returned here;
    `_unaccounted_jinja` reports it."""
    cleaned = _strip_comments(sql)
    lists = _select_lists(cleaned)
    items: list[dict[str, Any]] = []
    for list_index, list_text in enumerate(lists):
        text = re.sub(
            r"^(distinct|all)\b", "", list_text.strip(), flags=re.IGNORECASE
        )
        for raw_item in _split_top_level(text):
            if "{{" not in raw_item and "{%" not in raw_item:
                continue
            normalised = " ".join(raw_item.split())[:120]
            item: dict[str, Any] = {
                "text": normalised,
                "raw": raw_item,
                "final": list_index == len(lists) - 1,
                "shape": "other",
                "reference": None,
                "alias": None,
                "column": None,
                "args": [],
                "candidates": [],
                "values": [],
                "reason": (
                    "not a plain macro invocation (call blocks and "
                    "other Jinja do not draft as column mappings)"
                ),
            }
            match = _MACRO_ITEM.match(raw_item.strip())
            if match is None:
                # Tier 3: a macro call embedded in a larger
                # expression stays scaffold-class; the reason
                # names the macro path so the manual rewrite has
                # its target.
                embedded = _EMBEDDED_MACRO.search(raw_item)
                if embedded is not None:
                    item["reason"] = (
                        f"a macro call ({embedded.group(1)}) embedded "
                        "in a larger expression - an embedded call "
                        "does not draft as a column mapping (Tier 3); "
                        "the macro path is named for the manual "
                        "rewrite"
                    )
            if match is not None:
                reference, args_text, alias = match.groups()
                item["reference"] = reference
                item["alias"] = alias
                tokens = _split_macro_args(args_text)
                if not alias:
                    item["reason"] = (
                        "the invocation carries no output alias, so "
                        "no documented output column pairs with it"
                    )
                elif not tokens:
                    item["reason"] = (
                        "the invocation takes no arguments, so no "
                        "source column fills the slot's first position"
                    )
                else:
                    first_ok, first_value = _macro_literal(tokens[0])
                    rest: list[Any] = []
                    rest_ok = True
                    for token in tokens[1:]:
                        ok, value = _macro_literal(token)
                        if not ok:
                            rest_ok = False
                            break
                        rest.append(value)
                    if (
                        first_ok
                        and isinstance(first_value, str)
                        and re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*", first_value
                        )
                        and rest_ok
                        and len(rest) <= 8
                    ):
                        item["shape"] = "slot"
                        item["column"] = first_value
                        item["args"] = rest
                        item["reason"] = ""
                    else:
                        # Tier 2 candidate: every argument is
                        # either a slot literal or a bare column
                        # reference, and at least one candidate
                        # source column - a quoted identifier-shaped
                        # literal or a bare identifier - sits in a
                        # non-first position, for macro_column_param.
                        # Which candidate IS the source column is
                        # resolved by the fit against the source's
                        # documented columns (a literal like 'day'
                        # is identifier-shaped too; position alone
                        # cannot disambiguate it from the column).
                        candidates: list[tuple[int, str, bool]] = []
                        values: list[Any] = []
                        accounted = True
                        for index, token in enumerate(tokens):
                            ok, value = _macro_literal(token)
                            if ok:
                                values.append(value)
                                if isinstance(value, str) and re.fullmatch(
                                    r"[A-Za-z_][A-Za-z0-9_]*", value
                                ):
                                    candidates.append(
                                        (index + 1, value, False)
                                    )
                            elif re.fullmatch(
                                r"[A-Za-z_][A-Za-z0-9_]*", token.strip()
                            ):
                                values.append(token.strip())
                                candidates.append(
                                    (index + 1, token.strip(), True)
                                )
                            else:
                                accounted = False
                                break
                        if (
                            accounted
                            and len(tokens) <= 9
                            and any(
                                position > 1
                                for position, _, _ in candidates
                            )
                        ):
                            item["shape"] = "tier2"
                            item["candidates"] = candidates
                            item["values"] = values
                            item["reason"] = ""
                        else:
                            item["reason"] = (
                                "not in a draftable macro shape - "
                                "the slot drafts fixed literal "
                                "arguments with the source column "
                                "first, and macro_column_param "
                                "(Tier 2) drafts fixed literal "
                                "arguments with the source column "
                                "in a later position; a computed "
                                "argument or a call with no "
                                "identifiable source column cannot "
                                "draft"
                            )
            items.append(item)
    return items


def _unaccounted_jinja(sql: str, items: list[dict[str, Any]]) -> bool:
    """True when Jinja remains after the benign calls and the
    parsed select-item invocations are removed - Jinja the fit
    cannot account for (a call block, a set statement, a call in
    another clause)."""
    cleaned = _BENIGN_JINJA.sub("", _strip_comments(sql))
    for item in items:
        cleaned = cleaned.replace(item["raw"], "", 1)
    return "{{" in cleaned or "{%" in cleaned


_EXPR_AS_ALIAS = re.compile(
    r"^(?P<expr>.+?)\s+as\s+\"?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\"?$",
    re.IGNORECASE | re.DOTALL,
)
_EXPR_BARE_ALIAS = re.compile(
    r"^(?P<expr>.+\S)\s+\"?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\"?$",
    re.DOTALL,
)


def _split_expression_alias(item: str) -> tuple[str, str] | None:
    """Split an expression SELECT item into (expression, alias),
    or None when the item does not name its output with an
    alias. Only the AS form splits unconditionally; a bare
    trailing identifier splits only when the head is a real
    expression (contains a space or a parenthesis) - a plain
    column rename is not an expression."""
    text = " ".join(item.split())
    match = _EXPR_AS_ALIAS.match(text)
    if match is not None:
        return match.group("expr").strip(), match.group("alias")
    if _PLAIN_PROJECTION.match(text):
        return None
    match = _EXPR_BARE_ALIAS.match(text)
    if match is not None and (
        " " in match.group("expr") or "(" in match.group("expr")
    ):
        return match.group("expr").strip(), match.group("alias")
    return None


def _construct_inventory(sql: str) -> list[dict[str, Any]]:
    """The structured construct inventory for one model (M3):
    every plain-SQL expression item and every macro invocation
    in the model's SELECT lists, named COMPLETELY - full item
    text (no truncation), the macro's reference and arguments
    and wrapped column where determinable, and the item's
    location (the final projection, a named CTE, or a subquery
    in the final statement). The prose blocker reasons keep
    their summary form; this list is the complete account the
    draft record carries alongside them."""
    ctes, final = _split_ctes(_strip_comments(sql))
    segments: list[tuple[str, str, bool]] = [
        (f"cte '{name}'", body, False) for name, body in ctes.items()
    ]
    segments.append(("final statement", final, True))
    constructs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def note(entry: dict[str, Any]) -> None:
        key = (entry["kind"], entry["text"], entry["location"])
        if key not in seen:
            seen.add(key)
            constructs.append(entry)

    for segment_label, segment, is_final_statement in segments:
        lists = _select_lists(segment)
        for list_index, list_text in enumerate(lists):
            if is_final_statement and list_index == len(lists) - 1:
                location = "final projection"
            elif is_final_statement:
                location = "subquery in the final statement"
            else:
                location = segment_label
            text = re.sub(
                r"^(distinct|all)\b",
                "",
                list_text.strip(),
                flags=re.IGNORECASE,
            )
            for raw_item in _split_top_level(text):
                item_text = " ".join(raw_item.split())
                if not item_text:
                    continue
                if "{{" in raw_item or "{%" in raw_item:
                    continue  # macros are inventoried from the macro pass
                if _WINDOW.search(raw_item):
                    continue
                if _PLAIN_PROJECTION.match(item_text):
                    continue
                split = _split_expression_alias(item_text)
                note(
                    {
                        "kind": "expression",
                        "text": item_text,
                        "expression": split[0] if split else item_text,
                        "alias": split[1] if split else None,
                        "location": location,
                    }
                )
        for macro_item in _macro_select_items(segment):
            if is_final_statement:
                location = (
                    "final projection"
                    if macro_item["final"]
                    else "subquery in the final statement"
                )
            else:
                location = segment_label
            entry: dict[str, Any] = {
                "kind": "macro",
                "text": " ".join(str(macro_item["raw"]).split()),
                "reference": macro_item.get("reference"),
                "alias": macro_item.get("alias"),
                "location": location,
            }
            if macro_item.get("column"):
                entry["column"] = macro_item["column"]
            args = macro_item.get("args") or macro_item.get("values")
            if args:
                entry["args"] = args
            note(entry)
    return constructs


def _chain_projection_expressions(sql: str) -> list[dict[str, str]]:
    """Expression items in the model's EFFECTIVE final
    projection: the first non-star SELECT list on the output
    chain (the final statement's own list, or the list of the
    CTE the chain of bare stars resolves to). Each item is
    split into {expression, alias}; items without a
    determinable alias are not returned. This is the existing
    output-chain analysis `_projected_outputs` walks - no
    chain resolution beyond it."""
    account = _chain_projection_accounting(sql)
    if account is None:
        return []
    return [
        {"expression": entry["expression"], "alias": entry["alias"]}
        for entry in account["items"]
        if entry["kind"] == "expression"
    ]


def _chain_projection_accounting(
    sql: str,
) -> dict[str, Any] | None:
    """The authoring account of the model's EFFECTIVE final
    projection: every item of the first non-star SELECT list on
    the output chain (the walk `_projected_outputs` makes),
    classified in projection order - or None when the chain
    names no such list (every list is a bare star, so the
    projection's outputs are the read relation's own columns
    and no list in the SQL names them).

    Each item is one of:

    - ``expression``: an {expression, alias} item - the form
      the staging scaffold pre-places (the same split
      `_chain_projection_expressions` makes);
    - ``unmapped``: an item that names its output - a plain
      column, a rename, an aliased macro invocation, an aliased
      window item - but is not pre-placed, so a draft carrying
      only the pre-placed items does not map that output;
    - residue: an item that names no output (a star mixed with
      explicit items, an unaliased expression or literal, an
      unaliased macro call). Any residue makes the account
      incomplete: the named outputs are accounted, but the
      projection may produce more than the account can name -
      the enumeration bound `_projected_outputs` states for
      fit drafts, applied to the authoring path.
    """
    cleaned = _strip_comments(_BENIGN_JINJA.sub("", sql))
    for statement in _output_chain(cleaned):
        lists = _select_lists(statement)
        if not lists:
            return None
        text = re.sub(
            r"^(distinct|all)\b", "", lists[0].strip(), flags=re.IGNORECASE
        )
        items = [item.strip() for item in _split_top_level(text)]
        items = [item for item in items if item]
        if not items:
            return None
        if all(item == "*" or item.endswith(".*") for item in items):
            continue
        account: list[dict[str, str]] = []
        seen: set[str] = set()
        complete = True
        for item in items:
            if item == "*" or item.endswith(".*"):
                complete = False
                continue
            if "{{" in item or "{%" in item:
                name = _projection_output_name(item)
                if name is None:
                    complete = False
                elif name not in seen:
                    seen.add(name)
                    account.append({"kind": "unmapped", "alias": name})
                continue
            if _WINDOW.search(item):
                window_split = _split_expression_alias(item)
                if window_split is None:
                    complete = False
                elif window_split[1] not in seen:
                    seen.add(window_split[1])
                    account.append(
                        {"kind": "unmapped", "alias": window_split[1]}
                    )
                continue
            if _PLAIN_PROJECTION.match(" ".join(item.split())):
                name = _projection_output_name(item)
                if name is None:
                    complete = False
                elif name not in seen:
                    seen.add(name)
                    account.append({"kind": "unmapped", "alias": name})
                continue
            split = _split_expression_alias(item)
            if split is None:
                complete = False
            else:
                # Expression items are never deduped: the
                # pre-placement takes them exactly as the
                # enumeration finds them. The alias is still
                # recorded, so a later item naming the same
                # output is not accounted twice.
                seen.add(split[1])
                account.append(
                    {
                        "kind": "expression",
                        "expression": split[0],
                        "alias": split[1],
                    }
                )
        return {"items": account, "complete": complete}
    return None


def _single_source_blockers(sql: str) -> list[str]:
    """The blocking signals for a single-source model.

    A fit draft cannot carry a row filter, a plain-SQL column
    expression, or a LIMIT - the staging generator can express a
    row filter as authored vocabulary (ADR 0038), but convert
    does not draft one and bronze cannot express one - and
    none of the three used to be a fit signal at all, so a model
    carrying one was drafted as a fit that silently dropped it
    (a filter vacuous on proof data, or an expression the proof
    data does not expose, passes a content comparison while
    changing production rows - the QUALIFY OR-escape shape).
    Named here, they demote the model to a no-fit whose reason
    names the construct, exactly like the OR-escape precedent.
    Detection runs over the whole model (a filter or limit
    inside a CTE binds the output just as one on the final
    statement does), over comment-stripped text (a banner
    comment is not a clause)."""
    cleaned = _strip_comments(_BENIGN_JINJA.sub("", sql))
    blockers: list[str] = []
    if _ROW_FILTER.search(cleaned):
        blockers.append(
            "a row filter (WHERE) - a fit draft cannot carry a "
            "filter, so a fit would silently drop it (the staging "
            "generator can express a filter as authored vocabulary "
            "under ADR 0038, but convert does not draft one)"
        )
    expressions = _expression_items(sql)
    if expressions:
        named = ", ".join(f"`{item}`" for item in expressions[:2])
        if len(expressions) > 2:
            named += f" and {len(expressions) - 2} more"
        blockers.append(
            f"plain-SQL column expression(s) in a SELECT list "
            f"({named}) - a column mapping cannot express a "
            "computed column, so a fit would silently drop the "
            "computation"
        )
    if _LIMIT_CLAUSE.search(cleaned):
        blockers.append(
            "a LIMIT clause - neither the staging nor the bronze "
            "generator can express a row limit, so a fit would "
            "silently drop it"
        )
    return blockers


def _no_columns_caveat(subject: str) -> str:
    """The documented-columns caveat when nothing is documented
    (generalising the bronze caveat)."""
    return (
        f"caveat: the manifest documents no columns for {subject}, "
        "so the drafted columns assume source column names match "
        "the output names; the equivalence proof verifies this "
        "before any swap"
    )


def _partial_columns_caveat(
    subject: str, documented: int, total: int
) -> str:
    """The documented-columns caveat when documentation is
    partial: only some of the drafted columns are
    documented on the source/input the draft reads."""
    return (
        f"caveat: the manifest documents only some of the columns "
        f"for {subject} ({documented} of {total} drafted columns), "
        "so the remaining drafted columns assume source column "
        "names match the output names; the equivalence proof "
        "verifies this before any swap"
    )


# The unresolved-placeholder sentinel for a drafted staging column
# whose source column could not be paired from the manifest's
# documented columns. Executable positions carry the system-namespace
# sentinel (descriptive draft fields keep plain `TODO`); `loom
# validate` rejects it in a staging column `from`/`to`, so a
# placeholder can never survive promotion into the inventory.
_TODO_SENTINEL = "__TODO__"


def _draft_staging_columns(
    output_columns: list[str], source_columns: list[str]
) -> tuple[list[dict[str, str]], str]:
    """Draft a staging column mapping from documented columns.

    Outputs whose name matches a source column map 1:1. When exactly
    one source column and one output column remain unmatched, they are
    paired - the rename case (`id` selected as `order_id`) - and the
    inference is recorded in the returned evidence note. Every output
    that remains unpaired after that is drafted as an unresolved
    placeholder (`source: __TODO__`), never as an identity guess: an
    identity mapping for an unpaired output asserts a source column
    the manifest does not document, and on the textbook multi-rename
    staging shape that assertion is wrong. The evidence note names
    the unresolved outputs and the candidate source columns; the
    practitioner drafts those mappings from the original SQL, and
    `loom validate` rejects the sentinel if one survives."""
    source_set = set(source_columns)
    output_set = set(output_columns)
    unmatched_outputs = [c for c in output_columns if c not in source_set]
    unmatched_sources = [c for c in source_columns if c not in output_set]
    rename: dict[str, str] = {}
    placeholders: list[str] = []
    if len(unmatched_outputs) == 1 and len(unmatched_sources) == 1:
        rename[unmatched_outputs[0]] = unmatched_sources[0]
        note = (
            "columns drafted from the manifest's documented columns; "
            f"one rename inferred ({unmatched_sources[0]!r} selected as "
            f"{unmatched_outputs[0]!r}) because it is the only unmatched "
            "pair - verify it, then let the equivalence proof confirm"
        )
    elif not unmatched_outputs:
        note = (
            "columns drafted from the manifest's documented columns, "
            "assuming source column names match the output names; the "
            "equivalence proof verifies this before any swap"
        )
    else:
        placeholders = unmatched_outputs
        if unmatched_sources:
            candidates = (
                "documented source columns matched to no output "
                "(the candidates): " + ", ".join(unmatched_sources)
            )
        elif source_columns:
            candidates = (
                "every documented source column is already matched "
                "to an output, so no candidate source column remains"
            )
        else:
            candidates = (
                "the manifest documents no source columns, so no "
                "candidate source column can be named"
            )
        note = (
            "columns drafted from the manifest's documented columns; "
            f"{len(placeholders)} documented output(s) could not be "
            "paired with a documented source column and are drafted "
            f"as unresolved placeholders (source: {_TODO_SENTINEL}): "
            + ", ".join(placeholders)
            + "; "
            + candidates
            + " - draft each mapping from the original SQL; "
            "`loom validate` rejects the placeholder, and the "
            "equivalence proof verifies completed mappings before "
            "any swap"
        )
    mapping: list[dict[str, str]] = []
    for column in output_columns:
        if column in placeholders:
            mapping.append({"source": _TODO_SENTINEL, "alias": column})
        else:
            mapping.append(
                {"source": rename.get(column, column), "alias": column}
            )
    return mapping, note


def _column_descriptions(node: dict[str, Any]) -> dict[str, str]:
    """Documented column descriptions from the manifest.

    Instance metadata carries a per-column description
    field (the ``column_docs`` parameter), so these
    descriptions are drafted into that parameter - the earlier
    claim that they could not ride in drafted parameters is dead."""
    descriptions: dict[str, str] = {}
    for column, spec in sorted((node.get("columns") or {}).items()):
        if isinstance(spec, dict):
            text = spec.get("description")
            if isinstance(text, str) and text.strip():
                descriptions[column] = text
    return descriptions


def _column_types(node: dict[str, Any]) -> dict[str, str]:
    """Documented column data types from the manifest."""
    types: dict[str, str] = {}
    for column, spec in sorted((node.get("columns") or {}).items()):
        if isinstance(spec, dict):
            value = spec.get("data_type")
            if isinstance(value, str) and value.strip():
                types[column] = value
    return types


def _manifest_description(node: dict[str, Any]) -> str | None:
    """The model's documented description, verbatim."""
    description = node.get("description")
    if isinstance(description, str) and description.strip():
        return description
    return None


def _manifest_owner(node: dict[str, Any]) -> str | None:
    """The model's declared owner: manifest ``meta.owner`` when it
    is a non-empty string. No other meta key is read -
    alias keys (``team``, ``owner_email``, ...) are the project's
    own conventions, not a declaration Loom may guess at; where
    the project is silent the draft keeps its TODO."""
    meta = node.get("meta")
    if not isinstance(meta, dict):
        return None
    owner = meta.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner
    return None


def _harvest_column_metadata(
    node: dict[str, Any],
    params: dict[str, Any],
    evidence: list[str],
) -> None:
    """Draft documented column descriptions/types into the
    ``column_docs`` / ``column_types`` parameters.

    Both are copied verbatim from the manifest. The types are
    marked UNVERIFIED in the evidence: a harvested type is the
    project's documented spelling, not a value Loom has checked
    against the warehouse, and it becomes load-bearing only if
    the practitioner enables an enforced contract
    (``contract_enforced``)."""
    docs = _column_descriptions(node)
    if docs:
        params["column_docs"] = docs
        evidence.append(
            "documented column descriptions are drafted into "
            "column_docs from the manifest"
        )
    types = _column_types(node)
    if types:
        params["column_types"] = types
        evidence.append(
            "documented column types are drafted into column_types "
            "as declared in the manifest - UNVERIFIED: copied from "
            "the project's documentation, not checked against the "
            "warehouse, and load-bearing only if the practitioner "
            "enables an enforced contract (contract_enforced)"
        )


def _declared_incremental(node: dict[str, Any]) -> dict[str, Any] | None:
    """The model's declared incremental configuration, keyed as a
    Loom ``incremental`` block: ``incremental_strategy``
    becomes ``strategy``; ``unique_key`` and ``on_schema_change``
    keep their names. Only the declared subset is returned -
    drafted as declared, never completed by guessing - and only
    for a model materialized as incremental (the same condition
    the loss-warning flag reads)."""
    config = node.get("config") or {}
    if config.get("materialized") != "incremental":
        return None
    block: dict[str, Any] = {}
    strategy = config.get("incremental_strategy")
    if isinstance(strategy, str) and strategy:
        block["strategy"] = strategy
    unique_key = config.get("unique_key")
    if isinstance(unique_key, (str, list)) and unique_key:
        block["unique_key"] = unique_key
    on_schema_change = config.get("on_schema_change")
    if isinstance(on_schema_change, str) and on_schema_change:
        block["on_schema_change"] = on_schema_change
    return block or None


def _draft_incremental(
    node: dict[str, Any],
    params: dict[str, Any],
    evidence: list[str],
) -> bool:
    """Draft the declared incremental configuration into the
    fit's parameters. Returns whether a block was
    drafted - the caller suppresses the loss-warning flag
    for a model whose configuration a draft now carries. Only
    called for generators whose contract admits the
    ``incremental`` parameter (bronze, silver); a staging fit
    cannot carry the block, so its flag stays."""
    block = _declared_incremental(node)
    if block is None:
        return False
    params["incremental"] = block
    details = ", ".join(f"{key}={value!r}" for key, value in block.items())
    note = (
        "the model's declared incremental configuration is "
        f"drafted into the incremental block as declared "
        f"({details})"
    )
    if "strategy" not in block:
        note += (
            "; the project declares no incremental_strategy, so "
            "a strategy must be chosen before the block validates"
        )
    evidence.append(note)
    return True


_JINJA_CLOSERS = {"{{": "}}", "{%": "%}", "{#": "#}"}

_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _skip_quoted(sql: str, start: int) -> int:
    """Index just past the quoted span opening at ``start``.

    Single-quoted strings, double-quoted identifiers and
    backtick-quoted identifiers are skipped whole - honouring
    quote doubling (``''``) and, outside double quotes,
    backslash escapes - so a ``--`` inside a string literal is
    data, never a comment marker."""
    quote = sql[start]
    index = start + 1
    while index < len(sql):
        char = sql[index]
        if char == "\\" and quote != '"':
            index += 2
            continue
        if char == quote:
            if sql[index + 1: index + 2] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _skip_jinja(sql: str, start: int) -> int:
    """Index just past the Jinja block opening at ``start``.

    Jinja is not SQL: ``{{... }}`` expressions, ``{%... %}``
    statements and ``{#... #}`` comments are skipped whole, so
    nothing inside them - including a ``--`` in a config value -
    is harvested as a SQL comment. Quoted strings inside the
    block are skipped too, so a closer inside a string cannot
    end the block early."""
    closer = _JINJA_CLOSERS[sql[start: start + 2]]
    index = start + 2
    while index < len(sql):
        if sql.startswith(closer, index):
            return index + 2
        if sql[index] in "'\"":
            index = _skip_quoted(sql, index)
            continue
        index += 1
    return len(sql)


def _strip_comments(sql: str) -> str:
    """The SQL with every comment replaced by a single space.

    Uses the same scanning discipline as `_source_comments`:
    comment-like text inside string literals is data, never a
    comment; Jinja blocks and dollar-quoted strings are copied
    whole. Shape detectors that match whole SELECT items or
    clause keywords (the signals) run over the stripped text
    so a comment banner sitting inside a SELECT list - the
    jaffle staging idiom, `-- ---------- ids` above a plain
    rename - is not mistaken for SQL."""
    out: list[str] = []
    index = 0
    while index < len(sql):
        pair = sql[index: index + 2]
        if pair == "--":
            stop = sql.find("\n", index)
            index = len(sql) if stop == -1 else stop
            out.append(" ")
        elif pair == "/*":
            stop = sql.find("*/", index + 2)
            index = len(sql) if stop == -1 else stop + 2
            out.append(" ")
        elif pair in _JINJA_CLOSERS:
            stop = _skip_jinja(sql, index)
            out.append(sql[index:stop])
            index = stop
        elif sql[index] in "'\"`":
            stop = _skip_quoted(sql, index)
            out.append(sql[index:stop])
            index = stop
        elif sql[index] == "$":
            match = _DOLLAR_QUOTE.match(sql, index)
            if match is None:
                out.append(sql[index])
                index += 1
            else:
                delimiter = match.group(0)
                stop = sql.find(delimiter, match.end())
                stop = len(sql) if stop == -1 else stop + len(delimiter)
                out.append(sql[index:stop])
                index = stop
        else:
            out.append(sql[index])
            index += 1
    return "".join(out)


def _source_comments(sql: str) -> str | None:
    """All of the model's SQL comments, verbatim, in source
    order (every comment, not the leading block alone).

    Originally only the leading comment block was harvested; a
    Cal-ITP spot-check found real rationale lives inline, inside
    CTEs (the settlements model's issue-link comments), so every
    ``--`` line comment and ``/*... */`` block comment in the
    source is now copied. Nothing is selected, summarised or
    reordered: deciding which comments matter is interpretation
    (interpretation is out of scope here), and placing the note
    is the practitioner's act. Comment-like text inside
    string literals is data and is never harvested; Jinja blocks
    and PostgreSQL dollar-quoted strings are skipped whole.
    Returns None when the model carries no SQL comments."""
    comments: list[str] = []
    index = 0
    while index < len(sql):
        pair = sql[index: index + 2]
        if pair == "--":
            stop = sql.find("\n", index)
            if stop == -1:
                stop = len(sql)
            comments.append(sql[index:stop].rstrip())
            index = stop
        elif pair == "/*":
            stop = sql.find("*/", index + 2)
            stop = len(sql) if stop == -1 else stop + 2
            block = sql[index:stop]
            comments.append(
                "\n".join(line.rstrip() for line in block.splitlines())
            )
            index = stop
        elif pair in _JINJA_CLOSERS:
            index = _skip_jinja(sql, index)
        elif sql[index] in "'\"`":
            index = _skip_quoted(sql, index)
        elif sql[index] == "$":
            match = _DOLLAR_QUOTE.match(sql, index)
            if match is None:
                index += 1
            else:
                delimiter = match.group(0)
                stop = sql.find(delimiter, match.end())
                index = len(sql) if stop == -1 else stop + len(delimiter)
        else:
            index += 1
    text = "\n".join(comments).strip()
    return text or None


def _attach_source_rationale(
    proposal: dict[str, Any], node: dict[str, Any]
) -> None:
    """Carry all of the source model's SQL comments on the
    proposal, verbatim and in source order, so the
    conversion draft ships them as a source-rationale
    note for the practitioner to prune and place."""
    sql = node.get("raw_code") or node.get("compiled_code") or ""
    if not isinstance(sql, str):
        return
    comments = _source_comments(sql)
    if comments:
        proposal["source_rationale"] = comments


def _fit_proposal(
    name: str,
    node: dict[str, Any],
    source_deps: list[dict[str, str]],
    model_deps: list[str],
    source_columns: list[str] | None = None,
    input_columns: list[str] | None = None,
    macro_index: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    project_name: str | None = None,
    seed_deps: list[str] | None = None,
) -> dict[str, Any]:
    """Shape matching against the model's output (ADR 0025).

    Returns a proposal dict with `generator` + `drafted_params` +
    `evidence`, or a `no_fit` reason naming the blocking signals.
    Proposals are heuristic: they certify the output shape, not the
    semantics, and the equivalence proof gates any swap."""
    sql = node.get("compiled_code") or node.get("raw_code") or ""
    signals = _signals(sql)
    columns = sorted((node.get("columns") or {}).keys())

    # Tier 1 macro accounting: every Jinja select item is
    # classified; slot-shaped calls in the final projection whose
    # alias is a documented output are draftable (verified
    # against the manifest before they are drafted), and every
    # other macro shape is a named blocker, replacing the generic
    # Jinja signal in the model's no-fit account.
    macro_items = (
        _macro_select_items(sql) if ("{{" in sql or "{%" in sql) else []
    )
    unaccounted = _unaccounted_jinja(sql, macro_items) if macro_items else (
        "{{" in _BENIGN_JINJA.sub("", sql) or "{%" in _BENIGN_JINJA.sub("", sql)
    )
    macro_blockers: list[str] = []
    draftable_macro_items: list[dict[str, Any]] = []
    for item in macro_items:
        if item["shape"] not in ("slot", "tier2"):
            macro_blockers.append(
                "a macro invocation a staging column mapping "
                f"cannot draft: `{item['text']}` - {item['reason']}"
            )
        elif not item["final"]:
            macro_blockers.append(
                "a macro invocation inside a CTE or subquery, "
                f"not the final projection: `{item['text']}` - a "
                "staging column mapping describes the final "
                "projection only"
            )
        elif item["alias"] not in columns:
            macro_blockers.append(
                f"a macro invocation whose output {item['alias']!r} "
                "is not among the model's documented columns: "
                f"`{item['text']}` - the mapping drafts from "
                "documentation, so it cannot be drafted"
            )
        else:
            draftable_macro_items.append(item)

    def named_signals(source_signals: list[str]) -> list[str]:
        named: list[str] = []
        for signal in source_signals:
            if signal == _JINJA_SIGNAL and (macro_blockers or unaccounted):
                named.extend(macro_blockers)
                if unaccounted:
                    named.append(signal)
            else:
                named.append(signal)
        return named

    if node.get("resource_type") != "model":
        return {"no_fit": "not a model resource"}

    # A documented model keeps its documentation through
    # a conversion draft - the manifest description is drafted
    # verbatim instead of a TODO the practitioner must retype, and
    # a declared owner (manifest meta.owner) replaces the TODO.
    description = _manifest_description(node)
    owner = _manifest_owner(node)

    if len(source_deps) == 1 and not model_deps:
        source = source_deps[0]
        # A projection that extracts dotted struct fields
        # (metadata.extract_config.name, alert.cause, ...) is not a
        # flat column mapping - the extraction computes output
        # columns out of a struct/record value the manifest does
        # not document as flat source columns. Drafting the
        # extractions as identity renames (the Cal-ITP GTFS
        # staging drafts) invents source columns; abstain instead.
        extractions = _struct_extractions(sql)
        if extractions:
            named = ", ".join(f"`{path}`" for path in extractions[:3])
            if len(extractions) > 3:
                named += f" and {len(extractions) - 3} more"
            return {
                "no_fit": (
                    "no fit rule matched (blocking signal: struct-field "
                    f"extraction in a SELECT list - dotted paths such "
                    f"as {named} project fields out of a struct/record "
                    "column, which a flat column mapping cannot "
                    "express); assess manually - a bespoke model is a "
                    "valid end state (register it; do not force a "
                    "generator fit)"
                )
            }
        # The blockers: a row filter, a plain-SQL column
        # expression or a LIMIT anywhere in the model demotes
        # both single-source fits (staging and bronze) - a fit
        # draft cannot carry any of the three, and a draft
        # that ignores them silently drops them. Any other
        # signals present are named alongside, so the reason
        # stays the model's complete blocking account.
        construct_blockers = _single_source_blockers(sql)
        if construct_blockers:
            named_blockers = list(construct_blockers) + named_signals(signals)
            if _DISTINCT.search(sql):
                named_blockers.append("SELECT DISTINCT")
            if _ROW_NUMBER_CALL.search(sql):
                named_blockers.append(
                    "ROW_NUMBER() usage with no dedup filter binding "
                    "the final output"
                )
            return {
                "no_fit": (
                    "no fit rule matched (blocking signals: "
                    + "; ".join(named_blockers)
                    + "); assess manually - a bespoke model is a "
                    "valid end state (register it; do not force a "
                    "generator fit)"
                ),
                # The constructs alone are readiness blockers:
                # unlike the other named signals, which
                # authoring into a target generator can answer,
                # no generator can express these even after the
                # transform is authored, so the readiness account
                # carries them structurally, not only as prose
                # inside the reason.
                "blockers": list(construct_blockers),
            }
        # Bronze is a pass-through of the source unchanged, so
        # the whole model must be one - a transform, rename, DISTINCT
        # or window anywhere (typically inside a CTE whose final
        # statement is a bare SELECT *) blocks the fit, and the final
        # SELECT must itself be a plain pass-through. An explicit
        # column list counts only when the manifest documents the
        # same columns on the model and the source (a subset
        # projection would gain columns under bronze's SELECT *).
        if not _bronze_blockers(sql, signals) and _bronze_output_matches(
            sql, columns, source_columns or []
        ):
            bronze_params: dict[str, Any] = {
                "inline_source": {
                    "name": source["source_name"],
                    "table": source["name"],
                }
            }
            if description is not None:
                bronze_params["description"] = description
            if owner is not None:
                bronze_params["owner"] = owner
            bronze_evidence = [
                "depends on exactly one dbt source and no models",
                "output is a plain pass-through of the source: the "
                "final SELECT lists the source's columns unchanged "
                "and no part of the model transforms, renames, "
                "de-duplicates, aggregates or joins rows",
            ]
            _harvest_column_metadata(node, bronze_params, bronze_evidence)
            _draft_incremental(node, bronze_params, bronze_evidence)
            if not source_columns:
                # With no documented source columns the
                # pass-through claim rests on the model's SELECT
                # shape alone - nothing in the manifest verifies
                # that the model's output is the source's column set
                # (the scraped-URLs case). Say so on
                # the proposal, exactly when it applies.
                bronze_evidence.append(
                    "caveat: the manifest documents no columns for "
                    "the source, so this pass-through rests on the "
                    "model's SELECT shape alone; no documented source "
                    "column list verifies it"
                )
            elif columns and not set(columns) <= set(source_columns):
                # Partial documentation - the source's
                # documented columns do not cover every column the
                # model documents, so the pass-through is verified
                # only for the documented subset.
                bronze_evidence.append(
                    _partial_columns_caveat(
                        "the source",
                        len(set(columns) & set(source_columns)),
                        len(columns),
                    )
                )
            # Completeness accounting (M3): a bronze draft is a
            # pass-through and carries no column mapping, so an
            # output the final projection names but the manifest
            # does not document can only be accounted by naming
            # it - a star projection is the source's own columns
            # by construction and needs no accounting, but an
            # explicitly listed output beyond the documented set
            # rests on documentation the manifest does not have.
            bronze_projected = _projected_outputs(sql)
            if bronze_projected is not None:
                bronze_documented = {c.lower() for c in columns}
                bronze_unaccounted = [
                    output
                    for output in bronze_projected
                    if output.lower() not in bronze_documented
                ]
                if bronze_unaccounted:
                    bronze_evidence.append(
                        "caveat: the model's final projection "
                        "names output(s) the manifest does not "
                        "document: "
                        + ", ".join(bronze_unaccounted)
                        + "; a bronze draft is a pass-through of "
                        "the source and drafts no column mapping, "
                        "so those outputs are accounted only by "
                        "the pass-through itself - the equivalence "
                        "proof verifies the column set before "
                        "any swap"
                    )
            proposal: dict[str, Any] = {
                "generator": "bronze",
                "evidence": bronze_evidence,
                "drafted_params": bronze_params,
            }
            _attach_source_rationale(proposal, node)
            return proposal
        residual_signals = [s for s in signals if s != _JINJA_SIGNAL]
        if (
            not residual_signals
            and not macro_blockers
            and not unaccounted
            and not _DISTINCT.search(sql)
        ):
            params: dict[str, Any] = {
                "description": description or f"TODO: describe {name}",
                "owner": owner or "TODO",
                "source_ref": {
                    "name": source["source_name"],
                    "table": source["name"],
                },
                "columns": [],
            }
            evidence = [
                "depends on exactly one dbt source and no models",
                (
                    "no aggregation, join, UNNEST or window "
                    "signals in the SQL; the SELECT list's macro "
                    "invocations are accounted with the columns "
                    "below"
                    if draftable_macro_items
                    else "no aggregation, join, UNNEST, window or "
                    "macro signals in the SQL"
                ),
            ]
            if columns:
                drafted, note = _draft_staging_columns(
                    columns, source_columns or []
                )
                params["columns"] = drafted
                evidence.append(note)
            else:
                evidence.append(
                    "the manifest documents no columns; the column "
                    "mapping must be drafted manually before generation"
                )
            # The documented-columns caveat, generalised
            # beyond bronze (the caveat lived only in
            # bronze evidence). A staging draft is a column
            # mapping, so absent or partial source documentation
            # is exactly what its mappings rest on. Two cases are
            # exempt because the drafting note above already says
            # what the caveat would: the single inferred-rename
            # case (the note names that inference to verify), and
            # the placeholder case - unpaired outputs now draft as
            # `__TODO__` sentinels rather than identity mappings,
            # so the caveat's assumption ("source column names
            # match the output names") is no longer what the draft
            # rests on, and the note names the unresolved outputs
            # and the documentation state instead.
            source_set = set(source_columns or [])
            undocumented = [c for c in columns if c not in source_set]
            unmatched_sources = [
                c for c in (source_columns or []) if c not in set(columns)
            ]
            rename_inferred = (
                len(undocumented) == 1 and len(unmatched_sources) == 1
            )
            placeholders_drafted = any(
                entry.get("source") == _TODO_SENTINEL for entry in drafted
            ) if columns else False
            if placeholders_drafted:
                pass
            elif columns and not source_columns:
                evidence.append(_no_columns_caveat("the source"))
            elif columns and undocumented and not rename_inferred:
                evidence.append(
                    _partial_columns_caveat(
                        "the source",
                        len(columns) - len(undocumented),
                        len(columns),
                    )
                )
            # Macro drafting (Tier 1; Tier 2 resolution
            # inline below): a slot-shaped call
            # pairs with its documented output by alias; the
            # call names its source column, which is stronger
            # evidence than the pairing inference, so it settles
            # the entry's `from` (resolving a placeholder where
            # the inference could not pair the output). The call
            # itself is drafted only when verified against the
            # manifest macro index - existence and arity; an
            # unverifiable call leaves the plain mapping and an
            # evidence line the readiness account turns into an
            # evidence hole.
            for item in draftable_macro_items:
                entry = next(
                    (
                        column_entry
                        for column_entry in params["columns"]
                        if isinstance(column_entry, dict)
                        and column_entry.get("alias") == item["alias"]
                    ),
                    None,
                )
                if entry is None:
                    continue
                column_param: int | None = None
                if item["shape"] == "tier2":
                    # Resolve the Tier 2 candidate: exactly one
                    # quoted argument may name a documented source
                    # column ('day' in date_trunc('day', x) is
                    # identifier-shaped but is not a source
                    # column). Zero or several matches leave the
                    # plain mapping with an evidence line - the
                    # evidence-hole treatment an unverifiable
                    # Tier 1 call gets, never a silent pass.
                    matches = [
                        (position, value)
                        for position, value, _bare in item["candidates"]
                        if value in source_set
                    ]
                    extra_bare = [
                        value
                        for position, value, bare in item["candidates"]
                        if bare and (position, value) not in matches
                    ]
                    if len(matches) != 1 or extra_bare:
                        detail = (
                            "another argument is a bare column "
                            f"reference ('{extra_bare[0]}'), which a "
                            "staging column mapping cannot carry"
                            if extra_bare and len(matches) == 1
                            else "no argument names a documented "
                            "source column, so the call's source "
                            "column cannot be identified"
                            if not matches
                            else "more than one argument names "
                            "a documented source column ("
                            + ", ".join(
                                f"'{value}'" for _, value in matches
                            )
                            + "), so the call's source column is "
                            "ambiguous"
                        )
                        evidence.append(
                            f"the macro call on output '{item['alias']}' "
                            f"({item['reference']}) could not be drafted "
                            f"under macro_column_param: {detail}; the "
                            "draft carries the plain column mapping "
                            "for that output - write the macro column "
                            "into the draft manually if the call is "
                            "being kept"
                        )
                        continue
                    column_param, resolved_column = matches[0]
                    item = {
                        **item,
                        "column": resolved_column,
                        "args": [
                            value
                            for index, value in enumerate(item["values"])
                            if index + 1 != column_param
                        ],
                    }
                entry["source"] = item["column"]
                problem = (
                    verify_macro_reference(
                        macro_index,
                        item["reference"],
                        1 + len(item["args"]),
                        project_name=project_name,
                    )
                    if macro_index is not None
                    else "no manifest macro index was supplied"
                )
                if problem is None:
                    entry["macro"] = item["reference"]
                    if item["args"]:
                        entry["macro_args"] = item["args"]
                    if column_param is not None and column_param > 1:
                        entry["macro_column_param"] = column_param
                        param_name: str | None = None
                        if macro_index is not None:
                            if "." in item["reference"]:
                                pkg, _, nm = item["reference"].partition(".")
                            else:
                                pkg, nm = project_name, item["reference"]
                            signature = (
                                macro_index.get((pkg, nm)) if pkg else None
                            )
                            if signature and column_param <= len(signature):
                                param_name = signature[column_param - 1]["name"]
                        evidence.append(
                            f"the macro call on output '{item['alias']}' "
                            f"({item['reference']}) places its source "
                            f"column at argument position {column_param}"
                            + (
                                f" (the signature's '{param_name}' "
                                "parameter)"
                                if param_name
                                else ""
                            )
                            + " - verified against the manifest - "
                            "existence and arity - and is drafted as "
                            "a staging macro column under "
                            "macro_column_param"
                        )
                    else:
                        evidence.append(
                            f"the macro call on output '{item['alias']}' "
                            f"({item['reference']}) was verified against "
                            "the manifest - existence and arity - and "
                            "is drafted as a staging macro column"
                        )
                else:
                    evidence.append(
                        f"unverified macro '{item['reference']}': "
                        f"the call on output '{item['alias']}' "
                        f"could not be verified against the "
                        f"manifest ({problem}); the draft carries "
                        "the plain column mapping for that output "
                        "- restore the macro call in the draft "
                        "once the macro is verifiable, or leave "
                        "the plain mapping if the call is being "
                        "retired"
                    )
            # Completeness accounting (M3): the mapping above is
            # drafted from the manifest's documented columns, but
            # the draft must account for every output the model's
            # final projection produces - an output the manifest
            # does not document would otherwise vanish from the
            # draft silently while readiness, derived from the
            # draft record, certified the draft complete.
            # Enumerate the projection from the SQL (the parse
            # family the fit's signals already run): an
            # undocumented projected output is drafted as an
            # unresolved placeholder, exactly like an unpaired
            # documented one, so the readiness account names it
            # as a hole and the draft can never read
            # ready_to_promote while it stands; a projection
            # shape that cannot be enumerated falls back to a
            # named caveat (an evidence hole), never to silence.
            # With no documented columns at all the draft
            # carries no mapping and the no-mapping evidence
            # above is the account, so accounting starts from
            # the first documented column.
            if columns:
                projected = _projected_outputs(sql)
                if projected is None:
                    evidence.append(
                        "caveat: the model's final projection "
                        "could not be enumerated from its SQL "
                        "(a shape this assessment cannot resolve: "
                        "a star mixed with explicit items, a star "
                        "over a subquery, or a projection item "
                        "that does not name its output plainly), "
                        "so the drafted columns are the manifest's "
                        "documented columns only; any output the "
                        "model projects beyond the documented set "
                        "is not accounted for in this draft - "
                        "check the column set against the original "
                        "SQL; the equivalence proof verifies it "
                        "before any swap"
                    )
                else:
                    documented_names = {c.lower() for c in columns}
                    unaccounted = [
                        output
                        for output in projected
                        if output.lower() not in documented_names
                    ]
                    if unaccounted:
                        for output in unaccounted:
                            params["columns"].append(
                                {
                                    "source": _TODO_SENTINEL,
                                    "alias": output,
                                }
                            )
                        evidence.append(
                            "columns drafted from the manifest's "
                            "documented columns; the model's final "
                            "projection also names "
                            f"{len(unaccounted)} output(s) the "
                            "manifest does not document: "
                            + ", ".join(unaccounted)
                            + " - each is drafted as an unresolved "
                            f"placeholder (source: {_TODO_SENTINEL}) "
                            "so no projected output is silently "
                            "dropped; draft each mapping from the "
                            "original SQL; `loom validate` rejects "
                            "the placeholder, and the equivalence "
                            "proof verifies completed mappings "
                            "before any swap"
                        )
            # No incremental block: the staging contract does not
            # admit the parameter (it is drafted only where a
            # generator can carry it), so an incremental staging
            # model keeps its loss-warning flag.
            _harvest_column_metadata(node, params, evidence)
            proposal = {
                "generator": "staging",
                "evidence": evidence,
                "drafted_params": params,
            }
            _attach_source_rationale(proposal, node)
            return proposal

    if len(model_deps) == 1 and not source_deps:
        # A dedup QUALIFY whose predicate carries an OR
        # escape retains rows beyond the dedup winner (the
        # witnessed shape keeps null-key rows); the silver
        # contract cannot express the retention, and a proof
        # built on escape-free data would not catch the loss.
        # Blocking signal - no fit, with the construct named.
        escape = _qualify_or_escape(sql)
        if escape is not None:
            return {
                "no_fit": (
                    "no fit rule matched (blocking signal: the "
                    "dedup QUALIFY predicate has an OR escape "
                    f"({escape}) that retains rows beyond the "
                    "ROW_NUMBER() = 1 dedup winner - rows the "
                    "silver contract (dedup_key + dedup_order over "
                    "one input) cannot express); assess manually "
                    "- a bespoke model is a valid end state "
                    "(register it; do not force a generator fit)"
                )
            }
        # A silver fit requires a dedup construct whose
        # filter binds the model's final output - a DISTINCT or
        # ROW_NUMBER token anywhere in the file is not a dedup of
        # the output (percentile marts and LAG/LEAD history
        # models were proposed as silver on exactly that basis).
        # A join, aggregation, DISTINCT or macro anywhere else in
        # the model also blocks: silver dedups one input unchanged.
        hard_signals = [s for s in signals if s != "a window function"]
        binding = (
            None
            if hard_signals or _DISTINCT.search(sql)
            else _dedup_binds_output(sql)
        )
        if binding is not None:
            params: dict[str, Any] = {"input_models": [model_deps[0]]}
            if description is not None:
                params["description"] = description
            if owner is not None:
                params["owner"] = owner
            params["dedup_key"] = ", ".join(binding["keys"])
            evidence = [
                f"depends on exactly one model ({model_deps[0]}) and no sources",
                "ROW_NUMBER() OVER (PARTITION BY...) dedup whose "
                "rn = 1 filter binds the model's final output; the "
                "dedup key is drafted from the partition columns, "
                "with any qualifier (table-alias) prefixes stripped",
            ]
            if binding["order"]:
                params["dedup_order"] = binding["order"]
                evidence.append(
                    "the dedup ORDER BY - the tie-break that decides "
                    "which row survives - is drafted into dedup_order "
                    "so the generated model preserves it"
                )
            else:
                evidence.append(
                    "the original dedup declares no ORDER BY; the "
                    "generated model will order by __loaded_at DESC"
                )
            _harvest_column_metadata(node, params, evidence)
            _draft_incremental(node, params, evidence)
            # The documented-columns caveat for silver -
            # the generated model projects its input's columns, so
            # the draft's column story rests on what the manifest
            # documents for the input model. Absent documentation
            # leaves the projection unverifiable from the
            # manifest; partial documentation (output columns the
            # input does not document - derived columns, in the
            # witnessed enghouse case) names exactly the columns
            # the generated model would not produce.
            if input_columns is not None:
                if not input_columns:
                    evidence.append(
                        _no_columns_caveat("the input model")
                    )
                else:
                    undocumented_on_input = [
                        c for c in columns if c not in set(input_columns)
                    ]
                    if undocumented_on_input:
                        evidence.append(
                            _partial_columns_caveat(
                                "the input model",
                                len(columns) - len(undocumented_on_input),
                                len(columns),
                            )
                            + "; columns documented on the model "
                            "but not on its input: "
                            + ", ".join(undocumented_on_input)
                        )
            proposal = {
                "generator": "silver",
                "evidence": evidence,
                "drafted_params": params,
            }
            _attach_source_rationale(proposal, node)
            return proposal

    blockers = named_signals(signals)
    if _DISTINCT.search(sql):
        blockers.append("SELECT DISTINCT")
    if _ROW_NUMBER_CALL.search(sql):
        blockers.append(
            "ROW_NUMBER() usage with no dedup filter binding the final output"
        )
    if len(source_deps) > 1:
        blockers.append(f"{len(source_deps)} source dependencies")
    if len(model_deps) > 1:
        blockers.append(f"{len(model_deps)} model dependencies")
    if not source_deps and not model_deps:
        if seed_deps:
            # The model's inputs are seeds, read through ref():
            # no fit rule binds a seed, and "no dependencies"
            # would be false. Name the seeds and the decision
            # they force (P1) - the fit outcome does not change.
            blockers.append(
                f"depends on seed(s) {', '.join(seed_deps)} and no "
                "source or model dependencies; staging binds a dbt "
                "source, not a seed ref - declare the seed(s) as "
                "dbt source(s) and bind with source_ref, or bind "
                "the seed's relation with inline_source"
            )
        else:
            blockers.append("no source or model dependencies")
    elif seed_deps:
        blockers.append(
            f"depends on seed(s) {', '.join(seed_deps)}, which no "
            "fit rule binds"
        )
    reason = (
        "no fit rule matched"
        + (f" (blocking signals: {', '.join(blockers)})" if blockers else "")
        + "; assess manually - a bespoke model is a valid end state "
        "(register it; do not force a generator fit)"
    )
    return {"no_fit": reason}


# Consumer kinds in display order for the decomposed consumer
# count: the child unique_id's first segment names the
# kind; anything unlisted falls back to its own segment,
# pluralised, so the decomposition always sums to the total.
_CONSUMER_KIND_ORDER = (
    "models",
    "snapshots",
    "exposures",
    "semantic models",
    "metrics",
    "saved queries",
)
_CONSUMER_KINDS = {
    "model": "models",
    "snapshot": "snapshots",
    "exposure": "exposures",
    "semantic_model": "semantic models",
    "metric": "metrics",
    "saved_query": "saved queries",
}
_CONSUMER_KIND_LABELS = {
    "models": ("model", "models"),
    "snapshots": ("snapshot", "snapshots"),
    "exposures": ("exposure", "exposures"),
    "semantic models": ("semantic model", "semantic models"),
    "metrics": ("metric", "metrics"),
    "saved queries": ("saved query", "saved queries"),
}


def _consumer_kind(child_id: str) -> str:
    segment = child_id.split(".", 1)[0]
    return _CONSUMER_KINDS.get(segment, segment.replace("_", " ") + "s")


def _consumers_label(consumers_by_kind: dict[str, int]) -> str:
    """The decomposed parenthetical for the consumer flag:
    '3 models, 1 metric and 1 saved query'."""
    parts = []
    ordered = [k for k in _CONSUMER_KIND_ORDER if k in consumers_by_kind]
    ordered += sorted(k for k in consumers_by_kind if k not in ordered)
    for kind in ordered:
        count = consumers_by_kind[kind]
        singular, plural = _CONSUMER_KIND_LABELS.get(kind, (kind, kind))
        parts.append(f"{count} {singular if count == 1 else plural}")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def _materialization_scope(
    project_config: dict[str, Any],
    project_name: str | None,
    model_path: str | None,
) -> dict[str, Any] | None:
    """What dbt_project.yml's `models:` tree says about one
    model's materialization, and at what scope it says it.

    Returns the project-level `+materialized` (set on the
    project's own tree root) and the deepest directory-scoped
    `+materialized` covering the model's original path, with
    its depth (1 = the first directory under the model root).
    None when the project carries no models tree for this
    project - no config fact can be stated then."""
    models = project_config.get("models")
    if not isinstance(models, dict) or not isinstance(project_name, str):
        return None
    tree = models.get(project_name)
    if not isinstance(tree, dict):
        return {"project_value": None, "scoped_value": None, "scoped_depth": 0}
    project_value = tree.get("+materialized")
    if not isinstance(project_value, str):
        project_value = None
    segments: list[str] = []
    if isinstance(model_path, str):
        parts = [
            part
            for part in model_path.replace("\\", "/").split("/")
            if part
        ]
        for root in _configured_paths(project_config, "model"):
            root_parts = [part for part in root.split("/") if part]
            if parts[: len(root_parts)] == root_parts:
                segments = parts[len(root_parts) : -1]
                break
    scoped_value: str | None = None
    scoped_depth = 0
    node = tree
    for depth, segment in enumerate(segments, start=1):
        child = node.get(segment)
        if not isinstance(child, dict):
            break
        node = child
        value = node.get("+materialized")
        if isinstance(value, str):
            scoped_value, scoped_depth = value, depth
    return {
        "project_value": project_value,
        "scoped_value": scoped_value,
        "scoped_depth": scoped_depth,
    }


def _interface_flags(
    node: dict[str, Any],
    consumers_by_kind: dict[str, int],
    attached_tests: int,
    *,
    incremental_drafted: bool = False,
    materialization_scope: dict[str, Any] | None = None,
) -> list[str]:
    consumers = sum(consumers_by_kind.values())
    flags: list[str] = []
    config = node.get("config") or {}
    contract = config.get("contract") or {}
    if isinstance(contract, dict) and contract.get("enforced"):
        flags.append(
            "carries an enforced dbt contract (contract: {enforced: true}); "
            "a Loom-generated replacement drops enforcement unless "
            "`contract_enforced` is declared in the Loom metadata for "
            "the replacement (with column_types completing its types)"
        )
    # The flags lead with what a conversion puts at risk for
    # other people - attached tests and downstream consumers - and
    # only then list the model's own configuration. (The counts are
    # parameters, so leading with them is just block order here.)
    if attached_tests:
        flags.append(
            f"{attached_tests} test(s) are attached to this model in the "
            "manifest; a swap must keep or deliberately replace them"
        )
    if consumers:
        flags.append(
            f"{consumers} downstream consumer(s) in the manifest "
            f"({_consumers_label(consumers_by_kind)})"
        )
    # Dbt resolves `config.access` for every
    # model, defaulting to 'protected' when nothing is configured
    # anywhere (verified against a dbt 1.12 manifest: a model with
    # no access configuration still carries access: 'protected').
    # Flagging the resolved value therefore flagged 705/705 models
    # in real migration sessions - and the claim is wrong for
    # 'protected', whose point is that outside consumers may NOT
    # depend on the model. A Loom-generated replacement sets no
    # access, so it lands on the same 'protected' default: nothing
    # is lost, and nothing is flagged. Only the non-default values
    # carry a conversion consequence: 'public' is lost (the
    # replacement narrows back to 'protected'), and a 'private'
    # restriction is silently widened to 'protected'.
    access = config.get("access")
    if access == "public":
        flags.append(
            "access is 'public' (dbt Mesh); consumers outside this "
            "project may depend on its interface"
        )
    elif access == "private":
        flags.append(
            "access is 'private' (dbt Mesh); a Loom-generated "
            "replacement carries no access restriction and would "
            "default to 'protected', widening who can reference "
            "this model"
        )
    # Group membership gates cross-group Mesh refs and cannot
    # be re-declared in Loom metadata today.
    group = config.get("group")
    if isinstance(group, str) and group:
        flags.append(
            f"belongs to dbt group {group!r} (config.group); a Loom "
            "conversion does not carry group membership, so the group "
            "must be re-declared by hand or cross-group references "
            "may break"
        )
    deprecation_date = node.get("deprecation_date") or config.get(
        "deprecation_date"
    )
    if isinstance(deprecation_date, (str, int, float)) and deprecation_date:
        flags.append(
            f"carries a dbt deprecation date ({deprecation_date}); a "
            "Loom conversion does not carry it, so the deprecation "
            "must be re-declared by hand"
        )
    # Materialization is build semantics, not shape. An
    # incremental model converted to a generated replacement loses
    # its incremental configuration silently unless flagged.
    materialized = config.get("materialized")
    if materialized == "incremental" and not incremental_drafted:
        # Where a conversion draft carries the declared
        # configuration (a bronze/silver fit's incremental block),
        # the loss this flag warns about does not happen, so the
        # flag does not fire. It stays for models without a draft
        # - no fit, or a staging fit, whose generator cannot carry
        # the block.
        details = ", ".join(
            f"{key}={config[key]!r}"
            for key in ("incremental_strategy", "unique_key", "on_schema_change")
            if config.get(key) is not None
        )
        flags.append(
            "materialized as 'incremental'"
            + (f" ({details})" if details else "")
            + "; a Loom-generated replacement does not carry this "
            "incremental configuration, so converting it would "
            "silently turn incremental builds into full rebuilds"
        )
    elif materialized == "ephemeral":
        flags.append(
            "materialized as 'ephemeral' (never persisted); a "
            "Loom-generated replacement is a persisted model, so "
            "converting it would create a real table/view where "
            "none exists today"
        )
    # Materialization by declaration or by path scope (P4):
    # disclosure only - no generator takes a materialized
    # parameter, so both facts are conversion consequences the
    # record must state. A model's own declaration (its resolved
    # materialization differs from everything dbt_project
    # config would give it, or from dbt's default where the
    # project configures nothing) is not carried. A
    # directory-scoped dbt_project value IS what the model runs
    # with today, but the generated model lands under
    # models/_generated/, outside that scope. Project-root
    # config needs no flag: the generated path stays under it.
    # Incremental and ephemeral are flagged above in their own
    # stronger terms and are not repeated here.
    if (
        materialization_scope is not None
        and isinstance(materialized, str)
        and materialized not in ("incremental", "ephemeral")
    ):
        scoped_value = materialization_scope.get("scoped_value")
        effective = (
            scoped_value
            if scoped_value is not None
            else materialization_scope.get("project_value")
        )
        if effective is None:
            declared_differs = materialized != "view"
            path_scoped = False
        else:
            declared_differs = materialized != effective
            path_scoped = (
                not declared_differs
                and (materialization_scope.get("scoped_depth") or 0) >= 1
            )
        if declared_differs:
            flags.append(
                f"materialized as {materialized!r} by the model's "
                "own declaration; a Loom conversion carries no "
                "materialized config (no generator takes one), so "
                "the generated model takes the generated path's "
                "materialization instead - a swap can change how "
                "this model is built"
            )
        elif path_scoped:
            flags.append(
                "path-scoped dbt_project config sets "
                f"materialized: {scoped_value!r} over this model's "
                f"original path ({node.get('original_file_path')}); "
                "a generated replacement lands under "
                "models/_generated/, outside that scope, so a swap "
                "can change its materialization"
            )
    # Grants and physical warehouse configuration have no
    # Loom expression at all.
    grants = config.get("grants")
    if isinstance(grants, dict):
        rendered = ", ".join(
            f"{key}: {', '.join(str(v) for v in values)}"
            for key, values in sorted(grants.items())
            if values
        )
        if rendered:
            flags.append(
                f"carries dbt grants ({rendered}); grants are not "
                "carried by a Loom conversion; re-apply them by hand"
            )
    physical: list[str] = []
    for key in ("partition_by", "cluster_by", "file_format", "catalog_name"):
        value = config.get(key)
        if value is None:
            value = node.get(key)
        if value is not None:
            physical.append(f"{key}={value!r}")
    if physical:
        flags.append(
            "carries physical warehouse configuration ("
            + ", ".join(physical)
            + ") that is not carried by a Loom conversion; re-apply "
            "it by hand or warehouse cost/performance may regress"
        )
    return flags


# --- The scaffold tier ---------------------------------------------
#
# Between register and convert sits the tier for the majority:
# models in dbt's hand-authored layers (staging / intermediate /
# mart) whose shapes no generator rule matched. A scaffold is a
# starting instance carrying only facts copied from the manifest -
# the layer, the exact input set, the declared interface - with
# the transform as the single named authoring hole and a target
# generator named by layer as the authoring DESTINATION. It is
# not, and is never worded as, a match between the model's shape
# and that generator: the practitioner authors the transform, and
# promotion is gated on a proof against the declared interface
# (see the playbook's scaffold pathway).

# dbt's canonical layer folders. A model's directory under the
# model root is a declared fact of the project's own organisation,
# so the folder is read first; the naming conventions corroborate
# where the layout does not say.
_LAYER_BY_FOLDER: dict[str, str] = {
    "staging": "staging",
    "intermediate": "intermediate",
    "mart": "mart",
    "marts": "mart",
}

_LAYER_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("stg_", "staging"),
    ("int_", "intermediate"),
    ("fct_", "mart"),
    ("fact_", "mart"),
    ("dim_", "mart"),
    ("mart_", "mart"),
    ("obt_", "mart"),
)


def _model_layer(name: str, model_path: str | None) -> str | None:
    """The dbt layer a model sits in, or None when neither the
    project's layout nor its naming conventions place it in one.

    The manifest's ``original_file_path`` directory segments are
    read first (dbt's canonical layout is ``staging/`` /
    ``intermediate/`` / ``mart/`` under the model root), then the
    name prefixes (``stg_`` / ``int_`` / the mart-layer prefixes).
    Both are the project's own declarations; no SQL is read."""
    if model_path:
        for segment in _path_segments(model_path):
            layer = _LAYER_BY_FOLDER.get(segment)
            if layer is not None:
                return layer
    for prefix, layer in _LAYER_BY_PREFIX:
        if name.startswith(prefix):
            return layer
    return None


def _scaffold_target_generator(
    layer: str, classification: str, materialized: str | None
) -> str:
    """The authoring destination for a layer.

    Staging authors toward the staging generator; the intermediate
    layer toward `normalized_table` (or `view` when the model is
    declared as a view/ephemeral - its build semantics copied, not
    reinterpreted); the mart layer toward `fact` / `dimension`
    where the model's classification names one, else `mart`. The
    destination says where the authoring is headed; it claims
    nothing about the model's shape."""
    if layer == "staging":
        return "staging"
    if layer == "intermediate":
        if materialized in ("view", "ephemeral"):
            return "view"
        return "normalized_table"
    if classification == "kimball fact":
        return "fact"
    if classification == "kimball dimension":
        return "dimension"
    return "mart"


def _declared_interface(
    node: dict[str, Any],
    *,
    generic_tests: list[dict[str, Any]],
    unit_tests: list[dict[str, Any]],
    semantic_models: list[dict[str, Any]],
) -> dict[str, Any]:
    """The interface a project declares for a model, copied
    verbatim from the manifest.

    Every block is present only where the project states one:
    an enforced contract (with the documented columns' types and
    constraints - the proof's declared column spec), the dbt unit
    tests attached to the model (their given/expect fixtures are
    the proof oracle), the generic tests attached to the model -
    every kind, built-in or project-defined, copied as declared
    by `_generic_test_entry` - plus singular tests
    (bespoke SQL assertions, copied with their SQL), the semantic
    models reading it (declared entities / dimensions / measures:
    grain and keys), and model config facts (materialization,
    partition/cluster keys, Mesh access, group). Nothing is
    inferred: a `relationships` target is copied as declared,
    never derived from names. Access is copied only when it is a
    non-default value - dbt resolves `protected` for every model,
    configured or not, so the resolved default would
    assert a declaration the project may not have made."""
    interface: dict[str, Any] = {}
    config = node.get("config") or {}
    contract = config.get("contract") or node.get("contract") or {}
    if isinstance(contract, dict) and contract.get("enforced"):
        block: dict[str, Any] = {"enforced": True}
        columns: dict[str, Any] = {}
        for column, spec in sorted((node.get("columns") or {}).items()):
            if not isinstance(spec, dict):
                continue
            entry: dict[str, Any] = {}
            data_type = spec.get("data_type")
            if isinstance(data_type, str) and data_type.strip():
                entry["data_type"] = data_type
            constraints = spec.get("constraints")
            if isinstance(constraints, list) and constraints:
                entry["constraints"] = deepcopy(constraints)
            if entry:
                columns[column] = entry
        if columns:
            block["columns"] = columns
        interface["contract"] = block
    if unit_tests:
        interface["unit_tests"] = unit_tests
    if generic_tests:
        interface["generic_tests"] = generic_tests
    if semantic_models:
        interface["semantic_models"] = semantic_models
    config_facts: dict[str, Any] = {}
    materialized = config.get("materialized")
    if isinstance(materialized, str) and materialized:
        config_facts["materialized"] = materialized
    for key in ("partition_by", "cluster_by"):
        value = config.get(key)
        if value is None:
            value = node.get(key)
        if value is not None:
            config_facts[key] = deepcopy(value)
    access = config.get("access")
    if access in ("public", "private"):
        config_facts["access"] = access
    group = config.get("group")
    if isinstance(group, str) and group:
        config_facts["group"] = group
    if config_facts:
        interface["config"] = config_facts
    return interface


def _scaffold_wiring(
    target: str,
    source_deps: list[dict[str, str]],
    model_deps: list[str],
) -> dict[str, Any]:
    """The input parameters of the target generator that the
    manifest dependency graph wires mechanically.

    Single-input generators are wired only when
    the graph declares exactly one model input - wiring a list
    into a one-entry `input_models` would be a guess. Multi-input
    generators (hub, gold, view) take the
    whole input list; `staging` takes its one source. Anything
    else stays unwired and is named in the authoring hole."""
    params: dict[str, Any] = {}
    if target == "staging":
        if len(source_deps) == 1 and not model_deps:
            source = source_deps[0]
            params["source_ref"] = {
                "name": source["source_name"],
                "table": source["name"],
            }
    elif target == "view":
        if model_deps:
            params["input_models"] = list(model_deps)
    elif target in ("hub", "gold"):
        if model_deps:
            params["input_models"] = list(model_deps)
    elif len(model_deps) == 1:
        params["input_models"] = [model_deps[0]]
    return params


# The states of a not-yet-promoted scaffold, every one of
# them derived from what is on disk in the drafts directory at
# assessment time - a draft set is a draft record plus its
# starting instance document, and the pair's presence is the
# whole evidence:
#
# - `untouched`: the full draft set is on disk (or no scaffold
#   drafts have been written for this project at all yet, the
#   standing pre-write state);
# - `deleted`: exactly one half of the set is on disk - a set
#   was written and part of it was deleted by hand;
# - `missing`: draft sets exist for this project but none for
#   this model - its set was removed whole, or never written
#   while its peers were; either way, no draft is on disk, and
#   reporting `untouched` would claim one is.
_SCAFFOLD_CANDIDATE_STATES = ("untouched", "deleted", "missing")


def _authoring_hole_note(
    target: str,
    params: dict[str, Any],
    seed_deps: list[str],
) -> str:
    """The authoring hole's note for one scaffold.

    The base note names the transform as the one open task. A
    staging scaffold adds the two facts its author needs at
    exactly this point (P1, P6): how a computation is carried
    (an expression item for a plain-SQL computation, the
    column macro slot for a templated one), and, when the
    model's source is not already wired, the source-binding
    decision - stated, never pre-wired (D4)."""
    note = (
        "the model's transform logic - the SQL the project "
        "hand-authors today - is the single authoring task "
        "this scaffold leaves open; it is expressed as the "
        f"target generator's ({target}) parameters at "
        "promotion, by the practitioner"
    )
    if target != "staging":
        return note
    note += (
        "; a computation on one column is carried in the "
        "columns mapping in one of two forms: an expression "
        "item - {expression, alias}, the computation as plain "
        "SQL text (convert pre-places expression items it "
        "finds in the model's final projection) - or that "
        "column's macro slot - {source, alias, macro}, with "
        "macro_args for the call's other arguments - when the "
        "computation needs templating"
    )
    if "source_ref" in params or "inline_source" in params:
        return note
    if seed_deps:
        note += (
            f"; the model's input arrives as seed ref(s) "
            f"({', '.join(seed_deps)}), which staging cannot "
            "bind: decide the source binding - declare the "
            "seed(s) as dbt source(s) and bind with source_ref "
            "{name, table}, or bind the seed's relation with "
            "inline_source {name, schema, table}, which makes "
            "Loom generate the source declaration itself"
        )
    else:
        note += (
            "; staging binds exactly one dbt source, and none "
            "is wired here: decide the source binding - "
            "source_ref {name, table} against a declared dbt "
            "source, or inline_source {name, schema, table}, "
            "which makes Loom generate the source declaration "
            "itself"
        )
    return note


def _scaffold_record(
    *,
    name: str,
    node: dict[str, Any],
    state: str,
    fit: dict[str, Any] | None,
    source_deps: list[dict[str, str]],
    model_deps: list[str],
    seed_deps: list[str] | None = None,
    model_path: str | None = None,
    generic_tests: list[dict[str, Any]],
    carriage_tests: list[dict[str, Any]] | None = None,
    unit_tests: list[dict[str, Any]],
    semantic_models: list[dict[str, Any]],
    bundle_on_disk: bool,
    instance_on_disk: bool,
    drafts_written: bool,
) -> dict[str, Any] | None:
    """The scaffold record for one model, or None.

    A record exists for a model in a dbt layer that no generator
    rule matched (unknown or registered - the same reach as fit
    proposals), and for a controlled model whose scaffold draft
    draft set is still on disk: its scaffold has been promoted,
    which the re-assessment reports as state. The state is
    computed from what is on disk at assessment time - the
    drafts directory and the inventory - and nothing else; no
    state is persisted for it and nothing leaves the machine.
    A candidate's state distinguishes a draft set that is
    present (`untouched`) from one that was partly deleted by
    hand (`deleted`) or is absent while the project has draft
    sets for other models (`missing`) - previously every
    candidate reported `untouched`, so a hand-deleted scaffold
    was invisible to the re-assessment."""
    layer = _model_layer(name, model_path)
    if layer is None:
        return None
    candidate = (
        state != "controlled_by_loom"
        and fit is not None
        and "no_fit" in fit
    )
    if not candidate and not (
        bundle_on_disk and state == "controlled_by_loom"
    ):
        return None
    materialized = (node.get("config") or {}).get("materialized")
    classification, _ = _classification(name, materialized)
    target = _scaffold_target_generator(layer, classification, materialized)
    params = _scaffold_wiring(target, source_deps, model_deps)
    # Carriage (P9, ruling D5): an authoring draft carries the
    # model's declared generic tests in its instance exactly as
    # a fit draft does, for every target generator whose
    # contract accepts generic_tests - the asymmetry (tests in
    # the record's declared interface but never in the instance)
    # silently steered practitioners to re-declare them in the
    # wrong layer. Carried content is not a readiness hole.
    carriage_evidence: list[str] = []
    if carriage_tests:
        from loom.contracts import GENERATOR_CONTRACTS as _CONTRACTS

        if any(
            param.name == "generic_tests"
            for param in _CONTRACTS[target]
        ):
            params["generic_tests"] = deepcopy(carriage_tests)
            carriage_evidence.append(
                "the model's declared generic tests are copied "
                "into the draft's generic_tests "
                f"({len(carriage_tests)} declaration(s)) - "
                "carriage: they generate with the model after "
                "promotion"
            )
    description = _manifest_description(node)
    params["description"] = (
        description if description is not None else f"TODO: describe {name}"
    )
    owner = _manifest_owner(node)
    params["owner"] = owner if owner is not None else "TODO"
    from loom.contracts import GENERATOR_CONTRACTS

    hole_params = [
        param.name
        for param in GENERATOR_CONTRACTS[target]
        if param.required and param.name not in params
    ]
    # Pre-placement (M3, ruling R2): a staging authoring draft
    # carries the expression items of the model's effective
    # final projection (the output chain's first non-star list)
    # as real {expression, alias} column params - assistance,
    # not a fit: they were FOUND in the model's SQL, and the
    # record states so. Placed after hole_params is computed,
    # deliberately: the columns parameter remains an authoring
    # hole (the practitioner verifies and completes the
    # mapping), so the model still reports needs_authoring.
    # Chain-bound items (in a CTE the chain does not resolve
    # through bare stars) are named in the record's construct
    # inventory only, never placed.
    #
    # Completeness accounting (F1): the pre-placed items are
    # only part of the projection, and a partial columns
    # parameter defeats the unwired-columns hole - the
    # readiness account reads the parameter's presence as
    # wiring, so a draft carrying only the expression items
    # certified ready while generation dropped every other
    # output. When expression items are pre-placed, the draft
    # therefore accounts for EVERY output the effective
    # projection names: each remaining named output is drafted
    # as an unresolved placeholder (source: __TODO__) in
    # projection order - the fit drafts' hole language, whose
    # readiness derivation names each standing placeholder as
    # a hole - and a projection the enumeration cannot fully
    # name falls back to a caveat, never to silence. With no
    # expression items to pre-place the parameter stays
    # unwired and its authoring hole stands, exactly as
    # before: the accounting completes what pre-placement
    # starts, it does not widen it.
    if target == "staging" and "columns" not in params:
        sql = node.get("compiled_code") or node.get("raw_code") or ""
        account = _chain_projection_accounting(sql)
        placed = [
            entry
            for entry in (account or {}).get("items") or []
            if entry["kind"] == "expression"
        ]
        if placed:
            assert account is not None
            columns: list[dict[str, str]] = []
            unmapped: list[str] = []
            for entry in account["items"]:
                if entry["kind"] == "expression":
                    columns.append(
                        {
                            "expression": entry["expression"],
                            "alias": entry["alias"],
                        }
                    )
                else:
                    columns.append(
                        {"source": _TODO_SENTINEL, "alias": entry["alias"]}
                    )
                    unmapped.append(entry["alias"])
            params["columns"] = columns
            carriage_evidence.append(
                f"{len(placed)} expression item(s) from the "
                "model's final projection are pre-placed in the "
                "draft's columns as {expression, alias} params "
                "("
                + ", ".join(
                    f"{item['expression']} as {item['alias']}"
                    for item in placed
                )
                + ") - found in the model's SQL, not fitted: "
                "verify each before promotion, and complete the "
                "column mapping around them"
            )
            if unmapped:
                carriage_evidence.append(
                    "the model's final projection also names "
                    f"{len(unmapped)} output(s) the draft does "
                    "not map: "
                    + ", ".join(unmapped)
                    + " - each is drafted as an unresolved "
                    f"placeholder (source: {_TODO_SENTINEL}) in "
                    "the draft's columns so no projected output "
                    "is silently dropped; draft each mapping "
                    "from the original SQL; `loom validate` "
                    "rejects the placeholder, and the equivalence "
                    "proof verifies completed mappings before "
                    "any swap"
                )
            if not account["complete"]:
                carriage_evidence.append(
                    "caveat: the model's final projection "
                    "could not be enumerated in full from its "
                    "SQL (a star mixed with explicit items, or "
                    "a projection item that does not name its "
                    "output plainly), so the drafted columns "
                    "account for the outputs that could be "
                    "named only; any output the model projects "
                    "beyond those is not accounted for in this "
                    "draft - check the column set against the "
                    "original SQL before promotion"
                )
    inputs: dict[str, Any] = {}
    if model_deps:
        inputs["models"] = list(model_deps)
    if source_deps:
        inputs["sources"] = [
            {"source_name": dep["source_name"], "name": dep["name"]}
            for dep in source_deps
        ]
    if seed_deps:
        # Seeds are inputs the manifest graph declares but no
        # wiring binds (P1): named here so the authoring record
        # accounts for every dependency the model has.
        inputs["seeds"] = list(seed_deps)
    scaffold: dict[str, Any] = {
        "layer": layer,
        "classification": classification,
        "target_generator": target,
        "inputs": inputs,
        "declared_interface": _declared_interface(
            node,
            generic_tests=generic_tests,
            unit_tests=unit_tests,
            semantic_models=semantic_models,
        ),
        "authoring_hole": {
            "name": "transform",
            "params": hole_params,
            "note": _authoring_hole_note(
                target, params, seed_deps or []
            ),
        },
        "drafted_params": params,
        "evidence": carriage_evidence,
        "state": (
            "promoted"
            if state == "controlled_by_loom"
            else "untouched"
            if bundle_on_disk and instance_on_disk
            else "deleted"
            if bundle_on_disk != instance_on_disk
            else "missing"
            if drafts_written
            else "untouched"
        ),
    }
    _attach_source_rationale(scaffold, node)
    return scaffold


def _adapter_warnings(
    project_dir: Path, document: dict[str, Any]
) -> list[str]:
    """Provenance warning when the manifest was produced by a
    different adapter than the project's profile declares - the
    signature of a substitute-adapter parse (real
    projects whose own toolchain cannot parse them are rescued by
    parsing with a substitute adapter such as DuckDB). The
    substitute parse is blessed for assessment only, so the mismatch
    is a warning, never a failure; but the fiction must be labelled,
    because dialect-sensitive conclusions in this report derive
    from the substitute's parse. No warning when the manifest
    records no adapter, when no profile adapter can be resolved
    (absence of evidence is not a mismatch), or when the two agree
    after alias canonicalisation (e.g. 'synapse' vs 'azuresynapse')."""
    metadata = document.get("metadata") or {}
    manifest_adapter = metadata.get("adapter_type")
    if not isinstance(manifest_adapter, str) or not manifest_adapter.strip():
        return []
    profile_adapter = detect_profile_adapter(project_dir)
    if profile_adapter is None:
        return []
    if canonical_adapter_name(manifest_adapter) == canonical_adapter_name(
        profile_adapter
    ):
        return []
    return [
        "manifest adapter mismatch: the manifest was produced with "
        f"the {manifest_adapter.strip()!r} adapter, but the "
        "project's profile declares the "
        f"{profile_adapter!r} adapter, so the manifest came from a "
        "substitute-adapter parse. The assessment is still usable, "
        "but dialect-sensitive conclusions (fit shapes, interface "
        "flags and drafted configuration) were derived from the "
        "substitute adapter's parse, not the project's own "
        "toolchain; see the migration playbook's prerequisites."
    ]


def _manifest_provenance(document: dict[str, Any]) -> str:
    """The manifest's compilation provenance, declared on
    the report (``manifest_provenance``).

    ``dbt parse`` does not compile model SQL, so a parse-produced
    manifest carries empty ``compiled_code`` on every model node;
    a manifest produced by ``dbt build`` / ``dbt compile`` carries
    the compiled (macro-expanded) SQL. The discriminator is the
    manifest's own content, checked on model nodes: any model
    node with populated ``compiled_code`` marks the manifest
    build-produced. The distinction matters because the fit
    rules read compiled SQL where it exists (falling back to the
    authored ``raw_code``): on a build-produced manifest, macro
    calls are already expanded, so shape signals the authored
    SQL would have raised no longer fire
    - the same jaffle project proposed 2 fits from a parse
    manifest and 5–6 from a build manifest)."""
    for node in (document.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") != "model":
            continue
        compiled = node.get("compiled_code")
        if isinstance(compiled, str) and compiled.strip():
            return "build-produced"
    return "parse-produced"


def _provenance_warnings(provenance: str) -> list[str]:
    """The warning a build-produced manifest earns.

    Fit calibration assumes a parse-produced manifest (see
    ``_manifest_provenance``); on a build-produced one the fit
    rules read macro-expanded compiled SQL instead of the
    authored SQL, so proposals can differ from the calibrated
    behaviour. A parse-produced manifest - the calibrated input
    - earns no warning; the declaration on the report still
    names the provenance either way."""
    if provenance != "build-produced":
        return []
    return [
        "manifest provenance: the manifest is build-produced "
        "(its model nodes carry compiled SQL from `dbt build` / "
        "`dbt compile`), but fit calibration assumes a "
        "parse-produced manifest (`dbt parse` does not compile "
        "model SQL). On a build-produced manifest the fit rules "
        "read macro-expanded compiled SQL rather than the "
        "authored SQL, so fit proposals can differ; regenerate "
        "the manifest with `dbt parse` and re-run the assessment "
        "before acting on the fit proposals."
    ]


def _generic_test_entry(test_node: dict[str, Any]) -> dict[str, Any]:
    """The declared-interface copy of one manifest test node.

    A generic test - built-in (`unique`, `not_null`,
    `relationships`, `accepted_values`) or project-defined
    (`unique_combination_of_columns`, `expression_is_true`,
    `incremental_not_null`, any other name a project declares) -
    is copied as declared: its name, the column it attaches to
    where the manifest names one, and its kwargs verbatim
    (a multi-column test's column list rides in its kwargs, as
    the manifest records it). Only the built-in
    kinds were copied at first, and `accepted_values` was added
    later; the rest were counted in the interface flags but
    never copied, silently. Every named kind is now copied, so
    nothing a project declares is counted-but-not-copied.

    A singular test - a bespoke SQL assertion in the project's
    tests/ directory - carries no `test_metadata`; it is copied
    as its name and its SQL verbatim.

    A node whose metadata cannot be read faithfully (a
    `test_metadata` that is not a mapping, or a test name that
    is not a string) yields an explicit `uncopied` marker entry
    naming why, rather than being dropped in silence: the flags
    count every attached test, so a silently missing copy would
    misstate the interface the proof checks against."""
    test_meta = test_node.get("test_metadata")
    test_name = (
        test_meta.get("name") if isinstance(test_meta, dict) else None
    )
    if isinstance(test_name, str) and test_name:
        entry: dict[str, Any] = {"test": test_name}
        column_name = test_node.get("column_name")
        if isinstance(column_name, str) and column_name:
            entry["column_name"] = column_name
        kwargs = test_meta.get("kwargs")
        if isinstance(kwargs, dict) and kwargs:
            entry["kwargs"] = deepcopy(kwargs)
        return entry
    if test_name is None and (
        test_meta is None or isinstance(test_meta, dict)
    ):
        entry = {"test": "singular"}
        node_name = test_node.get("name")
        if isinstance(node_name, str) and node_name:
            entry["name"] = node_name
        raw_code = test_node.get("raw_code")
        if isinstance(raw_code, str) and raw_code.strip():
            entry["sql"] = raw_code
        return entry
    return {
        "test": "uncopied",
        "reason": (
            "the manifest's test_metadata for this test could not "
            "be read as a named generic test, so its definition "
            "was not copied; it is counted in the interface flags "
            "but is not part of the declared interface copy"
        ),
    }


def _carriage_test_entry(test_node: dict[str, Any]) -> dict[str, Any] | None:
    """One manifest test node as a `generic_tests` carriage
    declaration for a conversion draft, or None when the
    node is not a carriable generic test (a singular test is a
    bespoke SQL file, not a declaration; an `uncopied` marker
    names a definition Loom could not read).

    Starts from the declared-interface copy and adjusts for
    carriage: the kwargs dbt itself injects into every generic
    test's manifest kwargs (`model`, `column_name`) are NOT
    copied - dbt re-derives them from the attachment at build,
    and the manifest's resolved `model` value is a
    get_where_subquery expression naming the original model.
    A declared severity of warn IS copied (as
    `config: {severity: warn}`): it is the one resolved-config
    value whose loss would change the test's behaviour after
    conversion (warn would silently become error)."""
    entry = _generic_test_entry(test_node)
    if entry.get("test") in ("singular", "uncopied"):
        return None
    carried: dict[str, Any] = {"test": entry["test"]}
    if entry.get("column_name"):
        carried["column_name"] = entry["column_name"]
    kwargs = {
        key: deepcopy(value)
        for key, value in (entry.get("kwargs") or {}).items()
        if key not in ("model", "column_name")
    }
    if kwargs:
        carried["kwargs"] = kwargs
    config = test_node.get("config")
    severity = config.get("severity") if isinstance(config, dict) else None
    if isinstance(severity, str) and severity.casefold() == "warn":
        carried["config"] = {"severity": "warn"}
    return carried


def _attach_carried_generic_tests(
    fit: dict[str, Any], carriage: list[dict[str, Any]]
) -> None:
    """Copy a fitted model's carriable generic tests into the
    draft instance's `generic_tests`: the completeness
    mirror of the declared-interface copy - the interface keeps
    the proof record; this puts the declarations where they
    generate with the model after promotion. Carried content is
    not a readiness hole: the practitioner declared it, Loom
    hosts it."""
    if fit.get("generator") not in ("staging", "bronze", "silver"):
        return
    params = fit.get("drafted_params")
    if not isinstance(params, dict) or not carriage:
        return
    params["generic_tests"] = deepcopy(carriage)
    fit.setdefault("evidence", []).append(
        "the model's declared generic tests are copied into "
        f"the draft's generic_tests ({len(carriage)} "
        "declaration(s)) - carriage: they generate with the "
        "model after promotion"
    )


def migration_assessment(
    project_dir: Path,
    manifest_path: Path | None = None,
    *,
    only_models: set[str] | None = None,
) -> dict[str, Any]:
    """Build the assessment report for a project (ADR 0025).

    ``only_models`` (a ``--select`` narrowing) restricts the
    report's model list - and therefore its summary and both
    writers - to the named models. Each kept model's record
    still carries whole-project facts (its consumer and test
    counts read the full manifest graph); only the pool
    narrows."""
    # Canonicalise the root like the sibling manifest-backed commands
    #: a relative --project-dir must behave identically to the
    # absolute path, including for the manifest locality check.
    project_dir = Path(project_dir).expanduser().resolve()
    manifest_file, document = read_manifest(
        project_dir,
        manifest_path,
        sections=("nodes", "sources", "exposures"),
        project_local=True,
    )
    # The assessment derives everything it
    # reports from the manifest, so it must refuse a stale one -
    # the same freshness guard `plan` and the coverage commands
    # already invoke. Before this, `register`/`convert` exited 0
    # on a stale manifest with no warning in either format, while
    # the sibling commands hard-failed naming the stale files.
    validate_model_freshness(project_dir, manifest_file, document)
    project_name = document["metadata"]["project_name"]
    # The macros section is read opportunistically from the
    # returned document rather than requested: read_manifest
    # hard-requires every requested section, and a manifest
    # without a macros section must assess with an empty index
    # (macro calls then surface as unverifiable, never as a
    # read failure).
    macro_index = manifest_macro_index(document)
    entries = validate_project(project_dir)
    known: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry.data.get("name")
        if isinstance(name, str):
            known[name] = {
                "kind": "generated instance" if entry.is_generated else "registration",
                "state": (
                    "controlled_by_loom"
                    if entry.is_generated
                    else "known_not_controlled"
                ),
                "generator": entry.data.get("generator"),
                "classification": entry.data.get("classification"),
                "path": entry.path,
            }

    nodes = document["nodes"]
    sources = document.get("sources") or {}

    # Reverse dependency map for consumer/test counts: built
    # from every manifest section `migration_plan` treats as
    # consumers (`migration_plan._CONSUMER_SECTIONS`), not just
    # nodes + exposures - semantic models, metrics and saved queries
    # register as consumers of the models they read, and unit tests
    # register as attached tests, instead of both being invisible.
    children: dict[str, list[str]] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            children.setdefault(dep, []).append(unique_id)
    for exposure in (document.get("exposures") or {}).values():
        if isinstance(exposure, dict):
            for dep in (exposure.get("depends_on") or {}).get("nodes") or []:
                children.setdefault(dep, []).append(
                    f"exposure.{exposure.get('name')}"
                )
    for section in ("semantic_models", "metrics", "saved_queries", "unit_tests"):
        for unique_id, resource in (document.get(section) or {}).items():
            if not isinstance(resource, dict):
                continue
            for dep in (resource.get("depends_on") or {}).get("nodes") or []:
                children.setdefault(dep, []).append(unique_id)

    # Non-model resources the assessment saw but does not
    # assess, counted by type, so the summary never presents model
    # counts as the whole project.
    seen_but_not_assessed: dict[str, int] = {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        resource_type = node.get("resource_type")
        if not isinstance(resource_type, str) or resource_type == "model":
            continue
        package = node.get("package_name")
        if isinstance(package, str) and package != project_name:
            continue
        seen_but_not_assessed[resource_type] = (
            seen_but_not_assessed.get(resource_type, 0) + 1
        )
    # The semantic-layer sections are top-level manifest
    # sections, not nodes, and were invisible here: count
    # them alongside the node types, under the same
    # project-owned filter.
    for section, kind in (
        ("semantic_models", "semantic_model"),
        ("metrics", "metric"),
        ("saved_queries", "saved_query"),
    ):
        for resource in (document.get(section) or {}).values():
            if not isinstance(resource, dict):
                continue
            package = resource.get("package_name")
            if isinstance(package, str) and package != project_name:
                continue
            seen_but_not_assessed[kind] = (
                seen_but_not_assessed.get(kind, 0) + 1
            )

    # The declared-interface indexes the scaffold tier
    # copies from - generic tests, dbt unit tests and semantic
    # models, each keyed by the unique_id of the model they attach
    # to (the same depends_on edges the counts read).
    generic_tests_by_model: dict[str, list[dict[str, Any]]] = {}
    carriage_tests_by_model: dict[str, list[dict[str, Any]]] = {}
    for test_node in nodes.values():
        if not isinstance(test_node, dict):
            continue
        if test_node.get("resource_type") != "test":
            continue
        # Every attached test yields exactly one entry -
        # a verbatim copy of the generic test, a singular copy, or
        # an explicit `uncopied` marker - so the declared
        # interface accounts for the whole count the interface
        # flags report, with no silent counted-but-not-copied
        # remainder.
        entry = _generic_test_entry(test_node)
        for dep in (test_node.get("depends_on") or {}).get("nodes") or []:
            generic_tests_by_model.setdefault(dep, []).append(entry)
        # Carriage keys on the ATTACHED model, not on every
        # dependency: a relationships test also depends on its
        # target model, and the declaration belongs on the model
        # the test is attached to. Manifests carry
        # `attached_node`; fixtures without it fall back to the
        # dependency list.
        carriage_entry = _carriage_test_entry(test_node)
        if carriage_entry is not None:
            attached = test_node.get("attached_node")
            keys = (
                [attached]
                if isinstance(attached, str) and attached
                else (test_node.get("depends_on") or {}).get("nodes") or []
            )
            for key in keys:
                carriage_tests_by_model.setdefault(key, []).append(
                    carriage_entry
                )
    unit_tests_by_model: dict[str, list[dict[str, Any]]] = {}
    for unit_test in (document.get("unit_tests") or {}).values():
        if not isinstance(unit_test, dict):
            continue
        entry = {"name": unit_test.get("name")}
        if unit_test.get("given") is not None:
            entry["given"] = deepcopy(unit_test["given"])
        if unit_test.get("expect") is not None:
            entry["expect"] = deepcopy(unit_test["expect"])
        for dep in (unit_test.get("depends_on") or {}).get("nodes") or []:
            unit_tests_by_model.setdefault(dep, []).append(entry)
    semantic_models_by_model: dict[str, list[dict[str, Any]]] = {}
    for semantic_model in (document.get("semantic_models") or {}).values():
        if not isinstance(semantic_model, dict):
            continue
        entry = {"name": semantic_model.get("name")}
        for key in ("entities", "dimensions", "measures"):
            value = semantic_model.get(key)
            if isinstance(value, list) and value:
                entry[key] = deepcopy(value)
        for dep in (semantic_model.get("depends_on") or {}).get("nodes") or []:
            semantic_models_by_model.setdefault(dep, []).append(entry)

    # Semantic-layer reachability: which semantic
    # models attach to each model by unique_id, which metrics
    # read each semantic model, and which saved queries read
    # each metric (or a semantic model directly) - so the
    # metrics and saved queries reachable from a model can be
    # NAMED in its record and flags, not just counted in its
    # consumer total.
    semantic_model_ids_by_model: dict[str, list[str]] = {}
    for sm_uid, semantic_model in (document.get("semantic_models") or {}).items():
        if not isinstance(semantic_model, dict):
            continue
        for dep in (semantic_model.get("depends_on") or {}).get("nodes") or []:
            semantic_model_ids_by_model.setdefault(dep, []).append(sm_uid)
    metric_uids_by_semantic_model: dict[str, list[str]] = {}
    metric_names: dict[str, str] = {}
    for metric_uid, metric in (document.get("metrics") or {}).items():
        if not isinstance(metric, dict):
            continue
        if isinstance(metric.get("name"), str):
            metric_names[metric_uid] = metric["name"]
        for dep in (metric.get("depends_on") or {}).get("nodes") or []:
            metric_uids_by_semantic_model.setdefault(dep, []).append(metric_uid)
    saved_queries_by_metric: dict[str, list[str]] = {}
    saved_queries_by_semantic_model: dict[str, list[str]] = {}
    for saved_query in (document.get("saved_queries") or {}).values():
        if not isinstance(saved_query, dict):
            continue
        saved_name = saved_query.get("name")
        if not isinstance(saved_name, str):
            continue
        for dep in (saved_query.get("depends_on") or {}).get("nodes") or []:
            if dep.startswith("metric."):
                saved_queries_by_metric.setdefault(dep, []).append(saved_name)
            elif dep.startswith("semantic_model."):
                saved_queries_by_semantic_model.setdefault(dep, []).append(
                    saved_name
                )

    def semantic_reach(unique_id: str) -> dict[str, list[str]] | None:
        sm_uids = semantic_model_ids_by_model.get(unique_id, [])
        if not sm_uids:
            return None
        metric_uids = {
            metric_uid
            for sm_uid in sm_uids
            for metric_uid in metric_uids_by_semantic_model.get(sm_uid, [])
        }
        saved = {
            name
            for metric_uid in metric_uids
            for name in saved_queries_by_metric.get(metric_uid, [])
        } | {
            name
            for sm_uid in sm_uids
            for name in saved_queries_by_semantic_model.get(sm_uid, [])
        }
        return {
            "semantic_models": sorted(
                entry["name"]
                for entry in semantic_models_by_model.get(unique_id, [])
                if isinstance(entry.get("name"), str)
            ),
            "metrics": sorted(
                metric_names[uid] for uid in metric_uids if uid in metric_names
            ),
            "saved_queries": sorted(saved),
        }

    # Drafts already on disk - a re-assessment reads draft
    # state from this directory and the load path, locally, at
    # assessment time. Both halves of each draft are inventoried
    # (the draft records and the instance documents under
    # `instances/`), so the state can distinguish a present
    # draft from a partly deleted one. (The unified draft lives
    # in `local/drafts/`; this inventory used to read the
    # scaffold tier's directory and now reads the one draft
    # directory for every draft kind.)
    drafts_dir = project_dir / "local" / "drafts"
    scaffold_draft_names: set[str] = set()
    scaffold_instance_names: set[str] = set()
    if drafts_dir.is_dir():
        for path in drafts_dir.iterdir():
            if path.is_file() and path.suffix in (".yml", ".yaml"):
                scaffold_draft_names.add(path.stem)
        draft_instances_dir = drafts_dir / "instances"
        if draft_instances_dir.is_dir():
            for path in draft_instances_dir.iterdir():
                if path.is_file() and path.suffix in (".yml", ".yaml"):
                    scaffold_instance_names.add(path.stem)
    scaffold_drafts_written = bool(
        scaffold_draft_names or scaffold_instance_names
    )

    # The compilation provenance is computed once, before
    # the per-model loop, because each fit proposal on a
    # build-produced manifest carries the annotation at the
    # point of use (the report-level declaration alone was
    # bypassed by a skim of the yield line or a JSON pipeline).
    # The same value is declared at report level below.
    compilation_provenance = _manifest_provenance(document)
    # dbt_project.yml's parsed config, read once: the P4
    # materialization-scope facts are computed from it per
    # model. An unreadable/absent tree simply yields no scope
    # facts (the helper returns None per model).
    try:
        project_config = _project_config(project_dir)
    except Exception:
        project_config = {}
    # Pattern facts for readiness validation (P3): names by
    # generator, full definitions for defaults/required, and
    # the taken-name set the synthesiser avoids.
    patterns_by_generator = _project_patterns(project_dir, entries)
    pattern_definitions = _pattern_definitions(project_dir, entries)
    all_pattern_names = {
        pattern_name
        for names in patterns_by_generator.values()
        for pattern_name in names
    }

    models: list[dict[str, Any]] = []
    for unique_id, node in sorted(nodes.items()):
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") != "model":
            continue
        if node.get("package_name") != project_name:
            continue
        name = node.get("name")
        if not isinstance(name, str):
            continue
        deps = (node.get("depends_on") or {}).get("nodes") or []
        source_deps: list[dict[str, str]] = []
        source_columns: list[str] = []
        model_deps: list[str] = []
        seed_deps: list[str] = []
        model_dep_columns: dict[str, list[str]] = {}
        for dep in deps:
            if dep.startswith("seed."):
                dep_node = nodes.get(dep) or {}
                if isinstance(dep_node.get("name"), str):
                    seed_deps.append(dep_node["name"])
            elif dep.startswith("source."):
                source_node = sources.get(dep) or {}
                source_deps.append(
                    {
                        "source_name": source_node.get("source_name", ""),
                        "name": source_node.get("name", ""),
                    }
                )
                source_columns = sorted(
                    (source_node.get("columns") or {}).keys()
                )
            elif dep.startswith("model."):
                dep_node = nodes.get(dep) or {}
                if isinstance(dep_node.get("name"), str):
                    model_deps.append(dep_node["name"])
                    # The input model's documented columns,
                    # for the silver documented-columns caveat.
                    model_dep_columns[dep_node["name"]] = sorted(
                        (dep_node.get("columns") or {}).keys()
                    )
        input_columns = (
            model_dep_columns.get(model_deps[0])
            if len(model_deps) == 1
            else None
        )
        child_ids = children.get(unique_id, [])
        attached_tests = sum(
            1
            for child in child_ids
            if child.startswith("test.") or child.startswith("unit_test.")
        )
        # The consumer count, decomposed by kind: every
        # non-test child lands in exactly one kind, so the kinds
        # sum to the total the flag reports.
        consumers_by_kind: dict[str, int] = {}
        for child in child_ids:
            if child.startswith(("test.", "unit_test.")):
                continue
            kind = _consumer_kind(child)
            consumers_by_kind[kind] = consumers_by_kind.get(kind, 0) + 1
        consumers = sum(consumers_by_kind.values())
        record: dict[str, Any] = {"name": name}
        # Distinguish dbt model version nodes - two records
        # for one name are versions, not two models.
        version = node.get("version")
        if version is not None:
            record["version"] = version
            latest = node.get("latest_version")
            if latest is not None:
                record["latest_version"] = latest
        if isinstance(node.get("original_file_path"), str):
            record["model_path"] = node["original_file_path"]
        fit: dict[str, Any] | None = None
        if name in known:
            info = known[name]
            record["state"] = info["state"]
            record["status"] = (
                "controlled by Loom"
                if info["state"] == "controlled_by_loom"
                else "known to Loom, not controlled"
            )
            record["inventory"] = info
            if info["state"] == "known_not_controlled":
                fit = _fit_proposal(
                    name,
                    node,
                    source_deps,
                    model_deps,
                    source_columns,
                    input_columns=input_columns,
                    macro_index=macro_index,
                    project_name=project_name,
                    seed_deps=seed_deps,
                )
                _attach_carried_generic_tests(
                    fit, carriage_tests_by_model.get(unique_id, [])
                )
                record["fit"] = fit
        else:
            record["state"] = "unknown_to_loom"
            record["status"] = "not in Loom inventory"
            classification, evidence = _classification(
                name, (node.get("config") or {}).get("materialized")
            )
            draft: dict[str, Any] = {
                "classification": classification,
                "evidence": evidence,
            }
            # Harvest the declared facts the manifest
            # carries - the real model description and meta.owner
            # - instead of drafting boilerplate and a TODO the
            # practitioner must retype. Provenance is noted in the
            # draft's evidence; where the project is silent, no key
            # is drafted and the writer falls back as before.
            provenance: list[str] = []
            harvested_description = _manifest_description(node)
            if harvested_description is not None:
                draft["description"] = harvested_description
                provenance.append(
                    "model description copied verbatim from the "
                    "dbt manifest"
                )
            harvested_owner = _manifest_owner(node)
            if harvested_owner is not None:
                draft["owner"] = harvested_owner
                provenance.append(
                    "owner copied from the manifest's meta.owner "
                    ""
                )
            if provenance:
                draft["evidence"] = evidence + "; " + "; ".join(provenance)
            record["draft_registration"] = draft
            fit = _fit_proposal(
                name,
                node,
                source_deps,
                model_deps,
                source_columns,
                input_columns=input_columns,
                macro_index=macro_index,
                project_name=project_name,
                seed_deps=seed_deps,
            )
            _attach_carried_generic_tests(
                fit, carriage_tests_by_model.get(unique_id, [])
            )
            record["fit"] = fit
        # The structured construct inventory (M3): for a
        # no-fit model, every expression item and macro
        # invocation named completely on the record, where the
        # prose blockers only summarise.
        if fit is not None and "no_fit" in fit:
            constructs = _construct_inventory(
                node.get("compiled_code") or node.get("raw_code") or ""
            )
            if constructs:
                record["constructs"] = constructs
        # The incremental loss-warning flag is computed
        # after the fit, because a draft that carries the declared
        # configuration (a bronze/silver incremental block) makes
        # the warning false for that model.
        incremental_drafted = bool(fit) and "incremental" in (
            fit.get("drafted_params") or {}
        )
        flags = _interface_flags(
            node,
            consumers_by_kind,
            attached_tests,
            incremental_drafted=incremental_drafted,
            materialization_scope=_materialization_scope(
                project_config,
                project_name,
                record.get("model_path"),
            ),
        )
        if consumers_by_kind:
            record["consumers_by_kind"] = {
                kind: consumers_by_kind[kind]
                for kind in (
                    *_CONSUMER_KIND_ORDER,
                    *sorted(
                        k
                        for k in consumers_by_kind
                        if k not in _CONSUMER_KIND_ORDER
                    ),
                )
                if kind in consumers_by_kind
            }
        reach = semantic_reach(unique_id)
        if reach is not None:
            record["semantic_layer"] = reach
            if reach["metrics"] or reach["saved_queries"]:
                clauses = []
                if reach["metrics"]:
                    clauses.append(
                        f"{len(reach['metrics'])} metric(s) reachable "
                        f"({', '.join(reach['metrics'])})"
                    )
                if reach["saved_queries"]:
                    clauses.append(
                        f"{len(reach['saved_queries'])} saved quer(ies) "
                        f"reachable ({', '.join(reach['saved_queries'])})"
                    )
                flags.append(
                    "the semantic layer reads this model via semantic "
                    f"model(s) {', '.join(reach['semantic_models'])}: "
                    + "; ".join(clauses)
                )
        if flags:
            record["interface_flags"] = flags
        scaffold = _scaffold_record(
            name=name,
            node=node,
            state=record["state"],
            fit=fit,
            source_deps=source_deps,
            model_deps=model_deps,
            seed_deps=seed_deps,
            model_path=record.get("model_path"),
            generic_tests=generic_tests_by_model.get(unique_id, []),
            carriage_tests=carriage_tests_by_model.get(unique_id, []),
            unit_tests=unit_tests_by_model.get(unique_id, []),
            semantic_models=semantic_models_by_model.get(unique_id, []),
            bundle_on_disk=name in scaffold_draft_names,
            instance_on_disk=name in scaffold_instance_names,
            drafts_written=scaffold_drafts_written,
        )
        if scaffold is not None:
            record["scaffold"] = scaffold
        # The declared interface is standard furniture of every
        # draft (the copy half): a fit proposal used to count
        # the interface in its flags but never copy it, while
        # scaffolds carried the verbatim copy. Compute it once
        # per non-controlled model and carry it on the record,
        # so the unified draft writer attaches the same copy to
        # fit-derived and authoring drafts alike. (The scaffold
        # record keeps its own copy for its existing consumers.)
        if record["state"] != "controlled_by_loom":
            interface = _declared_interface(
                node,
                generic_tests=generic_tests_by_model.get(unique_id, []),
                unit_tests=unit_tests_by_model.get(unique_id, []),
                semantic_models=semantic_models_by_model.get(unique_id, []),
            )
            if interface:
                record["declared_interface"] = interface
        # Readiness: derived here, on every run, from the
        # fresh assessment plus the current content of any
        # unpromoted draft on disk - never stored in the
        # instance, and re-derived rather than remembered, so a
        # hole closed from either side (the draft edited, the
        # project's evidence improved) shows as closed, and a
        # change against the account the on-disk draft record
        # carries is reported as a transition. Controlled
        # models carry no account: they have been promoted out
        # of the drafting pool already.
        if record["state"] != "controlled_by_loom":
            fit_record = record.get("fit") or {}
            scaffold_record = record.get("scaffold") or {}
            if fit_record.get("generator"):
                r_basis: str | None = "fit"
                r_generator = fit_record["generator"]
                r_params = dict(fit_record.get("drafted_params") or {})
                r_blockers: list[str] = []
                r_hole_params: list[str] = []
                r_evidence = list(fit_record.get("evidence") or [])
            elif scaffold_record.get("state") in _SCAFFOLD_CANDIDATE_STATES:
                r_basis = "authoring"
                r_generator = scaffold_record["target_generator"]
                r_params = dict(scaffold_record.get("drafted_params") or {})
                r_blockers = list(fit_record.get("blockers") or [])
                r_hole_params = list(
                    (scaffold_record.get("authoring_hole") or {}).get(
                        "params"
                    )
                    or []
                )
                r_evidence = list(scaffold_record.get("evidence") or [])
            else:
                r_basis = None
                r_generator = None
                r_params = {}
                r_blockers = list(fit_record.get("blockers") or [])
                r_hole_params = []
                r_evidence = []
            params_sources = [r_params]
            pattern_sources: list[Any] = []
            disk_instance = _draft_instance_on_disk(project_dir, name)
            if disk_instance is not None:
                params_sources.append(disk_instance["params"])
                pattern_sources.append(disk_instance["pattern"])
            # P3: validate the effective draft instance - the
            # on-disk instance when one exists (it is what
            # promotion moves), else the freshly drafted params
            # under the pattern this run would reference.
            validation_problems: list[str] = []
            if r_generator is not None and params_sources:
                v_pattern: str | None = None
                if disk_instance is not None:
                    candidate = disk_instance.get("pattern")
                    if isinstance(candidate, str) and candidate != "TODO":
                        v_pattern = candidate
                else:
                    existing = sorted(
                        patterns_by_generator.get(r_generator, set())
                    )
                    if len(existing) == 1:
                        v_pattern = existing[0]
                    elif not existing:
                        v_pattern = _synthesised_pattern_name(
                            r_generator, all_pattern_names
                        )
                if v_pattern is not None:
                    validation_problems = _draft_validation_problems(
                        name=name,
                        generator=r_generator,
                        params=params_sources[-1],
                        pattern_name=v_pattern,
                        definitions=pattern_definitions,
                    )
            account = _readiness_account(
                basis=r_basis,
                generator=r_generator,
                blockers=r_blockers,
                params_sources=params_sources,
                pattern_sources=pattern_sources,
                authoring_hole_params=r_hole_params,
                evidence=r_evidence,
                validation_problems=validation_problems,
            )
            record["readiness"] = account
            previous_record = _draft_record_on_disk(project_dir, name)
            previous = (
                (previous_record or {}).get("readiness")
                if previous_record
                else None
            )
            # M3 (d): a multi-model mart-shaped model with no
            # draft and no scaffold gets the nearest-generator
            # account on its record - record-level naming only,
            # no scaffold instance is drafted for it.
            if (
                account["state"] == "not_draftable"
                and r_basis is None
                and len(model_deps) >= 2
            ):
                _sql = (
                    node.get("compiled_code") or node.get("raw_code") or ""
                )
                _signals_found = _signals(_sql)
                if (
                    "aggregation (GROUP BY)" in _signals_found
                    or "a JOIN" in _signals_found
                ):
                    record["nearest_generator"] = (
                        _nearest_generator_account(
                            sql=_sql,
                            model_deps=model_deps,
                            signals=_signals_found,
                            patterns_by_generator=patterns_by_generator,
                            definitions=pattern_definitions,
                            all_pattern_names=all_pattern_names,
                        )
                    )
            if (
                isinstance(previous, dict)
                and previous.get("state") in _READINESS_STATES
                and (
                    previous["state"] != account["state"]
                    or len(previous.get("holes") or [])
                    != len(account["holes"])
                )
            ):
                record["readiness_transition"] = {
                    "from": {
                        "state": previous["state"],
                        "holes": len(previous.get("holes") or []),
                    },
                    "to": {
                        "state": account["state"],
                        "holes": len(account["holes"]),
                    },
                }
        # Annotate, do not gate - a fit proposed on a
        # build-produced manifest carries its provenance basis on
        # the fit itself, so the JSON record (and the text entry
        # rendered from it) is distinguishable from a
        # parse-produced fit at the point of use. A
        # parse-produced report carries no per-fit annotation.
        if (
            compilation_provenance == "build-produced"
            and fit is not None
            and "generator" in fit
        ):
            fit["manifest_provenance"] = compilation_provenance
        models.append(record)

    if only_models is not None:
        models = [model for model in models if model["name"] in only_models]

    summary = {
        "project_models": len(models),
        "known_to_loom": sum(
            1 for m in models if m["state"] != "unknown_to_loom"
        ),
        "controlled_by_loom": sum(
            1 for m in models if m["state"] == "controlled_by_loom"
        ),
        "known_not_controlled": sum(
            1 for m in models if m["state"] == "known_not_controlled"
        ),
        "unknown_to_loom": sum(
            1 for m in models if m["state"] == "unknown_to_loom"
        ),
        "drafted_registrations": sum(
            1 for m in models if "draft_registration" in m
        ),
        # Project reporting is the readiness distribution,
        # replacing the three yields (`registered` /
        # `scaffolded` / `conversion_drafted`) the summary used
        # to headline: the yields counted operations, and on a
        # real project they said nothing about how much work
        # stood between a draft and promotion - the 2 jaffle
        # "fits" at the certified tree both carried holes the
        # yield counted as done. The distribution counts every
        # non-controlled model by its derived readiness state;
        # controlled models are outside it (already promoted).
        "readiness": {
            state: sum(
                1
                for m in models
                if (m.get("readiness") or {}).get("state") == state
            )
            for state in _READINESS_STATES
        },
        "readiness_transitions": sum(
            1 for m in models if "readiness_transition" in m
        ),
        "scaffolds_promoted": sum(
            1
            for m in models
            if (m.get("scaffold") or {}).get("state") == "promoted"
        ),
        "flagged_models": sum(1 for m in models if m.get("interface_flags")),
        "seen_but_not_assessed": dict(sorted(seen_but_not_assessed.items())),
    }
    return {
        "project": project_name,
        "manifest": str(manifest_file),
        # Compilation provenance is declared on every
        # report, and a build-produced manifest also warns (fit
        # calibration assumes a parse-produced manifest).
        "manifest_provenance": compilation_provenance,
        "warnings": _adapter_warnings(project_dir, document)
        + _provenance_warnings(compilation_provenance),
        "summary": summary,
        "models": models,
    }


def write_draft_registrations(
    project_dir: Path, report: dict[str, Any]
) -> list[str]:
    """Write drafted registrations for unassessed models into the
    practitioner-owned registrations directory. Never overwrites."""
    written: list[str] = []
    target_dir = Path(project_dir) / "local" / "registrations"
    for model in report["models"]:
        draft = model.get("draft_registration")
        if not draft:
            continue
        target = target_dir / f"{model['name']}.yml"
        if target.exists():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        # A harvested description/owner is written verbatim;
        # the boilerplate description and TODO owner remain only
        # where the manifest declares neither.
        document = {
            "name": model["name"],
            "classification": draft["classification"],
            "description": draft.get("description")
            or (
                "Drafted by `loom register`; "
                "review and complete before relying on it. "
                f"Evidence: {draft['evidence']}"
            ),
            "owner": draft.get("owner") or "TODO",
        }
        target.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        written.append(str(target))
    return written


_DRAFT_HEADER = """\
# Drafted by `loom convert` - a review artefact,
# NOT Loom metadata. Loom does not load files from
# local/drafts/.
#
# This record is the review half of a unified draft: the
# final-form instance document for this model sits under
# local/drafts/instances/, and - when the project has no pattern
# for the generator - a synthesised pattern definition in its
# final shape sits under local/drafts/patterns/, written once per
# generator for the whole run. Every section of this draft maps
# 1:1 to its destination file, so once the review todos are
# completed, promotion is a file move plus registration cleanup -
# no content edits. The `promotion` list below spells out exactly
# how. A draft never authorises --force, deleting the hand-written
# model, or skipping the equivalence proof in
# docs/adoption-playbook.md.
"""

_INSTANCE_DRAFT_HEADER = """\
# Drafted by `loom convert` - a loadable pattern
# document in final form, written outside the load path: Loom does
# not load files from local/drafts/. To adopt it, complete every
# TODO in the params (here, in the draft), then move the file into
# metadata/instances/ as-is (or merge its instance into an existing
# instances document for the same pattern); the draft record at
# local/drafts/<model>.yml lists the full promotion steps for this
# model.
"""

_PATTERN_DRAFT_HEADER = """\
# Synthesised by `loom convert` - a pattern definition
# in final form, written once per generator for this assessment
# run because the project has no pattern for the generator. Its
# name is final: the instance documents already reference it, and
# promotion moves this file into metadata/patterns/ unchanged -
# no rename step. (Renaming it later is ordinary pattern
# maintenance, not part of promotion.)
"""


def _synthesised_pattern_name(
    generator: str, taken: set[str]
) -> str:
    """The synthesised pattern's name for a generator - its
    FINAL name.

    Drafts are authored in final form: the pattern is named
    ``<generator>_pattern`` from the start, instances reference
    it by that name immediately, and promotion moves the file
    without renaming anything (the retired `draft_` prefix
    existed to force a rename-and-fix-references step at
    promotion, which the move-only rule eliminates). `taken`
    holds every pattern name the project already defines, for
    any generator: pattern names share one namespace in
    `metadata/patterns/`, so a clash with another generator's
    pattern is resolved with a deterministic numeric suffix -
    still a final name, chosen once, here."""
    candidate = f"{generator}_pattern"
    suffix = 2
    while candidate in taken:
        candidate = f"{generator}_pattern_{suffix}"
        suffix += 1
    return candidate


def _project_patterns(
    project_dir: Path, entries: list[Any]
) -> dict[str, set[str]]:
    """Pattern names by generator that the project already defines.

    Two sources: patterns with at least one instance, seen
    through the inventory entries (the generator lives on the
    expanded entry's data, the name on its pattern identity), and
    pattern definitions with no instances yet, read from
    ``metadata/patterns/`` directly. The caller has already run
    `validate_project`, so definition files are known to parse and
    validate; an unreadable one is skipped rather than fatal here.
    """
    patterns: dict[str, set[str]] = {}
    for entry in entries:
        definition = entry.definition
        generator = entry.data.get("generator")
        if (
            definition
            and isinstance(definition.get("name"), str)
            and isinstance(generator, str)
        ):
            patterns.setdefault(generator, set()).add(definition["name"])
    definitions_dir = Path(project_dir) / "metadata" / "patterns"
    if definitions_dir.is_dir():
        for path in sorted(definitions_dir.iterdir()):
            if path.suffix not in (".yml", ".yaml") or not path.is_file():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            if (
                isinstance(data, dict)
                and isinstance(data.get("name"), str)
                and isinstance(data.get("generator"), str)
            ):
                patterns.setdefault(data["generator"], set()).add(
                    data["name"]
                )
    return patterns


def _draft_promotion_steps(
    model_name: str,
    pattern_step: str,
    *,
    authoring: bool,
    registered: bool,
    practitioner_yaml: bool = False,
    siblings: int = 0,
    pattern_name: str | None = None,
) -> list[str]:
    """What adopting one unified draft consists of.

    Written into the draft record so the practitioner never has to
    guess which parts are loadable, which are review material, or
    which acts are theirs. The shape is the move-only rule made
    concrete: todos are completed in the draft, then promotion is
    a file move plus registration cleanup - no content edits at
    promotion time."""
    if siblings:
        # P10 (ruling D6, wording only): when one run drafts
        # siblings for a pattern, consolidation into a single
        # instance set is the default the step names first -
        # the fragmented one-file-per-model outcome was the
        # parenthesis winning over the main clause.
        instance_step = (
            f"Merge this instance into a single instance set "
            f"for pattern {pattern_name!r} in "
            "metadata/instances/, together with the "
            f"{siblings} other instance(s) this run drafted "
            "for the same pattern - one set per pattern is the "
            "default. Moving "
            f"local/drafts/instances/{model_name}.yml into "
            "metadata/instances/ as its own file is the "
            "exception, for when a separate set is deliberate."
        )
    else:
        instance_step = (
            f"Move local/drafts/instances/{model_name}.yml into "
            "metadata/instances/ unchanged (or merge its single "
            "instance into an existing instances document for the "
            "same pattern)."
        )
    steps = [
        "Review this record's evidence and review_todos, and "
        "complete every todo in the draft instance document at "
        f"local/drafts/instances/{model_name}.yml - edits happen "
        "now, in the draft; an inference is not a verification, "
        "and promotion itself is moves only.",
        pattern_step,
        instance_step,
    ]
    if registered:
        steps.append(
            "Delete the model's registration "
            f"(local/registrations/{model_name}.yml): the promoted "
            "instance replaces it as the model's inventory record, "
            "and while both exist `loom validate` fails with "
            "`duplicate inventory name`."
        )
    if practitioner_yaml:
        steps.append(
            "Practitioner-YAML note: this draft carries declared "
            "tests or semantic models, and the generic tests have "
            "two possible homes - choose exactly one. EITHER the "
            "tests carried in the instance's `generic_tests` "
            "generate with the model in Loom's properties "
            "document (the default: promote as drafted), OR the "
            "tests stay in your own schema YAML: then remove the "
            "carried `generic_tests` from the promoted instance "
            "and set `skip_properties_document: true` in its "
            "params - `loom generate` then leaves the model's "
            "properties document to your YAML instead of emitting "
            "a competing one (dbt allows one schema file per "
            "model), and `loom check` watches that the model "
            "keeps a practitioner description. `loom validate` "
            "refuses `generic_tests` together with "
            "`skip_properties_document`, so the two homes cannot "
            "both be chosen. (Unit tests and semantic models are "
            "not carried; they stay in your YAML either way.)"
        )
    steps.append(
        "Run `loom validate`: the promoted set must validate as "
        "inventory before anything is generated from it."
    )
    if authoring:
        steps.append(
            "Prove the authored model before swapping, per "
            "docs/adoption-playbook.md: build it and check it "
            "against the declared interface and fixtures copied "
            "in this record - the contract's columns, types and "
            "constraints, the unit tests' given/expect fixtures, "
            "and the generic tests - then run the equivalence "
            "comparison against the original over identical source "
            "data. Swap only on a pass: remove the hand-written "
            "model file (your act - a draft never authorises "
            "--force or deleting it), run `loom generate`, "
            "`dbt build`, and `loom coverage`."
        )
    else:
        steps.append(
            "Prove equivalence before swapping, per "
            "docs/adoption-playbook.md: build the original and "
            "the candidate against identical source data and "
            "compare contents over the original model's columns. "
            "A draft never authorises --force, deleting the "
            "hand-written model, or skipping the proof."
        )
    return steps


def _review_todos(model: dict[str, Any], fit: dict[str, Any]) -> list[str]:
    todos = [
        "Review the drafted parameters against the original SQL; the "
        "evidence below records what was inferred, and an inference is "
        "not a verification."
    ]
    params = fit.get("drafted_params") or {}
    if params.get("owner") == "TODO" or str(
        params.get("description", "")
    ).startswith("TODO"):
        todos.append("Replace the TODO description/owner with real values.")
    if params.get("dedup_key") == "TODO":
        todos.append(
            "Determine the dedup key; it could not be derived "
            "mechanically from the SELECT DISTINCT shape."
        )
    if params.get("columns") == []:
        todos.append(
            "Draft the staging column mapping; the manifest documents "
            "no columns for this model."
        )
    placeholder_outputs = [
        str(entry.get("alias"))
        for entry in params.get("columns") or []
        if isinstance(entry, dict) and entry.get("source") == _TODO_SENTINEL
    ]
    if placeholder_outputs:
        todos.append(
            "Draft the column mappings left as unresolved placeholders "
            f"(source: {_TODO_SENTINEL}) for output(s): "
            + ", ".join(placeholder_outputs)
            + " - read each source column from the original SQL; "
            "`loom validate` rejects the sentinel, so no placeholder "
            "may survive promotion."
        )
    unverified_macros = [
        entry
        for entry in fit.get("evidence") or []
        if isinstance(entry, str)
        and entry.startswith("unverified macro '")
    ]
    if unverified_macros:
        todos.append(
            "Resolve the unverified macro call(s) named in the "
            "evidence: the draft carries plain column mappings "
            "for those outputs; restore each macro call in the "
            "draft once it verifies against the manifest, or "
            "leave the plain mapping if the call is being retired."
        )
    if fit.get("generator") == "staging":
        # Staging emits no materialization config at all, so a
        # candidate materialized as anything but a view would
        # silently change behaviour on conversion (the generated
        # model takes the project's default materialization).
        # Name it on the draft record, where the practitioner
        # resolves it (owner ruling, 2026-11-04).
        materialized = (
            (model.get("declared_interface") or {}).get("config") or {}
        ).get("materialized")
        if isinstance(materialized, str) and materialized != "view":
            todos.append(
                f"Resolve the materialization change: the manifest "
                f"declares this model materialized as "
                f"{materialized!r}, but the staging generator emits "
                "no materialization config, so the generated model "
                "would take the project's default materialization "
                "once promoted. Accept the change deliberately, or "
                "plan to set the materialization outside Loom after "
                "promotion."
            )
    if fit.get("source_rationale"):
        todos.append(
            "This draft record's source_rationale field carries all of "
            "the source model's SQL comments verbatim, in source "
            "order, unselected; prune what does not belong "
            "and decide where the rationale belongs (the "
            "description, project docs, or alongside the generated "
            "model) - Loom copies it, it does not place it."
        )
    for flag in model.get("interface_flags") or []:
        todos.append(f"Decide explicitly before converting: {flag}")
    todos.append(
        "Prove equivalence before swapping: build the original and the "
        "candidate against identical source data and compare contents "
        "over the original model's columns."
    )
    return todos


def _path_segments(path: str) -> list[str]:
    """POSIX segments of a path, dropping empty and ``.`` components."""
    return [
        part
        for part in path.replace("\\", "/").split("/")
        if part not in ("", ".")
    ]


def _draft_output_subpath(
    config: dict[str, Any], model: dict[str, Any], generator: str
) -> tuple[str | None, list[str]]:
    """Draft an ``output_subpath`` for a conversion candidate.

    The model's manifest location is interpreted against the model
    roots configured in ``dbt_project.yml`` and the fit generator's
    primary output root (``loom.output_layout``), so a proposal is
    relative to where generation will actually write - never by
    stripping a presumed first segment. One segment is corrected,
    not carried: when the drafted relative path leads with the
    primary root's own last segment (a staging model living in
    ``models/staging/``), that segment is the generator's layer
    directory, which generation adds itself, so it is dropped from
    the proposal rather than doubled in the generated path.
    Returns the proposal (or
    None when there is nothing to propose) and the review TODOs the
    location warrants: a carried-over location to confirm, a move
    declared with its expected generated path, or an explanation
    when the current location cannot be carried over.
    """
    raw = model.get("model_path")
    if not isinstance(raw, str):
        return None, []
    name = model["name"]
    parts = _path_segments(raw)
    if len(parts) < 2:
        return None, []
    directory = parts[:-1]
    roots = _configured_paths(config, "model")

    def validate(candidate: str) -> tuple[str | None, str | None]:
        try:
            return (
                resolve_output_subpath(
                    {"name": name, "output_subpath": candidate}
                ),
                None,
            )
        except LoomError as error:
            return None, str(error)

    def unsafe_todo(candidate: str, reason: str) -> str:
        return (
            f"The model's current directory ({'/'.join(directory)}) "
            f"could not be carried over as an output_subpath: {reason} "
            "Choose a safe output_subpath by hand if the converted "
            "model should keep a nested location."
        )

    # Work relative to the most specific configured model root the
    # model actually sits under.
    matched: list[str] | None = None
    for root in roots:
        root_parts = _path_segments(root)
        if directory[: len(root_parts)] == root_parts and (
            matched is None or len(root_parts) > len(matched)
        ):
            matched = root_parts
    if matched is None:
        expected = expected_model_path(
            roots[0], {"name": name, "generator": generator}
        )
        return None, [
            "The model currently lives outside the configured "
            f"model-paths ({', '.join(roots)}), so no output_subpath "
            "was drafted; generation will place the converted model "
            f"at {expected}. Set output_subpath explicitly if it "
            "should land in a specific folder."
        ]
    matched_root = "/".join(matched)
    primary = _path_segments(primary_model_root(matched_root, generator))
    if directory == primary:
        # Already flat exactly where generation writes this
        # generator's models: nothing to propose.
        return None, []
    if directory[: len(primary)] == primary:
        candidate = "/".join(directory[len(primary):])
        proposal, reason = validate(candidate)
        if proposal is None:
            return None, [unsafe_todo(candidate, reason or "")]
        return proposal, [
            f"Confirm the proposed output_subpath ({proposal!r}), "
            "drafted from the model's current location under its "
            "generator's output root so the conversion does not move "
            "the file."
        ]
    # Under a configured model root, but not under the generator's
    # primary root: generation writes this generator elsewhere, so
    # even a carried-over subpath still moves the file - declare the
    # move and name the expected generated location.
    relative = "/".join(directory[len(matched):])
    if not relative:
        expected = expected_model_path(
            matched_root, {"name": name, "generator": generator}
        )
        return None, [
            "The model sits flat under the model root, but "
            f"generation writes {generator} models under "
            f"{'/'.join(primary)}; the converted model will be "
            f"generated at {expected}."
        ]
    relative_parts = relative.split("/")
    # A leading segment that repeats the primary root's last
    # segment is the generator's own layer directory (a staging
    # model in models/staging/ repeats the `staging` of
    # models/_generated/staging), not practitioner nesting:
    # carrying it over would double it in the generated path.
    # Strip it only where the primary root sits deeper than the
    # matched model root, i.e. the generator actually adds a layer
    # segment of its own.
    stripped = (
        len(primary) > len(matched) and relative_parts[0] == primary[-1]
    )
    if stripped:
        relative_parts = relative_parts[1:]
    if stripped and not relative_parts:
        expected = expected_model_path(
            matched_root, {"name": name, "generator": generator}
        )
        return None, [
            "The model's current directory under the model root "
            f"repeats the {generator} generator's own layer segment "
            f"({primary[-1]!r}), which generation adds itself, so "
            "no output_subpath was drafted: the converted model "
            f"will be generated at {expected}, not its current "
            "location."
        ]
    candidate = "/".join(relative_parts)
    proposal, reason = validate(candidate)
    if proposal is None:
        return None, [unsafe_todo(candidate, reason or "")]
    expected = expected_model_path(
        matched_root,
        {"name": name, "generator": generator, "output_subpath": proposal},
    )
    todo = (
        f"Confirm the proposed output_subpath ({proposal!r}), "
        "drafted from the model's current directory under the model "
        f"root. Note that generation writes {generator} models under "
        f"{'/'.join(primary)}, so the converted model will be "
        f"generated at {expected}, not its current location."
    )
    if stripped:
        todo += (
            f" The leading {primary[-1]!r} segment of the current "
            f"directory was recognised as the {generator} "
            "generator's own layer segment and is not carried into "
            "the proposal, because generation adds it itself."
        )
    return proposal, [todo]


def _scaffold_review_todos(
    model: dict[str, Any], scaffold: dict[str, Any]
) -> list[str]:
    todos: list[str] = []
    params = scaffold.get("drafted_params") or {}
    # The pre-wired-inputs check is only meaningful when the
    # wiring actually wired something (P1): a seed-fed staging
    # scaffold wires nothing, and telling its author to check
    # inputs that do not exist was pure boilerplate. Where the
    # staging source is unwired, the binding decision is the
    # todo instead.
    wired = bool(params.get("source_ref") or params.get("input_models"))
    if wired:
        todos.append(
            "Check the pre-wired inputs against the model's SQL; "
            "they were copied from the manifest dependency graph - "
            "what dbt resolved - and the authoring that follows "
            "builds on them."
        )
    elif scaffold.get("target_generator") == "staging":
        seeds = (scaffold.get("inputs") or {}).get("seeds") or []
        if seeds:
            todos.append(
                f"Decide the source binding for the seed input(s) "
                f"({', '.join(seeds)}): declare the seed(s) as dbt "
                "source(s) and bind with source_ref, or bind the "
                "seed's relation with inline_source - the authoring "
                "hole's note states both forms."
            )
        else:
            todos.append(
                "Decide the source binding: staging binds one dbt "
                "source (source_ref against a declared source, or "
                "inline_source) and none is wired in this draft."
            )
    if params.get("owner") == "TODO" or str(
        params.get("description", "")
    ).startswith("TODO"):
        todos.append(
            "Replace the TODO description/owner in the instance "
            "document with real values."
        )
    placeholder_outputs = [
        str(entry.get("alias"))
        for entry in params.get("columns") or []
        if isinstance(entry, dict) and entry.get("source") == _TODO_SENTINEL
    ]
    if placeholder_outputs:
        todos.append(
            "Draft the column mappings left as unresolved placeholders "
            f"(source: {_TODO_SENTINEL}) for output(s): "
            + ", ".join(placeholder_outputs)
            + " - read each source column from the original SQL; "
            "`loom validate` rejects the sentinel, so no placeholder "
            "may survive promotion."
        )
    interface = scaffold.get("declared_interface") or {}
    if interface.get("contract"):
        todos.append(
            "The declared contract copied in this draft record is the "
            "column spec the promotion proof checks against; if "
            "the replacement should keep enforcing it, declare "
            "`contract_enforced` in the promoted metadata (with "
            "column_types completing its types)."
        )
    if interface.get("unit_tests"):
        todos.append(
            "The dbt unit tests copied in this draft record are the "
            "proof oracle for the authored transform: their "
            "given fixtures are the input data and their expect "
            "fixtures the required output."
        )
    if not interface:
        todos.append(
            "The manifest declares no interface for this model "
            "- no enforced contract, unit test, generic test, "
            "semantic model or physical config was there to "
            "copy - so the promotion proof rests on the "
            "playbook's equivalence comparison alone."
        )
    if scaffold.get("source_rationale"):
        todos.append(
            "This draft record's source_rationale field carries the "
            "source model's leading SQL comments verbatim "
            "; decide where that rationale belongs (the "
            "description, project docs, or alongside the "
            "generated model) - Loom copies it, it does not "
            "place it."
        )
    for flag in model.get("interface_flags") or []:
        todos.append(f"Decide explicitly before promoting: {flag}")
    return todos


def _draft_sources(
    model: dict[str, Any],
) -> dict[str, Any] | None:
    """The draft inputs for one assessed model, or None when the
    model gets no draft.

    One draft class: a model whose output shape matched a
    generator's fit rule drafts from the fit (`basis: "fit"` -
    drafted parameters, recorded evidence); a layered model no
    rule matched drafts from its scaffold record
    (`basis: "authoring"` - copied facts, pre-wired inputs, the
    transform as the named authoring hole, and the no-fit reason
    carried as the draft's blocking account). Controlled models
    and models in no layer with no fit get no draft."""
    fit = model.get("fit") or {}
    if fit.get("generator"):
        return {
            "basis": "fit",
            "generator": fit["generator"],
            "params": dict(fit.get("drafted_params", {})),
            "evidence": list(fit.get("evidence", [])),
            "blockers": [],
            "todos": _review_todos(model, fit),
            "source_rationale": fit.get("source_rationale"),
            "constructs": list(model.get("constructs") or []),
        }
    scaffold = model.get("scaffold") or {}
    if scaffold.get("state") in _SCAFFOLD_CANDIDATE_STATES:
        return {
            "basis": "authoring",
            "generator": scaffold["target_generator"],
            "generator_note": (
                f"the authoring destination for the "
                f"{scaffold['layer']} layer - named by layer, not "
                "by shape: no generator shape was matched for "
                "this model, and nothing in this draft claims "
                "one was"
            ),
            "params": dict(scaffold.get("drafted_params", {})),
            "evidence": list(scaffold.get("evidence") or []),
            "blockers": list(fit.get("blockers") or []),
            "no_fit_reason": fit.get("no_fit"),
            "layer": scaffold["layer"],
            "classification": scaffold["classification"],
            "inputs": scaffold["inputs"],
            "authoring_hole": scaffold["authoring_hole"],
            "todos": _scaffold_review_todos(model, scaffold),
            "source_rationale": scaffold.get("source_rationale"),
            "constructs": list(model.get("constructs") or []),
        }
    return None


_READINESS_STATES = (
    "ready_to_promote",
    "needs_authoring",
    "not_draftable",
)


def _nearest_generator_account(
    *,
    sql: str,
    model_deps: list[str],
    signals: list[str],
    patterns_by_generator: dict[str, set[str]],
    definitions: dict[str, dict[str, Any]],
    all_pattern_names: set[str],
) -> dict[str, Any]:
    """The nearest-generator account for a multi-model
    (mart-shaped) model with no draft (M3): which generator a
    practitioner would author against by hand, the pattern
    that would carry it with its required params, and the
    constructs - where any - that make the model inexpressible
    even by hand-authoring, so 'author it by hand if you want
    a Loom end state' is distinguishable from 'no Loom end
    state; registration is the end state'.

    The gold/mart discriminator is the joins parameter: the
    mart generator has none, gold does - so a joining model
    names gold, an aggregating non-joining model names mart."""
    from loom.contracts import GENERATOR_CONTRACTS

    has_joins = "a JOIN" in signals
    generator = "gold" if has_joins else "mart"
    if has_joins:
        reasoning = (
            f"the model reads {len(model_deps)} models with "
            "joins and aggregation; the nearest generator is "
            "gold - the mart generator has no joins parameter, "
            "gold does (joins + aggregation over input_models)"
        )
    else:
        reasoning = (
            f"the model reads {len(model_deps)} models with "
            "aggregation and no joins; the nearest generator "
            "is mart (input_models + aggregation)"
        )

    def required_for(pattern_name: str) -> list[str]:
        definition = definitions.get(pattern_name)
        if definition is not None:
            return [
                field
                for field in definition.get("required_params") or []
                if isinstance(field, str)
            ]
        return [
            param.name
            for param in GENERATOR_CONTRACTS.get(generator, ())
            if param.required
        ]

    existing = sorted(patterns_by_generator.get(generator, set()))
    if len(existing) == 1:
        pattern: dict[str, Any] = {
            "name": existing[0],
            "required_params": required_for(existing[0]),
        }
    elif len(existing) > 1:
        pattern = {
            "name": "TODO",
            "required_params": {
                candidate: required_for(candidate)
                for candidate in existing
            },
        }
    else:
        synthesised = _synthesised_pattern_name(
            generator, all_pattern_names
        )
        pattern = {
            "name": synthesised,
            "required_params": required_for(synthesised),
        }
    inexpressible: list[str] = []
    if re.search(r"\{%-?\s*for\b", sql):
        inexpressible.append(
            "dynamic Jinja: a {% for %} loop generates columns "
            "at compile time (a pivot over values in the data); "
            "no generator expresses compile-time-generated "
            "columns, so this model has no Loom end state - "
            "registration is the end state"
        )
    elif ("{{" in sql or "{%" in sql) and _unaccounted_jinja(
        sql, _macro_select_items(sql)
    ):
        inexpressible.append(
            "statement-level Jinja beyond select-item macro "
            "calls and ref/source/config, which no generator's "
            "parameters express; this model has no Loom end "
            "state - registration is the end state"
        )
    return {
        "generator": generator,
        "suggested_pattern": pattern,
        "reasoning": reasoning,
        "inexpressible_constructs": inexpressible,
        "authorable_by_hand": not inexpressible,
    }


def _pattern_definitions(
    project_dir: Path, entries: list[Any]
) -> dict[str, dict[str, Any]]:
    """Pattern definitions by name - generator, defaults and
    required params - from the same two sources
    `_project_patterns` reads names from: entry definitions
    (patterns with instances) and `metadata/patterns/`
    documents (patterns without). Unreadable documents are
    skipped, mirroring the name pass."""
    definitions: dict[str, dict[str, Any]] = {}
    for entry in entries:
        definition = entry.definition
        if isinstance(definition, dict) and isinstance(
            definition.get("name"), str
        ):
            definitions[definition["name"]] = definition
    definitions_dir = Path(project_dir) / "metadata" / "patterns"
    if definitions_dir.is_dir():
        for path in sorted(definitions_dir.iterdir()):
            if path.suffix not in (".yml", ".yaml") or not path.is_file():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                definitions.setdefault(data["name"], data)
    return definitions


def _draft_validation_problems(
    *,
    name: str,
    generator: str,
    params: dict[str, Any],
    pattern_name: str,
    definitions: dict[str, dict[str, Any]],
) -> list[str]:
    """Validate one draft instance as the inventory it would
    become at promotion (P3, ruling D3).

    The draft instance is a final-form loadable document, so
    the would-be record is built exactly as the loader builds
    it - pattern defaults under the instance params, identity
    from the draft - and run through the same per-record
    validation `loom validate` applies (`_validate_record`),
    plus the loader's own two instance-level checks (params
    must not define identity fields; the pattern's required
    params must be present in the instance). The problems are
    the validator's own strings, addressed to the draft file
    the practitioner will edit."""
    path = f"local/drafts/instances/{name}.yml"
    problems: list[str] = []
    for field in ("generator", "name"):
        if field in params:
            problems.append(f"{path}: params must not define {field!r}")
    definition = definitions.get(pattern_name)
    defaults: dict[str, Any] = {}
    if definition is not None:
        raw_defaults = definition.get("defaults")
        if isinstance(raw_defaults, dict):
            defaults = raw_defaults
        required = [
            field
            for field in definition.get("required_params") or []
            if isinstance(field, str)
        ]
    else:
        # A synthesised pattern: its required params are the
        # generator contract's required params, exactly as
        # write_drafts synthesises the definition.
        from loom.contracts import GENERATOR_CONTRACTS

        required = [
            param.name
            for param in GENERATOR_CONTRACTS.get(generator, ())
            if param.required
        ]
    missing = [field for field in required if field not in params]
    if missing:
        problems.append(
            f"{path}: missing required param(s) for pattern "
            f"{pattern_name!r}: {', '.join(missing)}"
        )
    record = {**defaults, **params, "name": name, "generator": generator}
    problems.extend(_validate_record(path, record, False))
    return problems


def _readiness_account(
    *,
    basis: str | None,
    generator: str | None,
    blockers: list[str],
    params_sources: list[dict[str, Any]],
    pattern_sources: list[Any],
    authoring_hole_params: list[str],
    evidence: list[str],
    validation_problems: list[str] | None = None,
) -> dict[str, Any]:
    """One model's readiness account, derived - never stored
    in the instance.

    Three states, strict: ``ready_to_promote`` (a draft exists
    with zero holes and zero blockers), ``needs_authoring``
    (a draft exists; N holes are named), ``not_draftable``
    (no draft exists for the model, or a named blocker stands -
    a construct no generator can express even once the
    transform is authored, so completing the draft's holes
    could never make it promotable).

    A hole is one thing standing between the draft and
    promotion, of two kinds: ``authoring`` (only the
    practitioner can supply it - a TODO description or owner, a
    dedup key no rule could derive, a pattern choice, an
    unwired transform parameter) and ``evidence`` (Loom could
    not verify it from the project's declared evidence - an
    unresolved ``__TODO__`` column placeholder, a column set
    drafted under a documented-columns caveat).

    Validation (P3, ruling D3): ``validation_problems`` carries
    the problems the EFFECTIVE draft instance - the on-disk
    instance when one exists, else the freshly drafted params,
    since the on-disk file is what promotion moves - fails
    record validation with, computed by the caller via
    `_draft_validation_problems`. Each problem is an evidence
    hole in the account, so `ready_to_promote` means the
    instance passes the validation the promoted set must pass,
    not merely that its holes are filled.

    ``params_sources`` carries the parameter sets the account is
    derived from, freshest first: the current assessment's
    drafted parameters, plus the on-disk draft instance's
    parameters when a draft already exists. A content hole
    stands only while it stands in EVERY source - an evidence
    hole closes from either side: edit the
    draft (the on-disk parameters lose the hole) or improve the
    project's evidence (a re-assessment's fresh parameters lose
    it). An authoring-hole parameter is supplied once it appears
    in ANY source. ``pattern_sources`` applies the same
    every-source rule to the instance's ``pattern:`` value.
    """
    holes: list[dict[str, str]] = []
    if params_sources:
        sentinel_outputs: set[str] | None = None
        for params in params_sources:
            outputs = {
                str(entry.get("alias"))
                for entry in params.get("columns") or []
                if isinstance(entry, dict)
                and (
                    entry.get("source") == _TODO_SENTINEL
                    or entry.get("alias") == _TODO_SENTINEL
                )
            }
            sentinel_outputs = (
                outputs
                if sentinel_outputs is None
                else sentinel_outputs & outputs
            )
        for output in sorted(sentinel_outputs or set()):
            if basis == "authoring":
                cause = (
                    "the draft's columns account for every "
                    "output the model's projection names, and "
                    "this one is not mapped yet - draft its "
                    "mapping from the original SQL"
                )
            else:
                cause = (
                    "the manifest's documented columns could "
                    "not pair it - draft it from the original "
                    "SQL, or document the source columns so a "
                    "re-assessment can pair it"
                )
            holes.append(
                {
                    "kind": "evidence",
                    "detail": (
                        f"the column mapping for output '{output}' "
                        f"is an unresolved placeholder "
                        f"(source: {_TODO_SENTINEL}): {cause}"
                    ),
                }
            )
        if (
            basis == "fit"
            and generator == "staging"
            and all(
                params.get("columns") == [] for params in params_sources
            )
        ):
            holes.append(
                {
                    "kind": "evidence",
                    "detail": (
                        "no column mapping was drafted: the "
                        "manifest documents no columns for this "
                        "model - draft the mapping from the "
                        "original SQL, or document the model's "
                        "columns so a re-assessment can draft it"
                    ),
                }
            )
        if all(
            params.get("dedup_key") == "TODO" for params in params_sources
        ) and any("dedup_key" in params for params in params_sources):
            holes.append(
                {
                    "kind": "authoring",
                    "detail": (
                        "the dedup key is a TODO: it could not be "
                        "derived mechanically from the SELECT "
                        "DISTINCT shape - the practitioner "
                        "determines it"
                    ),
                }
            )
        if all(
            isinstance(params.get("description"), str)
            and params["description"].startswith("TODO")
            for params in params_sources
        ):
            holes.append(
                {
                    "kind": "authoring",
                    "detail": (
                        "the description is a TODO placeholder - "
                        "the practitioner writes the model's "
                        "description"
                    ),
                }
            )
        if all(
            params.get("owner") == "TODO" for params in params_sources
        ) and any("owner" in params for params in params_sources):
            holes.append(
                {
                    "kind": "authoring",
                    "detail": (
                        "the owner is a TODO placeholder - the "
                        "practitioner names the model's owner"
                    ),
                }
            )
    if pattern_sources and all(
        pattern == "TODO" for pattern in pattern_sources
    ):
        holes.append(
            {
                "kind": "authoring",
                "detail": (
                    "the instance's pattern is a TODO: several "
                    f"pattern definitions exist for the {generator} "
                    "generator - the practitioner chooses one in "
                    "the draft, before promotion"
                ),
            }
        )
    for param in authoring_hole_params:
        if all(param not in params for params in params_sources):
            holes.append(
                {
                    "kind": "authoring",
                    "detail": (
                        f"the {param} parameter of the {generator} "
                        "generator is unwired - the model's "
                        "transform, authored by the practitioner"
                    ),
                }
            )
    for entry in evidence:
        if entry.startswith("caveat:"):
            holes.append({"kind": "evidence", "detail": entry})
        elif entry.startswith("unverified macro '"):
            # An unverifiable macro call the draft could not
            # carry (Tier 1): an evidence hole, closable from
            # either side - the macro becomes verifiable (the
            # evidence line disappears on re-assessment) or the
            # practitioner restores the call in the draft (a
            # params source then carries the macro).
            reference = (
                entry.split("'", 2)[1] if entry.count("'") >= 2 else ""
            )
            restored = False
            for params in params_sources:
                drafted_columns = params.get("columns")
                if not isinstance(drafted_columns, list):
                    continue
                if any(
                    isinstance(column_entry, dict)
                    and column_entry.get("macro") == reference
                    for column_entry in drafted_columns
                ):
                    restored = True
                    break
            if not restored:
                holes.append({"kind": "evidence", "detail": entry})
    for problem in validation_problems or []:
        # Two validation classes restate holes the account
        # already derives in its own words; counting both
        # would double-state one defect. The sentinel
        # placeholder (the account's hole names the output and
        # the remedy) and a pattern-required param the
        # authoring holes already name as unwired are skipped
        # here - every other validation problem is new
        # information and lands.
        if "unresolved placeholder" in problem and any(
            "unresolved placeholder" in hole["detail"] for hole in holes
        ):
            continue
        missing_match = re.search(
            r"missing required param\(s\) for pattern '[^']+': (.+)$",
            problem,
        )
        if missing_match:
            named = [
                part.strip()
                for part in missing_match.group(1).split(",")
            ]
            if named and all(
                any(
                    hole["detail"].startswith(
                        f"the {param} parameter of the"
                    )
                    for hole in holes
                )
                for param in named
            ):
                continue
        holes.append({"kind": "evidence", "detail": problem})
    if basis is None or blockers:
        state = "not_draftable"
    elif holes:
        state = "needs_authoring"
    else:
        state = "ready_to_promote"
    return {
        "state": state,
        "basis": basis,
        "holes": holes,
        "blockers": list(blockers),
    }


def _draft_instance_on_disk(
    project_dir: Path, name: str
) -> dict[str, Any] | None:
    """The parameters and pattern of a model's draft instance
    already on disk, or None.

    Readiness re-derivation reads the draft's current content:
    a hole the practitioner has completed in the instance no
    longer stands, whatever the current assessment would draft
    afresh. An unreadable or malformed draft file is treated as
    absent - the assessment reports draft state separately, and
    a broken review-area file must not break the report."""
    path = (
        Path(project_dir) / "local" / "drafts" / "instances" / f"{name}.yml"
    )
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    for entry in document.get("instances") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            params = entry.get("params")
            return {
                "pattern": document.get("pattern"),
                "params": params if isinstance(params, dict) else {},
            }
    return None


def _draft_record_on_disk(
    project_dir: Path, name: str
) -> dict[str, Any] | None:
    """A model's draft record already on disk, or None - the
    previous readiness account a re-assessment reports
    transitions against. Same unreadable-means-absent rule as
    the instance read."""
    path = Path(project_dir) / "local" / "drafts" / f"{name}.yml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return document if isinstance(document, dict) else None


def write_drafts(
    project_dir: Path,
    report: dict[str, Any],
    *,
    include_registered: bool = False,
) -> list[str]:
    """Write the unified drafts for assessed models.

    Each assessed model that earns a draft gets one artefact set
    under ``local/drafts/`` - a directory Loom never loads as
    inventory, so an incomplete draft cannot break validation or
    clash with the model's registration:

    - a draft record ``<model>.yml`` (the review half: the
      per-parameter evidence basis or the authoring account, the
      declared interface copied verbatim, the readiness account,
      the review TODOs, and the exact promotion steps);
    - a final-form instance document ``instances/<model>.yml``
      in the pattern/instance shape `metadata/instances/`
      consumes;
    - when the project has no pattern for the draft's generator,
      one synthesised pattern definition ``patterns/<name>.yml``
      per generator for the whole run, in its final shape and
      under its final name, in the shape
      `metadata/patterns/` consumes.

    Drafts are authored in final form: sections map 1:1 to
    their destination files, the instance references the
    pattern's final name from the start, and promotion is a
    file move plus registration cleanup - no content edits at
    promotion time.

    The drafting pool is gated by registration: by default
    only models unknown to Loom are drafted. A registered
    (known-not-controlled) model is counted and reported like
    any other - its fit proposal and readiness account are
    computed - but no draft is written for it: registering a
    model is how a practitioner withdraws it from conversion,
    and deleting the registration returns it to the pool.
    ``include_registered=True`` (the CLI's
    ``--include-registered``) restores the pre-gate reach
    exactly, drafting registered models alongside unknown ones.

    Never overwrites an existing file - the write is per file,
    so a draft set partly deleted by hand is completed by
    writing only the missing half, and what the practitioner
    kept is never touched. Returns the paths written, one per
    drafted model (the record's path, or the instance's when
    only the instance needed writing).

    Divergence is reported, never repaired (owner ruling,
    2026-11-04): when an existing draft file's content differs
    from the draft this assessment would write today - the
    practitioner edited it, or the project's evidence moved
    under it - the file is left exactly as it stands and the
    divergence is recorded on the report as
    ``draft_divergences`` (one entry per file: model, file,
    kind), for the convert report to surface.
    """
    from loom.contracts import GENERATOR_CONTRACTS

    entries = validate_project(project_dir)
    config = _project_config(Path(project_dir))
    patterns_by_generator = _project_patterns(project_dir, entries)
    pattern_definitions = _pattern_definitions(project_dir, entries)

    def _required_params_for(pattern_name: str, gen: str) -> list[str]:
        """A pattern's required params for the draft record
        (M3): from the pattern's definition where one is
        readable, else the generator contract's required
        params - the same fallback the synthesised branch
        has always used."""
        definition = pattern_definitions.get(pattern_name)
        if definition is not None:
            return [
                field
                for field in definition.get("required_params") or []
                if isinstance(field, str)
            ]
        return [
            param.name
            for param in GENERATOR_CONTRACTS[gen]
            if param.required
        ]

    all_pattern_names = {
        name
        for names in patterns_by_generator.values()
        for name in names
    }

    written: list[str] = []
    divergences: list[dict[str, str]] = []
    diverged_files: set[str] = set()

    # P10: how many instances this run drafts per resolved
    # pattern name, computed in a pre-pass that mirrors the
    # main loop's gating and pattern resolution exactly (same
    # order, same synthesis), so each record's promotion steps
    # can name consolidation when siblings exist.
    pattern_draft_counts: dict[str, int] = {}
    _synth_names: dict[str, str] = {}
    _name_pool = set(all_pattern_names)
    for _model in report["models"]:
        _source = _draft_sources(_model)
        if _source is None:
            continue
        if _model.get("state") not in (
            "unknown_to_loom",
            "known_not_controlled",
        ):
            continue
        if (
            _model["state"] == "known_not_controlled"
            and not include_registered
        ):
            continue
        _generator = _source["generator"]
        _existing = sorted(patterns_by_generator.get(_generator, set()))
        if len(_existing) == 1:
            _resolved = _existing[0]
        elif len(_existing) > 1:
            continue  # pattern unresolved (TODO): no siblings claim
        else:
            if _generator not in _synth_names:
                _synth_names[_generator] = _synthesised_pattern_name(
                    _generator, _name_pool
                )
                _name_pool.add(_synth_names[_generator])
            _resolved = _synth_names[_generator]
        pattern_draft_counts[_resolved] = (
            pattern_draft_counts.get(_resolved, 0) + 1
        )

    def note_divergence(model_name: str, relative: str, kind: str) -> None:
        if relative in diverged_files:
            return
        diverged_files.add(relative)
        divergences.append(
            {"model": model_name, "file": relative, "kind": kind}
        )

    target_dir = Path(project_dir) / "local" / "drafts"
    patterns_dir = target_dir / "patterns"
    instances_dir = target_dir / "instances"
    synthesised: dict[str, str] = {}
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
            # The registration gate: the model is counted
            # and reported, but registration has withdrawn it
            # from the drafting pool.
            continue
        generator = source["generator"]
        target = target_dir / f"{model['name']}.yml"
        instance_target = instances_dir / f"{model['name']}.yml"
        existing = sorted(patterns_by_generator.get(generator, set()))
        if len(existing) == 1:
            suggested_pattern: dict[str, Any] = {
                "name": existing[0],
                "required_params": _required_params_for(
                    existing[0], generator
                ),
                "status": "existing pattern definition for this generator",
            }
            pattern_name: Any = existing[0]
            pattern_step = (
                "The instance document already references your "
                f"existing pattern {existing[0]!r}; no pattern "
                "promotion is needed."
            )
        elif len(existing) > 1:
            suggested_pattern = {
                "name": "TODO",
                "required_params": {
                    candidate: _required_params_for(candidate, generator)
                    for candidate in existing
                },
                "status": (
                    "several pattern definitions exist for this "
                    f"generator ({', '.join(existing)}); choose one; "
                    "required_params states each candidate's "
                    "required params until the choice is made"
                ),
            }
            pattern_name = "TODO"
            pattern_step = (
                "Choose one of the existing pattern definitions for "
                f"this generator ({', '.join(existing)}) and set "
                "`pattern:` in the instance document to its name "
                "(a choice made in the draft, before promotion); "
                "the instance document is not loadable until you do."
            )
        else:
            if generator in synthesised:
                pattern_name = synthesised[generator]
            else:
                pattern_name = _synthesised_pattern_name(
                    generator, all_pattern_names
                )
                synthesised[generator] = pattern_name
                all_pattern_names.add(pattern_name)
            required_params = [
                p.name for p in GENERATOR_CONTRACTS[generator] if p.required
            ]
            pattern_relative = f"local/drafts/patterns/{pattern_name}.yml"
            suggested_pattern = {
                "name": pattern_name,
                "generator": generator,
                "required_params": required_params,
                "status": (
                    "no pattern definition exists for this generator "
                    "yet; a pattern definition is synthesised once "
                    "per generator for this assessment run at "
                    f"{pattern_relative}, under its final name - "
                    "promotion moves it unchanged"
                ),
            }
            pattern_step = (
                f"Move {pattern_relative} into metadata/patterns/ "
                "unchanged - its name is final and the instance "
                "documents already reference it. (The pattern file "
                "is shared by every draft for this generator - "
                "promote it once.)"
            )
            pattern_target = patterns_dir / f"{pattern_name}.yml"
            pattern_document = {
                "name": pattern_name,
                "generator": generator,
                "required_params": required_params,
            }
            pattern_text = _PATTERN_DRAFT_HEADER + yaml.safe_dump(
                pattern_document, sort_keys=False
            )
            if pattern_target.exists():
                if (
                    pattern_target.read_text(encoding="utf-8")
                    != pattern_text
                ):
                    note_divergence(
                        pattern_name, pattern_relative, "pattern"
                    )
            else:
                patterns_dir.mkdir(parents=True, exist_ok=True)
                pattern_target.write_text(pattern_text, encoding="utf-8")
        params = dict(source["params"])
        todos = list(source["todos"])
        # Propose an output_subpath interpreted from the
        # model's current manifest location, the model roots in
        # dbt_project.yml and the generator's own output root, so a
        # conversion need not move the file - and declares the move,
        # with its expected generated location, when it still would.
        proposal, location_todos = _draft_output_subpath(
            config, model, generator
        )
        if proposal:
            params["output_subpath"] = proposal
        for offset, todo in enumerate(location_todos):
            todos.insert(1 + offset, todo)
        instance_relative = f"local/drafts/instances/{model['name']}.yml"
        instance_document = {
            "pattern": pattern_name,
            "instances": [{"name": model["name"], "params": params}],
        }
        instance_text = _INSTANCE_DRAFT_HEADER + yaml.safe_dump(
            instance_document, sort_keys=False
        )
        instance_written = False
        if instance_target.exists():
            if instance_target.read_text(encoding="utf-8") != instance_text:
                note_divergence(
                    model["name"], instance_relative, "instance"
                )
        else:
            instances_dir.mkdir(parents=True, exist_ok=True)
            instance_target.write_text(instance_text, encoding="utf-8")
            instance_written = True
        # The readiness account the record carries:
        # derived from the draft as it stands - this run's
        # drafted parameters and pattern, plus the on-disk
        # instance's content when the instance already existed
        # (the practitioner may have completed holes in it).
        # The account lives in the record and in convert's
        # JSON, never inside the instance itself.
        account_sources = [params]
        account_patterns: list[Any] = [pattern_name]
        if not instance_written:
            disk_instance = _draft_instance_on_disk(
                project_dir, model["name"]
            )
            if disk_instance is not None:
                account_sources.append(disk_instance["params"])
                account_patterns.append(disk_instance["pattern"])
        account = _readiness_account(
            basis=source["basis"],
            generator=generator,
            blockers=list(source.get("blockers") or []),
            params_sources=account_sources,
            pattern_sources=account_patterns,
            authoring_hole_params=list(
                (source.get("authoring_hole") or {}).get("params") or []
            ),
            evidence=list(source.get("evidence") or []),
            validation_problems=(
                _draft_validation_problems(
                    name=model["name"],
                    generator=generator,
                    params=account_sources[-1],
                    pattern_name=pattern_name,
                    definitions=pattern_definitions,
                )
                if isinstance(pattern_name, str) and pattern_name != "TODO"
                else []
            ),
        )
        model["readiness"] = account
        document: dict[str, Any] = {
            "model": model["name"],
            "current_state": model["state"],
            "generator": generator,
            "basis": source["basis"],
        }
        if source["basis"] == "fit":
            document["evidence"] = source["evidence"]
        elif source.get("evidence"):
            document["evidence"] = source["evidence"]
        if source["basis"] != "fit":
            document["generator_note"] = source["generator_note"]
            document["no_fit_reason"] = source["no_fit_reason"]
            document["layer"] = source["layer"]
            document["classification"] = source["classification"]
            document["inputs"] = source["inputs"]
            document["authoring_hole"] = source["authoring_hole"]
        if model.get("declared_interface"):
            document["declared_interface"] = model["declared_interface"]
        if source.get("constructs"):
            document["constructs"] = source["constructs"]
        document["suggested_pattern"] = suggested_pattern
        document["instance"] = {
            "name": model["name"],
            "pattern": pattern_name,
            "file": instance_relative,
        }
        document["readiness"] = account
        _interface = model.get("declared_interface") or {}
        document["promotion"] = _draft_promotion_steps(
            model["name"],
            pattern_step,
            authoring=source["basis"] == "authoring",
            registered=model["state"] == "known_not_controlled",
            practitioner_yaml=any(
                _interface.get(key)
                for key in ("generic_tests", "unit_tests", "semantic_models")
            ),
            siblings=(
                pattern_draft_counts.get(pattern_name, 1) - 1
                if isinstance(pattern_name, str) and pattern_name != "TODO"
                else 0
            ),
            pattern_name=(
                pattern_name if isinstance(pattern_name, str) else None
            ),
        )
        document["review_todos"] = todos
        if source.get("source_rationale"):
            document["source_rationale"] = source["source_rationale"]
        if model.get("interface_flags"):
            document["interface_flags"] = model["interface_flags"]
        record_text = _DRAFT_HEADER + yaml.safe_dump(
            document, sort_keys=False
        )
        record_relative = f"local/drafts/{model['name']}.yml"
        record_written = False
        if target.exists():
            if target.read_text(encoding="utf-8") != record_text:
                note_divergence(model["name"], record_relative, "record")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(record_text, encoding="utf-8")
            record_written = True
        if record_written:
            written.append(str(target))
        elif instance_written:
            written.append(str(instance_target))
    if divergences:
        report["draft_divergences"] = divergences
    return written
