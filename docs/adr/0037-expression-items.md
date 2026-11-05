# ADR 0037: Expression items - verbatim SQL as a column-item form

Status: Accepted

## Context

One parameter in the whole vocabulary carried an expression: the gold/mart measure (`aggregation.measures[].expression`), a verbatim SQL string rendered under its alias and never evaluated by Loom. Everywhere else, a computed column had exactly one sanctioned route - the staging macro slot - and convert named plain-SQL expressions (a staging model's `amount / 100 as amount`) as blockers, routing the model to authoring, where the practitioner then had no slot for the computation either: the pi-session review (2026-11-05) found the agent cycling through invented keys (`expression` among them) because the vocabulary genuinely lacked the item.

The owner's ruling on the finding: pure SQL expressions in instance metadata are a vocabulary oversight regardless of conversion yield - they align with dbt best practice for staging, where a plain projection computation is ordinary SQL, not a macro. The same item was extended in the same ruling beyond staging, to every surface whose items name an output column: view `select_columns`, gold/mart `aggregation.group_by` dimension items, and (in the follow-on PR) the silver/obt `columns` name lists. Fact, dimension and scd2_dimension were deliberately left out - their column lists are key/attribute declarations with their own semantics, and adding computed items there is a separate judgement call.

Two design questions rode with the decision. First, whether expression identifiers should be validated against the model's documented input columns (cheap on single-source staging, and it would catch typos at validate time): ruled NO (ruling R3) - expressions are practitioner-asserted verbatim carriage, exactly the gold/mart measures precedent; staging does not become stricter than the existing expression surface. Second, whether the vocabulary work needs a SQL parser (sqlglot) at runtime - for identifier validation, structural checks, or later fit drafting: sqlglot stays a development dependency, and the runtime-dependency decision is recorded here as considered and deferred to any future fit-drafting or macro programme, where structural expression handling would actually be decided. Nothing in this vocabulary parses expressions structurally.

## Decision

One item grammar, uniform on every surface that admits it:

- `expression` is a non-empty verbatim SQL string, carried and rendered exactly as written. Loom never evaluates, rewrites, or identifier-checks it.
- The item is mutually exclusive with `source`: an item carries one or the other, never both.
- `alias` is required wherever `expression` appears; the alias is the item's output column name on every surface.
- Jinja delimiters (`{{`, `}}`, `{%`, `%}`, `{#`, `#}`) and statement terminators (`;`) are banned at validation - an expression is plain SQL; templated SQL belongs to the macro slot.
- The staging macro slot combines only with `source`: a macro transforms a source column, so `macro`/`macro_args`/`macro_column_param` on an expression item is a validation error.

Per-surface forms:

- **Staging `columns`**: `{expression, alias}` joins `{source, alias, ...}` in the existing mapping list. The renderer emits `expression AS alias` in the projection. Expression outputs are not source columns: source-relationship checks and declared-unit-test given rows key on `source` items only.
- **View `select_columns`**: `{expression, alias}` is an alternative to `{source, alias}`; the expression may reference the view's input aliases like any select item.
- **Gold/mart `aggregation.group_by`**: an item may be a string (unchanged) or `{expression, alias}`. The renderer emits `expression AS alias` in the select list and the expression itself in GROUP BY; the item's grain key for incremental purposes is its alias.
- **`measures` is not renamed.** Role names carry the aggregate-context guardrail, and the `expression` key remains the single shared mechanism name under it.

Fit drafting is unchanged: convert still names expression-bearing models as blockers for fit purposes. This ADR is vocabulary only - it gives authoring the slot; it makes no equivalence or drafting claim about expressions.

## Extension (2026-11-05): the Kimball name lists

A follow-up vocabulary-gap analysis after the pi-remediation programme found the ADR's tail to be the largest remaining derivation gap: dimension `attribute_columns` and fact `measure_columns` were still name-only lists while the other item surfaces carried expressions. The owner ruled exactly these two surfaces in, pre-Alpha:

- **Dimension `attribute_columns`** and **fact `measure_columns`**: an item may be a column-name string (unchanged), a `{source, alias}` rename, or an `{expression, alias}` item under the unchanged uniform grammar. The rename form is accepted because the shared name-list machinery built for silver/obt carries it at no extra design cost - one grammar, stated once - and existence validation follows the standing split: a rename's `source` is checked against the input's output columns like any string item, while an expression item's identifiers are practitioner-asserted and never checked.
- Fact measures are honoured uniformly: all three fact variants (transaction, periodic_snapshot, accumulating_snapshot) share the renderer's single projection, so expression and rename measures need no variant-specific semantics; the generated `not_null` measure tests name the item's output column (its alias for mapping items).
- **The scd2/satellite exclusion stands.** scd2_dimension `tracked_columns` and satellite `attribute_columns` stay name-only: those lists feed change detection (SCD2 tracked-column comparison) and hashdiff computation (the satellite payload hash), so a computed item there is a separate design question, not a projection choice. Mapping items on both surfaces are rejected at validation. (The satellite half of that rejection was previously unenforced - a mapping item validated and then misrendered - and is enforced from this extension.)

## Consequences

- The architecture's stance is restated accordingly: metadata does not EVALUATE arbitrary SQL or expressions - it now CARRIES one verbatim expression form, under the bounds above, on the surfaces listed. The no-raw-SQL property of the macro slot is untouched.
- A practitioner can author what convert still refuses to draft. That asymmetry is accepted and stated (it predates this ADR in the measures precedent: convert has never drafted measure expressions into fits either); draft enrichment for convert's records is a separate lane's work.
- Output-column derivation treats an expression item's alias as its output column on every surface, so properties, downstream input validation, and unit-test expect rows see the computed column by name; its type is practitioner-data-defined, like any source column's.
- Any future fit-drafting or macro programme inherits the sqlglot question from this record rather than re-opening it cold: the dependency decision belongs to the programme that would parse expressions structurally.
