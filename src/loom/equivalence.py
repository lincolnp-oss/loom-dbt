"""The static equivalence stage (strict form).

The equivalence proof for a conversion is a warehouse act: build
the original and the candidate against identical source data and
compare contents (docs/adoption-playbook.md, Step 6). This stage
runs BEFORE that act, over two practitioner-compiled manifests at
fixed names - ``local/equivalence/original.manifest.json`` and
``local/equivalence/candidate.manifest.json`` - and answers, per
model, exactly one of two verdicts:

- ``proven``: the candidate is statically equivalent to the
  original under the pinned proof conditions; the warehouse
  comparison may be skipped for this model.
- ``inconclusive``: the stage abstains, naming why; the warehouse
  proof decides, exactly as it did before this stage existed.

There is no third verdict and no static disproof: the stage only
ever proves or abstains. Loom never shells to dbt here - the
manifests are practitioner-produced under the playbook protocol
(``dbt compile`` of both projects). Absent manifests mean the
stage was not run, which is a report, not an error.

Soundness rests on the narrowness of the comparison. Compiled
SQL is compared as a token stream under a small normalisation
set (comments, whitespace, case outside literals and quoted
identifiers, trailing semicolons, and the ``__`` declared-
difference exclusion in the candidate's final select list). Any
semantic surface - expression shape, aliasing, quoting style,
select-item order - is decisive: a difference abstains. Identical
normalised text shares its data-dependence and its coercion
behaviour by construction, so no separate detectors for those
classes exist; anything the conditions cannot see (a
run-varying value compiled to a literal, say) differs textually
and lands in the residual inconclusive class.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loom.dialects import canonical_adapter_name
from loom.errors import LoomError
from loom.migration_assessment import (
    _select_lists,
    _split_top_level,
    _strip_comments,
)

EQUIVALENCE_DIR = Path("local") / "equivalence"
ORIGINAL_MANIFEST_NAME = "original.manifest.json"
CANDIDATE_MANIFEST_NAME = "candidate.manifest.json"

# The C6 run-varying token set: whole identifier/function tokens,
# case-folded. A compiled text carrying one embeds a value that
# varies between the two builds, so textual identity cannot prove
# the outputs equal.
_RUN_VARYING_TOKENS = frozenset(
    {
        "current_timestamp",
        "current_date",
        "current_time",
        "localtimestamp",
        "localtime",
        "now",
        "random",
    }
)

_PROVEN_BASIS = (
    "compiled SQL identical after normalisation; every original "
    "column has a candidate counterpart; candidate additions are "
    "__-prefixed declared differences only"
)


def static_stage_report(project_dir: Path) -> dict[str, Any]:
    """Run the static equivalence stage for one project and
    return the proof record (the report the `prove` command
    renders). The stage is not run - a report, not an error -
    when either fixed-name manifest is absent."""
    original_path = project_dir / EQUIVALENCE_DIR / ORIGINAL_MANIFEST_NAME
    candidate_path = project_dir / EQUIVALENCE_DIR / CANDIDATE_MANIFEST_NAME
    # The fixed names are a protocol constant, rendered in
    # canonical forward-slash form on every platform (the OS
    # separator would leak into the report - and the proof
    # record - on Windows).
    missing = [
        (EQUIVALENCE_DIR / name).as_posix()
        for name, path in (
            (ORIGINAL_MANIFEST_NAME, original_path),
            (CANDIDATE_MANIFEST_NAME, candidate_path),
        )
        if not path.is_file()
    ]
    if missing:
        return {"stage": "not_run", "missing": missing, "models": []}
    original = _load_manifest(original_path)
    candidate = _load_manifest(candidate_path)
    return _compare_manifests(original, candidate)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LoomError(f"{path}: cannot read the manifest: {error}") from error
    if not isinstance(document, dict) or not isinstance(
        document.get("nodes"), dict
    ):
        raise LoomError(
            f"{path}: not a dbt manifest (a top-level object with a "
            "'nodes' mapping is required)"
        )
    return document


def _provenance(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "project_name": metadata.get("project_name"),
        "adapter": metadata.get("adapter_type"),
        "generated_at": metadata.get("generated_at"),
    }


def _subjects(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The equivalence subjects of one manifest: its model and
    snapshot nodes, by name."""
    subjects: dict[str, dict[str, Any]] = {}
    nodes = document.get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            if node.get("resource_type") not in ("model", "snapshot"):
                continue
            name = node.get("name")
            if isinstance(name, str):
                subjects[name] = node
    return subjects


