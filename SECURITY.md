# Security Policy

## Supported versions

Only the latest tagged release on `main` receives security updates.
Pre-1.0 versions move fast — pin in your project if you need stability.

| Version  | Supported          |
|----------|--------------------|
| 0.12.x   | :white_check_mark: |
| < 0.12   | :x:                |

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

Out of scope:

- Third-party dependencies — please report those upstream.
- Self-inflicted issues from running `autodj serve --host 0.0.0.0` on
  an untrusted network without a reverse proxy.  The web server has no
  built-in authentication; only bind to a trusted LAN.
