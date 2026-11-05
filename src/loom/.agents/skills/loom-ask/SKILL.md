---
name: loom-ask
description: Answer questions about a dbt project with Loom's inventory search - what exists, what state it is in, what depends on what, what a model's shape is, whether something like it already exists. Use when the practitioner asks a question about the project ("what do we have", "who owns X", "what breaks if this changes", "is there already a model like this", "which macros exist and who calls them") rather than asking for a change. Read-only - this workload never writes drafts or edits anything.
---

# Inventory & ask - questions about a project

The practitioner asks what a project contains, what state it is in,
or what depends on what - and wants Loom's answer, not your
recollection. The instrument is `loom search` over one corpus: Loom
metadata and the dbt manifest as a single derived inventory. There
is no server, no index and no warehouse connection. The standing
rules apply (`loom-core`), and **this workload is read-only: never
pass `--write`, never write anything.**

Search never refuses to answer over a stale manifest - it labels the
manifest's generation time and age in its output, so quote that
label when it matters. With no manifest at all, search runs in
**metadata-only mode**: only Loom-known objects appear, and the
output says so.

## The corpus and its statuses

Every model record carries a **derived status** - a fact about which
of metadata and manifest know the model, recomputed per run:

- `generated` - an instance exists in Loom metadata; Loom generates
  the model. Its metadata is its definition.
- `registered` - a registration exists; Loom knows and can describe
  the model, but it is hand-written and Loom does not vouch for its
  SQL. Registered models are also out of the convert pool.
- `unmanaged` - the manifest knows it; Loom has no record. You may
  conclude nothing about it beyond the manifest's facts until it is
  registered.
- `orphaned` - Loom metadata names it; the manifest does not. The
  usual cause is a model deleted outside Loom; orphaned entries are
  a maintenance finding, not a fact to act on alone.

In **metadata-only mode**, `unmanaged` and `orphaned` are
*unknowable* - the corpus has no manifest side to derive them from,
and no output implies them. Never report a project as free of
unmanaged or orphaned models from a metadata-only run; say the
corpus was metadata-only and a `dbt parse` widens it.

Each record also unifies the model's **documentation** from both
sides - Loom metadata (`description`, `column_docs`,
`column_types`) and the manifest (model and column descriptions) -
with a **provenance**: `loom`, `manifest`, or `both-agreeing`. The
winner rule is Loom for generated models, manifest otherwise; the
losing text is not indexed, so a `doc:` match is a match against the
winning text only. When you quote a description, name its
provenance, because it tells the practitioner which side to edit.

## Asking - the query language

`loom search` takes selector arguments in the shared grammar (the
full catalogue lives in `loom-core`; this is how to *ask* with it):

- **Start with the overview.** `loom search` with no query prints
  the project: counts by status, generator and paradigm, top owners
  and the unowned count, documentation coverage, macro counts split
  project/package - and **suggested queries drawn from the project's
  actual state**, including the exception queries below that
  currently return non-empty sets. For "what do we have?", this is
  the answer; do not assemble it from per-model reads.
- **One question, one selector string.** Commas within an argument
  intersect, separate arguments union: `loom search
  "key:customer_id,column:email"` is models keyed on customer_id
  *and* outputting email. Qualifier values match exactly; bare terms
  are ranked forgiving matches (exact name, then prefix, then
  substring, then fields, then documentation). `loom search
  <name>` for a known name; `kind:` to leave models entirely (next
  section).
- **Impact questions are graph questions.** Operator form for
  selection-shaped asks (`+hub_customer`, `hub_customer+`,
  `+name+2`), word form for reading (`downstream:hub_customer`,
  `"downstream:hub_customer,depth:2"` - bare word form is the full
  transitive closure). For a macro, downstream means impact: the
  models that call it and everything downstream of them.
- **A single hit is a detail view, not a list row.** When a query
  resolves to exactly one object, read the whole record: status,
  generator/pattern, paradigm, owner, tags, description (with
  provenance), grain keys, declared inputs, output columns in order
  (marked partial where the record is partial - a partial column
  list is a bound on what you may claim, not a gap to fill),
  upstream/downstream counts with nearest neighbours, test presence,
  file location. Answer from the record, not from the headline.
- **A known name reaches the same view directly.** A bare name
  borne by exactly one model opens that model's detail view even
  when other records match - its tests, whose names embed the
  model's name, above all - with the other matches listed beneath
  it in compact form. `loom search <name>` is the known-name
  lookup; no `kind:model,` prefix is needed to reach the record.
- **Zero results are diagnoses, not dead ends.** A zero-result
  search names which criterion emptied the set, lists the facet
  values that do exist for the qualifiers used, and offers "did you
  mean" names for a mistyped bare term. Relay that structure: which
  term to drop is usually the practitioner's actual question.
- **Machine forms when the answer feeds a next step:** `--fields`
  to trim JSON records, `--count-only`, `--names-only` for piping.
  And the query string is reusable: the same selector goes to
  `--select` on `register`, `convert`, `generate`, `check` and
  friends - with the strictness caveat below.

**The retired flag interface is gone.** The old per-field flags
(`--text`, `--generator`, `--classification`, `--owner`, `--tag`,
`--column`, `--key`, `--dependency`, `--paradigm`) meet the ordinary
unknown-option error. Never use them; every one of those questions
is a qualifier now (`input:` replaces the old dependency flag -
there is no `dependency:` qualifier).

## Search by shape - banding and `--similar-to`

