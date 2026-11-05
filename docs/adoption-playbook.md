# Adoption playbook: bringing an existing dbt project onto Loom

This playbook is the adoption pathway (design: [ADR 0025](adr/0025-migration-pathway.md)). It is written for a practitioner working with an AI agent, and every step is review-first: the agent drafts, the practitioner decides, and no model is converted without an empirical equivalence proof.

The short version: **convert first, register the rest, promote only what is ready, prove each conversion against the original before swapping.** One assessment drives everything: `loom convert` drafts replacements for the models it can - a fit where a generator's shape matches, an authoring draft where none does - and `loom register` describes the models you decide to keep hand-written. Registering a model is also how you withdraw it from conversion: the convert pool is the models Loom does not know, so a registered model is never drafted unless you ask for it back. A hybrid project - some models generated, the rest registered and hand-written - is a legitimate permanent end state, not a waypoint.

Terms in this playbook follow [Loom terminology](terminology.md); command detail is the [commands reference](commands.md); mechanism is the [architecture document](architecture.md).

## Step 0 - Prerequisites

Producing `target/manifest.json` is the adoption's first wall: the assessment reads the manifest, not the warehouse, and everything downstream inherits its provenance. Before Step 1:

- **Install the dbt adapter for the project's profile** - the `type` of the active target in `profiles.yml` - in the environment that will run the parse.
- **Make the profile authenticatable, or substitute deliberately.** The profile must be able to authenticate for a parse, *or* parse with a substitute adapter (for example a local DuckDB profile) purely to produce the manifest. A substitute parse is blessed for assessment only: the assessment detects it - the manifest's recorded adapter will not match the project profile's adapter - and warns, because dialect-sensitive conclusions (fit shapes, interface flags, drafted configuration) then derive from the substitute adapter's parse, not the project's own toolchain. Nothing converted under the equivalence proof may rest on a substitute parse; the proof build uses the real profile. One abstention case: if the project's profile is fully templated - its adapter `type` itself resolved from `env_var(...)` - no profile adapter can be read, so the warning stays silent even when the manifest came from a substitute parse; silence on a templated profile is not evidence that the adapters match.
- **Match the dbt version.** Parse with a dbt that satisfies the project's `require-dbt-version`. Loom's tested line is dbt Core 1.12.x, and Loom reads manifest schemas v10–v12 (dbt Core 1.6–1.12); a manifest from outside that range is rejected, not misread.
- **Set the required env vars.** Profiles and packages routinely read `env_var(...)`; a parse in a shell without them fails before it starts. Set them in the parse command's environment.
- **Produce a current manifest with `dbt parse`.** The assessment consumes a *parse-produced* manifest: `dbt parse` does not compile model SQL, so the manifest's model nodes carry no compiled code and the fit rules read the authored SQL - the input fit calibration assumes. A manifest from `dbt build` / `dbt compile` is *build-produced*: its model nodes carry macro-expanded compiled SQL, the fit rules read that instead, and proposals can differ. The assessment declares which provenance it was given (`manifest_provenance` in the report, and a header line in text output) and warns on a build-produced manifest - if you see that warning, regenerate the manifest with `dbt parse` and re-run the assessment before acting on any fit proposal. (`dbt build` remains the right command for actually building the project; it is just not the manifest the assessment should consume.)
- **Check `git status` afterwards.** Pre-flight commands can write tracked files - `dbt deps` has been observed rewriting a tracked `package-lock.yml`. Restore anything the pre-flight touched that you did not intend to change.
- **Work on a branch.** Nothing in this playbook requires committing until the very end.

## Step 1 - Assess

One assessment, read through its two operations:

```sh
loom register --project-dir .
loom convert --project-dir .
```

Both commands run the same assessment and report the same `summary`; `register` presents the inventory (states and drafted registrations), `convert` presents the drafts. Read the reports (human-readable text by default; agents and scripts should pass `--format json` for the structured payload):

