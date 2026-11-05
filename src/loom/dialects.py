from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from loom.errors import LoomError


@dataclass(frozen=True)
class Dialect:
    name: str
    hash_function: str
    hash_template: str
    string_type: str
    current_timestamp: str
    # Snapshot hard_deletes behaviour: True -> 'new_record',
    # False -> 'fail', None -> omit the property entirely because
    # the adapter rejects it (dbt-clickhouse validates it away).
    snapshot_hard_deletes: bool | None
    # Name of the deleted-flag column in the adapter's snapshot
    # tables, or None when the adapter records no such column
    # (dbt-clickhouse: hard deletes surface only as closed rows).
    snapshot_deleted_column: str | None
    # Expression producing a real boolean from a snapshot's
    # dbt_is_deleted column ({col} placeholder). Adapters materialize
    # that column with inconsistent types (VARCHAR 'True'/'False' on
    # DuckDB, native boolean elsewhere), so generated views normalize
    # it through this template instead of selecting it raw.
    deleted_flag_template: str
    # Gate expression for the link fail-fast guard: yields 1
    # when {violations} (a count of null/blank/unmatched input keys)
    # is zero and makes the query fail otherwise. SQL has no portable
    # raise, and the failure idiom differs per engine family -
    # verified live on DuckDB/PostgreSQL/ClickHouse:
    # message functions where the engine has one (error / ERROR /
    # raise_error), integer division by zero elsewhere (DuckDB's /
    # is float division returning inf, and ClickHouse needs intDiv
    # for the same reason).
    link_guard_template: str
    # SHA-256 wrapper for satellite hash diffs (ADR 0028):
    # the dialect's hash of the canonical payload string the
    # renderer builds into the {concat} placeholder (per-column text
    # casts, '~' for NULL, '|' delimiter). One complete template per
    # dialect, so the composed expression is valid by
    # construction. The wrapper intentionally mirrors hash_template
    # today; the separate field lets diff hashing diverge later
    # without touching key hashing.
    hash_diff_template: str
    # Incremental strategy capability set: the real
    # dbt incremental strategies this dialect's adapter supports,
    # transcribed from dbt's incremental-strategy adapter support
    # table (docs.getdbt.com/docs/build/incremental-strategy,
    # transcribed 2026-10-26). Adapter truth, not Loom's admission
    # policy: the set includes strategies Loom v1 does not admit
    # (insert_overwrite, microbatch). Contracts decide admission
    # (loom.contracts); the generate-time gate in renderers/base.py
    # checks membership. These fields replace the former
    # incremental_strategy / supports_merge pair, whose values were
    # not dbt strategy names and which no renderer read.
    incremental_strategies: frozenset[str]
    # Date-arithmetic fragment for the incremental cursor lookback
    #: renders the {expr} expression shifted back by {n}
    # {unit}s; {unit} is substituted as an uppercase unit keyword
    # (HOUR/DAY/MONTH/YEAR).
    cursor_dateadd_template: str
    # Star-exclusion fragment for dropping generated helper columns
    # from a `SELECT *` projection ({column} placeholder), or None
    # when the engine has no such syntax (Postgres family, T-SQL).
    # Silver's dedup output uses it to keep its internal ROW_NUMBER
    # column out of the generated model; dialects without it
    # must declare the projected columns explicitly instead.
    star_exclude_template: str | None = None
    # Adapter-reported type spellings for enforced contracts
    # (ADR 0032): the generic vocabulary mapped to the
    # strings dbt's contract preflight compares against. Distinct
    # from the descriptive spellings in DATA_TYPE_SPELLINGS - a
    # contract declaration must match what the adapter *reports*,
    # not what dbt docs should display. None where no map has been
    # derived yet (the descriptive spelling is rendered instead,
    # with a warning at generation time).
    contract_types: dict[str, str] | None = None
    # True only where an opted-in model's contract has passed dbt's
    # preflight against a live warehouse (runtime proof). Opting in
    # on an unproven dialect warns rather than blocks.
    contract_proven: bool = False
    # Timestamp literal for a {value} ISO timestamp string, in the
    # form the engine accepts in a timestamp-typed context (the
    # PIT ghost-record sentinel). ClickHouse has no TIMESTAMP
    # literal (toDateTime parses the string); T-SQL's TIMESTAMP is
    # rowversion, so a bare string literal relies on implicit
    # conversion against the timestamp branch it is coalesced with.
    timestamp_literal_template: str = "TIMESTAMP '{value}'"


