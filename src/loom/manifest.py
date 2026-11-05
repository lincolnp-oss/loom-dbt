from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loom.errors import LoomError
from loom.project_files import _configured_paths, _model_yaml_files, _project_config

# Manifest schema versions Loom reads: v10–v12 are the schemas emitted by
# the dbt Core range covered by Loom's compatibility evidence (1.6–1.12).
# A manifest from a newer schema is rejected with a clear error instead
# of being misread. Manifests without a dbt_schema_version (hand-built
# fixtures, older tooling) are still read.
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({10, 11, 12})

_SCHEMA_VERSION_PATTERN = re.compile(r"v(\d+)(?:\.json)?$")


def _manifest_not_found(path: Path) -> LoomError:
    """The remedy message for a manifest that does not exist.

    The remedy names `dbt parse` - the manifest the
    migration assessment consumes is a parse-produced one (fit
    calibration assumes it; a build-produced manifest is
    declared and warned about on the report). `dbt build`
    remains fine for other purposes; it is simply not the
    manifest-producing command this remedy should steer to."""
    return LoomError(
        f"{path}: dbt manifest not found; run `dbt parse` in "
        "the project first (or supply a manifest path)"
    )


# Every stale-manifest error names the remedy alongside the
# stale file, in the same spirit as the not-found remedy: a
# practitioner whose files moved after parsing is told how the
# manifest comes to be current again, not only that it is not.
_STALE_REMEDY = "re-run `dbt parse` to refresh the manifest"

# The stale error is a small report, not a
# sentence - project-relative paths, the changed files as a list
# (folded past ten, the shared threshold), and the remedy on its
# own line. The facts carried are unchanged: the manifest, every
# stale file, and the remedy.
_STALE_FOLD_LIMIT = 10


def _display_path(path: Path, root: Path | None) -> str:
    """``path`` relative to the project root when it sits under
    it (the form a practitioner navigates by), else as given."""
    if root is not None:
        try:
            return path.resolve(strict=False).relative_to(
                root.resolve(strict=False)
            ).as_posix()
        except ValueError:
            pass
    return str(path)


def _stale_error(
    manifest: Path,
    problems: list[tuple[str, str]],
    *,
    root: Path | None = None,
) -> LoomError:
    """Build the stale-manifest error from (file, reason) pairs.

    Reasons keep the established vocabulary: ``changed after
    parsing``, ``missing``, ``does not match manifest raw_code``,
    ``absent from the manifest``."""
    count = len(problems)
    noun = "file" if count == 1 else "files"
    lines = [
        f"{_display_path(manifest, root)}: dbt manifest is stale "
        f"- {count} changed {noun}:"
    ]
    for display, reason in problems[:_STALE_FOLD_LIMIT]:
        lines.append(f"  {display} ({reason})")
    if count > _STALE_FOLD_LIMIT:
        lines.append(f"  … and {count - _STALE_FOLD_LIMIT} more changed files")
    lines.append(f"  Remedy: {_STALE_REMEDY}")
    return LoomError("\n".join(lines))


def _freshness_problem(
    source: Path, timestamp: datetime, *, root: Path | None = None
) -> tuple[str, str] | None:
    """The staleness of one file as a (display, reason) problem,
    or ``None`` when the file is current. OSError still raises -
    an unreadable file is not staleness."""
    try:
        if not source.is_file():
            return (_display_path(source, root), "missing")
        if source.stat().st_mtime > timestamp.timestamp():
            return (_display_path(source, root), "changed after parsing")
    except OSError as error:
        raise LoomError(
            f"{source}: cannot check manifest freshness: {error}"
        ) from error
    return None


def _os_error_detail(error: OSError) -> str:
    """An OSError's message without the raw "[Errno N]" prefix."""
    return error.strerror or str(error)


def check_manifest_schema_version(path: Path, metadata: dict[str, Any]) -> None:
    """Reject manifests whose declared schema version Loom does not read."""
    raw = metadata.get("dbt_schema_version")
    if raw is None:
        return
    match = (
        _SCHEMA_VERSION_PATTERN.search(raw.strip())
        if isinstance(raw, str)
        else None
    )
    if match is None:
        raise LoomError(
            f"{path}: metadata.dbt_schema_version {raw!r} is not a recognised "
            "dbt manifest schema version"
        )
    version = int(match.group(1))
    if version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        supported = ", ".join(
            f"v{v}" for v in sorted(SUPPORTED_MANIFEST_SCHEMA_VERSIONS)
        )
        raise LoomError(
            f"{path}: unsupported dbt manifest schema version 'v{version}'; "
            f"Loom supports manifest schemas {supported} "
            "(dbt Core 1.6-1.12). Regenerate the manifest with a supported "
            "dbt version."
        )


