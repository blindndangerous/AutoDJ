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

Generate a token and export it in the current Bash session:

```bash
AUTODJ_ACCESS_TOKEN="$(openssl rand -hex 32)"
export AUTODJ_ACCESS_TOKEN
```

Store the generated token in a secret manager before startup. Then start the authenticated LAN
service, substituting the DNS name that clients use:

```bash
AUTODJ_LAN_HOST=radio.local \
AUTODJ_LAN_ORIGIN=http://radio.local:8080 \
docker compose --profile lan up autodj-lan
```

HTTP does not protect the token or session cookie from network observers. Use this Compose LAN
profile only on a trusted private network. For an untrusted network, terminate TLS at a trusted
reverse proxy or use `autodj serve` with its TLS options. Do not publish the loopback service
directly to the internet.

## Windows PowerShell setup

For native no-container operation, create paths and run diagnostics as follows:

```powershell
New-Item -ItemType Directory -Force music, index, models, backups | Out-Null
uv run autodj doctor
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

Generate an authenticated LAN token without placing it on a command line:

```powershell
$tokenBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $rng.GetBytes($tokenBytes)
} finally {
    $rng.Dispose()
}
$env:AUTODJ_ACCESS_TOKEN = -join ($tokenBytes | ForEach-Object { $_.ToString('x2') })
$env:AUTODJ_LAN_HOST = "radio.local"
$env:AUTODJ_LAN_ORIGIN = "http://radio.local:8080"
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

Native PowerShell stopped backup and restore equivalents are:

```powershell
docker compose --profile lan down
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
uv run autodj restore --force "backups\autodj-$stamp.zip"
uv run autodj doctor
docker compose up
```

## Upgrade checklist

1. Create and retain a backup.
2. Run `uv sync --frozen --all-extras` and `npm ci` from committed locks.
3. Run `uv run autodj doctor`.
4. Run Python, frontend, and container gates from `CONTRIBUTING.md`.
5. Start loopback-only and verify `/api/version` before enabling LAN access.
