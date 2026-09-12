# AutoDJ operations

Commands labeled Bash require a Linux host or WSL2. Native Windows operators should use the
PowerShell equivalents below. Run Linux container ownership and smoke commands inside WSL2 with
the repository on a WSL filesystem so UID 10001 and POSIX modes have their documented meaning.

## Configuration precedence

AutoDJ resolves defaults, `config.toml`, sibling `config.local.toml`, environment variables, then
explicit CLI flags. Omitting `--config` is valid. Explicitly naming a missing file is an error. Put
access tokens only in ignored local configuration or `AUTODJ_ACCESS_TOKEN`.

## Diagnose before serving

Run `uv run autodj doctor`. Use `uv run autodj doctor --json` for automation. A required failed
check returns exit 1. Doctor does not write the index and redacts both server and Hugging Face
tokens.

## Container ownership and exposure

Create bind sources before startup:

```bash
# Linux/WSL2 Bash
mkdir -p music index models
sudo chown 10001:10001 music index models
chmod 0755 music index models
AUTODJ_MUSIC_DIR=./music AUTODJ_INDEX_DIR=./index AUTODJ_MODEL_DIR=./models \
  docker compose up --build
```

The default process listens on container-internal `0.0.0.0` so Docker networking can reach it.
The Compose `--insecure-lan` flag acknowledges only that internal wildcard bind. Compose publishes
the port only on host `127.0.0.1` (`127.0.0.1:8080:8080`), so the default does not expose the
service to the host LAN.

Create fresh-clone LAN settings and start the authenticated service:

```bash
uv run autodj setup-lan --host-name radio.local
docker compose --profile lan up autodj-lan
```

Substitute the DNS name or IP that clients use. Setup writes the generated server secret and
Host/Origin policy to gitignored `.env`. Startup prints an 8-digit code. Enter it once in each
browser; subsequent visits reuse that browser's paired-device cookie.

Manage paired browsers from the running container:

```bash
docker compose --profile lan exec autodj-lan autodj devices list
docker compose --profile lan exec autodj-lan autodj devices pairing-code
docker compose --profile lan exec autodj-lan autodj devices revoke DEVICE_ID
```

HTTP does not protect the pairing code or session cookie from network observers. Use this Compose LAN
profile only on a trusted private network. For browser or LAN access on an untrusted network, use
end-to-end TLS: run `autodj serve` directly with both `--ssl-certfile` and `--ssl-keyfile` and a
certificate trusted by every browser. AutoDJ does not support TLS termination in front of its
server. For a private LAN that needs TLS, leave `AUTODJ_ACCESS_TOKEN` exported and run:

```bash
uv run autodj serve --host 0.0.0.0 \
  --allowed-host radio.local \
  --allowed-origin https://radio.local:8080 \
  --ssl-certfile radio.pem \
  --ssl-keyfile radio-key.pem
```

The certificate and key are local files on the AutoDJ server. This supports private LAN access,
not public Internet hosting. Do not publish the loopback service directly to the internet.

## Windows PowerShell setup

For native no-container operation, create paths and run diagnostics as follows:

```powershell
New-Item -ItemType Directory -Force music, index, models, backups | Out-Null
uv run autodj doctor
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

For the separate experimental AMD GPU environment, follow
[Experimental Windows AMD GPU setup](windows-amd.md). Its launcher and dependencies are separate
from the standard locked `.venv`.

Create authenticated LAN settings without placing a secret on the command line:

```powershell
uv run autodj setup-lan --host-name radio.local
docker compose --profile lan up autodj-lan
```

Docker Desktop bind-mount ownership depends on its WSL2/Linux filesystem mapping. Run
`bash scripts/container_smoke.sh` inside WSL2 for the authoritative UID and mode gate. Do not
replace the 0755 and UID 10001 contract with world-writable Windows mounts.

## Backup classifications

Re-derivable data includes `vectors.index`, `tracks.db`, `index-manifest.json`, and `dj_meta.db`.
Unique data includes profiles, liners, configured dayparts, optional history, and `web_state.json`.
A full archive contains available data from both classifications and labels every item in
`manifest.json`.

## Stopped-service backup

```bash
# Linux/WSL2 Bash
docker compose --profile lan down
uv run autodj backup backups/autodj-$(date +%F).zip
```

Stopped mode refuses `tracks.db-wal`, `tracks.db-shm`, `dj_meta.db-wal`, `dj_meta.db-shm`, and
SQLite rollback journals. Do not copy a live SQLite main file by itself. Backup rechecks sidecars
after copying. These checks can detect activity but cannot prove the process is stopped. Stopping
the service is the operator's responsibility. Backup refuses an existing destination unless
`--force` is explicitly supplied.

## SQLite online backup

```bash
# Linux/WSL2 Bash
uv run autodj backup --online backups/autodj-live-$(date +%F).zip
```

SQLite online backup includes committed DJ metadata WAL state consistently while serving and
archives one manifest-selected index generation. It retries a bounded generation race and refuses
continuous index churn instead of mixing generations.

## Restore and validate

```bash
# Linux/WSL2 Bash
docker compose --profile lan down
uv run autodj restore --force backups/autodj-2026-08-02.zip
uv run autodj doctor
docker compose up
```

Restore refuses unknown archive schema versions and existing destinations without `--force`. It
rejects encrypted, non-regular, unsafe, or symlink-derived content; preflights declared sizes and
target-filesystem free space; checks every member size and digest; and stages every payload before
replacing any target. An install failure rolls prior targets back. Cleanup warnings after a
successful install name retained recovery files and do not mean rollback occurred. Do not serve
until doctor exits 0. Keep an untouched archive until playback and profile and liner inventory are
confirmed.

For a native Windows process, press Ctrl+C in the terminal running `uv run autodj serve`, then wait
for the process to exit. If a service manager runs AutoDJ, stop that service and wait for it to
report that the process has stopped. The following commands create a stopped backup:

```powershell
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

To restore on native Windows, stop AutoDJ the same way, then run:

```powershell
uv run autodj restore --force "backups\autodj-2026-09-12.zip"
uv run autodj doctor
```

Replace the archive path with the backup you intend to restore. Use the same configuration for
backup, restore, and doctor that the previous `serve` process used. After doctor succeeds, restart
with the previous `uv run autodj serve` command and its options, or restart the service manager.

## Upgrade checklist

These steps apply to a native installation from a source checkout.

1. Stop AutoDJ, then create and retain a backup before changing the checkout or its dependencies.
2. Run `git status --short`. If it prints any paths, stop. Commit the changes on a branch or copy
   them outside the checkout. Continue only after `git status --short` prints nothing.
3. Replace `vX.Y.Z` below with the release tag you intend to run. Fetch the tags, then check out
   that release:

   ```bash
   git fetch --tags origin
   git switch --detach vX.Y.Z
   ```

4. Run `uv sync --frozen --all-extras`, `npm ci`, and `npm run build` from the fetched release and
   its committed locks. If you use the experimental Windows AMD environment, update it by following
   [Experimental Windows AMD GPU setup](windows-amd.md).
5. Run `uv run autodj doctor`.
6. Run Python, frontend, and container gates from `CONTRIBUTING.md`.
7. Start loopback-only and verify `/api/version` before enabling LAN access.
