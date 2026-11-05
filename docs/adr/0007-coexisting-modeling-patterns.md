# ADR 0007: Catalog-first coexistence of modeling patterns

Status: accepted. The design direction recorded here is the project's standing stance, stated in current terminology (ADR 0023).

## Context

Loom began with projection/rename staging models and a DuckDB SCD2 snapshot/view. The draft design prescribed AutomateDV wrappers for future Data Vault models. Practitioners may instead need Data Vault, Kimball-style dimensions or normalized/3NF entities within the same evolving dbt project. These have different grains, identities, relationships and histories, so presenting them as interchangeable "semantic-layer adapters" would promise unsafe conversion. dbt's Semantic Layer for metrics and dimensions is a different product boundary.

## Decision

Keep inventory discovery and existing staged inputs common; define executable metadata and output contracts per modeling pattern. Name, purpose, owner, tags and dependencies support cross-pattern discovery without implying that a hub and an SCD2 dimension share a key, grain or migration mechanism. Support coexistence before proposing any conversion tooling. Add one paradigm at a time against a real customer example and focused tests. Do not build a formal modeling-adapter interface until two concrete implementations demonstrate a stable shared contract.

AutomateDV is **not** a Loom strategy or a required Loom/dbt dependency: generation is native, and no dbt package is required at runtime. Before native generation or an existing package is chosen for any new paradigm, define and test key normalization/hash consistency, duplicates, nulls, replay, change history, and adapter support; if a package is ever chosen, document its continuing dbt runtime dependency explicitly.

Changing an existing consumer-facing model is a migration: add the new model beside the old, identify dbt `ref()` and external consumers, test selected business invariants and output parity where meaningful, migrate consumers over a defined window, then retire the old model. Version breaking dbt model shapes when consumers need that window, keeping the old canonical/latest relation until migration is complete. Do not promise lossless one-click paradigm switching.

## Consequences

The stance is realised in the current product: one generator registry carries every paradigm (Data Vault, Kimball, medallion, 3NF, OBT and their compositions) under a shared inventory, and the quickstart showcase composes them in a single project. Paradigm is context an instance inherits from its generator, never a project-level commitment. The migration discipline above is carried into the consumer-aware adoption decisions (ADR 0011, ADR 0025) and the adoption playbook. dbt warehouse adapters and dbt Semantic Layer generation remain separate, independently scoped decisions - the boundary is about generation: the adoption assessment *reports* semantic-layer objects (naming the metrics and saved queries a model reaches, ADR 0025) without generating or converting any, and that reporting does not move this boundary.
