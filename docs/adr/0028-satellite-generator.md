# ADR 0028: The satellite generator - variants, hash diffs, load semantics

Status: Accepted

## Context

Loom's Data Vault support began with hubs and links: the vault could hold identities and relationships but no descriptive attributes at all, and a source delete was invisible to it. That was the blocking Data Vault gap, and one parameterised `satellite` generator closes it in preference to a family of separate generators: five variants - standard, non-historised, record-tracking, multi-active and effectivity - under one parameterisation discipline. Point-in-time and bridge tables are separate records (ADR 0035, ADR 0036).

The grain, loading, provenance, retention, deletion and concurrency contracts had to be settled as one design, because each constrains the others; this record states them.

## Decision

### One generator, a closed variant enum

A single `satellite` generator takes a `variant` parameter validated against a closed set of five - `standard` (the default), `non_historised` and `record_tracking` stated first, then `multi_active` and `effectivity` below - following the hub `retention` precedent of one renderer with an internal branch. Instance names carry the `sat_` prefix, enforced in validation like `hub_`/`link_`, because downstream naming (the current-state view is `<name>_current`) is derived from it.

A satellite hangs off exactly one parent, declared in the one `parent` block: `{model, key}` for a hub parent, where `key` names the staging column carrying the hub's business key (validation requires it to equal the hub's `business_key`); `{model, endpoints}` for a link parent, its endpoint list mirroring the parent link's own declared endpoints, with `dependent_child_key` carried when the parent link declares one. Its payload comes from the one-entry `input_models` list, which must be generated staging - the same staging discipline links already require - so every key and payload column is a validated staging alias. Validation requires the parent block to match the parent hub's or link's declared contract exactly. Parentage was originally declared field by field (a parent model, a parent key, per-endpoint parent fields); the block replaced those spellings without changing the semantics or the generated SQL, because a parent is one fact and reads as one declaration.

### Grain

One satellite row is one observation of one parent key's payload at one load. The input grain is one row per parent key per load - the grain of the staging model the parent hub or link itself consumed. A staging extract carrying several rows per key in one load is outside the contract (multi-active satellites, below, are the construct for that grain).

### Loading

Satellites are append-only. All variants generate a dbt `incremental` model with the `append` strategy - the hub's execution-proven form - guarded by `is_incremental()` anti-joins against the model's own prior state (`{{ this }}`):

- **standard** inserts an input row when its hash diff differs from the *latest* row held for that parent key (or when no row is held). The latest row is resolved with a `ROW_NUMBER()` window ordered by `__loaded_at` descending, `__hash_diff` descending as a deterministic tie-break.
- **non_historised** inserts an input row when no identical (parent key, payload) row is already held, decided by comparing hash diffs recomputed over the held rows' payload columns. A changed payload is a new row; the variant keeps no history semantics beyond that - it is the immutable-event shape.
- **record_tracking** holds no payload. It inserts an appearance row (`__is_deleted` false) when a parent key is present in the input but not currently active for the instance's record source, and a deletion row (`__is_deleted` true) when a currently active key is absent from the input. The deletion row's `__loaded_at` is the applied-deletion timestamp: when Loom's load observed the absence, not when the source deleted the row.

`dbt build --full-refresh` deliberately resets a satellite to the current extract, as with persistent hubs. Loads assume one writer and retained database state; there is no concurrent-writer guarantee.

The satellite joins its input to the parent hub/link on the computed parent hash, so a satellite never *inserts* a key its parent does not hold at load time. Input rows whose key has no parent row are excluded by that join; unlike links (ADR 0030), satellites carry no fail-fast guard - the parent's own loading from the same staging extract is the ordering guarantee (dbt builds the parent first, via `ref()`).

Satellites deliberately carry **no `relationships` data test** to the parent. The lifecycle runtime proof showed why: a `retention: current` hub is rebuilt per extract and forgets identities that leave it, while append-only satellites correctly retain those identities' history - satellite rows legitimately outlive their parent rows, and a relationships test fails on correct history in exactly that situation. Referential permanence is the persistent hub's contract; practitioners who need satellite keys to always resolve to a hub row pair the satellite with a persistent parent.

### Hash diffs - a new dialect surface, renderer-owned

