# Threat Model — AutoDJ

Last reviewed: 2026-05-05.  Re-review on every major release.

## Scope

AutoDJ is a single-user, fully-offline music player.  Two surfaces
ship:

1. **CLI** — `autodj` subcommands.  Reads local audio files + the
   FAISS index; writes M3U / history files.  No network.
2. **Web UI** — `autodj serve`, FastAPI + WebSocket on default
   `127.0.0.1:8080`.  Optionally bound to LAN with `--host 0.0.0.0`.

Out of scope: cloud sync, multi-user, paid features.  No
authentication.

## STRIDE per surface

### CLI

| STRIDE | Threat | Mitigation |
|---|---|---|
| **S** Spoofing | n/a — local user | OS file permissions |
| **T** Tampering | Malicious audio file embedded with crafted tags | Mutagen handles malformed tags safely; defensive `try/except` around every tag read |
| **R** Repudiation | n/a — single user, optional history file | History is informational only |
| **I** Information disclosure | Path to private audio leaks via crash trace | Logs at INFO level redact full paths beyond music_dir |
| **D** Denial of service | Indexer hangs on huge corpora | `--limit` flag; per-track checkpoint so partial progress survives kill |
| **E** Elevation of privilege | n/a — runs as the invoking user | Subprocess use in `autodj.jobs` enforces hard-coded subcommand allowlist + metachar reject |

### Web UI (`autodj serve`)

| STRIDE | Threat | Mitigation |
|---|---|---|
| **S** Spoofing | Anyone on LAN reaches the server when bound to `0.0.0.0` | Default bind is `127.0.0.1`; LAN bind requires explicit `--host 0.0.0.0` and a documented warning |
| **T** Tampering | Path traversal through the `/api/audio?path=` parameter | Path must appear verbatim in the loaded FAISS index — paths are an allow-list, not a filesystem walk |
| **R** Repudiation | n/a — no per-action audit trail required | Logs identify the request via request ID |
| **I** Information disclosure | `/api/audio` could exfiltrate any file the user can read | Allow-list of indexed audio files; `Path.exists()` + `is_file()` re-check before streaming |
| **D** Denial of service | Single Python event loop blocks under heavy `index` job | Background subprocesses run via `autodj.jobs` so the API stays responsive; one job at a time |
| **E** Elevation of privilege | Web UI invokes `autodj index/enrich/prune` subprocess | `autodj.jobs.JobManager._ALLOWED` allow-list of subcommand names; metachar reject in args; `shell=False` |

## Subprocess hardening

`autodj.jobs.JobManager.start()`:

- Accepts only a fixed allow-list (`{"index", "enrich", "prune",
  "stats", "list-indexes"}`).
- Rejects any arg containing `&`, `|`, `;`, `` ` ``, `\n`, `\r`.
- Builds the command from a constant prefix (`sys.executable, "-m",
  "autodj"`) plus vetted args; never accepts an executable path.
- Uses `shell=False`.

Bandit reports B404 / B603 for the import + Popen call; both are
annotated `# nosec` with the rationale above.

## Network exposure

| Endpoint | Bind |
|---|---|
| `/`, `/api/*`, `/ws` | `127.0.0.1:8080` by default |
| `--host 0.0.0.0` | LAN-trusted only.  Documented in README + serve startup banner |

The web server has **no authentication**.  Public-internet exposure
is unsupported.  Reverse-proxy with auth (Tailscale, mTLS, Caddy +
basic auth) if you need remote access.

## Dependencies

- Lockfile (`uv.lock`) committed.
- `pip-audit` runs in CI on every PR.
- Renovate opens PRs on dep bumps.
- Dev-dep bumps auto-merge after CI green; runtime deps require
  manual review.

## Reporting a vulnerability

See [`SECURITY.md`](SECURITY.md).
