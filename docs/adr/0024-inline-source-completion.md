# ADR 0024: Complete inline_source by generating its source declaration

Status: Accepted

## Context

Staging and bronze instances can declare their input as an `inline_source` mapping (`name`, optional `schema`, `table`) instead of referencing an authored dbt source. The generated model SQL calls `source(name, table)` - but Loom never emitted the source *declaration*, so an inline-source project did not build unless the practitioner also hand-wrote the same declaration elsewhere, duplicating the metadata Loom exists to declare once. The input was left in this ambiguous state; the plan required deciding it: complete the input or retire it.

Retiring was unattractive: bronze has no `source_ref` alternative, and inline declaration is the convenient form for small projects and tests. The ambiguity, not the input, was the defect.

## Decision

Complete `inline_source`:

- `loom generate` emits the dbt source declarations for every inline source that has **no authored declaration**, grouped by source name, into a Loom-owned file, `models/_generated/staging/sources.yml` (under the configured model root). The file carries the Loom provenance header, joins the generation plan like any other output, and is therefore covered by the ownership preflight and the drift check.
- The document is derived from **all** inventory entries, never just the current `--select` selection, so a partial regeneration cannot shrink it and strand unselected models.
- An authored declaration for the same (source, table) **governs**: generation does not re-emit it. This preserves the composed shape where bronze instances read the same authored sources as staging (the quickstart does this). Staging keeps its existing stricter rule that an authored source *name* is referenced via `source_ref`, never declared inline.
- Two inline declarations of one not-authored source name with different `schema` values are a validation error: one source name has one schema in the generated document.
- Validation's authored-source scan skips files carrying the Loom header, so the generated document never counts as authored (which would make every inline source collide with itself).

## Consequences

An inline-source-only project now builds end-to-end with no authored sources file; this is proven by an integration test that seeds, builds and queries such a project. Switching a project entirely to `source_ref` leaves the generated sources document behind as a stale output - consistent with generation's documented no-cleanup rule; delete it explicitly.
