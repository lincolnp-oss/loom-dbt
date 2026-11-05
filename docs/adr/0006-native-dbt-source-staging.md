# ADR 0006: Reuse native dbt sources for generated staging

Status: accepted. The inline-source half of this record was delivered by ADR 0024; this record states the complete current decision.

## Context

The first SCD2 flow required a hand-written `stg_customers.sql`. Requiring practitioners to write staging SQL for a supported pattern defeats the objective of reducing the authoring surface. dbt already owns raw source configuration; copying it into another mandatory Loom registry would add redundant work.

## Decision

Staging metadata describes searchable purpose/ownership and an ordered raw-to-output column mapping, and renders a staging SELECT using dbt `source()`. It reaches its source two ways:

- **`source_ref`** points to a pre-existing native dbt `sources:` declaration, which remains practitioner-authored. When the native source declares columns, mapped source columns must exist in that declaration; when columns are not declared, dbt validates against the physical table at build.
- **`inline_source`** declares the source inside the staging metadata, and generation completes it (ADR 0024): the declaration is emitted, grouped by source name, into the Loom-owned `models/_generated/staging/sources.yml`, derived from all entries (never a `--select` subset) and covered by the ownership preflight and the drift check. An authored declaration for the same (source, table) governs and is not re-emitted; conflicting inline schemas for one not-authored source name are a validation error.

The transformation is projection and rename only (plus the staging column macros of ADR 0018). Source lookup is static across dbt model paths. Extra physical columns do not change emitted SQL; a missing/renamed mapped column fails at build. No live introspection, connection or automatic source migration is part of staging validation or generation - inspection exists only as the separate, opt-in, read-only surface of ADRs 0019–0020.

## Consequences

The consumer supplies raw data and a dbt profile, and owns any native source declarations they author; Loom owns only the declarations it generates for inline sources. Generated staging and generated source declarations carry the Loom provenance header, are protected by the overwrite preflight, and are drift-checked by `loom generate --check`. Bespoke hand-written models remain registrable alongside.
