# Loom architecture and current behavior

This is the current behavior reference for dbt practitioners and contributors. Start with the [README walkthrough](../README.md) and use the [canonical terminology](terminology.md) for exact names. Numbered [ADRs](adr/) preserve decisions at the time they were made; [ADR 0023](adr/0023-loom-and-dbt-terminology.md) supersedes their old terminology and interfaces.

The naming contract applies throughout the CLI, metadata, code and examples. Loom-owned interface names changed together; actual dbt resource names and execution semantics did not.

## The authoring-time boundary

```text
Pattern definition + instance set
  -> expanded pattern instances
  -> Python validation
  -> ordinary dbt SQL/YAML files
  -> dbt parsing, execution and tests
  -> warehouse relations

Expanded instances + hand-written model registrations
  -> searchable Loom model inventory
```

Loom is not in the dbt runtime. Generated models and tests are ordinary dbt resources; dbt does not import Loom to run them. Consumer-owned sources, macros and dbt's materializations remain runtime concerns. An instance can emit multiple resources: SCD2 emits a model and snapshot, plus generated test YAML. Staging emits a model that references a dbt source; it does not emit the source declaration.

The consumer owns its metadata, raw data, profile, dbt configuration and generated files. A normal project may review and commit metadata and generated SQL/YAML together. The small checked-in quickstart deliberately keeps outputs ignored so generation is visible on the first run. No sibling project is required by either workflow.

## Authoring and validation

Validation is implemented in Python; Loom does not package JSON Schemas. Authored documents, expanded generator inputs and cross-record relationships are checked by inventory validation. A named pattern reuses those rules; it does not need a separate validation schema.

| Authored document | Location | Required identity |
| --- | --- | --- |
| Pattern definition | `metadata/patterns/<name>.yml` | `name`, `generator` |
| Instance set | `metadata/instances/*.yml` | `pattern`, `instances` |
| Model registration | `local/registrations/*.yml` | `name`, `classification` |

Metadata files may use either the `.yml` or the `.yaml` extension; both are scanned everywhere metadata is read. Defining the same model name in files under both extensions is a duplicate-name error, as it is within one extension.

