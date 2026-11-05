# Metadata reference

Loom projects are authored in three document kinds, recognised by where the file lives, never by a type field inside it: **pattern definitions** in `metadata/patterns/`, **instance sets** in `metadata/instances/`, and **registrations** in `local/registrations/` (drafts under `local/drafts/` use the same shapes). This page is the format reference for all three. The source of truth is the generator contract table in `loom.contracts`, which `loom validate` enforces: an unknown parameter is an error that names the accepted set, a missing required parameter is an error, and a wrong top-level type is an error. Two fields on an expanded instance are system-set and never authored: its `generator` (taken from the pattern it references) and its `_paradigm` (taken from the generator registry). For design rationale see the [architecture](architecture.md); for command behaviour, the [command reference](commands.md); for adopting an existing project, the [adoption playbook](adoption-playbook.md).

The three examples below are complete files. The pattern definition and the instance set are **composites**: each gathers the full surface of its kind into one document, grouped and commented, so the whole format can be read in one place. A composite is a reference artefact, not a runnable document - it does not pass `loom validate`, because no single generator admits every parameter it shows. Every value in them is live YAML in its real form; anything that has no live form in that document is described in comment prose instead. The registration example is an ordinary complete document of its kind.

## The canonical order

Every parameter in this reference is presented in one grouped order, by function: **identity and common**, **sourcing**, **inputs**, **structure**, **keys**, **columns and content**, **behaviour**, **tests**, **envelope**. The pattern definition's defaults, the instance set's params and the mapping table's rows all follow it identically, and the grouping is visible in the examples as comment banners. Learn the order once and you can infer where in any document a given parameter lives.

## Example 1: a pattern definition (composite)

A pattern definition lives at `metadata/patterns/<name>.yml`. It names exactly one `generator` - one of `staging`, `bronze`, `silver`, `gold`, `hub`, `link`, `satellite`, `pit`, `bridge`, `reference_hub`, `reference_table`, `scd2_dimension`, `fact`, `dimension`, `obt`, `normalized_table`, `mart`, `view` - declares the `required_params` its instances must supply, and may carry `defaults`: values every instance of the pattern inherits. An instance supplies the rest, and the union is checked against the generator's contract. One rule governs the split: a parameter that appears in `defaults` may not also appear in an instance's `params` - that overlap is a validation error - **except** the three envelope keys (`output_subpath`, `contract_enforced`, `skip_properties_document`), which may be defaulted and then overridden per instance, the instance's value winning.

This composite definition is anchored on `hub` for its envelope lines; its `required_params` gathers every paradigm's required set and its `defaults` gathers the full defaultable surface, so both must be read with their comments, not as one generator's definition.

