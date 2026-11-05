# ADR 0009: Opt-in identity retention

Status: accepted. The retention decision is stated as it now stands - persistent hubs run on every dialect, not only the two adapters of the original increment.

The `hub` declaration remains a rebuilt current-state table by default (`retention: current`). The opt-in `retention: persistent` generates a dbt incremental model: an append guarded by an anti-join on the hash on every dialect, and merge keyed by the generated hash on Databricks. The first build loads distinct normalized business keys; later builds add only unseen identities. An identity absent from a subsequent complete extract stays in the hub. Attribute changes do not alter identity. Null keys are filtered out. `--full-refresh` intentionally rebuilds from the current extract and therefore discards previously retained identities.

The key contract is ADR 0008's, unchanged: uppercase/trimmed text, lowercase-hex SHA-256, identical across dialects. This is a single-writer pattern: no concurrent-writer guarantee is made.

Do not switch an existing consumer-facing hub to this mode in place: create and validate the alternative beside it if downstream consumers depend on current-state membership. Provenance for retained identities (the `__record_source` / `__loaded_at` columns) is a further opt-in decided by ADR 0010; Loom does not infer source timestamps or synthesise lineage fields.
