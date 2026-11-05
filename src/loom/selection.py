"""The selector engine: one parser and evaluator behind search
and ``--select``.

Grammar - two layers over one parser:

- Compatibility layer (dbt semantics, adopted): bare names and
  name patterns with ``*``, the ``tag:`` / ``path:`` /
  ``package:`` methods, graph operators ``+name``, ``name+``,
  ``+name+`` and ``@name`` with depth forms (``2+name``,
  ``name+3``), and ``config.`` - recognised but refused, because
  Loom's inventory records carry no dbt config fields in Alpha,
  so there is nothing truthful for it to match.
- Loom layer: ``generator:``, ``pattern:``, ``paradigm:``,
  ``classification:``, ``owner:``, ``column:``, ``key:``,
  ``input:`` (declared inputs; there is no ``dependency:``
  qualifier), ``is:``, ``has:``, ``doc:`` (substring over model
  and column documentation text), ``kind:``, the word-form
  graph qualifiers ``upstream:`` / ``downstream:`` with an
  optional ``depth:`` companion in the same selector, and
  ``selector:`` for dbt-style YAML named selectors.

Set algebra (mirroring dbt): commas inside one selector
argument intersect; separate arguments union; ``--exclude``
subtracts. There are no OR/AND keywords and no parentheses.

Evaluation has exactly two modes. ``select`` is strict: every
union branch must match at least one record in the command's
pool, or the branch is an error naming it (deliberately
stricter than dbt). ``search`` never errors on an empty branch,
and integrates shape banding: a branch carrying two or
more shape qualifiers (``key:``, ``column:``, ``input:``,
``generator:``, ``paradigm:``) returns full matches plus close
matches ranked by fingerprint overlap (``loom.similarity``:
the weighted shape comparison, key-gated).
Selection never bands: the
identical string on an action command matches only full
matches.

Qualifier values match exactly (case-insensitive) unless they
carry a ``*`` glob; bare terms are the forgiving form - exact
names in selection, ranked substring matches in search.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from loom.errors import LoomError
from loom.similarity import compare, fingerprint_from_record, fingerprint_from_shape

# The shape qualifiers whose overlap drives search banding.
SHAPE_KEYS = frozenset({"key", "column", "input", "generator", "paradigm"})

_QUALIFIER_KEYS = frozenset(
    {
        "generator",
        "pattern",
        "paradigm",
        "classification",
        "owner",
        "column",
        "key",
        "input",
        "doc",
        "kind",
        "is",
        "has",
        "tag",
        "path",
        "package",
    }
)

_KIND_VALUES = frozenset(
    {"model", "macro", "source", "seed", "snapshot", "test", "exposure"}
)
_IS_VALUES = frozenset(
    {"generated", "registered", "unmanaged", "orphaned", "project", "package"}
)
_HAS_VALUES = frozenset(
    {
        "owner",
        "no-owner",
        "tests",
        "no-tests",
        "description",
        "no-description",
        "undocumented-columns",
    }
)

_QUALIFIER_CATALOGUE = (
    "generator:, pattern:, paradigm:, classification:, owner:, "
    "column:, key:, input:, doc:, kind:, is:, has:, tag:, path:, "
    "package:, upstream:, downstream:, depth: (a companion to "
    "upstream:/downstream:), selector: (a named selector from "
    "selectors.yml)"
)

# Loom-known status as the final ranking tiebreak (7.5 item 1):
# generated and registered before unmanaged; records without a
# status (tier 2) last.
_STATUS_RANK = {"generated": 0, "registered": 1, "orphaned": 2, "unmanaged": 3}


@dataclass(frozen=True)
class SelectableRecord:
    """The engine's view of one inventory record, adapted from
    the inventory corpus (or, for prove, from a manifest document)."""

    name: str
    kind: str = "model"
    status: str | None = None
    generator: str | None = None
    pattern: str | None = None
    paradigm: str | None = None
    classification: str | None = None
    owner: str | None = None
    tags: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    source_inputs: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    columns_partial: bool = False
    description: str | None = None
    column_descriptions: tuple[tuple[str, str | None], ...] = ()
    has_owner: bool = False
    has_tests: bool = False
    documented: bool = False
    columns_documented: int = 0
    columns_total: int = 0
    upstream: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    package: str | None = None
    provenance: str = "project"
    path: str | None = None
    arguments: tuple[str, ...] = ()
    used_by: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        """The selection-layer identity of the record, unique
        per record: the bare name for everything except macros.
        Model names are unique project-wide (dbt guarantees
        it), but macro names live in package namespaces, so a
        macro's identity is package-qualified - same-name
        macros from different packages coexist instead of
        collapsing onto the first (M6)."""
        if self.kind == "macro" and self.package:
            return f"{self.package}.{self.name}"
        return self.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "generator": self.generator,
            "pattern": self.pattern,
            "paradigm": self.paradigm,
            "classification": self.classification,
            "owner": self.owner,
            "tags": list(self.tags),
            "keys": list(self.keys),
            "inputs": list(self.inputs),
            "source_inputs": list(self.source_inputs),
            "columns": list(self.columns),
            "description": self.description,
            "has_owner": self.has_owner,
            "has_tests": self.has_tests,
            "documented": self.documented,
            "columns_documented": self.columns_documented,
            "columns_total": self.columns_total,
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
            "package": self.package,
            "provenance": self.provenance,
            "path": self.path,
            "arguments": list(self.arguments),
            "used_by": list(self.used_by),
        }


def records_from_corpus(corpus: Any) -> list[SelectableRecord]:
    """Adapt an ``InventoryCorpus`` to engine records: tier-1
    models plus tier-2 objects, in corpus order."""
    records: list[SelectableRecord] = []
    project_name = corpus.project_name
    for model in corpus.models:
        records.append(
            SelectableRecord(
                name=model.name,
                kind="model",
                status=model.status,
                generator=model.generator,
                pattern=model.pattern,
                paradigm=model.paradigm,
                classification=model.classification,
                owner=model.owner,
                tags=model.tags,
                keys=model.keys,
                inputs=model.inputs,
                source_inputs=model.source_inputs,
                columns=tuple(column.name for column in model.columns),
                columns_partial=model.columns_partial,
                description=model.description,
                column_descriptions=tuple(
                    (column.name, column.description) for column in model.columns
                ),
                has_owner=model.has_owner,
                has_tests=model.has_tests,
                documented=model.documented,
                columns_documented=model.columns_documented,
                columns_total=model.columns_total,
                upstream=model.upstream,
                downstream=model.downstream,
                package=model.package,
                provenance=(
                    "package"
                    if model.package is not None
                    and project_name is not None
                    and model.package != project_name
                    else "project"
                ),
                path=model.path,
            )
        )
    for obj in corpus.objects:
        records.append(
            SelectableRecord(
                name=obj.name,
                kind=obj.kind,
                description=obj.description,
                documented=obj.description is not None,
                upstream=obj.upstream,
                downstream=obj.downstream,
                package=obj.package,
                provenance=obj.provenance,
                path=obj.path,
                arguments=tuple(
                    argument.name for argument in obj.arguments or ()
                ),
                used_by=tuple(obj.used_by or ()),
            )
        )
    return records


def records_from_manifest_document(document: dict[str, Any]) -> list[SelectableRecord]:
    """Engine records from a raw dbt manifest document: its
    model and snapshot nodes, with the graph read from
    ``depends_on``. Used where a command's pool is a manifest's
    own subjects (prove's equivalence stage; validate's
    manifest-only names)."""
    metadata = document.get("metadata")
    project_name = (
        metadata.get("project_name") if isinstance(metadata, dict) else None
    )
    nodes = document.get("nodes")
    if not isinstance(nodes, dict):
        return []
    names_by_id: dict[str, str] = {}
    for unique_id, node in nodes.items():
        if isinstance(node, dict) and isinstance(node.get("name"), str):
            names_by_id[str(unique_id)] = node["name"]
    records: list[SelectableRecord] = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in ("model", "snapshot"):
            continue
        name = node.get("name")
        if not isinstance(name, str):
            continue
        depends_on = node.get("depends_on")
        upstream: list[str] = []
        if isinstance(depends_on, dict):
            for target in depends_on.get("nodes") or []:
                if isinstance(target, str) and target in names_by_id:
                    upstream.append(names_by_id[target])
        columns = node.get("columns")
        column_names: list[str] = []
        column_descriptions: list[tuple[str, str | None]] = []
        documented_columns = 0
        if isinstance(columns, dict):
            for column_name, column in columns.items():
                if not isinstance(column_name, str):
                    continue
                column_names.append(column_name)
                description = (
                    column.get("description") if isinstance(column, dict) else None
                )
                if isinstance(description, str) and description:
                    documented_columns += 1
                    column_descriptions.append((column_name, description))
                else:
                    column_descriptions.append((column_name, None))
        description = node.get("description")
        description = description if isinstance(description, str) else None
        meta = node.get("meta")
        owner = meta.get("owner") if isinstance(meta, dict) else None
        tags = node.get("tags")
        package = node.get("package_name")
        records.append(
            SelectableRecord(
                name=name,
                kind=str(node.get("resource_type")),
                generator=None,
                owner=owner if isinstance(owner, str) else None,
                tags=tuple(tags) if isinstance(tags, list) else (),
                inputs=tuple(dict.fromkeys(upstream)),
                upstream=tuple(dict.fromkeys(upstream)),
                columns=tuple(column_names),
                column_descriptions=tuple(column_descriptions),
                description=description,
                documented=bool(description),
                columns_documented=documented_columns,
                columns_total=len(column_names),
                has_owner=isinstance(owner, str) and bool(owner),
                package=package if isinstance(package, str) else None,
                provenance=(
                    "package"
                    if isinstance(package, str)
                    and project_name is not None
                    and package != project_name
                    else "project"
                ),
                path=(
                    node["original_file_path"].replace("\\", "/")
                    if isinstance(node.get("original_file_path"), str)
                    else None
                ),
            )
        )
    # Downstream is the inversion of the upstream edges above.
    downstream: dict[str, list[str]] = {}
    for record in records:
        for target in record.upstream:
            downstream.setdefault(target, []).append(record.name)
    return [
        replace(record, downstream=tuple(sorted(downstream.get(record.name, []))))
        for record in records
    ]


# --- Parsing ---------------------------------------------------


@dataclass(frozen=True)
class Criterion:
    """One parsed criterion: a name pattern, a qualifier, a
    graph form, a named-selector reference, or the ``depth:``
    companion (folded into its branch's word-form graph
    criteria at parse time, so it never reaches evaluation)."""

    text: str
    kind: str  # name | qualifier | graph | named | depth
    pattern: str | None = None
    key: str | None = None
    value: str | None = None
    expand_up: bool = False
    up_steps: int | None = None  # None = unbounded, when expand_up
    expand_down: bool = False
    down_steps: int | None = None
    at_form: bool = False
    word_form: bool = False
    shape: bool = False


def _unknown_qualifier(key: str, whole: str) -> LoomError:
    return LoomError(
        f"unknown qualifier {key + ':'!r} in selector {whole!r}; valid "
        f"qualifiers are {_QUALIFIER_CATALOGUE} - for example "
        f"'key:customer_id', 'generator:hub' or 'tag:nightly'"
    )


def _config_error(whole: str) -> LoomError:
    return LoomError(
        f"config. matching in selector {whole!r} is not available: "
        "Loom's inventory records carry no dbt config fields in "
        "Alpha, so there is nothing truthful for config.<key>:<value> "
        "to match; select on the fields the records do carry "
        f"({_QUALIFIER_CATALOGUE})"
    )


def _parse_criterion(text: str, whole: str) -> Criterion:
    original = text
    if text.startswith("@"):
        anchor = text[1:]
        if not anchor or ":" in anchor or "+" in anchor:
            raise LoomError(
                f"criterion {original!r} in selector {whole!r}: the @ "
                "form wraps a bare model name (for example "
                "'@hub_customer') and takes no depth or qualifier"
            )
        return Criterion(
            text=original,
            kind="graph",
            pattern=anchor,
            expand_down=True,
            at_form=True,
        )
    core = text
    expand_up = expand_down = False
    up_steps: int | None = None
    down_steps: int | None = None
    lead = re.match(r"^(\d*)\+", core)
    if lead:
        expand_up = True
        up_steps = int(lead.group(1)) if lead.group(1) else None
        core = core[lead.end() :]
    trail = re.search(r"\+(\d*)$", core)
    if trail:
        expand_down = True
        down_steps = int(trail.group(1)) if trail.group(1) else None
        core = core[: trail.start()]
    if not core:
        raise LoomError(
            f"criterion {original!r} in selector {whole!r} is only a "
            "graph operator; it must wrap a model name or a qualifier "
            "(for example '+hub_customer' or 'hub_customer+2')"
        )
    wrapped = expand_up or expand_down
    if ":" in core:
        key, _, value = core.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not value:
            raise LoomError(
                f"qualifier {key + ':'!r} in selector {whole!r} needs a "
                f"value - for example '{key}:<name>'"
            )
        if key.startswith("config."):
            raise _config_error(whole)
        if key in ("upstream", "downstream"):
            if wrapped:
                raise LoomError(
                    f"criterion {original!r} in selector {whole!r}: "
                    f"{key}: already names its anchor and direction; "
                    "graph operators do not wrap it (state a depth "
                    f"with a depth: companion instead, as in "
                    f"'{key}:{value},depth:2')"
                )
            if key == "upstream":
                return Criterion(
                    text=original,
                    kind="graph",
                    pattern=value,
                    expand_up=True,
                    word_form=True,
                )
            return Criterion(
                text=original,
                kind="graph",
                pattern=value,
                expand_down=True,
                word_form=True,
            )
        if key == "depth":
            if wrapped:
                raise LoomError(
                    f"criterion {original!r} in selector {whole!r}: "
                    "depth: is a companion to upstream:/downstream: "
                    "and takes no graph operators"
                )
            if not value.isdigit():
                raise LoomError(
                    f"depth: in selector {whole!r} needs a whole number "
                    f"of steps, got {value!r}"
                )
            return Criterion(text=original, kind="depth", value=value)
        if key == "selector":
            return Criterion(
                text=original,
                kind="named",
                value=value,
                expand_up=expand_up,
                up_steps=up_steps,
                expand_down=expand_down,
                down_steps=down_steps,
            )
        if key not in _QUALIFIER_KEYS:
            raise _unknown_qualifier(key, whole)
        folded = value.casefold()
        if key == "kind" and folded not in _KIND_VALUES:
            raise LoomError(
                f"kind: in selector {whole!r} takes one of "
                f"{', '.join(sorted(_KIND_VALUES))}; got {value!r}"
            )
        if key == "is" and folded not in _IS_VALUES:
            raise LoomError(
                f"is: in selector {whole!r} takes one of "
                f"{', '.join(sorted(_IS_VALUES))}; got {value!r}"
            )
        if key == "has" and folded not in _HAS_VALUES:
            raise LoomError(
                f"has: in selector {whole!r} takes one of "
                f"{', '.join(sorted(_HAS_VALUES))}; got {value!r}"
            )
        return Criterion(
            text=original,
            kind="qualifier",
            key=key,
            value=value,
            expand_up=expand_up,
            up_steps=up_steps,
            expand_down=expand_down,
            down_steps=down_steps,
            shape=key in SHAPE_KEYS,
        )
    return Criterion(
        text=original,
        kind="graph" if wrapped else "name",
        pattern=core,
        expand_up=expand_up,
        up_steps=up_steps,
        expand_down=expand_down,
        down_steps=down_steps,
    )


def parse_branch(text: str) -> tuple[Criterion, ...]:
    """Parse one selector argument: comma-separated criteria,
    intersected. The ``depth:`` companion is folded into the
    branch's word-form graph criteria here."""
    parts = [part.strip() for part in text.split(",")]
    if any(not part for part in parts):
        raise LoomError(
            f"selector {text!r} contains an empty criterion (a "
            "leading, trailing or doubled comma); criteria are "
            "comma-separated and every criterion must name something"
        )
    criteria = [_parse_criterion(part, text) for part in parts]
    depths = [criterion for criterion in criteria if criterion.kind == "depth"]
    if depths:
        if len(depths) > 1:
            raise LoomError(
                f"selector {text!r} states depth: more than once; "
                "one depth applies to the selector's graph qualifiers"
            )
        word_forms = [c for c in criteria if c.word_form]
        if not word_forms:
            raise LoomError(
                f"selector {text!r}: depth: only applies alongside "
                "upstream: or downstream: in the same selector (the "
                "operator forms carry their depth inline, as in "
                "'2+hub_customer')"
            )
        steps = int(depths[0].value or "0")
        criteria = [
            (
                replace(criterion, up_steps=steps)
                if criterion.expand_up
                else replace(criterion, down_steps=steps)
            )
            if criterion.word_form
            else criterion
            for criterion in criteria
            if criterion.kind != "depth"
        ]
    return tuple(criteria)


