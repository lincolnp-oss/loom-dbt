"""Authoritative per-generator parameter contracts.

One contract per generator: the accepted parameter set, which parameters
are required, and each parameter's top-level type. Validation consumes
these contracts to reject unknown parameters with errors that name
the accepted keys, to report missing required parameters, and to catch
type mismatches early - replacing the previously implicit contract that
was scattered across renderer ``params.get`` calls and per-generator
validator branches (which is how silently-ignored misspellings shipped).

The contract covers top-level instance parameters. Nested shapes with
their own semantics (staging ``columns`` items, ``aggregation``
internals, ``joins`` items) keep their dedicated validators in
``inventory.py``; the two nested mappings for completed inline
sources (``inline_source``, ``source_ref``) have their accepted keys
declared here too.

Types are deliberately coarse (``string`` / ``list`` / ``mapping``):
enough to catch the common authoring errors - a scalar where a list
belongs fails at validate time instead of deep inside a renderer.
"""
from __future__ import annotations

from dataclasses import dataclass

from loom.errors import LoomError
from loom.generator_registry import GENERATOR_REGISTRY


@dataclass(frozen=True)
class ParamContract:
    """One accepted parameter of a generator."""

    name: str
    kind: str  # "string" | "list" | "mapping" | "string_or_mapping"
    required: bool = False


# Parameters every generator accepts. `description` feeds the generated
# model properties and search; `column_docs` and
# `column_types` declare per-column descriptions and generic data types
# for the same properties document - their contents are validated in
# inventory.py against the model's derivable output columns.
# `description` and `owner` are required only where the pre-contract
# validators required them (staging, hub, link, SCD2); `tags`,
# `column_docs` and `column_types` are optional everywhere.
# `tags` is descriptive instance metadata that the `tag:` search
# qualifier reads generically across instances and registrations;
# renderers ignore it.
COMMON_PARAMS: tuple[ParamContract, ...] = (
    ParamContract("description", "string"),
    ParamContract("owner", "string"),
    ParamContract("tags", "list"),
    ParamContract("column_docs", "mapping"),
    ParamContract("column_types", "mapping"),
    # Practitioner-declared generic (data) tests, carried
    # verbatim into the model's generated properties document
    # (carriage): Loom hosts the declaration because
    # dbt's single-description rule leaves it nowhere else to
    # live; it never originates, validates or infers the tests.
    # Optional on EVERY generator - staging, bronze and SCD2
    # models gain not_null/unique carriage by the same key.
    ParamContract("generic_tests", "list"),
)

# Keys that may appear on an expanded instance record without being
# generator parameters: the document envelope and expansion artefacts.
ENVELOPE_KEYS = frozenset(
    {
        "name",
        "generator",
        "_paradigm",
        # Layout metadata consumed by generation (ADR 0026),
        # never a renderer parameter.
        "output_subpath",
        # Opt-in enforced-contract flag consumed by validation and
        # generation (ADR 0032), never a renderer
        # parameter. Envelope keys may be set in pattern defaults
        # AND overridden per instance (instance wins) - the one
        # mechanism that can express a pattern-level default with a
        # per-model exception.
        "contract_enforced",
        # Opt-out of the generated properties document:
        # the practitioner keeps the model's schema YAML, so
        # generation emits no competing properties entry - and,
        # where the generator emits data tests, no merged
        # data-test document either. Consumed by generation and
        # the coverage check, never a renderer parameter.
        "skip_properties_document",
    }
)


def _contract(
    *params: ParamContract, common_required: bool = False
) -> tuple[ParamContract, ...]:
    # Only description/owner were ever required (staging, hub, link,
    # SCD2); descriptive metadata (tags, column_docs, column_types)
    # stays optional everywhere.
    common = tuple(
        ParamContract(
            p.name,
            p.kind,
            required=common_required and p.name in ("description", "owner"),
        )
        for p in COMMON_PARAMS
    )
    return common + params


def _s(name: str, required: bool = False) -> ParamContract:
    return ParamContract(name, "string", required)


def _l(name: str, required: bool = False) -> ParamContract:
    return ParamContract(name, "list", required)


def _m(name: str, required: bool = False) -> ParamContract:
    return ParamContract(name, "mapping", required)


