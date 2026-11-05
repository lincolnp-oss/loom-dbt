---
name: loom-adoption
description: Adopt Loom in an existing dbt project - assess its hand-written models, convert what fits, register the rest. Use when the practitioner points Loom at a project with existing models and a manifest - running register/convert, reading readiness, working drafts in local/drafts/, promoting an instance, or proving equivalence before a swap. Not for a project with no models yet (loom-bootstrap) or routine upkeep of an adopted project (loom-maintenance).
---

# Adoption - bringing an existing dbt project onto Loom

The core rules apply throughout (`loom-core`): Loom copies, you
propose, the practitioner decides; mutating commands preview by
default and write only under `--write`; escalation beats improvisation.

The short version, from the [adoption playbook](https://github.com/lincolnp-oss/loom-dbt/blob/main/docs/adoption-playbook.md)
(which governs where this skill paraphrases it): **convert first,
register the rest, promote only what is ready, prove each conversion
against the original before swapping.** One assessment drives
everything. A hybrid project - some models generated, the rest
registered and hand-written - is a legitimate permanent end state,
not a waypoint.

This skill ships with bundled copies of the documents it defers
to, under `references/` beside this file: the adoption playbook
(`references/adoption-playbook.md`), the metadata reference
(`references/metadata-reference.md`), and the first-project
example's metadata files (`references/first-project/`, verbatim
excerpts of the shipped example at their example paths). They
work offline; when a detail matters, read the section you need
rather than the whole file.

## 1. Assess

Prerequisite: a current, parse-produced `target/manifest.json` (see
`loom-core`). Run both reads - reads never write:

```text
loom register --project-dir . --format json
loom convert  --project-dir . --format json
```

Both report the same `summary`; `register` presents the inventory,
`convert` presents the drafts. Read, in this order:

- `warnings` and `manifest_provenance` - warnings label the evidence
  itself (substitute-adapter mismatch; build-produced manifest).
  Report them before any numbers, and act on the provenance warning
  as `loom-core` directs.
- `summary.readiness` - the **readiness distribution** over the
  project's non-controlled models: `ready_to_promote`,
  `needs_authoring`, `not_draftable`, plus `readiness_transitions`
  (how many unpromoted drafts changed account since the run that
  wrote them). Readiness is derived, never stored.
- `summary` state counts - `controlled_by_loom`,
  `known_not_controlled`, `unknown_to_loom`, plus
  `seen_but_not_assessed` (non-model resources - snapshots, seeds,
  tests, and the semantic layer: semantic models, metrics, saved
  queries - counted, not assessed as models).
- `models[]` - each model's `state`, `draft_registration` where
  drafted, `fit` (a generator proposal with drafted parameters and
  evidence, or a `no_fit` reason), `readiness` (state, `basis` -
  `fit` or `authoring` - `holes` of kind `authoring` or `evidence`,
  and `blockers`), and `interface_flags`. Consumer counts are
  decomposed by kind (`consumers_by_kind`), and the metrics and
  saved queries reachable from a model via its semantic models are
  *named* in its record and flags - treat semantic-layer consumers
  as real consumers when sequencing a conversion.

On a large project, narrow any pass with `--select` (the shared
grammar in `loom-core`): selection narrows within the command's pool
and an unmatched selector errors by name. Present the readiness
distribution, the states and the per-model picture to the
practitioner. Nothing has been written yet.

## 2. Convert first - write the drafts

With the practitioner's go-ahead, preview, then write:

```text
loom convert --project-dir . --format json            # preview
loom convert --project-dir . --write --format json    # drafts_written
```

Convert drafts for its pool - by default the models **unknown** to
Loom. Registered models are reported but not drafted: registration is
the withdrawal mechanism (step 3). `--include-registered` re-opens a
registered model for drafting when its circumstances change.

Convert writes **one draft class**. Every pooled model that earns a
draft - fit or layered no-fit - gets a single **unified draft** under
`local/drafts/`:

- a **draft record** `<model>.yml`: the generator and its basis
  (`fit` - output shape matched a fit rule - or `authoring` - a
  target generator named by layer), the evidence or the no-fit
  reason, the pre-wired inputs and named authoring hole (authoring),
  the declared interface copied verbatim, the readiness account,
  `review_todos`, interface flags, and a `promotion` list;
- a **final-form instance document** `instances/<model>.yml`, in the
  shape `metadata/instances/` consumes, carrying the drafted
  parameters (for an authoring draft, the transform parameters
  deliberately absent - the hole);
- where the project has no pattern for the generator, one
  **synthesised pattern** `patterns/<generator>_pattern.yml` per
  generator for the run. Its name is **final** - drafts reference it
  from the start and promotion moves it unchanged; there is no
  rename step.