# --- Evaluation ------------------------------------------------


def _glob(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value.casefold(), pattern.casefold())


def _value_match(pattern: str, value: str | None) -> bool:
    """A qualifier value against one field value: exact
    (case-insensitive), or a glob when the pattern carries
    ``*``."""
    if value is None:
        return False
    if "*" in pattern:
        return _glob(pattern, value)
    return value.casefold() == pattern.casefold()


def _any_match(pattern: str, values: Iterable[str]) -> bool:
    return any(_value_match(pattern, value) for value in values)


class _Index:
    """Records by identity, plus direct graph edges.

    Direct edges come from a record's declared inputs where it
    declares them. Manifest-backed model records carry their
    transitive closure in ``upstream`` instead, so their direct
    edges are the closure's transitive reduction (unique over a
    DAG); tier-2 records carry direct edges in their own fields,
    and a macro's downstream is its used-by list (7.4).

    Edges are written with bare names; they resolve to
    identities through :meth:`_resolve`. Only macro identities
    can be ambiguous under a bare name, and resolution follows
    dbt's override order: the referrer's own package first,
    then the project's record, then identity order."""

    def __init__(self, records: list[SelectableRecord]) -> None:
        self.records = records
        self.by_name: dict[str, SelectableRecord] = {}
        self.by_bare_name: dict[str, list[SelectableRecord]] = {}
        for record in records:
            self.by_name.setdefault(record.identity.casefold(), record)
            self.by_bare_name.setdefault(
                record.name.casefold(), []
            ).append(record)
        upstream_ids: dict[str, set[str]] = {}
        for record in records:
            resolved = {
                self._resolve(name, record) for name in record.upstream
            }
            upstream_ids[record.identity] = {
                item for item in resolved if item is not None
            }
        up: dict[str, set[str]] = {}
        for record in records:
            if record.kind == "model" and record.inputs:
                direct = {
                    item
                    for item in (
                        self._resolve(name, record) for name in record.inputs
                    )
                    if item is not None
                }
            elif record.kind == "model":
                closure = upstream_ids[record.identity] - {record.identity}
                direct = {
                    candidate
                    for candidate in closure
                    if not any(
                        candidate in upstream_ids.get(other, set())
                        for other in closure
                        if other != candidate
                    )
                }
            else:
                direct = set(upstream_ids[record.identity])
            up[record.identity] = direct
        down: dict[str, set[str]] = {}
        for name, targets in up.items():
            for target in targets:
                down.setdefault(target, set()).add(name)
        for record in records:
            if record.kind == "model":
                continue
            # A tier-2 record's declared downstream is lineage
            # (a source's consumers): the edge runs both ways.
            for target in record.downstream:
                canonical = self._resolve(target, record)
                if canonical is not None and canonical != record.identity:
                    down.setdefault(record.identity, set()).add(canonical)
                    up.setdefault(canonical, set()).add(record.identity)
            # A macro's used-by is impact only: it is the
            # macro's downstream, never a model's upstream - a
            # model does not depend on its macros for lineage.
            for target in record.used_by:
                canonical = self._resolve(target, record)
                if canonical is not None and canonical != record.identity:
                    down.setdefault(record.identity, set()).add(canonical)
        self.up_edges = up
        self.down_edges = down

    def _resolve(
        self, name: str, referrer: SelectableRecord | None = None
    ) -> str | None:
        """A bare (or already-qualified) edge name to the
        identity it denotes, or None when no record carries it."""
        folded = name.casefold()
        record = self.by_name.get(folded)
        if record is not None:
            return record.identity
        candidates = self.by_bare_name.get(folded, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0].identity
        if referrer is not None and referrer.package:
            for candidate in candidates:
                if candidate.package == referrer.package:
                    return candidate.identity
        for candidate in candidates:
            if candidate.provenance == "project":
                return candidate.identity
        return sorted(candidate.identity for candidate in candidates)[0]

    def closure(
        self, seeds: set[str], direction: str, steps: int | None
    ) -> set[str]:
        """The seeds plus everything reachable within ``steps``
        edges (unbounded when ``steps`` is None)."""
        edges = self.up_edges if direction == "up" else self.down_edges
        seen = set(seeds)
        frontier = set(seeds)
        remaining = steps
        while frontier and (remaining is None or remaining > 0):
            reached: set[str] = set()
            for node in frontier:
                reached |= edges.get(node, set())
            reached -= seen
            seen |= reached
            frontier = reached
            if remaining is not None:
                remaining -= 1
        return seen


