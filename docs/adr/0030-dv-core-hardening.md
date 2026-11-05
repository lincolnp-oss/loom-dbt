# ADR 0030: Data Vault core hardening - multi-source hubs, n-ary links, dependent-child keys, non-historised links

Status: Accepted

## Context

ADR 0028's satellite design closed with two named limits: binary links only, and multi-source hubs not supported. Practitioners hit those limits before the exotic satellite variants: a customer hub fed by two source systems and a ternary relationship are ordinary Data Vault shapes that the contracts made unrepresentable (a hub named exactly one input; a link named exactly two endpoints). The non-historised link belongs in the same closing: an immutable-event link carrying payload, the most common second link shape after the plain relationship link - without it, event relationships lose their transaction grain in a current-state link.

This record states the shape decisions that close those gaps. The satellite discipline carries over: closed enums over free text, fail explicitly, no silent fallbacks, and generated SQL for previously expressible shapes stays byte-identical.

## Decision

### The base link contract

A link is a narrow current-state relationship table over one staging input: it rebuilds the distinct relationships present in the input on every build. The input declares the link's endpoints as one ordered `endpoints` list of `{hub, key}` mappings, two or more. Each endpoint key must equal its hub's `business_key`, every endpoint hub must be in the model inventory, and each key must be a declared alias of the staging input. Source keys are normalised with the hub rule - `upper(trim(cast(value as varchar)))` - and hashed with the dialect's SHA-256 (lowercase hex); the link hash is the hash of the concatenated endpoint hashes, and because each component hash is a fixed-length 64-character hexadecimal value, the concatenation is unambiguous.

The link fails fast rather than filtering: a guard counts input keys that are null, blank after normalisation, or unmatched in their hub, and a per-dialect gate expression fails the query when the count is non-zero - a message-bearing function where the engine has one in plain SQL (DuckDB `error()`, BigQuery `ERROR()`, Databricks `raise_error()`), integer division by zero elsewhere (ClickHouse via `intDiv`, since its `/` is float division). Duplicate relationships collapse to one row. The guard is exercised by DuckDB runtime tests, including a poisoned quickstart build; the other dialects carry the same composed templates, parse-checked.

A link is current-state only. It carries no retention, no provenance columns (no record source, no load timestamp), no effectivity or deletion history, and no multi-writer guarantee; a referenced hub may be current or persistent, but the link does not inherit hub retention. It is not a durable audited Data Vault link and must not be presented as one.

### Multi-source hubs: `input_models` + per-source provenance

A hub names its inputs in the one `input_models` list: one entry for a single-source hub, two or more for a multi-source hub - the same list form every multi-input generator uses, with cardinality validated per generator. Every input must be inventoried staging, and the `business_key` must be a declared output alias of every generated staging input (the existing hub discipline, applied per input).

Load semantics: each input contributes its distinct normalized keys; the hub holds one row per hash. When the same key arrives from several inputs in one load, the winner is deterministic - the earliest input in declared `input_models` order. (For a rebuilt hub there is no "first ever seen" across loads; declared order is the practitioner's stated source precedence. A persistent hub's append anti-join keeps the first-loaded row across loads as before.)

Provenance is per source, because a single hub-level label would misattribute rows: `record_source` takes a mapping form keyed by input model, naming a record source label for each input. It is all-or-nothing - either every input is labelled (and the hub emits `__record_source` per row, from the input that supplied the winning row, plus `__loaded_at`) or none is (and neither column is emitted). The string form of the same parameter labels every input uniformly, and is the form a single-source hub uses. Both forms keep ADR 0010's rule: provenance requires `retention: persistent`, because on a rebuilt hub the labels and timestamps would be re-stamped every load and would not mean what they say.

The single-input renderer's SQL is unchanged. The multi-input renderer unions the per-input selects and collapses them with a `ROW_NUMBER()` window partitioned by the hash, ordered by the declared input ordinal; persistent hubs then apply the existing append anti-join (or, on Databricks, the merge with provenance columns excluded from updates).

### N-ary links: the `endpoints` list

