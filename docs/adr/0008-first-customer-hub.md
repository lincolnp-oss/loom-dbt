# ADR 0008: First customer hub contract

Status: accepted. This record states the hub identity contract as it stands - generalised to all dialects and to multi-source hubs (ADR 0030) without changing the contract's core. Retention is decided by ADR 0009, provenance by ADR 0010, links by ADR 0030; none of those are restated here.

## The contract

A hub is a current-state identity table over one or more staging models, authored as a pattern instance with `generator: hub`. It is not a replacement for an SCD2 dimension and carries no descriptive history - that is the satellites' job (ADR 0028).

- **Grain:** one row per distinct normalized business key, from the declared inputs (`input_models`, a list of staging models - one entry for a single-source hub, several for a multi-source hub, ADR 0030) and the `business_key` each input carries.
- **Key normalization and hash:** the business key is rendered as text by `upper(trim(cast(<key> as varchar)))`; the hash column is the lowercase-hex SHA-256 of that normalized key, rendered through the dialect's hash template so the same key hashes identically on every dialect. Hashes are deterministic for unchanged keys, not security tokens.
- **Naming:** the hub's name carries the `hub_` prefix (enforced in validation), and its hash column is derived from the name - `hub_customer` produces `customer_hk` beside the normalized key column `customer_id`.
- **Key validation:** when an input is a generated staging model, Loom validates that the business key is one of its declared output aliases. Identical normalized keys collapse to one row; null and whitespace-only keys reject the build - the output is a dbt table so the invalid-key guard executes during `dbt build` even if no dbt data tests are configured.
- **Composition rule:** anything that joins hubs - links first - must use this same canonicalization and hash contract, or explicitly migrate keys. This rule, not any single renderer's code, is what makes cross-model hash joins sound.

## Consequences

The hub is independent and additive: introducing one does not change existing dimensions or facts. By default (`retention: current`) a hub does not promise historical retention when an identity disappears from a current-state extract; persistence is the separate opt-in decided by ADR 0009.