def _apply_wrap(
    index: _Index, base: set[str], criterion: Criterion
) -> set[str]:
    if criterion.at_form:
        # dbt's @: the anchors, their full downstream closure,
        # and the parents of the descendants in that closure.
        descendants = index.closure(base, "down", None)
        parents: set[str] = set()
        for node in descendants - set(base):
            parents |= index.up_edges.get(node, set())
        return descendants | parents
    result = set(base)
    if criterion.expand_up:
        result |= index.closure(base, "up", criterion.up_steps)
    if criterion.expand_down:
        result |= index.closure(base, "down", criterion.down_steps)
    return result


def _anchor_names(index: _Index, pattern: str) -> set[str]:
    """Graph anchors resolve by exact name or ``*`` glob, in
    both modes: a graph question names specific models."""
    return {
        record.identity
        for record in index.records
        if _value_match(pattern, record.name)
        or (
            record.identity != record.name
            and _value_match(pattern, record.identity)
        )
    }


def _bare_tier(record: SelectableRecord, term: str) -> int | None:
    """A bare term's match tier against one record in search
    mode (7.5 item 1), or None when the term does not match."""
    folded = term.casefold()
    if record.identity != record.name and record.identity.casefold() == folded:
        return 0
    name = record.name.casefold()
    if name == folded:
        return 0
    if name.startswith(folded):
        return 1
    if folded in name:
        return 2
    fields: list[str] = []
    if record.owner:
        fields.append(record.owner)
    fields.extend(record.tags)
    fields.extend(record.columns)
    fields.extend(record.keys)
    if record.package:
        fields.append(record.package)
    if any(folded in value.casefold() for value in fields):
        return 3
    # Documentation matches split at the plan's line (7.5
    # item 1): the model's own description text ranks before
    # its columns' description text.
    if record.description and folded in record.description.casefold():
        return 4
    if any(
        description and folded in description.casefold()
        for _column, description in record.column_descriptions
    ):
        return 5
    return None


