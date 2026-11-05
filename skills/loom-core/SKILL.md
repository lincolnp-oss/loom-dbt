---
name: loom-core
description: The shared foundation for any work with Loom, the authoring-time dbt CLI - its mental model (inventory states, readiness), the copy/interpret/decide responsibility boundary, the write-safety and manifest prerequisites, the selection grammar every model-scoped command shares, and the escalation rules. Load this before or alongside any other loom-* skill, and whenever a Loom question turns on what may be copied, proposed, decided, selected or escalated rather than on one workload's steps.
---

# Loom - core

Loom is an authoring-time CLI for dbt. A project declares its models
once as metadata - pattern definitions in `metadata/patterns/`,
instance sets in `metadata/instances/`, registrations for
hand-written models in `local/registrations/` - and Loom validates
that metadata and generates ordinary dbt SQL and tests from it
(under `models/_generated/`). Documents are classified **by their
path alone**: there is no document-type field in any Loom document,
and no format-version stamp. Do not add either.

You run Loom's commands, draft and propose; the practitioner decides
and performs every irreversible act. The workload skills
(`loom-bootstrap`, `loom-adoption`, `loom-ask`, `loom-authoring`,
`loom-maintenance`) carry the procedures; this skill carries the
rules every procedure stands on.

Canonical references - where any loom skill paraphrases a Loom
document, that document governs:

