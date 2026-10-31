# Configure domain HTTPS and remote access

Set up the certificate, local DNS and the documented Cloudflare Access boundary before opening remote access.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## HTTPS with your own domain

This setup uses `autodj.example.net` for the domain and `192.168.1.20` for the machine that runs
AutoDJ; use your own. It needs a domain whose DNS is hosted on Cloudflare. The result:

- AutoDJ serves HTTPS itself on the LAN with a free Let's Encrypt certificate for the domain.
- At home, the domain resolves to the LAN address, so traffic stays on your network.
- Away from home, a Cloudflare Tunnel carries requests to AutoDJ over HTTPS, and Cloudflare
  Access asks for a one-time PIN sent to your email before any request reaches AutoDJ.
- Nothing on your network is opened to the internet: the certificate is issued through DNS,
  and the tunnel is an outbound connection.

## Get and renew a certificate in AutoDJ

Use this option when your domain's DNS zone is hosted on Cloudflare and you can
open AutoDJ directly at localhost on its host. It uses DNS-01, so you do not need
to open an inbound Internet port. Other DNS providers, delegated challenge
CNAMEs, headless hosts without local browser access, and externally managed
certificates can use the [certbot procedure](#1-get-the-certificate-with-certbot)
or their existing renewal tool and import path.

1. Create a Cloudflare API token with **Zone:DNS:Edit** and **Zone:Zone:Read**,
   limited to the single zone containing your domain. Copy that zone's ID.
   AutoDJ checks zone access but cannot prove that your token has no wider scope.
2. Configure local DNS so `autodj.example.net` reaches this host from your
   recording devices. Add the name and its HTTPS origin to AutoDJ's existing
   allowlists before activation. For the example port, use:

   ```toml
   [server]
   lan = true
   allowed_hosts = ["autodj.example.net"]
   allowed_origins = ["https://autodj.example.net:8080"]
   ```

   Restart AutoDJ after changing configuration. Keep the localhost browser
   session available for certificate management. The web setup never widens
   these allowlists for you.
3. On the host, open AutoDJ at localhost. Go to **Settings → Global → Server →
   Secure connections (HTTPS)**, choose **Add certificate**, then select
   **Get a certificate from Let's Encrypt** in **Certificate source**.
4. Enter a certificate name, the domain, the zone ID, and the API token.
   Leave **Let's Encrypt staging test — not trusted** selected for the first test.
   Choose **Check domain and terms**.
5. Review the addresses found by the host resolver. If AutoDJ cannot prove they
   belong to this host, check reachability from your devices before checking
   **I have checked that devices on my network reach this host at [domain]**.
   NAT and bridged containers can cause this warning. The warning does not block
   a confirmed name. `localhost`, `.local`, private-only suffixes, IP literals,
   wildcards, and names outside the selected zone are refused.
6. Read the linked directory terms and check **I accept these terms**. The
   checkbox starts unchecked. AutoDJ saves the terms URL and acceptance time
   before registering the account. Choose **Run staging test**.
7. Check that the staging test completes. Its certificate never enters the
   inventory and is never served. Start another setup and choose
   **Let's Encrypt production certificate**. Enter the token again, repeat the
   preflight and consent, then choose **Get production certificate**.
   Production names become public through Certificate Transparency logs.
8. Activate the saved **Let's Encrypt** row. Use its domain over HTTPS; the
   certificate cannot cover localhost. The first HTTP-to-HTTPS change needs a
   restart. **Restart now**, when available, uses the existing confirmation and
   probes the selected domain with normal browser TLS verification before
   navigating. Each recording browser still needs its own AutoDJ pairing.

The existing modal still has two steps: **Certificate** and **Trust**. For a
production Let's Encrypt certificate, the Trust step asks you to open its domain;
do not install the certificate as a trusted root. Continue with
[local DNS](#4-keep-home-traffic-on-the-lan) and, only if needed,
[remote access](#5-reach-it-from-outside-with-a-cloudflare-tunnel-and-cloudflare-access).
The external certbot and file-copy steps below are an alternative to this setup.

### Renewal and DNS cleanup

Keep AutoDJ running for automatic renewal. Only the active Let's Encrypt lineage
renews. AutoDJ uses the authority's renewal information when available; otherwise
it schedules renewal at two thirds of the certificate's actual validity, with
jitter that only advances that date. The schedule survives restart.

Transient failures retry after 15 minutes, 1 hour, 6 hours, then 24 hours, or later
when the provider requests it. Local daily budgets limit repeated failed orders
even after a setup is removed. Failures appear in the row and in read-only
`uv run autodj doctor` output. They retain the working pair. A successful renewal
keeps the new active pair and the pair it replaced, removing only older pairs
from the same lineage. Leave room in the 20-pair inventory for a renewal.

If you choose another certificate while renewal runs, the new pair is Saved.
The row says **Renewal paused: this Let's Encrypt certificate is not active**.
Activate that lineage again to resume its automatic schedule.

DNS cleanup deletes only the TXT record IDs AutoDJ created. It never replaces
the challenge's record set. If cleanup fails, its credentials remain private
on the host for retry. Open the saved setup to inspect its status. If the token
has been revoked or the record has changed permanently, choose **Stop retrying
DNS cleanup** and confirm. AutoDJ logs the record IDs and shows the challenge
name. Remove those records by hand in Cloudflare; leave unrelated TXT records
alone. Releasing cleanup does not delete DNS records for you. You can then remove
the unused setup or correct the problem and retry issuance.

Account keys are separate for staging and production. Account keys and DNS tokens
stay in owner-only files under `<index_dir>/.tls/acme`, never in browser responses,
logs, Doctor output, or AutoDJ backups. AutoDJ removes credentials once no setup,
certificate lineage, or pending DNS cleanup references them, including after
startup recovery. A token is never re-displayed; re-enter it for a new setup.

## 1. Get the certificate with certbot

Certbot's `dns-cloudflare` plugin proves you own the domain by adding a DNS record (the DNS-01
challenge), so no port has to be reachable from the internet. On a Linux machine that stays on
(it can be the AutoDJ machine), install certbot and the plugin, for example
`sudo apt install certbot python3-certbot-dns-cloudflare`.

In the Cloudflare dashboard, create an API token with the "Edit zone DNS" template, limited to
your domain's zone. Save it where only root can read it:

```bash
sudo install -m 600 /dev/null /etc/letsencrypt/cloudflare.ini
echo "dns_cloudflare_api_token = YOUR_API_TOKEN" | sudo tee /etc/letsencrypt/cloudflare.ini
```

Request the certificate:

```bash
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d autodj.example.net
```

Certbot installs a timer that renews the certificate once it has 30 days left, about every 60
days.

## 2. Copy the certificate to the AutoDJ machine on every renewal

If certbot runs on another machine, a deploy hook copies each renewed certificate across. Save
this as `/etc/letsencrypt/renewal-hooks/deploy/autodj.sh` and make it executable with
`sudo chmod 755`. The `autodj` account and the target folder are examples; use the account that
runs AutoDJ and a folder only it can read. The root account on the certbot machine needs an SSH
key that account accepts.

```sh
#!/bin/sh
set -eu
# certbot sets RENEWED_LINEAGE to the renewed certificate's folder.
scp "$RENEWED_LINEAGE/fullchain.pem" "$RENEWED_LINEAGE/privkey.pem" \
  autodj@192.168.1.20:/srv/autodj/tls/
```

Run it once by hand with `RENEWED_LINEAGE=/etc/letsencrypt/live/autodj.example.net` set, to copy
the first certificate. If certbot runs on the AutoDJ machine itself, the hook can `cp` the two
files instead, or `[server]` can point straight at `/etc/letsencrypt/live/autodj.example.net/`
when the AutoDJ account can read it.

AutoDJ checks the two files every five minutes and loads a renewed certificate without a
restart. New connections get it; open ones keep the old certificate until they reconnect. If
AutoDJ finds the files half copied, or a certificate that does not match the key, it logs a
warning, keeps serving the previous certificate, and tries again when the files change. If it
cannot read the files at all (the renewal tool still has them open, or a share is away), it
tries again at the next check.

## 3. Configure AutoDJ

In `config.local.toml` on the AutoDJ machine:

```toml
[server]
lan = true
allowed_hosts = ["autodj.example.net"]
allowed_origins = ["https://autodj.example.net"]
ssl_certfile = "/srv/autodj/tls/fullchain.pem"
ssl_keyfile = "/srv/autodj/tls/privkey.pem"
```

This does the same as `autodj serve --lan --allowed-host autodj.example.net --allowed-origin
https://autodj.example.net` with `--ssl-certfile` and `--ssl-keyfile`; the command-line TLS
options, when given, replace both configured files. `lan = true` also allows
`https://autodj.example.net:8080`, the address used at home, and keeps pairing on.
`https://autodj.example.net` without a port is the address used through the tunnel. AutoDJ
refuses to start when only one of the two files is set or either file is missing.

## 4. Keep home traffic on the LAN

In your router's or local DNS server's settings (for example a Pi-hole local DNS record), add an
`A` record so `autodj.example.net` resolves to `192.168.1.20` inside your network. At home, open
`https://autodj.example.net:8080`: the browser trusts the Let's Encrypt certificate and nothing
leaves the LAN. A device that uses its own DNS, such as a browser with DNS over HTTPS turned on,
skips this record and goes through the tunnel instead.

## 5. Reach it from outside with a Cloudflare Tunnel and Cloudflare Access

Set up Access first, so the hostname is never reachable without it:

1. In Cloudflare Zero Trust, under Access, add a self-hosted application for
   `autodj.example.net`.
2. Give it an Allow policy with an "Emails" rule listing your own address, and only the
   addresses of people you trust with the player. Turn on the "One-time PIN" login method.
3. Note the application's Audience (AUD) tag and your team name.

Then install `cloudflared` on a machine on the LAN and create the tunnel with
`cloudflared tunnel login`, `cloudflared tunnel create autodj` and
`cloudflared tunnel route dns autodj autodj.example.net`. Its `config.yml`:

```yaml
tunnel: TUNNEL_ID
credentials-file: /etc/cloudflared/TUNNEL_ID.json
ingress:
  - hostname: autodj.example.net
    service: https://192.168.1.20:8080
    originRequest:
      # Check AutoDJ's certificate against the domain, not the IP address.
      originServerName: autodj.example.net
      httpHostHeader: autodj.example.net
      # Refuse any request that did not pass Cloudflare Access.
      access:
        required: true
        teamName: YOUR_TEAM_NAME
        audTag:
          - YOUR_AUD_TAG
  - service: http_status:404
```

The tunnel connects to AutoDJ over HTTPS and checks its certificate, so the connection is
encrypted the whole way. Do not use `noTLSVerify` or an `http://` service. A tunnel managed from
the Cloudflare dashboard takes the same settings in the public hostname's TLS, HTTP and Access
options.

Cloudflare Access is required. Never put AutoDJ on the internet without it, whether through this
tunnel, port forwarding or any other proxy: AutoDJ's pairing is built for a home network, not for
strangers on the internet. After the Access login, pair each remote browser as usual. Every
request through the tunnel reaches AutoDJ from the `cloudflared` machine's address, so the
per-address limit on wrong pairing codes counts all remote browsers together unless
`[server] trusted_proxies` includes that direct peer. This setting affects client addresses on all
routes in every mode, including pairing, the listener request limits and audit logs. Set it only
when the proxy appends the address it saw to `X-Forwarded-For`, or overwrites the header with it.
A proxy that passes a client's own `X-Forwarded-For` through unchanged lets any client choose its
address.

The radio stream link (`/stream.mp3`) works at home. Through the tunnel it is behind
Access too, so players that cannot log in, such as Sonos, cannot use it from outside.
On the local network, anyone who can reach AutoDJ can listen without pairing.