def _name_matches_select(record: SelectableRecord, pattern: str) -> bool:
    """A bare term in selection: exact name, ``*`` glob over the
    name, a bare package name (dbt selects a package by name),
    or the dbt fqn shape - dot/slash-separated path segments
    ending in the model's name, matched against the record's
    path (``marts.finance`` selects the model ``finance`` under
    a ``marts`` path segment)."""
    if _value_match(pattern, record.name):
        return True
    if record.identity != record.name and _value_match(
        pattern, record.identity
    ):
        return True
    if "*" not in pattern and record.package is not None:
        if record.package.casefold() == pattern.casefold():
            return True
    segments = [part for part in re.split(r"[./]", pattern) if part]
    if len(segments) > 1 and record.path:
        if segments[-1].casefold() != record.name.casefold():
            return False
        path_segments = [part.casefold() for part in record.path.split("/")]
        wanted = [part.casefold() for part in segments[:-1]]
        position = 0
        for segment in path_segments:
            if position < len(wanted) and segment == wanted[position]:
                position += 1
        return position == len(wanted)
    return False


def _qualifier_matches(criterion: Criterion, record: SelectableRecord) -> bool:
    key = criterion.key
    value = criterion.value or ""
    if key == "generator":
        return _value_match(value, record.generator)
    if key == "pattern":
        return _value_match(value, record.pattern)
    if key == "paradigm":
        return _value_match(value, record.paradigm)
    if key == "classification":
        return _value_match(value, record.classification)
    if key == "owner":
        return _value_match(value, record.owner)
    if key == "tag":
        return _any_match(value, record.tags)
    if key == "path":
        if record.path is None:
            return False
        if "*" in value:
            return _glob(value, record.path)
        return value.casefold() in record.path.casefold()
    if key == "package":
        return _value_match(value, record.package)
    if key == "column":
        return _any_match(value, record.columns)
    if key == "key":
        return _any_match(value, record.keys)
    if key == "input":
        if _any_match(value, record.inputs):
            return True
        for source_input in record.source_inputs:
            if _value_match(value, source_input):
                return True
            source_name = source_input.partition(".")[0]
            if _value_match(value, source_name):
                return True
        return False
    if key == "doc":
        folded = value.casefold()
        if record.description and folded in record.description.casefold():
            return True
        return any(
            description and folded in description.casefold()
            for _column, description in record.column_descriptions
        )
    if key == "kind":
        return record.kind.casefold() == value.casefold()
    if key == "is":
        folded = value.casefold()
        if folded in ("project", "package"):
            return record.provenance == folded
        return record.status is not None and record.status.casefold() == folded
    if key == "has":
        folded = value.casefold()
        # Owner and test presence are model hygiene facts.
        # Tier-2 records carry both as false by construction,
        # so unscoped they would match every macro, source and
        # test in the corpus and the headline count would span
        # every record kind - the scope guard the
        # undocumented-columns form below already applies by
        # its own means (columns_total > 0).
        if folded == "owner":
            return record.kind == "model" and record.has_owner
        if folded == "no-owner":
            return record.kind == "model" and not record.has_owner
        if folded == "tests":
            return record.kind == "model" and record.has_tests
        if folded == "no-tests":
            return record.kind == "model" and not record.has_tests
        if folded == "description":
            return record.documented
        if folded == "no-description":
            return not record.documented
        if folded == "undocumented-columns":
            return (
                record.columns_total > 0
                and record.columns_documented < record.columns_total
            )
    return False