_GUARD_MESSAGE = (
    "Loom link guard: link input contains null, blank or unmatched keys"
)


def _division_gate() -> str:
    return "1 / (CASE WHEN {violations} > 0 THEN 0 ELSE 1 END)"


def _message_gate(function: str, cast: str = "") -> str:
    call = f"{function}('{_GUARD_MESSAGE}')"
    if cast:
        call = f"CAST({call} AS {cast})"
    return f"CASE WHEN {{violations}} > 0 THEN {call} ELSE 1 END"


# Enforced-contract type maps. DuckDB's is proven by
# the runtime integration proof; the Postgres and ClickHouse maps
# are derived from the adapters' reported types and graduate to
# proven when a live preflight proof runs (contract_proven stays
# False until then, so opting in warns).
_CONTRACT_TYPES_DUCKDB = {
    "text": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
    "decimal": "DECIMAL({precision},{scale})", "boolean": "BOOLEAN",
    "date": "DATE",
    # Loom's DuckDB stamp columns are naive TIMESTAMP (the
    # dialect's current_timestamp is `localtimestamp`), and
    # DuckDB reports them as TIMESTAMP. (The stamps were once
    # CURRENT_TIMESTAMP - TIMESTAMPTZ - and this entry had to
    # spell TIMESTAMP WITH TIME ZONE; the contract preflight
    # proof is what pinned that coupling.)
    "timestamp": "TIMESTAMP",
}
_CONTRACT_TYPES_POSTGRES = {
    "text": "character varying", "integer": "integer",
    "bigint": "bigint", "decimal": "numeric({precision},{scale})",
    "boolean": "boolean", "date": "date",
    "timestamp": "timestamp without time zone",
}
_CONTRACT_TYPES_CLICKHOUSE = {
    "text": "String", "integer": "Int32", "bigint": "Int64",
    "decimal": "Decimal({precision},{scale})", "boolean": "Bool",
    "date": "Date", "timestamp": "DateTime",
}


