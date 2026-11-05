# ADR 0011: Consumer-aware replacement planning

Status: accepted

`loom plan --project-dir <project> --from <model> --to <model>` produces a read-only consumer inventory from a current dbt manifest, for the act of replacing one model with another. Project-owned models are matched by name and package, and downstream model edges come from `depends_on.nodes`. The output separates direct and transitive dependents, includes manifest-visible exposures, semantic models, metrics and saved queries and other resources reachable through manifest dependency links - metrics and saved queries named among the consumers, as the assessment report names them (ADR 0025) - and always marks external consumer coverage as unknown.

This inventory is not a complete consumer registry: the manifest cannot enumerate every downstream system or consumer outside dbt. A plan recommends building the replacement alongside the existing model, proving invariants, deliberately moving dependent references, and retiring the old model only later. It does not edit models, move references, assert model equivalence, or compare row counts across potentially different grains. Both model names must identify distinct, project-owned models in the manifest. Missing, invalid, or stale manifests prevent planning - the plan runs on the same guarded manifest as the assessment and coverage commands.
