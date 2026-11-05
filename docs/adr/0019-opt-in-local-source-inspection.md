# ADR 0019: Explicit read-only DuckDB source inspection

Status: accepted

## Context

Catalog validation can compare staging mappings with declared native-source columns, but it cannot detect the physical columns when source YAML omits them. Authored dbt source YAML is likewise not proof of the physical table shape. Live lookup during routine authoring would introduce a connection requirement and could read credentials or remote data.

## Decision

`loom inspect-sources` is an explicit, read-only command for staging-referenced tables in an existing *project-local* DuckDB file. It requires a current dbt DuckDB manifest to identify the resolved database, schema and table for both native and inline sources; it does not guess dbt's schema naming rules. `duckdb.connect(..., read_only=True)` and parameterized catalog queries retrieve column name, type and ordinal without reading table rows. Result JSON also lists generated staging models that map each raw column. Database and manifest paths must resolve to existing files inside the project. Missing relations and stale/invalid manifests are explicit errors, not empty results. DuckDB is an optional CLI dependency for this command only; normal validation and generation remain offline. Manifest-backed inventory coverage is read-only and does not connect to the warehouse or check generated-file drift.

Inspection alone is this record's whole scope: no baselines, schema-difference severity or automatic mapping changes. Showing what changed since an earlier review is a separate, explicit act with its own baseline - `loom snapshot-sources` / `loom diff-sources` (ADR 0020); catalog schema does not imply whether an unmapped new physical column should become a generated output.