def generated_at(path: Path, document: dict[str, Any]) -> datetime:
    value = document["metadata"].get("generated_at")
    try:
        if not isinstance(value, str):
            raise ValueError("missing timestamp")
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("missing timezone")
        return timestamp.astimezone(timezone.utc)
    except ValueError as error:
        raise LoomError(
            f"{path}: metadata.generated_at must be a timezone-aware timestamp"
        ) from error


def read_manifest(
    root: Path,
    manifest_path: str | Path | None,
    *,
    sections: tuple[str, ...],
    project_local: bool = False,
) -> tuple[Path, dict[str, Any]]:
    path = (
        Path(manifest_path)
        if manifest_path is not None
        else Path("target/manifest.json")
    )
    if not path.is_absolute():
        path = root / path
    # Compare resolved paths against the resolved root. A
    # caller may legitimately pass a relative root (``--project-dir.``);
    # checking a resolved manifest path against an unresolved root
    # falsely reports the manifest as outside the project.
    resolved_root = Path(root).expanduser().resolve()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        # A missing manifest is the first error a
        # migrating practitioner hits, and the raw OSError leaked an
        # "[Errno 2]" string naming the first missing *directory*.
        # Name the manifest file itself and say how it comes to
        # exist instead.
        raise _manifest_not_found(path) from None
    except OSError as error:
        raise LoomError(
            f"{path}: cannot read dbt manifest: {_os_error_detail(error)}"
        ) from error
    if project_local and not resolved.is_relative_to(resolved_root):
        raise LoomError(f"{path}: expected an existing file inside {root}")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _manifest_not_found(path) from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        detail = (
            _os_error_detail(error) if isinstance(error, OSError) else error
        )
        raise LoomError(f"{path}: cannot read dbt manifest: {detail}") from error
    if not isinstance(document, dict):
        raise LoomError(f"{path}: dbt manifest root must be a JSON object")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(
        metadata.get("project_name"), str
    ):
        raise LoomError(f"{path}: metadata.project_name is required")
    if not metadata["project_name"]:
        raise LoomError(f"{path}: metadata.project_name must not be empty")
    check_manifest_schema_version(path, metadata)
    for section in sections:
        if not isinstance(document.get(section), dict):
            raise LoomError(f"{path}: {section} must be a JSON object")
    timestamp = generated_at(path, document)
    config = _project_config(root)
    if "name" in config and config["name"] != metadata["project_name"]:
        raise LoomError(
            f"{path}: manifest project {metadata['project_name']!r} does not match "
            f"dbt project {config['name']!r}"
        )
    return path, document


def check_file_timestamp(
    manifest: Path,
    source: Path,
    timestamp: datetime,
    *,
    required: bool = True,
    root: Path | None = None,
) -> None:
    """Fail-fast freshness for a single file (the source-file
    check in ``source_structure``). The multi-file walk in
    ``validate_model_freshness`` collects instead, so one error
    can name every stale file."""
    problem = _freshness_problem(source, timestamp, root=root)
    if problem is None:
        return
    if problem[1] == "missing" and not required:
        return
    raise _stale_error(manifest, [problem], root=root)


