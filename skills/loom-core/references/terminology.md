# Loom terminology

Loom authors ordinary dbt resource files. dbt parses and executes those files; Loom is not part of the dbt runtime. Use the terms below consistently in documentation, metadata, commands and code.

## The practitioner vocabulary

| Term | What it describes | What it does not describe |
| --- | --- | --- |
| **Generator** | Loom's built-in implementation and rules: `staging`, `hub`, `link`, `satellite`, `scd2_dimension`, `fact`, `dimension`, `bronze`, `silver`, `gold`, `obt`, `normalized_table`, `view`, `mart`, `pit`, `bridge`, `reference_hub`, or `reference_table` | A named pattern, dbt resource type, materialization or plugin API |
| **Pattern** | A named reusable configuration of one generator, declared in a pattern definition | Executable SQL, a JSON Schema or another generator |
| **Pattern instance** | One named application of a pattern with concrete parameters | A running object, deployment or necessarily one dbt resource |
| **Instance set** | One authoring document containing one or more instances of the same pattern | A dbt group, ownership boundary, execution batch or inheritance layer |
| **Model registration** | Descriptive metadata for a hand-written dbt model | A generated instance or an instruction to rewrite the model |
| **Model inventory** | Loom's searchable collection of expanded instances and model registrations | dbt Catalog, `catalog.json`, `manifest.json` or a warehouse catalog |
| **Migration** | The programme of adopting Loom in an existing dbt project (the [playbook](adoption-playbook.md) pathway: assess, convert first, register the rest, promote, prove) | Any single operation in that programme - the operations are register and convert (with `plan` for the consumer view), and `migration` itself is never a command name |
| **Register** (verb) | The migration operation that adds descriptive metadata for a hand-written model (`loom register --write` writes registration drafts); the model stays hand-written. Registration also withdraws the model from the convert pool - convert drafts only models unknown to Loom, unless `--include-registered` re-opens one | Generating the model, or a promise to convert it later |
| **Authoring draft** | A starting instance `loom convert` drafts for a hand-written model in a dbt layer (staging / intermediate / mart) that no generator fit matched: the layer, the pre-wired input set and the declared interface are copied from the manifest, a target generator is named by layer as the authoring destination, and the transform is left as the single named authoring hole. Report sections call these drafts scaffolds. (The retired command `loom scaffold` is a different thing - it is now `loom create-new`, an authoring-snippet printer.) | A fit, a fit proposal, or a claim that the model's shape matches the target generator - an authoring draft is completed by authoring, not by accepting a match |
| **Convert** (verb) | The migration operation that replaces a hand-written model with a generated instance, drafted by `loom convert` from a generator fit and gated on the playbook's equivalence proof | Registering it, or editing its SQL in place |
| **Fit proposal** | The assessment's heuristic judgement that a model's output shape matches one generator, with recorded evidence and drafted parameters (the mechanism that produces a conversion draft) | A conversion itself, or a claim about the model's semantics |
| **Draft** | The one artefact `loom convert --write` writes per assessed model under `local/drafts/`: a draft record (basis, evidence, declared interface, readiness account, promotion steps), a final-form instance document, and any synthesised pattern. Basis is `fit` or `authoring`; promotion is a move of the set into `metadata/`, never an edit | A registration, a generated file, or inventory - Loom never loads the drafts directory as inventory |
| **Readiness** | A draft's derived account of its distance from promotion: `ready_to_promote` (no holes, no blockers), `needs_authoring` (named holes - authoring holes the practitioner supplies, evidence holes Loom could not verify), or `not_draftable` (a named blocker no generator can express). Recomputed every assessment run; never stored in the instance | A stored status, a fit judgement, or a property of the model rather than of its draft |
| **Carriage** | Loom hosting a practitioner declaration it does not originate - generic (data) tests in an instance's `generic_tests`, generated into the model's properties document - because dbt's single-description rule leaves the declaration nowhere else to live | A Loom-generated test, or validation of the test's name or arguments - dbt resolves tests from installed packages at build |
| **JSON Schema** | Machine-readable validation of a Loom document or expanded generator input | A pattern, warehouse namespace or dbt model contract |
| **Lightweight validation** | Python-based semantic checks: envelope, required fields, cross-field constraints | JSON Schema, warehouse schemas or dbt contracts |
| **Warehouse schema** | A database namespace, including actual `schema` configuration values | The format of Loom YAML or JSON |
| **Generator guarantee** | Behavior enforced through generated SQL guards and/or generated dbt data tests | A guarantee about model *shape* - shape is the enforced-contract feature's domain (opt-in only, ADR 0032) |
| **Provenance (row-level)** | The `__record_source` / `__loaded_at` columns a generated model stamps on its rows, with per-generator semantics (see architecture's Provenance section) | A run/batch identifier (none is emitted), or metadata provenance (which instance/pattern produced a file) |
| **Paradigm** | A scoping and grouping concept for mixed-paradigm projects (e.g. `dv`, `kimball`, `medallion`, `staging`, `obt`, `hybrid`); inherited from the generator registry by default | A generator implementation, naming convention, or dbt materialization |
| **Dialect** | A database/warehouse technology (e.g. `duckdb`, `snowflake`, `bigquery`); determines SQL syntax for hash functions, string types, and incremental strategies | A dbt adapter name, project configuration, or rendering strategy |
| **Source-structure baseline** | An explicit local record of observed source columns, types and physical identity | A dbt snapshot, database backup or dbt catalog artifact |
| **Source declaration** | The dbt YAML - conventionally an authored `sources.yml` under the model paths - that names a raw warehouse table as a dbt source. The practitioner writes it; Loom reads it and never writes it | A Loom generation output, or the warehouse table itself |
| **Staging model** | A generated model that projects exactly one source table - explicit column renames and casts only, no joins or business logic. It is the adapter between raw sources and everything downstream: other generators consume models, not sources, so a source shape change is absorbed by exactly one staging model | A cleansing or conformance layer (that is medallion silver's job), or a place for joins and filters |
| **`_generated/staging/`** | The Loom-owned directory under the model root that holds the generated staging models (and `sources.yml`, when emitted). It is named for what it contains: staging models | The authored `sources.yml`, or a directory of declarations for authored sources |
| **Inline source** | A source declared inside Loom metadata (`inline_source`) instead of in an authored declaration. `loom generate` emits its dbt declaration into the Loom-owned `_generated/staging/sources.yml` - and only when no authored declaration for the same (source, table) exists; an authored declaration always governs | An authored dbt source, or a promise that `sources.yml` always exists |

For example, the **staging generator** implements column projection; a **standard_staging pattern** supplies reusable defaults; the **stg_customers instance** supplies a source reference and columns. Loom generates `models/_generated/staging/stg_customers.sql`; the project authors the dbt source declaration. dbt executes the SQL.

```text
Generator
  -> named pattern definition (generator + defaults + required parameters + paradigm)
  -> pattern instances in an instance set (pattern reference + parameters)
  -> paradigm resolved from generator registry
  -> lightweight Python validation
  -> generated dbt resource files
  -> dbt parsing/execution
  -> warehouse relations and data

Expanded instances + hand-written model registrations
  -> Loom model inventory (searchable by paradigm)
```

An instance can produce several files and resources. SCD2 generates a model and a dbt snapshot. A registration adds discovery and coverage information without generating the model.

## Keep the dbt boundary explicit

Use **dbt model**, **source**, **snapshot**, **seed**, **macro**, **materialization**, **group**, **model contract**, **data test** and **unit test** for those actual dbt concepts. Use **Python tests** for Loom's framework tests. A Loom instance set does not assign dbt groups or model access. Generator guarantees and generated tests do not imply an enforced dbt model contract; a generated model carries one only when its metadata opts in with `contract_enforced` (ADR 0032).

The `input_models` parameter lists the upstream dbt models used through `ref()` (exactly one entry for the single-input generators). Staging's `source_ref` references an authored dbt source used through `source()`. `inline_source` declares a source inline in the metadata instead; generation emits its dbt source declaration into `_generated/staging/sources.yml` when no authored declaration exists for the same source and table (an authored declaration governs). A registration's optional `upstream` field is descriptive text, not an executable source declaration.

Retain standard dbt keys and meanings: `schema`, `database`, `resource_type`, `models`, `sources`, `snapshots`, `data_tests` and `unit_tests`. Qualify ambiguous prose as **warehouse schema**, **dbt manifest** or **source structure** rather than calling every shape a "schema".

## Authoring documents and validation

An authoring document's kind is its directory, never a field it carries - documents state no type, and each directory's role fixes the shape its documents must have:

| Document kind | Location | Purpose |
| --- | --- | --- |
| Pattern definition | `metadata/patterns/<name>.yml` | Select a `generator` and declare defaults/required parameters |
| Instance set | `metadata/instances/*.yml` | Reference one pattern and supply named `instances` with `params` |
| Model registration | `local/registrations/*.yml` | Describe a hand-written model and its `classification` |

Loom documents carry no format version: no `format_version` field identifies the metadata document format, and the loaders do not look for one. Practitioners pin the Loom release they use; an incompatible format change in a future release is called out in the changelog with an explicit migration path (the release-hygiene rule).

Validation uses lightweight Python checks - envelope, required fields, and cross-field constraints - rather than JSON Schema. A named pattern reuses those rules; it does not require its own schema file.

A registration's **classification** is its descriptive category, not a generator implementation. Existing restrictions on classifications reserved for generated behavior still apply. Search generated instances with the `generator:` qualifier; search registrations with the `classification:` qualifier. The two qualifiers can be combined with the other selector qualifiers under the selector grammar's set rules.

For developers, the **generator registry** is the small private dispatch table. It is not another practitioner-authored resource, plugin system or promise that arbitrary classifications can generate SQL.

## References

- [dbt catalog artifact](https://docs.getdbt.com/reference/artifacts/catalog-json)
- [dbt manifest artifact](https://docs.getdbt.com/reference/artifacts/manifest-json)
- [Warehouse schemas](https://docs.getdbt.com/docs/build/custom-schemas)
- [dbt groups](https://docs.getdbt.com/docs/build/groups)
- [dbt model contracts](https://docs.getdbt.com/docs/mesh/govern/model-contracts)
- [dbt source properties](https://docs.getdbt.com/reference/source-properties)
- [dbt materializations](https://docs.getdbt.com/docs/build/materializations)
