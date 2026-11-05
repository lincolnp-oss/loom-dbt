# ADR 0038: The filter parameter - a verbatim row predicate on the single-input generators

Status: Accepted

## Context

No generator in the vocabulary had a general author-declared WHERE: a practitioner who needed to restrict a model's input rows had no parameter for it at all. The paradigm slices that look like filtering are fixed semantics, not a predicate surface - silver's dedup ranks and keeps, the Data Vault PIT performs as-of joins, reference_table has a snapshot literal - and none of them expresses "only rows where ...". The gap analysis verified this against the contract table at the certified tree: joins on the joined generators are LEFT plus key-equality only, and there is no HAVING anywhere either. The absence distorts evaluation of the thing the Alpha exists to test, the same test the expression item (ADR 0037) passed: a basic capability whose lack reads as a verdict on the whole tool. Convert already names the shape from the other side - a hand-written model carrying a row filter is a named blocker for fit drafting, because no generator could express it.

Two design questions rode with the decision. First, the grammar: ruled to reuse ADR 0037's posture wholesale - a predicate is practitioner-asserted verbatim SQL, never evaluated and never identifier-checked by Loom, because Loom has no more basis to second-guess a predicate's identifiers than an expression's. Second, the surface: which generators admit the parameter. The line drawn is single-input scans. A predicate over one input has exactly one scan to bind to and an unambiguous composition with that generator's machinery; a predicate over joined or multi-input models would have to name which input it binds to, how it interacts with the join, and whether it applies before or after it - a design of its own, not a parameter row. The aggregating generators' analogue is HAVING, a different clause with its own semantics, and it is not smuggled in as a WHERE.

## Decision

One optional parameter, `filter`, admitted by exactly six generators: staging, silver, dimension, fact, scd2_dimension, normalized_table.

- `filter` is a non-empty verbatim SQL predicate string, carried and rendered exactly as written into the generated model's WHERE clause. Loom never evaluates, rewrites, or identifier-checks it.
- Jinja delimiters (`{{`, `}}`, `{%`, `%}`, `{#`, `#}`) and statement terminators (`;`) are banned at validation, by the same content-check machinery as the expression item (a sibling checker sharing the delimiter set): an admitted predicate is plain SQL, so no macro or templated form is admitted through it. A non-string value or an empty string is a validation error naming the accepted form.
- On the twelve generators that do not admit it (bronze, gold, mart, obt, view, hub, link, satellite, pit, bridge, reference_hub, reference_table), `filter` is an unknown parameter, rejected by the ordinary contract error naming the accepted set.

Per-generator semantics - in each case the predicate binds to the generator's single input scan:

- **Staging**: a WHERE over the source scan, under either sourcing form and either projection shape; columns, expression items and the macro slot are unchanged.
- **Silver**: the predicate applies to input rows BEFORE dedup ranking - it sits inside the ranking subquery, so the winner per dedup key is the best row that passes, never the best row overall with the predicate applied to the outcome. On the pass-through form (no dedup key) it is a WHERE over the scan.
- **Dimension, fact, normalized_table**: a WHERE over the single input scan.
- **SCD2 dimension**: the predicate sits on the snapshot's input select, restricting which input rows enter SCD2 processing; it is not a filter over the generated output history, and the dimension views that expose the history are unchanged. A row the predicate excludes never opens a history row.

Composition with incrementals: where an incremental cursor predicate also applies (dimension, fact, normalized_table; silver's affected-key restriction), the generated SQL renders a SINGLE WHERE - the filter first, the incremental predicate ANDed behind it inside its `is_incremental()` gate. A full refresh therefore applies the filter alone and an incremental run applies both, and the append anti-join guards keep their existing structure. Silver's affected-key window subquery still names the touched keys over the unfiltered input, deliberately: a key whose newest rows fall out of the filter must still be recomputed over its surviving history, which a filtered window would never name. An instance without `filter` generates byte-identical SQL to before this decision, on every generator.

Convert is unchanged: this record is vocabulary only. A row filter in a hand-written model remains a fit blocker, because convert does not draft predicates - the same accepted asymmetry as ADR 0037's expressions (a practitioner can author what convert still refuses to draft).

## Consequences

- The predicate surfaces named here as NOT admitted are the programme's stated post-Alpha bounds: predicates on joined and multi-input generators, per-input filters, a HAVING form for the aggregating generators, and join conditions beyond key equality (including range joins). Each needs its own decision; none is implied by this one.
- Changing a filter between loads changes the population a model sees, with the generators' ordinary semantics: on silver and the key-strategy incrementals a key whose rows all fall out of the filter behaves like a key that left the input (its stale winner survives incremental runs until a full refresh, the documented delete-blindness escape), and on scd2_dimension a key that stops passing reads to the snapshot as a disappearance, closing under the snapshot's hard-delete handling. The filter is a population declaration, and editing it is a population change.
- The predicate's placement is fixed per generator by the renderers, not chosen per instance: there is no way to ask for silver's predicate after ranking, or for an scd2 predicate over history. A practitioner who needs another placement hand-writes the model - the exit-freely property is untouched.
- Output-column derivation is unaffected: a filter names no output column, so properties, downstream validation and unit tests see the same columns as without one.