`local/drafts/` is a review area - Loom never loads it, so an
incomplete draft cannot break validation. What a draft carries:

- **The declared interface, copied verbatim** - the exact input set,
  an enforced contract's columns/types/constraints, dbt unit tests'
  given/expect fixtures, generic tests, singular tests (copied with
  their SQL), semantic models' entities/dimensions/measures, and
  config facts. Copied, never inferred. It is the proof record of
  what a swap must not lose.
- **Carriage** - a fit draft copies the model's manifest generic
  tests into the instance's `generic_tests`, keyed on the model the
  test is declared on, so they generate with the model after
  promotion. Singular tests are files, not declarations; they travel
  as files and are never copied into an instance.
- **Macro drafting, in tiers** (staging fits). A slot-shaped macro
  call - one macro over one source column as the projection - drafts
  as a `macro:` / `macro_args:` column item, but only after
  verification against the manifest. References come in two forms: a
  **bare name** is a project macro (validation verifies it); a
  **`package.name`** reference names a package macro - validation
  cannot resolve those and warns "unverified package macro" without
  blocking, while `loom check` and `loom convert` verify existence
  and arity against the manifest, and `loom generate` renders the
  invocation exactly as declared. Where a call's arguments place the
  source column somewhere other than first, the draft pins the
  position with **`macro_column_param`** (the 1-based argument
  position the column fills; `macro_args` carries the rest). A
  slot-shaped call that cannot be verified is an **evidence hole**
  naming the macro - never silently passed or dropped. Macro calls
  in any other shape (inside a CTE, embedded in a larger expression)
  are named in the no-fit reason and wait for authoring.
- **Named blockers, not silent losses** - a model carrying a row filter, a plain-SQL column expression or a row limit is `not_draftable` with the construct named, never a fit that drops it: a row filter (WHERE) - a fit draft cannot carry a filter, so a fit would silently drop it (the staging generator can express a filter as authored vocabulary under ADR 0038, but convert does not draft one).

An authoring draft is **not** a fit and the record says so: its
generator (staging → `staging`; intermediate → `normalized_table`, or
`view` when declared a view; mart → `fact` / `dimension` by
classification, else `mart`) names where the authoring is headed,
nothing about the model's shape.

## 3. Register the rest

Register describes the models the practitioner is keeping
hand-written - no-fit models they decide not to author toward a
generator, flagged models whose coordination is not worth a
conversion, stateful resources, and models whose bespoke SQL is
simply their right form:

```text
loom register --project-dir . --write --format json   # registrations_written
loom validate --project-dir .
```

Drafted registrations land in `local/registrations/` directly - that
*is* the load path; there is no promotion for a registration. Review
each draft with the practitioner: the classification and its
evidence, the harvested `description` and `owner` (verbatim from the
manifest where declared); fix any remaining `owner: TODO`, complete
any description the manifest did not document, correct any
mis-classification. Those completions are holes - propose,
practitioner ratifies, then edit.

Registration does three things: the model joins the inventory; it
**leaves the convert pool**, so later convert runs report it without
drafting it; and it stands as a complete, legitimate end state.
Deleting a registration returns the model to the unknown pool.

## 4. Work the draft - one at a time

Pick one draft with the practitioner. Never batch. The draft record's
readiness account tells you which kind of work it is.

**A fit draft asks for review:**

1. Read the record's `evidence`; it is the audit trail. Check the
   drafted parameters in `local/drafts/instances/<model>.yml`
   against the original SQL by eye - especially the staging column
   mapping (items are `{source, alias, …}`; a rename inference is
   recorded as such, verify it; a mapping the manifest's documented
   columns could not pair is drafted as an unresolved placeholder,
   `source: __TODO__` - draft each such mapping from the original
   SQL, because `loom validate` rejects the sentinel and no
   placeholder may survive promotion), a silver `dedup_order`, the
   drafted macro calls (an unverifiable one sits in the account as
   an evidence hole; the remedy is a current manifest or a corrected
   call - not deleting the hole), the carried `generic_tests`, and
   any `TODO` the draft could not derive. Fill `description` and
   `owner` where they are TODOs. A fit certifies output shape, not
   semantics - step 6's proof is what verifies a conversion.
2. Work the remaining named holes as proposals: answer each
   `review_todos` item, and propose where the `source_rationale`
   comments belong (keep, prune, or move into documentation) -
   verbatim text, your placement proposal.

**An authoring draft asks for the transform:**

1. Check the copied facts against the model's SQL - layer, target
   generator, pre-wired inputs (the consolidated `input_models`
   list; a link target's `endpoints`, a vault target's `parent`
   block), declared interface. A copy is not a verification; where
   manifest and SQL disagree, the SQL is truth and the record is
   stale - regenerate the manifest and re-assess.