# A named-selector expression tree: ("branch", criteria),
# ("union", children), ("intersection", children) or
# ("difference", base, children).
_Expr = tuple


def _eval_expr(
    index: _Index, node: _Expr, mode: str, named: dict[str, _Expr]
) -> set[str]:
    form = node[0]
    if form == "branch":
        return set(_intersect(index, list(node[1]), mode, named))
    if form == "union":
        result: set[str] = set()
        for child in node[1]:
            result |= _eval_expr(index, child, mode, named)
        return result
    if form == "intersection":
        result: set[str] | None = None
        for child in node[1]:
            child_set = _eval_expr(index, child, mode, named)
            result = child_set if result is None else result & child_set
        return result or set()
    if form == "difference":
        result = _eval_expr(index, node[1], mode, named)
        for child in node[2]:
            result -= _eval_expr(index, child, mode, named)
        return result
    raise LoomError(f"internal: unknown named-selector form {form!r}")


def _criterion_set(
    index: _Index,
    criterion: Criterion,
    mode: str,
    named: dict[str, _Expr],
) -> dict[str, int]:
    """One criterion's match set: record identity to bare-term
    tier (0 except for bare terms in search mode)."""
    if criterion.kind == "named":
        name = (criterion.value or "").casefold()
        if name not in named:
            available = ", ".join(sorted(named)) or "none defined"
            raise LoomError(
                f"named selector {criterion.value!r} is not defined "
                f"(selectors.yml in the project root defines: "
                f"{available}); define it there or use a plain selector"
            )
        base = _eval_expr(index, named[name], mode, named)
        return {item: 0 for item in _apply_wrap(index, base, criterion)}
    if criterion.kind == "graph":
        base = _anchor_names(index, criterion.pattern or "")
        return {item: 0 for item in _apply_wrap(index, base, criterion)}
    if criterion.kind == "name":
        if mode == "search":
            pattern = criterion.pattern or ""
            if "*" in pattern:
                return {
                    record.identity: 2
                    for record in index.records
                    if _glob(pattern, record.name)
                    or (
                        record.identity != record.name
                        and _glob(pattern, record.identity)
                    )
                }
            tiers: dict[str, int] = {}
            for record in index.records:
                tier = _bare_tier(record, pattern)
                if tier is not None:
                    tiers[record.identity] = tier
            return tiers
        return {
            record.identity: 0
            for record in index.records
            if _name_matches_select(record, criterion.pattern or "")
        }
    # qualifier
    base = {
        record.identity
        for record in index.records
        if _qualifier_matches(criterion, record)
    }
    if criterion.expand_up or criterion.expand_down or criterion.at_form:
        base = _apply_wrap(index, base, criterion)
    return {item: 0 for item in base}