Canonical links relate two *or more* hubs, and the contract declares all of them the same way: the ordered `endpoints` list of `{hub, key}` mappings, with no privileged first pair. The link hash is the dialect hash of the concatenated component hashes **in declared order**; the components are fixed-length 64-character hex, so the concatenation is unambiguous at any arity (the base link contract's argument, extended). Binary links render byte-identical SQL: the concatenation helper uses the dialect's `concat` for two components everywhere and for more components on every dialect except Redshift, whose `CONCAT` takes exactly two arguments and which therefore chains the `||` operator. The endpoints were originally declared as a privileged first pair plus an additional-endpoints list; the one list replaced that shape without changing the semantics or the generated SQL, because an endpoint is an endpoint - the pair's privilege was an accident of the binary-only start.

The fail-fast guard generalises with the shape: an input row is invalid when *any* endpoint key (or the dependent-child key, below) is null or blank after normalization, and unmatched when *any* endpoint's computed hash has no hub row. The guard keeps its two counters and the per-dialect gate template - only the counted fragments multiply.

Each endpoint's key must equal its hub's `business_key`, each endpoint key must be a declared alias of the link's staging input, and all endpoint keys must be distinct.

### Hierarchical links: explicitly rejected

A hub may appear at most once among a link's endpoints. Same-hub ("hierarchical") links - an employee-to-manager link over one hub - are **rejected in validation**, deliberately: Loom derives each endpoint's foreign-key column name from the hub's name (`hub_employee` → `employee_hk`), so a repeated hub has no unambiguous column naming, and inventing role names is speculative surface with downstream effects on the catalogue resolver, satellites and facts. (Datavault4dbt can name same-hub link columns distinctly only because its staging computes hash-key columns under practitioner-chosen names; Loom's hashing is renderer-local by design.) The rejection is explicit, naming the repeated hub and this limit, so a practitioner learns it at validate time rather than from a column-collision error. Revisit trigger: a concrete practitioner modelling case that needs hierarchical links, which would reopen the naming design.

### Dependent-child keys

A link may declare one `dependent_child_key`: a staging column that is part of the link's grain without belonging to any hub - the order-line number that distinguishes two relationships between the same hub pair. The key's normalized form (the hub normalization rule: it is a key, not a payload) joins the link hash composition as the final component; the link table stores the dependent key normalized, as hubs store their business key normalized. Because the endpoint hashes ahead of it are fixed-length, the composition stays unambiguous. The key must be a declared staging alias, distinct from every endpoint key, and a null or blank dependent key is a guard violation like any other key.

### Non-historised links: a link `variant`

The link generator gains a closed `variant` enum in the satellite style: `standard` (the default - the current-state rebuilt table of the base link contract above) and `non_historised`. A non-historised link is the immutable-event shape: it **requires** a `dependent_child_key` (the event discriminator - without one, repeat events between the same hubs are indistinguishable) and a non-empty `payload_columns` list (the event's measurements, carried from staging onto the link row). It loads as an append-only incremental model that inserts an event when its link hash is not already held; an event is never updated, so a re-sent event with corrected payload keeps the first-seen payload - that immutability is the construct's meaning, and the generated `unique` test on the link hash makes a same-extract grain violation a build failure rather than a silent duplicate.

`payload_columns` is accepted only on the non-historised variant: on a standard link, attributes belong in a satellite (the methodology's separation of relationship from description), and accepting them there would blur the two constructs. Provenance columns stay off links in both variants - the link family's stance from the base contract on; the event's own timestamp is the dependent-child key when the practitioner models it as one.

### Satellites follow the parent shapes

A satellite on a link parent mirrors the parent's full endpoint set in its `parent` block: the block's `endpoints` list and `dependent_child_key` are validated to match the parent link's declared values exactly (order-sensitive, as the hash is). The satellite's computed parent hash then equals the link's own hash at any arity, with or without a dependent-child key. Hub parents are unaffected - a hub's hash construction does not change when the hub gains inputs.

## Consequences

- The Data Vault claim extends to multi-source hubs and n-ary links; the remaining named DV limit is hierarchical links (rejected above). The satellite record is ADR 0028, reference constructs ADR 0034, PIT ADR 0035 and bridges ADR 0036.
- Shapes expressible before the n-ary and multi-source extensions render byte-identical SQL, verified by hash-pinning the existing shapes across all dialects.
- The link renderer's guard, written originally for exactly two endpoints, is now endpoint-count agnostic; a future endpoint shape change pays no second generalisation.
- The family's parameter shapes are one per concept: inputs are always the `input_models` list, provenance the one `record_source` (string or mapping), endpoints the one `endpoints` list, and a satellite's or PIT's parent the one `parent` block. Each replaced a per-shape spelling (a singular input parameter, a separate per-source provenance parameter, a privileged endpoint pair, per-endpoint parent fields) without changing semantics or generated SQL, proven by the unchanged golden set.
