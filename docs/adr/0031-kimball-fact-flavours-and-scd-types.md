# ADR 0031: Kimball completion - fact flavours and SCD Type 3 / Type 6

Status: Accepted

## Context

Loom's Kimball coverage is a working star subset with named holes: the `fact` generator renders exactly one flavour (a transaction-shaped projection), two of Kimball's three fundamental fact grains - periodic snapshot and accumulating snapshot - are absent, and SCD Types 3 and 6 are absent despite being small builds on the runtime-proven `scd2_dimension`. There is also a discoverability gap: the `dimension` generator's shape *is* SCD Type 1 but nothing names it, and factless/degenerate shapes exist unnamed.

Two boundaries shape this design. First, accumulating facts are canonically defined by in-place milestone updates, and general incremental loading is a separate, unbuilt capability: these flavours and types are built on the current full-rebuild renderers, and must not smuggle in a merge renderer. Second, Loom's established discipline for generator families is one parameterised generator with a closed `variant` enum (satellites, ADR 0028; links, ADR 0030), not a generator per flavour.

## Decision

### Fact flavours: a `variant` enum on `fact`

`fact` gains `variant`: `transaction` (default), `periodic_snapshot`, `accumulating_snapshot`. The default renders byte-identical SQL and tests to the original generator.

A flavour is a *grain contract*, in the same spirit as the transaction fact's existing contract that dimension keys must already be warehouse keys upstream:

- `periodic_snapshot` requires `period_key`, naming the dimension key that identifies the period (validated as one of `dimension_keys`). The input must already carry one row per key combination per period - period completeness ("a row exists whether or not activity occurred") is an input contract, not something the renderer fabricates: generating a period spine needs a calendar construct Loom does not have, and the measure for an empty period (null vs zero) is practitioner policy, not a default Loom may choose silently. Semi-additive measure behaviour is a query-time property and is documented as such.
- `accumulating_snapshot` requires `milestone_keys`, a non-empty list drawn from `dimension_keys`: the milestone date keys of the process the fact tracks (ordered, shipped, delivered…). Its rows are one per process instance. The load-semantics consequence that *is* expressible on a full rebuild lands in the generated tests: milestone keys are legitimately null for in-flight instances, so they are exempt from the `not_null` test every other dimension key and measure carries. The canonical update-in-place load is explicitly **not** built here - it belongs to a future incremental-loading capability, and the flavour's metadata (grain, milestone keys, nullability) is the contract that capability will consume.

`period_key` is rejected on the other variants; `milestone_keys` likewise (the payload-on-standard-link precedent, ADR 0030).

### SCD Types 3 and 6: a `variant` enum on `scd2_dimension`

`scd2_dimension` gains `variant`: `type_2` (default), `type_3`, `type_6`. The generated dbt snapshot is identical for all three - the variants change only the dimension model generated over it:

- `type_3` emits a current-state dimension: business key, tracked columns, a `previous_<column>` for each column named in the new `previous_value_columns` list (required, non-empty, a subset of `tracked_columns`), `__valid_from`, `__is_deleted`. Previous values are computed with `LAG(...) OVER (PARTITION BY <key> ORDER BY dbt_valid_from)` over the full snapshot history, then filtered to current rows - so "previous" means the preceding held row, including across a deletion tombstone.
- `type_6` emits the full Type 2 history plus the Type 1 and Type 3 elements: a `current_<column>` for **every** tracked column (the value held by the key's current row, via a self-join to the current rows) and `previous_<column>` for each `previous_value_columns` entry (required, as for Type 3 - the hybrid is 1+2+3, so all three elements are present or the variant is misnamed).

Generated `previous_*`/`current_*` names must not collide with the business key, a tracked column, or each other; validation fails explicitly when they do. Tests follow the shape: Type 3 tests the business key `unique` outright (one current row per key), Type 6 keeps Type 2's current-row uniqueness form.

### Discoverability (docs, no build)

The architecture document now names the `dimension` generator's shape as SCD Type 1, names factless and degenerate-dimension composition where facts are described, documents Type 4/5 mini-dimension composition (a `dimension` over the rapidly changing attributes, its surrogate key carried in the fact's `dimension_keys` - key carriage stays practitioner-declared, no new construct), and records snowflaking as a deliberate non-goal (Kimball's own guidance discourages it; outriggers compose via `view`/`gold`).

## Consequences

- Existing fact and SCD2 declarations are untouched and render byte-identical output; flavours are opt-in per instance.
- A future incremental loader inherits declared grain contracts for facts instead of inferring them; the accumulating flavour's merge semantics remain that capability's explicit scope, not this record's.
- Still absent, deliberately: period-spine generation, Type 0/4/5/7 constructs, conformed-dimension declaration, role-playing constructs, late-arriving handling - each a documented composition or a separate decision, not a silent gap.
