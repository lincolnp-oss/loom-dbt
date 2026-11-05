---
name: loom-maintenance
description: Keep an adopted Loom project healthy over time. Use when the drift check fails, dbt work happened outside Loom (new hand-written models, deleted models, coverage findings), a physical source changed shape, a validate/generate error needs diagnosing, or the practitioner asks what changed since the last assessment. Not for the initial adoption itself (loom-adoption).
---

# Maintenance - an adopted project over time

The core rules apply throughout (`loom-core`). The project is on
Loom; dbt work continues around it. Four recurring jobs, plus one
rhythm underneath all of them.

## Drift - `loom generate --check` fails

The check names each generated file that is missing, differs, or is
stale. Before proposing anything, establish *which side moved*,
because the remedy differs:

- The **generated file was hand-edited** - its content no longer
  matches what the metadata renders. Evidence: `git log` / `git diff`
  on the generated file, with no corresponding metadata change. The
  metadata is still the definition; the edit is either deliberate
  (the practitioner takes the file back under manual control - its
  Loom header comes off and it leaves the generated set) or
  accidental (regenerate and the edit is lost - say so plainly
  before anyone approves that).
- The **metadata changed without regenerating** - evidence: the
  recent history is in `metadata/`, and the generated diff is exactly
  what the metadata change implies. Review that diff as a whole; a
  shared pattern change re-renders every instance using it.

Either way, **regeneration is a practitioner-ratified act**: show the
diagnosis and the preview (`loom generate` lists each file as *would
write* / *would change* / *unchanged*), then `loom generate --write`
on their go-ahead. Never reach for `--force` - a drift failure is
information, not an obstacle to override. A stale SCD2 create-only
output is the one file-level remedy: delete the file and regenerate.

## Coverage hygiene - dbt work happened outside Loom

New hand-written models show up as `unknown_to_loom` in a read-only
`loom register` pass and as `missing_models` in `loom coverage`.
Triage them with the practitioner, per model: convert-draft what
might convert, register what stays hand-written (the adoption loop -
drafts are written only on their go-ahead, with `--write`), and leave
genuinely out-of-scope resources alone. Symmetrically, deleted models
leave `orphan_inventory_entries`: confirm the deletion was deliberate
before proposing the inventory record be removed. Narrow a hygiene
pass with `--select` where the project is large. Hygiene is a rhythm,
not a cleanup event - the goal is that `loom coverage` stays a gate
the practitioner trusts.

## Source drift

The read the other two build on is `loom inspect-sources --database
<file>`: it inspects the physical columns of the local DuckDB sources
that generated staging models map - names, types, ordinals, and which
staging models map each column - without reading rows. Per
source/table it reports location, column count, mapped-column count
and consuming models; `--source` / `--table` narrow it. Reach for it
when the question is what physically exists *now* - does this source
still carry the column this staging model maps, what type does it
carry today - before deciding there is drift to manage at all. Its
prerequisites are a current manifest (DuckDB adapter) and the
project-local DuckDB file the profile itself produces: DuckDB names
the database after the file, so a renamed copy fails the check.

`loom snapshot-sources --database <file>` captures a baseline of
physical source structure - it is a mutating command, so it previews
by default and writes the baseline only under `--write`.
`loom diff-sources --database <file>` reports what changed against
that baseline (sources/columns added or removed, type/ordinal/location
changes, with the staging models that map each changed column).
Advisory only - report it; changing a staging mapping to absorb a
source change is an authoring proposal (`loom-authoring`), not an
automatic act.

## Error diagnosis

`loom validate` and `loom generate` errors name the file, the field
and the expected shape - start there, not with a guess. Work the
error against the metadata as written: unknown parameter (check for
a retired form - `input_model`, `left_hub`/`right_hub`, `from`/`to`
column items and the `document_type` field are all gone; the
consolidated forms in `loom-authoring` are current), missing required
parameter, a referenced input model absent from the inventory, a
`__TODO__` sentinel still standing, the `generic_tests` +
`skip_properties_document` conflict, an ownership conflict where a
planned output path holds a non-Loom file. Fix proposals follow the
standing rules: metadata edits are proposals until ratified, and a
diagnosis you cannot pin to a named error and a file is an
escalation, not a theory to act on.

## The rhythm - re-assessment

Readiness is recomputed every run from the drafts' current content,
so completing a draft's holes, refreshing the manifest, or deleting a
registration shows up as a `readiness_transition` in the next
assessment report. When a practitioner asks "what changed since last
time?", re-run the assessment (`loom register` / `loom convert`,
read-only) and answer from the transitions - never from memory.