def _intersect(
    index: _Index,
    criteria: list[Criterion],
    mode: str,
    named: dict[str, _Expr],
) -> dict[str, int]:
    """Intersect criteria match sets; a surviving record's tier
    is its weakest bare-term tier across the branch."""
    result: dict[str, int] | None = None
    for criterion in criteria:
        matched = _criterion_set(index, criterion, mode, named)
        if result is None:
            result = dict(matched)
        else:
            result = {
                name: max(tier, matched[name])
                for name, tier in result.items()
                if name in matched
            }
        if not result:
            break
    return result or {}


@dataclass
class CloseMatch:
    """A banded search near-miss: the shape criteria it
    satisfies and the ones it misses, plus its fingerprint
    similarity against the shape the branch describes:
    the weighted overlap ``score``, the key-gate band, and the
    shares / differs explanation phrases."""

    name: str
    satisfied: list[str]
    missing: list[str]
    tier: int = 0
    score: float = 0.0
    gate: int = 0
    shares: list[str] = field(default_factory=list)
    differs: list[str] = field(default_factory=list)

    @property
    def overlap(self) -> int:
        return len(self.satisfied)


@dataclass
class BranchOutcome:
    text: str
    full: dict[str, int]  # name -> bare-term tier
    close: list[CloseMatch] = field(default_factory=list)
    banded: bool = False


@dataclass
class Selection:
    """The result of evaluating selectors over a record set."""

    names: list[str]  # final names, after --exclude
    full: dict[str, list[str]]  # name -> matched criterion texts
    close: list[CloseMatch]
    banded: bool
    branches: list[BranchOutcome]
    excluded: list[str]
    ranks: dict[str, int]


def _eval_branch(
    index: _Index,
    criteria: tuple[Criterion, ...],
    text: str,
    *,
    mode: str,
    named: dict[str, _Expr],
) -> BranchOutcome:
    if mode == "search":
        shape = [criterion for criterion in criteria if criterion.shape]
        if len(shape) >= 2:
            others = [criterion for criterion in criteria if not criterion.shape]
            base = (
                _intersect(index, others, mode, named)
                if others
                else {record.identity: 0 for record in index.records}
            )
            shape_sets = [
                (criterion, set(_criterion_set(index, criterion, mode, named)))
                for criterion in shape
            ]
            # The shape the branch describes, as a fingerprint
            # target: the exact values of its shape
            # qualifiers. Glob values name a pattern, not a shape
            # element, so they stay per-criterion (they count in
            # satisfied / missing, never in the fingerprint).
            target = fingerprint_from_shape(
                keys=[
                    criterion.value or ""
                    for criterion in shape
                    if criterion.key == "key" and "*" not in (criterion.value or "")
                ],
                inputs=[
                    criterion.value or ""
                    for criterion in shape
                    if criterion.key == "input"
                    and "*" not in (criterion.value or "")
                ],
                columns=[
                    criterion.value or ""
                    for criterion in shape
                    if criterion.key == "column"
                    and "*" not in (criterion.value or "")
                ],
                generator=next(
                    (
                        criterion.value
                        for criterion in shape
                        if criterion.key == "generator"
                        and "*" not in (criterion.value or "")
                    ),
                    None,
                ),
                family=next(
                    (
                        criterion.value
                        for criterion in shape
                        if criterion.key == "paradigm"
                        and "*" not in (criterion.value or "")
                    ),
                    None,
                ),
            )
            full: dict[str, int] = {}
            close: list[CloseMatch] = []
            for name, tier in base.items():
                satisfied = [
                    criterion.text
                    for criterion, matched in shape_sets
                    if name in matched
                ]
                missing = [
                    criterion.text
                    for criterion, matched in shape_sets
                    if name not in matched
                ]
                if not missing:
                    full[name] = tier
                elif satisfied:
                    record = index.by_name[name.casefold()]
                    similarity = compare(
                        target,
                        fingerprint_from_record(record),
                        name=name,
                    )
                    close.append(
                        CloseMatch(
                            name=name,
                            satisfied=satisfied,
                            missing=missing,
                            tier=tier,
                            score=similarity.score,
                            gate=similarity.gate,
                            shares=list(similarity.shares),
                            differs=list(similarity.differs),
                        )
                    )
            return BranchOutcome(text=text, full=full, close=close, banded=True)
    full = _intersect(index, list(criteria), mode, named)
    return BranchOutcome(text=text, full=full)


def _status_rank(record: SelectableRecord) -> int:
    if record.status is None:
        return 4
    return _STATUS_RANK.get(record.status, 4)


