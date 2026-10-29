# Run analysis on a GPU worker

Prepare HTTPS and a machine token, install the worker environment, and run remote analysis against the server.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Using a GPU machine

Architecture and verification context: [Proposed background-work ADR](../../adr/0008-bound-background-work-outside-playout.md).

Similarity analysis on a NAS processor is slow. A computer with a graphics card can do it for the
server instead, over the network, with `autodj worker`. The worker asks the server for songs that
need analysis, downloads each one from the server, runs the compatible audio and MuQ analysis, and
sends results back. It never reads the server's music folder, so the two machines need no shared
drive and no matching paths, and the server keeps its own play history and settings. Results appear
in the server's library within a couple of seconds, without a restart.

Music folders can request **Basic**, **Audio** or **Similarity** analysis. Audio saves the shared
16-value feature result and DJ metadata; Similarity adds a MuQ vector. The catalog caches those
audio features independently of the vector, keyed to the file identity and analysis version.
A rescan can reuse a matching cache; changed file identity invalidates it, while an existing vector
remains until replacement analysis is saved. A feature-version change causes the cache to be
recomputed. The worker scan-analysis protocol version is bumped for the shared result. Tracks
longer than 15 minutes use the first and last five minutes for audio features.
The **Similarity maximum song length** setting under **Settings**, **Global**, **Server** controls which
tracks also receive a MuQ vector; longer tracks still get Audio analysis.

While a worker is connected, the server pauses its own analysis so the two never analyse the same
song. Two minutes after the last worker leaves, the server analyses whatever is left on its own
processor again, so new music is always analysed sooner or later. This includes `autodj index`
run by hand on the server: its MuQ analysis stops taking songs while a worker is connected
and leaves them to the worker (`autodj index --force` does not stop).

Workers that advertise `scan_decode_v1` and the matching `scan_analysis_version` also decode new
and changed music found by a library scan: length and energy, automatic cue points, intro lift,
loudness when enabled, and the audio features and DJ beat grid. Each song is downloaded once for
this work and its MuQ analysis. Songs set to Basic, or excluded from Similarity by the length
limit, still receive the analysis their folder requests without an embedding vector. A worker and
server with different scan-analysis versions do not share scan results; the server handles that
decoding locally. Older workers can still perform compatible vector jobs.

The server still discovers files, reads tags and lyric sidecars, reconciles moves, imports
external DJ cues, and scans liners. On-demand analysis during playback is unchanged. New music
is listed from tags but counts as silent until measured, so automatic selection waits for it.
Changed music with an existing playable record keeps its measurements, points and vector
until replacement analysis arrives, unless its new tag duration differs by more than the larger
of two seconds or two percent of the old duration. Such replacements use a silent tag-only
listing until measured, so playback cannot truncate them using the old cues. Unknown tag
durations keep the playable record. Tag rewrites therefore do not silence the existing library
while a worker drains the backlog. Timestamp-only changes do not trigger deferred DJ analysis.
Older workers without this capability leave scan decoding on the server. A newer worker also
works with an older server, using the original analysis protocol.

Deferred scan work survives restarts in `library.db`. Remote claims protect songs for ten
minutes without renewal. Local scan attempts have their own reservation and retry budget;
the CPU vector job's claims and failure holds do not delay scan decoding, and scan failures
do not hold back that job. The scan checks worker capability at most once every five seconds
and reads pending paths once per pass. Unchanged songs with valid cue points add no per-song
queue calls or writes.

The background library tick starts a recovery run on the library's scan thread, even
with automatic rescans disabled. The run takes one song at a time until no eligible work remains,
shutdown is requested, a scan is queued, or a capable worker resumes claiming. The 30-second
idle check interval does not delay songs within a run. Decoding does not run on the ticker. An active scan also
checks for abandoned songs between files, at most every five seconds, using cached capability.
This keeps recovery within the existing scan lock and shutdown ownership. One slow file can
still delay the next recovery attempt; these intervals are not throughput guarantees.
The library shows "Recovering analysis: n of m" during background decoding, without a completion
announcement. User-started scans retain their completion announcement. Cue-only snapshot
refreshes leave the import state idle. Recovery reloads the
catalog at most every 30 seconds and once at the end; each decoded song updates cached lookups.