- `summary` - leads with the **readiness distribution**: `ready_to_promote` (drafts with zero holes and zero blockers), `needs_authoring` (drafts whose named holes stand between the draft and promotion - *authoring* holes only the practitioner can supply, *evidence* holes Loom could not verify against the manifest) and `not_draftable` (no draft, or a named blocker no generator can express), counted across the project's non-controlled models, plus `readiness_transitions`: how many unpromoted drafts changed account since the run that wrote them. Expect the states to differ by a lot: converting is only for the few whose shape is a closed generator shape, authoring drafts cover most of the rest, and registering is for every model a practitioner decides to keep hand-written. The summary then breaks project models down by state: `controlled_by_loom` (a generated instance exists; Loom generates the model), `known_not_controlled` (a practitioner registration exists; Loom knows the model but does not generate it), and `unknown_to_loom` (manifest only) - plus drafted registrations, flagged models, and `seen_but_not_assessed`: counts of the non-model resources (snapshots, seeds, tests, semantic models, metrics, saved queries, …) the assessment saw in the manifest but does not assess as models. Model counts are model counts; a stateful snapshot in the project shows up here even though it is never proposed for conversion. In `loom convert`'s text output the conversion drafts are listed first, so the convert yield can be triaged without reading every registration.
- Per model: its `state`; for unknown models, `draft_registration` (classification + the evidence for it); for unknown and registered models, `fit` (a generator proposal with drafted parameters and recorded evidence, or a `no_fit` reason naming the blocking signals); for a no-fit model in a dbt layer, an authoring draft (the layer, the target generator, the pre-wired inputs, the declared interface, the authoring hole); and `interface_flags`. Controlled models are summarised in text output - they need no action.

If the report carries a `warnings` list (printed as `Warning:` lines ahead of the model sections in text mode), read it first: it records a provenance problem with the manifest itself - currently the adapter mismatch, meaning the manifest came from a substitute-adapter parse (Step 0) and dialect-sensitive conclusions in this report derive from the substitute, not the project's own toolchain. A warning never blocks the assessment; it labels the evidence.

Treat the flags as conversion blockers until addressed:

- **Enforced dbt contract** - a Loom-generated replacement drops enforcement unless `contract_enforced` is declared in the Loom metadata for the replacement (with `column_types` completing its types, ADR 0032). Declare it, or accept the drop explicitly.
- **Public/protected access (Mesh)** - external projects may depend on the interface; a conversion is a coordinated change.
- **Attached tests / downstream consumers** - the swap must keep or deliberately replace them; consumers are listed by `loom plan` in detail. Counts include semantic models, metrics, saved queries and unit tests from the manifest.
- **Incremental / ephemeral materialization** - a Loom-generated replacement carries no incremental configuration, so converting an incremental model would silently turn its builds into full rebuilds, and converting an ephemeral model would persist a model that is never persisted today. Keep the model hand-written, or accept the change explicitly. One exception: where a bronze or silver conversion draft exists, the model's declared `unique_key` / `incremental_strategy` / `on_schema_change` are drafted into the draft's `incremental` block and this flag does not fire - review the drafted block like any other drafted parameter instead.
- **Group / deprecation date** - group membership gates cross-group Mesh references and cannot be re-declared in Loom metadata today; re-declare the group (and any deprecation date) by hand after converting.
- **Grants / physical configuration** - grants, partitioning, clustering and similar warehouse configuration are not carried by a conversion; re-apply them by hand or warehouse cost, performance and access control may silently regress.

Documented models keep their documentation: a manifest description is drafted into the registration and the conversion draft, an owner declared in the manifest's `meta.owner` is drafted with it, and documented column descriptions and types are drafted into the `column_docs` / `column_types` parameters (the types as declared, marked UNVERIFIED in the draft evidence). The source model's SQL comments - all of them, verbatim, in source order - are carried into the conversion draft as `source_rationale`; prune what does not belong and decide where that rationale belongs before swapping.

## Step 2 - Convert first: write the drafts

```sh
loom convert --project-dir . --write
```

Convert drafts for the models in its pool - by default the models **unknown** to Loom (`unknown_to_loom`). Registered models are counted and reported but not drafted: registration is the withdrawal mechanism, and it is how the models you have already decided to keep hand-written stay out of the drafting pool. `--include-registered` drafts registered models too, when you deliberately re-open one.

