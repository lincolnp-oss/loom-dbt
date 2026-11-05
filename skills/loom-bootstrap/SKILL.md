---
name: loom-bootstrap
description: Start a brand-new Loom project from zero - a dbt project with no Loom metadata yet. Use when the practitioner is setting Loom up for the first time, wants the guided first-project walkthrough, or needs the project layout and their first pattern definition, instance set or registration authored. Not for adopting Loom into a project that already has hand-written models to assess (that is loom-adoption) or for adding models to a project already on Loom (loom-authoring).
---

# Bootstrap - a new Loom project from zero

The core rules apply throughout (`loom-core`): Loom copies, you
propose, the practitioner decides.

The starting position: a new (or still Loom-less) dbt project, no
Loom metadata yet, and **no manifest needed to start** - `loom
validate`, `loom generate` and `loom search` work on metadata alone.
The manifest matters later, when registering or assessing models dbt
already knows about; do not make the practitioner produce one before
there is anything to assess.

## The guided on-ramp

**Start with Loom's first-project example.** The [first-project
example](https://github.com/lincolnp-oss/loom-dbt/blob/main/examples/first-project/README.md)
is the smallest complete Loom project - two pattern definitions
(`staging`, `current_hub`), two staging instances and a hub
instance, one hand-written model with its registration, seeds and a
local DuckDB profile - worked end to end as *declare, generate,
prove, ask*. Walk the practitioner through it (or copy it as the
starting tree) before designing anything of their own; when they can
say what Loom did there, they have the whole authoring loop.

The example's metadata files ship bundled with this skill under
`references/first-project/` - verbatim excerpts of the shipped
example at their example paths: the two pattern definitions, the
two instance sets, the sources declaration and one registration.
Read the file you need rather than the whole set.

## Project layout

```text
metadata/patterns/        pattern definitions (one generator each)
metadata/instances/       instance sets (models Loom generates)
local/registrations/      registrations for hand-written models
local/drafts/             unified convert drafts - a review area
                          (Loom never loads it; not metadata)
local/equivalence/        the prove protocol's manifest pair
models/                   dbt models - generated output lands under
                          models/_generated/ (staging/, tests/)
dbt_project.yml           the ordinary dbt project file
```

Loom documents carry **no `document_type` field and no format
version** - a document is classified by the directory it lives in.
Do not add either field, in any document, ever.

## Author the first pattern and instance, together

1. A **pattern definition** in `metadata/patterns/<name>.yml`: a
   `name`, one `generator`, plus any shared `defaults` /
   `required_params`. Naming a practitioner-chosen pattern is the
   practitioner's act - propose, they choose. (Synthesised draft
   patterns during adoption are different: their
   `<generator>_pattern` name is final - see `loom-adoption`.)
2. An **instance set** in `metadata/instances/<set>.yml`: the
   `pattern` name, and `instances:` - each with a `name` and
   `params`. Every instance needs at least a `description` and an
   `owner`; both are the practitioner's facts, not yours to invent.
   A staging instance over one real source is the natural first
   model: it is the adapter layer everything else consumes. Its
   column items take the consolidated form - `{source, alias}`,
   plus `macro` / `macro_args` / `macro_column_param` on a
   macro-mapped column:

   ```yaml
   columns:
     - {source: id, alias: customer_id}
     - {source: customer_name, alias: customer_name}
   ```

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

   And the merge rule every document obeys: a field may live in a
   pattern's `defaults` **or** in an instance's `params` - never
   both. There is no override precedence; a field in both places
   is a validation error, so shared values go in `defaults` and
   per-model values in `params`.

3. Any model the practitioner keeps hand-written gets a
   **registration** in `local/registrations/`: `name`,
   `classification`, description, owner - descriptive only; Loom
   does not generate it.

Parameters across generators use the consolidated forms: generators
name their inputs as the `input_models` list; a link's endpoints are
one `endpoints` list of `{hub, key}`; a satellite, PIT or bridge
names its parent as a `parent` block (`{model, key}`, or `{model,
endpoints, dependent_child_key}` over a link parent). When an
example or an older document shows a retired form (a lone
`input_model`, `left_hub`/`right_hub`, `from`/`to` column items),
treat it as stale and use the consolidated form - the command
reference's per-generator sections carry the current parameter
lists.

## The first loop

```text
loom validate            # metadata sound?
loom generate            # preview: what would be written
loom generate --write    # write it (practitioner-ratified)
loom search              # see the inventory you just made
```

`generate` previews by default on every mutating command - the
preview is the proposal, `--write` is the commitment (see
`loom-core` on write safety). If the practitioner wants the proof,
`dbt seed` / `dbt build` in their environment builds the generated
files as ordinary dbt.

**End state:** the project validates, generates, and its inventory
answers questions - every generated model declared once in metadata,
every hand-written model registered. From here the project grows
through the authoring loop (`loom-authoring`), questions go to
`loom-ask`, and upkeep to `loom-maintenance`.