When a search query carries **two or more shape qualifiers**
(`key:`, `column:`, `input:`, `generator:`, `paradigm:`), plain
search stops intersecting and starts **banding**: full matches
first, then close matches ranked by a weighted fingerprint of the
described shape - **keys 5, inputs 4, output columns 3, generator
family 1** - with one gate: a candidate sharing none of the
described keys ranks below every candidate that shares the grain,
whatever its other overlap. The output labels this itself: a header
line states that close matches are shown by shape overlap, the
full-match and close-match counts are distinguished, and each close
match carries its score plus a **shares / differs** account naming
the actual keys, inputs and columns. Report close matches *as*
close matches, with their differs - never promote one to "a match"
in your summary.

`loom search --similar-to <model>` is the same comparison anchored
on a real model instead of a described shape: the model's
fingerprint is the target (components that cannot be derived are
named in the header and excluded from scoring, not scored as zero),
the model itself is excluded and shown as the reference, and extra
elements on a candidate are reported as differences. Models
sharing no overlap with the reference are omitted from the
ranking, and the output states how many were omitted - relay that
count rather than letting the ranked list read as the whole
project. This is the
duplication check's second step - "more like this one" - and its
language discipline is absolute: similarity output says **close
matches** and **shares / differs**, never "duplicate". Shape overlap
is evidence for a human judgement, not a verdict; say so when you
relay it.

**The strictness distinction, stated plainly:** banding is a
*search* behaviour only. `--select` never bands - the identical
selector string on an action command takes only the full matches,
so a query you found a shape with will scope an action to strictly
less than the search showed. When you hand a practitioner a selector
to reuse, tell them which of the two readings it will get.

## Exception queries - the hygiene questions, verbatim

These are reusable exactly as written, in search and as `--select`
strings on `register` and `check`:

- `is:unmanaged` - manifest models Loom has no record of (the
  registration backlog).
- `is:orphaned` - metadata entries whose model is gone from the
  manifest.
- `has:no-owner` - models with no owner named.
- `has:no-tests` - models with no tests attached.
- `has:no-description` - models with no description on either side.
- `has:undocumented-columns` - models with output columns lacking
  documentation.

The overview already suggests the ones that currently bite. An
exception query returning an empty set is a *finding* ("nothing is
unmanaged") - report it as one, with the corpus mode it ran over.

## Tier 2 - macros, sources and the rest of the project

The corpus is wider than models: `kind:` selects `macro`, `source`,
`seed`, `snapshot`, `test` and `exposure` records too. Tier-2
matches group beneath the models by kind, then by provenance -
**project first, then each package by name, presented exactly like
project matches**; a `kind:` qualifier promotes that kind to the
primary table with the columns that kind is read by (sources and
seeds: downstream count; snapshots: the model snapshotted; tests:
the model tested; exposures: upstream count).

**Macros are the reuse surface.** A macro's detail view carries its
signature - arguments with types and defaults read from the
signature itself - provenance and package, origin path, description,
and its **used-by** list: which models call it. Two reading skills:

- *Reuse vs write-new:* a macro with the right shape and real
  used-by is a candidate to reuse; an unused macro is a candidate
  for nothing yet. Search by name, arguments and description -
  macro *bodies* are not indexed, so never claim what a macro does
  beyond its signature, description and call sites.
- *The slot-compatibility note:* the detail view states, from the
  signature alone, whether the macro can fill the one slot a macro
  can fill - the staging column mapping. It names the parameter the
  mapped column fills at position 1 by default (`macro_column_param`
  is the pin that places it elsewhere) and how many `macro_args` a
  mapping must supply alongside the column to reach the last
  argument without a default. A zero-argument macro cannot fill the
  slot at all. The note states facts about the slot, never a
  judgement about the macro - relay it that way. (Behind it sit the
  verification tiers from `loom-adoption`: references, bare or
  `package.name`, are verified against the manifest by check and
  convert; position is the second tier.)

Macros and other tier-2 records are **findable, not selectable**:
kinds outside a command's pool match nothing in it, so a macro can
never be `--select`ed into `generate` or `register`. Their next
actions are view and inspect, not generation.

## Which instrument answers which question

- *"What do we have / what state is it in?"* - the search overview,
  then a targeted query. For the adoption-shaped picture (readiness
  distribution, drafted registrations, fit proposals) the instrument
  is a **read-only** `loom register` / `loom convert --format json`
  pass (`loom-adoption` step 1) - search does not carry readiness,
  and register/convert do not need `--write` to report.
- *"Is everything covered?"* - `loom coverage`: `missing_models`,
  `orphan_inventory_entries`, `misplaced_generated_models`,
  duplicates, versioned models. Coverage compares names and paths;
  search describes records. Do not answer a coverage question from
  search output.
- *"What breaks if this changes / what uses this?"* - search's graph
  forms for a quick read; `loom plan --from <old> --to <new>` for
  the replacement-shaped answer (dependents, observed manifest
  consumers by kind, phased actions). **External (non-manifest)
  consumers are always unknown** - every instrument says so, and so
  must you.

## Honest reporting

Quote counts, names, statuses and readiness from the output you just
read; attribute facts to their source (manifest, registration,
instance metadata - and for descriptions, their provenance); state
the corpus mode and the manifest's age when either could mislead;
state unknowns as unknowns - especially external consumers, anything
about an `unmanaged` model beyond the manifest's facts, and any
record whose columns are marked partial. Never present your
paraphrase, inference or prior conversation as a project fact, and
never answer from a stale report when a fresh read is one command
away.