def validate_model_freshness(
    root: Path, manifest: Path, document: dict[str, Any]
) -> None:
    timestamp = generated_at(manifest, document)
    project_name = document["metadata"]["project_name"]
    paths: set[Path] = set()
    # Staleness is collected across the whole walk and
    # raised as one error naming every stale file (design E1);
    # the walk's other errors (malformed nodes, unreadable files)
    # still raise at the point they are found. Each file earns at
    # most one problem - the timestamp verdict precedes the
    # content comparison, as the fail-fast order did before.
    problems: list[tuple[str, str]] = []

    def collect(source: Path) -> tuple[str, str] | None:
        problem = _freshness_problem(source, timestamp, root=root)
        if problem is not None and problem not in problems:
            problems.append(problem)
        return problem

    for unique_id, node in document["nodes"].items():
        if not isinstance(node, dict):
            raise LoomError(f"{manifest}: node {unique_id!r} must be a JSON object")
        if (
            node.get("resource_type") != "model"
            or node.get("package_name") != project_name
        ):
            continue
        if not isinstance(node.get("name"), str) or not node["name"]:
            raise LoomError(
                f"{manifest}: project-owned model {unique_id!r} has no valid name"
            )
        original = node.get("original_file_path")
        if not isinstance(original, str) or not original:
            raise LoomError(
                f"{manifest}: project model {unique_id!r} has no original_file_path"
            )
        source = root / original
        try:
            resolved = source.resolve(strict=False)
            if not resolved.is_relative_to(root):
                raise LoomError(
                    f"{manifest}: project model {unique_id!r} is outside the project"
                )
            problem = collect(source)
            if problem is not None:
                # Already counted stale; the content comparison
                # would only restate it. The path still counts as
                # a manifest model file for the absence scan.
                paths.add(resolved)
                continue
            raw_code = node.get("raw_code")
            if not isinstance(raw_code, str):
                raise LoomError(
                    f"{manifest}: project model {unique_id!r} has no valid raw_code"
                )
            actual = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise LoomError(
                f"{source}: cannot check manifest model content: {error}"
            ) from error
        normalize = (
            lambda text: text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        )
        if normalize(actual) != normalize(raw_code):
            problem = (
                _display_path(source, root),
                "does not match manifest raw_code",
            )
            if problem not in problems:
                problems.append(problem)
        paths.add(resolved)

    disabled = document.get("disabled", {})
    if not isinstance(disabled, dict):
        raise LoomError(f"{manifest}: disabled must be a JSON object")
    for values in disabled.values():
        if not isinstance(values, list):
            raise LoomError(f"{manifest}: disabled resources must be lists")
        for node in values:
            if not isinstance(node, dict):
                raise LoomError(f"{manifest}: disabled resources must be JSON objects")
            if (
                node.get("resource_type") == "model"
                and node.get("package_name") == project_name
            ):
                original = node.get("original_file_path")
                if not isinstance(original, str) or not original:
                    raise LoomError(
                        f"{manifest}: disabled model has no original_file_path"
                    )
                source = (root / original).resolve()
                if not source.is_relative_to(root):
                    raise LoomError(
                        f"{manifest}: disabled model is outside the project"
                    )
                collect(source)
                paths.add(source)

    config = _project_config(root)
    model_paths = _configured_paths(config, "model")
    inputs = _model_yaml_files(
        root, model_paths, suffixes=frozenset({".sql", ".py", ".yml", ".yaml"})
    )
    for source in inputs:
        if source.suffix.lower() in {".sql", ".py"} and source.resolve() not in paths:
            problem = (
                source.relative_to(root).as_posix(),
                "absent from the manifest",
            )
            if problem not in problems:
                problems.append(problem)
            continue
        collect(source)
    for source in _model_yaml_files(
        root,
        _configured_paths(config, "macro"),
        suffixes=frozenset({".sql"}),
        path_label="macro",
        file_label="dbt macro file",
    ):
        collect(source)
    # The project file itself (QOL M4): its freshness used to be
    # a fail-fast check inside read_manifest, below every
    # caller's tolerance switch, so even search — whose contract
    # is to answer and label the manifest's age — refused over a
    # routine config edit. It is collected here instead: guarded
    # commands keep refusing over it, naming it alongside every
    # other stale file, while a tolerant caller simply never
    # runs this walk. A missing project file stays tolerated,
    # as the fail-fast check's required=False had it.
    project_problem = _freshness_problem(
        root / "dbt_project.yml", timestamp, root=root
    )
    if (
        project_problem is not None
        and project_problem[1] != "missing"
        and project_problem not in problems
    ):
        problems.append(project_problem)
    if problems:
        raise _stale_error(manifest, problems, root=root)


# --- Macro verification (Tier 1) -------------------------
#
# The manifest's `macros` section keys its entries
# macro.<package>.<name>; each entry carries the ordered
# `arguments` (name/type/description) and the full `macro_sql`.
# dbt does NOT record argument defaults in the arguments list -
# they exist only in the macro_sql signature - so the index
# derives each argument's has_default from the signature, and a
# signature whose defaults cannot be read is treated as
# all-required (verification stricter, never looser). dbt also
# leaves the arguments list EMPTY for macros not documented in
# properties YAML, so for those the index derives the whole
# parameter list - names and defaults - from the signature
# (M7): the signature is the only truthful source there, and a
# signature that cannot be parsed falls back to the empty list
# rather than fabricating parameters.


