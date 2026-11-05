# Architecture decision records - start here

This directory holds Loom's architecture decision records (ADRs): the durable design decisions, each with the context that produced it. There are 26 records, numbered 0003–0038 with gaps: 0001, 0002 and 0004 were never used, and nine further numbers (0012, 0013, 0014, 0016, 0017, 0021, 0022, 0027, 0033) belong to records removed during development - numbers are never reused or renumbered, because code and documentation cite them.

Each record states its complete current decision, so no reader has to assemble a chain across increments: where a decision was extended by a later one, the substance lives in the record that now governs it - the satellite record (0028) covers all five variants, and the Data Vault hardening record (0030) states the complete link contract.

You do not need to read them in numeric order. Start from the question you actually have:

## “Why should I trust the generated code?”

| ADR | What it decided |
| --- | --- |
| [0003](0003-check-generated-artifacts.md) | Checking is split: `generate --check` answers drift, `loom check` answers inventory coverage |
| [0005](0005-installable-authoring-cli.md) | Loom ships as an installable authoring-time package, never a dbt runtime dependency |
| [0006](0006-native-dbt-source-staging.md) | Generated staging reuses native dbt sources |
| [0015](0015-contract-derived-dbt-data-tests.md) | Data tests are derived from generator contracts |
| [0029](0029-metadata-declared-dbt-unit-tests.md) | Tests: Loom originates paradigm guarantees and hosts practitioner declarations (generic-test carriage, metadata-declared unit tests) |
| [0032](0032-opt-in-enforced-model-contracts.md) | Enforced model contracts are opt-in only - never by default |
| [0037](0037-expression-items.md) | Expression items: one verbatim-SQL column-item form, carried never evaluated |
| [0038](0038-filter-parameter.md) | The filter parameter: one verbatim row predicate on the single-input generators |

## “How do paradigms coexist without a methodology being imposed?”

| ADR | What it decided |
| --- | --- |
| [0007](0007-coexisting-modeling-patterns.md) | Modeling patterns coexist, inventory-first |
| [0018](0018-grouped-instance-only-authoring.md) | One authoring format: named pattern definitions + instance sets (incl. staging column macros) |
| [0026](0026-practitioner-controlled-output-layout.md) | Output layout is practitioner-controlled, never taxonomy-imposed |
| [0023](0023-loom-and-dbt-terminology.md) | Loom authoring terms and dbt resource terms are kept distinct |

## “How would I adopt this on a real project?”

| ADR | What it decided |
| --- | --- |
| [0011](0011-consumer-aware-migration.md) | `loom plan` inventories a model's consumers before it is replaced |
| [0025](0025-migration-pathway.md) | The adoption pathway is assessment-first and proof-gated |
| [0019](0019-opt-in-local-source-inspection.md) | Source inspection is explicit, opt-in and read-only |
| [0020](0020-source-schema-baseline.md) | Source-structure baselines are explicit and advisory |
| [0024](0024-inline-source-completion.md) | Inline sources are completed by generating their declaration |

## Generator internals

Read these when a specific generator's contract is the question: [0008](0008-first-customer-hub.md) first hub contract · [0009](0009-opt-in-hub-retention.md) opt-in identity retention · [0010](0010-audited-hub-provenance.md) audited hub provenance · [0028](0028-satellite-generator.md) the satellite generator, all five variants · [0030](0030-dv-core-hardening.md) the complete link contract, multi-source hubs, n-ary links · [0031](0031-kimball-fact-flavours-and-scd-types.md) fact flavours, SCD Types 3/6 · [0034](0034-reference-constructs.md) reference hub / satellite / table · [0035](0035-pit-snapshot-control-ghost-records.md) PIT tables, snapshot control, ghost records · [0036](0036-bridge-tables.md) bridge tables over the link graph.

Each record stands on its own. Behaviour is documented in [`../architecture.md`](../architecture.md) - where an ADR and the architecture document disagree, the architecture document describes what the code does today and the ADR records why it was decided.
