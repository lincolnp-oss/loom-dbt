# Loom command reference

`loom --help` groups the commands by task family - **author** (validate, generate, create-new, search), **adopt** (register, convert, plan), **verify** (check, coverage, prove), **sources** (inspect-sources, snapshot-sources, diff-sources). The operational lookup for every `loom` command: what it is for, its parameters, what it writes, its output and exit codes, and an example. For behaviour and design detail, see the [architecture document](architecture.md); for the metadata formats and every authorable option, see the [metadata reference](metadata-reference.md); for migrating an existing project, see the [adoption playbook](adoption-playbook.md).

## Conventions that apply to every command

- Run Loom from the consuming dbt project's directory, or pass `--project-dir <project>`. Both relative and absolute project directories work.
- Manifest paths are relative to the project directory unless absolute; the default is `target/manifest.json`. Manifest-backed commands require a **current** manifest: produce or refresh it with `dbt parse`. The migration assessment (`register`/`convert`) declares which kind of manifest it consumed (see *Manifest provenance* under `loom register`) and its fit calibration assumes a parse-produced one. Commands that inspect built artefacts (for example `inspect-sources`, which reads a built DuckDB file) additionally need those artefacts built - `dbt seed` first if the project uses seeds, then `dbt build` - but the manifest they read is the same parse-refreshed one.
- Every command accepts `--format` and `--verbose`. Report commands (`search`, `coverage`, `plan`, `register`, `convert`, `inspect-sources`, `diff-sources`) print summary-first **text** by default; `--format json` emits the complete structured payload - the form scripts and AI agents should request explicitly - and `--verbose` adds low-level detail (unique IDs, dependency paths, exhaustive lists) to the text rendering. The remaining commands emit the same-shaped payload under `--format json` (for `create-new`, the snippet rides a `snippet` field; for `validate` and `check`, the verdict and counts).
- **Mutating commands preview by default.** `generate`, `register`, `convert` and `snapshot-sources` write nothing unless write mode is on: the `--write` flag, or the `LOOM_WRITE` environment variable (set to anything but `0`, `false` or `no`, case-insensitive) when no flag is given. There is no negative flag - unset the variable to opt back out. Every mutating run states its mode in its headline: `writing`, `writing (LOOM_WRITE)`, or `dry run - nothing written`. A preview reports the same counts everywhere - *would write N, would change M, unchanged K* - and ends with the exact command to re-run with `--write`. (`generate --check` is separate: the CI drift mode, which never writes and takes precedence over `--write`.)
- Colour follows one rule everywhere, help included: ANSI styling appears only when stdout is a terminal and `NO_COLOR` is unset; piped output and `--format json` are never coloured.
- Exit codes: `0` means success (or, for checks, "no problem found"); `1` means a validation error, a failed check, drift, or an incomplete generation. Reports go to stdout; errors and write-notices go to stderr.
- `loom --help` lists the commands; `loom <command> --help` shows a command's options; `loom --version` prints the version.
- Shell completion is not shipped in the Alpha: Loom provides no completion scripts, and selector qualifier values are not completed from the live project. The deferral is recorded here, in the shipped reference, rather than left silent.

---

## Selector grammar

Search and every model-scoped command (`generate`, `convert`, `register`, `check`, `coverage`, `prove`, `search`, `validate`) share one selector grammar: the same strings are the query on `loom search` and the value of `--select` / `--exclude` on the action commands, evaluated by the one engine over the inventory records (Loom metadata and the dbt manifest as one corpus). The set algebra mirrors dbt's selector semantics exactly, so dbt muscle memory carries over: within one selector argument, comma-separated criteria **intersect**; separate arguments **union** (space-separated arguments at the search top level, repeated `--select` flags); `--exclude` takes its own selector arguments and subtracts what they match. There are no `OR` / `AND` keywords and no parenthesised expressions. An intersected query therefore reads `loom search "customer,generator:hub,tag:finance"`.

