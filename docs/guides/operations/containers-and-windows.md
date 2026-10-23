# Run AutoDJ in Docker or Windows PowerShell

Use the deployment section for your platform, check the startup log, and pair each browser before operating the station.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Docker

In Docker, AutoDJ always runs `serve --lan` on the host network: it listens on all of the
machine's interfaces (`0.0.0.0`), detects the machine's own names and addresses, and requires every
browser to pair. A bare-metal `autodj serve` stays local-only unless you pass `--lan`.

Settings come from three places beside `compose.yaml`:

- `.env` holds the host folders and the user the container runs as. Set `AUTODJ_UID` and
  `AUTODJ_GID` to the owner of the folders (`id -u` and `id -g`), so the container writes files
  you own and nothing needs a `chown`. Without them it runs as UID and GID 10001.
- `config.toml` and `config.local.toml` hold everything else, as on bare metal. The container
  mounts this folder read-only and starts in it, so relative paths in them, such as
  `ssl_certfile = "certs/fullchain.pem"`, are read from here.
- `AUTODJ_BEETS_PATH` in `.env` mounts the folder holding beets' `library.db` read-only at `/beets`;
  set `beets_db = "/beets/library.db"` in `config.local.toml` to use it. If beets stores paths
  relative to its music folder, leave `music_dir` unset or set it to `/music`, where the
  container sees the music (see [Beets paths and `music_dir`](categories.md#beets-paths-and-music_dir)).

```dotenv
AUTODJ_MUSIC_DIR=/path/to/music
AUTODJ_INDEX_DIR=./index
AUTODJ_MODEL_DIR=./models
AUTODJ_UID=1000
AUTODJ_GID=1000
#AUTODJ_BEETS_PATH=/home/you/.config/beets
```

Without `.env`, `./music` is mounted at `/music` and the index and model live in the named
volumes `autodj-index` and `autodj-models`. Add `/music` in Library after starting the container;
mounting a folder does not register it automatically.

```bash
docker compose up -d --build
docker compose logs autodj
```

The log shows the addresses to open and, while no browser is paired, an 8-digit pairing code. The
server secret is created on first start and saved in the index folder. Enter the code once in each
browser; later visits reuse that browser's paired-device cookie. The container restarts after a
crash or a reboot unless you stop it with `docker compose down`.

Manage paired browsers from the running container:

```bash
docker compose exec autodj autodj devices list
docker compose exec autodj autodj devices pairing-code
docker compose exec autodj autodj devices revoke DEVICE_ID
```

Over plain HTTP, network observers can see the pairing code and session cookie. Use it only on a
trusted private network. Otherwise set a certificate trusted by every browser
(`[server] ssl_certfile` and `ssl_keyfile`). A proxy that ends TLS and talks plain HTTP to AutoDJ
is not supported: the `Secure` cookie flag and the origin checks follow AutoDJ's own TLS setting.
For remote access, see [HTTPS with your own domain](domain-https.md#https-with-your-own-domain). The image's
health check tries HTTP and then HTTPS, so it works either way.

## Stop the container

AutoDJ saves its state as it stops, which normally takes a few seconds. `compose.yaml` sets
`stop_grace_period: 90s`, so `docker compose down`, `docker compose restart` and the recreation
that `docker compose up -d` does wait up to 90 seconds before they kill AutoDJ. Docker's default
is 10 seconds. If you copied an older `compose.yaml`, add the same line beside `restart:`. To stop
the container with plain Docker, give it the same time: `docker stop --time 90 autodj`.

## Update the container

AutoDJ in a container checks for updates like any installation and says in **Settings**,
**Updates** when one is available, with a link to its release notes and the commands to run. It
does not update itself, and nothing in the Compose file gets the Docker or Podman socket. Make a
backup, then, on the computer that runs the container, in the folder with `compose.yaml`, update
the checkout and rebuild. For a release:

```bash
git fetch --tags origin
git switch --detach vX.Y.Z
AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

The rebuild replaces the running container, which gets the
[time to stop](#stop-the-container) set in `compose.yaml`.

For the development channel, replace the two `git` lines with `git fetch origin`,
`git switch master` and `git merge --ff-only origin/master`. With Podman, use `podman compose` in
place of `docker compose`. The container's channel follows the checkout's branch: `master` is
Development, anything else is Stable.

**Restart AutoDJ** works in a container when the Compose file says how the container restarts.
`compose.yaml` sets `restart: unless-stopped` and, beside it, the environment variable
`AUTODJ_RESTART_POLICY: unless-stopped`; AutoDJ exits and the restart policy starts it again. A
container cannot see its own restart policy, so without that variable (`always`,
`unless-stopped` or `on-failure`) the page offers no Restart and says how to turn it on. If you
copied an older `compose.yaml`, add the variable with the same value as `restart:`.

## Build stamp

The footer of the web page names the commit the server runs, linked to that commit on GitHub,
once the browser is paired. A bare-metal checkout reads it from git. The image has no git and
its build leaves out `.git`, so pass the commit when you build it:

```bash
AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose build
AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Without it the footer says "build unknown". The value must be 7 to 40 hexadecimal digits;
anything else is ignored.

## Windows PowerShell setup

For native no-container operation, create paths and run diagnostics as follows:

```powershell
New-Item -ItemType Directory -Force music, index, models, backups | Out-Null
uv run autodj doctor
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

For the separate experimental AMD GPU environment, follow
[Experimental Windows AMD GPU setup](../../windows-amd.md). Its launcher and dependencies are separate
from the standard locked `.venv`.

For Docker on Windows, run Compose inside WSL2 (see [Docker](#docker)). Docker Desktop
bind-mount ownership depends on its WSL2/Linux filesystem mapping, and its host networking must
be turned on in Docker Desktop's settings. Run `bash scripts/container_smoke.sh` inside WSL2 for
the authoritative check. Do not use world-writable Windows mounts.
