# Loom Showcase - the conformance project

**This is Loom's second project, not its first.** If you are new to Loom, start with the [first project](../first-project): one source, two staging models, one hub, one hand-written model, walked through step by step. This showcase is the conformance fixture that project points to - the proof, exercised end-to-end in CI, that one declared metadata model can generate a project in which several modelling paradigms live and compose in a single dbt project, alongside practitioner-authored dbt. It uses synthetic seeds and builds locally with DuckDB.

The example is layered. The opening layer - staging → Data Vault → SCD2, plus a hand-written model - is the walkthrough below. The sections after it add paradigm compositions (vault core feeding Kimball marts, a parallel medallion reading, a 3NF slice, and a reporting surface). Read them in any order, and stop anywhere: each is an extension of the same generated project, not a prerequisite for the next.

Nothing here is a Loom-endorsed reference architecture. Loom is non-prescriptive: the compositions demonstrate arrangements the metadata *permits*, two different readings of the same sources included, not arrangements Loom recommends.

## What it contains

```text
seeds/                  Synthetic customers, orders and order items
models/sources.yml      One native dbt source grouping the four seed tables,
                        with practitioner-authored generic tests on its columns
metadata/patterns/      Reusable definitions for every generator used
metadata/instances/     The generated models, declared per paradigm
local/registrations/    Inventory entry for the hand-written order totals model
models/fct_order_totals.sql   The hand-written model
models/fct_order_totals.yml   Practitioner-authored tests for it
models/exposures.yml    An exposure declaring the dashboard that consumes it
tests/                  A practitioner-authored singular reconciliation test
```

Not in the tree (it is generated, and git-ignored): `models/_generated/staging/`, the Loom-owned directory `loom generate` writes the staging models into - one per source table, renames and casts only. It is also where `sources.yml` would appear if a source were declared inline in the metadata: that file holds Loom's own generated source declarations. This quickstart's source is authored by hand in `models/sources.yml`, so no `sources.yml` is generated here.

## Prerequisites

