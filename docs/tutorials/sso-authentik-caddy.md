# Sign in to Practice with Authentik and Caddy

You will put the Practice station behind an Authentik sign-in, open AutoDJ
without a pairing code, and check that only an authorized account can enter.
Caddy handles HTTPS and sends AutoDJ the identity Authentik verified.

## Before you begin

Use an unused AutoDJ installation with no listeners. You can reuse Practice
from [Build your first station](first-station.md); music and model analysis
are unnecessary for these access checks. Keep playback stopped. Access mode
applies to every station on this AutoDJ server.

This example assumes Bash on Linux, a source checkout with dependencies
installed, and an AutoDJ version supporting `server.auth_mode`. Run AutoDJ
commands from that checkout. You also need:

- An existing Authentik installation, administrator access, and a working
  HTTPS sign-in page, represented here by `https://auth.example.org`.
- Two ordinary Authentik test accounts: one you will allow and one you will
  leave outside the application binding. Neither needs administrator status.
- Caddy installed directly on the AutoDJ machine, with its existing HTTPS
  configuration working. This lesson does not install either service.
- A domain you control for AutoDJ, represented by `radio.example.org`, with
  DNS pointing to Caddy and certificate issuance working for that domain.
- Authentik's embedded proxy outpost reachable by Caddy at
  `127.0.0.1:9000`. If using containers, publish that endpoint only on
  loopback. A container's own `127.0.0.1` is a different network namespace;
  the Caddy configuration below assumes Caddy runs on the host.

Replace the example domains everywhere, including Authentik's provider and
AutoDJ's allowlists. The loopback addresses and ports below are example
defaults, not addresses to publish. AutoDJ requires a proxy shared secret before
accepting identity headers. Direct requests without that secret are rejected,
including requests from other local processes. Keep the backend off the network
and protect the secret files and both services' environments.

The lesson changes AutoDJ configuration, adds one Caddy site, and creates an
Authentik application/provider and access binding. Save copies of the files
you will edit and record the original AutoDJ startup command. Keep a local
terminal and Authentik administrator session open for recovery. To stop
early, stop the practice AutoDJ process; leave Authentik and other Caddy
sites running. Recovery steps appear at the end.

This example requires sign-in for all AutoDJ routes, including listener
pages, streams, workers and external integrations. Use it on the practice
installation before deciding how a production gateway should admit those
clients. It creates no anonymous listener exceptions.

## Give one account access in Authentik

An Authentik **proxy provider** connects an application to an authentication
check. Its **outpost** answers those checks. We use single-application
forward auth so this station has its own access bindings.

1. In Authentik's Admin interface, open **Applications**, **Applications**,
   then **New Application**. Name it `AutoDJ Practice` and use the slug
   `autodj-practice`.
2. Choose **Proxy Provider**. Name the provider `AutoDJ Practice`, select
   your installation's authorization flow, choose **Forward auth (single
   application)**, and set **External host** to `https://radio.example.org`.
   Create the application/provider pair. Leave unauthenticated path
   exceptions empty.
3. Open the application's **Policy / Group / User Bindings** tab. Use
   **Create or bind...**, then **Bind a user** under **Bind Existing...**
   to bind the allowed test account. Save with **Create**. Keep just this
   one enabled access binding for the exercise.
4. Open **Applications**, **Outposts**. Edit the embedded outpost, select
   `AutoDJ Practice` under **Applications**, and save with **Update**.

