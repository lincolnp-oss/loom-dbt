# ADR 0032: Opt-in enforced model contracts - never by default

Status: Accepted

## Context

ADR 0015 chose generated data tests for generator guarantees and scoped itself explicitly *away* from dbt's enforced-contract feature. Project prose (architecture, terminology, the migration playbook) hardened that scope note into a blanket "never emit contracts" stance. Three pressures made the blanket stance wrong:

- Descriptions and column types live in Loom metadata and reach the catalogue descriptively - but a project that needs enforcement (dbt Mesh-style public/versioned models *require* enforced contracts) still hand-maintains YAML that Loom regeneration can silently drift from.
- Migration assessment flagged enforcement loss on every contracted model converted to Loom, with hand-re-addition as the only remedy.
- Where Loom knows a model's types (its own fixed types, plus practitioner `column_types`), the tedious part of a contract - a `data_type` for every column - is already declared knowledge.

## Decision

Loom emits an enforced contract **never by default, only on explicit opt-in**: the envelope key `contract_enforced` (boolean), carried in pattern `defaults` or instance `params` with the instance winning - the `output_subpath` mould (ADR 0026), and the one mechanism that can express "enforced for this pattern, except this one model", since a generator param may not appear in both places. It is exempt from the generator contracts and never reaches a renderer.

For an opted-in model, the model's single schema document gains `config: {contract: {enforced: true}}`, a `data_type` for every output column, and `not_null` constraints only where the generator's data-test matrix already guarantees them (no `unique`, `primary_key`, `foreign_key` or `check` constraints - platform support for those is definable-but-unenforced on most adapters, and the data tests already carry those semantics honestly). `loom validate` fails an opted-in model whose output columns are not all typed, naming the model and the columns, and one whose outputs cannot be derived - authoring-time completeness is the core value over hand-layered YAML. The SCD2 view may be opted in for names/types; its business-key constraint is dropped with a validation warning (view contracts carry no constraints). The snapshot itself cannot be opted in. Data tests remain default-on and unchanged (ADR 0015's decision stands).

Contract types render from a per-dialect `contract_types` map on the `Dialect` record - the spellings the adapter *reports*, which is what dbt's preflight compares against, and which differ from the descriptive spellings (e.g. Postgres reports `character varying`, `timestamp without time zone`). Enablement is gated on runtime proof: DuckDB is proven; Postgres and ClickHouse ship derived maps and graduate when a live preflight proof runs; opting in on an unproven dialect warns at generation time rather than blocking.

This decision replaces the no-contracts stance wherever project prose had stated it - the architecture document (generator guarantees), the terminology glossary (the generator-guarantee definition) and the adoption playbook. ADR 0015's data-test decision is **not** superseded: it stands, with its scope stated against this record.

## Alternatives considered

- **Status quo / hand-layering** - rejected: drift and migration friction are measured, not hypothetical.
- **Enforcement by default** - rejected: it would break every existing project whose types Loom cannot know, and inverts Loom's stance that generation must be safe to re-run.
- **Contracts instead of data tests** - rejected: they answer different questions (shape preflight vs data behaviour), and the data-test matrix is load-bearing in the value statement.

## Consequences

- Generated models can serve Mesh consumers natively; the migration wedge strengthens (the assessment flag now names the Loom-native remedy and self-resolves once a converted model is built with enforcement).
- Envelope keys are confirmed as a standing category (generation-consumed, contract-exempt, instance-overridable); the defaults/params overlap rule exempts them, which is also what makes `output_subpath` instance-overridable (ADR 0026).
- Loom takes on a per-dialect type-rendering maintenance obligation tied to runtime-proof evidence; a practitioner who opts in on an unproven dialect owns a warned risk.