def _sm(name: str, required: bool = False) -> ParamContract:
    return ParamContract(name, "string_or_mapping", required)


GENERATOR_CONTRACTS: dict[str, tuple[ParamContract, ...]] = {
    "staging": _contract(
        _m("source_ref"),
        _m("inline_source"),
        _l("columns", required=True),
        _s("filter"),
        _l("unit_tests"),
        common_required=True,
    ),
    "hub": _contract(
        _l("input_models", required=True),
        _s("business_key", required=True),
        _s("retention"),
        # One provenance parameter, two forms: a string labels
        # every input uniformly; a mapping keyed by input model
        # labels each feed (parameter consolidation, 2026-11-04).
        _sm("record_source"),
        _l("unit_tests"),
        common_required=True,
    ),
    "link": _contract(
        _l("input_models", required=True),
        _l("endpoints", required=True),
        _s("variant"),
        _s("dependent_child_key"),
        _l("payload_columns"),
        _m("incremental"),
        _l("unit_tests"),
        common_required=True,
    ),
    "satellite": _contract(
        _m("parent", required=True),
        _l("input_models", required=True),
        _s("record_source", required=True),
        _s("variant"),
        _l("attribute_columns"),
        _s("driving_key"),
        _s("subsequence_key"),
        _l("unit_tests"),
        common_required=True,
    ),
    "pit": _contract(
        _m("parent", required=True),
        _l("satellites", required=True),
        _l("unit_tests"),
        common_required=True,
    ),
    "reference_hub": _contract(
        _l("input_models", required=True),
        _s("business_key", required=True),
        _s("record_source", required=True),
        _l("unit_tests"),
        common_required=True,
    ),
    "reference_table": _contract(
        _s("reference_hub", required=True),
        _l("satellites", required=True),
        _s("business_key", required=True),
        _l("attribute_columns", required=True),
        _s("variant"),
        _s("snapshot_timestamp"),
        _l("unit_tests"),
        common_required=True,
    ),
    "bridge": _contract(
        _m("parent", required=True),
        _l("path", required=True),
        _s("weighting"),
        _l("unit_tests"),
        common_required=True,
    ),
    "scd2_dimension": _contract(
        _l("input_models", required=True),
        _s("business_key", required=True),
        _l("tracked_columns", required=True),
        _s("variant"),
        _l("previous_value_columns"),
        _s("filter"),
        common_required=True,
    ),
    "fact": _contract(
        _l("input_models", required=True),
        _l("dimension_keys", required=True),
        _l("measure_columns", required=True),
        _s("variant"),
        _s("period_key"),
        _l("milestone_keys"),
        _m("incremental"),
        _s("filter"),
        _l("unit_tests"),
    ),
    "dimension": _contract(
        _l("input_models", required=True),
        _s("natural_key", required=True),
        _s("surrogate_key", required=True),
        _l("attribute_columns", required=True),
        _m("incremental"),
        _s("filter"),
        _l("unit_tests"),
    ),
    "bronze": _contract(
        _m("inline_source", required=True),
        _m("incremental"),
        _l("unit_tests"),
    ),
    "silver": _contract(
        _l("input_models", required=True),
        _s("dedup_key", required=True),
        _s("dedup_order"),
        _l("columns"),
        _m("incremental"),
        _s("filter"),
        _l("unit_tests"),
    ),
    "gold": _contract(
        _l("input_models", required=True),
        _l("joins"),
        _m("aggregation"),
        _m("incremental"),
        _l("unit_tests"),
    ),
    "obt": _contract(
        _l("input_models", required=True),
        _l("columns", required=True),
        _l("joins", required=True),
        _l("unit_tests"),
    ),
    "normalized_table": _contract(
        _l("input_models", required=True),
        _l("entity_columns", required=True),
        _l("attribute_columns", required=True),
        _m("incremental"),
        _s("filter"),
        _l("unit_tests"),
    ),
    "view": _contract(
        _l("input_models", required=True),
        _l("joins"),
        # Required, matching the renderer and the
        # relationship validation - a view without select_columns
        # renders an empty SELECT list, and validation already
        # rejected it as a non-empty-list error; the contract was
        # the only surface still calling it optional, so pattern
        # synthesis and scaffold holes omitted it.
        _l("select_columns", required=True),
        _l("unit_tests"),
    ),
    "mart": _contract(
        _l("input_models", required=True),
        _m("aggregation", required=True),
        _l("unit_tests"),
    ),
}