DIALECTS: dict[str, Dialect] = {
    "duckdb": Dialect(
        name="duckdb",
        hash_function="sha256",
        hash_template="LOWER(sha256({key}))",
        string_type="VARCHAR",
        # DuckDB's CURRENT_TIMESTAMP is TIMESTAMP WITH TIME ZONE;
        # Loom's load stamps are naive local timestamps by design
        # (the PIT ghost sentinel is a naive literal, ADR 0035).
        # `localtimestamp` is DuckDB's naive spelling - the same
        # wall-clock value, typed TIMESTAMP end-to-end.
        current_timestamp="localtimestamp",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BOOLEAN)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_message_gate("error"),
        hash_diff_template="LOWER(sha256({concat}))",
        incremental_strategies=frozenset({"append", "delete+insert", "merge", "microbatch"}),
        cursor_dateadd_template="{expr} - INTERVAL '{n}' {unit}",
        star_exclude_template="* EXCLUDE ({column})",
        contract_types=_CONTRACT_TYPES_DUCKDB,
        contract_proven=True,
    ),
    "snowflake": Dialect(
        name="snowflake",
        hash_function="SHA2",
        hash_template="LOWER(SHA2({key}))",
        string_type="VARCHAR",
        current_timestamp="CURRENT_TIMESTAMP",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BOOLEAN)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_division_gate(),
        hash_diff_template="LOWER(SHA2({concat}))",
        incremental_strategies=frozenset({"append", "delete+insert", "insert_overwrite", "merge", "microbatch"}),
        cursor_dateadd_template="DATEADD({unit}, -{n}, {expr})",
        star_exclude_template="* EXCLUDE ({column})",
    ),
    "bigquery": Dialect(
        name="bigquery",
        hash_function="SHA256",
        hash_template="LOWER(TO_HEX(SHA256(CAST({key} AS BYTES))))",
        string_type="STRING",
        current_timestamp="CURRENT_TIMESTAMP",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BOOL)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_message_gate("ERROR"),
        hash_diff_template="LOWER(TO_HEX(SHA256(CAST({concat} AS BYTES))))",
        incremental_strategies=frozenset({"insert_overwrite", "merge", "microbatch"}),
        cursor_dateadd_template="{expr} - INTERVAL {n} {unit}",
        star_exclude_template="* EXCEPT ({column})",
    ),
    "postgres": Dialect(
        name="postgres",
        hash_function="sha256",
        hash_template="encode(sha256(convert_to({key}, 'UTF8')), 'hex')",
        string_type="VARCHAR",
        current_timestamp="CURRENT_TIMESTAMP",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BOOLEAN)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_division_gate(),
        hash_diff_template="encode(sha256(convert_to({concat}, 'UTF8')), 'hex')",
        incremental_strategies=frozenset({"append", "delete+insert", "merge", "microbatch"}),
        cursor_dateadd_template="{expr} - INTERVAL '{n}' {unit}",
        star_exclude_template=None,
        contract_types=_CONTRACT_TYPES_POSTGRES,
    ),
    "redshift": Dialect(
        name="redshift",
        hash_function="SHA2",
        hash_template="LOWER(SHA2({key}, 256))",
        string_type="VARCHAR",
        current_timestamp="GETDATE()",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BOOLEAN)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_division_gate(),
        hash_diff_template="LOWER(SHA2({concat}, 256))",
        incremental_strategies=frozenset({"append", "delete+insert", "merge", "microbatch"}),
        cursor_dateadd_template="{expr} - INTERVAL '{n}' {unit}",
        star_exclude_template=None,
    ),
    "databricks": Dialect(
        name="databricks",
        hash_function="sha2",
        hash_template="lower(sha2({key}, 256))",
        string_type="STRING",
        current_timestamp="current_timestamp()",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BOOLEAN)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_message_gate("raise_error", cast="BIGINT"),
        hash_diff_template="lower(sha2({concat}, 256))",
        incremental_strategies=frozenset({"append", "delete+insert", "insert_overwrite", "merge", "microbatch"}),
        cursor_dateadd_template="{expr} - INTERVAL {n} {unit}",
        star_exclude_template="* EXCEPT ({column})",
    ),
    "azuresynapse": Dialect(
        name="azuresynapse",
        hash_function="HASHBYTES",
        hash_template="LOWER(CONVERT(VARCHAR(MAX), HASHBYTES('SHA2_256', {key}), 2))",
        string_type="VARCHAR",
        current_timestamp="GETDATE()",
        snapshot_hard_deletes=True,
        deleted_flag_template="CAST({col} AS BIT)",
        snapshot_deleted_column="dbt_is_deleted",
        link_guard_template=_division_gate(),
        hash_diff_template="LOWER(CONVERT(VARCHAR(MAX), HASHBYTES('SHA2_256', {concat}), 2))",
        incremental_strategies=frozenset({"append", "delete+insert", "merge", "microbatch"}),
        cursor_dateadd_template="DATEADD({unit}, -{n}, {expr})",
        star_exclude_template=None,
        timestamp_literal_template="'{value}'",
    ),
    "clickhouse": Dialect(
        name="clickhouse",
        hash_function="SHA256",
        hash_template="lower(hex(SHA256({key})))",
        string_type="String",
        current_timestamp="now()",
        snapshot_hard_deletes=None,
        deleted_flag_template="CAST({col} AS Bool)",
        snapshot_deleted_column=None,
        link_guard_template="intDiv(1, CASE WHEN {violations} > 0 THEN 0 ELSE 1 END)",
        hash_diff_template="lower(hex(SHA256({concat})))",
        incremental_strategies=frozenset({"append", "delete+insert", "insert_overwrite"}),
        cursor_dateadd_template="date_add({expr}, INTERVAL -{n} {unit})",
        star_exclude_template="* EXCEPT ({column})",
        contract_types=_CONTRACT_TYPES_CLICKHOUSE,
        timestamp_literal_template="toDateTime('{value}')",
    ),
}

ADAPTER_TO_DIALECT: dict[str, str] = {
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "azuresynapse": "azuresynapse",
    "clickhouse": "clickhouse",
}


def get_dialect(name: str) -> Dialect:
    """Get a dialect by name. Raises LoomError for unknown names."""
    if name not in DIALECTS:
        raise LoomError(
            f"unknown dialect {name!r}; supported: {', '.join(sorted(DIALECTS))}"
        )
    return DIALECTS[name]


