# ADR 0020: Explicit advisory source-structure baselines

Status: accepted

## Context

Read-only source inspection (ADR 0019) can show the current physical schema, but cannot show what changed since an earlier review. dbt's manifest records resolved source relations, not a durable previous physical-schema snapshot. Automatically capturing a baseline during inspection or a normal `loom check` would hide when the accepted source contract changed.

## Decision

Provide separate `loom snapshot-sources` and `loom diff-sources` commands. Capture requires a successful complete inspection of a project-local DuckDB database and writes only a JSON baseline - project identity plus, per source, its identity and physical column names, types and ordinals - in an envelope of exactly two keys, `{project, sources}`. The default `target/loom/source-structure.json` path may be overridden, but the selected path must be inside the project and Git-ignored. Capture is atomic and refuses to overwrite; deliberate recapture requires explicit user removal first.

Compare reads that baseline plus a newly inspected local DuckDB database and reports source additions/removals or location changes, column additions, removals, type changes and order changes. It includes current staging-model impact for each changed raw column. A potential rename is reported as remove/add; no identity inference is made. Schema differences are advisory and the command succeeds after producing the report. Invalid baselines, stale manifests, missing sources and inspection failures remain explicit errors. Compare never modifies its baseline, metadata, dbt artifacts or the database. Routine validation, generation and `loom check` remain offline and do not read or update this baseline.

The baseline contains schema metadata only - no sampled rows or values. It is not a version-control decision or an assertion that changed metadata is safe: reviewers may retain an ignored local baseline or intentionally capture a replacement after reviewing the change. The default under `target/` is removed by `dbt clean`; preserving it across cleans requires an explicit copy to another Git-ignored project-local path.

Two naming notes, because both have bitten before: the baseline is a *source-structure snapshot* in Loom's terminology - "snapshot" here never means a dbt snapshot resource - and the envelope carries no format version, by the same no-format-versioning rule as every Loom document (ADR 0018). Earlier envelopes carried one; they are not read, and the remedy is the same as for any invalid baseline: recapture.