# Accepted keys inside the source mappings. `schema` is meaningful for
# staging (the source's schema override); bronze ignores it. Both are
# accepted for both generators so the same source shape can move
# between them without a validation surprise. `loaded_at_field` and
# `freshness` declare dbt source freshness on the generated
# source declaration; their shapes are validated by
# `_validate_source_freshness` below.
SOURCE_MAPPING_KEYS = frozenset(
    {"name", "table", "schema", "loaded_at_field", "freshness"}
)
SOURCE_REF_KEYS = frozenset({"name", "table"})

# Accepted keys inside `unit_tests` declarations. The
# contract layer checks only the top-level kind (list); the nested
# shapes are validated against these sets by the dedicated validator
# in inventory.py, following the staging-columns precedent.
UNIT_TEST_KEYS = frozenset({"name", "description", "given", "expect", "overrides"})

# Accepted keys inside one `generic_tests` declaration:
# the test's name, the column it attaches to (absent = a
# model-level test), its arguments and its dbt test config -
# the kwargs and config carried verbatim, never examined.
GENERIC_TEST_KEYS = frozenset({"test", "column_name", "kwargs", "config"})
UNIT_TEST_GIVEN_KEYS = frozenset({"input", "format", "rows"})
UNIT_TEST_EXPECT_KEYS = frozenset({"format", "rows"})
UNIT_TEST_OVERRIDES_KEYS = frozenset({"macros"})
UNIT_TEST_MACRO_OVERRIDE_KEYS = frozenset({"is_incremental"})

_FRESHNESS_PERIODS = ("minute", "hour", "day")
_FRESHNESS_THRESHOLDS = ("warn_after", "error_after")


def _validate_source_freshness(
    path: str, mapping_name: str, value: object
) -> list[str]:
    """Validate an inline source's `freshness` mapping.

    Shape follows dbt source freshness: a mapping with at least one
    of `warn_after`/`error_after`, each a `{count, period}` mapping
    with a positive-integer count and a period of minute/hour/day.
    Errors name the file, the field and the expected shape."""
    prefix = f"{path}: {mapping_name}.freshness"
    if not isinstance(value, dict):
        return [
            f"{prefix} must be a mapping with warn_after and/or "
            "error_after thresholds, e.g. "
            "freshness: {warn_after: {count: 12, period: hour}}"
        ]
    errors: list[str] = []
    for key in sorted(set(value) - set(_FRESHNESS_THRESHOLDS)):
        errors.append(
            f"{prefix} has unknown key {key!r}; accepted keys are: "
            + ", ".join(_FRESHNESS_THRESHOLDS)
        )
    if not any(key in value for key in _FRESHNESS_THRESHOLDS):
        errors.append(
            f"{prefix} must declare at least one of "
            + ", ".join(_FRESHNESS_THRESHOLDS)
        )
    for threshold in _FRESHNESS_THRESHOLDS:
        if threshold not in value:
            continue
        spec = value[threshold]
        field = f"{prefix}.{threshold}"
        if not isinstance(spec, dict):
            errors.append(
                f"{field} must be a mapping of {{count, period}}, "
                "e.g. {count: 12, period: hour}"
            )
            continue
        for key in sorted(set(spec) - {"count", "period"}):
            errors.append(
                f"{field} has unknown key {key!r}; accepted keys "
                "are: count, period"
            )
        count = spec.get("count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
        ):
            errors.append(f"{field}.count must be a positive integer")
        period = spec.get("period")
        if period not in _FRESHNESS_PERIODS:
            errors.append(
                f"{field}.period must be one of: "
                + ", ".join(_FRESHNESS_PERIODS)
            )
    return errors