After two minutes without a capable worker's heartbeat, unclaimed songs become eligible.
Live remote claims remain protected until expiry. Failed or stale remote scan claims release
their analysis lease and reserve the song for immediate local recovery, without a vector hold. An unclaimed song's ten-minute deadline allows recovery only when no connected
capable worker has claimed work in the last ten minutes. A busy worker therefore keeps its
backlog even during a large import. Recovery reads one candidate and uses a separate count for
the batch-start log. Idle ticks check at most every 30 seconds, outside the library lock,
before taking the scan lock or starting a thread. A worker-progress lookup selects an indexed
handoff/failed-lease query while a capable worker is claiming; ordinary backlog rows are excluded.
Exhausted attempts, backoff and live remote claims
do not occupy the scan slot. Due automatic rescans run before recovery.

When a pending file changes, the next scan refreshes its pending identity. New or silent
listings also refresh their tags; playable records retain the previous bytes' measurements
unless the known tag duration exceeds the tolerance above. A tag edit during this wait updates
the pending identity transactionally. A pending replacement preserves the old record's analysis
identity; a tag-only listing updates its record identity too and retains its original provenance.
Claims use the pending size and fingerprint, checking them against the file on disk. A mismatch
hands that scan to local recovery immediately. Results recheck pending identity and disk bytes
before replacing catalog measurements in one transaction. A second rewrite during a lease
rejects the stale result and leaves the newer pending work available for recovery.
Local deferred decoding also compares file identity before and after decoding. If readable
bytes change, it discards the measurements, retains the attempt already counted, and waits
60 seconds for the new bytes to settle. Other eligible songs can proceed during that wait.
Repeated changes during decoding exhaust the same three-attempt automatic recovery budget.

When the pending and catalog byte identities match, worker completion and local recovery keep
all user/imported points and manual BPM/key values, including edits made before deferral and
pending rows from older versions without a snapshot. For changed bytes, the server compares
choices with the deferral snapshot and preserves only later edits; a missing snapshot preserves
none. Explicit point clears remain clears. Saved Start/End clamp to
the decoded length; an impossible bound yields to analysis. Intro/Outro/Segue clamp to the
resulting Start/End range. Automatic trim expands to include valid saved mix points.
DJ metadata follows the catalog's user and imported markers. With external cue import on,
cached markers from the old file are replaced by the DJ libraries' current ones; with it off,
cached cues are kept. Aligned markers are deduplicated. Other saved cues remain within the new length;
cues beyond it are dropped with debug logging. Editing a new listing does not make it measured
or skip pending decoding.

A rename or move preserves pending work and play history when exactly one missing song and
one new file share the pending byte identity, with no present song holding that identity.
Ambiguous matches remain separate removals and additions. Pending retry state follows the
new path. Existing worker claims are cancelled, so an old-path result is rejected. Failed and stale
holds move with their original expiry. If both paths have holds, the later expiry wins. The move
and hold reconciliation roll back together on failure. New claims respect the retained hold
and retry budget.

Remote claims select at most one requested batch of
eligible pending paths in SQL before reading files; local reservations and retry holds are excluded.
Pending membership for advanced candidates uses individual indexed lookups. Missing or unreadable
pending files receive six-hour backoff, allowing later batches to reach available songs.
Claims leave pending rows without a catalog record untouched until their deadline passes:
the scan may still be reading tags. Expired rows are handed to local recovery, which cancels
them under the scan lock only if the catalog record is still absent.

Loudness measurement failure is non-fatal: points and other measurements remain usable with
no stored loudness. Missing or unreadable files retain their catalog identity and receive backoff;
confirmed missing-file cleanup remains the only removal path for unavailable files. Decoder
rejection removes only an unchanged tag-only listing created by that pending scan, after checking
that the same bytes remain readable. Older songs, including their history, remain in the catalog.
The original file remains untouched. Other scan failures
wait six hours between automatic attempts, independently of vector holds. After three local
attempts, automatic retries stop with a WARNING naming the file. If any attempt failed for a
reason other than file access or the decoder's recognized resource errors, the unchanged,
readable new tag-only listing and its pending row are removed. This includes missing FFmpeg,
decode timeouts, unsupported decoders and analysis errors. Older entries and their history
remain. Failures appear in the scan's import errors.