Python 3.11–3.14, Loom installed from this repository, and `dbt-duckdb`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install ..\..
python -m pip install "dbt-duckdb>=1.9,<2"
```

On macOS or Linux, activate with `. .venv/bin/activate` and install Loom with `python -m pip install ../..`.

## Validate, generate and build

From this directory, validate the metadata and generate the dbt models:

```sh
loom validate
loom generate --write
```

Validation reports 33 inventory entries: 32 declared instances plus the hand-written order totals model's registration. Generation writes 37 model files from the metadata (several instances produce a companion view or snapshot), catalogue properties for the 32 generated models, and 23 generated test files - 69 generated files in total. The authored source declaration, the hand-written model, its tests and the exposure remain practitioner-owned in `models/`.

Load the seeds first, then build all dbt resources including their data tests. The seed step is required: the staging models read dbt sources backed by the seed tables, and dbt creates no seed-to-model edge through a source, so `dbt build` alone fails on a fresh checkout. The `dbt` commands pass `--profiles-dir .` so they use this example's own `profiles.yml`, wherever your shell's dbt defaults point.

```sh
dbt seed --profiles-dir .
dbt build --profiles-dir .
```

The build runs the generated models, the snapshot, generated data tests (uniqueness, not-null, relationship), the practitioner-authored source and model tests, and the singular reconciliation test in `tests/` (which fails if the hand-written totals ever disagree with the staged order lines). All data is synthetic and the DuckDB database is created locally as `quickstart.duckdb`. The link model joins the hubs on their generated hash keys and carries generated relationship tests to both hubs; built from the seed data it produces 4 rows, one per order. See [links](../../docs/architecture.md#links) for the link contract, including its fail-fast guard: a null, blank or hub-unmatched key in the link's input fails the build instead of being silently dropped.

Run `dbt build` a second time to see the persistent product hub (`hub_product`, `retention: persistent`) do its incremental append: no new products exist, so it inserts nothing and stays at 4 rows. `hub_product` is a multi-source hub (pattern `multi_source_hub`): products arrive from order lines (`record_source: orders`) and from the product catalog (`record_source: catalog`) - the catalog-only Widget D is in the hub even though no order references it. Two further links ride on the vault core: `link_order_customer_product`, a ternary (n-ary) link over the three hubs, and `link_order_item_events`, a non-historised event link whose rows carry the order-line quantity as payload.

Three satellites hang off the vault core (pattern `vault_satellite`, instances in `metadata/instances/satellites.yml`): `sat_customer_details` keeps the customers' descriptive history with hash-diff change detection (plus a `sat_customer_details_current` view of the latest row per customer), `sat_order_customer_details` carries the order-line payload (`product_name`, `quantity`) on the order/customer link, and `sat_customer_tracking` records customer appearances - Loom's record-tracking variant, the vault's deletion story. On a second build with unchanged seeds the satellites append nothing: their `is_incremental()` anti-joins compare each extract against the latest held state. See [satellites](../../docs/architecture.md#satellites) for the variant contracts.

More Loom surface, against the built project:

```sh
loom coverage
loom generate --write --select hub_product
dbt parse --profiles-dir .
loom plan --from fct_order_totals --to fact_orders
```

`loom coverage` reports inventory coverage against the dbt manifest. `generate --select` regenerates just the named model (or a `generator:<name>` set). Regenerating rewrites a model file, which leaves the manifest behind the project: the `dbt parse` step refreshes the manifest (Loom's manifest-backed commands otherwise report the manifest as stale and stop). `plan` reasons about replacing one model with another over the manifest graph - here it shows the `order_totals_dashboard` exposure as an observed consumer of the hand-written model you would be migrating away from.

## Add a model (the recipe)

Every new generated model in a Loom project is a **seed ↔ source ↔ instance trio**: data behind a native source declaration, plus one instance in an instance set. Adding a staging model for a new source table - here, a `suppliers` table - is the whole procedure:

1. **Seed.** Create `seeds/suppliers.csv`:

   ```text
   id,supplier_name
   1,Supplier One
   2,Supplier Two
   ```

2. **Source.** Add the table to the `raw` source in `models/sources.yml`, alongside the existing tables:

   ```yaml
         - name: suppliers
           columns:
             - name: id
               tests:
                 - unique
                 - not_null
             - name: supplier_name
   ```

3. **Instance.** Append one instance to the existing `staging` pattern's instance set, `metadata/instances/staging.yml`:

   ```yaml
     - name: stg_suppliers
       params:
         description: Supplier staging from raw seed data.
         source_ref:
           name: raw
           table: suppliers
         columns:
           - source: id
             alias: supplier_id
           - source: supplier_name
             alias: supplier_name
   ```

   The instance supplies only what the pattern does not: the pattern's `defaults:` already provide `owner`. A field may live in a pattern's `defaults:` **or** in an instance's `params:` - never both; Loom has no override precedence, and a field in both places is a validation error.

4. **Validate, generate, build, find it:**

   ```sh
   loom validate
   loom generate --write --select stg_suppliers
   dbt seed --profiles-dir .
   dbt build --profiles-dir . --select stg_suppliers
   loom search supplier
   ```

   Expected: validation reports 34 inventory entries; one model is generated (plus its catalogue properties); the seed run reports `PASS=5` (the new seed joins the existing four); the selected build passes; search finds `stg_suppliers`. On a fresh, never-generated copy of this project, run a full `loom generate --write` before the `dbt` commands - dbt parses the whole project, and the generated models the hand-written model references must exist.

The hand-written variant is shorter: write the `.sql` under `models/`, add a registration under `local/registrations/` naming its `depends_on` and key, and it joins the same inventory - that is exactly how `fct_order_totals` sits alongside the generated models here. Adding an instance of a *new generator* follows the trio with a new pattern file declaring that generator and its `required_params`.

## Output layout: nested where it helps, flat elsewhere

Generation writes most models flat under `models/`, as dbt projects usually do - but not all of them. The Data Vault patterns (`current_hub`, `current_link`, `vault_satellite`, `slowly_changing`) declare `output_subpath: vault` in their pattern defaults, so the hubs, the link, the satellites and the SCD2 dimension view are generated under `models/vault/`, the SCD2 snapshot under `snapshots/vault/`, and their generated data tests under `models/_generated/tests/vault/`. The Kimball patterns (`kimball_fact`, `kimball_dimension`) likewise declare `output_subpath: kimball`, placing `fact_orders` and `dim_customers` under `models/kimball/` with their data tests under `models/_generated/tests/kimball/`. Generated model properties (catalogue) entries are merged into those same schema documents - one described file per model, as dbt requires. Everything else - staging (under `models/_generated/staging/`, as always), medallion, the 3NF slice, the OBT, the mart and the reporting view - stays flat.

The mix is deliberate. `output_subpath` is layout metadata declared per pattern (it can also be set per instance, and supports placeholders such as `{paradigm}`); Loom imposes no folder taxonomy, so a project nests only where a folder earns its keep. Folders organise files only: dbt model names remain project-unique, so `dim_customers` is still just `dim_customers` to dbt and to every `ref()` - only its file moved.

## Composition: Data Vault core → Kimball marts

The hubs and link do not only feed the SCD2 dimension. `fact_orders` is a factless fact read directly from `link_order_customer` (its dimension keys are the hub hashes), and `dim_customers` is a plain Kimball dimension read from `hub_customer`, carrying the hub hash as an attribute, beside the SCD2 dimension over the same entity. Loom validates the columns a fact or dimension references against the hub's or link's output columns, so cross-paradigm references are checked in metadata, not discovered at build time.

## Composition: a parallel medallion reading

Alongside the vault, the same raw sources are read again as medallion: `bronze_*` loads them exactly as seeded, `silver_*` deduplicates each by its source id, and `gold_customer_orders` aggregates per customer over an explicit join. The join (orders to order lines on `customer_id`) fans out, so its counts use `DISTINCT` and its date uses `MAX` rather than double-counted sums - the instance description records why. Two paradigms interpreting the same inputs differently is the point: Loom permits both in one project and prescribes neither.

## Extension: a 3NF slice and the reporting surface

A third reading of the staged sources: `n_customers` and `n_orders` are normalized tables whose first entity column is the entity key (it carries a generated `unique` test), joined by the `vw_customer_orders` reporting view over explicit join keys. Downstream, `obt_order_lines` is a wide table over the staged order lines enriched with the hubs' keys and hashes, and `mart_customer_orders` aggregates the Kimball fact per customer hash. Join keys in generated OBT SQL are compared as text, so the hubs' text hash keys join cleanly to typed staging keys on strict engines.

## Dialect evidence for this example

**Executed & supported: DuckDB.** This quickstart - all paradigms above - is generated for DuckDB, seeded and built end-to-end in CI, and its rows are asserted (link = 4 rows, persistent hub steady at 4 products across rebuilds, SCD2 lifecycle exercised). Build PASS=120 of 121, one snapshot no-op, NO-OP=1 - verified against this tree 2026-10-26; lifecycle runtime tests run the same project locally.

**Generation only - parse-checked, not executed: PostgreSQL, ClickHouse, Databricks, Snowflake, BigQuery, Redshift, Azure Synapse.** Generated SQL is dialect-flavoured - the files `loom generate` writes serve one target dialect (`--dialect`) - and the parse layer in Loom's test suite parses every generator's SQL under all eight dialects' grammars. That is a syntax guarantee, not execution evidence and not a support claim. Development builds on PostgreSQL, ClickHouse and Databricks before this Alpha are recorded once, as historical validation, in the architecture document's [compatibility and evidence](../../docs/architecture.md#compatibility-and-evidence) table.

## Explore the inventory

Search registered and generated models, then check inventory coverage against the dbt manifest:

```sh
loom search customer
loom search classification:mart
loom check
```

`loom check` verifies model inventory coverage; it does not detect edits to generated files.
