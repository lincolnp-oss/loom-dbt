# Support

Loom is maintained on a best-effort basis. There is no support SLA and no response-time promise.

## Before opening an issue

- Work through the [command reference](docs/commands.md) - most questions are answered by the per-command prerequisites and the common-failure notes.
- Check the [architecture document](docs/architecture.md) for current behaviour, deliberate limits, and the compatibility/evidence table, and the [migration playbook](docs/migration-playbook.md) if you are bringing an existing project onto Loom.
- Make sure your dbt manifest is current - produce or refresh it with `dbt parse` - several Loom commands read `target/manifest.json` and will report a stale manifest (and how to refresh it) rather than guess. Commands that inspect built artefacts additionally need those built (`dbt seed` first if your project uses seeds, then `dbt build`).

## Where to ask

- **Reproducible bugs** - open a GitHub issue with the bug-report template.
- **Feature requests** - open a GitHub issue with the feature-request template; lead with the practitioner problem, not a proposed design.
- **Everything else** - open a GitHub issue and say it is a question.

## Supported surface

- The current release only; older pre-release checkouts are not supported.
- Python and dbt/adapter ranges exactly as stated in the README's compatibility section and the architecture document's compatibility-and-evidence table. DuckDB is the only executed and supported dialect; the other seven are generation-only (parse-checked, not executed).

## Hygiene

Never post credentials, private relation names, data rows, or local paths in a public report. Reduce logs and manifests to a minimal synthetic example before sharing them.