Access-only failures preserve the entry and reopen exhausted work on the next scan, either for
a capable worker or local decoding; missing-file cleanup handles actual deletions.
A later scan with no capable worker retries pending rows even during backoff or after
exhaustion, while respecting live remote claims. Background recovery retains the retry limits.
Further identity refreshes during settling or after local handoff retain attempts and backoff for
the same pending lifetime; other changed-byte refreshes reset the budget, and all clear old failure evidence.
Queue errors produce a rate-limited warning and later ticks continue.

Worker results use the catalog duration cross-check only for measured records matching the
claimed bytes. Silent tag-only listings (zero energy and no points) may have inaccurate tag
durations, so results allow half to twice the positive tag duration, with three seconds of slack
at each end; replacements use the new bytes' tags, and unknown tag lengths add no cross-check.
Every result still requires a finite, positive duration of at most 24 hours and passes the other result checks.
The server fails owned leases immediately when result validation rejects them, including
malformed measurements. Pending scans pass to local recovery; advanced-only work receives
the existing failure hold for invalid content, preventing immediate repeat claims.
Vector-only model-identity conflicts retain the worker's immediate-release behavior without a failed hold.

Scan algorithm versions must match. A mismatch leaves decoding local and logs once per worker
while its token remains in the bounded warning history. Transient network errors and server
outages preserve negotiated scan support. Rejected capability fields or a server response with
incompatible support trigger negotiation, so server downgrades retain the original vector protocol.
A vector result from a lease issued
before a scan deferral can still be stored; it closes that lease while scan work remains pending.
Workers release rejected results so heartbeats cannot renew their claims indefinitely.

External DJ cue imports load lazily once per scan pass or recovery run. Between scans, remote
DJ results can request a refresh on the same background slot, at most once a minute. Result requests merge
the latest snapshot without opening external libraries. Catalog measurements and vectors commit
together; the DJ cache is a separate write. A DJ cache failure preserves the catalog result and
leaves scan work pending for local recovery.
Scan-only results preserve existing key, mode, BPM, tempo confidence, embedding time and vector;
they update only scan measurements, including energy.

## 1. Give the server HTTPS

