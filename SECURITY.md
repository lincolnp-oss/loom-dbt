# Security Policy

## Supported versions

Security fixes are applied to the latest release in the current minor series. Older series are not patched; upgrade to the latest release to receive a fix. Before the 1.0 release, only the most recent 0.x release is supported.

## Reporting a vulnerability

Please report vulnerabilities **privately**. Do not open a public issue, pull request, or discussion for a suspected vulnerability.

Preferred: use GitHub's private vulnerability reporting - the “Report a vulnerability” button under this repository's **Security** tab.

Alternatively, email **lincolnp.projects@gmail.com** with “Loom security” in the subject line.

Please include: the Loom version (`loom --version`), your Python and dbt versions if relevant, a minimal reproduction (a small Loom metadata file, manifest, or command line is ideal), and the impact as you understand it.

## What to expect

Loom is maintained by one person. You can expect: acknowledgement within 7 days; an initial assessment within 14 days of acknowledgement; and coordinated disclosure - a fix and a release first, public details after, with credit to the reporter unless they prefer otherwise. If a report has not been acknowledged in 14 days, please re-send it; silence means it was not received, not that it is being ignored.

## Scope

In scope: the `loom` CLI as published on PyPI - its handling of Loom metadata files, dbt manifests, and source-structure files (all of which may come from a project you did not write), the files it generates, and its refusal to overwrite files it does not own.

Out of scope: vulnerabilities in dbt Core, dbt adapters, or your warehouse; SQL you wrote yourself in hand-authored models or macros; and issues that require an attacker to already have write access to your dbt project (Loom metadata is code-adjacent: anyone who can edit your project's metadata can already edit its models directly).

## Loom collects nothing

Loom has no telemetry, analytics, crash reporting, update checks, or any other phone-home behaviour. See the no-telemetry statement in the README; any future change to that position would be a documented, opt-in feature, never a silent addition.
