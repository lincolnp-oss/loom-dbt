# ADR 0005: Distribute Loom as an authoring-time package

Status: accepted. Implemented for local packaging; the package is MIT-licensed, and publication is an owner act performed manually from the public release repository (`loom-dbt`) - this record states the distribution decision, not a release announcement. Terminology follows [ADR 0023](0023-loom-and-dbt-terminology.md) and the [canonical glossary](../terminology.md); see the [README installation path](../../README.md#install) and [current package boundary](../architecture.md#contributor-workflow).

## Context

A source-tree `python -m loom` invocation does not prove a wheel can be installed in an independent consumer environment. Loom must remain usable without bundling a dbt example project.

## Decision

Build wheel and source distribution with a `loom` console script. Install the artifact in an isolated Python environment separate from the consumer project and prove its commands work outside this repository. Consumers own `metadata/`, `local/registrations/`, and generated SQL/YAML; their dbt environment does not need the Loom package. dbt and DuckDB stay optional development/test dependencies rather than base package dependencies.

Use Astral's standard [`src/loom/` layout and `uv_build` backend](https://docs.astral.sh/uv/concepts/build-backend/) for the pure-Python wheel; keep pytest and dbt-duckdb in local dependency groups, not installable extras. This isolates source imports from package acceptance and keeps package metadata focused on runtime requirements.

Snapshot adapter evidence is limited to the combinations listed in the [current compatibility record](../architecture.md#compatibility-and-evidence); the snapshot remains a dbt runtime resource, not a detached MERGE.

## Consequences

Building and installing a wheel/sdist in a clean environment is a required acceptance gate; source-tree imports are insufficient. Publishing to a hosted index, testing another adapter, and adding a second SCD2 materialization require separate decisions and tests.