A worker token is sent with every request, so the server accepts it only over HTTPS (or from the
server machine itself). Set up TLS as in [HTTPS on your home network](network.md#https-on-your-home-network).
With a self-signed certificate, copy the certificate file (not the key) to the GPU machine and pass
it with `--ca-cert`.

## 2. Create a token

To skip copying a token, pair the worker instead: see [Pair the worker](#pair-the-worker) in
step 4. The token below still works the same way.

On any paired browser, open Settings, Global, Analysis workers. Under Machine tokens, type a name for the
computer, such as Studio PC, leave Token use at Analysis worker, and choose Create token. The
token appears once, in a read-only field with a Copy token
button; copy it now, because it is never shown again. Choose Done to hide it. If it is lost,
choose Revoke on its row and create another. A worker token can only claim songs, download those
songs and post their analysis.

Use one token per worker process, including separate processes on the same machine. Capability,
heartbeats, counts and rate belong to the token. On startup, a compatible worker's connect/check
handshake releases that token's old claimed leases, including claims orphaned by a crash or
forced shutdown. Heartbeats then renew only lease IDs received by that process. Older workers
that omit the lease list keep holder-wide renewal. Older servers keep the original protocol.
Sharing one token is unsupported: a new process can release the other process's claims and
overwrite its negotiated capability.

On a server without a browser at hand, for example inside a container, the same can be done from
the command line (`docker exec -it autodj autodj tokens ...` in a container):

```bash
uv run autodj tokens create studio-pc --scope analysis-worker   # prints the token once
uv run autodj tokens list                                       # ids, names, scopes, last use
uv run autodj tokens revoke <id>
```

`tokens create` prints only the token on standard output, and its warning that the token is never
shown again on standard error.

## 3. Install AutoDJ on the GPU machine

Install AutoDJ as usual with the `index` extra, then check that PyTorch sees the graphics card:

```bash
uv run python -c "import torch; print(torch.cuda.is_available())"
```

`True` means the worker runs on the GPU (NVIDIA CUDA, or AMD ROCm as in
[Windows AMD GPU setup](../../windows-amd.md)); `False` means it would run on the processor. The GPU
machine needs no music folder and no library. The model downloads on first use.

The worker's results count only if it ran exactly the server's model weights. `[model] revision`
is a branch name (`main`), and two machines that downloaded `main` at different times can hold
different weights, so the server compares the snapshot commit of each machine's downloaded copy
(or, for a `[model] manual_path` folder, a digest of its weight files). The server needs its own
copy of the model for this. With a model-capable server runtime and songs pending
Similarity analysis, `autodj index` downloads that copy. If the copies differ, the worker stops
before it loads anything and says which commit the server has; set
`[model] revision = "<that commit>"` in the GPU machine's `config.toml` and start the worker again,
and it downloads exactly the server's weights.

A [no-model server](../../setup.md#no-model-installation) can use remote analysis
only if it already has a compatible cache at its configured `model_dir`, or a
compatible model folder at `manual_path`. Keep that copy available: the server
checks its exact identity without loading the model. Running `autodj index` in
no-model mode imports songs but does not download weights. A fresh no-model
server without a cache cannot accept remote analysis; preserve an existing
compatible cache or obtain it using ordinary analysis in a model-capable runtime
before returning to no-model mode. Merely setting a revision does not create a
cache. Allow disk space for the retained model even though its runtime packages
are optional.

## 4. Run the worker

Put the token in `AUTODJ_WORKER_TOKEN`, so it stays out of the shell history, and start the
worker with the server's address:

```bash
export AUTODJ_WORKER_TOKEN='adj1.…'
uv run autodj worker --server https://nas.local:8080
```

```powershell
$env:AUTODJ_WORKER_TOKEN = 'adj1.…'
uv run autodj worker --server https://nas.local:8080
```

- `--once` stops when nothing is left to analyse. Without it the worker keeps running and picks up
  new songs within half a minute.
- `--idle-exit MINUTES` also stops when nothing is left, but only once the queue has stayed empty
  for that many minutes, asking again every half minute. Use it when the server may still be
  scanning new music as the worker starts. New songs during the wait start the count again.
  `--once` is the same as `--idle-exit 0`; given both, `--idle-exit` sets the wait.
- `--idle-release MINUTES` waits for songs before loading the model. After the queue stays
  empty for that many minutes, it frees the model and keeps polling. The minimum is 1 minute.
  New songs load the model again. Heartbeats continue while the model loads and while it is
  released, so the server's CPU analysis stays paused. If `--idle-exit` and `--idle-release`
  both expire, exit wins.
- `--batch N` claims N songs at a time (default 4, at most 16).
- `--device cuda` insists on the GPU and stops if PyTorch sees none; `--device cpu` forces the
  processor. The default uses the GPU whenever PyTorch sees one, and the worker says which it uses
  when it starts.
- `--ca-cert FILE` trusts the server's self-signed certificate.
- `--token TOKEN` gives the token on the command line instead of the environment variable.
- `--pair CODE` trades a pairing code for a token; see [Pair the worker](#pair-the-worker).

Without `--token` or `AUTODJ_WORKER_TOKEN`, the worker uses the token that `--pair` saved for the
same `--server` address. Without any of these, it stops and says how to get a token.

The worker logs one line per song. Network trouble is retried with a growing pause, up to five
minutes, so a worker left running rides out a server restart. Ctrl+C finishes the song in hand,
gives the rest of its batch back and exits; a second Ctrl+C quits at once. A worker that stops
without giving its songs back (a crash or a pulled cable) loses them after ten minutes, and the
server hands them out again. For MuQ analysis, a song the worker cannot decode is held back
for six hours, and a song whose file changed on the server since the last scan for one hour;
a Rescan that reads the song's new file clears either hold at once. Pending scan measurements
instead receive the local recovery attempt described above.

Settings, Global, Analysis workers shows each worker (connected or not, its graphics card, when it was
last seen, songs analysed and failed, and songs per hour), how many songs are waiting, and whether
the server's own analysis is paused. Revoking a token stops its worker at its next request.

### Pair the worker

Pairing gives a worker its own token without copying the token by hand.

1. On any paired browser, open Settings, Global, Analysis workers. Under Worker pairing, optionally type a
   Worker name, then choose Pair a worker. With no name, the token takes the worker computer's own
   host name.
2. The page shows an 8-digit pairing code and the command to run, with Copy code and Copy command
   buttons. The code works once, for ten minutes. A new code replaces the old one.
3. On the GPU machine, run the command, for example:

   ```bash
   uv run autodj worker --pair 12345678 --server https://nas.local:8080
   ```

The worker trades the code for a new analysis-worker token over HTTPS (or plain HTTP to the server
machine itself) and starts working. It says where it saved the token: on Windows
`%APPDATA%\autodj\worker-tokens.json`, elsewhere `~/.config/autodj/worker-tokens.json` (or under
`$XDG_CONFIG_HOME`). Only your user account can read the file: mode 0600 on Linux and macOS, and
an access list for your account alone on Windows. The worker refuses to use the file if others
can read it. Later runs with the same `--server` address use the saved token, so leave out
`--token` and `AUTODJ_WORKER_TOKEN`; `--pair` refuses to run when either is given.

To remove the token, delete that file, or its entry for the server, and revoke the token in
Settings, Global, Analysis workers. Pair with one server at a time: two `--pair` runs at once can lose one
of the saved tokens. Wrong codes have the same limits as browser pairing: after ten wrong
codes from one computer, that computer must wait until the code expires; after fifty wrong codes in
all, the code stops working and you need a new one.

## Manage a worker from the server

Each worker row in Settings, Global, Analysis workers has its own settings and a Save button:

- **Paused**: the server hands the worker no songs. A worker of this release stops claiming,
  finishes the song in hand and gives the rest of its batch back. It keeps sending heartbeats, so
  it still counts as connected and the server's own analysis stays paused too: you paused analysis
  on purpose. Once the claims are back, the worker frees the model and runs `--after-command`
  straight away, with or without an idle release, so a program sharing the GPU gets it back. A
  worker that starts while paused does not run `--before-command` or load the model. When resumed,
  it runs `--before-command` and loads the model again only when songs arrive. Paused counts as an
  empty queue: `--once` stops at once, and `--idle-exit` stops after its wait. Without either, a
  paused worker waits to be resumed or stopped.
- **Songs per claim**: 1 to 16, like `--batch`.
- **Idle release minutes**: 1 or more, like `--idle-release`. With it set, the worker loads the
  model only while songs are waiting and runs `--before-command` and `--after-command` around each
  load, as with `--idle-release`.

Leave a box empty to use the worker's own setting. A value given on the worker's command line wins
over the server's, and the worker logs that once. The worker applies a saved change at its next
heartbeat or claim, within about a minute, without a restart. The row says whether the worker has
applied the latest settings. A worker from an older release cannot apply settings, but Paused still
stops the server handing it songs; it finishes the batch it holds first.

After restoring the server from an older backup, restart the workers so they take the server's
settings again: a worker ignores a settings revision at or below the one it already applied.

The server never sends anything for the worker to run. `--before-command` and `--after-command`
come only from the worker's own command line.

## When the server refuses a worker

When the server refuses a worker, Settings, Global, Analysis workers lists it under Refused workers with
when it happened and what to do, until that worker next connects successfully or you choose
Dismiss. The server keeps only the latest refusal for each token.

- **Model weights or analysis version differ**: the notice gives the fix, such as setting
  `[model] revision = "<commit>"` in the worker's `config.toml` (see
  [step 3](#3-install-autodj-on-the-gpu-machine)), or updating AutoDJ on both machines to the same
  release. A worker of this release reports its model to the server, so this shows before it
  loads anything. An older worker stops on its own, and the server sees the mismatch only if it
  sends a result.
- **Scan analysis version differs**: the worker still does MuQ analysis, but the server
  decodes new music itself. Update AutoDJ on both machines to the same release.
- **Revoked token, wrong secret, or not a worker token**: pair the worker again, or give it a new
  analysis-worker token.

## Sharing the GPU with another program

When another program uses the same graphics card, such as a text-to-speech service in a
container, it can leave too little graphics memory for the model. The worker can stop that
program first and start it again when it is done:

```bash
uv run autodj worker --server https://nas.local:8080 --idle-exit 10 \
  --before-command "podman stop tts-service" --after-command "podman start tts-service"
```

```powershell
uv run autodj worker --server https://nas.local:8080 --idle-exit 10 `
  --before-command "podman stop tts-service" --after-command "podman start tts-service"
```

The worker logs "Queue empty; waiting up to 10 minutes for new songs." when it runs out of work,
and "Queue empty for 10 minutes; stopping." when it gives up.

An idle release set in Settings works the same as `--idle-release`.

- Without `--idle-release`, `--before-command` runs once, after the worker has checked the token
  and the model with the server and before it loads the model.
- Without `--idle-release`, `--after-command` runs once, when the worker stops for any reason:
  the queue stayed empty for the `--idle-exit` wait (or was empty with `--once`), Ctrl+C,
  a revoked token or another error. The
  other program stays stopped while the worker waits for new songs; neither command runs between
  empty polls. The worker moves the model off the graphics card and
  frees its memory first.
- Each runs without a shell: the text is split into words as a shell would, but pipes, `&&` and
  variables mean nothing, so put several steps in a script and name the script. Each may run for
  two minutes. The worker logs the command, its output and its exit code, and goes on even if it
  fails.

Without `--once`, `--idle-exit` or `--idle-release`, the worker keeps the other program stopped
for as long as it runs.

Use `--idle-release` to keep the worker available while letting the other program use the GPU
between batches:

```bash
uv run autodj worker --server https://nas.local:8080 --idle-release 5 \
  --before-command "podman stop tts-service" --after-command "podman start tts-service"
```

The worker checks its identity and starts polling without loading the model or running either
command. The first songs trigger `--before-command` and model loading. Heartbeats renew the
claims while loading. After five minutes of empty claims, the worker frees the model and runs
`--after-command`. It logs "Queue empty for 5 minutes; released analysis resources." and keeps
polling. New songs repeat the cycle. On exit, it runs `--after-command` only if the current
cycle has not already released the model. If loading fails after `--before-command`, it still
runs `--after-command` to restore the other program.

If the server stops answering, the worker keeps trying again, waiting up to five minutes between
tries. Once the server has been unreachable for the idle release time, or for 10 minutes without
an idle release, the worker frees the model and runs `--after-command`. It logs "Server
unreachable for 10 minutes; released analysis resources until it hands out songs." once and keeps
trying. When the server answers again and hands out songs, the worker runs `--before-command` and
loads the model again.

## Keep the worker running as a systemd user service

On Linux, install AutoDJ with GPU support in `~/autodj/.venv` and put its configuration in
`~/autodj/config.toml`. Check the worker command in a terminal first.

Create the configuration and service directories:

```bash
mkdir -p ~/.config/autodj ~/.config/systemd/user
```

Save your token in `~/.config/autodj/worker.env`:

```ini
AUTODJ_WORKER_TOKEN=adj1.REPLACE_WITH_YOUR_TOKEN
```

Restrict access:

```bash
chmod 600 ~/.config/autodj/worker.env
```

Save this as `~/.config/systemd/user/autodj-worker.service`. Replace the server address and
commands as needed. Use a hostname covered by the server's TLS certificate.

```ini
[Unit]
Description=AutoDJ GPU analysis worker

[Service]
WorkingDirectory=%h/autodj
EnvironmentFile=%h/.config/autodj/worker.env
ExecStart=%h/autodj/.venv/bin/autodj --config %h/autodj/config.toml worker --server https://nas.local:8080 --idle-release 5 --before-command "podman stop tts-service" --after-command "podman start tts-service"
Restart=on-failure
RestartSec=30
KillSignal=SIGINT
TimeoutStopSec=300

[Install]
WantedBy=default.target
```

Start the service and read its log:

```bash
systemctl --user daemon-reload
systemctl --user enable --now autodj-worker.service
journalctl --user -u autodj-worker.service -f
```

The service keeps polling after releasing the model. Its heartbeats keep server CPU analysis
paused. To keep the user service running after logout, enable lingering with
`loginctl enable-linger "$USER"`; this may need administrator approval.
Stop it with `systemctl --user stop autodj-worker.service`. The service sends SIGINT so the
worker can finish its current song and restore the other program. Systemd forces shutdown
after five minutes if cleanup has not finished.

## Adding new music

1. Add the new music to the library as usual (for example `beet import /path/to/new`).
2. In AutoDJ, open Library tools and choose Rescan (or restart the server). New songs appear
   from their tags; automatic picks wait until basic measurements arrive.
3. The worker catches up: a worker left running analyses them within a minute or so; otherwise run
   the `--idle-exit 10` command above on the GPU machine. You can start it at the same time as the
   Rescan: it analyses songs as the scan finds them, and exits once nothing new has turned up for
   ten minutes, running its `--after-command` if it has one.
