from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from loom.contracts import INCREMENTAL_STRATEGIES
from loom.dialects import Dialect
from loom.errors import LoomError


def _ensure_trailing_newline(text: str | None) -> str | None:
    """A rendered document ends with a trailing newline (QOL A-8).

    Append-only: text that already ends with a newline is returned
    unchanged, so the guarantee costs exactly one byte where it
    changes anything - the property the golden regeneration for
    this change verified across every emitter.
    """
    if text is None or text.endswith("\n"):
        return text
    return text + "\n"


def _wrap_emitter(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        return _ensure_trailing_newline(method(self, *args, **kwargs))

    return wrapped


@dataclass(frozen=True)
class IncrementalSpec:
    """A resolved `incremental` declaration.

    Produced by ``BaseRenderer._incremental_spec`` from the
    contract-validated block. ``on_schema_change`` is None only for
    the hub's original emission, which never wrote the key; specs
    built from a declared block default it to ``'fail'``.
    """

    strategy: str
    unique_key: tuple[str, ...] = ()
    on_schema_change: str | None = "fail"
    cursor_column: str | None = None
    lookback: int = 0
    lookback_unit: str | None = None
    full_refresh: bool | None = None


class BaseRenderer(ABC):
    """Base class for all generator renderers.

    Each renderer produces dialect-appropriate SQL. The ``output_path``
    parameter is reserved for future file-writing logic; renderers
    return SQL strings only.

    Every emitted document (``render`` and ``render_tests``)
    ends with a trailing newline, so a generated file is a
    well-formed text file and a captured block of generated SQL
    in documentation can equal the file byte-for-byte. The
    guarantee is enforced here, not in each renderer:
    ``__init_subclass__`` wraps the two emitter methods of every
    subclass with the append-only normaliser, so a renderer
    cannot forget it and an already-terminated document (the
    SCD2 snapshot form, the YAML documents) passes through
    byte-identical.
    """

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for name in ("render", "render_tests"):
            method = cls.__dict__.get(name)
            if method is not None:
                setattr(cls, name, _wrap_emitter(method))

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    def _normalize_key(self, dialect: Dialect, key: str) -> str:
        """Build the key normalization expression for this dialect."""
        if dialect.name == "snowflake":
            return f"UPPER(TRIM(CAST({key} AS VARCHAR)))"
        elif dialect.name == "bigquery":
            return f"UPPER(TRIM(CAST({key} AS STRING)))"
        elif dialect.name == "databricks":
            return f"UPPER(TRIM(CAST({key} AS STRING)))"
        elif dialect.name == "azuresynapse":
            return f"UPPER(TRIM(CAST({key} AS VARCHAR(4000))))"
        elif dialect.name == "clickhouse":
            return f"upper(trim(cast({key} as String)))"
        else:  # duckdb, postgres, redshift
            return f"UPPER(TRIM(CAST({key} AS VARCHAR)))"

    def _key_cast(self, dialect: Dialect, expression: str) -> str:
        """Cast an expression to the dialect's key text type.

        Mirrors the cast inside _normalize_key: hub business keys are
        stored as normalized text, so key comparisons against raw
        typed keys (an integer staging key, say) need both sides cast
        to text on engines that refuse int = text (PostgreSQL).
        """
        if dialect.name in ("bigquery", "databricks"):
            return f"CAST({expression} AS STRING)"
        if dialect.name == "azuresynapse":
            return f"CAST({expression} AS VARCHAR(4000))"
        if dialect.name == "clickhouse":
            return f"cast({expression} as String)"
        return f"CAST({expression} AS VARCHAR)"

    def _hash_diff_expression(self, columns: list[str], dialect: Dialect) -> str:
        """Build the satellite hash-diff expression (ADR 0028).

        ``columns`` are qualified column references (e.g. ``s.email``).
        Canonical form: each value is cast to the dialect's key text
        type, NULL becomes the literal token ``~``, the values are
        joined with ``|``, and the dialect's ``hash_diff_template``
        hashes the joined string.
        """
        parts = [
            f"COALESCE({self._key_cast(dialect, column)}, '~')"
            for column in columns
        ]
        if dialect.name == "redshift":
            # Redshift's CONCAT takes exactly two arguments, so the
            # canonical string is built with the || operator instead.
            concat = " || '|' || ".join(parts)
        else:
            concat = "concat(" + ", '|', ".join(parts) + ")"
        return dialect.hash_diff_template.replace("{concat}", concat)

    def _concat_expression(self, parts: list[str], dialect: Dialect) -> str:
        """Concatenate hash components for a link hash (ADR 0030).

        Components ahead of any dependent-child key are fixed-length
        hex hashes, so plain concatenation is unambiguous. Redshift's
        CONCAT takes exactly two arguments, so three or more parts
        chain the ``||`` operator there; every other dialect (and
        the two-part case everywhere) uses variadic ``concat`` -
        which keeps binary link SQL byte-identical to the original
        renderer.
        """
        if len(parts) > 2 and dialect.name == "redshift":
            return " || ".join(parts)
        return "concat(" + ", ".join(parts) + ")"

    def _hash_expression(self, normalized_key: str, dialect: Dialect) -> str:
        """Build the hash expression for this dialect.

        The dialect owns one complete template producing a lowercase
        64-character hexadecimal SHA-256 value from the normalized key
        expression, so the composed SQL is valid by construction.
        """
        return dialect.hash_template.replace("{key}", normalized_key)

    # -- Shared incremental core ------------------------------------
    #
    # Every generator's incremental rendering composes these pieces;
    # no generator hand-builds a config block or an is_incremental()
    # fragment. Validation layering: the contract layer checks the
    # block's shape (§1.6(a)), per-generator inventory validators
    # check cross-field rules (§1.6(b)), and the dialect gate below
    # checks strategy capability at generate time (§1.6(c)) -
    # validation stays dialect-free.

    def _incremental_spec(
        self, params: dict, *, default_key: Sequence[str] = ()
    ) -> IncrementalSpec | None:
        """Resolve a validated `incremental` block to a spec.

        Returns None when the record declares no block. The block's
        shape was already validated by the contract layer, so the
        renderer trusts it. ``default_key`` supplies the unique key
        for generators whose grain implies one (the hub: its hash
        column); generators without an implied grain pass nothing
        and rely on their §1.6(b) validator to require the key for
        the strategies that need one.
        """
        block = params.get("incremental")
        if block is None:
            return None
        raw_key = block.get("unique_key")
        if raw_key is None:
            unique_key = tuple(default_key)
        elif isinstance(raw_key, str):
            unique_key = (raw_key,)
        else:
            unique_key = tuple(raw_key)
        return IncrementalSpec(
            strategy=block["strategy"],
            unique_key=unique_key,
            on_schema_change=block.get("on_schema_change", "fail"),
            cursor_column=block.get("cursor_column"),
            lookback=block.get("lookback", 0),
            lookback_unit=block.get("lookback_unit"),
            full_refresh=block.get("full_refresh"),
        )

    def _incremental_config(
        self, spec: IncrementalSpec, *, merge_exclude: Sequence[str] = ()
    ) -> str:
        """Render the dbt config block for an incremental model.

        Key order matches the hub's original emission
        (materialized, unique_key, incremental_strategy, then the
        policy keys) so the hub refactor stays byte-identical
        (§2.5). ``on_schema_change`` is emitted only when the spec
        carries one, ``full_refresh`` only when declared, and the
        merge exclusions only when the renderer passes them (hub
        provenance).
        """
        parts = ["materialized='incremental'"]
        if len(spec.unique_key) == 1:
            parts.append(f"unique_key='{spec.unique_key[0]}'")
        elif len(spec.unique_key) > 1:
            keys = ", ".join(f"'{key}'" for key in spec.unique_key)
            parts.append(f"unique_key=[{keys}]")
        parts.append(f"incremental_strategy='{spec.strategy}'")
        if spec.on_schema_change is not None:
            parts.append(f"on_schema_change='{spec.on_schema_change}'")
        if spec.full_refresh is not None:
            parts.append(f"full_refresh={str(spec.full_refresh).lower()}")
        if merge_exclude:
            excluded = ", ".join(f"'{column}'" for column in merge_exclude)
            parts.append(f"merge_exclude_columns=[{excluded}]")
        return "{{ config(" + ", ".join(parts) + ") }}"

    def _anti_join_filter(
        self, key_expression: str, target_column: str, *, where: bool = False
    ) -> str:
        """Append guard (design §2.3 flavour A) for a single key.

        ``key_expression`` is the source-side expression (a hash
        expression or a projected key column); ``target_column`` is
        the column it is compared against on ``{{ this }}``. The
        default form continues an existing WHERE clause (``AND``);
        ``where=True`` opens the clause instead, for queries that
        carry no WHERE of their own (bronze's raw load, §3.2).
        """
        keyword = "WHERE" if where else "AND"
        return (
            "\n{% if is_incremental() %}\n"
            f"  {keyword} {key_expression} NOT IN (\n"
            f"    SELECT target.{target_column} FROM {{{{ this }}}} AS target\n"
            "  )\n"
            "{% endif %}"
        )

    def _anti_join_filter_multi(
        self, key_pairs: Sequence[tuple[str, str]], *, where: bool = False
    ) -> str:
        """Append guard for a composite key: NOT EXISTS over all pairs.

        Single-column NOT IN misbehaves on NULLs (three-valued
        logic), so composite keys correlate every (source expression,
        target column) pair instead (design §2.3). ``where`` has the
        same meaning as in ``_anti_join_filter``.
        """
        keyword = "WHERE" if where else "AND"
        conditions = " AND ".join(
            f"target.{column} = {expression}"
            for expression, column in key_pairs
        )
        return (
            "\n{% if is_incremental() %}\n"
            f"  {keyword} NOT EXISTS (\n"
            "    SELECT 1 FROM {{ this }} AS target\n"
            f"    WHERE {conditions}\n"
            "  )\n"
            "{% endif %}"
        )

    def _cursor_predicate(
        self,
        spec: IncrementalSpec,
        *,
        source_alias: str = "",
        strict: bool = False,
        conjunction: bool = False,
    ) -> str:
        """Cursor guard (design §2.3 flavour B).

        Keeps source rows at or after the target's current maximum
        cursor value. With a lookback and a declared unit, the bound
        shifts back through the dialect's ``cursor_dateadd_template``;
        with a lookback and no unit it is plain numeric subtraction
        (the presence/absence of the unit chooses the form, §1.1).

        ``strict=True`` renders the append-mode form (design §3.1):
        rows strictly after the target maximum. An append has no key
        to reconcile a re-appended boundary row, so the ``>=`` form
        would duplicate the boundary batch on every rerun; key
        strategies use the default form and let dbt reconcile the
        boundary. A lookback is meaningless under the strict form
        (validation forbids lookback with append) and is rejected
        here rather than silently dropped.

        ``conjunction=True`` renders the guard as an ``AND``
        continuation instead of opening the WHERE clause: the
        caller already opened one (an instance `filter`, ADR 0038),
        and the composition is a single WHERE - filter first, then
        this predicate ANDed behind it inside the same
        ``is_incremental()`` gate.
        """
        if spec.cursor_column is None:
            raise LoomError(
                "incremental cursor predicate requires a cursor_column"
            )
        if strict and spec.lookback:
            raise LoomError(
                "a strict (append-mode) cursor predicate does not "
                "accept a lookback"
            )
        column = (
            f"{source_alias}.{spec.cursor_column}"
            if source_alias
            else spec.cursor_column
        )
        bound = self._cursor_bound(spec)
        operator = ">" if strict else ">="
        keyword = "AND" if conjunction else "WHERE"
        return (
            "\n{% if is_incremental() %}\n"
            f"  {keyword} {column} {operator} {bound}\n"
            "{% endif %}"
        )

    def _cursor_bound(self, spec: IncrementalSpec) -> str:
        """The target-maximum cursor bound for ``spec``.

        ``(SELECT MAX(cursor) FROM {{ this }})``, shifted back by
        the lookback when one is declared: through the dialect's
        ``cursor_dateadd_template`` when a unit is declared, as
        plain numeric subtraction otherwise (the presence/absence
        of the unit chooses the form, §1.1). Shared by the cursor
        predicate (flavour B) and the medallion affected-key
        restriction (§4/§5), which differ only in how the window is
        applied, never in how it is computed.
        """
        if spec.cursor_column is None:
            raise LoomError(
                "incremental cursor bound requires a cursor_column"
            )
        bound = f"(SELECT MAX({spec.cursor_column}) FROM {{{{ this }}}})"
        if spec.lookback:
            if spec.lookback_unit is not None:
                bound = (
                    self.dialect.cursor_dateadd_template.replace(
                        "{expr}", bound
                    )
                  .replace("{n}", str(spec.lookback))
                  .replace("{unit}", spec.lookback_unit.upper())
                )
            else:
                bound = f"{bound} - {spec.lookback}"
        return bound

    def _affected_key_filter(
        self,
        spec: IncrementalSpec,
        *,
        key_column: str,
        input_ref: str,
        conjunction: bool = False,
    ) -> str:
        """Affected-key restriction (medallion recomputation, §4.2).

        Gates an ``IN`` restriction, spliced into the caller's
        query over the input, on the keys whose input rows fall
        inside the cursor window - the flavour-B bound applied to a
        DISTINCT projection of the input itself. The caller's query
        then recomputes over the input's full history for exactly
        those keys, so a late-arriving older version of a touched
        key is considered by the recomputation even though the
        window itself only names the key. The fragment carries its
        own ``is_incremental()`` gate, so the first build - and any
        full refresh - computes over the whole input.

        ``conjunction=True`` renders the restriction as an ``AND``
        continuation instead of opening the WHERE clause, for a
        caller that already opened one (an instance `filter`,
        ADR 0038): the composition is a single WHERE - filter
        first, then this restriction ANDed behind it. The window
        subquery itself is unchanged: it names the touched keys
        over the unfiltered input, exactly as without a filter.
        """
        if spec.cursor_column is None:
            raise LoomError(
                "incremental affected-key filter requires a cursor_column"
            )
        bound = self._cursor_bound(spec)
        keyword = "AND" if conjunction else "WHERE"
        return (
            "\n{% if is_incremental() %}\n"
            f"  {keyword} {key_column} IN (\n"
            f"    SELECT DISTINCT {key_column}\n"
            f"    FROM {input_ref}\n"
            f"    WHERE {spec.cursor_column} >= {bound}\n"
            "  )\n"
            "{% endif %}"
        )

    def _affected_grain_filter(
        self,
        spec: IncrementalSpec,
        *,
        grain_columns: Sequence[str],
        input_ref: str,
        source_alias: str,
    ) -> str:
        """Affected-grain restriction (gold recomputation, §5.3).

        The composite-key analogue of ``_affected_key_filter``:
        an ``EXISTS`` correlation over every grain column (the
        §2.3 composite-key discipline - a tuple ``IN`` is not
        portable SQL) against the driving input, gated on the
        rows inside the cursor window - the flavour-B bound
        applied to the driving input itself. The caller's
        aggregation then recomputes over the full current inputs
        for exactly the touched grains: joined inputs are read
        whole for those grains, only the grain set is windowed.
        ``source_alias`` is the alias the driving input carries in
        the caller's query (gold aliases every input by model
        name), so the correlated grain references resolve to the
        caller's rows. The fragment carries its own
        ``is_incremental()`` gate, like the key filter.
        """
        if spec.cursor_column is None:
            raise LoomError(
                "incremental affected-grain filter requires a cursor_column"
            )
        if not grain_columns:
            raise LoomError(
                "incremental affected-grain filter requires a grain"
            )
        bound = self._cursor_bound(spec)
        conditions = " AND ".join(
            f"__window.{column} = {source_alias}.{column}"
            for column in grain_columns
        )
        return (
            "\n{% if is_incremental() %}\n"
            "  WHERE EXISTS (\n"
            "    SELECT 1\n"
            f"    FROM {input_ref} AS __window\n"
            f"    WHERE {conditions}\n"
            f"      AND __window.{spec.cursor_column} >= {bound}\n"
            "  )\n"
            "{% endif %}"
        )

    @staticmethod
    def _check_incremental_strategy(
        spec: IncrementalSpec, dialect: Dialect, *, model_name: str
    ) -> None:
        """Generate-time dialect gate (design §1.6(c), §6).

        Raises LoomError naming the model, the strategy, the dialect
        and the strategies that dialect supports among the
        v1-admitted set. Runs at generate time only: validation is
        dialect-free, so the same metadata must validate identically
        against every dialect.
        """
        if spec.strategy in dialect.incremental_strategies:
            return
        supported = ", ".join(
            strategy
            for strategy in INCREMENTAL_STRATEGIES
            if strategy in dialect.incremental_strategies
        )
        raise LoomError(
            f"model {model_name!r}: incremental strategy "
            f"{spec.strategy!r} is not supported by the "
            f"{dialect.name!r} dialect; {dialect.name} supports: "
            f"{supported}"
        )

    @abstractmethod
    def render(self, params: dict, dialect: Dialect, output_path: Path | None) -> str:
        """Render SQL for the given parameters and dialect. Returns the SQL text."""
        ...

    @abstractmethod
    def render_tests(
        self, params: dict, dialect: Dialect, output_path: Path | None
    ) -> str | None:
        """Render dbt data tests. Returns test YAML text or None if no tests."""
        ...
