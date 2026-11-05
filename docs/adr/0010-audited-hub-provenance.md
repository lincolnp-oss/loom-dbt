# ADR 0010: Opt-in audited hub provenance

Status: accepted. Provenance is stated as it now stands - it applies on all dialects and to multi-source hubs (ADR 0030), not only the original DuckDB increment. The per-generator provenance matrix in the architecture document's [Provenance](../architecture.md#provenance) section is the current reference; this record states the decision.

A hub with `retention: persistent` (ADR 0009) may declare an explicit `record_source`. Only then does Loom add two columns:

- `__loaded_at`: the load timestamp of the dbt run whose insert first appended that identity, rendered through the dialect's timestamp template (a naive local timestamp; on DuckDB, `localtimestamp`). Because the append/anti-join model never re-inserts a known hash, later builds keep the original value; on the Databricks merge path both provenance columns are excluded from updates, so first-insert values are retained there too.
- `__record_source`: the declared value as a SQL string literal. `record_source` takes two forms: a string labels every input uniformly, and a mapping keyed by input model labels each feed individually (all-or-nothing across a multi-source hub's inputs, ADR 0030), each row carrying its own input's label.

`record_source` is required rather than inferred, is rejected with `current` retention or without a retention setting, and must be 1–128 characters matching `^[A-Za-z0-9](?:[A-Za-z0-9 ._:/@-]*[A-Za-z0-9])?$` (no quotes, Jinja delimiters, or leading/trailing whitespace; single quotes are escaped anyway). A business key named `__loaded_at` or `__record_source` is rejected - an authored column may not collide with a generated system column. Loom never emits `__loaded_at` without a declared `record_source`: a timestamp without declared provenance would be a partial audit contract.

Semantics and limits:

- `__loaded_at` is **processing time** of the warehouse load, not a source event, extract, or business-effective time. Do not use it as one.
- The `__record_source` column is a declared static label - per hub, or per input source for a multi-source hub - not per-row lineage inferred from data.
- `dbt build --full-refresh` rebuilds from the current extract: absent identities are discarded and all `__loaded_at` values reset to that run.
- One writer is assumed; concurrent runs may race and are not protected.
- Adding `record_source` to an existing persistent hub changes its column set. dbt incremental models do not add columns by default, so create the audited hub beside the existing one (or deliberately full-refresh it, accepting the reset) rather than changing a consumer-facing model in place.

Omitting `record_source` leaves current and persistent hub SQL byte-identical to their output without it. Links do not inherit these audit columns or hub retention (ADR 0030).

Provenance was originally two parameters - the string form here and a separate per-input mapping for multi-source hubs; the shipped contract is the one `record_source` parameter in its two forms, because one concept with two spellings invited exactly the divergence the all-or-nothing rule exists to prevent. The persistent-only rule and the all-or-nothing discipline are unchanged by that unification.