Change detection needs a deterministic hash over the payload columns. Following the rule that a dialect owns one complete template producing the value, `Dialect` gains a `hash_diff_template`: the dialect's SHA-256 wrapper over a `{concat}` placeholder. The canonical string being hashed is built by the renderer: each payload column is cast to the dialect's key text type (the hub key cast), `NULL` is replaced by the literal token `~`, and the values are joined with `|` (via the dialect's variadic `concat`, except Redshift, whose `CONCAT` takes two arguments and which therefore uses the `||` operator). Column order is the declared `attribute_columns` order; payload values are hashed as stored - no case folding or trimming, because attributes are case-significant (unlike business keys). A payload value that is exactly `~` after casting is indistinguishable from `NULL`; that collision is accepted, as in canonical Data Vault practice.

Hash diffs are computed in the satellite renderer, not in staging: staging stays deliberately thin, and hashing already lives in the hub/link renderers. Standard satellites store the hash diff as a `__hash_diff` column; non-historised satellites compute it transiently for the load decision and do not store it.

### Provenance - mandatory, unlike hubs

A satellite cannot function without load timestamps, so provenance is not opt-in here (the hub's stance, ADR 0010, does not carry over). Every variant requires `record_source` and emits `__loaded_at` (the dialect's current timestamp, evaluated by the load run) and `__record_source` on every row.

### Outputs

A standard satellite produces two outputs from one instance, following the `scd2_dimension` two-output precedent: the historised table (the instance's own name) and a current-state view named `<name>_current`, exposing the latest row per parent key with the table's columns except `__hash_diff`, which is load mechanics, not data. Non-historised and record-tracking satellites produce the single table only; neither has a history/current distinction to expose. The catalogue resolver (`loom.output_columns`) describes the table's columns; the current view is a convenience output for direct SQL consumers, as Data Vault tooling's v1 views are.

### Retention and deletion

Satellites retain everything they append until a full refresh; there is no retention window parameter. Deletion of a parent entity is represented only by the record-tracking variant: hub and standard-satellite rows are never removed or end-dated by a source delete (end-dating a relationship is the effectivity variant's job, below).

### Multi-active and effectivity variants

The variant enum is closed at five: the three above plus `multi_active` and `effectivity`, both variants of the one `satellite` generator, under the same parameterisation discipline. For effectivity, the design choice between classic date windows and an `is_active` flag was ruled for **date windows**, for vocabulary consistency with Loom's SCD2 validity columns.

**Multi-active** is a standard satellite whose grain gains a sub-sequence: several attribute rows per parent key per load, distinguished by a declared `subsequence_key` (a staging column - a phone type, an address type, an ordinal), required alongside `attribute_columns` and rejected on every other variant. The stored hash diff is unchanged (payload identity within the sub-sequence); the load's latest-row resolution and anti-join partition by (parent key, subsequence key), and the variant emits the `<name>_current` view like standard, with latest-row resolution and the standard tie-break (`__loaded_at` descending, `__hash_diff` descending) within each sub-sequence.

**Effectivity** is a satellite on a link parent only - it end-dates relationships. It carries no payload: `attribute_columns` is rejected, and `driving_key` is required, naming the staging column of the driving endpoint's business key (one of the parent link's endpoint key columns). One endpoint drives: the relationship set of a driving key is what each load enumerates, and each load's input enumerates the full current relationship set, so absence from a batch is meaningful. The table is a presence log - one row per (link hash, driving hash) per load batch in which the relationship is present, carrying the link hash column, the driving endpoint's hash column, `__loaded_at` and `__record_source` - loaded by the family's usual incremental append of the staged presence set, the input joined to the parent link. The windows are derived in a generated `<name>_windows` view: one row per maximal run of consecutive load batches in which the relationship is present, with `__valid_from` the first batch's load timestamp and `__valid_to` the first later batch in which it is absent (`NULL` while still effective), derived gaps-and-islands over the satellite's own load calendar (the distinct `__loaded_at` values it holds). A batch in which no relationship at all is present leaves no calendar trace, so a window can only close at a batch the satellite observed; this limit is documented, not hidden. The variant has no `__hash_diff` and no `<name>_current` view - the windows view is its derived output, and the point-in-time tables (ADR 0035) consume its (link hash, `__valid_from`, `__valid_to`) shape.

Both variants keep the family rules: `__record_source` and `__loaded_at` on every row, the load-time parent join as the integrity mechanism, and no generated `relationships` test. Existing declarations of the core variants render byte-identical output.

## Consequences

- Data Vault is claimable for hub/link/satellite models with the documented limits: hierarchical links are rejected (ADR 0030); point-in-time tables, bridge tables and reference constructs are their own records (ADR 0035, ADR 0036, ADR 0034).
- Every future dialect owes one more template (`hash_diff_template`) next to `hash_template`, with composed-expression parse coverage.
- The satellite is a two-output generator in its standard and multi-active variants; the current-state view's file is written next to the table's under the same output subpath. The PIT generator (ADR 0035) declares its parent in the same `parent` block.