def find_profiles_file(project_dir: Path) -> Path | None:
    """Locate the dbt profiles file the way dbt itself does.

    Search order: the project directory, then ``$DBT_PROFILES_DIR``,
    then ``~/.dbt``. Returns None when no profiles file exists in any
    of those locations.
    """
    candidates = [project_dir / "profiles.yml"]
    profiles_dir = os.environ.get("DBT_PROFILES_DIR")
    if profiles_dir:
        candidates.append(Path(profiles_dir).expanduser() / "profiles.yml")
    candidates.append(Path.home() / ".dbt" / "profiles.yml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def detect_dialect(project_dir: Path) -> str:
    """Detect the active dbt target's dialect, defaulting only without a profile."""
    profile_path = find_profiles_file(project_dir)
    if profile_path is None:
        return "duckdb"

    project_path = project_dir / "dbt_project.yml"
    project_config = _load_yaml_mapping(project_path)
    profile_name = project_config.get("profile")
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise LoomError(
            "dbt_project.yml must define the active dbt profile with a non-empty "
            "'profile' value when profiles.yml is present"
        )

    profiles = _load_yaml_mapping(profile_path)
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        raise LoomError(
            f"profile {profile_name!r} from dbt_project.yml is missing or invalid "
            "in profiles.yml"
        )

    target = profile.get("target")
    if not isinstance(target, str) or not target.strip():
        raise LoomError(f"profile {profile_name!r} must define a target")

    outputs = profile.get("outputs")
    if not isinstance(outputs, dict):
        raise LoomError(f"profile {profile_name!r} must define an outputs mapping")

    output = outputs.get(target)
    if not isinstance(output, dict):
        raise LoomError(f"target {target!r} is not defined in profile {profile_name!r}")

    adapter = output.get("type")
    if not isinstance(adapter, str) or not adapter.strip():
        raise LoomError(
            f"target {target!r} in profile {profile_name!r} must define an adapter "
            "'type'"
        )
    adapter_name = adapter.strip().lower()
    dialect = ADAPTER_TO_DIALECT.get(adapter_name)
    if dialect is None:
        supported = ", ".join(sorted(ADAPTER_TO_DIALECT))
        raise LoomError(
            f"unsupported dbt adapter {adapter_name!r}; supported adapters: {supported}"
        )
    return dialect


# Adapter-type aliases: names a dbt adapter registers (and a profile
# declares) that differ from the key ADAPTER_TO_DIALECT is indexed
# by. dbt-synapse registers the adapter type "synapse" while Loom's
# map is keyed by "azuresynapse". An alias names the same
# adapter, so comparisons between a manifest's recorded adapter and
# a profile's declared adapter must canonicalise through this map
# instead of reporting a false mismatch.
ADAPTER_ALIASES: dict[str, str] = {
    "synapse": "azuresynapse",
}


def canonical_adapter_name(name: str) -> str:
    """Canonical adapter name for comparisons: the ADAPTER_TO_DIALECT
    key an adapter type denotes, resolving known aliases."""
    cleaned = name.strip().lower()
    return ADAPTER_ALIASES.get(cleaned, cleaned)


def detect_profile_adapter(project_dir: Path) -> str | None:
    """The adapter type declared by the project's active profile
    target, or None when it cannot be resolved.

    The tolerant counterpart to detect_dialect: the same profiles
    search order (find_profiles_file) and the same
    profile/target/output traversal, but every failure - no profiles
    file, no profile name in dbt_project.yml, a missing target or
    output, unreadable YAML - returns None instead of raising.
    Callers that need provenance only (a warning, never a hard
    dependency) use this: an unresolvable profile is absence of
    evidence, not an error.
    """
    profile_path = find_profiles_file(project_dir)
    if profile_path is None:
        return None
    try:
        project_config = _load_yaml_mapping(project_dir / "dbt_project.yml")
        profile_name = project_config.get("profile")
        if not isinstance(profile_name, str) or not profile_name.strip():
            return None
        profiles = _load_yaml_mapping(profile_path)
        profile = profiles.get(profile_name)
        if not isinstance(profile, dict):
            return None
        target = profile.get("target")
        if not isinstance(target, str) or not target.strip():
            return None
        outputs = profile.get("outputs")
        if not isinstance(outputs, dict):
            return None
        output = outputs.get(target)
        if not isinstance(output, dict):
            return None
        adapter = output.get("type")
        if not isinstance(adapter, str) or not adapter.strip():
            return None
        return adapter.strip().lower()
    except LoomError:
        return None


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as profile_file:
            value = yaml.safe_load(profile_file)
    except OSError as error:
        raise LoomError(f"cannot read {path.name}: {error}") from error
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark is not None else ""
        raise LoomError(f"invalid YAML in {path.name}{line}") from error

    if not isinstance(value, dict):
        raise LoomError(f"{path.name} must contain a YAML mapping")
    return value


# Generic data-type vocabulary for declared column types.
# Practitioners declare types in `column_types` metadata using these
# generic names; the generated model properties render the dialect's
# spelling. `decimal` is a template with {precision}/{scale}
# placeholders. DESCRIPTIVE GRADE: these spellings are
# what dbt docs should display; they are not yet the
# adapter-reported strings an *enforced* contract preflight compares
# against - that proof is the per-dialect contract work, gated on
# runtime evidence. `text` deliberately equals each dialect's
# `string_type` (a test pins the two together).
DATA_TYPE_SPELLINGS: dict[str, dict[str, str]] = {
    "duckdb": {
        "text": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
        "decimal": "DECIMAL({precision},{scale})", "boolean": "BOOLEAN",
        "date": "DATE", "timestamp": "TIMESTAMP",
    },
    "snowflake": {
        "text": "VARCHAR", "integer": "NUMBER(38,0)",
        "bigint": "NUMBER(38,0)",
        "decimal": "NUMBER({precision},{scale})", "boolean": "BOOLEAN",
        "date": "DATE", "timestamp": "TIMESTAMP_NTZ",
    },
    "bigquery": {
        "text": "STRING", "integer": "INT64", "bigint": "INT64",
        "decimal": "NUMERIC({precision},{scale})", "boolean": "BOOL",
        "date": "DATE", "timestamp": "TIMESTAMP",
    },
    "postgres": {
        "text": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
        "decimal": "DECIMAL({precision},{scale})", "boolean": "BOOLEAN",
        "date": "DATE", "timestamp": "TIMESTAMP",
    },
    "redshift": {
        "text": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
        "decimal": "DECIMAL({precision},{scale})", "boolean": "BOOLEAN",
        "date": "DATE", "timestamp": "TIMESTAMP",
    },
    "databricks": {
        "text": "STRING", "integer": "INT", "bigint": "BIGINT",
        "decimal": "DECIMAL({precision},{scale})", "boolean": "BOOLEAN",
        "date": "DATE", "timestamp": "TIMESTAMP",
    },
    "azuresynapse": {
        "text": "VARCHAR", "integer": "INT", "bigint": "BIGINT",
        "decimal": "DECIMAL({precision},{scale})", "boolean": "BIT",
        "date": "DATE", "timestamp": "DATETIME2",
    },
    "clickhouse": {
        "text": "String", "integer": "Int32", "bigint": "Int64",
        "decimal": "Decimal({precision},{scale})", "boolean": "Bool",
        "date": "Date", "timestamp": "DateTime",
    },
}

GENERIC_DATA_TYPES = frozenset(
    {"text", "integer", "bigint", "decimal", "boolean", "date", "timestamp"}
)


def _render_from_spellings(
    spellings: dict[str, str], generic: str
) -> str | None:
    value = generic.strip().lower()
    if value.startswith("decimal"):
        # Bare `decimal` is not renderable: precision and scale are
        # part of the declared type (validation enforces this too).
        template = spellings["decimal"]
        if value.startswith("decimal(") and value.endswith(")"):
            parts = value[len("decimal(") : -1].split(",")
            if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
                return template.format(
                    precision=int(parts[0]), scale=int(parts[1])
                )
        return None
    return spellings.get(value)


def render_data_type(dialect: Dialect, generic: str) -> str | None:
    """Render one generic `column_types` value in a dialect's spelling.

    Accepts the plain generic names and `decimal(p,s)` with integer
    precision/scale. Returns None for anything else - callers treat
    an unrenderable type as a validation matter, never a guess.
    """
    spellings = DATA_TYPE_SPELLINGS.get(dialect.name)
    if spellings is None:
        return None
    return _render_from_spellings(spellings, generic)


def render_contract_data_type(dialect: Dialect, generic: str) -> str | None:
    """Render a generic type for an *enforced contract* declaration.

    Uses the dialect's adapter-reported contract spellings where a
    map exists; otherwise falls back to the descriptive spelling
    (generation warns that enforcement is unproven there). Same
    renderability rules as `render_data_type`.
    """
    if dialect.contract_types is not None:
        return _render_from_spellings(dialect.contract_types, generic)
    return render_data_type(dialect, generic)
