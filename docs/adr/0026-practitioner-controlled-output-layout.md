# ADR 0026: Practitioner-controlled nested output organisation

Status: Accepted

## Context

An earlier assessment of generated outputs concluded: keep the flat per-generator roots, because nested organisation would impose organisational semantics Loom deliberately does not own. The standing decision is the opposite outcome: large projects need nested organisation **derived from practitioner-chosen structure** - names, tags, or other metadata - without a fixed taxonomy, with flat output remaining equally valid. The earlier conclusion's caution (Loom must not impose the taxonomy) is retained as the design constraint.

Two existing mechanisms bound the design:

- Pattern definitions already group instances and carry `defaults` that expand into every instance record. A generator field may not appear in both defaults and instance params (Loom has no override precedence), so inheritance already has exactly one channel.
- Configured output roots are already resolved (`model-paths` / `snapshot-paths`); any subpath mechanism must compose with those roots rather than replace them.

## Decision

Add one optional layout field, **`output_subpath`**, carried in an instance's params or in its pattern's defaults. It is layout metadata consumed by generation, not a generator parameter: it is exempt from the generator contracts (an envelope key), is never passed to renderers, and is meaningless on registrations. As an envelope key it is exempt from the no-duplicate-field rule for generator parameters: it may be defaulted in a pattern and overridden in an instance, the instance winning - the mechanism that expresses "this layout for this pattern, except this one model" (the envelope-key category is ADR 0032's).

The value is a relative path template. Placeholders are limited to values already in Loom metadata:

- `{name}`, `{generator}`, `{paradigm}` - the model's own identity;
- `{owner}` - fails validation at generation time if the instance has no owner (fail explicitly, never invent a fallback folder);
- `{tags}` - all tags, sorted, each becoming one path segment; no tags expands to no segment.

Any other placeholder, an unresolvable placeholder, or a malformed template is a validation/generation error. Literal segments are the practitioner's own structure - team, source system, subject area - which is the point: Loom supplies the mechanism, not the taxonomy.

Path safety is part of the feature. After expansion, every segment must match `[A-Za-z0-9][A-Za-z0-9._-]*`; absolute templates, backslashes, `.`/`..` segments and empty segments are rejected, and the resolved path must stay under the configured root. Two generated outputs resolving to the same file fail the generation plan phase before anything is written.

One resolver (`loom.output_layout`) serves every consumer:

- **Generation** applies the subpath under the configured roots to model SQL, to the SCD2 view (model root) and snapshot (snapshot root), and mirrors it for generated data-test YAML under `_generated/tests/<subpath>/`. The shared inline-source document (`sources.yml`) stays in `_generated/staging/` (one project-level artefact, not a model).
- **`generate --check`** uses the same resolver and additionally reports a Loom-headed SQL file found at a selected model's previous location as stale/moved. Loom still never deletes files; the practitioner removes the stale file explicitly.
- **Search** exposes the expected `output_path` for generated entries (JSON, and verbose text).
- **Coverage / `check`** compare a controlled model's manifest `original_file_path` against the expected path and report a distinct `misplaced_generated_models` category. Registered models are exempt - they have no expected generated path.

With no `output_subpath` anywhere, resolved paths are exactly the current flat paths. dbt model names remain project-unique: folders organise files, they do not create namespaces.

Adoption drafts propose the candidate's current manifest directory as its `output_subpath`, so a conversion need not move a model merely because Loom generated it.

## Alternatives considered

- **A project-level rules engine** (match on generator/tag/owner, first-match wins): more expressive, but a new config format, new precedence questions, and a much larger validation surface for a first release. The template covers the same structures through metadata the practitioner already authors. Revisit only on concrete evidence the template is insufficient.
- **A pattern-definition-level layout field**: duplicates the defaults channel and would need its own precedence rule against params. Rejected; defaults already are the pattern-level channel.

## Consequences

- Existing projects are untouched until they opt in.
- Layout changes are explicit metadata edits; the drift check names both the new missing file and the stale file at the old location.
- The layout field appears in expanded inventory records (alongside `_paradigm`) but never in generated SQL or test YAML content.
