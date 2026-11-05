# ADR 0023: Distinguish Loom authoring from dbt resources

Status: Accepted

## Context

The public-facing framework must be understandable without its development history. `kind` previously described both document categories and generation behavior; "catalog", "schema", "group" and "contract" overlapped with established dbt meanings. A glossary alone would leave users translating documentation back into contradictory metadata, commands and diagnostic messages.

## Decision

Use the canonical vocabulary and interface map in [Loom terminology](../terminology.md). A generator implements behavior; a pattern configures one generator; an instance supplies concrete parameters; an instance set groups authoring inputs. Model registrations describe hand-written models. The model inventory combines expanded instances and registrations without becoming a dbt catalog artifact or another service.

Separate document type, generator selection and descriptive classification. Qualify JSON Schema versus warehouse schema, and generator guarantees versus dbt model contracts. Reserve dbt resource names for the actual dbt resources. Loom produces ordinary dbt files and remains outside the dbt runtime.

Apply the names together across metadata, code, CLI/JSON, generated provenance, examples and documentation. This unpublished greenfield project does not retain old formats, commands, imports or aliases. The vocabulary has since been completed by the final nomenclature pass that retired the last development-era names - the command surface is flat verbs (`create-new`, `coverage`, `snapshot-sources`, `diff-sources` among them), the adoption artefacts live under `local/drafts/` and the playbook is the adoption playbook; retired forms error with a pointer to the current form and are never aliases.

Pre-release, the decision records themselves are consolidated in place to their final state rather than preserved with signposting: the public repository starts from a single commit, so supersession chains would be archaeology without an audience, and the development history lives in the private archive. Each ADR therefore states its complete current decision, this one included. Current validation is implemented in Python; no JSON Schemas are packaged.

## Consequences

Consumers must update authoring metadata explicitly and regenerate. Existing source baselines must be recaptured. Generated metadata fingerprints and comments change, as do Loom-owned JSON fields and diagnostics. Those differences are intentional; SQL logic, generated dbt resource names, dbt configuration, warehouse identities, key hashing and history semantics must not change.

The nomenclature change does not introduce plugins, automatic migration, another type hierarchy, runtime services or new modeling features. Licensing and publication remain separate owner decisions.