```yaml
# metadata/patterns/composite_pattern.yml - composite reference artefact, not a runnable document.
generator: hub # a real definition names exactly one of the 18 generators

# The union of every generator's required parameters, in canonical order.
# A real definition lists only its own generator's set (see the table after this example).
required_params:
  # Identity and common - required by the nine generators that carry prose:
  # staging, hub, link, satellite, pit, bridge, reference_hub, reference_table, scd2_dimension.
  - description
  - owner
  # Sourcing - bronze requires its source declared inline.
  - inline_source
  # Inputs - input_models is required by 13 generators; the rest are one paradigm each or two.
  - input_models
  - endpoints # link
  - parent # satellite, pit, bridge
  - reference_hub # reference_table
  - satellites # pit, reference_table
  # Structure
  - path # bridge
  - joins # obt
  - aggregation # mart
  # Keys
  - business_key # hub, reference_hub, reference_table, scd2_dimension
  - natural_key # dimension
  - surrogate_key # dimension
  - dedup_key # silver
  - dimension_keys # fact
  # Columns and content
  - columns # staging, obt
  - select_columns # view
  - attribute_columns # dimension, normalized_table, reference_table
  - entity_columns # normalized_table
  - measure_columns # fact
  - tracked_columns # scd2_dimension
  - record_source # satellite, reference_hub

# The full defaultable surface, in canonical order. Values are schematic.
defaults:
  # Identity and common (all 18 generators)
  description: TODO - one sentence on what this model is
  owner: analytics
  tags: [vault]
  column_docs: {customer_id: The customer business key}
  column_types: {customer_id: VARCHAR}
  # Sourcing - staging requires exactly one of source_ref / inline_source (neither or both is a validation error); bronze requires inline_source
  source_ref: {name: ecom, table: raw_customers}
  inline_source: {name: ecom, schema: raw, table: raw_customers, loaded_at_field: loaded_at, freshness: {warn_after: {count: 12, period: hour}, error_after: {count: 24, period: hour}}}
  # Inputs
  input_models: [stg_customers] # exactly one entry for link, satellite, reference_hub, dimension, fact, scd2_dimension, silver, normalized_table, mart, obt; one or more for hub, gold, view
  endpoints: # link - at least two {hub, key} mappings, in declared order
    - {hub: hub_customer, key: customer_id}
    - {hub: hub_order, key: order_id}
  parent: {model: hub_customer, key: customer_id} # satellite, pit - hub-parent form; the link-parent form is shown in Example 2
  reference_hub: ref_hub_country # reference_table
  satellites: [sat_customer_details] # reference_table takes exactly one entry; pit takes the set it resolves
  # Structure
  variant: standard # satellite and link; reference_table takes latest | full | snapshot, scd2_dimension type_2 | type_3 | type_6, fact transaction | periodic_snapshot | accumulating_snapshot
  path: [hub_customer, hub_order] # bridge - hub instance names, anchor first
  weighting: line_count # bridge - the weighting column for unequal paths
  joins: # gold, obt, view - on names the join key columns
    - {model: stg_orders, on: [customer_id]}
  aggregation: # gold, mart
    group_by: [customer_id]
    measures:
      - {column: order_id, expression: "COUNT(order_id)", alias: order_count}
  # Keys
  business_key: customer_id # hub, reference_hub, reference_table, scd2_dimension
  natural_key: customer_id # dimension
  surrogate_key: customer_sk # dimension
  dedup_key: customer_id # silver
  dedup_order: loaded_at # silver - the dedup winner is the latest row by this column
  dimension_keys: [customer_id] # fact
  dependent_child_key: line_no # link - part of the link grain when declared
  driving_key: valid_from # satellite, effectivity variant
  subsequence_key: address_seq # satellite, multi_active variant
  period_key: order_month # fact, periodic_snapshot variant
  milestone_keys: [paid_at, shipped_at] # fact, accumulating_snapshot variant
  # Columns and content
  columns: # staging form - {source, alias} with the optional macro slot; on obt and silver this is a plain list of column names
    - {source: id, alias: customer_id}
    - {source: created_at, alias: created_date, macro: dbt.date_trunc, macro_args: [day], macro_column_param: 2}
  select_columns: # view - source is a model.column reference
    - {source: n_customers.customer_id, alias: customer_id}
  attribute_columns: [customer_name, email] # satellite, dimension, reference_table, normalized_table
  entity_columns: [customer_id, customer_name] # normalized_table
  payload_columns: [quantity, unit_price] # link, non_historised variant
  measure_columns: [quantity, total_amount] # fact
  tracked_columns: [customer_name, email] # scd2_dimension; also read descriptively on registrations
  previous_value_columns: [email] # scd2_dimension, type_3 variant
  record_source: crm # satellite, reference_hub - a plain label; on hub, this or the mapping form in Example 2
  # Behaviour
  retention: persistent # hub - or rebuilt; provenance (record_source) requires persistent
  incremental: {strategy: merge, unique_key: customer_id, on_schema_change: fail} # bronze, silver, gold, dimension, fact, link, normalized_table - strategy append | delete+insert | merge; conditional keys: cursor_column, lookback with lookback_unit, full_refresh
  filter: "status = 'active'" # staging, silver, dimension, fact, scd2_dimension, normalized_table - a verbatim SQL predicate over the input scan (see the filter parameter)
  snapshot_timestamp: "2026-01-31T00:00:00Z" # reference_table, snapshot variant
  # Tests - no live default exists: generic_tests and unit_tests are instance-only and a
  # pattern that defaults either is rejected, so they appear in Example 2 only.
  # Envelope - the three keys an instance may override when defaulted (see the rule above)
  output_subpath: customers # a relative path template - the placeholders {name}, {generator}, {paradigm}, {owner} and {tags} expand from the instance's metadata
  contract_enforced: false
  skip_properties_document: false
```