A criterion is a bare term, a `key:value` qualifier, or a graph form. Bare terms name models: an exact name, a name pattern with `*` (`hub_*`), a bare package name (which selects that package's records, as in dbt), or a dotted path tail in the dbt fqn shape (`marts.finance` selects the model `finance` under a `marts` path segment). Qualifier values match **exactly** (case-insensitively) unless they carry a `*` glob - they are drawn from controlled vocabularies and project metadata - while bare terms are the forgiving form: in search they are ranked substring matches (exact name first, then name prefix, then name substring, then field matches over owner, tag, column and key, then documentation text, with Loom-known status as the final tiebreak; ranking never hides a match). This split - exact for qualifiers, forgiving for bare terms - is deliberate and is the point practitioners are most likely to mis-predict. `config.` matching is refused with the reason stated: Loom's inventory records carry no dbt config fields, so there is nothing truthful for it to match.

| Qualifier | Matches records where |
| --- | --- |
| `generator:` | the model's generator is the value (generated models) |
| `pattern:` | the model's pattern definition is the value |
| `paradigm:` | the model's paradigm is the value |
| `classification:` | the registration's classification is the value |
| `owner:` | the owner is the value |
| `tag:` | the value is among the model's tags |
| `path:` | the model's file path contains the value (a glob when it carries `*`) |
| `package:` | the record's package is the value |
| `column:` | the value is one of the model's output columns |
| `key:` | the value is one of the model's declared grain keys |
| `input:` | the value is one of the model's declared inputs - a model input by name, or a source input by full identity (`raw.customers`) or by source name alone (`raw`) |
| `doc:` | the value appears (substring) in the model's description or any column description |
| `kind:` | the record's kind is the value: `model`, `macro`, `source`, `seed`, `snapshot`, `test`, `exposure` |
| `is:` | the value is the model's status (`generated`, `registered`, `unmanaged`, `orphaned`) or the record's provenance (`project`, `package`) |
| `has:` | the record has the named property: `owner`, `no-owner`, `tests`, `no-tests`, `description`, `no-description`, `undocumented-columns`; the owner and tests forms are model hygiene facts and match model records only |
| `selector:` | the value names a dbt-style named selector defined in `selectors.yml` in the project root (see below) |

Graph questions have two surface forms over one traversal of the record graph. The operator form wraps a name (or a qualifier): `+name` adds the full upstream closure, `name+` the full downstream closure, `+name+` both, `@name` the downstream closure plus the parents of its members, and a number bounds the depth (`2+name`, `name+3`, `+name+2`); the anchor itself is always included. The word form reads `upstream:hub_customer` or `downstream:hub_customer` (bare form: full transitive closure), with an optional `depth:` companion **in the same selector** bounding it: `"downstream:hub_customer,depth:2"`. For macros, downstream means impact: the models that call the macro (its used-by list) and everything downstream of them.

Search integrates shape banding: when a search query carries two or more **shape qualifiers** (`key:`, `column:`, `input:`, `generator:`, `paradigm:`), results band by shape overlap - full matches first, then close matches (records satisfying some but not all of the shape qualifiers) ranked by a weighted fingerprint comparison over the shape the query describes - and the output says so in a header line with the full-match and close-match counts distinguished. The fingerprint has four components with fixed weights: **keys 5, inputs 4, output columns 3, generator family 1** (the family is the generator's paradigm); each component scores as coverage of the described shape, and a candidate sharing none of the described keys ranks below every candidate that shares the grain, whatever its other overlap. Each close match carries its score and a shares / differs account naming the actual keys, inputs and columns. `loom search --similar-to <model>` is the same comparison anchored on a real model instead of a described shape: the named model's fingerprint is the target, every other model is ranked against it with extra elements reported as differences, the named model itself is excluded and shown as the reference header, and components that cannot be derived for it (a partial shape) are named in the header and excluded from scoring rather than scored as zero. Similarity language is always "close matches" and "shares / differs" - it never asserts duplication. `--select` never bands: the identical string on an action command matches only the full matches, so an action's pool is never widened by overlap. Single-shape-qualifier queries and non-shape queries are strict everywhere.

Errors teach. An unknown qualifier is an error listing the valid qualifiers with an example. A `--select` selector that matches nothing in the command's pool is an error naming it (deliberately stricter than dbt; kinds outside a command's pool match nothing in it, so macros are never selectable into a model pool). A zero-result search is not an error: it names which criterion emptied the set, suggests dropping it, lists the facet values that do exist for the qualifiers used, and offers closest names ("did you mean") for a bare term that looks like a typo.

Named selectors follow dbt: a `selectors.yml` in the project root defines names under a `selectors:` list, each with a `name` and a `definition` - a selector string, a list of definitions (union), or a mapping with `union`, `intersection`, or `method` (the `tag`, `path`, `package_name`, `resource_type` and `fqn` methods, with `parents` / `children` and their depth keys as graph flags), plus an optional `exclude` list subtracted from the definition. Reference one anywhere a selector is accepted as `selector:<name>`. Loom defines no saved-selection format of its own.

## `loom validate`

**Purpose.** Validate a project's Loom metadata: pattern definitions, instance sets (expanded and checked against the generator contracts), and model registrations, including cross-model relationships. Run it after any metadata edit - it is the gate every other workflow builds on.

**Prerequisites.** None beyond the project itself; no manifest, no database.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project to validate |
| `--select` | all | Validate only the selected models' authoring documents (see scoped validation below); repeatable (see [Selector grammar](#selector-grammar)) |
| `--exclude` | - | Subtract models matching the selector from the validated set |
| `--format` | `text` | `text` or `json` (a verdict payload: the entry count and `ok`) |
| `--verbose` | off | List each validated entry (name and metadata path) before the count |

**Writes.** Nothing. **Output.** A one-line count of the validated entries and the authoring documents carrying them; validation failures print to stderr as one grouped report: a count headline, then problems grouped by file and entry with each root cause stated once (the missing-parameters line replaces the per-property cascade it causes). When no inventory entries are found at all, a warning is printed on stderr (`loom: warning: no Loom metadata found under <project-dir>`) and the exit code stays `0` - an empty project is a legitimate intermediate state, but it should not look like a pass. A metadata file that fails to load (for example malformed YAML) stops validation at that point: the load errors are the whole report, rather than a cascade of downstream "not in the inventory" errors against the partial inventory. Exit `0`/`1`.

**Scoped validation.** With `--select` / `--exclude`, validation narrows to the selected models' authoring documents: the instance documents containing a selected generated model, the registration documents containing a selected registered model, and the pattern documents defining the patterns those instances use. Inclusion is at document granularity - a document that contains a selected model is validated whole, including its other models (the report states how many documents were included and how many models they contain beyond the selection), because a document's validation includes cross-record relationship checks inside it. Relationships from included records to models in documents that are not included are checked for resolvability against the inventory (the referenced model exists and is shape-compatible at the reference level) but are not deep-validated, and coverage-style cross-checks against the whole project are not part of scoped validation (they remain `check` and `coverage`'s job, themselves selectable). A selected unmanaged model has no authoring documents: it contributes nothing to the included set, is named in the report as having none, and is not an error. If the included set is empty, scoped validation reports that plainly and succeeds, listing the selected models it applies to. In text output, the report's model lists (the selected models, the models beyond the selection, and the selected models without documents) print inline while they fit ten names and fold past ten to the shared fold form - the first ten, the folded count, and the way to unfold (`--verbose`, or `--format json`). The JSON payload carries the scoping account under `selection` (`selected`, `documents`, `extra_models`, `without_documents`) with `scoped: true`, always with the full lists.

```sh
loom validate --project-dir .
loom validate --select hub_customer
```

**Common failures.** Unknown generator parameter (the error names the accepted set); an explicitly null parameter (null is not a supplied value - omit the key instead); a referenced input model missing from the inventory.

Behaviour: [Authoring and validation](architecture.md#authoring-and-validation)
## `loom create-new`

**Purpose.** Print an authoring snippet for a new artefact on a model - the kind is a positional argument (`unit-test` or `data-test`). The form follows the model's state. For a **generated model**, the snippet is a `unit_tests:` block to paste into the model's instance params: one stub test with a `given` entry for every declared upstream, its rows keyed by the upstream's derivable output columns and filled with the sentinel value `"__TODO__"`, plus an `expect` row keyed by the model's deterministic output columns (the run-time `__loaded_at` stamp omitted). Validation rejects any fixture still containing `"__TODO__"`, so a pasted-and-forgotten snippet can never generate a passing-but-meaningless test. For a **hand-written model** - in the dbt manifest but not generated inventory - the snippet is the dbt-YAML `unit_tests:` document dbt itself reads (with `model:` named): `given` inputs derive from the manifest's `depends_on` (models as `ref('…')`, sources as `source('…', '…')`), rows keyed by each input's documented columns, and `expect` rows keyed by the model's documented columns. Where columns cannot be derived or are not documented, either form emits empty rows with a comment saying so rather than inventing columns.

The `data-test` kind declares **one** generic test: `--test` names it (required - Loom never invents the test; a bare name or `package.name`), `--column` attaches it to a column (omit for a model-level test). For a generated model the snippet is a `generic_tests:` carriage declaration for the instance params; for a hand-written model it is the dbt-YAML `models:` fragment with the test under the column's (or the model's) `tests:` list. `unique` and `not_null` - the no-argument built-ins - print bare; any other test carries the `kwargs: {__TODO__: …}` sentinel where its arguments go, because Loom does not presume to know a test's signature (dbt's `accepted_values` needs `values`, `relationships` needs `to`/`field`, package tests are unknowable) - and the carriage validator rejects the sentinel, so the pasted snippet cannot validate unfinished. A named column is checked before printing: against the model's derivable output columns (generated) or its documented columns in the manifest (hand-written) - a column that is derivably absent is refused, naming the columns that exist; where columns cannot be derived at all, the snippet prints with a comment saying the column could not be verified. On a model declaring `skip_properties_document`, the command warns that the metadata snippet would conflict with the opt-out (carriage needs the properties document) and prints it anyway.

**Prerequisites.** A project whose metadata validates (the command validates first, like `generate`). The hand-written form additionally reads the dbt manifest (the freshness guard every manifest reader applies).

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project to read |
| `--manifest` | `<project-dir>/target/manifest.json` | dbt manifest path; read for hand-written models |
| `--test` | - | `data-test` only (required there): the generic test to declare |
| `--column` | - | `data-test` only: the column the test attaches to; omit for a model-level test |
| `--format` | `text` | `text` (the raw snippet) or `json` (a payload carrying the snippet in its `snippet` field) |
| `--verbose` | off | Accepted for parity; the snippet has no further detail to add |

The positional arguments are the snippet kind (`unit-test` or `data-test`) and the model name.

**Writes.** Nothing - stdout only; the command is print-only in every mode. **Output.** The snippet text. Exit `0`, or `1` when the kind is unknown, `--test` is missing or malformed for `data-test` (or supplied for `unit-test`), the named column is derivably absent, the model is in neither the inventory nor the manifest, or its generator does not support unit tests in v1 (`scd2_dimension`).

```sh
loom create-new unit-test dim_customers --project-dir .
loom create-new data-test stg_customers --test not_null --column customer_id
```

Behaviour: [Generated dbt unit tests](architecture.md#generated-dbt-unit-tests)

## `loom search`

**Purpose.** Find what already exists before writing new ones: search the inventory - Loom metadata and the dbt manifest as one corpus - with selector arguments in the shared [selector grammar](#selector-grammar), or by example with `--similar-to <model>`. With no query, print a project overview instead of a result list.

**Prerequisites.** None beyond the project itself: the corpus is metadata plus `target/manifest.json` when one exists (its generation time is labelled in the output), or metadata only when none does, and the output says which. Search never refuses to answer over a stale manifest; it labels the manifest's age so the practitioner can judge. In metadata-only mode only Loom-known objects appear, and unmanaged and orphaned are unknowable - run `dbt parse` to widen the corpus to every model in the project.

| Option | Default | Effect |
| --- | --- | --- |
| `SELECTOR` (positional) | - | Selector arguments to search by; comma-separated criteria in one argument intersect, separate arguments union. With none, the overview prints |
| `--project-dir` | current directory | The dbt project |
| `--select` | - | Additional selector arguments, equivalent to supplying them as the query (the flag form scripts can use on every model-scoped command); repeatable |
| `--exclude` | - | Subtract records matching the selector from the results |
| `--similar-to` | - | Search by example: rank every other model by shape overlap with the named model's fingerprint (keys, inputs, output columns, generator family; weights and key gate as in the selector grammar); cannot be combined with selector arguments, `--select`, `--exclude`, `--count-only` or `--names-only` |
| `--fields` | all fields | JSON output only: comma-separated record fields to include in each result instead of the full record (for example `--fields name,status,owner`); an unknown field is an error listing the available fields |
| `--count-only` | off | Print only the match count; under shape banding the text form prints the full and close counts distinguished, with the headline's labels, instead of one merged number; with `--format json`, an object carrying `count`, `full` and `close` |
| `--names-only` | off | Print one matching name per line, clean for piping to other tools; with `--format json`, an object carrying the `names` list |
| `--format` | `text` | `text` or `json` (the corpus mode and project, the facet counts, the match records with their matched criteria and `band` (`full` / `close`, or `similar` under `--similar-to`, whose payload also carries the `similar_to` reference fingerprint), close matches when banded, and any zero-result diagnosis; banded and similar results carry a `similarity` object with `score`, `gate`, `shares` and `differs`; with no query, the overview object) |
| `--verbose` | off | Add per-result detail (path and description; macro arguments and used-by counts), unfold capped tier-2 groups, and show a detail view's metadata path |

**Writes.** Nothing. **Output.** With no query, the **overview**: project name and corpus mode (with the manifest's generation time and age, or the metadata-only label), the total object count, model counts by status, counts by generator and paradigm, top owners plus the unowned count, documentation coverage (models with a description, and output columns documented), macro counts split project and package, and suggested queries drawn from the project's actual state - the exception queries (`is:unmanaged`, `has:no-owner`, and kin) that currently return non-empty sets. For a query, text output is a match-count headline naming the corpus mode, the query, a facet line (counts over the returned set by status, kind, generator, owner, paradigm and provenance, recalculated per result set), and the model table - name, status, generator (or classification), owner, and what the row matched, with status as the colour carrier when colour is on. Tier-2 matches (macros, sources, seeds, snapshots, tests, exposures) appear in grouped sections beneath the models, grouped by kind then by provenance - project first, then each package labelled by name, presented exactly like project matches - and a group longer than ten rows is capped with its full count and a narrowing query (for example `kind:macro,<term>`). A `kind:` qualifier promotes that kind to the primary table with kind-appropriate columns: every tier-2 kind shows name, provenance and package plus the fact that kind is read by - for macros the arguments summary and used-by count, for sources and seeds the downstream count, for snapshots the model snapshotted, for tests the model tested, for exposures the upstream count. The promoted table folds past ten rows under the same capping rule as the grouped sections - the first ten rows, the folded count and a narrowing query - and `--verbose` unfolds it. A bare name that names exactly one model in the result set also opens that model's detail view, even when other records match (a model's tests, whose names embed the model's name, above all), with the remaining matches listed compactly beneath it. When a query resolves to exactly one object, output switches to its **detail view**: for a model, status, generator and pattern, paradigm and classification, owner, tags and description, grain keys, declared inputs, output columns in order (marked partial where the record is partial) with each column's type and description where truthful ones exist, upstream and downstream counts with nearest neighbours, test presence, and the file location (plus the expected generated output path for generated models); for a macro, its signature with arguments, provenance and package, origin path, description, used-by list, and a slot-compatibility note derived from the signature alone: the staging column mapping is the one generator slot a macro can fill, and the note states whether this macro can fill it - which parameter the mapped column fills at position 1 by default (`macro_column_param` can place it at another position), and how many `macro_args` a mapping must supply alongside the column to reach the last argument without a default, or that the macro takes no arguments and so cannot fill the slot at all. The note states facts about the slot, never a judgement about the macro. When shape banding is active (two or more shape qualifiers in the query), the headline distinguishes full from close matches, a header line states that close matches are shown by shape overlap and that `--select` on an action command would take only the full matches, and close matches follow in their own section, each with the shape qualifiers it shares and the ones it differs on, its weighted shape-overlap score, and a shares / differs account over the described shape's keys, inputs, columns and generator family. With `--similar-to`, the headline names the reference model and the close-match count instead, a reference-shape line states the fingerprint the ranking compares against (naming any components that could not be derived - those are excluded from scoring rather than scored as zero), and every result renders in the ranked table with its score and shares / differs account, extra elements included. The output closes by stating how many models shared no shape overlap with the reference and were omitted from the ranking; the JSON payload carries that count as `omitted_no_overlap` under `similar_to`. A zero-result search names the criterion that emptied the set, lists the facet values that do exist for the qualifiers used, and suggests closest names for a mistyped bare term. In metadata-only mode, a zero-result `is:unmanaged` search also restates that unmanaged is unknowable without a manifest, so the empty set is not read as a finding about the project. At an interactive terminal, the model table, the promoted tables, the facet line and the overview's census lines adapt to the terminal's width - wrapping at token and entry boundaries, and truncating a single over-long token with an ellipsis marker rather than splitting it - while piped output and JSON are unchanged. Exit `0` (no matches is not an error).

```sh
loom search
loom search "key:customer_id,column:email"
loom search --similar-to sat_customer_details
loom search downstream:hub_customer
loom search has:no-owner
loom search kind:macro
loom search hub_customer
loom search --select is:unmanaged --names-only
loom search is:orphaned --count-only
```

Behaviour: [Finding existing models](architecture.md#finding-existing-models)
## `loom generate`

**Purpose.** Render the project's generated instances to dbt model SQL, data-test YAML, SCD2 snapshots, and the inline-source declarations document. Everything is rendered before anything is written, and an existing non-Loom file at a planned output path blocks the whole write unless `--force` is given. The default mode is a preview (dry run); `--write` writes.

**Prerequisites.** Valid metadata. No manifest or warehouse connection is needed; the dialect comes from `--dialect` or the dbt profile (DuckDB when no profile is found, with a warning).

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--dialect` | auto-detected | Target dialect; generated files are dialect-flavoured - regenerate per target |
| `--write` | off | Write the generated files; without it the run is a preview (also enabled by `LOOM_WRITE`) |
| `--select` | all | Only generate models matching the selector (see [Selector grammar](#selector-grammar)): comma-separated criteria in one selector intersect, repeated flags union; a selector matching no generated models is an error |
| `--exclude` | - | Subtract models matching the selector from the generated set |
| `--force` | off | Overwrite existing files even when they are not Loom-generated |
| `--check` | off | Drift check: write nothing; exit `1` when any generated file is missing, differs, or sits stale at a previous location (takes precedence over `--write`; the CI drift mode) |
| `--format` | `text` | `text` or `json` (mode, per-mode counts, and the planned files with their state) |
| `--verbose` | off | Include the per-file lists (in a preview, each planned file's state; in a write run, which unit-test and test files were written) |

**Writes.** In write mode, generated files under the configured `model-paths` / `snapshot-paths` roots (first entry of each), honouring each instance's optional `output_subpath`. SCD2 snapshot/view outputs are create-only. **Output.** The headline states the project, dialect and mode (`writing`, `writing (LOOM_WRITE)`, or `dry run - nothing written`). A preview classifies every planned file against disk - *would write* (absent), *would change* (present and different; a differing file without the Loom header counts here but is flagged blocked, since a write run would refuse it without `--force`), *unchanged* (present and identical) - reports the three counts, lists the files only under `--verbose`, and ends with the exact command to re-run with `--write`. A write run reports a counts headline: models written with their properties, unit-test and test file counts; when SCD2 create-only files already exist on disk, the headline also states how many create-only files were already current - they are not part of the written count, so the headline never falls just because a previous run's create-only outputs are still in place. The per-file lists are behind `--verbose`. An instance's `generic_tests` declarations (practitioner generic/data tests: `test`, optional `column_name`, `kwargs`, `config`) are carried verbatim into the model's schema document's `tests:` lists - merged with the properties entry and any generator-emitted data tests, on every generator. A model declaring the envelope key `skip_properties_document: true` (in its instance params or pattern defaults) gets no properties document - and, where its generator emits data tests, no merged `data_<model>.yml` - and the report names each skip; a Loom-headed document left over from before the opt-out is reported (as `stale` in `--check` mode, `left in place` otherwise) and never deleted. In `--check` mode, per-file `missing`/`differs`/`stale` lines and a summary. A preview or write run that would create, or that runs in, the **shadowed state** (a generated model and a hand-written model sharing a name) appends a `Shadowed models` section: each pair with its generated and hand-written paths, the consequence (dbt cannot parse the project while the names collide, so every dbt command will fail until the swap is completed), and the way out (remove or move the hand-written originals once the generated models are proven, or prove the generated models in a scratch copy and swap last, per the adoption playbook's Steps 6-7); the preview forecasts the pairs its plan would create, so the warning lands before the write. The JSON payload gains a `shadowed_models` key only in this state, and pair lists fold past ten entries in text. Exit `0`, or `1` on drift, an ownership conflict, a path collision, or a failed model; the shadowed state alone does not change the exit code, because it is a legitimate mid-adoption position for the command that creates it.

```sh
loom generate                        # preview: what would change
loom generate --write --dialect duckdb
loom generate --select generator:hub --write
loom generate --check                # CI drift gate
```

**Common failures.** "Refusing to overwrite" - a planned path holds a file without the Loom header: move it, or re-run with `--force` deliberately. A drift report naming SCD2 outputs - create-only files: delete the file and regenerate to apply the metadata.

Behaviour: [Generation and output behavior](architecture.md#generation-and-output-behavior)
## `loom check`

**Purpose.** The combined gate: validate the metadata, check manifest-backed inventory coverage (including misplaced generated outputs), and verify generated instances' staging macro references (bare project or dotted package) against the manifest - existence, and arity against the manifest signature including defaults - in one command.

**Prerequisites.** A current manifest.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--manifest` | `<project-dir>/target/manifest.json` | Nondefault manifest path |
| `--strict` | off | Fail when a `generic_tests` declaration is not attested by the manifest (the same condition is a warning without this flag) |
| `--select` | all | Check only the selected models: coverage compares only them, macro references are verified only on their entries, and the success-path warnings read only their declarations (validation itself stays whole-project; scoped validation is `loom validate --select`'s). See [Selector grammar](#selector-grammar) |
| `--exclude` | - | Subtract models matching the selector from the checked set |
| `--format` | `text` | `text` or `json` (the coverage payload, on success and on a coverage failure alike) |
| `--verbose` | off | Accepted for parity; the gate form has no further detail to add |

**Writes.** Nothing. **Output.** One success line (`Inventory coverage is current.`), or, on failure, the short coverage gate form: on success it also warns (stderr, exit unchanged) for each model that declares `skip_properties_document` while its manifest entry carries no practitioner description - the opt-out's safety net; for each `generic_tests` declaration that exactly duplicates a data test the model's generator already emits (generation emits the test once, merging any config the declaration carries onto the emitted test, so the declaration's only remaining effect is its config - drop it unless that config is the point; a declaration carrying config is not redundant, since dropping it loses the config); and for each `generic_tests` declaration the manifest records no matching test for (re-run `loom generate --write` and `dbt parse`, or drop the declaration) - the last of these is an error instead under `--strict`. The gate form: the state counts, the first ten missing and orphan names with the folded remainder, and the recommended next step - the same counts `loom coverage` reports in full. The gate opens with the same filesystem shadowed-state pre-check as `coverage`, ahead of validation, coverage and the selection block (which itself reads the manifest): in the **shadowed state** (a generated model and a hand-written model sharing a name) check prints the shared verdict `Inventory coverage: CANNOT RUN - shadowed models`, its state line (the manifest cannot be refreshed and check cannot run), the pair list and the way out, ending "then re-run check"; it never surfaces the stale-manifest error or its `dbt parse` remedy in this state. The block is project-wide (`--select` and `--exclude` do not narrow it) and applies under `--strict` as well. JSON in this state is the same minimal shadowed payload as `coverage` (`{"ok": false, "state": "shadowed", "shadowed_models": [...]}`), not the coverage payload. Exit `0`/`1`; the shadowed state exits `1`.

```sh
loom check
```

Behaviour: [Fresh manifests and coverage](architecture.md#fresh-manifests-and-coverage)
## `loom coverage`

**Purpose.** Compare the Loom inventory against the project-owned dbt models in the manifest, by name and - for generated models - by expected output path. `check` runs the same comparison plus validation; use `coverage` when validation is handled elsewhere or you want the structured report.

**Prerequisites.** A current manifest.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--manifest` | `<project-dir>/target/manifest.json` | Nondefault manifest path |
| `--select` | all | Compare only the selected models, on both sides of the comparison, so the verdict describes the selection's coverage rather than the whole project's; the JSON payload gains a `selection` account. See [Selector grammar](#selector-grammar) |
| `--exclude` | - | Subtract models matching the selector from the compared set |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | Include low-level detail in text output |

**Writes.** Nothing. **Output.** Headline (`OK` / `PROBLEMS FOUND`), a state line (covered of total project models, orphan entries) on failure, and the non-empty problem sections: `missing_models`, `orphan_inventory_entries`, duplicate entries/models, `versioned_manifest_models` (same-name nodes that are dbt model versions of one model - reported distinctly, with their version labels, because Loom tracks models by name and does not support dbt model versions), and `misplaced_generated_models` (a controlled model whose manifest file is not where its layout metadata says it should be). Name lists fold past ten entries in text output; `--verbose` lists every name. One state is detected from the filesystem before any manifest read: the **shadowed state**, where a generated model and a hand-written model share a name. The manifest can never be refreshed in that state, so coverage cannot run: the report is the headline `Inventory coverage: CANNOT RUN - shadowed models`, a state line saying so, the shadowed pair list (folding past ten), and the way out (remove or move the hand-written originals once the generated models are proven, or prove the generated models in a scratch copy and swap last, per the adoption playbook's Steps 6-7; then re-run coverage). The report never prescribes `dbt parse` for this state, because no parse can succeed while the names collide. JSON prints a minimal payload (`{"ok": false, "state": "shadowed", "shadowed_models": [...]}`) instead of the coverage payload, whose counts cannot be computed. Detection is project-wide: `--select` does not narrow it. Exit `0` when coverage is complete, `1` otherwise, including in the shadowed state.

```sh
loom coverage --format json
```

Behaviour: [Fresh manifests and coverage](architecture.md#fresh-manifests-and-coverage)
## `loom prove`

**Purpose.** Run the static equivalence stage for conversions: compare two practitioner-compiled manifests - the original project's and the candidate (converted) project's - and report, per model, whether the candidate is *statically proven* equivalent to the original or the stage is *inconclusive* and the warehouse comparison decides. The stage only ever proves or abstains; there is no static disproof. It is the pre-proof for the adoption playbook's Step 6 equivalence proof: proven models may skip the warehouse comparison, inconclusive models proceed to it unchanged.

**Prerequisites.** The two manifests at their fixed names, produced by `dbt compile` of both projects under the playbook protocol: `local/equivalence/original.manifest.json` and `local/equivalence/candidate.manifest.json`. If either is absent the stage reports **not run** (naming the missing file) and exits `0` - the warehouse proof proceeds as it always has. Loom never shells to dbt for this command.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The converted dbt project (holding `local/equivalence/`) |
| `--select` | all | Report only the selected subjects: the stage's verdict models and candidate-only non-subjects form the pool, graph forms traverse the candidate manifest's graph, and the JSON record gains a `selection` account with the summary recomputed over the kept subjects. See [Selector grammar](#selector-grammar) |
| `--exclude` | - | Subtract subjects matching the selector from the reported set |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | Include low-level detail in text output |

**Writes.** Nothing. **Output.** The proof record. In text: the verdict counts, then each model's verdict with its basis, then any candidate-only models (new machinery with no original counterpart - listed, never verdicted). In JSON: the full record - stage state, both manifests' provenance (project, adapter, generated-at), the per-model verdicts, and the summary counts - which the playbook's recording step cites. A model is proven only when every condition holds: present in both manifests; neither side stateful (snapshots and incremental models always abstain); compiled code on both sides with no unresolved Jinja; no run-varying values (`current_timestamp` and kin) outside the declared differences; every original column has a candidate counterpart, and candidate additions are `__`-prefixed declared differences only; and the compiled SQL is identical after normalisation (comments, whitespace, and case outside literals and quoted identifiers collapsed; trailing semicolons dropped; the candidate's `__` declared-difference items removed from its final select list). Any other difference - expression shape, aliasing, quoting style, select-item order - abstains. The bound of the strict form, accepted for the Alpha: the comparison is over the compiled SQL as written, so a model whose SQL wraps its projection in a CTE (a wrapper or pass-through shape) does not prove statically and reports inconclusive even when the wrapped projection is identical - the warehouse comparison decides those models. Exit `0` whenever the stage runs or reports not-run; a manifest that exists but cannot be read is an error.

```sh
loom prove --format json
```

Behaviour: [The static equivalence stage](architecture.md#the-static-equivalence-stage)
## `loom plan`

**Purpose.** Plan replacing one project model with another: a read-only, manifest-backed inventory of everything that depends on the old model - direct and transitive model dependents and observed manifest consumers (tests, exposures, metrics, semantic models, saved queries, unit tests) - annotated with each model's Loom state (`controlled_by_loom` / `known_not_controlled` / `unknown_to_loom`), plus the phased migration actions. (`migration` names the pathway - adopting Loom in an existing project; `plan` phases its two operations, register and convert, so it is its own flat verb.)

**Prerequisites.** A current manifest and valid Loom inventory (invalid inventory fails explicitly rather than producing an unannotated plan). External (non-manifest) consumers are always reported as unknown - the inventory is not complete.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--from` | required | The existing project-owned model name |
| `--to` | required | The replacement project-owned model name (must differ) |
| `--manifest` | `<project-dir>/target/manifest.json` | Nondefault manifest path |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | Also show unique IDs and full dependency paths |

**Writes.** Nothing. **Output.** Replacement header with states, dependent counts and grouped lists, observed consumers by resource type, the external-consumer caveat, phased actions, and limitations. The JSON payload's `operation` field reads `plan`. Exit `0`/`1`.

```sh
loom plan --from dim_customers_scd2 --to hub_customer
```

Behaviour: [Consumer migration](architecture.md#consumer-migration)
## `loom register`

**Purpose.** The register operation of the migration programme: bring an existing dbt project's models into the Loom inventory as descriptive metadata. Every project-owned model is reported with its state (`controlled_by_loom` / `known_not_controlled` / `unknown_to_loom`); unknown models get a drafted registration (classification plus its evidence, with the manifest's declared description and `meta.owner` harvested verbatim), and registered models are listed as known. Registering describes a model; it stays hand-written - replacing it with a generated instance is `loom convert`'s operation. The underlying assessment also computes the fit proposals and interface flags `loom convert` presents, and its summary reports non-model resources as seen-but-not-assessed counts, the manifest's semantic-layer sections (semantic models, metrics, saved queries) among them; each model's consumer count is decomposed by kind, and the metrics and saved queries reachable from a model via its semantic models are named in its record and flags; records for dbt model version nodes carry their `version`/`latest_version`. If the manifest's recorded adapter does not match the adapter the project's profile declares - the signature of a substitute-adapter parse - the report carries an adapter-mismatch warning naming both adapters.

**Manifest provenance.** The report declares the manifest's compilation provenance: `manifest_provenance` is `parse-produced` when no model node carries compiled SQL - `dbt parse` does not compile model SQL - and `build-produced` when the manifest was left behind by `dbt build` or `dbt compile` and its model nodes carry compiled SQL. The declaration appears as a `Manifest:` header line in the text report and as a top-level `manifest_provenance` field in JSON. Fit calibration assumes a parse-produced manifest: on a build-produced manifest the fit rules read the macro-expanded compiled SQL rather than the authored SQL, so fit proposals can differ (and scaffold coverage, the complement of the fit set, shifts with them). A build-produced manifest therefore also earns a report-level warning advising the practitioner to regenerate the manifest with `dbt parse` and re-run the assessment before acting on the fit proposals. `loom convert`'s report declares provenance in the same way.

**Prerequisites.** A current manifest. See the [adoption playbook](adoption-playbook.md) for the full workflow.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project (relative paths such as `.` work) |
| `--manifest` | `<project-dir>/target/manifest.json` | Nondefault manifest path |
| `--write` | off | Write the drafted registrations to `local/registrations/`; never overwrites an existing file (also enabled by `LOOM_WRITE`). Without it the run is a preview |
| `--select` | all | Report (and, with `--write`, draft registrations) only for the selected models; the report's summary counts describe the narrowed pool, each kept model's record still carries its whole-project facts, and the JSON report gains a `selection` account. See [Selector grammar](#selector-grammar) |
| `--exclude` | - | Subtract models matching the selector from the reported set |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | Show controlled models individually, plus classification evidence and inventory paths |

**Writes.** Nothing without `--write`; with it, drafted registrations only - no drafts (those are `loom convert --write`). Registering a model has a second effect: it withdraws the model from `loom convert`'s drafting pool (the registration gate), and deleting the registration returns it. **Output.** The headline states the mode (`writing`, `writing (LOOM_WRITE)`, or `dry run - nothing written` - also the JSON report's `mode`/`mode_source` fields). The report leads with the readiness distribution - how many of the project's non-controlled models are ready to promote, need authoring, or are not draftable - in the JSON `summary` (the `readiness` counts) and as one `Readiness:` line in the text headline, followed by the project state counts and the manifest provenance. Text output groups unknown models by layer (with their drafted classification), folds name lists past ten entries, and shows controlled models summarised unless `--verbose`; the interface flags summarised for triage appear as compact codes bracketed on each entry with a legend, and in full prose only under `--verbose`. With `--write`, the JSON report gains a `registrations_written` path list. A preview instead gains a `drafts_preview` count object (registrations are create-only, so *would change* is always 0 and an existing file counts as unchanged), and the text preview ends with the draft-file counts and the exact command to re-run with `--write`. Report-level warnings - the adapter-mismatch and manifest-provenance warnings - print as a headed `Warnings (N):` section in text mode and appear as a top-level `warnings` list in JSON. Exit `0`/`1`.

```sh
loom register --project-dir .
loom register --project-dir . --write
loom register --format json > register.json
```

Behaviour: [Migration and AI-assisted authoring](architecture.md#migration-and-ai-assisted-authoring)
## `loom convert`

**Purpose.** The convert operation of the migration programme: draft replacing hand-written models with generated instances where a model's output shape matches a generator. Drafting is gated by registration: by default only models **unknown** to Loom are drafted - a registered model is counted and reported but not drafted, because registering it is how a practitioner withdraws it from conversion (deleting the registration returns it to the pool); `--include-registered` drafts registered models too. For unknown **and** registered models the report gives a generator-fit proposal (a heuristic match against the model's output shape, with recorded evidence and drafted parameters) or the named no-fit reason - and, for a no-fit model in a dbt layer, an authoring draft: a starting instance carrying the layer, the pre-wired input set and the declared interface copied from the manifest, a target generator named by layer as the authoring destination, and the transform as the single named authoring hole. An authoring draft is not a fit and claims no shape match; it is completed by authoring. Fit drafts and authoring drafts are one class - the unified draft - and every draft carries the declared interface copied from the manifest. A fit draft also copies the model's manifest generic tests into the instance's `generic_tests` (the carriage declarations - dbt-injected kwargs are not copied, a declared warn severity is), so the tests generate with the model after promotion; singular tests are bespoke SQL and are never copied. The report also gives the interface flags a conversion would put at risk (enforced dbt contract, Mesh access beyond the dbt default - the default `protected` is not flagged - incremental/ephemeral materialization, group membership, grants and physical warehouse configuration, attached tests, downstream consumers - the consumer/test counts include the manifest's semantic-layer and unit-test sections). A fit certifies output shape, not semantics: a draft never swaps a model, and the swap is gated on the playbook's equivalence proof. The command is project-scoped - drafts are written for every fit in the project; the per-model act is promotion, and generation of one model is `loom generate --select`.

**Prerequisites.** A current manifest. See the [adoption playbook](adoption-playbook.md) for the full workflow; conversion runs first under the registration gate - convert drafts the unknown models, and the models a practitioner decides to keep hand-written are registered afterwards, which withdraws them from the drafting pool.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project (relative paths such as `.` work) |
| `--manifest` | `<project-dir>/target/manifest.json` | Nondefault manifest path |
| `--write` | off | Write the unified drafts to `local/drafts/`; never overwrites an existing file (also enabled by `LOOM_WRITE`). Without it the run is a preview |
| `--include-registered` | off | Also draft registered (known, not controlled) models; by default they are counted and reported but not drafted |
| `--select` | all | Report (and, with `--write`, draft) only for the selected models; selection narrows within the drafting pool, so a selected registered model is still not drafted unless `--include-registered` is given. The JSON report gains a `selection` account. See [Selector grammar](#selector-grammar) |
| `--exclude` | - | Subtract models matching the selector from the reported set |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | Show controlled models individually, plus fit evidence lists |

**Writes.** Nothing without `--write`; with it, the unified drafts - no registrations (that is `loom register --write`). A draft is a set in a directory Loom never loads as inventory: a draft record (the generator and its basis - `fit` or `authoring` - the evidence or the no-fit reason with the layer, pre-wired inputs and named authoring hole, the declared interface copied verbatim, the readiness account, the suggested pattern, `review_todos`, and the exact promotion steps), a final-form instance document under `instances/` carrying the drafted parameters (for an authoring draft, the hole's parameters deliberately absent), and - when the project has no pattern for the generator - one synthesised pattern definition per generator under `patterns/`, named `<generator>_pattern`: the name is final, drafts reference it from the start, and promotion moves every file unchanged. **Output.** The headline states the mode (`writing`, `writing (LOOM_WRITE)`, or `dry run - nothing written` - also the JSON report's `mode`/`mode_source` fields). The same readiness distribution headlines as `loom register`; the JSON payload is the shared assessment report (identical `summary`, whose `readiness` counts give the distribution and whose per-model `readiness` accounts name each draft's holes - *authoring* holes only the practitioner can supply, *evidence* holes Loom could not verify - and any blockers: constructs no generator can express, which make a model not draftable even though its draft is written). Readiness is derived, never stored in the instance: every run re-derives it for unpromoted drafts from their current content plus current evidence, and a change against the account in the on-disk draft record is reported per model (`readiness_transition` in JSON, a **Readiness transitions** section in text). Text output is fits-first: a **Conversion drafts proposed** section lists every model with a conversion draft, then the **Scaffolds drafted** section - grouped by layer and target with counts in default text, per-model entries under `--verbose` (and any scaffolds a re-assessment finds promoted) - then the **No fit, no layer** record, so conversions can be triaged without reading the full register list; each remaining no-fit model carries its named reason. Interface flags appear as compact codes bracketed on each entry with a legend printed after the header; the full prose flags print only under `--verbose`, where they keep their text verbatim. Controlled models are summarised unless `--verbose`. With `--write`, the JSON report gains a `drafts_written` path list and, when an existing draft file differs from the draft the current assessment would write, a `draft_divergences` list (a **Draft divergences** section in text); diverged files are kept, never overwritten. A preview instead gains a `drafts_preview` count object (drafts are create-only, so *would change* is always 0 and an existing draft file counts as unchanged), and the text preview ends with the draft-file counts and the exact command to re-run with `--write`. Report-level warnings print as a headed `Warnings (N):` section in text mode and appear as a top-level `warnings` list in JSON. Exit `0`/`1`.

```sh
loom convert --project-dir .
loom convert --project-dir . --write
loom convert --format json > convert.json
```

Behaviour: [Migration and AI-assisted authoring](architecture.md#migration-and-ai-assisted-authoring)
## `loom inspect-sources`

**Purpose.** Inspect the physical columns of the local DuckDB sources that generated staging models map - names, types, ordinals, and which staging models map each column - without reading rows.

**Prerequisites.** A current manifest (DuckDB adapter), an existing project-local DuckDB file, and the integration dependencies (`dbt-duckdb` provides DuckDB). The file must be the one the project's profile produces for the database the sources resolve to: DuckDB names the database after the file, so a renamed copy fails the check - in the quickstart, pass `quickstart.duckdb`.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--database` | required | Existing project-local DuckDB file |
| `--manifest` | `target/manifest.json` (relative to the project) | Nondefault manifest path |
| `--source` | all mapped sources | Only this dbt source name |
| `--table` | - | Only this dbt source table; requires `--source` |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | List every column with its type and mapping models |

**Writes.** Nothing. **Output.** Per source/table: location, column count, mapped-column count, consuming models. Exit `0`/`1`.

```sh
loom inspect-sources --database quickstart.duckdb --source raw
```

Behaviour: [Source-structure inspection](architecture.md#source-structure-inspection)
## `loom snapshot-sources`

**Purpose.** Capture an explicit, create-only baseline of the physical source structure (the `inspect-sources` view, minus mapping detail) so later drift can be reported against it. The baseline is a local artefact: its path must be git-ignored. The default mode is a preview; `--write` captures.

**Prerequisites.** As for `inspect-sources`, plus git (to verify the output path is ignored).

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--database` | required | Existing project-local DuckDB file |
| `--manifest` | `target/manifest.json` (relative to the project) | Nondefault manifest path |
| `--output` | `target/loom/source-structure.json` (relative to the project) | Baseline path; must be new and git-ignored |
| `--write` | off | Capture the baseline (also enabled by `LOOM_WRITE`). Without it the run is a preview |
| `--format` | `text` | `text` or `json` (mode, output path, existence, and source/table counts) |
| `--verbose` | off | In a preview, list each source table the baseline would capture |

**Writes.** With `--write`, the baseline JSON, atomically, never overwriting. **Output.** The headline states the mode (`writing`, `writing (LOOM_WRITE)`, or `dry run - nothing written`). A capture reports the captured path. A preview reports the baseline that would be captured - its path and the source tables it would cover (each listed under `--verbose`) - notes when a baseline already exists there (a capture would refuse), and ends with the exact command to re-run with `--write`. Exit `0`/`1`.

```sh
loom snapshot-sources --database quickstart.duckdb          # preview
loom snapshot-sources --database quickstart.duckdb --write
```

Behaviour: [Source-structure inspection](architecture.md#source-structure-inspection)
## `loom diff-sources`

**Purpose.** Report physical source-structure changes against a captured baseline: sources/columns added or removed, type, ordinal, and location changes, with the staging models that map each changed column. Advisory only - it never writes and never sets failure severity.

**Prerequisites.** As for `inspect-sources`, plus an existing baseline from `snapshot-sources`.

| Option | Default | Effect |
| --- | --- | --- |
| `--project-dir` | current directory | The dbt project |
| `--database` | required | Existing project-local DuckDB file |
| `--manifest` | `target/manifest.json` (relative to the project) | Nondefault manifest path |
| `--baseline` | `target/loom/source-structure.json` (relative to the project) | The captured baseline |
| `--format` | `text` | `text` or `json` |
| `--verbose` | off | Also show which models map each changed column |

**Writes.** Nothing. **Output.** "No changes", or the change count grouped by source/table and change kind. Exit `0` unless the baseline or inputs are invalid (`1`).

```sh
loom diff-sources --database quickstart.duckdb
```

Behaviour: [Source-structure inspection](architecture.md#source-structure-inspection)
