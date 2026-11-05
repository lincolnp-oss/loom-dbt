# ADR 0034: Reference constructs - reference hub, reference satellite, reference table

Status: Accepted

## Context

Reference data (code sets: countries, statuses, units) is ubiquitous in Data Vault projects, but its keys are not enterprise business keys - they are externally governed codes, integrated from their own source, never arbitrated across systems. Without dedicated constructs, practitioners approximate reference data outside the vault with Kimball/3NF generators, splitting the model. Three constructs cover it: a reference hub, a reference satellite, and a reference table with latest/full/snapshot variants.

## Decision

Two new generators; the third construct reuses an existing one.

### `reference_hub` - a new generator

The reference hub is hub-shaped but deliberately distinct from `hub`, because the semantics differ in ways a hub variant would obscure:

- The name prefix is `ref_` (instances name code sets: `ref_country`); the hash column rule is the family's (`<name minus prefix>_hk`).
- Provenance is **mandatory**: `record_source` is a required parameter and every row carries the `__record_source` and `__loaded_at` columns. Reference data is governed data; its audit trail is not optional (hubs keep provenance optional for the general identity case).
- Retention is always persistent: the load is the hub's proven incremental anti-join form (Databricks merge included). Codes are never forgotten because an extract omitted them.
- The row carries the code itself (`business_key`) alongside its hash, as canonical reference hubs do - the reference table flattens from the hub without returning to staging.
- Its input is the one-entry `input_models` list, as for hubs.

### Reference satellite - the existing `satellite` generator

A reference satellite is a satellite whose parent is a reference hub. No new generator and no new variant: the satellite machinery (hub-parent hash derivation, the parent-key mirror validation, the load-time parent JOIN, all variants) accepts a `reference_hub` parent exactly as it accepts a `hub` parent - the `ref_` prefix is four characters like `hub_`, so hash-column derivation is unchanged. In practice reference satellites use the standard or non-historised variants.

### `reference_table` - a new generator

The flattened consumer output: the reference hub joined to **one** reference satellite (declared as the one-entry `satellites` list), projected as a dbt **view** in three variants (the `variant` parameter):

- `latest` (default): the satellite's latest row per code - `business_key` is unique in this variant.
- `full`: every satellite history row, flattened.
- `snapshot`: the latest row per code among rows loaded at or before a declared `snapshot_timestamp` (an ISO-8601 literal; static as-of - cadence-driven snapshot control is the PIT generator's (ADR 0035), which this variant composes with but does not duplicate).

The satellite's descriptive columns to project are declared (`attribute_columns`, mirroring the satellite's own list and validated against it), and `business_key` mirrors the reference hub's key, per the family's mirror-and-validate discipline. The flattened satellite must be a standard-variant satellite for `latest`/`snapshot` (its stored hash diff is the tie-break); `full` also accepts non-historised. Variants beyond one satellite (temporal joins across several satellites' timelines) are out of scope: a reference table flattens one code set's descriptions, and wider flattening is a mart/obt composition. The flattening shape was originally declared as a `mode` parameter naming a single satellite model; the shipped form is the family's shared spellings - the `variant` enum and the `satellites` list - with the same values and semantics.

## Consequences

- The vault family is complete for reference data: codes load into `ref_*` hubs, descriptions historise in satellites, and consumers read flat `reference_table` views.
- Two new registry entries (`reference_hub`, `reference_table`, paradigm `dv`); satellites gain a third accepted parent type. Existing generators' output is unchanged.
