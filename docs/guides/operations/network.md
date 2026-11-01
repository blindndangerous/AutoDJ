# Connect browsers and configure access

Serve on your LAN with native pairing, or delegate browser access to an authenticating reverse proxy. Use the advanced overrides only for names automatic detection cannot discover.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Local network access

Architecture and verification context: [Proposed access-boundary ADR](../../adr/0007-separate-browser-worker-and-listener-access.md).

Start the native server for other devices on your network with one switch:

```bash
uv run autodj serve --lan
```

`--lan` (or `[server] lan = true`, or `AUTODJ_LAN=1`) binds `0.0.0.0` unless `--host` or
`[server] host` names a specific non-loopback address. It allows the Host names and origins of
this machine that it can detect: the hostname, `<hostname>.local`, the fully qualified name, every
non-loopback address, and `localhost`, `127.0.0.1` and `::1`. With TLS files set
(`[server] ssl_certfile` and `ssl_keyfile`, or `--ssl-certfile` and `--ssl-keyfile`) it allows
the matching `https://` origins too. It uses a configured
`access_token` or `AUTODJ_ACCESS_TOKEN` when there is one; otherwise it loads or creates
`<index_dir>/.access-token`. Only you can read that file: on Windows it gets an access list granting your account alone, not the index folder's permissions. Deleting `.access-token` makes the next start create a new
token, which ends every paired session; each browser must pair again. Startup prints the addresses
to open and, while no browser is paired, a pairing code with how long it stays valid; it never
prints the token. After that no code is printed or usable until someone asks for one:
`uv run autodj devices pairing-code` prints a code that pairs one browser, and it also works with
the saved token and says so on stderr.