- [Adoption playbook](https://github.com/lincolnp-oss/loom-dbt/blob/main/docs/adoption-playbook.md) - the adoption procedure in full.
- [Command reference](https://github.com/lincolnp-oss/loom-dbt/blob/main/docs/commands.md) - every command's options, writes, output and exit codes.
- [First-project example](https://github.com/lincolnp-oss/loom-dbt/blob/main/examples/first-project/README.md) - the smallest complete Loom project.

Two canonical documents ship bundled with this skill for offline
use: the command reference at `references/commands.md` and the
terminology reference at `references/terminology.md`. When a
command's options, writes or exit codes matter, or a term's exact
meaning is in question, read the relevant section of the bundled
copy rather than the whole file.

## The mental model

**States.** Every dbt model is in exactly one state, and the state
bounds what may be concluded about it:

- `controlled_by_loom` - a generated instance exists; Loom generates
  the model. Its metadata is its definition.
- `known_not_controlled` - a registration exists; Loom knows and can
  describe the model, but it is hand-written and Loom does not
  generate or vouch for its SQL.
- `unknown_to_loom` - the manifest knows it; Loom has no record.
  Nothing may be concluded about it beyond the manifest's facts
  until it is registered.

**Readiness.** For any non-controlled model, the assessment derives
a readiness account - never stored, recomputed on every run from the
drafts' current content plus current evidence:

- `ready_to_promote` - a draft with zero holes and zero blockers.
- `needs_authoring` - named holes stand between the draft and
  promotion. A hole is one of two kinds: **authoring** (only a
  person can supply it - a transform, a description, an owner) or
  **evidence** (Loom could not verify something against the
  manifest - its remedy is a current manifest or a corrected
  declaration, never invention).
- `not_draftable` - no draft, or a named blocker no generator can
  express.

Unfinished content is marked with the `__TODO__` sentinel (an
unpaired staging column drafts as `source: __TODO__`; a create-new
test with unknowable arguments carries `kwargs: {__TODO__: …}`).
Validation rejects the sentinel: no placeholder may survive into
loadable metadata, and an unfinished declaration can never validate
silently.

**System columns are Loom's, in the `__` namespace.** Generated
models stamp `__`-prefixed columns - `__loaded_at` on most, plus
`__valid_from` / `__valid_to`, `__is_deleted`, `__hash_diff`,
`__record_source`, `__snapshot_date`, `__weighting_factor`, and
`__<satellite>_ldts` on PIT tables. Never author, rename or map
practitioner columns onto these names; in an equivalence proof they
are declared differences by rule, not mismatches.

## The standing rules - copy / interpret / decide

This division is the whole design. Do not blur it.

- **Copy is Loom's job.** Facts the project has already declared -
  descriptions, `meta.owner`, column docs/types, declared
  incremental config, SQL comments, dependency inputs, declared
  interfaces - are harvested verbatim by `loom register` /
  `loom convert`. Never re-derive, summarise or "improve" a
  declared fact yourself, and never present your paraphrase as the
  project's fact.
- **Interpret is your job, as proposals only.** Where Loom leaves a
  named hole - a draft's authoring hole or readiness holes, a
  `__TODO__`, a `review_todos` item, a `source_rationale` note (the
  model's SQL comments, copied verbatim) that needs placing or
  pruning, a pattern left as `TODO` because several exist - you may
  propose content for that hole, labelled as your proposal, with
  your reasoning. A proposal changes nothing until the practitioner
  ratifies it.
- **Decide is the practitioner's job.** Promotion (moving a draft
  into `metadata/`), the swap (deleting the hand-written model
  file), `--force`, and accepting any difference in an equivalence
  proof are practitioner acts. Never do them yourself, never imply
  they are done, and stop and ask instead.

Further hard rules:

- Work on a branch; work one draft at a time.
- Never use `loom generate --force` - overwriting a
  practitioner-authored file is the practitioner's act.
- Never delete or overwrite a hand-written model file.
- Never skip the equivalence proof, and never convert a stateful
  resource (snapshots and similar) by the adoption pathway - it
  stays registered and hand-written.
- A `no_fit` is a successful outcome, not a problem to argue with.
  Do not force a fit, and do not call an authoring draft a fit: its
  target generator names a destination, not a shape match.
- Do not treat an evidence hole as an authoring hole (see Readiness
  above).
- Interface flags (enforced contract, Mesh access, attached tests,
  downstream consumers, incremental/ephemeral materialization,
  groups, grants/physical config) are coordination blockers. Do not
  convert a flagged model in the same pass as an unflagged one;
  surface the flags and let the practitioner sequence them.

## Write safety - one rule on every mutating command

The mutating commands - `generate`, `register`, `convert`,
`snapshot-sources` - all behave the same way: **they preview by
default and write only under `--write`.** A preview writes nothing;
its output states the mode (`dry run - nothing written`) and, for
`generate`, ends with the exact command to re-run with `--write`.
The environment variable `LOOM_WRITE` (set to anything but
`0`/`false`/`no`) turns writing on for a session; the flag wins over
the environment, and the run's banner says when the environment set
the mode (`writing (LOOM_WRITE)`; in JSON, the `mode` /
`mode_source` fields). Treat a write run as a practitioner-ratified
act: show the preview first, then run with `--write` on their
go-ahead. The retired spellings `--write-drafts` and `generate`'s
`--dry-run` are gone - if a command rejects them, switch to the
preview-by-default form above; there is no other write vocabulary.

`loom generate --check` is separate: the CI drift mode. It writes
nothing, takes precedence over `--write`, and exits 1 when any
generated file is missing, differs, or sits stale.

## Prerequisites - check first

1. `loom --version` works, and you are in (or pass `--project-dir`
   for) the practitioner's dbt project.
2. **For the manifest-backed commands only** (`register`, `convert`,
   `plan`, `coverage`, `prove`, `check`): `target/manifest.json`
   exists and is current. The assessment reads the manifest, not the
   warehouse. Produce it with **`dbt parse`** - the assessment
   consumes a *parse-produced* manifest (authored SQL, no compiled
   code); a manifest left by `dbt build` / `dbt compile` is
   *build-produced*, the report says so in `manifest_provenance` and
   warns, because fit proposals read from compiled SQL can differ.
   On that warning, have the manifest regenerated with `dbt parse`
   before acting on any fit proposal. Prerequisites that bite in
   practice: the profile's adapter installed and able to
   authenticate for a parse, the project's `require-dbt-version`
   satisfied (Loom's tested line is dbt Core 1.12.x, manifest
   schemas v10–v12), and any `env_var(...)` values set. A manifest
   parsed with a substitute adapter is assessment-only - Loom warns
   on the mismatch (look for `warnings` in the report) and nothing
   may be proven from it. If the manifest is missing or stale, tell
   the practitioner to produce it; the parse is their environment's
   job.
3. `git status` is understood - pre-flight commands such as
   `dbt deps` can rewrite tracked files.

Use `--format json` for every report command (all commands carry
`--format` / `--verbose` parity). `validate`, `prove` and
`create-new` are shell commands; where an MCP server is connected,
its tools wrap only the six loop commands and return the same JSON.

## Selection - one grammar on every model-scoped command

`--select` and `--exclude` accept the same selector strings on every
model-scoped command - `generate`, `convert`, `register`, `check`,
`coverage`, `prove`, `search` and `validate` - and selection always
**narrows within the command's existing pool** (selection never
widens a pool: convert still drafts only unknown models unless
`--include-registered` is given). The grammar is dbt's, in two
layers:

- **Set operators mirror dbt exactly.** Within one selector
  argument, commas **intersect**: `"tag:finance,generator:hub"` is
  finance-tagged *and* hub-generated. Separate arguments (repeated
  `--select` flags, or space-separated terms) **union**. There is
  no `AND`/`OR` keyword and no parentheses. `--exclude` negates with
  its own selector arguments. (This is a deliberate flip: generate's
  old ad-hoc comma-as-union reading is gone.)
- **Compatibility layer.** Bare names and `*` name patterns (a bare
  package name selects that package's records; a dotted fqn tail
  like `marts.finance` selects by path segment); the methods dbt
  practitioners already know where they overlap Loom fields
  (`tag:`, `path:`, `package:`); graph operators `+` as prefix
  (upstream), suffix (downstream) or both, `@`, and depth forms
  such as `2+name` and `name+3`. One dbt form is deliberately
  **refused**: `config.` matching - Loom's records carry no dbt
  config fields, so there is nothing truthful for it to match, and
  the error says so. Do not reach for it.
- **Loom layer** (exact match by default; bare terms are forgiving
  substring matches - the split is deliberate): `generator:`,
  `pattern:`, `paradigm:`, `classification:`, `owner:`, `column:`,
  `key:`, `input:` (declared inputs; there is no `dependency:`
  qualifier), `is:` (`generated`, `registered`, `unmanaged`,
  `orphaned`, `project`, `package`), `has:` (`owner`, `no-owner`,
  `tests`, `no-tests`, `description`, `no-description`,
  `undocumented-columns`), `doc:` (substring over model and column
  documentation), `kind:` (`model`, `macro`, `source`, `seed`,
  `snapshot`, `test`, `exposure`), `package:`, `selector:` (a
  dbt-style named selector from the project's `selectors.yml` -
  Loom defines no saved-selection format of its own), and the
  word-form graph qualifiers `upstream:` / `downstream:` (bare
  form = full transitive closure; an optional `depth:` companion
  in the same selector bounds it, as in
  `"downstream:hub_customer,depth:2"`).

An **unmatched selector is an error naming it** - deliberately
stricter than dbt, which warns. Never work around that error by
silently widening or dropping the selector; report it and let the
practitioner correct it. On `validate`, selection scopes by document:
the documents containing the selected models (plus the patterns they
use) are validated whole, and the report says how much was included.
`plan` (pair-scoped) and the sources commands (source-scoped) take
no `--select`.

## Escalation - do not resolve residuals silently

Applies to every workload. Stop and hand back to the practitioner,
with the evidence, when:

- a model is `not_draftable` (named blockers no generator can
  express), or an authoring draft's target generator cannot express
  its transform;
- a fit's evidence contradicts the model's SQL, or a drafted
  parameter cannot be verified against the original;
- an evidence hole stands - the remedy is the practitioner's
  environment or declaration (current manifest, corrected call),
  not your content;
- interface flags require coordination (contract, Mesh, tests,
  consumers, groups, grants);
- the equivalence proof fails, comes back inconclusive *and* the
  warehouse proof cannot settle it, or cannot be run at all
  (stateful model, no identical source data, substitute-adapter or
  build-produced manifest);
- a hole needs a decision, not a proposal (pattern choice among
  several, accepting a dropped contract or a materialization
  change, the properties-document decision where both sides carry
  content);
- the manifest is stale, missing, or warned;
- a selection names something that does not exist, or a drift or
  coverage finding's cause cannot be established from the evidence
  (generated file vs metadata, deliberate vs accidental deletion).

An escalation is a short report: model, what Loom said, what you
checked, what is blocked, and the decision you need. Never paper
over a residual by editing around it.