Convert writes **one draft class**. Every pooled model that earns a draft - a model with a fit proposal, and every model with no fit that sits in a dbt layer (on a real project, most of them) - gets a single unified draft under `local/drafts/`:

- a draft record `<model>.yml`: the generator and its basis (`fit` - the model's output shape matched a fit rule - or `authoring` - a target generator named by layer for a model no rule matched), the evidence basis per parameter (for a fit) or the no-fit reason, the layer, the pre-wired inputs and the named authoring hole (for an authoring draft), the declared interface copied verbatim, the model's **readiness account** (its state, its holes, its blockers), a `review_todos` list, the interface flags, and a `promotion` list spelling out exactly how to adopt the draft;
- a final-form instance document `instances/<model>.yml`, in the pattern/instance shape `metadata/instances/` consumes, carrying the drafted parameters (for an authoring draft, the inputs wired from the manifest dependency graph and the transform parameters deliberately absent - the hole) and referencing the pattern to use - an existing one where the project has exactly one for the generator, otherwise the synthesised pattern;
- when the project has no pattern for the generator, one synthesised pattern definition `patterns/<generator>_pattern.yml` per generator for the whole run, in the shape `metadata/patterns/` consumes, with the required params taken from the generator contract. Its name is **final**: drafts reference it from the start and promotion moves it unchanged - there is no rename step.

That directory is a review area - Loom never loads it as inventory, so an incomplete draft cannot break validation. Drafts are authored in final form, so once the review todos are completed, promotion is a file move plus registration cleanup - no content edits at promotion time (Step 5). Readiness is derived, never stored in the instance: every assessment run re-derives each unpromoted draft's account from its current content plus current evidence, and a change is reported as a readiness transition.

What a draft carries, beyond the fitted parameters:

- **The declared interface, copied verbatim** - bookkeeping the project has already declared: the exact input set, an enforced contract's columns/types/constraints, dbt unit tests' given/expect fixtures, generic tests, singular tests (copied with their SQL), semantic models' entities/dimensions/measures, and config facts (materialization, partition/cluster keys, access, group). Copied, never inferred. The declared interface is standard furniture of every draft, fits included - it is the proof record of what a swap must not lose.
- **Carriage** - a fit draft also copies the model's manifest generic tests into the instance's `generic_tests`, so the tests generate with the model after promotion instead of needing to be re-declared by hand. The copy is keyed on the model the test is declared on (a `relationships` test lands on its declaring model, not its target), drops the arguments dbt injects itself, and preserves a declared warn severity. Singular tests are bespoke SQL files, not declarations, and are never copied - they travel as files.
- **Macro drafting, in tiers.** A staging fit whose select items are macro calls in the slot shape - one macro invocation over one source column, as the projection - is verified against the manifest (the macro exists; the argument count fits its declared signature) and drafted as a `macro:` / `macro_args:` column; a call whose arguments place the source column elsewhere drafts with `macro_column_param` naming the argument position. A slot-shaped call that cannot be verified is an **evidence hole** naming the macro - it blocks readiness, and it is never silently passed or silently dropped. Macro calls in any other shape (inside a CTE, embedded in a larger expression) are named in the no-fit reason; they wait for authoring, not for a wider fit rule.
- **Named blockers, not silent losses.** A model carrying a row filter, a plain-SQL column expression or a row limit is `not_draftable` with the construct named, never a fit that drops it: a row filter (WHERE) - a fit draft cannot carry a filter, so a fit would silently drop it (the staging generator can express a filter as authored vocabulary under ADR 0038, but convert does not draft one).

An authoring draft is **not** a fit and the record says so: its generator (staging → `staging`; intermediate → `normalized_table`, or `view` when the model is declared a view; mart → `fact` / `dimension` by classification, else `mart`) names where the authoring is headed, nothing about the model's shape.

## Step 3 - Register the rest

```sh
loom register --project-dir . --write
loom validate --project-dir .
```

Register describes the models you are keeping hand-written: the no-fit models you decide not to author toward a generator, the flagged models (Step 1) whose coordination is not worth a conversion, stateful resources, and any model whose bespoke SQL is simply its right form. Drafted registrations land in `local/registrations/` (never overwriting an existing file). A model's declared description and `meta.owner` are drafted in verbatim where the manifest carries them; review each draft: fix any remaining `owner: TODO`, complete any description the manifest did not document, correct any mis-classification.

Registration does three things: the model joins the inventory (searchable, covered, counted); it leaves the convert pool, so future `loom convert` runs report it without drafting it; and it stands as a complete, legitimate end state - most projects should live with most models registered for a while, and many for good. Deleting a registration returns the model to the unknown pool and to future drafting. `--include-registered` on convert re-opens a registered model for drafting without deleting anything, for when a model's circumstances change.

## Step 4 - Work the draft

Work one draft at a time. The draft record's readiness account tells you which kind of work it is. The [metadata reference](metadata-reference.md) annotates every parameter and nested shape the drafts use, in place in its worked examples, when a draft asks for a value this playbook does not spell out.

**A fit draft** asks for review, in this order:

1. Read the record's `evidence` array; it is the audit trail for the fit. Check the drafted parameters (in the instance document under `local/drafts/instances/`) against the original SQL by eye - especially the staging column mapping (a rename inference is recorded as such; verify it; a mapping the manifest's documented columns could not pair is drafted as an unresolved placeholder, `source: __TODO__`, naming the output in the record's review todos - draft each such mapping from the original SQL, because `loom validate` rejects the sentinel and no placeholder may survive promotion), a silver `dedup_order` drafted from the original dedup's ORDER BY, the drafted macro calls (each was verified against the manifest; an unverifiable one is sitting in the account as an evidence hole, and the remedy is a current manifest or a corrected call - not deleting the hole), and any `TODO` the draft could not derive. Fill `description` and `owner`. A fit certifies the model's output shape, not its semantics - the equivalence proof in Step 6 is what verifies a conversion.

**An authoring draft** asks for the transform, in this order:

1. Check the copied facts against the model's SQL: the layer and target generator, the pre-wired inputs (copied from the manifest dependency graph - what dbt resolved), and the declared interface. A copy is not a verification; where the manifest and the SQL disagree, the SQL is the truth and the record is stale - regenerate the manifest and re-assess.
2. Author the transform - the named hole. The record's `authoring_hole` lists the target generator's required parameters the draft could not wire. Express the model's transform as those parameters in `local/drafts/instances/<model>.yml`. This is the one authoring task; everything else about the model was bookkeeping. If the target generator cannot express the transform, stop here: the model stays hand-written and registered (Step 3), and the draft remains its starting point for a later attempt. That is a valid end state, not a failure.

   For a `mart` target the hole is the `aggregation` parameter, a nested map - `group_by` lists the key columns, and each measure is an entry with `column`, `expression` and `alias`:

   ```yaml
   aggregation:
     group_by: [customer_id]
     measures:
       - {column: order_id, expression: "COUNT(order_id)", alias: order_count}
   ```

   A second graph input has no slot in a singular-input target: express what you need from it inside a measure expression (a scalar subquery, for instance), restructure the model upstream, or stop - the contract is not stretched by hand.

   For a `staging` target fed by dbt seeds, the hole includes the source binding. A seed-fed staging model reaches convert as an authoring draft, because every staging fit rule needs a dbt source dependency and a seed read through `ref()` is not one; the draft record names the seed dependencies the model actually has, so the decision below is the one authoring choice the draft cannot make for you. A staging instance binds exactly one dbt source, so choose how the seeds become a source. Either declare the seeds as a dbt source in your own schema YAML and bind the instance with `source_ref: {name, table}`: the source declaration stays practitioner-owned YAML that you maintain by hand, Loom emits nothing for it, and the generated staging SQL references the declared source. Or bind with `inline_source: {name, schema, table}`: Loom generates the source declaration itself, into its generated sources document, so the declaration is Loom-owned and regenerates with the project instead of being maintained by hand. Both are complete bindings, and the choice is only about who owns the source declaration; it is the same exactly-one choice every staging instance makes, and the metadata reference states both shapes in full. The schema in an `inline_source` binding is the warehouse schema the seed materialises into under the project's dbt profile (the target schema, adjusted by any `+schema` configured for the seed), not the name of the seeds directory; if unsure, look the seed up in the manifest (`target/manifest.json`), where each seed entry records the schema it resolved to.

## Step 5 - Promote

Promotion is one pathway for both draft kinds, and it is **move-only**: the draft was authored in final form, so nothing is retyped, renamed or edited at promotion time. A draft is promotable when its readiness account shows no remaining holes - re-run `loom convert` and read the account (and any transition) rather than trusting the state you remember.

1. If the draft references a synthesised pattern (`<generator>_pattern`), move `local/drafts/patterns/<generator>_pattern.yml` into `metadata/patterns/` unchanged - its name is final and the instance document already references it. Where several patterns already exist for the generator, the instance document's `pattern:` is a `TODO`: choosing among them is yours - a choice you make in the draft, before promotion - and the document is not loadable until you set it.
2. Move the instance document into `metadata/instances/` unchanged, or merge its instance into an existing instances document for the same pattern.
3. If the model has a registration (you registered it in an earlier pass, or it was drafted under `--include-registered`), delete `local/registrations/<model>.yml` now, before the gate: the promoted instance replaces the registration as the model's inventory record, and while both exist `loom validate` fails with `duplicate inventory name`. The registration is retired here, not at swap time.
4. Gate on `loom validate`. The record's `promotion` list repeats these steps for the specific draft in hand.

## Step 6 - Prove equivalence

Promotion puts the instance in the inventory; the swap (Step 7) waits on proof. Build the candidate in a scratch copy of the project: remove the hand-written model file in the copy (an explicit act - `loom generate` will refuse to overwrite it, by design), run `loom generate --write`, then `dbt seed` (if applicable) and `dbt build` in the copy against the **same** source data as the original.

**The properties-document decision comes first**, because the candidate build hits it immediately: `loom generate` publishes a properties document describing the generated model (under `models/_generated/tests/`), and your schema YAML may still describe the model too - dbt refuses a resource described twice. Decide by what your schema entry carries:

- **Only description, columns and tests that carriage or the generator covers** → keep Loom's generated document and delete your entry for the model. Carried generic tests (Step 2) generate inside Loom's document; generated data tests live there too.
- **Content Loom does not generate** - a semantic model defined alongside the entry, or generic tests you keep declaring by hand - → keep your schema YAML and declare `skip_properties_document: true` in the model's instance params, so `loom generate` stops publishing its document for the model instead of you deleting it after every generate. One combination is refused by validation: the opt-out plus a non-empty `generic_tests` - carriage needs the document the opt-out suppresses. If a model needs both, declare the generic tests in your schema YAML by hand, drop the carried `generic_tests` from the instance, and the opt-out stands.
- For an authored (ex-fit or ex-authoring-draft) model, also confirm the declared interface held: the contract's columns, types and constraints; the dbt unit tests as a ready-made oracle (`dbt test` - their given fixtures are the inputs and their expect fixtures the required outputs); the generic tests passing.

**Then the static stage.** `dbt compile` in the original project and in the scratch copy; copy the two manifests to the fixed names `local/equivalence/original.manifest.json` and `local/equivalence/candidate.manifest.json` in the scratch copy (copy each manifest into place before the next dbt run overwrites `target/`); run `loom prove`. The stage compares the two compiled forms under normalisation and reports one of three outcomes per model: **statically proven** (the swap may skip the warehouse comparison below - record the proof record, its JSON output, with the conversion in Step 7), **inconclusive** (proceed to the warehouse comparison exactly as before - the warehouse decides), or no verdict because the manifest pair is absent (the stage did not run). The stage never disproves anything; candidate-only `__`-prefixed system columns are declared differences by rule, not mismatches.

**Then, for every model the static stage did not prove, the warehouse comparison** - the candidate's table against the original's, over the original model's columns:

- equal row counts;
- equal contents - e.g. in DuckDB, compare ordered full-row selects, or a per-column checksum such as `sum(hash(col::varchar))` per column plus a whole-row hash count;
- generator-added columns (`__loaded_at` provenance) are expected additions: list them explicitly as accepted differences, do not let them mask a real one.

**Proof-data standard.** The proof is only as strong as the data it runs over. For a dedup-shaped conversion (silver, and any shape whose rule treats keys specially) the data must contain the rows the shape exists to handle: duplicate keys - including the cases the `dedup_order` tie-break decides - **null keys**, and boundary rows (the first and last row of each group, empty strings, earliest and latest timestamps). A proof run on null-free data is insufficient evidence for a swap: a shape that retains null-key rows and one that drops them produce identical output on data that has none, so the proof cannot tell them apart. If the available data cannot supply such rows, record the proof as partial in Step 7 and name the cases it did not cover. A model that cannot be built twice against identical data (snapshots and other stateful resources) is **not** converted by this pathway - it stays registered and hand-written.

## Step 7 - Swap and record

Swap only on a pass. In the real project: remove the hand-written model file, run `loom generate --write` (the Step 6 properties-document decision applies to this generate too), `dbt build`, and `loom coverage`. If the model carried an enforced contract or attached tests, declare `contract_enforced` in its Loom metadata, per the Step 1 flags, and confirm the model's attached tests still run against the generated model and cover the intent. Generated data tests are additional where the generator emits them; **staging emits none**, so for a staging conversion the kept schema-YAML tests - or the carried ones - are the whole test story. The model's registration was already deleted at promote time (Step 5); the instance is its inventory record now. A re-run of `loom convert` reports the model as controlled.

**Record the outcome** in the commit message: model, generator, the equivalence evidence (the static proof record, or row counts and checksums), any accepted differences, and - for a partial proof - the cases it did not cover.

## After adoption - living on Loom

- **Adding a test to a generated model.** Generic (data) tests are declared in the instance's `generic_tests` and generate into the model's properties document; `loom create-new data-test <model> --test <name> [--column <col>]` prints the declaration (for a hand-written model it prints the dbt-YAML form instead). `loom create-new unit-test <model>` prints a unit-test declaration the same way. A test whose arguments Loom cannot know is printed with a `__TODO__` sentinel the validator rejects - an unfinished declaration can never validate silently. Remember the one refused combination: `generic_tests` on a model that declares `skip_properties_document`.
- **New hand-written models** enter the inventory as `unknown_to_loom` at the next assessment; convert drafts for them, register keeps them out - the same fork as Step 2/Step 3, taken per model as the project evolves.
- **Re-assessment is cheap and derived.** Readiness is recomputed every run from the drafts' current content, so completing a draft's holes, refreshing the manifest, or deleting a registration shows up as a readiness transition in the next report rather than as stale stored state.

## What not to do

- Do not force a fit. A `no_fit` with named signals is a successful assessment outcome: bespoke models stay hand-written and registered. The pathway's value is the inventory plus the provable conversions, not a conversion count.
- Do not call an authoring draft a fit, or its target generator a match. An authoring draft is a starting instance with the transform left as the author's work; treating the destination as a shape claim would recreate, for the majority of models, exactly the false-fit failure the fit rules were tightened against. If the target generator cannot express the transform, the draft stays a draft.
- Do not delete Loom's generated properties document after every `loom generate` to keep your own schema YAML - declare `skip_properties_document` on the instance; that is what the opt-out is for. And do not pair the opt-out with `generic_tests` carriage; validation refuses the pair because the carriage has nowhere to land.
- Do not treat an evidence hole as an authoring hole. An authoring hole is yours to fill; an evidence hole names something Loom could not verify - the remedy is a current manifest or a corrected declaration, after which the same draft re-derives clean.
- Do not let an agent apply `--force`, delete hand-written files, or skip the equivalence build. Those are the practitioner's acts.
- Do not convert a flagged model (contract/Mesh/tests) in the same pass as an unflagged one; flags mean coordination, and mixing them hides the coordination in routine diffs.
