# ADR 0015: Contract-derived native dbt data tests

Status: accepted

This record is the origination half of the tests principle (ADR 0029): the data tests Loom itself creates, derived from generator contracts. Its scope is generated *data tests* only - it was never a decision about dbt's enforced model contracts, although project prose once hardened its scope into a blanket "never emit contracts" stance. ADR 0032 settles that question separately - enforced contracts are emitted on explicit opt-in (`contract_enforced`), never by default - and leaves this record's data-test decision (default-on, no opt-out) fully in force. Generated tests are default-on and cannot be suppressed; see [current generated tests](../architecture.md#generated-dbt-tests).

## Context

Generated model patterns encode a small number of structural guarantees that are worth carrying into the consuming dbt project's normal test graph. Loom must not infer primary keys or non-null guarantees from raw data or a staging projection that declares only source-to-alias mappings.

## Decision

Loom emits dbt schema YAML at `models/_generated/tests/data_<model>.yml` when a supported generator implies data tests. These are native dbt `data_tests`; Loom does not create custom test SQL or modify generated model SQL. (Unit-test fixtures are a separate, declaration-driven mechanism - ADR 0029; nothing here generates them.)

The default matrix is:

| Generator | Column | Tests |
| --- | --- | --- |
| `hub` | `<hub-name-without-hub_>_hk` | `unique`, `not_null` |
| `link` | `<link-name-without-link_>_hk` | `unique`, `not_null` |
| `link` | Each endpoint's `<hub-name-without-hub_>_hk` | `relationships` to that endpoint hub's hash |
| `scd2_dimension` | `business_key` | `not_null`; `unique` where `__valid_to is null` |
| `fact` | Dimension keys and measures | `not_null` |
| `dimension` | Natural key | `not_null`, `unique` |
| `dimension` | Surrogate key | `unique` |
| `silver` | Dedup key | `not_null`, `unique` |
| `gold` | Measure aliases | `not_null` |
| `normalized_table` | Entity columns | `not_null` |
| `mart` | Measure aliases | `not_null` |

The hub and link hashes are the generated identity keys. Link relationship tests point at each endpoint hub's generated hash column. The dimension's natural key is unique-tested as well as not-null because the renderer assigns surrogate keys by row number over the natural key, so a duplicate natural key would otherwise produce duplicate dimension rows silently. SCD2 snapshots reject null and duplicate source business keys and expose `dbt_valid_to`; therefore a non-null history key and at most one open/current row per key are part of this pattern's supported contract. These SCD2 tests do not assert business-specific tracked-column values.

Staging and generated source declarations receive no default tests. Their contracts list source-to-alias mappings and source columns but do not declare primary keys, uniqueness, or nullability; a mapping does not prove source uniqueness or completeness. There is no metadata field that guarantees a staging key or source-column invariant, so Loom *originates* no staging or source-column tests. That is a statement about origination only: a practitioner may declare generic tests on a staging model and Loom will host them verbatim, under the carriage half of the principle (ADR 0029). Future originated tests require explicit contract semantics rather than inferred raw-data properties.

There is no metadata opt-out. `test_opt_outs` is rejected as unsupported, and tests are default-on whenever a renderer generates them. The broader generator/test matrix is maintained in the [current architecture reference](../architecture.md#generated-dbt-tests).

Generated test YAML carries the Loom provenance header like every generated file: generation preflights ownership and refuses to overwrite a file without the header (unless `--force`), and `loom generate --check` detects drift between the metadata and the files on disk. Outputs of a removed instance are not detected by the drift check and are removed explicitly.

## Consequences

The generated tests participate in ordinary `dbt test` and `dbt build` selection and reporting. They cannot be opted out through Loom metadata. No test is inferred for a staging key unless a future contract explicitly guarantees that property. (These consequences concern data tests; the separate question of enforced model contracts is decided by ADR 0032.)
