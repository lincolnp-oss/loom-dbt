---
name: loom-authoring
description: Extend a project that is already on Loom - add a new generated model, a new pattern or instance, or a test on an existing model. Use when the practitioner wants a new model declared in metadata, asks for a unit test or data test on a model, or needs generated output refreshed after a metadata change. Not for first-time setup (loom-bootstrap) or converting existing hand-written models (loom-adoption).
---

# Authoring - extending a project on Loom

The core rules apply throughout (`loom-core`): metadata is authored,
generated SQL never is; proposals are ratified before they land;
`generate` previews by default and writes under `--write`.

When a parameter shape is unknown, read the relevant generator's
section of `references/metadata-reference.md` - a bundled copy of
the metadata reference that ships with this skill. Read the
section, not the whole file.

## The authoring loop

1. **Search first** - `loom search` with the filters that match the
   intent, so you do not author a duplicate. (Search's own grammar
   and result reading are `loom-ask`'s territory; the duplicate
   check needs only a name/column/key pass.)
2. **Draft metadata only:** a pattern instance in
   `metadata/instances/` (and a pattern definition in
   `metadata/patterns/` only if none fits). Never draft generated
   SQL. Use the consolidated parameter forms - inputs are the
   `input_models` list; a link's endpoints are one `endpoints` list
   of `{hub, key}`; satellite/PIT/bridge parents are a `parent`
   block; a hub's `record_source` is a string, or a mapping over its
   `input_models`; dimension attributes are `attribute_columns`;
   staging column items are `{source, alias, macro, macro_args,
   macro_column_param}` or `{expression, alias}` (Expression
   items, below). Documents carry no `document_type` field - the
   directory classifies them.
3. **Gate on `loom validate`**, then propose; the practitioner
   ratifies. On a large project, `loom validate --select
   <selector>` scopes validation to the documents containing the
   selected models (validated whole - see `loom-core`).
4. **Render:** `loom generate` previews every planned file as *would
   write* / *would change* / *unchanged* and ends with the exact
   `--write` command; run `loom generate --write` on the
   practitioner's go-ahead. Narrow with `--select` - a model name,
   `generator:<name>`, graph forms like `+<model>` - using the shared
   grammar (`loom-core`); comma within one argument intersects,
   separate arguments union. Never `force`.
5. **Check:** `loom check` in a shell, or `loom coverage --format
   json` for the structured coverage payload, once a fresh manifest
   exists.

## Expression items

On the surfaces whose items name an output column, an item may
carry an `expression` instead of a `source`: a non-empty SQL
string carried and rendered exactly as written. Loom never
evaluates it and never checks the identifiers it names, so the
expression is practitioner-asserted. The grammar is uniform:

- `expression` is mutually exclusive with `source` on an item,
  and `alias` is required wherever `expression` appears: the
  alias is the item's output column name.
- Jinja delimiters and statement terminators in the expression
  are validation errors - an expression is plain SQL. Templated
  SQL belongs to the macro slot, which combines only with
  `source` and exists only on staging columns.
- The surfaces: staging `columns` (`{expression, alias}` beside `{source, alias, ...}`); a view's `select_columns` (`{expression, alias}` as an alternative to `{source, alias}`); the silver and obt `columns` name lists, where an item is a plain column name, a `{source, alias}` rename or an `{expression, alias}` item; gold/mart `aggregation.group_by`, where an item is a column name or `{expression, alias}` - rendered `expression AS alias` in the select list and as the expression itself in the GROUP BY; and dimension `attribute_columns` / fact `measure_columns`, where an item is a plain column name, a `{source, alias}` rename or an `{expression, alias}` item. A rename's `source` is existence-checked against the input's output columns like any string item, while an expression item's identifiers are practitioner-asserted and never checked. scd2 `tracked_columns` and satellite `attribute_columns` stay name-only: mapping items on those surfaces are rejected at validation, because they feed change detection and hashdiff computation rather than projection.

A measure under `aggregation.measures` is the aggregate-context
form of the same key: `{column, expression, alias}`, where the
expression aggregates its inputs rather than projecting a row
value.

## Filter parameter

Six generators take a `filter` parameter (ADR 0038): staging, silver, dimension, fact, scd2_dimension, normalized_table. It is a non-empty verbatim SQL predicate string, carried into the generated model's WHERE clause exactly as written: Loom never evaluates it and never checks the identifiers it names, so the predicate is practitioner-asserted like an expression item. Jinja delimiters and statement terminators in the predicate are validation errors - a filter is plain SQL, no templating, no stacked statements. The other twelve generators (bronze, gold, mart, obt, view, hub, link, satellite, pit, bridge, reference_hub, reference_table) reject `filter` as an unknown parameter naming the accepted set. There is no filter on a joined or multi-input generator, no per-input filter, no HAVING form for the aggregating generators, and no join-condition form: a predicate over joined inputs would have to name which input it binds to and how it composes with the join, which is a design of its own.

