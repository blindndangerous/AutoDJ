# Run analysis on a GPU worker

Prepare HTTPS and a machine token, install the worker environment, and run remote analysis against the server.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Using a GPU machine

Architecture and verification context: [Proposed background-work ADR](../../adr/0008-bound-background-work-outside-playout.md).

Advanced analysis on a NAS processor is slow. A computer with a graphics card can do it for the
server instead, over the network, with `autodj worker`. The worker asks the server for songs that
need analysis, downloads each one from the server, analyses it with the same model and code as
`autodj index`, and sends the result back. It never reads the server's music folder, so the two
machines need no shared drive and no matching paths, and the server keeps its own play history and
settings. Results appear in the server's library within a couple of seconds, without a restart.

While a worker is connected, the server pauses its own analysis so the two never analyse the same
song. Two minutes after the last worker leaves, the server analyses whatever is left on its own
processor again, so new music is always analysed sooner or later. This includes `autodj index`
run by hand on the server: its advanced analysis stops taking songs while a worker is connected
and leaves them to the worker (`autodj index --force` does not stop).

## 1. Give the server HTTPS

A worker token is sent with every request, so the server accepts it only over HTTPS (or from the
server machine itself). Set up TLS as in [HTTPS on your home network](network.md#https-on-your-home-network).
With a self-signed certificate, copy the certificate file (not the key) to the GPU machine and pass
it with `--ca-cert`.

## 2. Create a token

On any paired browser, open Settings, Analysis workers. Under Machine tokens, type a name for the
computer, such as Studio PC, leave Token use at Analysis worker, and choose Create token. The
token appears once, in a read-only field with a Copy token
button; copy it now, because it is never shown again. Choose Done to hide it. If it is lost,
choose Revoke on its row and create another. A worker token can only claim songs, download those
songs and post their analysis.

Use one token per machine. Settings counts a worker's songs and rate per token, so two machines
sharing a token show up as one worker, and revoking it stops both.

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
advanced analysis, `autodj index` downloads that copy. If the copies differ, the worker stops
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

The worker logs one line per song. Network trouble is retried with a growing pause, up to five
minutes, so a worker left running rides out a server restart. Ctrl+C finishes the song in hand,
gives the rest of its batch back and exits; a second Ctrl+C quits at once. A worker that stops
without giving its songs back (a crash or a pulled cable) loses them after ten minutes, and the
server hands them out again. A song the worker cannot decode is held back for six hours, and a song
whose file changed on the server since the last scan for one hour; a Rescan that reads the song's
new file clears either hold at once.

Settings, Analysis workers shows each worker (connected or not, its graphics card, when it was
last seen, songs analysed and failed, and songs per hour), how many songs are waiting, and whether
the server's own analysis is paused. Revoking a token stops its worker at its next request.

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
2. In AutoDJ, open Library tools and choose Rescan (or restart the server). The new songs can be
   played at once, before advanced analysis.
3. The worker catches up: a worker left running analyses them within a minute or so; otherwise run
   the `--idle-exit 10` command above on the GPU machine. You can start it at the same time as the
   Rescan: it analyses songs as the scan finds them, and exits once nothing new has turned up for
   ten minutes, running its `--after-command` if it has one.
