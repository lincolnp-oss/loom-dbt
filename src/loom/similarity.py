"""Shape similarity over inventory records.

Every model record reduces to a **shape fingerprint**: its
generator family, its declared key set (its grain), its input
set (declared model inputs plus declared source inputs,
normalised to source and model identities), and its output
column set. The fingerprint is derived data for ranking and
explanation; it is never persisted and never presented as an
artefact in its own right.

Similarity is a deterministic overlap function over the four
components. The component weights are fixed here and are part
of the shipped documentation of behaviour (commands.md states
them too), not magic constants:

- **keys: weight 5.** The grain is the strongest shape signal,
  and it is also a gate: a candidate sharing none of the target
  keys can never enter the top band, however much else it
  shares (plan 6.3: "key match is a gate for the top band, then
  inputs, then output columns, then generator family").
- **inputs: weight 4.** Sharing lineage - the same declared
  inputs - is the next strongest signal.
- **output columns: weight 3.** Attribute overlap, scored as
  coverage of the target's column set.
- **generator family: weight 1.** Same paradigm (a satellite
  and a hub are both Data Vault) is a weak tiebreak-level
  signal; the exact generator is reported in the explanation
  but the scored component is the family, so the same shape
  built by a different generator family still ranks high.

Each component scores as coverage of the target: the share of
the target's elements the candidate also has (1.0 or 0.0 for
the family). The overall score is the weighted mean over the
components the target actually carries - a component the
target does not have (a reference model with no derivable
keys, say) is excluded from the denominator, never scored as
zero, and is named in the fingerprint's ``partial_components``
so a partial shape is visible rather than silent (plan 7.7).

Every comparison is explainable: ``shares`` and ``differs``
phrase lists per component, in target order, naming the actual
elements. A score without an explanation is a defect (6.3),
and the output language is always "close matches" / "shares /
differs" - similarity never asserts duplication (7.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from loom.errors import LoomError
from loom.generator_registry import GENERATOR_PARADIGM

# The documented component weights (see the module docstring).
WEIGHT_KEYS = 5
WEIGHT_INPUTS = 4
WEIGHT_COLUMNS = 3
WEIGHT_FAMILY = 1

_WEIGHTS = {
    "keys": WEIGHT_KEYS,
    "inputs": WEIGHT_INPUTS,
    "columns": WEIGHT_COLUMNS,
    "family": WEIGHT_FAMILY,
}


def _column_name(column: Any) -> str:
    """Output columns are bare names on engine records and
    ``CorpusColumn`` records on corpus models; both reduce to
    the name."""
    return column.name if hasattr(column, "name") else str(column)


def _input_forms(value: str) -> set[str]:
    """The identities one declared input answers to: a source
    input ``ecom.raw_customers`` is the source ``ecom``'s table
    and is also matched by the source name alone (the same
    reading the ``input:`` qualifier uses)."""
    folded = value.casefold()
    forms = {folded}
    source = folded.partition(".")[0]
    if source != folded:
        forms.add(source)
    return forms


@dataclass(frozen=True)
class ShapeFingerprint:
    """One model's normalised shape (plan 6.3). Element tuples
    keep their declared order and case for explanation; all
    comparison is case-insensitive."""

    generator: str | None = None
    family: str | None = None
    keys: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    columns_partial: bool = False

    @property
    def partial_components(self) -> tuple[str, ...]:
        """The components no shape could be derived for (plus
        the columns marker when the column list is partial):
        these are excluded from scoring, and named so the
        partiality is stated, not hidden."""
        partial: list[str] = []
        if not self.keys:
            partial.append("keys")
        if not self.inputs:
            partial.append("inputs")
        if not self.columns:
            partial.append("columns")
        elif self.columns_partial:
            partial.append("columns (partial list)")
        if self.generator is None and self.family is None:
            partial.append("generator family")
        return tuple(partial)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generator": self.generator,
            "family": self.family,
            "keys": list(self.keys),
            "inputs": list(self.inputs),
            "columns": list(self.columns),
            "columns_partial": self.columns_partial,
            "partial_components": list(self.partial_components),
        }


def fingerprint_from_record(record: Any) -> ShapeFingerprint:
    """Derive a fingerprint from anything carrying the record
    fields (an engine ``SelectableRecord`` or a corpus
    ``ModelRecord``): the family is the record's paradigm,
    inputs are the declared model inputs plus the declared
    source inputs in their ``name.table`` identity."""
    return ShapeFingerprint(
        generator=getattr(record, "generator", None),
        family=getattr(record, "paradigm", None),
        keys=tuple(getattr(record, "keys", ()) or ()),
        inputs=tuple(getattr(record, "inputs", ()) or ())
        + tuple(getattr(record, "source_inputs", ()) or ()),
        columns=tuple(
            _column_name(column)
            for column in getattr(record, "columns", ()) or ()
        ),
        columns_partial=bool(getattr(record, "columns_partial", False)),
    )


def fingerprint_from_shape(
    *,
    keys: Iterable[str] = (),
    inputs: Iterable[str] = (),
    columns: Iterable[str] = (),
    generator: str | None = None,
    family: str | None = None,
) -> ShapeFingerprint:
    """The target fingerprint a banded search query describes:
    the exact (non-glob) values of its shape qualifiers. A
    ``generator:`` value implies its family through the
    generator registry unless a ``paradigm:`` qualifier states
    the family itself."""
    if family is None and generator is not None:
        family = GENERATOR_PARADIGM.get(generator.casefold())
    return ShapeFingerprint(
        generator=generator,
        family=family,
        keys=tuple(keys),
        inputs=tuple(inputs),
        columns=tuple(columns),
    )


@dataclass(frozen=True)
class Similarity:
    """One candidate scored against a target fingerprint, with
    its explanation. ``gate`` is the key-gate band: 0 when the
    candidate shares every target key (or the target has no
    keys to gate on), 1 when it shares some, 2 when it shares
    none - band 2 always ranks below bands 0 and 1, whatever
    the score."""

    name: str
    score: float
    gate: int
    shares: tuple[str, ...] = ()
    differs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 6),
            "gate": self.gate,
            "shares": list(self.shares),
            "differs": list(self.differs),
        }


def _set_overlap(
    target: tuple[str, ...], candidate: tuple[str, ...]
) -> tuple[list[str], list[str], list[str]]:
    """(shared, missing, extra) for a plain element set,
    compared case-insensitively; shared and missing in target
    order and case, extra in candidate order and case."""
    candidate_folded = {value.casefold() for value in candidate}
    target_folded = {value.casefold() for value in target}
    shared = [value for value in target if value.casefold() in candidate_folded]
    missing = [
        value for value in target if value.casefold() not in candidate_folded
    ]
    extra = [value for value in candidate if value.casefold() not in target_folded]
    return shared, missing, extra


def _input_overlap(
    target: tuple[str, ...], candidate: tuple[str, ...]
) -> tuple[list[str], list[str], list[str]]:
    """Input overlap over normalised identities: a target input
    is shared when any of its forms is among the candidate's
    forms (so ``input:ecom`` in a query matches a model fed by
    ``ecom.raw_customers``, as the qualifier itself does)."""
    candidate_forms: set[str] = set()
    for value in candidate:
        candidate_forms |= _input_forms(value)
    shared: list[str] = []
    missing: list[str] = []
    for value in target:
        if _input_forms(value) & candidate_forms:
            shared.append(value)
        else:
            missing.append(value)
    target_forms: set[str] = set()
    for value in target:
        target_forms |= _input_forms(value)
    extra = [
        value for value in candidate if not (_input_forms(value) & target_forms)
    ]
    return shared, missing, extra


def _coverage_phrase(
    label: str,
    shared: list[str],
    missing: list[str],
    extra: list[str],
    total: int,
    *,
    include_extras: bool,
    shares: list[str],
    differs: list[str],
) -> float:
    """Append one component's shares/differs phrases and
    return its coverage of the target."""
    if shared:
        shares.append(
            f"{label}: {', '.join(shared)} ({len(shared)} of {total})"
        )
    if missing:
        differs.append(f"{label}: missing {', '.join(missing)}")
    if extra and include_extras:
        differs.append(f"{label}: extra {', '.join(extra)}")
    return len(shared) / total if total else 0.0


def compare(
    target: ShapeFingerprint,
    candidate: ShapeFingerprint,
    *,
    name: str = "",
    include_extras: bool = False,
) -> Similarity:
    """Score one candidate fingerprint against the target.

    ``include_extras`` is for the model-anchored form
    (``--similar-to``), where the target is a real model and
    its candidates' extra elements are genuine differences;
    in query banding the target is a practitioner's partial
    description, and elements beyond it are not differences,
    so extras are not reported there."""
    shares: list[str] = []
    differs: list[str] = []
    weighted = 0.0
    weight_total = 0

    shared_keys: list[str] = []
    if target.keys:
        shared_keys, missing, extra = _set_overlap(target.keys, candidate.keys)
        coverage = _coverage_phrase(
            "keys",
            shared_keys,
            missing,
            extra,
            len(target.keys),
            include_extras=include_extras,
            shares=shares,
            differs=differs,
        )
        weighted += _WEIGHTS["keys"] * coverage
        weight_total += _WEIGHTS["keys"]
    if target.inputs:
        shared, missing, extra = _input_overlap(target.inputs, candidate.inputs)
        coverage = _coverage_phrase(
            "inputs",
            shared,
            missing,
            extra,
            len(target.inputs),
            include_extras=include_extras,
            shares=shares,
            differs=differs,
        )
        weighted += _WEIGHTS["inputs"] * coverage
        weight_total += _WEIGHTS["inputs"]
    if target.columns:
        shared, missing, extra = _set_overlap(target.columns, candidate.columns)
        coverage = _coverage_phrase(
            "columns",
            shared,
            missing,
            extra,
            len(target.columns),
            include_extras=include_extras,
            shares=shares,
            differs=differs,
        )
        weighted += _WEIGHTS["columns"] * coverage
        weight_total += _WEIGHTS["columns"]
    if target.generator is not None or target.family is not None:
        same_generator = (
            target.generator is not None
            and candidate.generator is not None
            and candidate.generator.casefold() == target.generator.casefold()
        )
        same_family = (
            target.family is not None
            and candidate.family is not None
            and candidate.family.casefold() == target.family.casefold()
        )
        if same_generator:
            family = candidate.family or target.family
            shares.append(
                f"generator: {candidate.generator} (family {family})"
                if family
                else f"generator: {candidate.generator}"
            )
            coverage = 1.0
        elif same_family:
            shares.append(f"generator family: {target.family}")
            coverage = 1.0
        else:
            target_label = target.family or target.generator
            candidate_label = (
                candidate.family or candidate.generator or "none declared"
            )
            differs.append(
                f"generator family: {candidate_label}, not {target_label}"
            )
            coverage = 0.0
        weighted += _WEIGHTS["family"] * coverage
        weight_total += _WEIGHTS["family"]

    score = weighted / weight_total if weight_total else 0.0
    if not target.keys or len(shared_keys) == len(target.keys):
        gate = 0
    elif shared_keys:
        gate = 1
    else:
        gate = 2
    return Similarity(
        name=name,
        score=score,
        gate=gate,
        shares=tuple(shares),
        differs=tuple(differs),
    )


def rank_similar(
    target: ShapeFingerprint,
    candidates: Iterable[tuple[str, ShapeFingerprint]],
    *,
    include_extras: bool = False,
) -> list[Similarity]:
    """Score and rank candidates against a target: key-gate
    band first, then descending score, then name. Candidates
    sharing nothing (score 0) are not similarities and are
    dropped."""
    scored = [
        compare(target, fingerprint, name=name, include_extras=include_extras)
        for name, fingerprint in candidates
    ]
    scored = [item for item in scored if item.score > 0]
    scored.sort(key=lambda item: (item.gate, -item.score, item.name.casefold()))
    return scored


def find_reference(records: list[Any], name: str) -> Any:
    """The model record ``--similar-to`` anchors on: an exact
    (case-insensitive) model name. Errors teach: an unknown
    name says so, and a non-model record explains that
    similarity is a model shape question."""
    folded = name.casefold()
    for record in records:
        if record.name.casefold() == folded:
            if record.kind != "model":
                raise LoomError(
                    f"--similar-to {name!r}: {record.name} is a "
                    f"{record.kind}, not a model; shape similarity "
                    "compares model shapes (keys, inputs, output "
                    "columns, generator family)"
                )
            return record
    raise LoomError(
        f"--similar-to {name!r}: no record named {name!r} in the "
        "inventory; name a model exactly as search lists it"
    )


def similar_to(
    records: list[Any], name: str
) -> tuple[Any, ShapeFingerprint, list[Similarity], int]:
    """Search by example (plan 7.7): the named model's
    fingerprint as the target, every other model ranked
    against it with extras reported (the target is a real
    model, so a candidate's extra keys, inputs and columns
    are differences). The named model itself is excluded.

    Returns the ranked matches plus the number of candidate
    models omitted for sharing no shape overlap at all
    (``rank_similar`` drops score-0 candidates): the caller
    states that count, so "scored zero" is never
    indistinguishable from "never considered"."""
    reference = find_reference(records, name)
    target = fingerprint_from_record(reference)
    if not (target.keys or target.inputs or target.columns) and (
        target.generator is None and target.family is None
    ):
        raise LoomError(
            f"--similar-to {name!r}: no shape can be derived for "
            f"{reference.name} - it declares no keys, inputs or "
            "output columns and has no generator family, so "
            "there is nothing to compare other models against"
        )
    candidates = [
        (record.name, fingerprint_from_record(record))
        for record in records
        if record.kind == "model" and record.name != reference.name
    ]
    ranked = rank_similar(target, candidates, include_extras=True)
    return reference, target, ranked, len(candidates) - len(ranked)
