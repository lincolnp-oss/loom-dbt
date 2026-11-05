# ADR 0036: Bridge tables - declared paths over the current link graph

Status: Accepted

## Context

Bridge tables pre-join a hub to the hubs it reaches through links, so relationship traversal (hierarchies, multi-valued dimensions) becomes a flat scan instead of a join chain at query time. The construct's scope is three elements: a declared hub/link path, a snapshot cadence, and optional weighting. One build can serve Kimball multi-valued-dimension bridging as well.

One scoping element meets a codebase fact: **Loom links retain no load history** (a link is the current relationship key set, rebuilt each run; links carry no load timestamp). A snapshot-cadence bridge would have nothing historical to snapshot. The temporal story for relationships already exists - effectivity windows (ADR 0028) for when a relationship held, PIT tables (ADR 0035) for satellite resolution - so the bridge's honest scope is the current graph.

## Decision

A new generator, `bridge`:

- **Declared path.** The anchor hub is declared in the `parent` block, `{model}` naming it. `path` is an ordered list of `{link, hub}` steps: each step's link must connect the hub the path has reached so far to the step's hub, and every other endpoint of that link must already be reached, so binary and n-ary links both traverse, a step never revisits a hub, and the traversal is fully validated against the link records. The bridge row carries the anchor hash then each reached hub's hash in path order.
- **Current-state flattening.** The bridge is a dbt `table` rebuilt each run: the anchor hub inner-joined through each link to each reached hub (a hub with no relationships on the path has no bridge rows), stamped with the build's `__loaded_at`.
- **Optional weighting.** `weighting: equal` adds a `__weighting_factor` column, `1.0 / COUNT(*) OVER (PARTITION BY <anchor hash>)` - the standard equal weighting that keeps measures aggregated through a many-to-many bridge from double-counting (the Kimball bridge use). It is a closed string enum, like the family's other mode selectors, so further weighting schemes have somewhere to land.
- Name prefix `bridge_` is structural, as with the family's other generators. Data tests: the anchor hash `not_null` (and `__weighting_factor` `not_null` when weighting is declared).

The anchor was originally declared as a `parent_model` parameter; the `parent` block replaced it, as for satellites and PIT tables, so every construct that hangs off a parent declares that parent the same way. The path semantics and the generated SQL are unchanged.

## Consequences

- The Data Vault family is complete: hubs, links (n-ary, dependent-child, non-historised), satellites (five variants), reference constructs, PIT tables, and bridges.
- Historical bridges remain composable, not generated: a relationship's lifespan is its effectivity windows, and a point-in-time path is a windows join away. If links ever gain load retention, snapshot bridges become a variant of this generator rather than a new one.