Check that the application shows the provider and allowed-user binding, and
that the outpost includes it. Authentik's
[provider setup](https://docs.goauthentik.io/add-secure-apps/providers/proxy/create-proxy-provider/)
describes the workflow; labels can differ between releases. Its
[application binding guide](https://docs.goauthentik.io/add-secure-apps/applications/manage_apps/)
explains that an application with no bindings admits all users. Removing
the last binding is therefore not a way to deny everyone.

Authentik decides who may enter; AutoDJ does not assign per-user roles.
Every admitted user gets server-wide browser control. The owner restriction
on saved browser names does not restrict station operations.

## Set AutoDJ's access mode

Stop the practice AutoDJ process with Ctrl+C and wait for its terminal
prompt. Generate a secret without printing it, using AutoDJ's owner-only atomic
writer. These files stay outside station state and backups; `local/` is ignored
by Git. Run once, preserving the same value for both services:

```bash
uv run --frozen --no-sync python - <<'PY'
import secrets
from pathlib import Path
from autodj.secret_files import write_private_file

secret = secrets.token_urlsafe(32)
private = Path("local/sso-practice")
write_private_file(private / "proxy-secret", secret + "\n")
write_private_file(private / "caddy.env", "AUTODJ_TRUSTED_PROXY_SECRET=" + secret + "\n")
PY
```

Check that both files exist without displaying their contents. Do not copy the
secret into shared configuration, logs or issues. In the `config.toml` used for
Practice, merge these settings into
the existing `[server]` section. Keep other sections and any unrelated
server settings. Do not create a second `[server]` table.

```toml
[server]
host = "127.0.0.1"
port = 8080
auth_mode = "trusted_proxy"
trusted_proxy_secret_file = "local/sso-practice/proxy-secret"
allowed_hosts = ["radio.example.org", "127.0.0.1", "localhost"]
allowed_origins = ["https://radio.example.org"]
trusted_proxies = ["127.0.0.1"]
sso_logout_url = "/outpost.goauthentik.io/sign_out"
```

Check the adjacent `config.local.toml` for overrides. Remove any direct
`trusted_proxy_secret` and unset `AUTODJ_TRUSTED_PROXY_SECRET` in AutoDJ's service
environment when using the secret file. Configuring both sources is an error.
The Caddy service still needs its own environment variable as described below.
AutoDJ verifies private file permissions: POSIX group/other bits are rejected,
and Windows access lists must grant access only to the account running AutoDJ.
For this example,
AutoDJ serves plain HTTP on loopback and Caddy supplies browser HTTPS;
remove any effective `ssl_certfile` and `ssl_keyfile` settings for this
practice backend. Do not start with `--lan`, `--insecure-lan`, or address
overrides. Preserve existing native tokens for rollback; trusted-proxy mode
does not accept them as browser identity.

Start AutoDJ in the foreground, using the actual configuration filename if
different:

```bash
uv run --frozen --no-sync autodj --config config.toml serve
```

In another terminal on the AutoDJ machine, run:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/status
```

Expect `401`: direct requests lack proxy proof and an asserted identity. A
connection error means you must check the server terminal and effective port. A `200` means
this is not the intended trusted-proxy configuration; check overrides and
the process listening on port 8080 before proceeding. Repeat with a forged
identity; the response must remain `401` because no proxy secret was supplied:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'X-AutoDJ-User-ID: forged-user' http://127.0.0.1:8080/api/status
```

`trusted_proxies` controls forwarded scheme and client-address handling; it
is not an allowlist of peers permitted to assert user identity. The shared
secret proves the proxy's authority. See the
[provider-neutral guide](../guides/operations/network.md#single-sign-on-through-a-trusted-proxy).

## Connect Caddy to the outpost and AutoDJ

Give the Caddy service the secret through its environment. On a systemd
installation, run `sudo systemctl edit caddy` and add this service override,
replacing `/path/to/checkout` with the absolute checkout path:

```ini
[Service]
EnvironmentFile=/path/to/checkout/local/sso-practice/caddy.env
```

The service manager reads this owner-only file before starting Caddy. Keep the
generated files private; do not widen permissions to make them readable by other
users. AutoDJ reads `proxy-secret` as its own account. For another service manager,
provide the same `AUTODJ_TRUSTED_PROXY_SECRET` privately to Caddy's process.

Add this site to your existing Caddyfile, or replace only the existing site
for the practice domain. Keep the Authentik site and unrelated sites intact.
Use the path actually loaded by your Caddy service; `/etc/caddy/Caddyfile`
is the example here.

```caddyfile
radio.example.org {
    route {
        request_header -X-AutoDJ-User-ID
        request_header -X-AutoDJ-User-Name
        request_header -X-AutoDJ-Proxy-Auth

        reverse_proxy /outpost.goauthentik.io/* 127.0.0.1:9000

        forward_auth 127.0.0.1:9000 {
            uri /outpost.goauthentik.io/auth/caddy
            copy_headers X-Authentik-Uid>X-AutoDJ-User-ID X-Authentik-Name>X-AutoDJ-User-Name
        }

        reverse_proxy 127.0.0.1:8080 {
            header_up X-AutoDJ-Proxy-Auth {env.AUTODJ_TRUSTED_PROXY_SECRET}
        }
    }
}
```

The `route` preserves this sequence: strip client assertions, route outpost
callbacks, check access, then proxy to AutoDJ. A successful check supplies
the user ID and display name; only the final AutoDJ upstream gets the secret.
The environment placeholder keeps the secret out of the Caddyfile. The ID comes
from Authentik's UID rather than its mutable username or email address. Preserve
the header spelling shown.
This adapts [Authentik's Caddy template](https://docs.goauthentik.io/add-secure-apps/providers/proxy/server_caddy/)
using Caddy's documented
[header renaming](https://caddyserver.com/docs/caddyfile/directives/forward_auth).
The upstream secret uses Caddy's [header manipulation](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#headers)
and [environment placeholders](https://caddyserver.com/docs/extending-caddy/placeholders).
Do not enable request-header debug logging or publish environment dumps.

Validate the edited file before reloading. On a Linux service installation
where these paths and commands apply:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Continue only if it reports that the configuration is valid. Correct parse
or provisioning errors before applying it. The new service environment requires
a restart, briefly interrupting all Caddy sites. Apply it during a maintenance
window, then use reloads for later Caddyfile-only edits:

```bash
sudo systemctl daemon-reload
sudo systemctl restart caddy
```

These commands affect the reader's deployment; validation alone does not
verify Authentik login. See [Caddy's command reference](https://caddyserver.com/docs/command-line)
for installations managed another way.

## Sign in and name your browser

1. In a fresh browser profile or private session, open
   `https://radio.example.org`. Expect Authentik sign-in, then authenticate
   as the allowed test account. An existing Authentik session may skip the
   password prompt.
2. Expect AutoDJ to open without a pairing-code dialog. Open **Settings**,
   **Global**, **Server**. **Browser access** should show `Signed in as`
   followed by the account's name, and a **Your browsers** list. There
   should be no **Show pairing code** control.
3. Use that browser row's **Rename** control, enter `Practice laptop` in
   **Device name**, select **Save**, then refresh the page. The name should
   persist.
4. Open `https://radio.example.org/api/auth/status` in another tab of the
   same browser profile. Its JSON should contain `"mode":"trusted_proxy"`
   and `"authenticated":true` (spacing may differ). Return to AutoDJ.

If Caddy returns `502`, check that AutoDJ and the outpost are listening at
the configured loopback ports. A callback `404` or login loop calls for
checking the provider's external URL, outpost assignment and callback route.
If AutoDJ reports that SSO identity could not be verified, check both copied
headers, matching proxy-secret configuration and provider assignment; do not insert a fixed identity to bypass
the error. A rejected Host or Origin calls for correcting the exact domain
and scheme in AutoDJ's allowlists, rather than widening them to wildcards.

## Check the access boundary

First, try a request with forged identity headers and no login cookies:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'X-AutoDJ-User-ID: forged-user' \
  -H 'X-AutoDJ-User-Name: Forged User' \
  -H 'X-AutoDJ-Proxy-Auth: forged-proof' \
  https://radio.example.org/api/status
```

Expect an Authentik redirect or denial, not `200`. Do not add `-L` or a
browser cookie: those would change what this check measures. An allowed
response requires investigating the gateway before relying on this setup.

In a separate browser profile, sign in as the unbound test account and
open the same site. Expect an access denial. If it opens AutoDJ, review the
application's enabled bindings and the provider mode. Hiding the application
from a dashboard does not revoke access.

In the allowed account's AutoDJ tab, inspect the browser's Network tools
while reloading. The control WebSocket at `/ws` or a named station's `/ws`
route should connect (normally HTTP `101`). Without login cookies, the same
upgrade request must be redirected or denied by the gateway. For the
default station, a terminal check is below. Replace `NONCE`
with a fresh value from `openssl rand -base64 16`; it is a nonce, not a secret:

```bash
curl --http1.1 -sS -D - -o /dev/null --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: NONCE' \
  -H 'Origin: https://radio.example.org' \
  https://radio.example.org/ws
```

Expect a redirect or denial, never `101`. This checks the unauthenticated
handshake; it does not establish immediate revocation of existing sockets.
The public `/player` URL also requires sign-in in this worked example.
AutoDJ's own public-route policy still applies behind Caddy; making a route
anonymous at the gateway is a separate deployment change.

## Try forgetting a browser, then sign out

Predict what happens if you **Forget** `Practice laptop`: will that stop
the account controlling AutoDJ?

Choose **Forget** from that row's **Options** menu and read the confirmation before accepting.
Reload AutoDJ. It should still open and register a browser again, initially
using the account's display name. Forgetting removes a saved browser record;
Authentik still authorizes the account. Access revocation belongs in
Authentik, not the browser inventory.

Now use **Sign out through SSO** and confirm. AutoDJ should leave for the
outpost's sign-out page. Authentik documents that
[provider logout](https://docs.goauthentik.io/add-secure-apps/providers/proxy/#log-out)
can invalidate the user's sessions across that outpost. Your Authentik login
session may still allow a later sign-in without another password. Test that
behavior before promising users a particular logout scope. Already-open
AutoDJ control and microphone sockets in other tabs may stay connected for at
most 900 seconds, or the shorter `session_ttl_seconds`. They then close with
4401 and reconnect through the proxy, which checks authorization again. Provider
session policy still determines whether that fresh handshake is admitted.

For an ownership exercise, explicitly bind the second test account too,
sign in from its separate profile, and open **Your browsers**. Each account
should see only its own records; both have full station control. Restore
the original single-user binding afterward. Do not interpret a binding
change as proof of immediate session or socket revocation; check fresh
sessions as well as your provider's session-expiry behavior.

## Stop and keep or remove your work

To pause, stop only the practice AutoDJ process with Ctrl+C and check that
its terminal prompt returns. Its Caddy URL should then fail to reach the
backend. Authentik, Caddy and your saved station remain; browser records and
the application/provider binding also remain.

To return to the original practice setup:

1. Keep AutoDJ stopped. Restore its saved configuration, including any
   adjacent `config.local.toml` changes. Confirm the effective mode is
   `native` and the bind/TLS settings match your original command.
2. Restore just the practice site's saved Caddy block, or remove the block
   if the lesson added it. Validate the Caddyfile and reload as above.
   Keep the SSO proxy block disconnected from a native backend.
3. Start AutoDJ with the original command and verify its original local
   access or pairing behavior. SSO browser records do not become native
   paired-device permissions.
4. If retiring the exercise, unassign `AutoDJ Practice` from the outpost,
   then remove only its application and provider. Keep shared Authentik
   accounts, flows and other applications.
5. Remove only the Caddy service override added for this exercise and its two
   generated files under `local/sso-practice/` after neither service needs them.
   Reload systemd and restart Caddy to remove the secret from its environment.

If you are locked out, perform these recovery steps from the retained local
terminal and administrator session. Do not expose the backend publicly to
recover access. Your Practice library and station settings remain available.

## What you can do next

Use the [trusted-proxy guide](../guides/operations/network.md#single-sign-on-through-a-trusted-proxy)
for the provider-neutral contract and deployment checks. Consult the
[public-player guide](../guides/operations/public-player.md) before designing
anonymous listener routes; neither a broad `/api/*` bypass nor an exception
for control WebSockets is appropriate. Review machine-client access
separately before using this gateway with workers or live sources.

AutoDJ and this page were developed with AI assistance.