# Accepted keys inside the `incremental` mapping. The contract
# layer validates the block's shape: unknown keys, scalar types,
# enums, and the reserved-key / reserved-strategy rejections.
# Cross-field rules belong to the per-generator inventory
# validators for the generator that admits
# the block; dialect capability (§1.6(c)) is gated at generate time.
# A generator contract admits `incremental` only in the same change
# that teaches its renderer to honour the block (the
# contract-admission rule): link, bronze and normalized_table
# admitted it first, fact and dimension next, silver last; on
# every other generator this validator is reached only
# alongside the unknown-parameter error.
INCREMENTAL_KEYS = frozenset(
    {
        "strategy",
        "unique_key",
        "on_schema_change",
        "cursor_column",
        "lookback",
        "lookback_unit",
        "full_refresh",
        "event_time",
        "begin",
        "batch_size",
    }
)
# Strategies Loom v1 admits (design §1.2). Dialect support is
# separate adapter truth (Dialect.incremental_strategies).
INCREMENTAL_STRATEGIES = ("append", "delete+insert", "merge")
_ON_SCHEMA_CHANGE_VALUES = (
    "ignore",
    "fail",
    "append_new_columns",
    "sync_all_columns",
)
_LOOKBACK_UNITS = ("hour", "day", "month", "year")
# Microbatch-only keys (design §1.1/§1.3): rejected by name in v1
# so a copied dbt example fails loudly instead of being silently
# ignored; the microbatch fast-follow admits them.
_INCREMENTAL_RESERVED_KEYS = ("event_time", "begin", "batch_size")


def _validate_incremental_block(path: str, value: object) -> list[str]:
    """Validate an `incremental` mapping's shape.

    Covers unknown keys (naming the accepted set), the required
    `strategy`, scalar types, the strategy / on_schema_change /
    lookback-unit enums, and the named rejections: the reserved
    microbatch strategy and keys, and `insert_overwrite` (excluded
    from v1 because it needs partition metadata Loom does not
    declare). Cross-field rules are §1.6(b) and are not checked
    here."""
    prefix = f"{path}: incremental"
    if not isinstance(value, dict):
        return [
            f"{prefix} must be a mapping of incremental options, e.g. "
            "incremental: {strategy: merge, unique_key: order_id}"
        ]
    errors: list[str] = []
    for key in sorted(set(value) - INCREMENTAL_KEYS):
        errors.append(
            f"{prefix} has unknown key {key!r}; accepted keys are: "
            + ", ".join(sorted(INCREMENTAL_KEYS))
        )
    for key in _INCREMENTAL_RESERVED_KEYS:
        if key in value:
            errors.append(
                f"{prefix}.{key} is reserved for the microbatch "
                "fast-follow and is not accepted yet"
            )
    if "strategy" not in value:
        errors.append(
            f"{prefix}.strategy is required when the 'incremental' "
            "block is present"
        )
    else:
        strategy = value["strategy"]
        if not isinstance(strategy, str):
            errors.append(f"{prefix}.strategy must be a string")
        elif strategy == "microbatch":
            errors.append(
                f"{prefix}.strategy 'microbatch' is reserved for the "
                "microbatch fast-follow and is not accepted yet"
            )
        elif strategy == "insert_overwrite":
            errors.append(
                f"{prefix}.strategy 'insert_overwrite' is not "
                "accepted: it needs partition metadata Loom does not "
                "declare"
            )
        elif strategy not in INCREMENTAL_STRATEGIES:
            errors.append(
                f"{prefix}.strategy must be one of: "
                + ", ".join(INCREMENTAL_STRATEGIES)
            )
    if "unique_key" in value:
        unique_key = value["unique_key"]
        if not (
            isinstance(unique_key, str)
            or (
                isinstance(unique_key, list)
                and len(unique_key) > 0
                and all(isinstance(item, str) for item in unique_key)
            )
        ):
            errors.append(
                f"{prefix}.unique_key must be a string or a list of strings"
            )
    if "on_schema_change" in value:
        if value["on_schema_change"] not in _ON_SCHEMA_CHANGE_VALUES:
            errors.append(
                f"{prefix}.on_schema_change must be one of: "
                + ", ".join(_ON_SCHEMA_CHANGE_VALUES)
            )
    for key in ("cursor_column", "lookback_unit"):
        if key in value and not isinstance(value[key], str):
            errors.append(f"{prefix}.{key} must be a string")
    if isinstance(value.get("lookback_unit"), str):
        if value["lookback_unit"] not in _LOOKBACK_UNITS:
            errors.append(
                f"{prefix}.lookback_unit must be one of: "
                + ", ".join(_LOOKBACK_UNITS)
            )
    if "lookback" in value:
        lookback = value["lookback"]
        if (
            not isinstance(lookback, int)
            or isinstance(lookback, bool)
            or lookback < 0
        ):
            errors.append(f"{prefix}.lookback must be an integer >= 0")
    if "full_refresh" in value and not isinstance(
        value["full_refresh"], bool
    ):
        errors.append(f"{prefix}.full_refresh must be a boolean")
    return errors