Command reference: [`loom validate`](commands.md#loom-validate).

### Metadata format changes

Loom documents carry no format version: pattern definitions, instance sets, model registrations, assessment drafts and source-structure baselines state no `format_version`, and the loaders do not look for one. Practitioners pin the Loom release they use. If a future release changes the metadata format incompatibly, the release-hygiene rule applies: the change is called out in the changelog with an explicit migration path for existing documents. A document written by an older Loom that still carries the field loads as if it were absent; source-structure baselines are the one exception, because their envelope is exact - an old baseline is rejected with an instruction to recapture it.

### Patterns and instance sets

Each definition selects exactly one of the 13 registered generators. Its filename stem must match its `name`; definitions are not nested under further directories. Optional `description`, `defaults` and `required_params` describe and constrain reuse.

- An instance set references one named pattern and has at least one member. Each member supplies `name` and `params`. Use an instance set even for one instance; several sets may reference the same pattern.
- Defaults merge shallowly. A field in both `defaults` and `params` is an error, not an override. A `required_params` field must be present in every member's `params` and cannot also have a default.
- Metadata YAML is parsed as YAML 1.1, in which an unquoted `on:` mapping key reads as the boolean `true`. Joins entries (which pair a `model` with an `on` key list) are restored to the intended `on` key at load, so hand-written metadata can use the natural unquoted form.
- Defaults and parameters must be valid generator fields, not identity fields. The generator's own required fields apply after expansion. Duplicate YAML keys, invalid references and duplicate model names are errors. Unknown generator parameters are not comprehensively rejected and may have no effect. Unused pattern definitions are still validated.
- Expanded instances participate individually in search, semantic checks, generation and model coverage. Their origin identifies the member, for example `metadata/instances/hubs.yml#instances[hub_customer]`. Pattern definitions themselves are not model inventory entries.
- Generated provenance includes the definition's identity and fingerprint. Changing a shared definition, even descriptive metadata, can change the provenance of every instance using it. Review the complete generated diff.

Metadata does not evaluate arbitrary SQL or Jinja, and it never evaluates expressions either: the one expression form the vocabulary carries (ADR 0037) is carried verbatim - a bounded plain-SQL string on the column-item surfaces, rendered exactly as written under its required alias, with Jinja delimiters and statement terminators banned at validation and no identifier checking (practitioner-asserted, like measure expressions). Grain, keys, history and output shape belong to the selected generator, not a universal materialization switch. An instance set does not assign dbt groups, access or ownership semantics to dbt nodes. Identifier validation and generated-column collision checks still apply; carried-verbatim expression support is not arbitrary SQL support.

Retired authoring forms and names are not aliases. Update files to the current forms named in the [terminology](terminology.md) and regenerate; recapture older source baselines. Historical ADR examples are not accepted current input.

### Hand-written model registrations

A model registration describes an existing hand-written model without generating or rewriting it. For example, `local/registrations/fct_orders.yml`:

```yaml
name: fct_orders
classification: mart
description: Order totals for reporting
owner: analytics
tags: [orders]
upstream: Order staging and customer history
depends_on: [stg_orders, dim_customers_scd2]
keys: [order_id]
```

`classification` is descriptive, not a request for another generator. `hub`, `link` and `scd2_dimension` are reserved for generated behavior; `staging` can describe a hand-written staging model. Registrations also support descriptive `business_key` and `tracked_columns`. `upstream` is text for discovery, not a dbt `source()` declaration or a validated executable dependency. `depends_on` likewise does not edit dbt SQL. Actual dbt dependencies used by migration planning come from the dbt manifest.

## Daily authoring workflow

The per-command operational reference (parameters, writes, output formats, exit codes, examples) is [docs/commands.md](commands.md); this section is the summary.

Run Loom from the consuming dbt project's directory, or supply `--project-dir <project>`. Manifest paths are relative to that project unless absolute; the normal default is `target/manifest.json`.

| Command | Purpose and prerequisite |
| --- | --- |
| [`loom validate`](commands.md#loom-validate) | Validate authored documents, expanded generator inputs and semantic relationships; no database connection |
| [`loom search`](commands.md#loom-search) | Search the inventory corpus (Loom metadata union the dbt manifest): a project overview with no query, a faceted table for a query, a detail view for a single hit; human-readable text by default, `--format json` for the full structured payload |
| [`loom generate`](commands.md#loom-generate) | Preview by default: validate and report the generated model names, generators, dialect and would-write / would-change counts without writing (every mutating command previews by default and writes only with `--write` or `LOOM_WRITE`) |
| [`loom generate`](commands.md#loom-generate) | Render and write supported dbt artifacts; everything renders before anything is written, and an unmarked differing file at a planned output path blocks all writes unless `--force` |
| [`loom coverage`](commands.md#loom-coverage) | Compare model inventory with project-owned dbt models in a current manifest; text report by default, `--format json` on request |
| [`loom check`](commands.md#loom-check) | Check manifest-backed inventory coverage; read-only, not a dbt invocation |
| [`loom plan`](commands.md#loom-plan) `--from old_model --to new_model` | Read-only manifest-visible consumer inventory for two distinct project-owned models |
| [`loom register`](commands.md#loom-register) | The register half of the assessment of an existing project: per-model state (`controlled_by_loom` / `known_not_controlled` / `unknown_to_loom`) and drafted registrations; the summary reports the readiness distribution; text by default, `--format json` on request; `--write` writes drafted registrations into `local/registrations/` (never overwriting) |
| [`loom convert`](commands.md#loom-convert) | The convert half of the assessment: generator-fit proposals (registered models included), authoring drafts for layered models with no fit, and interface flags; text by default, `--format json` on request; `--write` writes the unified drafts (draft record + final-form instance document + synthesised pattern in its final shape where the project has none) into `local/drafts/` (never overwriting) |
| [`loom inspect-sources`](commands.md#loom-inspect-sources) `--database quickstart.duckdb` | Explicit local physical-column inspection; see its separate prerequisites below |
| [`loom snapshot-sources`](commands.md#loom-snapshot-sources) `--database quickstart.duckdb` | Explicit create-only source-structure baseline |
| [`loom diff-sources`](commands.md#loom-diff-sources) `--database quickstart.duckdb` | Read-only advisory comparison with that baseline |

Use `--manifest` on coverage, check, plan, register, convert and source-structure commands to select a nondefault manifest. Generate does not need a manifest or a warehouse connection. Check does not compile SQL or run models.

Report commands (`search`, `coverage`, `plan`, `register`, `convert`, `inspect-sources`, `diff-sources`) render summary-first, human-readable text by default: counts before detail, non-empty sections only, and low-level identifiers only under `--verbose`. `--format json` emits the complete structured payload unchanged and is the form automation and AI workflows should request explicitly. The text renderings share one grammar across the report commands: the verdict or yield first, then counts, then names - with name lists folded past ten entries under a head sample and a folded count, report-level warnings as a headed section, provenance as a header field, and the migration assessment's prose interface flags reduced to compact codes with a legend in default text (the prose survives verbatim under `--verbose`). The text is a view over the JSON payload: renderers regroup data the payload already carries and never compute facts the payload lacks; the JSON shapes, fields and exit codes are unchanged.

### Finding existing models

Search runs on one selector engine (`loom.selection`) over the inventory corpus: Loom metadata and the dbt manifest unioned into tier-1 model records (with status, Loom enrichment, shape fields and graph edges) and tier-2 records for sources, macros and the other kinds, in full mode when a manifest is present and metadata-only mode when none is. The same engine evaluates `--select` and `--exclude` on every model-scoped command, each command narrowing within its own pool - generation's generatable instances, the assessment's project models, coverage's compared set, prove's stage subjects, validate's authoring documents - with selection always strict, while search alone integrates shape banding on multi-shape-qualifier queries (full matches plus labelled close matches by shape overlap). The grammar mirrors dbt's selector semantics: commas intersect within one selector argument, separate arguments union, graph operators and word-form graph qualifiers traverse the record graph, and dbt-style YAML named selectors are accepted as an input form. The grammar has two layers: a dbt layer (bare names and `*` patterns, `tag:`, `path:`, `package:`, and the graph operators) and a Loom layer of qualifiers over the record fields Loom itself derives - `generator:`, `pattern:`, `paradigm:`, `classification:`, `owner:`, `column:`, `key:`, `input:`, `is:`, `has:`, `doc:`, `kind:`, and the word-form graph qualifiers with their `depth:` companion. Scoped validation is document-granular: `validate --select` includes the instance and registration documents containing a selected model whole, plus the pattern documents those instances use, and checks relationships into documents outside the included set for resolvability only; a selected unmanaged model has no authoring documents, is named as having none, and is not an error. The full grammar is specified in the command reference's [selector grammar](commands.md#selector-grammar) section.

```sh
loom search "key:customer_id,column:email"
loom search downstream:hub_customer
loom search --select has:no-owner
loom search
loom search kind:macro
```

The corpus itself is a derived view, rebuilt from the files on disk on every call - nothing is stored or synced. A tier-1 record unifies one model's metadata entry with its manifest node, and its status is derived from which of the two exist: generated or registered when both do (a metadata entry the manifest still carries), orphaned when the metadata entry remains but the manifest node is gone, and unmanaged when the manifest knows the model and Loom does not. Loom-known records are enriched with the model's generator, pattern, paradigm, owner and tags, its declared grain keys, its declared model and source inputs, and its derivable output columns (flagged partial where derivation is incomplete), plus the graph edges and the health facts state checks already compute (owner and test presence, misplaced and duplicate files). Documentation is unified per record rather than read from one side: the model description and each output column's type and description come from Loom metadata (`description`, `column_docs`, `column_types`) and from the manifest, each field carrying its provenance - `loom`, `manifest`, or `both-agreeing` - with Loom text winning for generated models and the manifest winning otherwise, the losing text kept out of the record entirely; documentation coverage is derived from those unified fields. Tier-2 records carry kind, name, provenance (`project`, or `package` with the package named), file path, description where documented, and graph edges where the manifest provides them. Sources come from the manifest and from Loom's own declarations (a staging model's declared source that the manifest does not carry still appears, as a project record). Macro records additionally carry the ordered argument list - name, type where the manifest declares one, and whether the macro's signature gives the argument a default, read from the signature itself because the manifest's argument list does not record defaults - and the used-by set of models and macros that call the macro. Macro bodies are not indexed: macro discovery is by name, argument names, description, origin and usage only, and two macros are never claimed equivalent because their signatures look alike. In metadata-only mode only Loom-known objects appear and statuses collapse to generated and registered, and every output names the mode it ran in; in full mode a manifest that fails the freshness guard still answers, labelled with the manifest's generation time so the practitioner can judge.

The legacy filter flags (`--text`, `--generator`, `--classification`, `--owner`, `--tag`, `--column`, `--key`, `--dependency`, `--paradigm`) are retired: selector arguments are the one interface, and the retired spellings meet the ordinary unknown-option error. Column search (`column:`) covers derivable output columns, and key search (`key:`) covers declared business and grain keys; graph questions are the `upstream:` / `downstream:` qualifiers or the operator forms. Search's presentation applies the CLI-consistency standard: the no-query overview (counts by status, generator, owner and paradigm, documentation coverage, suggested exception queries drawn from the project's state), the faceted table with tier-2 objects in grouped sections beneath the models (package groups presented exactly like project groups, capped past ten rows with their full count and a narrowing query), a `kind:` qualifier promoting any tier-2 kind to the primary table with kind-appropriate columns, the single-hit detail view (a macro's detail view closes with a slot-compatibility note derived from its signature: whether the macro can fill the staging column mapping slot - the one generator slot a macro can fill - which parameter the column fills by default, and how many `macro_args` a mapping must supply to reach the last argument without a default), `--fields` choosing the JSON record fields, and the `--count-only` / `--names-only` scripting forms. Search is explicit today: generation does not automatically recommend similar models or block a new model because its intent resembles an existing one.

Command reference: [`loom search`](commands.md#loom-search).

### Fresh manifests and coverage

After generation, run dbt with the project's profile, for example:

```sh
dbt parse --profiles-dir .
loom check
```

A dbt build can also produce the fresh manifest, as in the quickstart. Coverage checks project ownership, model contents and configured model paths, including additions, deletions and disabled models. Model YAML, source declarations, project configuration and macros changed since parsing may require reparsing. Wrong-project, stale, invalid or missing manifests are errors, not evidence of successful coverage.

Timestamp checks are deliberately conservative: copying or touching inputs can require reparsing even when their content is unchanged. They cannot prove every environment variable, profile value or external consumer is current. These checks operate on project models, not a claim that every dbt resource must have a Loom model registration.

Command reference: [`loom check`](commands.md#loom-check) and [`loom coverage`](commands.md#loom-coverage).

### Generation and output behavior

`generate` without `--write` previews: it validates metadata, resolves output paths and reports the model names, generator names, selected dialect and would-write / would-change / unchanged counts (the per-file classification under `--verbose`), and writes nothing. `generate --select` restricts generation to the models the shared selector grammar matches (see [Finding existing models](#finding-existing-models)): commas within one selector intersect, repeated flags union, `--exclude` subtracts, and a selector that matches no generated model is an error. `loom --version` prints the installed Loom version.

Normal generation renders every selected model before writing anything, so a render failure cannot leave a half-generated project. A failure in one model does not stop the others; the command reports each failure and exits nonzero after writing the successful models. Obsolete files are not removed.

Generated files are owned by Loom: every generated SQL and data-test file carries a `-- Generated by Loom` (SQL) or `# Generated by Loom` (YAML) header on its first line, and regeneration overwrites Loom-headed files freely. Before writing, generation checks every planned output path: an existing file whose first line lacks that header, and whose content differs from what would be written, is treated as practitioner-authored work. If any such file exists, generation refuses, names each file, writes nothing, and exits nonzero. `--force` is the explicit override that replaces those files anyway. Removing the header from a generated file is therefore how a practitioner takes a file back under manual control. SCD2 snapshot and view SQL keep separate create-only semantics: existing files at their paths are skipped regardless of ownership.

Output roots follow the project's configured paths: generation writes under the **first** `model-paths` entry and the **first** `snapshot-paths` entry of `dbt_project.yml`, defaulting to `models` and `snapshots` when unconfigured - the same configuration the reading side (inventory, coverage) already honoured.

An instance may additionally declare **`output_subpath`** ([ADR 0026](adr/0026-practitioner-controlled-output-layout.md)) - a relative path template carried in the instance params or the pattern defaults - to nest its generated files under those roots: `{name}`, `{generator}`, `{paradigm}`, `{owner}` and `{tags}` placeholders expand from the instance's own metadata, and literal segments are the practitioner's own structure. The same resolver (`loom.output_layout`) drives generation, `generate --check` (which also reports a Loom-headed file left at a model's previous location as stale), search's `output_path`, and coverage: a controlled model whose manifest file is not at its expected path is reported under `misplaced_generated_models` by `coverage` and fails `check`. With no `output_subpath`, paths are exactly the flat paths above. With the defaults: staging SQL is written to `models/_generated/staging/<instance>.sql`; other model SQL uses `models/<instance>.sql`. SCD2 writes its snapshot SQL to `snapshots/<instance>.sql` and its view to `models/<instance>.sql`. Data-test YAML is written to `models/_generated/tests/data_<instance>.yml`. Model properties (catalogue) entries live in the same `_generated/tests/` documents; see [Generated model properties](#generated-model-properties-catalogue). Inline sources declared by staging/bronze metadata produce one further output - the generated sources document described under [Staging](#staging) - at `models/_generated/staging/sources.yml`.


`generate --check` is the drift gate: it renders the full plan in memory - SCD2 outputs included, even though normal generation treats them as create-only - compares every planned file with what is on disk, and writes nothing. It exits nonzero and names each divergent file as missing or differing; a stale SCD2 output is flagged with its remedy (delete the file and regenerate, since generation will not update it in place). The intended CI shape is: `loom validate`, `loom generate --check`, then the dbt build - a metadata edit without regeneration, or a hand-edited generated file, fails the first gate. Obsolete files left behind by a removed instance are not detected (nothing renders them); removing an instance still means removing its outputs explicitly.

Command reference: [`loom generate`](commands.md#loom-generate).

### Paradigm context

Paradigm is propagated from the generator registry into every expanded instance. Search's `paradigm:` qualifier filters across paradigm boundaries; coverage reports `by_paradigm` statistics. Model registrations have no paradigm, so a `paradigm:` qualifier never matches them.

Naming conventions are enforced per-generator, not per-paradigm. The generator registry is the single source of truth for the paradigm-to-generator mapping.

## Generator guarantees

All examples below show current parameters. Put them under an instance's `params`, with a matching pattern definition and required shared fields such as `description` and `owner`. The README contains the complete staging form. Generator guarantees are enforced by generated SQL guards and/or dbt data tests. They are distinct from dbt's `contract: {enforced: true}` feature, which Loom emits only on explicit opt-in - never by default (ADR 0032; see "Enforced contracts (opt-in)" in the catalogue section).

Each generator's accepted parameters, required set and top-level types are defined once in `loom.contracts` and enforced by `loom validate`: an unknown parameter is an error that names the accepted set - a misspelled parameter can no longer be silently ignored - as are a missing required parameter and a wrong top-level type (a scalar where a list belongs). An explicit null is not a supplied value: it fails a required parameter with a required-value error and an optional parameter with a type error, and null members inside `source_ref`/`inline_source` mappings are rejected; omitting a parameter remains the way to leave it unset. The "Parameters" line under each generator heading below is checked against the contract by the test suite, so this document and the validator cannot drift apart. Nested shapes (staging `columns` items, `aggregation` internals, `joins` items) keep their dedicated validators. The practitioner-facing [metadata reference](metadata-reference.md) presents the metadata formats as annotated working examples - a pattern definition and one composite instance - covering every envelope key, parameter and nested shape; this document keeps the design rationale behind those shapes.

### Incremental core

Incremental rendering is built on one shared core in `loom.renderers.base`, not per-generator string building: a resolved `IncrementalSpec` (strategy, unique key, `on_schema_change`, cursor and lookback), a config-block builder, the `is_incremental()` guard fragments (single- and composite-key anti-joins, and the cursor predicate), and a generate-time dialect gate. The persistent hub renders through the core today, byte-identically to its pre-core output (golden-fixture tested across all dialects); the remaining generators adopt it as their incremental support lands.

Dialect capability is data on `loom.dialects.Dialect`: `incremental_strategies` is the adapter-verified set of dbt incremental strategies per dialect - `merge` exists on seven of the eight (not ClickHouse), `append` and `delete+insert` are absent on BigQuery, `insert_overwrite` exists only where the adapter supports partition replacement, and `microbatch` is absent on ClickHouse - plus a `cursor_dateadd_template` per dialect for cursor lookback arithmetic. The gate checks a declared strategy against that set when generating (never at validate time: validation is dialect-free) and fails before anything is written, naming the model, the strategy, the dialect and the strategies it supports. Which strategies Loom admits at all is a separate, contract-layer decision: v1 admits `append`, `delete+insert` and `merge`; `insert_overwrite` and the microbatch keys are rejected by name in validation. Each generator's contract gains the `incremental` parameter only when its renderer honours it. The generators that admit the block are `link`, `bronze`, `normalized_table`, `fact`, `dimension`, `silver` and `gold` (see their sections below); the generators outside that set - hub (whose `retention: 'persistent'` switch predates the block), staging, view, `scd2_dimension`, `obt` and `mart` - still reject the parameter as unknown.

The block itself is one mapping: `strategy` (required), `unique_key`, `on_schema_change` (default `fail` - a schema change fails the build rather than silently never reaching the target, the opposite of dbt's `ignore` default), `cursor_column`, `lookback` (with `lookback_unit`), and `full_refresh` (only emitted when declared; a declared `false` locks the model against `--full-refresh`). A lookback requires a cursor and a key strategy - an append has no key to reconcile rows a widened window re-reads. Presence of the block *is* the opt-in to `materialized='incremental'`; there is no separate materialization field, and a generated `config(...)` takes precedence over any project-level `+materialized` default for that model. Declaring the block in a pattern's `defaults` makes every instance of the pattern incremental (the loader's rule against a field living in both defaults and instance params applies as usual). Runtime proof for the incremental shapes is DuckDB lifecycle tests; on the other dialects the strategies are parse-checked only - generation, not execution (see Compatibility and evidence).

### Staging

Staging is the adapter layer between raw sources and the rest of the project. A staging model projects exactly one source table: explicit column renames and casts, no joins, no business logic. It is where direct dbt `source()` references belong - every other generator consumes models through `input_models`, never raw sources - so when a source changes shape, exactly one staging model absorbs the change and nothing downstream moves. Staging SQL is written to the Loom-owned `_generated/staging/` directory under the model root (the same directory that holds the generated sources document described below); medallion bronze is the one partial exception, since as a raw landing layer it may declare an `inline_source` of its own instead of reading a staging model.

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `source_ref`, `inline_source`, `columns`, `filter`, `unit_tests`. Required: `description`, `owner`, `columns`.

Choose exactly one source mode per instance/raw table:

- `source_ref: {name: raw, table: customers}` reuses a native dbt source under the project's configured model paths. Loom never edits that source YAML. Where it lists columns, raw mapping names are checked statically against them. Omitting its column list does not inspect the physical table.
- `inline_source: {name: raw, schema: main, table: customers}` declares the source inline and renders as a dbt `source()` reference. Generation completes the declaration ([ADR 0024](adr/0024-inline-source-completion.md)): the source declarations for every inline source with no authored declaration are emitted, grouped by source name, into the Loom-owned `models/_generated/staging/sources.yml`, derived from all inventory entries (never just a `--select` subset). An authored declaration for the same (source, table) governs and is not re-emitted; staging keeps the rule that an authored source *name* is used via `source_ref`, never inline. Two inline declarations of one not-authored source name with different schemas are a validation error. An inline source may also declare dbt source freshness: `loaded_at_field` (a column name) and `freshness` with `warn_after` and/or `error_after` thresholds, each `{count, period}` with a positive-integer count and a period of `minute`, `hour` or `day`. Both are emitted onto the table's generated declaration in `sources.yml`, so `dbt source freshness` covers inline sources exactly as it covers authored ones. Two entries declaring different freshness for the same (source, table) are a validation error.

`source_ref` emits `models/_generated/staging/<instance>.sql` with dbt `source()` and ordered, explicit `columns` mappings, plus a `__loaded_at` timestamp column. An explicitly empty `columns` list is rejected: it would silently become a `SELECT *` projection, contradicting the explicit-projection contract. Extra physical columns do not enter the output; a removed or renamed mapped column fails at dbt build. Update mappings and regenerate deliberately. There is no ingestion or automatic introspection during validation, generation or check.

Projection/rename is the default. A mapping may opt into a consumer-authored column macro:

```yaml
columns:
  - {source: subtotal, alias: subtotal_cents}
  - {source: subtotal, alias: subtotal, macro: cents_to_dollars}
  - {source: tax, alias: tax_dollars, macro: cents_to_dollars, macro_args: [4, true, USD]}
  - {source: created_at, alias: created_date, macro: dbt.date_trunc, macro_args: [day], macro_column_param: 2}
```

The macro receives the quoted input-column name first (position 1 by default), then optional fixed literal arguments. The reference is a bare project-macro name - a safe, nonreserved identifier defined under the project's `macro-paths`, which validation verifies - or a dotted `package.name` naming a package macro (Tier 1): validation cannot resolve package macros (it is deliberately manifest-free), so it warns "unverified package macro" and never blocks, while `loom check` and `loom convert` verify the reference against the manifest - existence, and arity against the manifest's ordered arguments including defaults read from the macro signature - and `loom generate` stays manifest-free and renders the invocation exactly as declared. Convert drafts a macro column only for a call in this slot's own calling convention in the model's final projection (the source column as the first, quoted argument, then fixed literal arguments) and only when the call verifies against the manifest; a slot-shaped call that cannot be verified drafts as the plain column mapping and stands as an evidence hole naming the macro, and any other macro shape - a list or computed argument, a call inside a CTE - is named in the model's no-fit reason instead of the generic Jinja signal. A mapping may instead pin the column's argument position with `macro_column_param` (Tier 2), for macro signatures that are not column-first: the integer is the 1-based position the source column fills, `macro_args` carries the remaining literal arguments in signature order, and the renderer - still manifest-free - assembles the call from the declaration alone (in the example above, `dbt.date_trunc('day', 'created_at')`); an explicit position of 1 renders byte-identically to the slot. Validation requires `macro` for the key, requires a positive integer, and bounds the position by the arguments the declared call itself supplies; the position's fit against the macro's real signature is subsumed by check's arity verification of the same call. Convert drafts a `macro_column_param` column for a final-projection call whose arguments are all fixed literals with the source column in a later position - quoted or bare - resolving which argument is the source column against the source's documented columns (exactly one may match; a literal like `'day'` is identifier-shaped but is not a source column), gated on the same manifest verification, with the draft evidence naming the position and the signature parameter it fills; an unresolvable or unverifiable Tier 2 call drafts as the plain mapping with an evidence line, never a silent pass. A macro call embedded in a larger expression (Tier 3) stays in the authoring class: the no-fit reason names the macro path for the manual rewrite, and no fit rule drafts it. `loom check` verifies every generated instance's macro references against the manifest as errors alongside the coverage verdict. Arbitrary SQL/Jinja strings are not accepted in either form. `macro_args` requires `macro` and accepts 1–8 integers, booleans or restricted strings up to 200 characters, not floats/nulls or nested values. Staging does not execute or validate the macro body; its correctness and adapter compatibility belong to the dbt project.

One raw column may have distinct transformations, but only one plain mapping and no duplicate macro/argument transformation. Output aliases are unique. Neither staging nor its source declaration receives inferred uniqueness or non-null data tests. The literal/name restrictions above are enforced at validation.

### Hubs

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `business_key`, `retention`, `record_source`, `unit_tests`. Required: `description`, `owner`, `input_models`, `business_key`.


The `hub` generator requires a `hub_*` model name, one `business_key`, and its staging inputs: `input_models` naming one staging model for a single-source hub, or several for a multi-source hub ([ADR 0030](adr/0030-dv-core-hardening.md)). Every upstream model must be in the model inventory as generated staging or a hand-written registration with `classification: staging`. For generated staging, the key must be a declared output alias of every input; registration does not prove physical column presence.

```yaml
input_models: [stg_customers]
business_key: customer_id
```

A multi-source hub unions its inputs' distinct keys and holds one row per hash. When the same key arrives from several inputs in one load, the earliest input in declared `input_models` order wins:

```yaml
input_models: [stg_customers_crm, stg_customers_web]
business_key: customer_id
retention: persistent
record_source: {stg_customers_crm: crm, stg_customers_web: web}
```

For `hub_customer`, the output is `customer_hk` and `customer_id`. The business key is normalized with `upper(trim(cast(key as varchar)))`; its hash is SHA-256 represented as lowercase hex (DuckDB `sha256` or Databricks `sha2`). Duplicate normalized identities collapse to one row. Null keys are excluded; blank strings are not treated as null. String comparison guards (`!= ''`) are not applied; integer keys from seed data or staging are supported. These hashes are identities, not security tokens.

Default `retention: current` produces a rebuilt current-state dbt table: identities missing from a later extract disappear. Opt-in `retention: persistent` produces an incremental append model that retains seen identities and inserts only unseen hashes (an `is_incremental()` anti-join on the hash guards the append). Databricks persistent hubs instead use dbt's `merge` incremental strategy keyed by the generated hash. Both assume one writer and retained database state. `dbt build --full-refresh` deliberately resets retained identities to the current extract.

Only a persistent hub may opt into `record_source`, a restricted nonblank literal label. This adds `__record_source` and `__loaded_at`, the processing time of the run that first inserted an identity. It is not source event time, multi-source row lineage or an inferred timestamp. Full refresh resets it. Without `record_source`, neither audit column is emitted.

A multi-source hub declares provenance per source with the same parameter in its mapping form: `record_source` maps each `input_models` entry to its label, and each hub row carries the label of the input that supplied it. Provenance is all-or-nothing - either every input is labelled or none is, and a mapping must name exactly the declared inputs; both forms require `retention: persistent`.

Do not switch retention or add provenance to an existing consumer-facing hub without assessing the changed behavior/columns. Prefer a new side-by-side model; a deliberate full refresh accepts history loss. There is no concurrent-writer guarantee. Persistent hub generation renders on every dialect - an incremental append guarded by an anti-join on the hash, except Databricks, which merges on the hash; runtime proof for the persistent shape is DuckDB (lifecycle tests), with the other dialects parse-checked for it. Details: ADRs [0008](adr/0008-first-customer-hub.md), [0009](adr/0009-opt-in-hub-retention.md), [0010](adr/0010-audited-hub-provenance.md) and [0030](adr/0030-dv-core-hardening.md) (multi-source hubs).

### Links

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `endpoints`, `variant`, `dependent_child_key`, `payload_columns`, `incremental`, `unit_tests`. Required: `description`, `owner`, `input_models`, `endpoints`.


The `link` generator is intended to represent a narrow current-state relationship. Its output is covered by non-empty runtime tests on DuckDB (`tests/test_link_runtime.py`), and the quickstart build produces one link row per distinct order/customer pair:

```yaml
input_models: [stg_orders]
endpoints:
  - {hub: hub_order, key: order_id}
  - {hub: hub_customer, key: customer_id}
```

Every endpoint must be a generated hub entry in the model inventory; each endpoint key must match its hub's `business_key`. Endpoint keys must be distinct, and a hub may appear at most once among the endpoints: hierarchical links (a hub linked to itself) are rejected in validation, because Loom derives each endpoint's hash column name from the hub's name and a repeated hub has no unambiguous column naming (ADR 0030). The input model must be **generated staging**, with every endpoint key declared as an output alias; a descriptive staging registration is not enough for links. Generated hash columns must be distinct, and input keys cannot collide with generated intermediate columns. The generated model uses `ref()` for the input and every endpoint hub, and the same normalization/SHA-256 rule as hubs.

For `link_order_customer`, the output is `order_customer_hk`, `order_hk` and `customer_hk`; the link hash is SHA-256 of the concatenated fixed-length 64-character component hashes. Each join compares a hub's hash column to the hash computed from the input key with the same normalization and hash rule as that hub, and the generated relationship tests are attached to the endpoint hash columns.

A link may relate more than two hubs (ADR 0030): the `endpoints` list simply carries further `{hub, key}` mappings, and the link hash composes every endpoint's hash in declared order - the fixed-length components keep the concatenation unambiguous at any arity. A link may also declare a `dependent_child_key`, a staging column that is part of the link's grain without belonging to any hub (an order-line number distinguishing two relationships between the same hub pair): its normalized form joins the hash composition as the final component, and the link stores it normalized, as hubs store their business key.

The generated model implements the ADR 0030 fail-fast contract: it **fails at build time** when the input contains a null key, a blank key (empty after key normalization), or a key with no row in the corresponding hub - such keys are never silently filtered or dropped. A one-row guard CTE (`__link_guard`) counts invalid and unmatched keys using hash expressions identical to the joins, and the dialect's `link_guard_template` turns a non-zero count into a query failure. SQL has no portable raise, so the failure idiom is per-dialect data: a message-bearing function where the engine has one in plain SQL (DuckDB `error()`, BigQuery `ERROR()`, Databricks `raise_error()`), and integer division by zero elsewhere (PostgreSQL, Redshift, Snowflake, Azure Synapse; ClickHouse uses `intDiv`, since its `/` is float division and `1/0` is `inf`). The guard row drives the query through a LEFT JOIN, so the gate fires even when every input row is invalid and the link would otherwise come out empty; an empty input is valid and produces an empty link. Unmatched-key detection treats an empty hash column as unmatched as well as NULL, because ClickHouse fills LEFT JOIN misses with default values instead of NULLs. The guard is exercised by runtime tests on DuckDB, including a poisoned quickstart build; the other dialects are parse-checked and carry the same composed templates, which is not runtime proof.

The standard variant is a rebuilt dbt table, not a durable audited Data Vault link. It has no retention, record source, load/effectivity time or deletion history, even if a referenced hub is persistent. See [ADR 0030](adr/0030-dv-core-hardening.md).

`variant: non_historised` (ADR 0030) is the immutable-event link. It requires a `dependent_child_key` - the event discriminator, without which repeat events between the same hubs are indistinguishable - and a non-empty `payload_columns` list, carried from staging onto the link row. It loads as an append-only incremental model that inserts an event when its link hash is not already held; an event is never updated, so a re-sent event keeps its first-seen payload. `payload_columns` is accepted only on this variant: on a standard link, descriptive attributes belong in a satellite. Neither variant emits provenance columns.

The standard variant accepts an `incremental` block. The link hash is the model's key - a deterministic function of the endpoint hashes, stable at any arity, dependent-child key included - so `unique_key` defaults to it and may only ever name it. With `strategy: append` the load is guarded by an anti-join on the link hash: re-observed relationships are never duplicated and new ones append. With `merge` or `delete+insert`, dbt reconciles on the hash. No cursor window is offered for links: the guard scans the full input and both hubs on every run, incremental or not - its whole point is that invalid keys fail the build rather than being filtered, and scoping it to a window would let invalid keys outside the window pass silently. Incremental links therefore save writes and downstream scan, not guard cost. The block is rejected on the non_historised variant, which already *is* an incremental append load: one behaviour, one switch.

### Satellites

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `parent`, `input_models`, `record_source`, `variant`, `attribute_columns`, `driving_key`, `subsequence_key`, `unit_tests`. Required: `description`, `owner`, `parent`, `input_models`, `record_source`.

The `satellite` generator ([ADR 0028](adr/0028-satellite-generator.md)) carries a hub's or link's descriptive attributes as append-only history - the Data Vault construct hubs and links alone cannot express. One instance hangs off exactly one parent (the `parent` block, naming a generated hub or link) and reads its payload from generated staging (`input_models`, the link discipline: the key and payload columns are validated staging aliases). The block takes one of two shapes: for a hub parent, `{model, key}`, where `key` names the staging column carrying the hub's business key and must equal the hub's `business_key`; for a link parent, `{model, endpoints}`, whose endpoint list mirrors the link's own endpoints and keys and must match them in declared order - and, when the link declares one, the block's `dependent_child_key` mirrors its dependent-child key the same way. Instance names carry the `sat_` prefix. The input grain is one row per parent key per load.

```yaml
parent: {model: hub_customer, key: customer_id}
input_models: [stg_customers]
attribute_columns: [customer_name, customer_email]
record_source: crm
```

A closed `variant` enum selects the loading semantics:

- `standard` (default) is the historised satellite. Each load computes a **hash diff** - the dialect's SHA-256 (`hash_diff_template`) over the canonical payload string: each `attribute_columns` value cast to the key text type, `NULL` replaced by `~`, joined with `|` in declared order - and appends a row only when the diff differs from the latest held row for that parent key. The table stores the hash diff as `__hash_diff`. A second output, the `<name>_current` view, exposes the latest row per parent key without the hash diff. Inventory coverage treats the view as part of the satellite's own record, not a model needing a separate one.
- `non_historised` is the immutable-event shape: a load appends a (parent key, payload) row only when an identical row is not already held - decided by a hash diff recomputed over the held rows, never stored. A changed payload is a new row; nothing is ever updated or removed.
- `record_tracking` takes no payload. It appends an appearance row (`__is_deleted` false) when a parent key is present in the input but not currently active for the instance's `record_source`, and a deletion row (`__is_deleted` true, its `__loaded_at` the applied-deletion timestamp) when a currently active key is absent from the input. It is the vault's only deletion story in this build.
- `multi_active` (ADR 0028) is the standard shape at a finer grain: a required `subsequence_key` (a staging column - a phone type, an address type) distinguishes several simultaneously-valid attribute rows per parent key, and the latest-row resolution, anti-join and `<name>_current` view all partition by (parent key, subsequence key).
- `effectivity` (ADR 0028) end-dates relationships: a link-parent satellite with no payload and a required `driving_key` naming one of the parent link's endpoint keys. The table is a **presence log** - one row per relationship per load batch in which the input carries it (the input contract, as for record-tracking, is that each load enumerates the full current relationship set) - and the generated `<name>_windows` view derives the effectivity date windows from it: `__valid_from` is a maximal run of consecutive batches' first load timestamp, `__valid_to` the first later batch in which the relationship is absent (`NULL` while still effective). A batch in which no relationship at all is present leaves no trace in the log, so a window can only close at a batch the satellite observed.

All variants are dbt `incremental` models with the `append` strategy, guarded by `is_incremental()` anti-joins against their own prior state - the hub's executed form. Provenance is mandatory, unlike hubs: `record_source` is a required parameter and every row carries `__record_source` and `__loaded_at`. The satellite joins its input to the parent on the computed parent hash (the hub/link hash rule applied to the input keys), so it never inserts a key the parent does not hold at load time. It carries no fail-fast guard and **no `relationships` data test** to the parent: a current-retention hub forgets identities that leave the extract while the satellite correctly retains their history, so satellite rows legitimately outlive parent rows - referential permanence is the persistent hub's contract. Loads assume one writer and retained state; `dbt build --full-refresh` resets a satellite to the current extract.

The lifecycle is runtime-proven on DuckDB (`tests/test_satellite_runtime.py`: load, no-change reload, change, disappearance and new-key loads across the core variants; `tests/test_satellite_second_wave_runtime.py`: per-sub-sequence change detection and the effectivity close-and-return lifecycle); other dialects are parse-checked only - generation, not execution (see Compatibility and evidence).

### Point-in-time tables

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `parent`, `satellites`, `unit_tests`. Required: `description`, `owner`, `parent`, `satellites`.

The `pit` generator ([ADR 0035](adr/0035-pit-snapshot-control-ghost-records.md)) emits a point-in-time table over one hub or link parent: one row per (parent key, snapshot date) carrying, for each declared satellite, the `__loaded_at` of the satellite row effective at that date - turning temporal satellite joins into equi-joins on (key, `<satellite>_ldts`). Snapshot dates are the **load calendar**: the distinct `__loaded_at` values across the declared satellites (no calendar construct is fabricated; period-end cadences are a downstream filter). When a key has no satellite row at or before a snapshot, the cell holds the **ghost timestamp** `0001-01-01 00:00:00` rather than NULL, so PIT rows are complete and a consumer joining on a ghost timestamp finds no row - the ghost's meaning is "no data yet". The key universe is the parent's held keys: a `retention: current` hub bounds its PIT to current keys, so PIT parents are normally persistent hubs. Effectivity satellites are not PIT satellites - their point-in-time answer is the windows view. The PIT is derived state and rebuilds as a dbt `table` each run.
### Reference data

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `business_key`, `record_source`, `unit_tests`. Required: `description`, `owner`, `input_models`, `business_key`, `record_source`.

The `reference_hub` generator ([ADR 0034](adr/0034-reference-constructs.md)) holds reference code sets - countries, statuses, units - whose keys are externally governed codes rather than enterprise business keys. It is hub-shaped (hash column derived from the `ref_` name prefix, the code carried alongside its hash) with two deliberate differences from `hub`: provenance is **mandatory** (`record_source` is a required parameter; every row carries it and `__loaded_at`), and retention is always persistent - codes are never forgotten because an extract omitted them. A reference satellite is an ordinary `satellite` instance whose parent is a reference hub; the satellite machinery accepts that parent exactly as it accepts a hub.

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `reference_hub`, `satellites`, `business_key`, `attribute_columns`, `variant`, `snapshot_timestamp`, `unit_tests`. Required: `description`, `owner`, `reference_hub`, `satellites`, `business_key`, `attribute_columns`.

The `reference_table` generator flattens one reference hub with one of its satellites (the one-entry `satellites` list) into a consumer view. `variant: latest` (the default) emits the satellite's latest row per code; `variant: full` emits every satellite history row; `variant: snapshot` emits the latest row per code among rows loaded at or before a declared `snapshot_timestamp`. The projected columns are declared (`attribute_columns`, validated against the satellite's own list) and `business_key` mirrors the reference hub's key. Latest/snapshot require a standard-variant satellite (its stored hash diff is the tie-break); full also accepts non-historised.
### Bridge tables

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `parent`, `path`, `weighting`, `unit_tests`. Required: `description`, `owner`, `parent`, `path`.

The `bridge` generator ([ADR 0036](adr/0036-bridge-tables.md)) pre-joins a hub to the hubs it reaches through links, so relationship traversal is a flat scan. The `parent` block's `model` is the anchor hub; `path` is an ordered list of `{link, hub}` steps - each step's link must connect the hub reached so far to the step's hub, every other endpoint of the link must already be reached (n-ary links traverse deterministically), and a path never revisits a hub. The bridge is a dbt `table` rebuilt each run over the **current** link graph: Loom links retain no load history, so there is nothing historical to snapshot - a relationship's lifespan lives in its effectivity windows and satellite resolution in PIT tables. Rows carry the anchor hash, each reached hub's hash in path order, and the build's `__loaded_at`. With `weighting: equal`, a `__weighting_factor` column (`1.0 / COUNT(*)` per anchor) keeps measures aggregated through the many-to-many bridge from double-counting - the Kimball multi-valued-dimension bridge use.

### SCD2 dimensions

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `business_key`, `tracked_columns`, `variant`, `previous_value_columns`, `filter`. Required: `description`, `owner`, `input_models`, `business_key`, `tracked_columns`.

The `scd2_dimension` generator emits a dbt check-strategy snapshot and dimension view, **not detached MERGE**:

```yaml
input_models: [stg_customers]
business_key: customer_id
tracked_columns: [customer_name]
```

The `stg_` input prefix is required. `tracked_columns` must be a non-empty list; an empty list is rejected rather than producing a view that selects the business key twice. For instance `dim_customers_scd2`, the snapshot dbt resource is `dim_customers_scd2_history`, while its SQL file is `snapshots/dim_customers_scd2.sql`. Loom checks required keys/tracked columns against generated staging aliases and rejects collisions with generated validity and snapshot columns. The view SQL file is `models/<instance>.sql`. The view exposes the business key/tracked columns with aliases `valid_from`, `valid_to` and `is_deleted` for dbt's snapshot metadata, over **all** history rows - open, closed and tombstone; there is no current-row filter (filter on `valid_to IS NULL` in a downstream model if only current state is wanted). `is_deleted` is cast to the dialect's boolean type via the dialect's `deleted_flag_template`, because adapters materialize `dbt_is_deleted` with inconsistent types (VARCHAR `'True'`/`'False'` on DuckDB, native boolean elsewhere). ClickHouse is the exception: dbt-clickhouse snapshot tables carry no deleted-flag column at all, so the view emits a constant `FALSE AS is_deleted` there (see below).

The `variant` parameter ([ADR 0031](adr/0031-kimball-fact-flavours-and-scd-types.md)) selects which SCD shape the dimension model exposes over the **same** generated snapshot: `type_2` (the default) is the full-history view described above. `type_3` exposes current state only - business key, tracked columns, a `previous_<column>` for each column named in `previous_value_columns` (required, non-empty, drawn from `tracked_columns`), `__valid_from` and `__is_deleted`; previous values come from a `LAG` window over the snapshot history, so "previous" is the preceding held row, including across a deletion tombstone. `type_6` exposes the full history plus the Type 1 and Type 3 elements: a `current_<column>` for every tracked column (the value on the key's current row) and the `previous_<column>` columns (also required). Generated `previous_*`/`current_*` names must not collide with declared columns; validation fails explicitly when they do. Type 3's business-key test is an unconditional `unique` (one current row per key); Type 6 keeps the current-row uniqueness form.

The snapshot uses `hard_deletes='new_record'` where the adapter accepts the property. dbt-clickhouse rejects `hard_deletes` outright, so the generated config omits it for ClickHouse. Supply complete current-state extracts with a unique non-null business key: a partial or CDC feed would incorrectly mark omitted keys deleted. Changes, deletion tombstones and reappearance are history semantics, not just current-row projection. This snapshot behavior has been directly exercised on DuckDB, including rename and delete lifecycles. Other adapters and dbt versions are not established by these tests.

The snapshot warehouse schema is the profile target schema. Loom provides no variable to override it. Use `dbt build --profiles-dir . --select +dim_customers_scd2` to build dependencies, snapshot and view in order. Snapshot lifecycle needs real dbt builds across extracts.

### Kimball facts

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `dimension_keys`, `measure_columns`, `variant`, `period_key`, `milestone_keys`, `incremental`, `filter`, `unit_tests`. Required: `input_models`, `dimension_keys`, `measure_columns`.

The `fact` generator produces a Kimball-shaped table with measure columns, dimension-key values and a `__loaded_at` column. It does not join or enforce foreign-key relationships to dimension tables:

```yaml
input_models: [stg_orders]
measure_columns: [quantity, unit_price, total_amount]
dimension_keys: [customer_id, product_id]
```

The single `input_models` entry must reference a model in the inventory whose output columns Loom can determine: a staging instance (its column aliases), a hub (its business key and hash column, plus `__record_source` and `__loaded_at` when the hub itself declares `record_source` - persistent hubs only), or a link (its own and every endpoint hash column; links emit no audit columns). Loom validates that all measure columns and dimension keys are among the referenced model's output columns - which is what lets a fact read a link directly in the vault-core-to-marts composition. A measure item may also be a `{source, alias}` rename (its source is validated the same way) or an `{expression, alias}` item (ADR 0037; the expression is carried verbatim and its identifiers are practitioner-asserted, never validated). A fact with no measure columns over a link is a factless fact. A dimension key that references no dimension at all - an order number carried on the fact itself, say - is a degenerate dimension: declare it in `dimension_keys` like any other key.

The `variant` parameter ([ADR 0031](adr/0031-kimball-fact-flavours-and-scd-types.md)) declares the fact's flavour: `transaction` (the default), `periodic_snapshot` or `accumulating_snapshot`. A flavour is a grain contract on the input, which must already carry one row per grain element - the renderer projects, it does not aggregate or fabricate rows. `periodic_snapshot` requires `period_key`, naming the dimension key that identifies the period; period completeness (a row exists for a period whether or not activity occurred) is the input's responsibility, and semi-additive measure behaviour is a query-time property, not something generation changes. `accumulating_snapshot` requires `milestone_keys`, a non-empty list drawn from `dimension_keys`: the milestone date keys of the process the fact tracks, one row per process instance. Milestone keys are exempt from the generated `not_null` tests below - a milestone is legitimately null until it occurs. All flavours are full rebuilds unless an `incremental` block is declared (below); the accumulating flavour's canonical update-in-place load is that block's key mode, not part of the flavour declaration. Generated SQL uses `{{ config(materialized='table') }}` and selects the dimension keys, measures, and a timestamp column.

Generated data tests include `not_null` on each dimension key and measure column, whatever the model is named - except an accumulating fact's milestone keys, which carry no `not_null` test (above). Fact model names follow the `fct_<instance_name>` convention, but the convention is not a gate: tests are generated for off-convention names too.

An `incremental` block turns the fact incremental in one of two modes. **Key mode** (`merge` / `delete+insert`) requires a declared `unique_key` - the fact's grain. There is deliberately no default: `dimension_keys` are foreign keys, and two fact rows routinely share every one of them, so the key must be declared from `dimension_keys` / `measure_columns` rather than inferred. An optional `cursor_column` (an emitted column, `__loaded_at` included) bounds the scan while dbt reconciles by key, boundary rows included. **Append mode** requires a `cursor_column` and reads only rows *strictly after* the target's current maximum cursor value: a row restated at an already-loaded cursor value is not captured - append mode is for append-only event facts; if the source restates, use key mode. The flavours share the emitted column set (the period and milestone keys are dimension keys), so the block composes with them under the same rules, with one exception: `append` is rejected by name for `accumulating_snapshot`, whose milestones complete by updating the grain row already loaded. No composite-unique test is generated for key mode: key uniqueness rests on the strategy mechanics (duplicate keys in the incoming batch fail the merge loudly on most adapters) plus the practitioner's own tests.

### Kimball dimensions

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `natural_key`, `surrogate_key`, `attribute_columns`, `incremental`, `filter`, `unit_tests`. Required: `input_models`, `natural_key`, `surrogate_key`, `attribute_columns`.

The `dimension` generator produces a Kimball dimension table with a surrogate key (generated via `ROW_NUMBER()`), a natural key, and attribute columns. This shape is **SCD Type 1**: the current attribute values overwrite in place on every rebuild and no history is kept - use `scd2_dimension` (Types 2, 3 and 6) when history matters. A mini-dimension (SCD Type 4, or Type 5 with a Type 1 outrigger) composes from this generator: build the mini-dimension as a `dimension` over the rapidly changing attributes and carry its surrogate key in the fact's `dimension_keys`; key carriage is practitioner-declared, there is no separate construct:

```yaml
input_models: [stg_customers]
natural_key: customer_id
surrogate_key: customer_sk
attribute_columns: [customer_name, email, created_at]
```

The single `input_models` entry follows the same rule as facts: a staging instance, a hub, or a link in the model inventory - a dimension over a hub is the vault-core-to-marts composition. Loom validates that the natural key and all attribute columns are among the referenced model's output columns. An attribute item may also be a `{source, alias}` rename (its source is validated the same way) or an `{expression, alias}` item (ADR 0037; carried verbatim, identifiers practitioner-asserted). Generated SQL uses `{{ config(materialized='table') }}` and selects the surrogate key, natural key, attributes, and a timestamp column.

Generated data tests include `not_null` and `unique` on the natural key and `unique` on the surrogate key. The natural-key `unique` test is what makes duplicate natural keys fail visibly: surrogate keys are assigned by `ROW_NUMBER()` over the natural key, so undetected duplicates would produce duplicate dimension rows. Dimension model names follow the `dim_<instance_name>` convention, but the convention is not a gate: tests are generated for off-convention names too.

An `incremental` block admits `merge` only, keyed on the `natural_key` by default. The restriction is the surrogate key: surrogates are positional (`ROW_NUMBER()` over the full input), so `append` cannot express attribute updates, and `delete+insert` would re-insert rows under freshly numbered surrogates, silently re-pointing the facts that reference them - both strategies are rejected by name. Under merge, two mechanisms keep existing members' surrogates stable: the surrogate column is excluded from merge updates, and in the incremental branch new members are numbered from the target's current maximum plus their batch position, so a key is never re-issued. An optional `cursor_column` (an emitted column, `__loaded_at` included) bounds the batch; without one the batch is the full input and the saving is write-side only - the common case at dimension sizes. Two honest limits: surrogate gaps and ordering are not stable across incremental history (a late-arriving member receives a higher surrogate than an earlier member that sorts before it; surrogates are meaningless identifiers, but no ordering assumption on them survives), and a `--full-refresh` rebuild renumbers positionally - the deliberate identity reset `--full-refresh` means for every incremental model. Because `merge` is the only admitted strategy and the ClickHouse adapter has no merge, dimension has **no incremental strategy on ClickHouse** in v1: the generate-time gate rejects it there, naming the strategies ClickHouse does support, and ClickHouse dimensions use table mode.

### Medallion bronze

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `inline_source`, `incremental`, `unit_tests`. Required: `inline_source`.

The `bronze` generator produces a raw load: selects all columns from the source with a `__loaded_at` provenance column.

```yaml
inline_source: {name: raw, schema: main, table: customers}
```

`inline_source` must name a non-empty source `name` and `table`; validation rejects a bronze instance without them rather than rendering a placeholder source reference. It accepts the same optional freshness declaration as staging's inline sources (`loaded_at_field` + `freshness` - see [Staging](#staging)), emitted into the generated sources document. Generated SQL uses `{{ config(materialized='table') }}` and emits a `source()` reference. The matching source declaration is generated by `loom generate` into `models/_generated/staging/sources.yml` unless an authored declaration for the same (source, table) already exists, in which case the authored one governs (see [Staging](#staging)). No data tests are generated.

An `incremental` block turns the raw load incremental. Bronze declares no key of its own and Loom cannot know the source's columns, so the block must carry its own guard - validation is structural rather than membership-based. With `strategy: append`, declare either `unique_key` (an anti-join guard on that column, or a composite key via a list) or `cursor_column` (a strict cursor window: only source rows *after* the target's current maximum are read), but not both. The strict window has the append honesty consequence: a row restated at an already-loaded cursor value is not captured - append bronze is for append-only sources; if the source restates, use a key strategy. With `merge` or `delete+insert`, `unique_key` is required (there is no default to fall back to) and an optional `cursor_column` bounds the scan, boundary rows included - dbt reconciles them by key. `cursor_column: __loaded_at` is rejected by name: that is bronze's own load stamp, and the source has no such column to bound against. Columns named by the block are asserted by the practitioner: Loom does not know the source's columns and does not pretend to check them.

### Medallion silver

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `dedup_key`, `dedup_order`, `columns`, `incremental`, `filter`, `unit_tests`. Required: `input_models`, `dedup_key`.

The `silver` generator produces a cleaned/deduplicated model using `ROW_NUMBER()` partitioned by `dedup_key`, ordered by `dedup_order` when declared and by `__loaded_at DESC` otherwise.

```yaml
input_models: [bronze_customers]
dedup_key: customer_id
```

The single `input_models` entry must reference a model in the inventory; the intended Medallion flow commonly points to bronze. Generated SQL uses `{{ config(materialized='table') }}` and selects only the latest row per `dedup_key` under the declared ordering (`dedup_order` is a verbatim SQL ORDER BY body, e.g. `record_updated_at DESC, id ASC`; it exists so a migrated dedup keeps the tie-break that decides which row survives). The intermediate `rn` column never appears in the output: where the instance declares `columns` (the input model's columns) they are projected explicitly, with `__loaded_at` appended when not listed; otherwise the dialect's star-exclusion syntax (`* EXCLUDE` / `* EXCEPT`) drops `rn`. Dialects with no star-exclusion syntax (postgres, redshift, azuresynapse) require `columns` when a `dedup_key` is set - generation fails with that instruction rather than emitting a model carrying `rn`. Generated data tests include `not_null` and `unique` on the dedup key. Equal load timestamps do not define a tie-breaker. Without a `dedup_key`, silver passes its input through unchanged: the input already carries `__loaded_at`, and appending a second one would produce a duplicate column.

An `incremental` block turns the dedup into **key-scoped recomputation**. The block admits the key strategies only - `merge` and `delete+insert`; `append` is rejected by name, because the dedup output is the current winner per key (replacement semantics) and an append would duplicate keys on every rerun. The block requires a `dedup_key` - pass-through silver has no key whose rows an incremental run could recompute - and a declared `unique_key` must equal the `dedup_key`: the batch computes one winner per dedup key, so the strategy must reconcile on that same key. The cursor (default `__loaded_at`; declare `cursor_column` when the input's arrival stamp lives in another column) has a dual life here: it never bounds which rows load, it *names the touched keys*. An incremental run computes the affected-key set - the distinct dedup keys of input rows inside the cursor window (rows at or after the target's maximum cursor value, widened by any `lookback`) - then recomputes the winner over the input's **full history** for exactly those keys and writes that batch under the declared strategy; untouched keys' rows are not rewritten. Recomputing over full history, rather than deduping the window's rows, is what keeps a late-arriving older copy of a touched key from evicting the held winner: the winner under the dedup ordering may be a row the window does not contain, for example when `dedup_order` orders by a business timestamp and the cursor tracks arrivals. The winner's `__loaded_at` is carried from the winning input row, never restamped, and the batch keeps exactly the table-mode output column set, so the internal `rn` column never reaches the target.

The incremental result equals a full rebuild exactly when two conditions hold, and both are the practitioner's to hold:

- **(E-S1) Window completeness** - every dedup key whose input rows changed since the last run has at least one row inside the scan window. Size `lookback` to cover the input's arrival lateness: a change that arrives later than the lookback spans is never seen, and its key is never recomputed.
- **(E-S2) Input retention** - the input retains the full row history per key for as long as incremental runs are expected to match rebuilds. Recomputation reads the input as it now is; a source that purges superseded versions makes recomputation converge on what remains, not on what was once loaded.

Two degradations are designed behaviour, not defects. **Delete-blindness:** a key whose rows are *all* deleted from the input is never named by the window, so its stale winner persists in silver across incremental runs; a periodic `dbt build --full-refresh` is the documented reconciliation. (Partial deletions for a *touched* key are handled exactly, by the recomputation.) And when the input is a **staging view**, whose `__loaded_at` is computed at query time, every row reads as newer than the target maximum on every run: the affected set is all keys and the run recomputes everything - correct, merely un-incremental; incremental silver wants a persisted input. Ties under the dedup ordering resolve exactly as they do in table mode: equal ordering values define no tie-breaker, and none is invented here.

### Medallion gold

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `joins`, `aggregation`, `incremental`, `unit_tests`. Required: `input_models`.

The `gold` generator produces business aggregates over silver inputs.

```yaml
input_models: [silver_order_items]
```

`input_models` may name multiple inventory models. With multiple inputs, each additional input needs an explicit `joins` entry naming the join keys (the same `{model, on}` shape the OBT generator uses); a multi-input aggregation without join keys is rejected, and Loom never generates a cross join:

```yaml
input_models: [silver_customers, silver_orders]
joins:
  - {model: silver_orders, on: [customer_id]}
```

Or with aggregation:

```yaml
input_models: [silver_customers]
aggregation:
  group_by: [customer_id]
  measures:
    - {column: order_count, expression: COUNT(*), alias: order_count}
```

Inputs must reference models in the inventory; silver inputs are the intended Medallion flow, but the validator does not enforce that generator kind. With aggregation, SQL groups by the configured columns. Every input is aliased by its model name, and group-by columns in a multi-input query are qualified with the first input's alias, so a join key present in several inputs is never ambiguous. Every measure requires a non-empty `expression`. An aggregation with measures but no `group_by` is a grand total and emits no `GROUP BY` clause. Without aggregation, gold is a single-input pass-through with `__loaded_at`; pass-through with multiple inputs is rejected rather than silently dropping the extra inputs. Generated data tests include `not_null` on each measure alias.

An `incremental` block turns an aggregate gold model into **grain-period recomputation**. The block is admitted in aggregate mode only: a grand total (measures, no `group_by`) and pass-through gold both reject it by name - the grand total's grain would be the whole table, so every run would recompute everything and "incremental" would be a label on a full rebuild, and pass-through has no grain at all. The admitted strategies are the key strategies - `merge` and `delete+insert`; `append` is rejected by name, because a recomputed grain replaces the stored grain row. The grain - the `group_by` tuple - is the key: it is the default `unique_key`, and a declared `unique_key` must equal the `group_by` set (in any order), since the batch computes one row per grain and the write strategy must reconcile on that same identity. A `cursor_column` is **required** and names a column of the first (driving) input - in the canonical chain, the silver input's event or load column; it must also be a column gold emits (a `group_by` column, a measure alias, or `__loaded_at`), because the window bound is the target's maximum cursor value.

An incremental run computes the affected grains - the distinct `group_by` values of driving-input rows inside the cursor window (rows at or after the target's maximum cursor value, widened by any `lookback`) - then re-runs the aggregation over the **full current inputs** for exactly those grains and writes that batch under the declared strategy; untouched grains are not rewritten. For multi-input gold the cursor is read against the first input only and the joins are not windowed: joined inputs are read whole for the touched grains, so a change confined to a joined input (a dimension-side attribute inside a measure expression, say) names no grain and recomputes nothing - the multi-input limit of the semantics below.

The value stored for a touched grain is **exact**: it is the same aggregation, over the same full grain population a table rebuild would read at that moment - nothing is aggregated incrementally, so there is no sum-of-sums drift, for any measure expression, including non-algebraic ones (`count distinct`, percentiles). Which grains get recomputed is the approximation. Gold incremental equals a full rebuild exactly when three conditions hold, and all three are the practitioner's to hold:

- **(E-G1) Driving accompaniment** - every change to any input that affects a grain's measures is accompanied by a driving-input row for that grain inside the window. True for append-mostly fact inputs with in-lookback restatements; false when a joined input changes alone (above).
- **(E-G2) Arrival within lookback** - late data arrives within `lookback` of the target's cursor maximum. Older late data touches no window and its grain is never recomputed: a sale that arrives ten days late under a three-day lookback leaves its grain's stored total stale indefinitely, until a full-refresh or a run with a larger lookback. For gold, `lookback` is therefore a business decision (how late can this feed arrive?), not a performance knob.
- **(E-G3) Grain survival** - a grain whose rows are *all* deleted from the driving input never enters the affected set, and its stored row persists (the same delete-blindness as silver); a periodic `dbt build --full-refresh` is the reconciliation.

The published stance, in the design's words: *Gold incremental is exact for grains touched within the lookback window and stale - silently - for everything else; full-refresh is the reconciliation.*

One consequence of recomputation is the `__loaded_at` asymmetry: the stamp is `current_timestamp` at the run that (re)computed *that grain row*, so untouched grains keep their earlier stamps and two rows in one gold table can carry stamps from different runs. It is a last-recompute stamp, and any consumer treating it as an event time is misreading it. In particular, `MAX(__loaded_at)` on gold is **not** a safe cursor for a downstream incremental consumer - a recomputed old grain's stamp advances while its grain period stays old - so downstream models should cursor on a grain or event column, not on gold's stamp.

### OBT (One Big Table)

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `columns`, `joins`, `unit_tests`. Required: `input_models`, `columns`, `joins`.

The `obt` generator produces a wide denormalized table with all dimensions joined into a single table:

```yaml
input_models: [stg_orders]
columns: [order_id, amount, status]
joins:
  - model: stg_customers
    on: [customer_id]
    columns: [customer_name, email, country]
  - model: stg_products
    on: [product_id]
    columns: [product_name, category]
```

The `input_models` entry and joined models must reference inventory entries. `columns` names the input model's own columns to project; it is required. The declared source columns are projected first, in declared order; a declared `__loaded_at` is skipped because the output always ends with a freshly stamped `__loaded_at`. `joins` must be non-empty: an OBT without joins would contain only a timestamp, so validation rejects it. Each join specifies a model to LEFT JOIN, the join keys, and columns to select; join keys are compared as text (both sides cast to the dialect's key text type), so a normalized hub key - stored as text - joins cleanly to a raw typed staging key on engines that refuse mixed-type comparisons. Selected columns are aliased to their original column names, except that when a later contribution repeats a column name already projected the later occurrences are aliased `<join_alias>_<column>` so output column names stay unique. Generated SQL uses `{{ config(materialized='table') }}`.

### 3NF normalized tables

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `entity_columns`, `attribute_columns`, `incremental`, `filter`, `unit_tests`. Required: `input_models`, `entity_columns`, `attribute_columns`.

The `normalized_table` generator produces one normalised table from one input model. That is Loom's 3NF subset, stated plainly:

- **Generated:** a single table whose columns are split by role - `entity_columns` (identifying; the first is the entity key) and `attribute_columns` (descriptive) - projected row-for-row from the input, so the atomic grain is retained, plus a `__loaded_at` provenance column.
- **Validated, never generated:** relationships. Validation checks that the input model exists in the inventory and that both column roles are declared; the `view` generator (below) joins normalised tables on keys the practitioner declares explicitly. Loom emits no primary/foreign keys and no constraints - entity integrity is enforced by the generated data tests described below, not by SQL.
- **Not generated:** decomposition. Loom does not split one declaration into several related normalised tables. Splitting a source into entities is the practitioner's design - one `normalized_table` per entity, each authored separately.
- **No in-paradigm history:** the subset has no time-variance construct. An entity that needs history is composed with `scd2_dimension` beside it.

So "3NF" in Loom names this dbt-adapted subset - normalised tables, validated relationships, reporting views - not strict third normal form in the Inmon sense. A decomposition build is revisited only on named practitioner evidence.

```yaml
input_models: [bronze_customers]
entity_columns: [customer_id, customer_name, email]
attribute_columns: [country, segment, created_at]
```

The single `input_models` entry must reference a model in the model inventory. Entity columns are selected as identifying columns and attribute columns as descriptive columns; entity columns are listed with the entity key first. Generated SQL uses `{{ config(materialized='table') }}` and includes a `__loaded_at` timestamp column. Generated data tests check every entity column for `not_null` and the first entity column - the entity key - for `unique`: uniqueness is a test contract that fails the build, not SQL deduplication.

An `incremental` block admits key strategies only - `merge` or `delete+insert`; `append` is rejected by name, because an append cannot apply an attribute correction to an existing entity row. The key defaults to the first entity column (a composite entity key may be declared as `unique_key`, validated against the entity and attribute columns), and an optional `cursor_column` - an emitted column, `__loaded_at` included - bounds the scan while dbt reconciles by key.

### 3NF reporting views

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `joins`, `select_columns`, `unit_tests`. Required: `input_models`, `select_columns`.

The `view` generator produces a reporting view that joins normalized tables:

```yaml
input_models: [n_customer, n_order]
joins:
  - {model: n_order, on: [customer_id]}
select_columns:
  - {source: n_customer.customer_id, alias: customer_id}
  - {source: n_customer.customer_name, alias: customer_name}
  - {source: n_order.order_id, alias: order_id}
```

Each model in `input_models` must reference a model in the inventory. `select_columns` specifies which columns to select with source references and aliases. Every input is aliased by its model name in the FROM/JOIN clauses, so the `select_columns` references resolve. Additional inputs are LEFT JOINed to the first input on the explicit keys in their `joins` entries; a multi-input view without join keys for an input is rejected - Loom does not generate cross joins. The join keys are the practitioner's declaration: Loom validates that every input exists in the inventory and that every selected column declares its source and alias, and generates no keys or constraints itself - in the 3NF subset, relationships are validated, never generated. Generated SQL uses `{{ config(materialized='view') }}`. No data tests are generated.

### Hybrid marts

Parameters (checked against the generator contract in `loom.contracts`): `description`, `owner`, `tags`, `column_docs`, `column_types`, `generic_tests`, `input_models`, `aggregation`, `unit_tests`. Required: `input_models`, `aggregation`.

The `mart` generator produces lightweight aggregations directly on staging inputs, for teams that want minimal intermediate layers:

```yaml
input_models: [stg_orders]
aggregation:
  group_by: [customer_id]
  measures:
    - {column: order_count, expression: COUNT(*), alias: order_count}
    - {column: total_amount, expression: SUM(total_amount), alias: total_amount}
```

The single `input_models` entry must reference a model in the model inventory. Generated SQL uses `{{ config(materialized='table') }}` and performs a simple aggregation with `GROUP BY`. An empty `group_by` list is a grand-total aggregation and emits no `GROUP BY` clause. Every measure requires a non-empty `expression`. No JOINs are performed. Generated data tests include `not_null` on each measure alias when measures are present.

## Generated dbt tests

Loom emits native default-on dbt `data_tests` in `models/_generated/tests/data_<model>.yml` for these supported guarantees:

| Generator | Column | Data tests |
| --- | --- | --- |
| Hub | Generated hash | `unique`, `not_null` |
| Link | Generated link hash | `unique`, `not_null` |
| Link | Each endpoint hash column | `relationships` to the corresponding hub's hash column |
| SCD2 | Business key | `not_null`; `unique` where `__valid_to is null` |
| Fact | Each dimension key | `not_null` |
| Fact | Each measure column | `not_null` |
| Dimension | Natural key | `not_null`, `unique` |
| Dimension | Surrogate key | `unique` |
| Silver | Dedup key | `not_null`, `unique` |
| Gold | Each measure alias | `not_null` |
| Normalized table | Each entity column | `not_null` |
| Mart | Each measure alias | `not_null` |

There are no inferred staging/raw-source tests and no business-specific tracked-value tests. Generated data tests are default-on and cannot be suppressed. Declared dbt unit tests are a separate, metadata-driven mechanism - see the next section. See ADRs [0015](adr/0015-contract-derived-dbt-data-tests.md) and [0029](adr/0029-metadata-declared-dbt-unit-tests.md) for the current boundary.

## Generated dbt unit tests

A practitioner may declare dbt **unit tests** in an instance's params with the optional `unit_tests` parameter (ADR 0029). Generation emits them centrally - declaration-driven and generator-agnostic, unlike the data tests above - to `models/_generated/tests[/<output_subpath>]/unit_<model>.yml`, one file per model holding all its declared tests, through the same generation plan (so the generated-header ownership rule and `generate --check` apply unchanged). Each test names synthetic `given` rows for the model's declared upstreams and the `expect` rows the model must produce:

```yaml
unit_tests:
  - name: dim_customers__surrogate_keys_follow_natural_key_order
    given:
      - input: hub_customer
        rows:
          - {customer_id: 1, customer_hk: "<sha256 hex>"}
    expect:
      rows:
        - {customer_sk: 1, customer_id: 1, customer_hk: "<sha256 hex>"}
```

Rules the validator enforces: test names are lowercase snake_case and unique across the project (dbt requires project-unique names); `given` inputs must be declared upstreams of the model (a staging or bronze model's given instead names its own source table, checked against the instance's declared source, and generation renders it in dbt's `source('name', 'table')` form); fixture rows are `dict` (lists of scalar-valued mappings) or `csv` (one string with a header line) - `format: sql` and fixture files are not supported; where output columns are derivable, row keys must be columns the input (for `given`) or the model (for `expect`) actually generates; `overrides` is restricted to `overrides.macros.is_incremental`. Two conditions warn rather than fail: a test name that does not start with the model name, and an `expect` that pins `__loaded_at` (a run-time stamp - expect rows normally omit it, which the partial-column `dict` format makes possible). A fixture proves a single-load transformation; it never proves an incremental or snapshot lifecycle.

Support in v1: every generator except `scd2_dimension` - `staging`, `bronze`, `satellite`, `pit`, `reference_hub`, `reference_table`, `bridge`, `dimension`, `fact`, `hub`, `link`, `silver`, `gold`, `mart`, `normalized_table`, `obt` and `view`. A satellite's tests declare `overrides.macros.is_incremental` where the fixture needs to state which load it models, as on any incremental model. A persistent hub's tests must each declare `overrides.macros.is_incremental`, so the fixture states which load it models. Links carry a caveat: the The consistency guard runs inside the link's SQL, so a fixture whose given rows are inconsistent fails the *run* rather than asserting - negative scenarios (null or unmatched keys) are not expressible as unit tests on links; positive-path fixtures work. Only `scd2_dimension` is excluded (the model under test is a dbt snapshot, and dbt unit tests target models). Staging and bronze were excluded until the 2026-11-04 extension ruling: the stated reason described the emitter, not a dbt limit, and a practitioner unit test mocking a staging model's own source - macro transform included - passes `dbt build`. `unit_tests` is declared per instance, never in pattern `defaults`.

`loom create-new unit-test <model>` prints a starter `unit_tests:` snippet - for a generated model, the metadata form (sentinel rows keyed by the derivable columns); for a hand-written model, the dbt-YAML form derived from the manifest's `depends_on` and documented columns - instead of a hollow generated file; validation rejects any fixture still containing the `"__TODO__"` sentinel. Unit tests require dbt >= 1.8; that floor is conditional (it binds only projects that declare `unit_tests`), and `generate` warns - without failing - when the project's existing manifest was produced by an older dbt.

## Generated model properties (catalogue)

Generation also emits a descriptive dbt properties entry for every generated model that has anything truthful to publish. It carries the model's `description` from metadata, its output columns in SELECT order, and per-column `description` / `data_type` entries where Loom or the practitioner actually knows them. The entry is **descriptive only**: it never carries a `contract:` stanza or constraints, so nothing in it can fail a build - it exists so generated models appear documented in dbt docs and catalogue tools instead of bare.

dbt rejects a model described by two schema files, so each generated model has exactly **one** Loom-owned schema document under `models/_generated/tests/` (mirroring any `output_subpath`, like the SQL layout): when the generator emits data tests, the properties entry is merged into `data_<model>.yml` alongside them; otherwise the document is `properties_<model>.yml` in the same directory and carries the properties entry alone.

Output columns come from one resolver (`loom.output_columns`), derived from each renderer's SELECT construction - the same source the fact/dimension input validation uses. A column's `data_type` is emitted only when it is known: columns whose type Loom's own SQL fixes (hash columns as text, `__loaded_at` as a timestamp, a dimension surrogate key as an integer, SCD2 `__valid_from`/`__valid_to`/`__is_deleted`), or types the practitioner declares. Every other column is listed without a type rather than with a guessed one.

A further common parameter, **`generic_tests`**, carries practitioner-declared generic (data) tests into the same document (under the tests principle: Loom hosts practitioner declarations where dbt's single-description rule leaves them nowhere else to live, and originates none). Each declaration names a `test`, optionally a `column_name` (absent = a model-level test), and optional `kwargs` / `config` mappings; generation merges them into the entry's `tests:` lists - the bare name, or the mapping form `{test: {**kwargs, config: …}}` - appending a column entry for a declared column the properties entry does not list, so a carried test is never dropped. One merge rule applies where a declaration meets a test the generator itself emits: a declaration duplicating an emitted test - same test name, same column, same kwargs, the identity dbt names a generic test by - is emitted once, not twice, because a second entry with the same name fails `dbt parse` outright; a `config` the declaration carries is merged onto the single emitted entry, never silently dropped, while a declaration whose kwargs differ from the emitted test's is a distinct test to dbt and is carried in full. Loom never validates test names (dbt resolves them from installed packages at build); `loom check` warns about the declarations generation will collapse this way, naming each declaration's config (or that it carries none), so the redundancy is named where the practitioner can act on it. The parameter is accepted by every generator, is barred from pattern defaults like `unit_tests`, and conflicts with `skip_properties_document`: carriage needs the document the opt-out suppresses, so validation rejects the combination, naming both keys.

Convert completes the carriage circuit: a fit draft copies the model's manifest generic tests into the draft's `generic_tests` - the completeness mirror of the declared-interface copy, keyed on the test's attached model (a `relationships` test belongs to the model it is declared on, not its target). The copy drops the kwargs dbt injects into every manifest test (`model`, `column_name` - re-derived from the attachment at build) and preserves a declared warn severity as `config`; singular tests are bespoke SQL files, not declarations, and are never copied. Copied tests are carried content, not readiness holes. At check, two rules watch the carriage: an exact duplicate of a generator-emitted test (same name, same column, same kwargs - derived from the generator's own rendered tests) warns, because generation emits the test once, merging any config the declaration carries onto the emitted test, so a declaration carrying config is not redundant - dropping it loses the config; and a declaration the manifest's test nodes do not attest for the model warns, becoming an error under `loom check --strict` - the one rule `--strict` scopes to.

Two optional parameters, accepted by every generator, let practitioners supply the rest:

- **`column_docs`**: a mapping of column name to description text.
- **`column_types`**: a mapping of column name to a *generic* data type - `text`, `integer`, `bigint`, `decimal(precision,scale)`, `boolean`, `date`, `timestamp` - rendered into the target dialect's spelling in the generated YAML (e.g. `text` renders `VARCHAR` on DuckDB, `STRING` on BigQuery, `String` on ClickHouse). These spellings are what dbt docs should display; they are descriptive, not the adapter-reported strings an enforced contract would be checked against.

Validation checks both mappings: values must be strings of the right shape, and where a model's output columns can be determined, every named column must be one of them. Where they cannot - the `SELECT *` generators (bronze; staging without an explicit `columns` list) and chains that pass such an input through (silver or gold over bronze) - the named columns cannot be verified, and the properties document publishes the practitioner's declared columns as a partial list alongside the model description. Silver's output columns are its declared `columns` projection plus the carried `__loaded_at` when it declares one, and otherwise exactly its input model's (its internal dedup `ROW_NUMBER` column is not a model column and is never catalogued); gold's are its grain columns, measure aliases and `__loaded_at` in aggregate mode, or its input's columns plus `__loaded_at` in pass-through mode.

A model with no description, no derivable columns and no declared column documentation gets no properties entry. Registered (hand-written) models get none either: their owners already own the YAML at the model path. A generated model may instead hand its schema YAML back to the practitioner deliberately: the envelope key **`skip_properties_document: true`** (instance params or pattern defaults, instance wins) makes generation emit no properties entry for the model - and, where the generator emits data tests, no merged `data_<model>.yml` at all, since the entry and the tests share the one schema file - with the skip named in the generate report. A Loom-headed document left over from before the opt-out is reported (as stale under `generate --check`), never deleted. `loom check` warns when an opted-out model's manifest entry carries no practitioner description, so opting out and never writing the YAML cannot happen silently. The key exists for converted models that keep practitioner YAML (declared tests, semantic models) under the adoption pathway - set once at promotion, as the draft records' promotion steps suggest for such models. Because the Loom schema document is the one place a generated model is described, practitioners documenting a generated model further should declare it in metadata (`description`, `column_docs`, `column_types`) rather than in a second schema file of their own, which dbt would reject as a duplicate description.

### Enforced contracts (opt-in)

A generated model carries an enforced dbt contract **only** when its metadata opts in - never by default (ADR 0032). The opt-in is the envelope key `contract_enforced` (a boolean), carried in a pattern's `defaults` or an instance's `params` like `output_subpath`, with the instance value winning over the pattern default - the expected shape is a pattern-level `true` for a layer (the mart layer, say) with per-model `false` opt-outs as the escape hatch. It is exempt from the generator contracts and never reaches a renderer.

For an opted-in model the properties entry gains `config: {contract: {enforced: true}}`, a `data_type` for **every** output column, and `not_null` constraints exactly where the generator's data-test matrix already guarantees them (hub/link hash keys, dimension keys, fact keys, the SCD2 business key, the silver dedup key, …) - no other constraint types, and no decorative ones. The one exception is the SCD2 *view*: dbt view contracts carry names and types only, so its business-key constraint is dropped, with a validation warning saying so. The snapshot itself cannot be opted in. Generated data tests are unaffected - enforcement is additive.

The completeness rule is checked at authoring time: `loom validate` **fails** an opted-in model whose output columns are not all typed (from the Loom-fixed types or `column_types`), naming the model and the untyped columns, and fails one whose output columns cannot be derived at all. The practitioner learns this in seconds from `validate`, not from dbt's preflight at build time.

Contract `data_type`s must match what the adapter *reports*, so they render from a per-dialect contract map on the `Dialect` record (`render_contract_data_type`), separate from the descriptive spellings above. Enablement is gated on runtime proof: DuckDB is proven (an opted-in project builds green under `dbt build`, and a wrong declared type fails preflight). Postgres and ClickHouse ship adapter-derived maps but remain unproven; the five cloud dialects have no map yet and fall back to the descriptive spellings. Opting in on an unproven dialect makes `loom generate` warn - not fail - that the declared types are rendered from adapter documentation and may fail dbt preflight.

## Provenance

Generated models carry row-level provenance in at most two columns - `__record_source` and `__loaded_at` - and what they mean is per-generator, not universal. This section is the current record. ADR 0010's provenance table predates the satellite generator and multi-source hubs and is **not** updated (ADRs are historical record); its both-or-neither rule for hubs - Loom never emits `__loaded_at` without `__record_source` - still holds. Every fresh `__loaded_at` is the dialect's `current_timestamp` expression evaluated by the warehouse at dbt run time; Loom never stamps a generate-time value. `__record_source`, where emitted, is a **static string literal from metadata**: it names a source *system*, not a run.

| Generator | Provenance emitted | Semantics |
|---|---|---|
| `staging` | `__loaded_at`, unconditional | Fresh stamp - but staging models are views, so the stamp is the view's **evaluation time** each time it is read. No `__record_source` of its own (a source column passes only if explicitly mapped) |
| `bronze` | `__loaded_at`, unconditional, appended to `SELECT *` | Rebuild time (table materialisation). No `__record_source` |
| `hub` | `record_source` + `__loaded_at`, both or neither, opt-in via the `record_source` param, `retention: persistent` only (ADR 0010) | **First-insert preserved**: the append/merge load writes the pair only for keys not already held, so a row's stamp is when its key was *first* seen. Multi-source hubs (`input_models`) take the literal per source from the mapping form of `record_source`, the earliest declared input winning a contested key |
| `link` | None - every variant, including `non_historised` (its `payload_columns` are data, not provenance) | By design (ADR 0030): a link row is its hashes (plus any dependent child key / payload); it carries no load time and no source |
| `satellite` | `__record_source` + `__loaded_at`, **mandatory** on every row, every variant (ADR 0028) | Per-row first-insert for that row: append-only loads stamp each newly inserted row - a new payload version (standard, hash-diff detected), an immutable event (non_historised), an appearance or deletion row (record_tracking, with `__is_deleted`) - at its insertion, and never rewrite held rows. The standard variant's `<name>_current` view exposes the currently held version's stamps |
| `pit` | None of its own - no `__record_source`, no fresh stamp | Derived state (ADR 0035): `__snapshot_date` is a satellite load-calendar value and each `__<satellite>_ldts` cell carries the declared satellite's own `__loaded_at` - the row effective at that snapshot. A key with no satellite row at or before a snapshot carries the ghost timestamp `0001-01-01 00:00:00` in place of a pointer |
| `silver` | No fresh stamp - the input's `__loaded_at` is carried through | Inherited stamp. With a `dedup_key`, the carried stamp is also the default dedup ordering (`dedup_order` param). The internal `rn` dedup helper never reaches the output |
| `gold` | `__loaded_at`, fresh, in both modes | Pass-through: rebuild stamp. Aggregated: one stamp per *aggregate* row - upstream row-level provenance is discarded by the aggregation and replaced |
| `dimension` | `__loaded_at`, fresh | Rebuild time (table materialisation) |
| `fact` | `__loaded_at`, fresh - all variants (`transaction`, `periodic_snapshot`, `accumulating_snapshot`) | Rebuild time; every fact variant is a full rebuild |
| `scd2_dimension` | No `__loaded_at`, no `__record_source` | Validity semantics only: the view (types 2, 3 and 6 alike) exposes `__valid_from` / `__valid_to` / `__is_deleted`, renamed and normalised from dbt's snapshot columns. The values are **dbt's snapshot machinery's**, not Loom-computed; consumers filter `__valid_to IS NULL` for current rows |
| `normalized_table` | `__loaded_at`, fresh | Rebuild time |
| `obt` | `__loaded_at`, fresh | Rebuild time; output is the source columns, join columns and the stamp |
| `view` | None | Selects only declared columns; an upstream stamp appears only if explicitly selected |
| `mart` | `__loaded_at`, fresh | As gold-aggregated: group-by grain, rebuild stamp |

Read honestly, `__loaded_at` has **four semantics under one name**: first-insert (persistent hub; per-version-row in satellites), rebuild time (every table-materialised generator), inherited upstream time (silver), and aggregate-grain rebuild time (gold/mart) - plus evaluation time for the staging view. None is wrong per paradigm, but they are not one "when did this row load" guarantee, and first-insert provenance exists only where a retention pattern demands it.

**No run-level identifier exists.** No generator emits a `batch_id`, `run_id` or `invocation_id`; Loom generates no macros and no run-logging artefacts, and it is not in the dbt runtime - generated models and tests are ordinary dbt resources. Tying rows to the run that produced them is a dbt-runtime concern (dbt's own artefacts and observability tooling), deliberately not a generated column: a run identifier exists only as unevaluated Jinja at generate time, and adding a column to every generated model would be a breaking output-schema change for every downstream consumer.

**The system columns, whole census.** Every column a generator manages itself - as opposed to a column the practitioner declares - carries the `__` prefix, and the full set is small enough to enumerate: `__loaded_at` (the load stamp; per-generator semantics in the table above), `__record_source` (hubs that opt in and satellites, mandatorily: the static source-system literal), `__hash_diff` (satellites: the SHA-256 change-detection diff over the canonical payload string), `__valid_from` / `__valid_to` / `__is_deleted` (SCD2 views of every type, and satellite effectivity windows views: validity semantics), `__snapshot_date` (point-in-time tables: the load-calendar date), one `__<satellite>_ldts` per declared satellite (point-in-time tables: the effective satellite row's load stamp at the snapshot date), and `__weighting_factor` (bridges declared with `weighting: equal`: the per-anchor share that keeps bridge aggregation from double-counting). Internal SQL helpers - dedup row numbers, guard CTEs - live in the same `__` namespace (`__rn`, `__window`, `__link_guard`) but never reach a model's output. The prefix is also the collision rule: `loom validate` reports a practitioner-declared column that collides with a generated system column as one named error, because system columns are fixed and are never renamed per project.

## Source-structure inspection

Explicit local inspection **already exists** and is separate from ordinary authoring. It does not read rows, infer mappings, alter dbt files or connect to a remote warehouse.

Prerequisites:

- Install the optional DuckDB Python module into the **Loom CLI environment** (for example `python -m pip install "duckdb>=1.2,<2"` in that environment). An adapter installed only in a different dbt environment is insufficient.
- Supply an existing project-local DuckDB file whose referenced source relations already exist, and a current project-local DuckDB dbt manifest. Inspection uses resolved database, warehouse schema and table identities, not guessed dbt naming rules. Its freshness checks concern project/source declarations; coverage/migration additionally check active model content.
- Paths must resolve inside the project; missing relations or invalid/stale manifests are errors, not empty success reports.

From the quickstart directory after seeding/building:

```sh
loom inspect-sources --database quickstart.duckdb --source raw --table customers
loom snapshot-sources --database quickstart.duckdb
loom diff-sources --database quickstart.duckdb
```

Inspection reports source/table identities, ordered physical column names and types, and staging models mapping each raw column. Omit `--source` to inspect all staging-referenced sources. `--table` requires `--source`.

Capture inspects all mapped sources and atomically creates `target/loom/source-structure.json`. Use `--output` for another project-local path; it must be Git-ignored. Capture refuses overwrite. A baseline records physical structure, identity and column mapping labels in an envelope of `project` and `sources`, not sampled data. It is not a dbt snapshot or dbt catalog artifact.

Compare accepts `--baseline`, reads the baseline and performs fresh inspection. It reports source additions/removals/location changes, column additions/removals, type changes, order changes and mapped-model impact. A possible rename is remove/add; equivalence is not guessed. **Differences are advisory and return success**. Malformed baselines, missing relations and inspection failures are errors. Comparison does not change the baseline, metadata, generated files or database.

Command reference: [`loom inspect-sources`](commands.md#loom-inspect-sources), [`loom snapshot-sources`](commands.md#loom-snapshot-sources) and [`loom diff-sources`](commands.md#loom-diff-sources).

To accept a new baseline, review changes, explicitly remove the old file and capture again. `dbt clean` removes the default under `target`; preserve a needed baseline in another deliberately Git-ignored project-local location before cleaning. Normal `validate`, `generate` and `check` neither read nor update baselines. ADRs [0019](adr/0019-opt-in-local-source-inspection.md) and [0020](adr/0020-source-schema-baseline.md) record the boundary under their historical names.

## Consumer migration

```sh
loom plan --from dim_customers_scd2 --to hub_customer
```

Both names must identify distinct project-owned models in a current manifest. The report (text by default; `--format json` for the structured payload) lists direct/transitive model dependents and other reachable manifest resources, including exposures, metrics and semantic models where present. External consumer coverage remains **unknown**.

This command does not edit `ref()` calls, equate grains, compare data or automatically migrate anything. Build the replacement beside the existing model, prove appropriate key/history/business invariants, account for external consumers, migrate each dependent deliberately, then retire the old relation. Use dbt model versions where appropriate for breaking consumer-facing changes - but note Loom's own inventory and coverage do not support versioned models: a project carrying model versions fails `loom check` with the versions named as the cause until they are collapsed to a single model. Coexisting Data Vault and Kimball outputs are not a lossless configuration switch. See [ADR 0011](adr/0011-consumer-aware-migration.md).

Command reference: [`loom plan`](commands.md#loom-plan).

## Migration and AI-assisted authoring

Adopting Loom in an existing dbt project is incremental. Hand-written models are described, not replaced, until a conversion is proven: model registrations (`local/registrations/`) bring the models a project keeps hand-written into inventory, `search`, `check`, and coverage without any generation - and registering a model withdraws it from the convert drafting pool (below). New or replacement models are then authored as metadata one at a time and generated individually with `generate --select <name>`, so adoption never requires a big-bang rewrite, and consumer migration stays deliberate (see above).

Everything in this pathway runs locally: Loom reads the project's own files and its dbt manifest in place, and nothing is sent to a hosted service. The manifest-backed commands share one freshness discipline - before any of them reads the manifest, its project identity and timestamp are checked against the project's current files (source inspection applies the same checks to its source inputs), and a stale, wrong-project or invalid manifest is an error, never silently treated as current. That shared guard is what lets `register` and `convert` refuse a stale manifest, naming the stale files, before producing any output.

AI assistance builds on the same surface and is deliberately review-first:

1. An agent reads the project through Loom's JSON outputs - passing `--format json` explicitly - (`search`, `check`, `coverage`, `plan`) and, where relevant, the dbt manifest - never by guessing project structure.
2. The agent drafts **metadata YAML only** - pattern definitions, instances, registrations. It never writes generated SQL or generated data-test YAML: those are `loom generate` outputs, identified by the Loom provenance header.
3. Every draft is gated on `loom validate`. Metadata that does not validate is an error to fix, not a draft to hand over.
4. The practitioner reviews the metadata diff, then runs `loom generate` themselves. Overwriting a practitioner-authored file (`--force`) is always the practitioner's decision, never the agent's.

Loom ships no AI command of its own; the workflow is harness-neutral and works with any agent that can run shell commands and edit files.

For a project being migrated wholesale, [`loom register`](commands.md#loom-register) and [`loom convert`](commands.md#loom-convert) ([ADR 0025](adr/0025-migration-pathway.md)) accelerate the first two steps: the one assessment reads the manifest and the current inventory and reports, per project-owned model, a drafted registration (classification plus its evidence), a generator-fit proposal where the model's output shape matches (staging, bronze, silver - with drafted parameters and recorded evidence), or the specific no-fit reason, and flags interface commitments a conversion would put at risk: an enforced dbt contract, Mesh access that differs from the dbt default, attached tests, downstream consumers, incremental or ephemeral materialization a conversion would silently replace with a full rebuild or a persisted model, dbt group membership, a deprecation date, grants, and physical warehouse configuration such as partitioning and clustering. The access flag fires only for `public` (whose outside-project consumers a conversion would cut off when the replacement narrows back to the default) and for `private` (a restriction a conversion would silently widen to the default) - not for the resolved `protected` value dbt assigns every model, configured or not - and the flag list leads with the contract, attached tests, and downstream consumers. Consumer and test counts include the manifest's semantic-layer and unit-test sections, and the consumer count is decomposed by kind (models, snapshots, exposures, semantic models, metrics, saved queries); the summary reports non-model resources as seen-but-not-assessed counts by type, the semantic-layer sections counted alongside the node types; and each model's record and flags name the metrics and saved queries reachable from it via its semantic models (plan names them too, among its manifest consumers), dbt model version nodes are distinguished in the records by their `version`/`latest_version`, and documented model descriptions are carried into conversion drafts. Drafts harvest the facts the manifest already declares, verbatim: registration drafts copy the model's real description (boilerplate only as fallback, with the provenance noted in the draft evidence) and an `owner` harvested from the manifest's `meta.owner` when it is a non-empty string - no alias keys are read - with the same harvest applied to conversion-draft parameters; documented column descriptions and types are drafted into the `column_docs` / `column_types` parameters (the types in the manifest's own spellings, marked UNVERIFIED in the draft evidence: load-bearing only under an enforced contract); where a bronze or silver draft exists for an incremental model, its declared `unique_key` / `incremental_strategy` / `on_schema_change` are drafted into the `incremental` block and the loss-warning flag does not fire for that model (the flag stays where no draft carries the configuration, including staging fits, whose contract admits no `incremental` parameter); and a conversion draft carries all of the source model's SQL comments verbatim - leading and inline, in source order - as `source_rationale`. Coverage treats a versioned model as a distinct, named problem rather than a duplicate name: Loom tracks models by name and does not support dbt model versions. The report also labels manifest provenance: when the manifest's recorded adapter does not match the adapter the project's profile declares - the signature of a substitute-adapter parse, blessed for assessment only in the playbook - the report carries a warning naming both adapters, since dialect-sensitive conclusions then derive from the substitute's parse rather than the project's own toolchain. The report also declares the manifest's *compilation* provenance: `manifest_provenance` is `parse-produced` when no model node carries compiled SQL (`dbt parse` does not compile model SQL) and `build-produced` when any does (`dbt build` / `dbt compile` populate `compiled_code` with macro-expanded SQL). Fit calibration assumes a parse-produced manifest - the fit rules read compiled SQL where it exists - so a build-produced manifest also earns a warning naming the provenance and directing the practitioner to regenerate with `dbt parse`; the no-manifest remedy and the playbook's Step 0 steer to `dbt parse` for the same reason. The provenance is annotated at the point of use as well (annotate, do not gate): on a build-produced manifest every fit record carries `manifest_provenance`, the text header names the provenance and the readiness headline carries the marker, each per-fit text entry is marked, and a `convert --write` report carries `drafts_provenance` with its written-drafts sections annotated - the drafts themselves are written exactly as before, and a parse-produced report carries none of these annotations. Every model is classified by state: `controlled_by_loom` (generated instance), `known_not_controlled` (registration), or `unknown_to_loom` (manifest only); fit proposals are computed for registered models just as for unknown ones and the report counts them, but registration is the conversion gate: convert drafts only unknown models by default, so registering a model withdraws it from the drafting pool and deleting the registration returns it (`--include-registered` restores the full drafting reach). `--write` writes the drafted registrations into `local/registrations/` and, for each assessed model that earns one, a unified draft into `local/drafts/` - a review area Loom never loads - without overwriting. On a re-run, an existing draft file whose content differs from the draft the current assessment would write is reported as a divergence (a Draft divergences section in text, a `draft_divergences` list in JSON) and left exactly as it stands: divergence is reported, never repaired. The draft is a draft record (the generator and its basis, the evidence or no-fit account, the declared interface, review TODOs, and the exact promotion steps; a staging fit whose model is materialized as anything but a view carries a named materialization TODO among its review TODOs, because the staging generator emits no materialization config and the conversion would otherwise change the materialization silently), a final-form instance document under `instances/` in the shape `metadata/instances/` consumes, and - when the project has no pattern for the generator - one synthesised pattern definition per generator under `patterns/`, named `<generator>_pattern`: a final name the instances reference from the start, its required params taken from the generator contract. Promotion stays a deliberate practitioner act with a deliberately small shape: complete the TODOs in the draft, move the files into `metadata/` unchanged, delete any registration the instance replaces, and gate on `loom validate` - no content edits and no rename step at promotion time. Conversions then follow the [adoption playbook](adoption-playbook.md): one model at a time, gated on an equivalence proof - original and candidate built against identical source data and compared - before the hand-written file is removed. Stateful resources (snapshots) are never proposed. Fit proposals are heuristic and are matched against the model's *output* shape: a bronze fit requires the whole model to be a plain pass-through of its source (a transform, rename, DISTINCT or window anywhere - typically inside a CTE - blocks it), and a silver fit requires a ROW_NUMBER dedup whose `rn = 1` filter binds the final output, with the dedup key drafted from the partition columns (qualifier prefixes stripped) and any dedup ORDER BY drafted into `dedup_order`. Proposals certify shape, not semantics; the equivalence proof remains the gate. The assessment's summary reports the readiness distribution - `ready_to_promote` / `needs_authoring` / `not_draftable` across the non-controlled models - replacing the three yields (`registered` / `conversion_drafted` / `scaffolded`) it once led with, and the convert rendering is fits-first (a Convert section ahead of the no-fit record). Each model's readiness account is derived on every run from the draft's current content plus current evidence: holes are named and kinded (authoring - only the practitioner can supply it; evidence - Loom could not verify it, closable by editing the draft or by improving the project's evidence), constructs that no generator can express stand as blockers (a model carrying one is not draftable, though its draft is still written), and a change against the account stored in the on-disk draft record is reported as a transition. The account lives in the draft record and in the JSON report, never inside the instance; promotion itself is unpoliced, with the validator's rejection of `__TODO__` sentinels as the hard backstop. The programme's operations are the flat verbs `loom register` and `loom convert` over the one assessment engine (each `--write` writes only its own drafts), and `loom plan` stands alone; the retired `loom migration …` group has no aliases, "migration" names the programme only, and the CLI surface is frozen. The assessment invokes the same manifest freshness guard as `loom plan` and the coverage commands - `register` and `convert` hard-fail on a stale manifest, naming the stale files, before producing any output in either format. An UNNEST in a model's SQL counts as a join signal for the fit checks (it flattens rows like one), and a bronze pass-through proposal carries a caveat when the manifest documents no source columns to verify the claim against. The fit rules tighten in the abstain-only direction: a silver candidate whose dedup QUALIFY predicate carries an OR escape (`... = 1 OR <col> IS NULL` - rows beyond the dedup winner are retained, which the silver contract cannot express) is a blocking signal, not a fit; a single-source candidate whose SELECT lists extract dotted struct fields (three or more segments, e.g. `metadata.extract_config.name`) abstains rather than drafting the extractions as flat identity renames; a single-source candidate carrying a row filter (WHERE), a plain-SQL column expression or a LIMIT abstains with the construct named, because a fit draft cannot carry any of the three and a draft ignoring them would silently drop them: the staging generator can express a row filter as authored vocabulary (ADR 0038) and a column expression likewise (ADR 0037), but convert does not draft either, bronze cannot express a filter or an expression, and neither generator can express a LIMIT (a filter vacuous on proof data passes a content comparison while changing production rows - the OR-escape shape); and every generator's draft evidence now carries the documented-columns caveat when the manifest documents no source (bronze/staging) or input-model (silver) columns, or only some of the drafted columns - the draft's columns then rest on the assumption that source column names match output names, which the equivalence proof verifies. A staging draft's column mapping is the one place that assumption is never left implicit: beyond exact matches and the single guarded rename inference, an output the manifest's documented source columns cannot pair is drafted as an unresolved placeholder (`source: __TODO__`) rather than an identity mapping - the evidence names the unresolved outputs and the candidate source columns, the draft record's review todos name the placeholder outputs, and the validator rejects the sentinel in a staging column `source`/`alias` (repeated sentinels reported as unresolved placeholders, not duplicate-`source` errors), so a guessed mapping cannot survive promotion. The authoring tier sits between register and convert: for every unknown or registered model in a dbt layer (the manifest path's folder layout read first, then the `stg_` / `int_` / mart naming prefixes) that no fit rule matched, `loom convert --write` writes an authoring draft in the same unified shape - a draft record and a starting instance document whose inputs are pre-wired from the manifest dependency graph (a one-entry `input_models` only when the graph declares exactly one model input), plus the shared synthesised pattern for the target generator where the project has none. An authoring draft carries only copied facts: the layer; the declared interface, copied verbatim - an enforced contract's columns, types and constraints, attached dbt unit tests' given/expect fixtures, every generic test the project declares - built-in kinds (`unique` / `not_null` / `relationships` / `accepted_values`) and project-defined kinds alike, copied as name, attached column and kwargs verbatim (a test whose manifest metadata cannot be read faithfully is marked `uncopied` in the copy, never silently dropped) - plus singular tests (name and SQL), semantic models' entities, dimensions and measures, and config facts (materialization, partition/cluster keys, non-default Mesh access, group); and a target generator named by layer (staging → `staging`; intermediate → `normalized_table`, or `view` when the model is declared view/ephemeral; mart → `fact` / `dimension` by classification, else `mart`) as the authoring destination. The transform is the single named authoring hole, its unwired required parameters listed; an authoring draft is never worded as a fit or a shape claim, and promotion is the practitioner's act - complete the hole, validate, prove against the declared interface and fixtures, then swap (the playbook's authoring pathway). The declared interface is standard furniture of every draft, not the authoring draft's alone: a fit's record carries the same verbatim copy (before the unification, fit drafts counted the interface in their flags but never copied it). A re-assessment reports each authoring draft candidate's state - `untouched` while its draft set (draft record + starting instance) sits on disk as written, `deleted` when part of the set was deleted by hand, `missing` when the project has draft sets but none for this model, or `promoted` once an instance for the model is in the load path - derived locally from the drafts directory and the inventory; nothing is persisted or sent anywhere for it.

The step-by-step procedure - including the authoring-draft pathway and the equivalence-proof gate - is the [adoption playbook](adoption-playbook.md).

### The static equivalence stage

The equivalence proof that gates a conversion swap is a warehouse act - original and candidate built against identical source data, contents compared (the [adoption playbook](adoption-playbook.md) Step 6 procedure). [`loom prove`](commands.md#loom-prove) runs a static pre-stage over two practitioner-compiled manifests at fixed names in the converted project - `local/equivalence/original.manifest.json` (the original project, `dbt compile`) and `local/equivalence/candidate.manifest.json` (the converted project, `dbt compile`) - and reports per model one of two verdicts: **proven** (the warehouse comparison may be skipped for that model) or **inconclusive** (the stage abstains, with the reason named; the warehouse proof decides). There is no third verdict and no static disproof: the stage only ever proves or abstains. Either manifest absent means the stage was not run - a report, not an error - and Loom never shells to dbt to produce them. The command's JSON output is the proof record the playbook's recording step cites; before this stage existed, the proof's only record was the practitioner's commit message.

A model is proven only when every condition holds: the two manifests record the same adapter (cross-adapter proof is never attempted); the model exists in both as a model node; neither side is stateful (snapshots and incremental materialisations abstain by construction - a stateful model is never statically proven); both nodes carry compiled code with no unresolved Jinja; neither compiled text embeds a run-varying value (`current_timestamp`, `current_date`, `current_time`, `localtimestamp`, `localtime`, `now`, `random` as whole tokens) outside the declared differences; every original column has a candidate counterpart, and every candidate-only column is `__`-prefixed (the declared-difference rule - there are no declared mappings anywhere in the stage); and the compiled SQL is identical after normalisation. Candidate-only models (new generated machinery) are listed as non-subjects, never verdicted. The counterpart check sees the original's DOCUMENTED columns only: where a candidate column has no documented counterpart, the basis says so in those terms (the original's documentation may be incomplete) rather than blaming the candidate for an addition, and the report carries a documentation-coverage note naming the compared models whose original documentation is partial (no documented columns, or a verdict that turned on such a gap) - so the practitioner knows the stage's reach on a partially documented project before trusting its reasons.

The normalisation set is deliberately the smallest one that absorbs form, not semantics. Compiled SQL is compared as a token stream: SQL comments never tokenise; whitespace only separates tokens; unquoted words fold to lower case, while string literals and quoted identifiers are single verbatim tokens (case and content preserved); trailing statement semicolons are dropped; and the candidate's declared-difference items - final-select-list items whose output name is a candidate-only `__` column - are removed whole before comparison, which is what takes the generated `current_timestamp AS __loaded_at` item out before the run-varying scan can see it. Everything else is decisive, by design: expression shape or parenthesisation, aliasing (an added, removed or renamed alias), identifier quoting style, select-item order, DISTINCT placement and numeric literal formatting all abstain. Identical normalised text shares its data-dependence and coercion behaviour by construction, so those classes need no separate detector; anything the conditions cannot see (a run-varying value a macro compiled to a literal timestamp, for instance) differs textually and lands in the residual inconclusive class. Every mechanical failure of the stage itself - an unlocatable final select list while declared differences are present, an unnameable select item - degrades toward inconclusive, never toward proven.

## Diagnostics and recovery

| Symptom | Action |
| --- | --- |
| Unknown/retired metadata fields or directories | Update files to the current authoring forms in the [terminology](terminology.md); do not combine old and new authoring forms |
| A field occurs in defaults and params | Choose one location; Loom has no override precedence |
| Generation refuses to overwrite a file | The existing file at a planned output path lacks the Loom provenance header and differs from the generated content: it is treated as authored work. Move or rename it, or re-run with `--force` to replace it deliberately; nothing was written |
| A model fails to render during generation | Render failures are collected before the write phase; models that rendered successfully are still written and the command exits nonzero. Fix the failing instance's metadata and re-run |
| Interrupted generation | Inspect partial outputs, rerun generation, then dbt parse/build and check coverage; the output set is not transactional |
| Stale output after removing an instance or changing metadata | `loom generate --check` names files whose content no longer matches the metadata; regenerate to update them. Outputs of a *removed* instance are not detected - review and explicitly remove obsolete outputs; generation does not clean them up |
| Missing/stale manifest | Generate first, then run dbt parse/build with the correct profile/project before check or migration planning |
| Missing source relation or mapped physical column | Seed/load the intended data or correct metadata; inspection does not ingest or rewrite it |
| Inspection cannot import DuckDB | Install the optional dependency in the CLI environment, not only the adapter environment |
| Baseline already exists | Review before explicit removal/recapture; compare never updates it |
| An integration test cannot resolve/install a dependency offline | Populate the required uv cache deliberately before retrying; a missing wheel is not a passing/skipped compatibility result |

## Compatibility and evidence

Dialect support has exactly two states. **Executed & supported: DuckDB (in CI).** **Generation only - parse-checked, not executed: PostgreSQL, ClickHouse, Databricks, Snowflake, BigQuery, Redshift, Azure Synapse.** Development runs on PostgreSQL, ClickHouse and Databricks before this Alpha are recorded once, in the table below, as historical validation; they are not a support tier.

| Surface | Declared or bounded support | What the available local evidence means |
| --- | --- | --- |
| Loom Python | `>=3.11,<3.15` in package metadata | CI runs the default suite with the coverage floor on Linux across Python 3.11–3.14 on every push and pull request; the local quickstart is also verified on Windows with Python 3.14.5 |
| DuckDB Python module | Development constraint `>=1.2,<2`; optional in an installed CLI for inspection | DuckDB 1.5.6 in local source-structure and incremental-hub tests, not all releases |
| dbt-duckdb | Integration constraint `>=1.9,<2`; not a Loom base dependency | dbt-duckdb 1.11.0 in the CI quickstart job and local runs |
| dbt Core | Resolved with the adapter/lockfile; no independent Loom dependency range | dbt Core 1.12.5 in the CI and local proofs, not blanket support for every Core version |
| DuckDB dbt lifecycle | dbt Core 1.12.5, dbt-duckdb 1.11.0, DuckDB 1.5.6 | The full quickstart (every generator, six paradigms) seeds and builds in CI; runtime tests assert the link's non-empty, hub-consistent output, its fail-fast guard, the SCD2 lifecycle and the persistent-hub append across rebuilds, and the incremental lifecycles for link, bronze, normalized_table, fact, dimension, silver and gold - each checked against full-refresh equality, with restated-boundary, delete-blindness and stale-grain behaviours asserted as expected where they apply |
| PostgreSQL, ClickHouse, Databricks dbt lifecycles (development, pre-Alpha) | dbt-postgres / dbt-clickhouse (lockfile), PostgreSQL 16 / ClickHouse server; dbt Core 1.12.5, dbt-databricks 1.12.7, Serverless Starter Warehouse | Historical validation only - not a support tier. During development the full quickstart was built on PostgreSQL and ClickHouse (hub hashes byte-identical to DuckDB's; the PostgreSQL SCD2 lifecycle, including delete tombstones, passed; poisoned link input failed the build; ClickHouse snapshots did not propagate source deletes, so the SCD2 view's `is_deleted` stayed false there), and a Databricks run on synthetic data passed staging, SCD2, bronze, silver and gold plus the persistent-hub merge. None of these dialects is executed in this release |
| Other adapter/generator combinations | Not runtime-validated | Every generator's SQL is parse-checked under all eight dialects' grammars by the sqlglot test layer - a syntax guarantee, not adapter compatibility. Macro SQL remains consumer-owned. |

One conditional floor sits outside the table: projects that declare `unit_tests` in metadata need dbt Core >= 1.8 (the first release with unit tests). The floor binds only those projects, and Loom cannot enforce it through packaging (dbt is not a Loom dependency), so `generate` warns - without failing - when the project's existing manifest records an older `dbt_version`.

Manifest consumers gate on the manifest's declared schema: `read_manifest` rejects a `dbt_schema_version` outside v10–v12 (the schemas emitted by the dbt Core 1.6–1.12 range above) with a clear unsupported-version error instead of misreading it; a manifest with no declared schema version is still read. Manifests produced by dbt Fusion are not separately tested or claimed as supported - the same schema gate applies to them, and passing the gate is not a Fusion support claim.

Profile detection for `loom generate` follows dbt's own search order: the project directory, then `$DBT_PROFILES_DIR`, then `~/.dbt/profiles.yml`. When no profiles file exists anywhere, generation defaults to DuckDB and the CLI prints a warning naming the default, rather than defaulting silently.

The eight dialect definitions and renderer unit tests are not blanket support claims. The installed-consumer wheel test runs as a blocking CI job and passes there; locally it needs the offline uv wheel cache populated, and when a required wheel is absent it is blocked, not passed. The lockfile records selected versions, not proof they all ran in every environment.

## Stability

This is an Alpha release. The supported interfaces below are the surfaces practitioners and tools may build against, but they may still change between releases; changes are recorded in the changelog. Generated artefacts are plain dbt, owned by the consuming project once generated, so an interface change never strands a built project - regeneration is the migration path for generated files. For declared metadata, one commitment holds: the format carries no version, and an incompatible change ships only with a changelog call-out and an explicit migration path (see *Metadata format changes*).

## Supported interfaces

Loom's supported surfaces - the ones a practitioner or a tool may build against - are:

- **The `loom` command-line interface**: the commands, their long options, and their exit codes.
- **The JSON output** of the reporting commands (`--format json`): a machine-readable contract, extended additively. The default text output is written for humans and may change its wording in any release.
- **The metadata format** (the pattern, instance, and local-registration document shapes): the shapes carry no format version; incompatible changes follow the release-hygiene rule - a changelog call-out with an explicit migration path.
- **The generated artefacts**: the dbt model SQL and test YAML Loom writes are ordinary dbt inputs, owned by the consuming project once generated.

The Python modules under `src/loom` are **internal**. They are not a supported programming interface, carry no stability promise, and may change in any release - import them at your own risk. For the same reason Loom publishes no Python API documentation for now: the supported surfaces above are the whole contract, and the position is revisited if genuine extension points emerge. (The code-level surfaces a contributor extends inside this repository - the `Dialect` record, `GeneratorSpec`, `ParamContract`, and renderer base classes - are contribution surfaces, not a public API.)

## Contributor workflow

The wheel contains the Python CLI and YAML handling. `pyproject.toml` uses `uv_build`; base dependencies are YAML handling only. Dev/integration groups are repository tooling, not wheel extras. Runtime dbt can be installed in a separate environment with no Loom package.

From the repository root, with uv and a supported Python installed:

```sh
uv sync --locked --group integration
uv run --locked --no-sync python -m pytest -q
uv run --locked --no-sync python -m pytest tests/test_distribution.py tests/test_source_structure.py -q
uv run --locked --no-sync python -m pytest -m integration -q
uv run --locked --no-sync python -m pytest tests/test_installed_consumer.py -m integration -q
uv build --offline
```

Choose a focused test path first when changing one capability. For example, test pattern expansion with the first command below, or manifest freshness and generated drift with the second:

```sh
uv run --locked --no-sync python -m pytest tests/test_inventory.py -q
uv run --locked --no-sync python -m pytest tests/test_manifest.py tests/test_check.py -q
```

For generator wiring and focused renderer behavior, use the first command below; for source structure and consumer migration planning, use the second:

```sh
uv run --locked --no-sync python -m pytest tests/test_generator_registry.py tests/test_renderers.py -q
uv run --locked --no-sync python -m pytest tests/test_source_structure.py tests/test_migration_plan.py -q
```

The default pytest configuration excludes `integration`; the explicit marker selects the integration scenarios. The installed-consumer test is a targeted end-to-end proof that builds an sdist, rebuilds and installs a wheel, and runs the quickstart using a separate dbt environment. Run it directly when its optional dependencies are available.

Continuous integration (`.github/workflows/ci.yml`) runs on every push to `main` and every pull request: the default suite with the coverage floor across Python 3.11–3.14 plus a distribution build, the dbt runtime integration tests, and a quickstart end-to-end job (validate, generate, seed, build). The installed-consumer test runs as a **blocking** job.

**Offline prerequisite:** `uv build --offline` and the installed-consumer harness do not fetch missing dependencies. Populate uv's cache for locked dependencies and build requirements, including wheels needed in isolated CLI and dbt environments. Missing cached artifacts block that proof; they are not a passing or skipped result.

The installed-consumer harness builds an sdist, rebuilds a wheel from it and uses separate installed CLI/dbt environments with lockfile-constrained dependencies. Source-tree imports alone do not prove distribution contents. Generated SQL runtime checks in the default suite (`tests/test_generated_sql_execution.py`) use a bounded Jinja/DuckDB materialization harness covering hub, link, bronze, silver, gold, view, OBT, mart, fact, dimension and the SCD2 view output; they are not equivalent to a full dbt adapter lifecycle. A parse layer in the same suite (`tests/test_generated_sql_parse.py`) renders every generator for all eight dialects and requires the compiled SQL to parse under the target dialect's grammar (sqlglot), with no unmodelled function calls beyond a reviewed exception list. Parse success is a syntax guarantee only - not execution evidence and not a dialect support claim. Snapshot history, adapter behavior and the quickstart use real installed-consumer dbt commands. After initial dependency sync, `--no-sync` keeps test execution from mutating a shared environment. Run another sync when dependencies change, not concurrently with tests.

### Code map

Project-file parsing is independent of inventory semantics. Inventory and manifest processing share that boundary without an import cycle.

| Module/area | Responsibility |
| --- | --- |
| `src/loom/cli.py` | Command parsing, result presentation, shared error reporting and generate command |
| `src/loom/inventory.py` | Inventory facade: entry types, project loading, search and coverage (validation lives in the three modules below) |
| `src/loom/inventory_records.py` | Record-level validation of one metadata document at a time |
| `src/loom/inventory_patterns.py` | Pattern definition loading and instance expansion; native source/macro reading |
| `src/loom/inventory_relationships.py` | Cross-record relationship validation between inventory entries |
| `src/loom/generator_registry.py` | Private generator metadata: 13 `GeneratorSpec` registrations, output roots and paradigm mapping |
| `src/loom/dialects.py` | Eight SQL dialect definitions and dbt profile/target detection |
| `src/loom/renderers/` | `BaseRenderer` and the 13 generator renderers; `get_renderer` selects implementations |
| `src/loom/errors.py` | Shared `LoomError` boundary |
| `src/loom/project_files.py` | Shared project configuration, duplicate-key-safe YAML reading and relevant traversal |
| `src/loom/check.py` | Read-only manifest-backed inventory coverage |
| `src/loom/manifest.py` | dbt manifest identity, freshness and project-model checks |
| `src/loom/source_structure.py` | Local inspection, create-only baseline capture, baseline validation and pure structural comparison |
| `src/loom/migration_plan.py` | Named manifest resources and breadth-first dependent traversal |
| `tests/` | Capability-focused Python regression coverage, optional adapter lifecycle tests, and installed-consumer proof |
| `examples/quickstart/` | Self-contained authored dbt example used directly by installed walkthrough proof |

The generator registry is a small private metadata table, not a plugin API. Renderer dispatch is explicit in `get_renderer`. Generator-specific keys, relationships, SQL and history remain explicit. Generator wiring is checked, not assumed: project loading fails when the registry and the parameter contracts name different generators, record validation reports a registered generator that has no contract, and the CLI refuses every command when a registered generator has no renderer - an incompletely wired build fails explicitly instead of validating or generating with checks missing. Validation uses lightweight Python checks - per-record and cross-record - source, macro, hub, link, SCD2 and Kimball relationships have named local helpers. Invalid inputs still produce aggregated diagnostics. Source comparison validates the baseline before inspecting the database, then compares validated structures. Shared entries and project-file helpers are not a persistent cache or a claim of measured speedup.

Changes should preserve validated input behavior, generated resource identities and SQL/data semantics unless a separately approved behavior change requires otherwise. Generation has no ownership preflight or rollback. Add focused Python tests, generated-SQL checks or real dbt lifecycle tests at the appropriate boundary. Sanitise issue examples; report commands actually executed rather than implied support.

## Deliberate limits

Not implemented: automatic warehouse introspection during generation, automatic mapping changes, arbitrary expression/join metadata, generated-model override files or pre/post-hook fields, durable audited links, hierarchical links (a hub linked to itself - rejected in validation, ADR 0030), detached SCD2 MERGE, broader Kimball generators, cross-pattern conversion, Semantic Layer metric generation, plugins or hosted CI enforcement. AutomateDV is not required. dbt Cloud and other adapter/platform combinations are not execution-supported; generation for them is parse-checked only.

**Deliberate non-goals.** The decisions not to build - anchor modelling, event-sourced outputs, snowflaked dimensions, a `loom init`-style scaffold command, and a raw-SQL escape-hatch generator or Loom-claimed historising conversion - are deliberate non-goals. Event-sourced *inputs* remain supported: land events through bronze/staging and model projections with any Loom paradigm.

Licensing, publication rights/destinations and any public support arrangement remain owner decisions. This document supplies no license or publishing/support promise.

**Decisions.** The design decisions behind this document are recorded as [ADRs](adr/) (see the [ADR index](adr/README.md)); this document states current behaviour only.