### Required parameters by generator

Generated mechanically from `loom.contracts`; the mapping table further down marks the same facts per parameter.

- `bridge`: description, owner, parent, path
- `bronze`: inline_source
- `dimension`: input_models, natural_key, surrogate_key, attribute_columns
- `fact`: input_models, dimension_keys, measure_columns
- `gold`: input_models
- `hub`: description, owner, input_models, business_key
- `link`: description, owner, input_models, endpoints
- `mart`: input_models, aggregation
- `normalized_table`: input_models, entity_columns, attribute_columns
- `obt`: input_models, columns, joins
- `pit`: description, owner, parent, satellites
- `reference_hub`: description, owner, input_models, business_key, record_source
- `reference_table`: description, owner, reference_hub, satellites, business_key, attribute_columns
- `satellite`: description, owner, parent, input_models, record_source
- `scd2_dimension`: description, owner, input_models, business_key, tracked_columns
- `silver`: input_models, dedup_key
- `staging`: description, owner, columns
- `view`: input_models, select_columns

## Example 2: an instance set (composite)

An instance set lives at `metadata/instances/<name>.yml`. Its first line names the `pattern` it applies; each entry under `instances` gives a `name` (the generated model's name) and its `params`. One obligation the composite necessarily shows violated and a real instance must honour: a staging instance declares exactly one of `source_ref` and `inline_source` - neither, or both, is a validation error. The instance below is the composite: one instance carrying every parameter as live YAML in canonical order. Lines that exclude each other on a real instance (the two sourcing modes, the several `variant` value sets, the string and mapping forms of `record_source`) are all shown, each with its applicability comment; that is what makes the document a reference artefact rather than a runnable one. A second, small instance follows it, carrying the one structural form the composite's single `parent` slot cannot also hold: a satellite whose parent is a link.

```yaml
# metadata/instances/composite_instances.yml - composite reference artefact, not a runnable document.
pattern: composite_pattern
instances:
  - name: composite_model
    params:
      # Identity and common (all 18 generators)
      description: Composite instance carrying every parameter in the format
      owner: analytics
      tags: [vault, composite]
      column_docs: {customer_id: The customer business key}
      column_types: {customer_id: VARCHAR}
      # Sourcing (staging, bronze) - staging requires exactly one of the two, as an obligation; bronze takes inline_source only
      source_ref: {name: ecom, table: raw_customers}
      inline_source: {name: ecom, schema: raw, table: raw_customers, loaded_at_field: loaded_at, freshness: {warn_after: {count: 12, period: hour}, error_after: {count: 24, period: hour}}}
      # Inputs
      input_models: [stg_customers, stg_customers_web] # cardinality per generator is stated with the pattern example and in the table
      endpoints: # link - every endpoint in declared order; the link hash composes them in this order
        - {hub: hub_customer, key: customer_id}
        - {hub: hub_order, key: order_id}
        - {hub: hub_product, key: product_id}
      parent: {model: hub_customer, key: customer_id} # satellite, pit - hub-parent form, keyed by the parent's kind; link-parent form in the second instance below
      reference_hub: ref_hub_country # reference_table
      satellites: [sat_customer_details] # reference_table - exactly one entry; pit - the satellites the table resolves
      # Structure
      variant: standard # satellite, link; other value sets per generator are stated with the pattern example and in the table
      path: [hub_customer, hub_order] # bridge
      weighting: line_count # bridge
      joins: # gold, obt, view
        - {model: stg_orders, on: [customer_id]}
      aggregation: # gold, mart
        group_by: [customer_id]
        measures:
          - {column: order_id, expression: "COUNT(order_id)", alias: order_count}
      # Keys
      business_key: customer_id # hub, reference_hub, reference_table, scd2_dimension
      natural_key: customer_id # dimension
      surrogate_key: customer_sk # dimension
      dedup_key: customer_id # silver
      dedup_order: loaded_at # silver
      dimension_keys: [customer_id] # fact
      dependent_child_key: line_no # link
      driving_key: valid_from # satellite, effectivity variant - one of the parent link's endpoint keys
      subsequence_key: address_seq # satellite, multi_active variant
      period_key: order_month # fact, periodic_snapshot variant
      milestone_keys: [paid_at, shipped_at] # fact, accumulating_snapshot variant
      # Columns and content
      columns: # staging form; on obt and silver a plain list of column names
        - {source: id, alias: customer_id}
        - {source: name, alias: customer_name}
        - {source: created_at, alias: created_date, macro: dbt.date_trunc, macro_args: [day], macro_column_param: 2}
      select_columns: # view
        - {source: n_customers.customer_id, alias: customer_id}
        - {source: n_orders.order_id, alias: order_id}
      attribute_columns: [customer_name, email] # satellite, dimension, reference_table, normalized_table
      entity_columns: [customer_id, customer_name] # normalized_table
      payload_columns: [quantity, unit_price] # link, non_historised variant
      measure_columns: [quantity, total_amount] # fact
      tracked_columns: [customer_name, email] # scd2_dimension
      previous_value_columns: [email] # scd2_dimension, type_3 variant
      record_source: {stg_customers: crm, stg_customers_web: web} # hub mapping form - one label per input model, all-or-nothing; a plain string labels every input uniformly, and is the only form on satellite and reference_hub
      # Behaviour
      retention: persistent # hub - required by record_source provenance
      incremental: {strategy: merge, unique_key: customer_id, on_schema_change: fail} # bronze, silver, gold, dimension, fact, link (standard variant), normalized_table - conditional keys: cursor_column, lookback with lookback_unit, full_refresh
      filter: "status = 'active'" # staging, silver, dimension, fact, scd2_dimension, normalized_table - a verbatim SQL predicate over the input scan (see the filter parameter)
      snapshot_timestamp: "2026-01-31T00:00:00Z" # reference_table, snapshot variant
      # Tests
      generic_tests: # all 18 generators - dbt generic test declarations, carried verbatim onto the generated model
        - {test: not_null, column_name: customer_id}
        - {test: unique, column_name: customer_id}
        - {test: relationships, column_name: customer_id, to: "ref('hub_customer')", field: customer_id}
      unit_tests: # 17 of 18 generators - barred on scd2_dimension only, because dbt unit tests target models and its model under test is a snapshot.
        # Each item is {name, description, given, expect, overrides}; given entries are {input, format, rows} and
        # expect is {format, rows}, with format: csv the non-default form. overrides carries dbt overrides:
        # overrides.macros.is_incremental is required per test on a persistent hub.
        - name: latest_customer_wins
          given:
            - input: stg_customers # the fed model: an inventory model name (rendered as a ref call). For a source-fed model
              # (staging, bronze) declare the bare source table name instead - validation requires it to name the
              # model's declared source table, and the emitter renders the source('name', 'table') call itself.
              rows:
                - {customer_id: 1, customer_name: Ana}
          expect:
            rows:
              - {customer_id: 1, customer_name: Ana}
      # Envelope - may override a pattern default; the instance's value wins
      output_subpath: customers # a relative path template - the placeholders {name}, {generator}, {paradigm}, {owner} and {tags} expand from the instance's metadata
      contract_enforced: false
      skip_properties_document: false
  # The link-parent form of the parent block: the endpoints list mirrors the parent
  # link's own endpoints, in declared order, and dependent_child_key mirrors its key.
  - name: sat_order_line_effectivity
    params:
      description: Effectivity satellite over the order link
      owner: analytics
      parent:
        model: link_order_customer
        endpoints:
          - {hub: hub_customer, key: customer_id}
          - {hub: hub_order, key: order_id}
        dependent_child_key: line_no
      input_models: [stg_order_lines]
      record_source: pos
      variant: effectivity
      driving_key: customer_id # one of the parent link's endpoint keys
      attribute_columns: [quantity, unit_price]
```

## Example 3: a registration

A registration lives at `local/registrations/<name>.yml` and describes a hand-written model so inventory, coverage and search account for it. It generates nothing, and registering a model withdraws it from `loom convert`'s drafting pool (deleting the registration returns it). Unlike the composites above, this example is an ordinary complete document: every field a registration may carry is shown, and it validates as written.

```yaml
# local/registrations/legacy_customer_export.yml - a complete registration.
name: legacy_customer_export
classification: mart # descriptive; hub, link and scd2_dimension are reserved for generated instances and rejected here
description: Hand-written customer export maintained outside Loom
owner: analytics
keys: [customer_id] # the model's key columns
depends_on: [hub_customer, sat_customer_details] # the inventory models it reads
business_key: customer_id # descriptive - read by reporting and output-column derivation
tracked_columns: [customer_name, email] # descriptive - the columns whose history matters
upstream: crm nightly extract # descriptive text, not an executable source declaration
```

## The expression item

One concept, stated once (ADR 0037): on the surfaces whose items name an output column, an item may carry an `expression` instead of a `source`. `expression` is a non-empty verbatim SQL string, carried and rendered exactly as written - Loom never evaluates it and never checks the identifiers it names (practitioner-asserted). It is mutually exclusive with `source`, and `alias` is required wherever it appears: the alias is the item's output column name. Jinja delimiters and statement terminators are validation errors - an expression is plain SQL; templated SQL belongs to the macro slot, which combines only with `source`. The surfaces: staging `columns` (`{expression, alias}` beside `{source, alias, ...}`), view `select_columns` (`{expression, alias}` as an alternative to `{source, alias}`), gold/mart `aggregation.group_by` (a string item, or `{expression, alias}` - rendered `expression AS alias` in the select list and as the expression itself in GROUP BY), the silver/obt `columns` name lists (an item is a string naming a plain column, a `{source, alias}` rename, or an `{expression, alias}` item; these surfaces have no macro slot), and dimension `attribute_columns` / fact `measure_columns` (the same three item forms; scd2 `tracked_columns` and satellite `attribute_columns` stay name-only - they feed change detection and hashdiff computation). The aggregate-context variant of the same key predates the item form: a measure under `aggregation.measures` is `{column, expression, alias}`, an expression that aggregates its inputs rather than projecting a row value. `measures` keeps its name; `expression` is the single shared mechanism key under it.

## The filter parameter

One concept, stated once (ADR 0038): `filter` is a non-empty verbatim SQL predicate string, carried into the generated model's WHERE clause exactly as written - Loom never evaluates it and never checks the identifiers it names (practitioner-asserted, the expression item's posture). Jinja delimiters and statement terminators are validation errors, so a filter is plain SQL only: no templating, no stacked statements. Six generators admit it, each applying it to its single input scan: staging (a WHERE over the source scan, whatever the projection), dimension, fact and normalized_table (a WHERE over the input scan), silver (the predicate sits inside the dedup ranking, so rows are filtered before ROW_NUMBER ranks them and the winner is the best row that passes), and scd2_dimension (the predicate sits on the snapshot's input select, restricting which input rows enter SCD2 processing at all; it is not a filter over the recorded history the dimension views expose). Where an admitted generator also carries an `incremental` block with a cursor, the two compose as a single WHERE: the filter first, the cursor predicate ANDed behind it inside its `is_incremental()` gate, so a full refresh applies the filter alone and an incremental run applies both; silver's affected-key restriction composes the same way behind the filter. An instance without `filter` generates exactly the SQL it generated before the parameter existed.

The predicate surface is deliberately bounded, and the bounds are post-Alpha: there is no `filter` on the joined or multi-input generators (bronze, gold, mart, obt, view, hub, link, satellite, pit, bridge, reference_hub, reference_table), no per-input filters, no HAVING form for the aggregating generators, and no join conditions or range joins - a predicate there would have to name which input it binds to and how it composes with the join, which is a design of its own, not a parameter row.

## Gold joins and measure tests

A gold model with several inputs aggregates with LEFT JOINs: each `joins` entry renders as a LEFT JOIN, so a driving-input row whose join keys match nothing in a joined input survives into the aggregation instead of being dropped. Its measures over the joined input aggregate that null-extended row: a `sum` measure is NULL for the row, while a `count` of a joined column is 0. Gold emits a `not_null` test on every measure alias, so such a model fails that generated test at build time: the unmatched row is reported rather than lost silently. Where those NULLs are correct for the model, the remedy is to declare the test on the instance through `generic_tests` with a `config` (for example `severity: warn`): a declaration that duplicates a generator-emitted test (same test, column and kwargs) is emitted once, with the declaration's config merged onto the emitted entry, so the generated test is softened by declaration rather than by editing it. Do not edit the generated tests document instead: generation rewrites a generated file from the metadata, so a hand edit there does not survive the next generation, while the instance declaration does.

## Post-Alpha bounds

The rest of the vocabulary surface is deliberately bounded the same way, and the bounds are post-Alpha - named here so an absence is never mistaken for an oversight. There is no UNION as an authoring construct: no parameter composes two inputs with a union, and where a generator's own shape needs one (a hub reads its several sources as a union of per-source selects; a pit builds its calendar spine the same way) the union is fixed inside the generator, not declared. There is no UNNEST as authoring vocabulary: no item flattens an array column into rows (convert reads an UNNEST in a hand-written model only as a signal in its fit checks). Join types are fixed, not selectable: `joins` names the model and its key columns only - every entry joins LEFT - and no parameter chooses another type. There is no DISTINCT as authoring vocabulary: no parameter asks a generator to deduplicate its output, and the DISTINCTs in generated SQL are fixed parts of the generators that need them (a hub's per-source key select is one). There is no general window stage: no parameter declares a window function over a partition - the window functions in generated SQL are likewise fixed inside specific generators, silver's dedup ranking above all. An incremental model takes no runtime reprocessing window: its cursor predicate derives from the target's own maximum cursor value, widened only by the fixed `lookback` and with no end bound, and Loom has no `--vars` passthrough by which a bounded window could be supplied at run time, so a bounded historical rerun is a full refresh or a hand-written model, never a variable. And readiness is absent from the inventory surfaces: the search corpus carries no readiness for a model and no search result or detail view reports it, because no inventory source derives one - readiness is the account convert derives over its drafts, and it lives only in draft records and convert reports.