_KIND_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "list": lambda v: isinstance(v, list),
    "mapping": lambda v: isinstance(v, dict),
    "string_or_mapping": lambda v: isinstance(v, (str, dict)),
}
_KIND_LABELS = {
    "string": "a string",
    "list": "a list",
    "mapping": "a mapping",
    "string_or_mapping": "a string or mapping",
}


# Jinja delimiters: an expression is plain SQL carried verbatim
# (ADR 0037); Jinja belongs to the macro slot, never to an
# expression item.
_JINJA_DELIMITERS = ("{{", "}}", "{%", "%}", "{#", "#}")


def expression_content_errors(path: str, field: str, value: object) -> list[str]:
    """The uniform expression-item grammar's content rules.

    One concept on every surface that admits an ``expression``
    item (staging columns, view select_columns, gold/mart
    group_by dimensions; the aggregate-context variant under
    measures predates it): the value is a non-empty verbatim SQL
    string, carried and rendered exactly as written - never
    evaluated, never identifier-checked (practitioner-asserted,
    the measures precedent) - with Jinja delimiters and statement
    terminators banned, so an expression can neither smuggle a
    template nor stack a second statement into generated SQL.
    """
    if not isinstance(value, str) or not value.strip():
        return [f"{path}: {field} must be a non-empty string"]
    if any(token in value for token in _JINJA_DELIMITERS) or ";" in value:
        return [
            f"{path}: {field} must not contain Jinja delimiters or a "
            "statement terminator (';'): an expression is plain SQL "
            "carried verbatim - use the macro slot for templated SQL"
        ]
    return []


def filter_content_errors(path: str, value: object) -> list[str]:
    """The `filter` parameter's content rules (ADR 0038).

    One concept on every surface that admits a ``filter``
    (staging, silver, dimension, fact, scd2_dimension,
    normalized_table): the value is a non-empty verbatim SQL
    predicate string, carried into the generated model's WHERE
    clause exactly as written - never evaluated, never
    identifier-checked (practitioner-asserted, the expression-item
    posture of ADR 0037) - with Jinja delimiters and statement
    terminators banned, so a filter can neither smuggle a
    template (the macro admission the Jinja ban excludes) nor
    stack a second statement into generated SQL.
    """
    if not isinstance(value, str) or not value.strip():
        return [
            f"{path}: filter must be a non-empty string: a SQL "
            "predicate carried verbatim into the generated WHERE "
            "clause, e.g. filter: \"status = 'active'\""
        ]
    if any(token in value for token in _JINJA_DELIMITERS) or ";" in value:
        return [
            f"{path}: filter must not contain Jinja delimiters or a "
            "statement terminator (';'): a filter is a plain SQL "
            "predicate carried verbatim - templated or stacked SQL "
            "is not admitted"
        ]
    return []


def accepted_params(generator: str) -> frozenset[str]:
    """All parameter names a generator accepts (excluding envelope)."""
    return frozenset(p.name for p in GENERATOR_CONTRACTS[generator])