2. Author the transform - the named hole. The record's
   `authoring_hole` lists the target generator's required parameters
   the draft could not wire; express the transform as those
   parameters in `local/drafts/instances/<model>.yml`, labelled as
   your proposal. Everything else was bookkeeping Loom already
   copied. (For a `mart` target the hole is the nested `aggregation`
   map - `group_by` plus measures with `column`/`expression`/`alias`;
   a second graph input has no slot in a singular-input target -
   express it inside a measure expression, restructure upstream, or
   stop.) If the target generator cannot express the transform, stop:
   the model stays hand-written and registered (step 3), the draft
   remains its starting point. A valid end state - report it as one.

A staging instance also binds to its source, in exactly one of
two ways - declare one of them, never both, never neither (the
exactly-one rule is a validation error either way it is broken):

- `source_ref: {name, table}` - a pointer to a source the
  practitioner has declared in their own schema YAML. Both keys
  are required and no others are accepted; Loom emits nothing
  for it, and the generated staging SQL references the declared
  source.
- `inline_source: {name, table, ...}` - a declaration Loom
  itself emits into the generated sources document. `name` and
  `table` are required; `schema`, `loaded_at_field` and
  `freshness` are optional (a `freshness` block takes
  `warn_after` and/or `error_after`, each `{count, period}`).

(Medallion bronze takes `inline_source` only, and requires it.)

**Seed-fed staging drafts arrive with the source binding as a
hole.** A seed-fed staging model reaches convert as an authoring
draft, because every staging fit rule needs a dbt source
dependency and a seed read through `ref()` is not one; the draft
record names the seed dependencies the model actually has, and
the binding is the one authoring choice the draft cannot make
for you. A staging instance binds exactly one dbt source, so
choose how the seeds become a source. Either declare the seeds
as a dbt source in your own schema YAML and bind the instance
with `source_ref: {name, table}` (the source declaration stays
practitioner-owned YAML that you maintain by hand, Loom emits
nothing for it, and the generated staging SQL references the
declared source), or bind with `inline_source: {name, schema,
table}` (Loom generates the source declaration itself, into its
generated sources document, so the declaration is Loom-owned and
regenerates with the project instead of being maintained by
hand). Both are complete bindings, and the choice is only about
who owns the source declaration; it is the same exactly-one
choice every staging instance makes, and the metadata reference
states both shapes in full. The schema in an `inline_source`
binding is the warehouse schema the seed materialises into
under the project's dbt profile (the target schema, adjusted
by any `+schema` configured for the seed), not the name of the
seeds directory; if unsure, look the seed up in the manifest
(`target/manifest.json`), where each seed entry records the
schema it resolved to.

A computation on one column (an `amount / 100` division, say) is
carried by the macro slot: the column item is
`{source, alias, macro}`.

For a plain-SQL computation with no templating, staging columns
also take an `{expression, alias}` item; the expression-item
section of `references/metadata-reference.md` states the uniform
grammar and every surface that takes the form. Dimension `attribute_columns` and fact `measure_columns` take the same three item forms (a plain column name, a `{source, alias}` rename, or an `{expression, alias}` item); a rename's `source` is existence-checked against the input's output columns, an expression item's identifiers are practitioner-asserted and never checked, and scd2 `tracked_columns` and satellite `attribute_columns` stay name-only.

A row restriction is carried the same way, as authored vocabulary rather than as a fit: six generators take a `filter` parameter, a non-empty verbatim SQL predicate rendered into the generated model's WHERE clause (staging, silver, dimension, fact, scd2_dimension, normalized_table). Silver applies it before dedup ranking, scd2_dimension on the snapshot's input select, and changing a filter between loads is a population change; the filter parameter section of `references/metadata-reference.md` states the full surface and the incremental composition. Convert does not draft a filter, so a model carrying one is `not_draftable` with the construct named (see step 2).

## 5. Promote - move-only, practitioner only

Promotion is one pathway for both draft kinds, and it is
**move-only**: the draft was authored in final form, so nothing is
retyped, renamed or edited at promotion time. Confirm readiness
first - re-run `loom convert` and read the account and any
`readiness_transition`, rather than trusting remembered state. Then
the practitioner:

1. If the draft references a synthesised pattern
   (`<generator>_pattern`), moves
   `local/drafts/patterns/<generator>_pattern.yml` into
   `metadata/patterns/` **unchanged**. Where several patterns already
   exist for the generator, the instance document's `pattern:` is a
   `TODO`: choosing among them happens *in the draft, before
   promotion* - present the candidates; choosing is the
   practitioner's.
2. Moves the instance document into `metadata/instances/`
   unchanged, or merges its instance into an existing instances
   document for the same pattern.
