# Security Policy

## Supported versions

Only the latest tagged release on `master` receives security updates. The current supported line is
`0.19.x`. Versions before 0.19 no longer receive security fixes.

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
- AutoDJ serving TLS itself (`[server] ssl_certfile` and `ssl_keyfile`, or `--ssl-certfile` and
  `--ssl-keyfile`) on a private LAN, including reloading renewed certificate files.
- Remote access through a Cloudflare Tunnel that connects to AutoDJ over HTTPS (checking its
  certificate with `originServerName`) with Cloudflare Access in front, as described in
  [docs/operations.md](docs/operations.md#https-with-your-own-domain). This is the only
  supported way to reach AutoDJ from the internet.

Out of scope:

- Vulnerabilities entirely within third-party dependencies. Report those upstream, but tell us if
  AutoDJ makes affected behavior reachable.
- Any Internet exposure without Cloudflare Access in front: port forwarding, a public bind, or a
  tunnel or proxy without Access. AutoDJ has token-based pairing for a home network, not
  multi-user authorization or an Internet-facing identity system, so never expose it that way.
- A proxy that ends TLS and forwards to AutoDJ over plain HTTP. AutoDJ's `Secure` cookie flag and
  origin checks follow its own TLS setting, so AutoDJ must terminate TLS itself.
- Anyone who has passed Cloudflare Access and paired a browser; they are trusted like a paired
  browser on the LAN.
- A paired browser showing a pairing code: any paired device can ask for one and pair one more
  device, by design.
- Attacks that already control filesystem roots trusted through local operating-system ACLs.
  For example, a local process or share user who can write to the liner folder, index folder or
  music library can already change what AutoDJ reads and writes there; AutoDJ does not race-proof
  its file operations against that user.

Operational security boundaries and recovery procedures are documented in
[THREAT_MODEL.md](THREAT_MODEL.md) and [docs/operations.md](docs/operations.md).