def validate_against_contract(path: str, data: dict) -> list[str]:
    """Validate one expanded generator record against its contract.

    Returns error strings covering: unknown parameters (naming the
    accepted set), missing required parameters, top-level type
    mismatches, and unknown keys inside source mappings. Deeper
    semantic rules (non-empty lists, name prefixes, cross-field rules)
    stay with the dedicated validators in ``inventory.py``.
    """
    generator = data.get("generator")
    contract = GENERATOR_CONTRACTS.get(generator)  # type: ignore[arg-type]
    if contract is None:
        # A *registered* generator without a contract is a wiring
        # error, not a pass - before this check it validated with no
        # parameter checking at all (fail-open). Unregistered names keep
        # the empty result: the record validator already reports them as
        # unsupported generators.
        if isinstance(generator, str) and generator in GENERATOR_REGISTRY:
            return [
                f"{path}: generator {generator!r} is registered but has "
                "no parameter contract; generator wiring is incomplete"
            ]
        return []
    errors: list[str] = []
    by_name = {p.name: p for p in contract}
    for key in sorted(set(data) - ENVELOPE_KEYS):
        if key not in by_name:
            accepted = ", ".join(sorted(by_name))
            errors.append(
                f"{path}: unknown parameter {key!r} for generator "
                f"{generator!r}; accepted parameters are: {accepted}"
            )
    for param in contract:
        if param.name not in data:
            if param.required:
                errors.append(f"{path}: '{param.name}' is a required property")
            continue
        value = data[param.name]
        if value is None:
            # An explicit null is not a supplied value. No
            # current parameter is nullable, so required parameters
            # fail their required-value guarantee and optional ones
            # fail their type check; omitting the key stays valid.
            if param.required:
                errors.append(
                    f"{path}: '{param.name}' is a required property "
                    "and must not be null"
                )
            else:
                errors.append(
                    f"{path}: '{param.name}' must be "
                    f"{_KIND_LABELS[param.kind]}, not null"
                )
            continue
        if not _KIND_CHECKS[param.kind](value):
            errors.append(
                f"{path}: '{param.name}' must be {_KIND_LABELS[param.kind]}"
            )
    for mapping_name, allowed in (
        ("inline_source", SOURCE_MAPPING_KEYS),
        ("source_ref", SOURCE_REF_KEYS),
    ):
        mapping = data.get(mapping_name)
        if isinstance(mapping, dict):
            # `name` and `table` are genuinely required in both
            # source mappings (pi-review P2): without them the
            # staging renderer used to substitute the placeholder
            # 'unknown' into generated SQL, where the defect only
            # surfaced at dbt compile time. `schema` stays
            # optional - it has a real default.
            for key in ("name", "table"):
                if key not in mapping:
                    errors.append(
                        f"{path}: {mapping_name}.{key} is a required property"
                    )
            for key in sorted(set(mapping) - allowed):
                errors.append(
                    f"{path}: {mapping_name} has unknown key {key!r}; "
                    f"accepted keys are: {', '.join(sorted(allowed))}"
                )
            for key in sorted(set(mapping) & allowed):
                if key == "freshness":
                    # A mapping with its own shape, validated
                    # separately below; not a string.
                    continue
                if not isinstance(mapping[key], str):
                    errors.append(
                        f"{path}: {mapping_name}.{key} must be a string"
                    )
            if "freshness" in mapping:
                errors.extend(
                    _validate_source_freshness(
                        path, mapping_name, mapping["freshness"]
                    )
                )
    if "incremental" in data:
        # Shape-check the block wherever it appears.
        # On a generator whose contract does not admit the parameter
        # yet, the unknown-parameter error above also fires; the
        # block's own errors still name the precise problem with
        # the block.
        errors.extend(_validate_incremental_block(path, data["incremental"]))
    if "filter" in data:
        # Content-check the predicate wherever it appears, on the
        # incremental precedent: on a generator whose contract does
        # not admit the parameter, the unknown-parameter error above
        # also fires; the content errors still name the precise
        # problem with the value.
        errors.extend(filter_content_errors(path, data["filter"]))
    return errors


def check_generator_wiring() -> None:
    """Fail explicitly when generator wiring is incomplete.

    The registry and the contract table must name exactly the same
    generators. Before this check, a generator registered without a
    contract (or a contract without a registration) was only caught by
    a test-suite set-equality assertion; at runtime it validated and
    generated with no parameter checking (fail-open). Project loading
    calls this so every command - and direct `validate_project` use -
    refuses an incompletely wired build instead of silently passing.
    """
    registered = set(GENERATOR_REGISTRY)
    contracted = set(GENERATOR_CONTRACTS)
    problems = [
        f"generator {name!r} is registered but has no parameter contract"
        for name in sorted(registered - contracted)
    ] + [
        f"generator {name!r} has a parameter contract but is not registered"
        for name in sorted(contracted - registered)
    ]
    if problems:
        raise LoomError(
            "generator wiring is incomplete: " + "; ".join(problems)
        )