3. If the model has a registration, deletes
   `local/registrations/<model>.yml` now, before the gate - while
   both exist, `loom validate` fails with `duplicate inventory name`.
4. Gates on `loom validate`. The record's `promotion` list repeats
   these steps for the specific draft in hand.

Your job in this step is the checklist and the readiness evidence -
not the moves.

## 6. Prove equivalence - before any swap, every time

Build the candidate in a **scratch copy** of the project: the
practitioner removes the hand-written model file in the copy (their
act - `loom generate` refuses to overwrite it, by design), then
`loom generate` to preview and `loom generate --write` to write,
`dbt seed` if applicable, and `dbt build` against the **same** source
data as the original. Building and proving happen in the
practitioner's environment; prepare the commands, the decisions and
the comparison, and run only what you are authorised to run there.

**The properties-document decision comes first.** `loom generate`
publishes a properties document for each generated model (under
`models/_generated/tests/`), and the project's schema YAML may still
describe the model too - dbt refuses a resource described twice.
Decide by what the schema entry carries:

- Only description, columns and tests that carriage or the generator
  covers → keep Loom's generated document and delete the
  practitioner's entry for the model.
- Content Loom does not generate (a semantic model defined alongside
  the entry, generic tests kept by hand) → keep the schema YAML and
  declare `skip_properties_document: true` in the instance params, so
  generate stops publishing its document for that model. One
  combination is refused by validation: `skip_properties_document`
  **plus** a non-empty `generic_tests` - carriage needs the document
  the opt-out suppresses. If a model needs both, the generic tests
  are declared in the schema YAML by hand and the carried
  `generic_tests` comes out of the instance. Never propose deleting
  Loom's generated document after every generate as a workaround -
  the opt-out is what it is for.

For an authored model, also confirm the declared interface held:
contract columns/types/constraints; the copied dbt unit tests as an
oracle (`dbt test` - `given` fixtures are inputs, `expect` the
required outputs); the generic tests passing.

**Then the static stage - `loom prove`.** `dbt compile` in the
original project and in the scratch copy; copy the two manifests to
the fixed names `local/equivalence/original.manifest.json` and
`local/equivalence/candidate.manifest.json` in the scratch copy
(copy each into place before the next dbt run overwrites `target/`);
run `loom prove --format json`. Per model the stage reports
**statically proven** (the warehouse comparison may be skipped - cite
the proof record, its JSON, in step 7), **inconclusive** (proceed to
the warehouse comparison exactly as before), or **not run** because
the manifest pair is absent. The stage only proves or abstains - it
never disproves - and candidate-only `__`-prefixed system columns are
declared differences by rule.

**Then, for every model the static stage did not prove, the warehouse
comparison** - candidate vs original over the original model's
columns: equal row counts; equal contents (ordered full-row selects,
or per-column checksums plus a whole-row hash count); generator-added
`__` system columns listed explicitly as accepted differences, never
allowed to mask a real one.

**Proof-data standard.** A proof is only as strong as its data. For
dedup-shaped conversions (silver, and any shape whose rule treats
keys specially) the data must contain duplicate keys (including the
rows the `dedup_order` tie-break decides), **null keys**, and
boundary rows (first/last row of each group, empty strings,
earliest/latest timestamps). A proof on null-free data cannot
distinguish a shape that keeps null-key rows from one that drops
them: if the data cannot supply such rows, record the proof as
**partial** in step 7 and name the cases it did not cover. A model
that cannot be built twice against identical data is not converted by
this pathway - it stays registered and hand-written.

## 7. Swap and record - practitioner only, on a pass

In the real project the practitioner: removes the hand-written model
file, runs `loom generate --write` (the step 6 properties-document
decision applies to this generate too), `dbt build`, and `loom
coverage` (`ok`, `missing_models`, `orphan_inventory_entries`,
`misplaced_generated_models`). If the model carried an enforced
contract or attached tests, they declare `contract_enforced` in its
Loom metadata (with `column_types` completing its types), per the
step 1 flags, and confirm the attached tests still run against the
generated model - generated data tests are additional where the
generator emits them, and **staging emits none**, so for a staging
conversion the kept or carried tests are the whole test story. The
registration was already deleted at promote time (step 5); a re-run
of `loom convert` now reports the model as controlled.

**Record the outcome** in the commit message: model, generator, the
equivalence evidence (the static proof record, or row counts and
checksums), any accepted differences, and - for a partial proof - the
cases it did not cover. Your job in this step is the checklist and
the evidence record - not the acts.

Use `loom plan --from <old> --to <new>` before a swap that renames or
replaces a model others depend on: it inventories direct/transitive
dependents and observed manifest consumers (decomposed by kind,
semantic layer included), plus its phased actions. External
(non-manifest) consumers are always unknown - say so.
