"""Practitioner-controlled output layout for generated files.

One resolver serves generation, the drift check, search and
coverage (ADR 0026). An instance may declare ``output_subpath`` - a
relative path template - directly or via its pattern's defaults.
Placeholders are limited to metadata Loom already holds: ``{name}``,
``{generator}``, ``{paradigm}``, ``{owner}`` and ``{tags}`` (all
tags, sorted, one segment each). With no ``output_subpath``, the
resolved subpath is empty and generated files land exactly where
they always have: flat under the configured roots.
"""
from __future__ import annotations

import re
from typing import Any

from loom.errors import LoomError
from loom.generator_registry import GENERATOR_REGISTRY

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ALLOWED_PLACEHOLDERS = frozenset({"name", "generator", "paradigm", "owner", "tags"})


def resolve_contract_enforced(data: dict[str, Any]) -> bool:
    """The `contract_enforced` envelope key, resolved.

    An envelope key like `output_subpath`: carried in pattern
    defaults or instance params (instance wins - expansion merges
    params over defaults and envelope keys are exempt from the
    both-places error), exempt from the generator contracts, never
    passed to renderers. Anything but a literal `true` is off; the
    boolean shape itself is a validation error (see
    inventory_records).
    """
    return data.get("contract_enforced") is True


def resolve_skip_properties_document(data: dict[str, Any]) -> bool:
    """The `skip_properties_document` envelope key, resolved.

    An envelope key like `contract_enforced`: carried in pattern
    defaults or instance params (instance wins), exempt from the
    generator contracts, never passed to renderers. When true,
    generation emits no properties document for the model - the
    practitioner keeps the model's schema YAML - and, where
    the generator emits data tests, no merged data-test document
    either, since the two share one schema file. Anything but a
    literal `true` is off; the boolean shape itself is a
    validation error (see inventory_records).
    """
    return data.get("skip_properties_document") is True


def validate_output_subpath_template(path: str, value: Any) -> list[str]:
    """Structural validation of an ``output_subpath`` template."""
    if not isinstance(value, str):
        return [f"{path}: 'output_subpath' must be a string"]
    errors: list[str] = []
    if "\\" in value:
        errors.append(
            f"{path}: 'output_subpath' must use '/' separators: {value!r}"
        )
    if value.startswith("/"):
        errors.append(f"{path}: 'output_subpath' must be relative: {value!r}")
    for match in _PLACEHOLDER.finditer(value):
        if match.group(1) not in ALLOWED_PLACEHOLDERS:
            errors.append(
                f"{path}: 'output_subpath' has unknown placeholder "
                f"{{{match.group(1)}}}; allowed: "
                + ", ".join("{" + name + "}" for name in sorted(ALLOWED_PLACEHOLDERS))
            )
    remainder = _PLACEHOLDER.sub("", value)
    if "{" in remainder or "}" in remainder:
        errors.append(f"{path}: 'output_subpath' has unbalanced braces: {value!r}")
    # Literal segments are checked now; segments containing
    # placeholders are checked after expansion by the resolver.
    for segment in value.split("/"):
        if "{" in segment or "}" in segment:
            continue
        if segment in ("", ".", "..") or not _SEGMENT.fullmatch(segment):
            errors.append(
                f"{path}: 'output_subpath' segment {segment!r} is not a "
                "safe path segment"
            )
    return errors


def resolve_output_subpath(data: dict[str, Any]) -> str:
    """Expand an entry's ``output_subpath`` template to a safe
    POSIX-style relative directory ("" means flat). Raises LoomError
    on an unresolvable placeholder or an unsafe segment."""
    raw = data.get("output_subpath")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise LoomError("'output_subpath' must be a string")
    label = f"model {data.get('name')!r}"

    def expand(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "name":
            return str(data.get("name", ""))
        if key == "generator":
            return str(data.get("generator", ""))
        if key == "paradigm":
            paradigm = data.get("_paradigm")
            if not isinstance(paradigm, str) or not paradigm:
                raise LoomError(
                    f"{label}: output_subpath placeholder {{paradigm}} "
                    "cannot be resolved (no paradigm recorded)"
                )
            return paradigm
        if key == "owner":
            owner = data.get("owner")
            if not isinstance(owner, str) or not owner:
                raise LoomError(
                    f"{label}: output_subpath placeholder {{owner}} "
                    "cannot be resolved (no owner set)"
                )
            return owner
        if key == "tags":
            tags = data.get("tags")
            if not isinstance(tags, list):
                return ""
            return "/".join(sorted(str(tag) for tag in tags))
        raise LoomError(
            f"{label}: output_subpath has unknown placeholder {{{key}}}"
        )

    expanded = _PLACEHOLDER.sub(expand, raw)
    if "{" in expanded or "}" in expanded:
        raise LoomError(f"{label}: output_subpath has unbalanced braces: {raw!r}")
    segments = [segment for segment in expanded.split("/") if segment != ""]
    for segment in segments:
        if segment in (".", "..") or not _SEGMENT.fullmatch(segment):
            raise LoomError(
                f"{label}: output_subpath segment {segment!r} is not a "
                "safe path segment"
            )
    return "/".join(segments)


def primary_model_root(model_root: str, generator: Any) -> str:
    """The root generation writes a generator's model SQL under,
    remapped onto the configured model root.

    Mirrors the generation plan exactly: scd2_dimension's model file
    is its view (the second output root; the snapshot is not a
    manifest model), and every other generator's SQL lands under its
    last output root - for staging that is
    ``<model-root>/_generated/staging``, not the model root itself.
    """
    spec = GENERATOR_REGISTRY.get(generator) if isinstance(generator, str) else None
    roots = spec.output_roots if spec and spec.output_roots else ("models",)
    raw = roots[1] if generator == "scd2_dimension" and len(roots) > 1 else roots[-1]
    if raw == "models":
        return model_root
    if raw.startswith("models/"):
        return f"{model_root}/{raw.removeprefix('models/')}"
    return raw


def expected_model_path(model_root: str, data: dict[str, Any]) -> str:
    """The project-relative POSIX path a generated model's SQL file
    is expected at, under the configured model root."""
    base = primary_model_root(model_root, data.get("generator"))
    subpath = resolve_output_subpath(data)
    name = data.get("name")
    if subpath:
        return f"{base}/{subpath}/{name}.sql"
    return f"{base}/{name}.sql"