def manifest_macro_index(
    document: dict[str, Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Index the manifest's macros by (package_name, name).

    Each value is the ordered argument list as
    ``{"name": str, "has_default": bool}`` entries, in declared
    signature order."""
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    macros = document.get("macros")
    if not isinstance(macros, dict):
        return index
    for entry in macros.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        package = entry.get("package_name")
        if not isinstance(name, str) or not isinstance(package, str):
            continue
        defaults = _macro_signature_defaults(entry.get("macro_sql"), name)
        arguments: list[dict[str, Any]] = []
        declared = entry.get("arguments")
        if isinstance(declared, list):
            for argument in declared:
                if isinstance(argument, dict) and isinstance(
                    argument.get("name"), str
                ):
                    arguments.append(
                        {
                            "name": argument["name"],
                            "has_default": argument["name"] in defaults,
                        }
                    )
        if not arguments:
            # Undocumented macro: dbt recorded no arguments, so
            # the signature itself is the parameter list.
            arguments = _macro_signature_parameters(
                entry.get("macro_sql"), name
            )
        index[(package, name)] = arguments
    return index


def _macro_signature_param_text(macro_sql: Any, name: str) -> str | None:
    """The raw parameter-list text of a macro's
    ``{% macro name(...) %}`` signature, read from macro_sql:
    locate the signature, then scan to its closing paren
    honouring nesting and quotes, so a default containing
    parens cannot truncate the list. None when the signature
    cannot be located or read."""
    if not isinstance(macro_sql, str):
        return None
    match = re.search(
        r"\{%-?\s*macro\s+" + re.escape(name) + r"\s*\(", macro_sql
    )
    if match is None:
        return None
    depth = 1
    quote: str | None = None
    index = match.end()
    while index < len(macro_sql) and depth:
        char = macro_sql[index]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    if depth:
        return None
    return macro_sql[match.end() : index - 1]


def _macro_signature_defaults(macro_sql: Any, name: str) -> set[str]:
    """The parameter names carrying a default in a macro's
    signature, read from macro_sql. Empty when the signature
    cannot be located or read."""
    params = _macro_signature_param_text(macro_sql, name)
    if params is None:
        return set()
    defaults: set[str] = set()
    for param in _split_macro_params(params):
        head, separator, _ = param.partition("=")
        if separator:
            candidate = head.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
                defaults.add(candidate)
    return defaults


def _macro_signature_parameters(
    macro_sql: Any, name: str
) -> list[dict[str, Any]]:
    """The macro's ordered parameters - names and which carry
    a default - read from the macro_sql signature itself, for
    macros whose manifest arguments list is empty (dbt records
    arguments only for documented macros). Every parameter must
    read as a bare identifier (optionally with a default): a
    signature containing anything else is unparseable for this
    purpose and yields the empty list - the pre-derivation
    behaviour - rather than a fabricated parameter."""
    params = _macro_signature_param_text(macro_sql, name)
    if params is None:
        return []
    parameters: list[dict[str, Any]] = []
    for param in _split_macro_params(params):
        param = param.strip()
        if not param:
            continue
        head, separator, _ = param.partition("=")
        candidate = head.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
            return []
        parameters.append(
            {"name": candidate, "has_default": bool(separator)}
        )
    return parameters


def _split_macro_params(text: str) -> list[str]:
    """Split a macro signature's parameter list on top-level
    commas (commas inside nested brackets or quotes do not
    split)."""
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
    return parts


def verify_macro_reference(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    reference: str,
    supplied: int,
    *,
    project_name: str | None,
) -> str | None:
    """Verify one staging macro reference against the manifest
    macro index: existence, then arity against the ordered
    arguments including defaults (a call is arity-valid when
    required ≤ supplied ≤ total, where required counts the
    arguments without defaults). A bare reference resolves
    against the manifest's own project package. Returns None
    when verified, else a description of the problem."""
    if "." in reference:
        package, _, name = reference.partition(".")
    else:
        package, name = project_name, reference
    if package is None:
        return (
            f"macro {reference!r} cannot be resolved: the manifest "
            "names no project package"
        )
    arguments = index.get((package, name))
    if arguments is None:
        return f"macro {reference!r} is not defined in the manifest"
    required = sum(1 for argument in arguments if not argument["has_default"])
    total = len(arguments)
    if not required <= supplied <= total:
        span = (
            f"{total} argument(s)"
            if required == total
            else f"between {required} and {total} arguments"
        )
        return (
            f"macro {reference!r} takes {span} per the manifest "
            f"signature, but the call supplies {supplied}"
        )
    return None
