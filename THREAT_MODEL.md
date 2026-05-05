# Threat Model: AutoDJ

Last reviewed: 2026-05-05. Re-review every major release.

## Scope

AutoDJ is a single-user offline music player. Two surfaces ship:

1. **CLI** (`autodj` subcommands). Reads local audio + the FAISS
   index, writes M3U and history files. No network.
2. **Web UI** (`autodj serve`). FastAPI + WebSocket bound to
   `127.0.0.1:8080` by default. Opt into LAN with `--host 0.0.0.0`.

Out of scope: cloud sync, multi-user, anything paid. No authentication.
If you need remote access, put it behind Tailscale, mTLS, or a
reverse proxy with real auth.

## STRIDE per surface

### CLI

| STRIDE | Threat | Mitigation |
|---|---|---|
| Spoofing | n/a, local user | OS file permissions |
| Tampering | Malicious audio with crafted tags | mutagen handles malformed tags safely; every tag read is in a defensive try/except |
| Repudiation | n/a, single user. Optional history file is informational | (n/a) |
| Information disclosure | Path to private audio leaks via crash trace | Logs at INFO level redact paths beyond `music_dir` |
| Denial of service | Indexer hangs on huge corpora | `--limit N` flag, plus per-track checkpoint so partial progress survives a kill |
| Elevation of privilege | n/a, runs as the invoking user | `autodj.jobs` allows only a fixed subcommand allowlist; arg metachars rejected |

### Web UI (`autodj serve`)

| STRIDE | Threat | Mitigation |
|---|---|---|
| Spoofing | Anyone on the LAN reaches the server when bound to `0.0.0.0` | Default bind is `127.0.0.1`. LAN bind needs explicit `--host 0.0.0.0` and the startup banner shouts about it |
| Tampering | Path traversal via `/api/audio?path=` | The path parameter must appear verbatim in the loaded FAISS index. It's an allowlist, not a filesystem walk |
| Repudiation | No per-action audit trail | Logs include a request ID; that's enough for a single-user player |
| Information disclosure | `/api/audio` could exfiltrate any readable file | Allowlist of indexed paths; `Path.exists()` + `is_file()` re-checked before streaming |
| Denial of service | Heavy `index` job blocks the event loop | Long jobs run as subprocesses via `autodj.jobs`, one at a time. The API stays responsive |
| Elevation of privilege | Web UI invokes `autodj index/enrich/prune` as a subprocess | `JobManager._ALLOWED` is a hard-coded allowlist; arg metachars rejected; `shell=False` |

## Subprocess hardening

`autodj.jobs.JobManager.start()`:

- Accepts only `{"index", "enrich", "prune", "stats", "list-indexes"}`.
- Rejects any arg containing `&`, `|`, `;`, `` ` ``, `\n`, `\r`.
- Builds the command from a constant prefix (`sys.executable, "-m",
  "autodj"`) and vetted args. It never accepts an executable path
  from the request.
- `shell=False`.

Bandit flags B404 (`import subprocess`) and B603 (`Popen` with
non-literal args). Both are annotated `# nosec` with the rationale
above. If you change the allowlist or relax the metachar reject, the
annotations have to be re-justified.

## Network exposure

| Endpoint | Bind |
|---|---|
| `/`, `/api/*`, `/ws` | `127.0.0.1:8080` by default |
| `--host 0.0.0.0` | LAN-trusted only. README plus the serve banner say so loudly |

The web server has no authentication. Public-internet exposure is
unsupported. If you need remote access, put it behind Tailscale, mTLS,
or a reverse proxy with auth.

## Dependencies

- `uv.lock` committed.
- `pip-audit` runs in CI on every PR.
- Renovate opens PRs on dep bumps. Dev-dep bumps auto-merge after CI
  green; runtime deps need a human review.
- Trivy + OSV-Scanner run nightly via `.github/workflows/security.yml`
  and on every PR.

## Reporting a vulnerability

[`SECURITY.md`](SECURITY.md).