def evaluate_selectors(
    records: list[SelectableRecord],
    select_texts: list[str],
    exclude_texts: Iterable[str] = (),
    *,
    mode: str,
    pool: Iterable[str] | None = None,
    named: dict[str, _Expr] | None = None,
    pool_label: str = "models",
) -> Selection:
    """Evaluate selector arguments over ``records``.

    ``mode`` is ``"select"`` (strict; every branch must match
    within ``pool`` when a pool is given, or it is an error
    naming the branch) or ``"search"`` (banding; empty
    branches are results, not errors). With no select texts the
    base set is every record, narrowed by ``pool`` and
    ``exclude_texts`` - the --exclude-only form."""
    if mode not in ("select", "search"):
        raise LoomError(f"internal: unknown selection mode {mode!r}")
    named = named or {}
    index = _Index(records)
    pool_set = set(pool) if pool is not None else None
    branches: list[BranchOutcome] = []
    full: dict[str, list[str]] = {}
    ranks: dict[str, int] = {}
    close_by_name: dict[str, CloseMatch] = {}
    banded = False
    if select_texts:
        for text in select_texts:
            criteria = parse_branch(text)
            outcome = _eval_branch(index, criteria, text, mode=mode, named=named)
            branches.append(outcome)
            if mode == "select" and not any(
                name in outcome.full
                for name in (pool_set if pool_set is not None else outcome.full)
            ):
                raise LoomError(f"--select {text!r} matched no {pool_label}")
            banded = banded or outcome.banded
            for name, tier in outcome.full.items():
                texts = full.setdefault(name, [])
                for criterion in criteria:
                    if criterion.text not in texts:
                        texts.append(criterion.text)
                ranks[name] = min(tier, ranks.get(name, tier))
            for match in outcome.close:
                existing = close_by_name.get(match.name)
                if existing is None or match.overlap > existing.overlap:
                    close_by_name[match.name] = match
    else:
        full = {record.identity: [] for record in records}
    names_set = set(full)
    if pool_set is not None:
        names_set &= pool_set
    excluded: list[str] = []
    if exclude_texts:
        excluded_set: set[str] = set()
        for text in exclude_texts:
            criteria = parse_branch(text)
            outcome = _eval_branch(index, criteria, text, mode="select", named=named)
            excluded_set |= set(outcome.full)
        excluded = sorted(names_set & excluded_set)
        names_set -= excluded_set
        close_by_name = {
            name: match
            for name, match in close_by_name.items()
            if name not in excluded_set
        }
    full = {name: full[name] for name in names_set if name in full}
    close = [
        match for name, match in close_by_name.items() if name not in names_set
    ]
    if mode == "search":
        names = sorted(
            names_set,
            key=lambda name: (
                ranks.get(name, 0),
                _status_rank(index.by_name[name.casefold()]),
                name.casefold(),
            ),
        )
        close.sort(
            key=lambda match: (
                match.gate,
                -match.score,
                -match.overlap,
                match.tier,
                _status_rank(index.by_name[match.name.casefold()]),
                match.name.casefold(),
            )
        )
    else:
        names = [
            record.identity for record in records if record.identity in names_set
        ]
    return Selection(
        names=names,
        full=full,
        close=close,
        banded=banded,
        branches=branches,
        excluded=excluded,
        ranks=ranks,
    )


# --- Zero-result diagnosis and suggestions (7.5 items 3-4) -----