Placement is fixed per generator, on its single input scan: staging takes a WHERE over the source scan under either sourcing form and either projection shape; dimension, fact and normalized_table take a WHERE over the input scan; silver applies the predicate inside the dedup ranking, so rows are filtered before ROW_NUMBER ranks them and the winner is the best row that passes, never the best row overall with the predicate applied to the outcome (on the pass-through form with no dedup key it is a WHERE over the scan); scd2_dimension puts the predicate on the snapshot's input select, restricting which input rows enter SCD2 processing at all - it is not a filter over the recorded history the dimension views expose, and a row the predicate excludes never opens a history row.

Composition with an incremental block: where a cursor predicate also applies (dimension, fact, normalized_table, and silver's affected-key restriction), the two compose as a single WHERE with the filter first and the incremental predicate ANDed behind it inside its `is_incremental()` gate, so a full refresh applies the filter alone and an incremental run applies both. Silver's affected-key window subquery still names the touched keys over the unfiltered input, deliberately: a key whose newest rows fall out of the filter must still be recomputed over its surviving history, which a filtered window would never name. An instance without `filter` generates exactly the SQL it generated before the parameter existed, and a filter names no output column, so properties, downstream validation and unit tests see the same columns as without one.

Changing a filter between loads is a population change, with the generators' ordinary semantics: on silver and the key-strategy incrementals a key whose rows all fall out of the filter behaves like a key that left the input (its stale winner survives incremental runs until a full refresh, the documented delete-blindness escape), and on scd2_dimension a key that stops passing reads to the snapshot as a disappearance, closing under the snapshot's hard-delete handling. The filter parameter section of `references/metadata-reference.md` states the same surface in full.

## Adding a test to a generated model

Generic (data) tests are declared in the instance's `generic_tests`
and generate into the model's properties document. Get the
declaration from Loom - never invent the test name yourself; Loom
never does either:

- `loom create-new data-test <model> --test <name> [--column <col>]`
  prints the `generic_tests` declaration (bare or `package.name`
  test; omit `--column` for a model-level test). A test whose
  arguments Loom cannot know is printed with the `kwargs:
  {__TODO__: …}` sentinel, which validation rejects - fill the real
  arguments as a proposal for ratification; an unfinished
  declaration can never validate silently.
- `loom create-new unit-test <model>` prints a unit-test declaration
  the same way (given rows carry the `__TODO__` sentinel until
  filled from the model's actual inputs).

Remember the one refused combination: `generic_tests` on a model
that declares `skip_properties_document` - carriage needs the
properties document the opt-out suppresses (`create-new` warns about
exactly this on such a model and prints anyway; the pair itself is
for the practitioner to resolve, per the properties-document
decision in `loom-adoption` step 6). For a hand-written model,
`create-new` prints the dbt-YAML form instead of the metadata form.

Know what the generator already emits before declaring anything, because each generated model's tests document carries the generator's own tests: staging, bronze, view and obt emit none; silver emits `not_null` and `unique` on its dedup key; gold and mart emit `not_null` on every measure alias; hub emits `unique` and `not_null` on its hash key, and reference_hub the same plus `not_null` on its business key; link emits `unique` and `not_null` on its hash key and a `relationships` test to each hub on every endpoint foreign key; satellite emits `not_null` on its parent key and on the generated system columns it writes; pit adds `not_null` on `__snapshot_date` and bridge on `__weighting_factor`; fact emits `not_null` on each dimension key; dimension emits `not_null` and `unique` on its natural key and `unique` on the surrogate key; scd2_dimension emits `not_null` on the business key and a uniqueness test over current rows; normalized_table emits `not_null` on every entity column and `unique` on the first; reference_table emits `unique` and `not_null` on its key column.

Redeclaring one of those tests is not an error, but it never produces a second test: dbt names a test from its name, column and arguments, and rejects two entries carrying one name at parse. When a declaration matches an emitted test on all three, generation emits the test once. A `config` on the declaration (for example `severity: warn`) is merged onto the emitted entry, so a redeclaration is the way to change an emitted test's config, and `loom check` warns that the declaration duplicates an emitted test either way. A declaration whose arguments differ is a different test to dbt and is carried in full beside the emitted one.

Gold is where the redeclaration remedy bites most often. A multi-input gold model aggregates with LEFT JOINs, so a measure over a joined input (a `sum`, say) is NULL for a driving row that matches nothing in the joined input, while a `count` of a joined column is 0 - and the `not_null` gold emits on every measure alias then fails at build time, rather than the unmatched row being dropped silently. When those NULLs are correct for the model, redeclare the measure's test with a `config` (for example `severity: warn`) as above. Never edit the generated tests document to soften it: generation rewrites generated files from the metadata, so the edit is lost on the next run and the failure returns.

One argument is never declarable: `model`. dbt injects the model argument itself and rejects any declaration that supplies it (Test arguments include "model", which is a reserved argument), so validation refuses a `model` key in `kwargs`, whether it sits directly in the mapping or inside its `arguments:` wrapper. Declarations copied from a dbt manifest carry `model` (and `column_name`) as injected values; drop them when writing the metadata form.

## Habits that keep the loop clean

- One model per change where practical; a shared pattern change
  re-renders every instance using it - preview shows the full blast
  radius before anything is written, so read it.
- `description` and `owner` are the practitioner's facts on every new
  instance. Propose them; never silently invent them.
- After generate --write, a `git diff` of the generated tree is the
  review artefact: the metadata change and its rendered consequence,
  side by side.
