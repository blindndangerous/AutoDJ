# Connect devices on a private network

Serve on your LAN, set up HTTPS and pair browsers. Use the advanced overrides only for names automatic detection cannot discover.

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

In the web page, Settings, Profiles and access lists every paired device with when it was paired and
when it was last seen, and marks this browser. Each row has Rename and Revoke. Names are 1 to 64
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

The certificate and key are local files on the AutoDJ server. Never publish the AutoDJ port to
the internet. The only supported way to reach AutoDJ from outside your network is the Cloudflare
Tunnel with Cloudflare Access described next.