Giving a network address with `--host` (or `[server] host`) does the same at that address, so
`uv run autodj serve --host 192.168.1.20 --port 9000` serves on 192.168.1.20 port 9000 with
pairing. Without `--host`, `autodj serve` listens only on this machine (`127.0.0.1:8080`). A
network host keeps its old manual behaviour when you also give allowlists, an access token, or
`--insecure-lan` (see [Advanced overrides](#advanced-overrides)).

`uv run autodj doctor` shows the detected hosts and whether the token will be created. Doctor sees
LAN mode only from `[server] lan` or `AUTODJ_LAN`, not from a `--lan` given only to
`autodj serve`. Name lookups are given two seconds; on a machine with broken DNS, detection keeps
the hostname, the `.local` name and the route address.

```bash
uv run autodj serve --lan --ssl-certfile radio.pem --ssl-keyfile radio-key.pem
```

`--lan --insecure-lan` keeps the detected allowlists but turns pairing off; use it only on a
trusted network. `--insecure-lan` without `--lan` still needs explicit allowlists.

## HTTPS on your home network

On the AutoDJ host, open `http://127.0.0.1:8080` (use your configured port),
then **Settings → Global → Server → Secure connections (HTTPS)**.
Choose **Add certificate** to create a certificate for the listed host names
and addresses, import an existing PEM certificate and private key, or choose
**Get a certificate from Let's Encrypt** for a domain hosted on Cloudflare.
The built-in option offers an explicit staging test before production issuance.
Follow [Get and renew a certificate in AutoDJ](domain-https.md#get-and-renew-a-certificate-in-autodj)
for its DNS token, domain, terms, and hostname checks.
Activate the certificate you want AutoDJ to serve. The first switch from HTTP
to HTTPS requires restarting AutoDJ; subsequent activations take effect for new
connections. The previous certificate stays in the table until you delete it.
After an automatic Let's Encrypt renewal, AutoDJ retains the active pair and the
pair it replaced. Older pairs from that same renewal lineage are removed.
Manual certificates and other lineages are not removed by renewal.
When AutoDJ was started by its launcher or a supported supervisor, **Restart now**
uses the normal restart confirmation and reopens setup over HTTPS once the browser
can connect securely. Install the public certificate's trust before restarting.

Choose **Set up a remote device** to save the public certificate and follow
the trust instructions for the recording device. Transfer that public copy
from the host through a trusted connection, then open the HTTPS address on
the recording device. Once connected, go to **Voice tracks** and allow microphone
access. Each browser profile keeps its own AutoDJ pairing.

For Let's Encrypt, open the certificate's domain over HTTPS on the recording
device. Its browser verifies the certificate without installing a public copy
as a trusted root. A public certificate cannot cover `localhost` or a LAN IP.
The certificate step explains the domain handoff before activation and restart.

The table shows each certificate's exact expiry and approximate remaining time.
The active certificate cannot be deleted. **How to remove trust on this device**
explains how to remove an installed public certificate through the local browser
or operating system's settings.

Let's Encrypt failures and paused renewal appear beside the certificate.
`uv run autodj doctor` reads the saved failure without contacting the certificate
authority or changing DNS. Only an active Let's Encrypt lineage renews
automatically. Choosing another certificate pauses renewal for the saved lineage.
See [Renewal and DNS cleanup](domain-https.md#renewal-and-dns-cleanup) for recovery.

Managed pairs and their selection live under `<index_dir>/.tls` with owner-only
permissions. Keep a separate protected copy if you need to recover the keys;
AutoDJ backups omit this folder. A saved active selection overrides TOML TLS paths;
explicit `serve --ssl-certfile` and `--ssl-keyfile` flags override it for that run.
Existing externally configured files remain where you put them and can continue
to be renewed by your certificate tool. To return to TOML configuration, stop
AutoDJ and move `.tls/inventory.json` to a protected backup location before restarting.

For a headless host, use a trusted terminal/admin connection and the procedure
below. Certificate changes in the web page require direct localhost access.

A browser trusts a certificate only when the certificate names the address in the address bar
and the device trusts whoever signed it. Without a domain of your own, make a self-signed
certificate and install it as trusted on each device that opens AutoDJ. With a domain,
[HTTPS with your own domain](domain-https.md#https-with-your-own-domain) gets a certificate that browsers
already trust.

List every name and address you open AutoDJ by in `subjectAltName`; startup of
`autodj serve --lan` prints them, and `localhost` is the address it gives for this machine. This
example is for a machine called `nas` at `192.168.1.20`:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -subj "/CN=nas" \
  -addext "subjectAltName=DNS:nas,DNS:nas.local,DNS:localhost,IP:192.168.1.20" \
  -keyout autodj-key.pem -out autodj.pem
```

On Windows, Git for Windows includes OpenSSL as `C:\Program Files\Git\usr\bin\openssl.exe`; in
PowerShell, put the command on one line without the trailing backslashes. Keep
`autodj-key.pem` where only the account that runs AutoDJ can read it, and point `[server]` at
both files, for example in `config.local.toml`:

```toml
[server]
lan = true
ssl_certfile = "/srv/autodj/tls/autodj.pem"
ssl_keyfile = "/srv/autodj/tls/autodj-key.pem"
```

Startup then prints `https://` addresses, and LAN mode allows their `https://` origins. Copy
`autodj.pem`, never the key, to each device and install it as a trusted certificate; each
operating system, and Firefox, has its own place for that. Until a device trusts it, its browser
shows a certificate warning. The certificate above expires after 365 days; before then, make a
new one, replace the two files, and install the new certificate on each device. AutoDJ picks up
the replaced files within five minutes.

## Paired devices

In native mode, the web page's Settings, Global, Server lists every paired device with when it was paired and
when it was last seen, and marks this browser. Each row's **Options** menu contains **Rename** and **Revoke access**. Names are 1 to 64
printable characters. **Show pairing code** shows a code with a Copy button and how long it stays
valid; the code changes by itself every five minutes and cannot be edited. Each code pairs one
device. While the code is shown the page checks the list every five seconds; when a device pairs
it says the device's name and hides the used code. Any paired device can show a code, so any
paired device can pair another one.

From the command line, `autodj devices list` prints each device's id, `autodj devices rename
DEVICE_ID NAME` renames one, `autodj devices revoke DEVICE_ID` revokes one, `autodj devices reset`
revokes them all, and `autodj devices pairing-code` prints the current code.

## Advanced overrides

`--lan` covers names this machine knows about. For a DNS name it cannot detect, such as a name
in your router's DNS or a CNAME, add it with `--allowed-host` and `--allowed-origin` (or
`[server] allowed_hosts` and `allowed_origins`); with `--lan` these merge with the detected
lists, and without it they replace them. These flags and `--access-token` are hidden from
`autodj serve --help`. In Docker, put the names in `config.toml`.

To start a private LAN server with TLS for a custom name without `--lan`, set
`AUTODJ_ACCESS_TOKEN` (or `[server] access_token` in `config.local.toml`) and pass the host and
origin as flags:

```bash
uv run autodj serve --host 0.0.0.0 \
  --allowed-host radio.local \
  --allowed-origin https://radio.local:8080 \
  --ssl-certfile radio.pem \
  --ssl-keyfile radio-key.pem
```

The certificate and key are local files on the AutoDJ server. Do not expose the backend port
to the internet. Remote access requires an authenticating gateway: use the Cloudflare Access
procedure linked from [operations](../../operations.md#https-with-your-own-domain), or configure
the trusted-proxy mode below.

## Single sign-on through a trusted proxy

For a worked example with an existing identity service, follow
[Sign in to Practice with Authentik and Caddy](../../tutorials/sso-authentik-caddy.md).

Use this mode only when an authenticating reverse proxy controls access to the
backend. AutoDJ has no provider SDK, OAuth client, group policy or passkey-type
classification. The proxy authenticates and authorizes each new control request
and WebSocket connection. Configure the SSO provider's application access there.

1. Keep the backend reachable only on loopback, or a private network on the same
   host; use TLS between hosts. A non-loopback bind without AutoDJ TLS logs one
   startup warning because the proxy secret could cross the network in cleartext.
   Terminate browser HTTPS at the proxy. Configure a random shared secret of at
   least 32 printable ASCII bytes without spaces. AutoDJ refuses to start without
   it, including on loopback. Store it in an owner-only file outside station
   directories and backups, using the existing `autodj.secret_files.write_private_file`
   helper, or supply `AUTODJ_TRUSTED_PROXY_SECRET` through the service environment.
   `server.trusted_proxy_secret_file` or `AUTODJ_TRUSTED_PROXY_SECRET_FILE` selects
   the file. Direct secrets and secret files are mutually exclusive, including
   across TOML and environment settings; remove the old source when switching.
   Startup rejects group/other permission bits on POSIX. On Windows, the file's
   access list must grant access only to the account running AutoDJ.
   Keep secrets out of shared TOML, logs and version control.
2. Remove **all client-supplied** `X-AutoDJ-User-ID`, `X-AutoDJ-User-Name` and
   `X-AutoDJ-Proxy-Auth`
   headers before authentication. After successful authentication, set
   `X-AutoDJ-User-ID` to one stable, nonempty, opaque identifier and optionally set
   `X-AutoDJ-User-Name` to a display name. Overwrite rather than append identity
   headers. Send exactly one `X-AutoDJ-Proxy-Auth` containing the shared secret
   only on authorized requests to AutoDJ. AutoDJ compares it in constant time;
   direct requests with identity headers alone, a wrong secret or duplicate secret
   headers remain unauthenticated. Never use a mutable name or email address as
   the stable identifier. Use TLS between hosts: the secret authenticates the proxy
   but does not encrypt the connection.
3. Set the server policy in `config.toml`, replacing the example domain:

   ```toml
   [server]
   host = "127.0.0.1"
   auth_mode = "trusted_proxy"
   # Supply AUTODJ_TRUSTED_PROXY_SECRET or select an owner-only secret file:
   # trusted_proxy_secret_file = "/srv/autodj/private/proxy-secret"
   allowed_hosts = ["radio.example.org", "127.0.0.1", "localhost"]
   allowed_origins = ["https://radio.example.org"]
   # Optional: the provider's operator-approved logout endpoint.
   # sso_logout_url = "https://login.example.org/logout"
   ```

   Keep exact Host and Origin allowlists, body limits and machine-token rules.
   Configure trusted forwarding addresses for your deployment; identity headers
   are a separate contract from forwarded client-address/protocol headers.
   Restart AutoDJ after changing access mode. Do not use `--insecure-lan` to
   approximate SSO or expose a native pairing fallback through this instance.
4. Sign in through the public proxy URL. A browser is registered automatically;
   no code or mandatory naming prompt is shown. Its initial label uses the
   display name (or stable ID when no name is supplied). Rename it to something
   useful such as “Studio laptop” in Settings, Global, Server.

Each SSO user sees, renames and forgets only their own browser records. Choose **Forget** from the browser row's **Options** menu. A separate
SSO browser cookie identifies the record; it grants no access without the verified
identity and proxy-secret headers. Each user keeps at most 20 browser records;
creating another evicts the least recently seen record. Changing accounts creates
a separate record. Forgetting a browser
is housekeeping, not access revocation: an authorized browser can register again.
Revoke access at the SSO provider. SSO records and native paired devices are stored
separately; switching back to native mode does not turn SSO records into paired
devices, and native pairing/revocation behavior remains available there.

When `sso_logout_url` is configured, **Sign out through SSO** follows that URL.
Its scope is provider-defined and may sign the person out of other applications.
Clearing an AutoDJ browser record alone does not end the SSO session. Existing
control and microphone WebSockets expire after at most 900 seconds, or the shorter
`session_ttl_seconds`. They close with 4401 and reconnect through the proxy for a
fresh authorization check. Provider sign-out or revocation is not immediate on
existing sockets. Browser records do not provide a
per-action user audit. Passkey type is not inferred from browser identity.

Before publishing, verify that anonymous controls redirect or fail at the proxy,
client-supplied identity or proxy-secret headers cannot bypass it, and backend
control requests without the secret fail even with a forged user ID or old native
cookie. Test account switching,
ownership of browser records, and control WebSocket authentication. Check any
explicit public listener exceptions separately: listener pages and streams remain
anonymous only where configured, and never authorize control or browser management.
For provider migration, plan stable-ID mapping; an ID from a new provider does not
automatically identify the same person.