def _levenshtein(a: str, b: str, limit: int) -> int:
    """Edit distance, early-exiting above ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            value = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[len(b)]


def suggest_names(
    records: list[SelectableRecord], term: str, *, limit: int = 3
) -> list[str]:
    """Closest record names to a zero-result bare term, by edit
    distance (suggestion only - never a silent correction)."""
    folded = term.casefold()
    ceiling = max(2, len(folded) // 3)
    scored: list[tuple[int, str]] = []
    for record in records:
        distance = _levenshtein(folded, record.name.casefold(), ceiling)
        if distance <= ceiling:
            scored.append((distance, record.name))
    scored.sort(key=lambda item: (item[0], item[1].casefold()))
    return [name for _distance, name in scored[:limit]]


def _facet_values(index: _Index, key: str) -> list[str]:
    if key == "has":
        # The has: dimension has no field to read values
        # from: its values are the named forms, and the ones
        # that exist in the project are the forms at least
        # one record satisfies.
        probe = Criterion(text="", kind="qualifier", key="has")
        return sorted(
            form
            for form in _HAS_VALUES
            if any(
                _qualifier_matches(replace(probe, value=form), record)
                for record in index.records
            )
        )
    values: set[str] = set()
    for record in index.records:
        if key == "generator":
            values |= {record.generator} if record.generator else set()
        elif key == "pattern":
            values |= {record.pattern} if record.pattern else set()
        elif key == "paradigm":
            values |= {record.paradigm} if record.paradigm else set()
        elif key == "classification":
            values |= {record.classification} if record.classification else set()
        elif key == "owner":
            values |= {record.owner} if record.owner else set()
        elif key == "tag":
            values |= set(record.tags)
        elif key == "column":
            values |= set(record.columns)
        elif key == "key":
            values |= set(record.keys)
        elif key == "input":
            values |= set(record.inputs)
            values |= {item.partition(".")[0] for item in record.source_inputs}
        elif key == "kind":
            values |= {record.kind}
        elif key == "package":
            values |= {record.package} if record.package else set()
        elif key == "path":
            values |= {record.path} if record.path else set()
        elif key == "doc":
            # doc: searches description text, so the values
            # that exist are the description texts themselves
            # - the model's own and its columns'.
            if record.description:
                values.add(record.description)
            values |= {
                description
                for _column, description in record.column_descriptions
                if description
            }
        elif key == "is":
            values |= {record.status} if record.status else set()
            values |= {record.provenance}
    return sorted(values, key=str.casefold)


@dataclass
class EmptyBranchDiagnosis:
    """Why one search branch returned nothing: the criterion
    that emptied the running set, the facet values that do
    exist for the branch's qualifiers, and name suggestions
    when the culprit is a bare term."""

    branch: str
    culprit: str | None
    facets: dict[str, list[str]]
    suggestions: list[str]


def diagnose_empty_branch(
    records: list[SelectableRecord], branch_text: str
) -> EmptyBranchDiagnosis:
    """Replay one branch's criteria in order over the strict
    reading and name the first that empties the set (7.5
    item 4). Banding is ignored here: a branch with no full
    and no close matches is empty under both readings."""
    index = _Index(records)
    criteria = parse_branch(branch_text)
    running: set[str] = {record.identity for record in records}
    culprit: Criterion | None = None
    for criterion in criteria:
        matched = set(_criterion_set(index, criterion, "search", {}))
        narrowed = running & matched
        if not narrowed:
            culprit = criterion
            break
        running = narrowed
    facets: dict[str, list[str]] = {}
    for criterion in criteria:
        if criterion.kind == "qualifier" and criterion.key is not None:
            values = _facet_values(index, criterion.key)
            if values:
                facets[criterion.key] = values[:8]
    suggestions: list[str] = []
    if culprit is not None and culprit.kind == "name" and culprit.pattern:
        suggestions = suggest_names(records, culprit.pattern)
    return EmptyBranchDiagnosis(
        branch=branch_text,
        culprit=culprit.text if culprit is not None else None,
        facets=facets,
        suggestions=suggestions,
    )


# --- Named selectors (dbt-style YAML) ----------------


def _method_criterion(definition: dict[str, Any], selector_name: str) -> Criterion:
    method = definition.get("method")
    value = definition.get("value")
    if not isinstance(method, str) or not isinstance(value, str):
        raise LoomError(
            f"named selector {selector_name!r}: a method definition "
            "needs string 'method' and 'value' entries"
        )
    expand_up = bool(definition.get("parents"))
    expand_down = bool(definition.get("children"))
    up_steps = definition.get("parents_depth")
    down_steps = definition.get("children_depth")
    wrap = {
        "expand_up": expand_up,
        "up_steps": up_steps if isinstance(up_steps, int) else None,
        "expand_down": expand_down,
        "down_steps": down_steps if isinstance(down_steps, int) else None,
    }
    text = f"{method}:{value}"
    if method in ("tag", "path"):
        return Criterion(text=text, kind="qualifier", key=method, value=value, **wrap)
    if method in ("package_name", "package"):
        return Criterion(
            text=text, kind="qualifier", key="package", value=value, **wrap
        )
    if method == "resource_type":
        return Criterion(text=text, kind="qualifier", key="kind", value=value, **wrap)
    if method == "config":
        raise _config_error(f"selector:{selector_name}")
    if method == "fqn":
        return Criterion(text=text, kind="name", pattern=value, **wrap)
    raise LoomError(
        f"named selector {selector_name!r}: method {method!r} is not "
        "supported; supported methods are tag, path, package_name, "
        "resource_type and fqn"
    )


def _translate_definition(definition: Any, selector_name: str) -> _Expr:
    if isinstance(definition, str):
        return ("branch", parse_branch(definition))
    if isinstance(definition, list):
        if not definition:
            raise LoomError(
                f"named selector {selector_name!r}: an empty definition "
                "list selects nothing; give at least one definition"
            )
        return (
            "union",
            tuple(_translate_definition(item, selector_name) for item in definition),
        )
    if isinstance(definition, dict):
        base: _Expr | None = None
        if isinstance(definition.get("union"), list):
            base = (
                "union",
                tuple(
                    _translate_definition(item, selector_name)
                    for item in definition["union"]
                ),
            )
        elif isinstance(definition.get("intersection"), list):
            base = (
                "intersection",
                tuple(
                    _translate_definition(item, selector_name)
                    for item in definition["intersection"]
                ),
            )
        elif "method" in definition:
            base = ("branch", (_method_criterion(definition, selector_name),))
        if base is None:
            raise LoomError(
                f"named selector {selector_name!r}: a definition mapping "
                "must carry 'union', 'intersection' or 'method'"
            )
        exclude = definition.get("exclude")
        if exclude is not None:
            items = exclude if isinstance(exclude, list) else [exclude]
            return (
                "difference",
                base,
                tuple(_translate_definition(item, selector_name) for item in items),
            )
        return base
    raise LoomError(
        f"named selector {selector_name!r}: a definition is a selector "
        "string, a list of definitions, or a mapping with 'union', "
        "'intersection' or 'method'"
    )


def load_named_selectors(project_dir: str | Path) -> dict[str, _Expr]:
    """dbt-style named selectors from ``selectors.yml`` in the
    project root, translated to engine expressions. Returns an
    empty mapping when the project defines none."""
    root = Path(project_dir)
    path = root / "selectors.yml"
    if not path.is_file():
        path = root / "selectors.yaml"
    if not path.is_file():
        return {}
    import yaml

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise LoomError(f"{path}: cannot read named selectors: {error}") from error
    if not isinstance(document, dict) or not isinstance(
        document.get("selectors"), list
    ):
        raise LoomError(
            f"{path}: expected a mapping with a 'selectors' list of "
            "name/definition pairs (the dbt named-selector form)"
        )
    named: dict[str, _Expr] = {}
    for item in document["selectors"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise LoomError(
                f"{path}: every named selector needs a string 'name' "
                "and a 'definition'"
            )
        name = item["name"]
        if "definition" not in item:
            raise LoomError(f"{path}: named selector {name!r} has no 'definition'")
        if name.casefold() in named:
            raise LoomError(f"{path}: named selector {name!r} is defined twice")
        named[name.casefold()] = _translate_definition(item["definition"], name)
    return named
