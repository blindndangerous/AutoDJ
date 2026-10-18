# Manage and recover access

Use this guide to grant access, replace credentials or recover after a move.
You need local terminal access as the AutoDJ account, or a paired browser for
browser-managed settings. Keep credentials in a password manager or private
local configuration, outside issues and station archives. Follow the
[network and TLS procedure](../operations.md#local-network-access) first.

## Browser controllers

A paired browser controls every station and can pair more browsers. Pair only
trusted operators; there are no separate station-operator roles. Listening alone
does not grant control.

1. In **Settings, Profiles and access**, find **Browser access** and choose
   **Show pairing code**. Enter the short-lived, single-use code on the new
   browser's pairing page; confirm its name appears in the device list.
2. Use **Rename** for recognizable device names. Choose **Revoke** on a lost or
   untrusted device's row; it loses access without a restart.
   **Sign out this browser** ends the current session.
3. If no browser remains, use the server terminal. Replace `./autodj` with
   `.\autodj.cmd` in PowerShell. Put the same `--config PATH` before `devices`
   if serving uses an explicit configuration.

   ```bash
   ./autodj devices list
   ./autodj devices pairing-code
   ./autodj devices revoke DEVICE_ID
   ```

To revoke every browser deliberately, `devices reset --yes` confirms without a
prompt. Obtain a new code and pair trusted devices again.
The LAN signing token comes from configured `access_token`,
`AUTODJ_ACCESS_TOKEN`, or generated `<index_dir>/.access-token`. Changing the
active token invalidates sessions after restart. Deleting the generated file
works only when no configured/environment token wins. Prefer device revocation
for one lost browser. Do not share the signing token.

## Worker and announcer tokens

1. Open **Settings, Analysis workers**, under **Machine tokens**. Create a named
   token for its intended use and copy it immediately; it is shown only once.
   Analysis workers can claim songs, fetch claimed audio and submit analysis.
   Announcer tokens are bound to a station and its break operations. Neither
   token is a browser-control login.
2. Put the replacement in the client's private configuration, test its next
   request, then **Revoke** the old token. For suspected compromise, revoke
   first and accept the interruption. Workers stop at their next request.
3. If lost, revoke and create another; the secret cannot be displayed again.
   The CLI provides the same operations:

   ```bash
   ./autodj tokens list
   ./autodj tokens create studio-pc --scope analysis-worker
   ./autodj tokens create news --scope announcer --station default
   ./autodj tokens revoke TOKEN_ID
   ```

Creation prints the secret: avoid shared transcripts. Use HTTPS for remote tokens
and the worker's `--ca-cert` for a private certificate; do not bypass certificate
checks. See [workers](../operations.md#using-a-gpu-machine) and
[announcers](../operations.md#announcer-breaks-dj-breaks-news-and-weather).

## Station and provider credentials

- **Live DJ:** in **Settings, Live DJ**, set a new password for the selected
  station's account. Leaving it blank preserves the old password; it cannot be
  read back. Removing a DJ takes that DJ off air immediately. For suspected
  compromise, remove the account first, then recreate it with a new password.
  Restore retains account details without password hashes: set passwords again.
- **Listeners:** update `stream.listener_user` and `stream.listener_password`
  in the station's private `secrets.json`, preserving other entries, then restart.
  For the default station: `<index_dir>/stations/default/secrets.json`. Update
  each player's login. Listening is public without a password; removing the
  password is not revocation. Use HTTPS outside trusted networks. See
  [listener access](../operations.md#listener-access-controls).
- **Relays:** replace the source password at the external server and in the
  station's relay settings. Saved passwords are write-only. The environment
  override `AUTODJ_STREAM_RELAY_<NAME>_PASSWORD` wins and cannot create a relay;
  update that environment and restart if it manages the value. Disable the relay
  while resolving login failure. Relay transport has no TLS: use unique source
  passwords over trusted paths. See [relay setup](streaming-and-relays.md).
- **Speech providers:** replace the write-only API key in **Speech connections
  for all stations** in the Speech studio. Connections are shared across stations.
  `AUTODJ_SPEECH_<ID>_KEY` overrides saved keys; uppercase the ID and replace
  hyphens with underscores. Environment-managed keys cannot be edited in the
  browser: update the process environment and restart. Revoke the old key at
  the provider; deleting it locally does not revoke it there. Configuration
  checks do not authenticate; a synthesis test can incur charges.
- **Hugging Face downloads:** replace `[huggingface] token` in private local
  configuration or the overriding `AUTODJ_HUGGINGFACE_TOKEN` environment value.
  Restart processes using it and revoke the old token at Hugging Face. Keep it
  separate from worker credentials; do not download models merely to test recovery.
- **Webhooks:** preserve each station's private webhook URLs/credentials separately,
  replace any secret at its receiving service and update the configured webhook.
  Disable delivery while resolving a failed or compromised destination. See
  [feed and webhook settings](../operations.md#now-playing-feed-and-webhooks).
- **Stereo Tool:** the write-only licence can be install-wide or station-specific.
  `AUTODJ_STEREOTOOL_LICENSE_KEY` overrides the install-wide saved key and is
  read-only in the page. Recover it from private records or the vendor, not a
  station backup. See [licence management](../operations.md#getting-stereo-tool-and-its-licence).

## Check recovery

Backups exclude pairing secrets/devices, machine tokens, listener and webhook
secrets, relay credentials, provider keys and Stereo Tool licences. Configuration
files and environment values are separate too. Restored live-DJ accounts need
new passwords. Keep a private inventory of these dependencies; do not turn a
station ZIP into a credential archive.

After a move or restore, pair browsers, restore required credentials and update
player addresses. Run [read-only diagnostics](collect-diagnostics.md), then check
the intended browser, player and integration can connect. Leave an integration
disabled while recovering its missing credential. Preserve the working installation
and archives until [rehearsal](prepare-station.md) succeeds. Report possible
credential exposure through the [private security path](../../SECURITY.md#reporting-a-vulnerability).

AutoDJ and this page were developed with AI assistance.
