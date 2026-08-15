# Security Policy

## Supported versions

Only the latest tagged release on `main` receives security updates. The current supported line is
`0.15.x`. Versions before 0.15 no longer receive security fixes.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security reports.

Instead, use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Fill in what you found, how to reproduce, and the impact.

You can expect:

- An acknowledgement within 7 days.
- A fix or status update within 30 days for confirmed reports.
- Credit in the release notes if you'd like to be named (anonymous
  reports are also welcome).

## Scope

In scope:

- The CLI (`autodj` and its subcommands).
- The web UI (`autodj serve`).
- The background job runner (`autodj.jobs`).
- Backup archive creation and restore validation.
- Container build and runtime configuration, plus release artifacts.

Out of scope:

- Vulnerabilities entirely within third-party dependencies. Report those upstream, but tell us if
  AutoDJ makes affected behavior reachable.
- Public Internet hosting, including end-to-end TLS deployments. AutoDJ has token-based LAN
  authentication, not multi-user authorization or an Internet-facing identity system.
- Attacks that already control filesystem roots trusted through local operating-system ACLs.

Operational security boundaries and recovery procedures are documented in
[THREAT_MODEL.md](THREAT_MODEL.md) and [docs/operations.md](docs/operations.md).