## The parameter mapping table

Generated mechanically from `loom.contracts` at the consolidation head: the generator lists and required marks are read from the contracts, never transcribed, so the table and the validator cannot disagree. Rows follow the canonical order.

| Parameter | Group | Admitted by (`*` = required there) | Form |
|---|---|---|---|
| `description` | Identity and common | bridge*, bronze, dimension, fact, gold, hub*, link*, mart, normalized_table, obt, pit*, reference_hub*, reference_table*, satellite*, scd2_dimension*, silver, staging*, view | string |
| `owner` | Identity and common | bridge*, bronze, dimension, fact, gold, hub*, link*, mart, normalized_table, obt, pit*, reference_hub*, reference_table*, satellite*, scd2_dimension*, silver, staging*, view | string |
| `tags` | Identity and common | bridge, bronze, dimension, fact, gold, hub, link, mart, normalized_table, obt, pit, reference_hub, reference_table, satellite, scd2_dimension, silver, staging, view | list of strings |
| `column_docs` | Identity and common | bridge, bronze, dimension, fact, gold, hub, link, mart, normalized_table, obt, pit, reference_hub, reference_table, satellite, scd2_dimension, silver, staging, view | mapping of column name to description |
| `column_types` | Identity and common | bridge, bronze, dimension, fact, gold, hub, link, mart, normalized_table, obt, pit, reference_hub, reference_table, satellite, scd2_dimension, silver, staging, view | mapping of column name to type |
| `source_ref` | Sourcing | staging | mapping: `{name, table}` - an authored dbt source |
| `inline_source` | Sourcing | bronze*, staging | mapping: `{name, schema, table, loaded_at_field, freshness}` - a source declared in the metadata; `freshness` holds `warn_after` / `error_after`, each `{count, period}` with period `minute` \| `hour` \| `day` |
| `input_models` | Inputs | dimension*, fact*, gold*, hub*, link*, mart*, normalized_table*, obt*, reference_hub*, satellite*, scd2_dimension*, silver*, view* | list of model names - exactly one entry for link, satellite, reference_hub, dimension, fact, scd2_dimension, silver, normalized_table, mart and obt; one or more for hub, gold and view |
| `endpoints` | Inputs | link* | list of `{hub, key}` mappings, at least two, in declared order |
| `parent` | Inputs | bridge*, pit*, satellite* | mapping: `{model, key}` for a hub parent, `{model, endpoints}` (plus `dependent_child_key` when the link declares one) for a link parent, `{model}` for a bridge |
| `reference_hub` | Inputs | reference_table* | string - the reference hub's instance name |
| `satellites` | Inputs | pit*, reference_table* | list of satellite instance names - exactly one entry on reference_table; the resolved set on pit |
| `variant` | Structure | fact, link, reference_table, satellite, scd2_dimension | string - satellite: `standard` \| `non_historised` \| `record_tracking` \| `multi_active` \| `effectivity`; link: `standard` \| `non_historised`; reference_table: `latest` \| `full` \| `snapshot`; scd2_dimension: `type_2` \| `type_3` \| `type_6`; fact: `transaction` \| `periodic_snapshot` \| `accumulating_snapshot` |
| `path` | Structure | bridge* | list of hub instance names, anchor first |
| `weighting` | Structure | bridge | string - the weighting column for unequal bridge paths |
| `joins` | Structure | gold, obt*, view | list of `{model, on}` mappings; `on` is the list of join key columns; the join type is fixed, never a parameter - every entry renders as a LEFT JOIN |
| `aggregation` | Structure | gold, mart* | mapping: `{group_by: [...], measures: [{column, expression, alias}]}`; `group_by` items are column names or `{expression, alias}` items (see the expression item); the mapping is closed, so any key other than `group_by` and `measures` is a validation error, and each measure's keys are closed to `column`, `expression` and `alias` |
| `business_key` | Keys | hub*, reference_hub*, reference_table*, scd2_dimension* | string - a column name |
| `natural_key` | Keys | dimension* | string - a column name |
| `surrogate_key` | Keys | dimension* | string - a column name |
| `dedup_key` | Keys | silver* | string - a column name |
| `dedup_order` | Keys | silver | string - a column name; the dedup winner is the latest row by it |
| `dimension_keys` | Keys | fact* | list of column names |
| `dependent_child_key` | Keys | link | string - a column name |
| `driving_key` | Keys | satellite | string - a column name (effectivity satellites) |
| `subsequence_key` | Keys | satellite | string - a column name (multi-active satellites) |
| `period_key` | Keys | fact | string - a column name (periodic facts) |
| `milestone_keys` | Keys | fact | list of column names (accumulating facts) |
| `columns` | Columns and content | obt*, silver, staging* | staging: list of `{source, alias, macro, macro_args, macro_column_param}` or `{expression, alias}` mappings (see the expression item); obt and silver: list of column names, `{source, alias}` renames, or `{expression, alias}` items (see the expression item) |
| `select_columns` | Columns and content | view* | list of `{source, alias}` or `{expression, alias}` mappings (see the expression item); `source` is a `<model>.<column>` reference |
| `attribute_columns` | Columns and content | dimension*, normalized_table*, reference_table*, satellite | dimension: list of column names, `{source, alias}` renames, or `{expression, alias}` items (see the expression item); the other generators: list of column names |
| `entity_columns` | Columns and content | normalized_table* | list of column names |
| `payload_columns` | Columns and content | link | list of column names |
| `measure_columns` | Columns and content | fact* | list of column names, `{source, alias}` renames, or `{expression, alias}` items (see the expression item) |
| `tracked_columns` | Columns and content | scd2_dimension* | list of column names |
| `previous_value_columns` | Columns and content | scd2_dimension | list of column names |
| `record_source` | Columns and content | hub, reference_hub*, satellite* | hub: a string, or a mapping of input model to label (per-feed provenance); satellite and reference_hub: a string |
| `retention` | Behaviour | hub | string: `persistent` \| `rebuilt` (hub only) |
| `incremental` | Behaviour | bronze, dimension, fact, gold, link, normalized_table, silver | mapping: `{strategy, unique_key, on_schema_change, cursor_column, lookback, lookback_unit, full_refresh}` - strategy (required) `append` \| `delete+insert` \| `merge`; `on_schema_change` `ignore` \| `fail` \| `append_new_columns` \| `sync_all_columns` (default `fail`); `cursor_column` names the incremental cursor; `lookback` (integer) with `lookback_unit` `hour` \| `day` \| `month` \| `year` widens the cursor window; `full_refresh` is a boolean |
| `filter` | Behaviour | dimension, fact, normalized_table, scd2_dimension, silver, staging | string - a SQL predicate carried verbatim into a WHERE over the model's input scan (see the filter parameter); silver applies it before dedup ranking and scd2_dimension on the snapshot's input select; never evaluated or identifier-checked by Loom; Jinja delimiters and statement terminators are validation errors |
| `snapshot_timestamp` | Behaviour | reference_table | string - an ISO-8601 timestamp literal (reference_table snapshot variant) |
| `generic_tests` | Tests | bridge, bronze, dimension, fact, gold, hub, link, mart, normalized_table, obt, pit, reference_hub, reference_table, satellite, scd2_dimension, silver, staging, view | list of dbt generic test declarations, carried verbatim into the generated tests document; a declaration duplicating a generator-emitted test (same test, column and kwargs) is emitted once, with any `config` it carries merged onto the emitted entry, and a `model` key in `kwargs` (or inside its `arguments:` wrapper) is a validation error, because dbt reserves that argument |
| `unit_tests` | Tests | bridge, bronze, dimension, fact, gold, hub, link, mart, normalized_table, obt, pit, reference_hub, reference_table, satellite, silver, staging, view | list of `{name, description, given, expect, overrides}` declarations; each `given` entry is `{input, format, rows}`, `expect` is `{format, rows}` (`format: csv` is the non-default form the renderer honours); `overrides` carries dbt overrides, of which `overrides.macros.is_incremental` is required per test on a persistent hub |
| `output_subpath` | Envelope | all generators (in `defaults` or instance `params`) | string - a relative path template under the generator's artefact root; the placeholders `{name}`, `{generator}`, `{paradigm}`, `{owner}` and `{tags}` (all tags, sorted, one segment each) expand from the instance's metadata, and any other placeholder is a validation error |
| `contract_enforced` | Envelope | all generators (in `defaults` or instance `params`) | boolean - emit the dbt contract block for the model |
| `skip_properties_document` | Envelope | all generators (in `defaults` or instance `params`) | boolean - suppress the generated properties document |

## Where generated artefacts land

Generated model SQL lands under the project's primary model root plus the instance's `output_subpath` when one is declared. `output_subpath` is a template: the placeholders {name}, {generator}, {paradigm}, {owner} and {tags} expand from the instance's own metadata (tags sorted, one segment each), and any other placeholder is a validation error. Three fixed exceptions apply: staging SQL lands at `models/_generated/staging/<instance>.sql`, and every inline source's declaration is emitted to the one fixed location `models/_generated/staging/sources.yml` (bronze included); an scd2_dimension's snapshot SQL lands under the snapshots root, named `<instance>_history` (the instance's own leading `stg_` dropped first). Generated properties documents, data-test documents and unit-test documents land under `models/_generated/tests/` (plus the output subpath when one is declared): unit tests specifically at `models/_generated/tests/unit_<model>.yml`. Reference hubs are always persistent; a `view` and a `reference_table` materialize as views.
