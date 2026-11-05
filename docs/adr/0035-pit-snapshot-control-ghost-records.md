# ADR 0035: Point-in-time tables, snapshot control, ghost records

Status: Accepted

## Context

Point-in-time (PIT) tables turn temporal satellite joins into equi-joins: one row per (parent key, snapshot date) carrying, for each satellite, the load timestamp of the row effective at that date. Three questions arrive together: the PIT itself, snapshot control (where snapshot dates come from), and ghost-record support (what a PIT cell holds when a key has no satellite row at a snapshot). It is the family's largest design surface, and the cadence and ghost mechanics are the decisions that matter.

## Decision

A new generator, `pit`, one instance per parent (hub or link), declaring its satellites (`satellites`, a non-empty list of satellite instances on that parent) and its parent in the family's one `parent` block (ADR 0028).

### Snapshot control: the load calendar

Snapshot dates are the **distinct `__loaded_at` values across the declared satellites** - the loads the vault actually observed. No separate control model is generated and no calendar construct is introduced: a PIT exists to resolve satellite rows, and its useful cadence is exactly the loads those satellites took. This is the same stance taken for periodic facts (period completeness is an input contract; Loom fabricates no calendar spine) and the calendar the effectivity windows already derive from (ADR 0028). Period-end cadences are a downstream filter over `__snapshot_date`, not a generation concern.

### Ghost records: the sentinel, in the PIT

When a key has no satellite row at or before a snapshot, the PIT cell holds the **ghost load timestamp** `0001-01-01 00:00:00` (per-dialect literal via the `timestamp_literal_template` dialect field) instead of NULL, so PIT rows are complete and equi-joins stay total: a consumer joining a satellite on a ghost timestamp finds no row, which is the ghost's meaning - "no data yet". Satellites themselves do **not** grow ghost rows: that would rewrite every existing satellite's load for a consumer-side convention, and LEFT JOIN consumption needs no physical ghost. (Ghost semantics therefore live at the PIT layer, not in staging or in the satellites themselves; physical ghost rows in satellites remain available as a future opt-in if a consumer contract ever requires inner joins to succeed.)

### Shape

- The PIT is a dbt `table` (derived state, rebuilt each run): parent hash column, `__snapshot_date`, then one `__<satellite_name>_ldts` column per declared satellite, valued by a correlated latest-row resolution (`MAX(__loaded_at) ≤ __snapshot_date`, ghost-coalesced).
- Effectivity satellites are **excluded** from PIT declaration: their point-in-time answer is the windows view (ADR 0028), not a latest load timestamp. All other variants resolve.
- Name prefix `pit_` is structural, as with the family's other generators. Data tests: parent key and `__snapshot_date` not_null.

## Consequences

- Temporal consumption of the vault becomes equi-join based: hub/link → PIT → satellites on (key, `__<sat>_ldts`).
- One new dialect field (`timestamp_literal_template`) is paid by every future dialect, like the hash templates before it.
- Still out of scope: logarithmic PIT retention (pruning old snapshot rows - a retention policy, not a shape), calendar / period-end snapshot cadences, physical ghost rows.