def _compare_manifests(
    original: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    original_subjects = _subjects(original)
    candidate_subjects = _subjects(candidate)
    original_provenance = _provenance(original)
    candidate_provenance = _provenance(candidate)
    adapter_mismatch = canonical_adapter_name(
        original_provenance["adapter"]
    ) != canonical_adapter_name(candidate_provenance["adapter"])
    models: list[dict[str, Any]] = []
    documentation_gaps: dict[str, bool] = {}
    for name in sorted(original_subjects):
        if adapter_mismatch:
            models.append(
                {
                    "name": name,
                    "verdict": "inconclusive",
                    "basis": (
                        "adapter mismatch: the original manifest "
                        f"records adapter {original_provenance['adapter']!r} "
                        "and the candidate manifest records "
                        f"{candidate_provenance['adapter']!r}; cross-adapter "
                        "proof is never attempted"
                    ),
                }
            )
            documentation_gaps[name] = False
            continue
        record, gap = _verdict(
            name,
            original_subjects[name],
            candidate_subjects.get(name),
        )
        models.append(record)
        documentation_gaps[name] = gap
    non_subjects = sorted(set(candidate_subjects) - set(original_subjects))
    proven = sum(1 for model in models if model["verdict"] == "proven")
    # The documentation-coverage note (QOL B-2): the counterpart
    # check sees the original's DOCUMENTED columns only, so the
    # stage's reach on a partially documented original is
    # limited in ways the per-model bases alone used to
    # misdescribe. A subject counts as partially documented when
    # its original documents no columns at all, or when its
    # verdict turned on a candidate column with no documented
    # counterpart (the gap flag from _verdict).
    documentation_partial = [
        model["name"]
        for model in models
        if documentation_gaps[model["name"]]
        or not _columns(original_subjects[model["name"]])
    ]
    return {
        "stage": "run",
        "original": original_provenance,
        "candidate": candidate_provenance,
        "models": models,
        "non_subjects": non_subjects,
        "documentation_partial": documentation_partial,
        "summary": {
            "subjects": len(models),
            "proven": proven,
            "inconclusive": len(models) - proven,
        },
    }


def _columns(node: dict[str, Any]) -> set[str]:
    columns = node.get("columns")
    if not isinstance(columns, dict):
        return set()
    return {name for name in columns if isinstance(name, str)}


def _materialized(node: dict[str, Any]) -> str | None:
    config = node.get("config")
    if isinstance(config, dict):
        materialized = config.get("materialized")
        if isinstance(materialized, str):
            return materialized
    return None


# The C7 basis for a candidate column with no counterpart among
# the original's DOCUMENTED columns (QOL B-2): _verdict flags
# the documentation gap by this prefix so the report's
# documentation-coverage note can name the affected models.
_NO_DOCUMENTED_COUNTERPART_PREFIX = (
    "no documented counterpart: candidate column "
)


def _verdict(
    name: str,
    original: dict[str, Any],
    candidate: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """One model's verdict record, plus whether the verdict
    turned on a documentation gap in the original."""
    record = _verdict_record(name, original, candidate)
    return record, record["basis"].startswith(
        _NO_DOCUMENTED_COUNTERPART_PREFIX
    )


def _verdict_record(
    name: str,
    original: dict[str, Any],
    candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    def inconclusive(basis: str) -> dict[str, Any]:
        return {"name": name, "verdict": "inconclusive", "basis": basis}

    # C2 - present in both manifests.
    if candidate is None:
        return inconclusive(
            "absent from the candidate manifest: the model has no "
            "candidate counterpart to prove against"
        )
    # C3 - stateful resources abstain by construction.
    for label, node in (("original", original), ("candidate", candidate)):
        if node.get("resource_type") == "snapshot":
            return inconclusive(
                f"stateful: the {label} is a snapshot; snapshots are "
                "never statically proven"
            )
        if _materialized(node) == "incremental":
            return inconclusive(
                f"stateful: the {label} is incremental "
                "(materialized='incremental'); incremental models are "
                "never statically proven"
            )
    # C4 - compiled code on both sides.
    original_sql = original.get("compiled_code")
    candidate_sql = candidate.get("compiled_code")
    for label, sql in (("original", original_sql), ("candidate", candidate_sql)):
        if not isinstance(sql, str) or not sql.strip():
            return inconclusive(
                f"no compiled code: the {label} node carries no "
                "compiled_code (the protocol requires `dbt compile`; "
                "a parse-produced manifest cannot feed this stage)"
            )
    assert isinstance(original_sql, str) and isinstance(candidate_sql, str)
    # C5 - unresolved Jinja.
    for label, sql in (("original", original_sql), ("candidate", candidate_sql)):
        if "{{" in sql or "{%" in sql:
            return inconclusive(
                f"unresolved Jinja in the {label} compiled code: the "
                "manifest was not produced by a full compile"
            )
    # C7 - column counterparts, checked before the text so the
    # basis names the column rather than the residual class.
    original_columns = _columns(original)
    candidate_columns = _columns(candidate)
    for column in sorted(original_columns - candidate_columns):
        return inconclusive(
            f"missing counterpart: original column {column!r} has no "
            "candidate counterpart (declared mappings are not "
            "supported; the warehouse proof decides)"
        )
    for column in sorted(candidate_columns - original_columns):
        if not column.startswith("__"):
            return inconclusive(
                f"{_NO_DOCUMENTED_COUNTERPART_PREFIX}{column!r} has "
                "no counterpart among the original's documented "
                "columns - the original's documentation may be "
                "incomplete, and the counterpart check sees "
                "documented columns only; the column is not a "
                "__-prefixed declared difference, so the stage "
                "cannot admit it (the warehouse proof decides)"
            )
    # The candidate's __ exclusion (the declared-difference rule),
    # applied before both remaining checks: C6 must not see the
    # generated audit item, and C8 compares without it.
    reduced_candidate, exclusion_failed = _exclude_declared_differences(
        candidate_sql, original_columns
    )
    if exclusion_failed:
        return inconclusive(
            "the candidate carries apparent __-prefixed declared "
            "differences but no final select list could be located, "
            "so the declared-difference exclusion cannot be applied "
            "safely"
        )
    # C6 - run-varying tokens, on the post-exclusion texts.
    for label, sql in (
        ("original", original_sql),
        ("candidate", reduced_candidate),
    ):
        varying = sorted(set(_tokens(sql)) & _RUN_VARYING_TOKENS)
        if varying:
            return inconclusive(
                f"run-varying value: the {label} compiled code embeds "
                f"{varying[0]!r}, whose value varies between builds; "
                "textual identity cannot prove the outputs equal"
            )
    # C8 - normalised text identity.
    if _tokens(original_sql) != _tokens(reduced_candidate):
        return inconclusive(
            "compiled SQL differs after normalisation (the residual "
            "class: any semantic surface - expression shape, "
            "aliasing, quoting style, select-item order - is "
            "decisive by design)"
        )
    return {"name": name, "verdict": "proven", "basis": _PROVEN_BASIS}


_ALIAS_AT_END = re.compile(
    r"\bas\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*$", re.IGNORECASE
)
_PLAIN_COLUMN_REF = re.compile(
    r"^(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?\.)*\"?([A-Za-z_][A-Za-z0-9_]*)\"?$"
)


def _item_output_name(item: str) -> str | None:
    """The output name of one final-select-list item: its AS
    alias, or the column name when the item is a plain column
    reference. Anything else has no mechanically certain output
    name and returns None - such an item is never excluded, which
    degrades safely to inconclusive at C8."""
    alias = _ALIAS_AT_END.search(item.strip())
    if alias is not None:
        return alias.group(1)
    plain = _PLAIN_COLUMN_REF.match(item.strip())
    if plain is not None:
        return plain.group(1)
    return None


def _exclude_declared_differences(
    sql: str, original_columns: set[str]
) -> tuple[str, bool]:
    """Remove the candidate's declared-difference select items
    from its final select list: items whose output name is
    ``__``-prefixed and not one of the original's columns.
    Returns (possibly reduced SQL, failed).

    The differences are read from the select list itself, not
    from the candidate's documented columns: a generated model's
    ``__`` audit columns are routinely undocumented in the
    candidate manifest (the playbook's recipe keeps the project's
    own schema YAML and deletes Loom's generated properties
    document), so a manifest-derived difference set would miss
    exactly the items the rule exists to exclude. C7's documented-
    column gate still applies independently. Failed only when no
    select list can be located while the text carries an apparent
    ``__`` item - the safe direction, since a failed exclusion
    degrades the model to inconclusive, never to a proof."""
    cleaned = _strip_comments(sql)
    lists = _select_lists(cleaned)
    if not lists:
        return sql, bool(re.search(r"\bas\s+\"?__", cleaned, re.IGNORECASE))
    final_list = lists[-1]
    items = _split_top_level(final_list)
    kept: list[str] = []
    dropped = False
    for item in items:
        output = _item_output_name(item)
        if (
            output is not None
            and output.startswith("__")
            and output not in original_columns
        ):
            dropped = True
            continue
        kept.append(item)
    if not dropped:
        return sql, False
    position = cleaned.rfind(final_list)
    if position < 0:
        return sql, True
    leading = final_list[: len(final_list) - len(final_list.lstrip())]
    trailing = final_list[len(final_list.rstrip()) :]
    reduced = (
        cleaned[:position]
        + leading
        + ", ".join(kept)
        + trailing
        + cleaned[position + len(final_list) :]
    )
    return reduced, False


def _tokens(sql: str) -> list[str]:
    """Tokenise compiled SQL under the pinned normalisation set:
    comments never tokenise; whitespace only separates; unquoted
    words fold to lower case; string literals and quoted
    identifiers are single verbatim tokens; trailing statement
    semicolons are dropped."""
    tokens: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char == "'":
            end = index + 1
            while end < length:
                if sql[end] == "'":
                    if end + 1 < length and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            tokens.append(sql[index:end])
            index = end
            continue
        if char in "\"`":
            end = sql.find(char, index + 1)
            end = length if end < 0 else end + 1
            tokens.append(sql[index:end])
            index = end
            continue
        if char == "[":
            end = sql.find("]", index + 1)
            end = length if end < 0 else end + 1
            tokens.append(sql[index:end])
            index = end
            continue
        if char.isalnum() or char == "_":
            end = index
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(sql[index:end].lower())
            index = end
            continue
        tokens.append(char)
        index += 1
    while tokens and tokens[-1] == ";":
        tokens.pop()
    return tokens
