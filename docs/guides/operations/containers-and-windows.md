# Run AutoDJ in Docker or Windows PowerShell

Use the deployment section for your platform, check the startup log, and pair each browser before operating the station.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Docker

In Docker, AutoDJ always runs `serve --lan` on the host network: it listens on all of the
machine's interfaces (`0.0.0.0`), detects the machine's own names and addresses, and requires every
browser to pair. A bare-metal `autodj serve` stays local-only unless you pass `--lan`.

`compose.yaml` runs the image AutoDJ publishes, so nothing is built on your computer; see
[Choose an image](#choose-an-image). Settings come from three places beside `compose.yaml`:

- `.env` holds the image, the host folders and the user the container runs as. Set `AUTODJ_UID` and
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
docker compose pull
docker compose up -d
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

## Choose an image

AutoDJ publishes its image on GitHub Container Registry as `ghcr.io/blindndangerous/autodj`. The
package is public, so pulling it needs no GitHub account or login. The project's GitHub Actions
build each image, run the same container smoke test and vulnerability scan as for every pull
request, then push it. The tags:

- `stable`, the default in `compose.yaml`: the newest release. `latest` is the same image.
- `dev`: the newest commit on `master`, published after every change to it.
- A version such as `0.22.0`: that release. It never changes.
- `sha-` and the first seven digits of a commit, such as `sha-0db0a92`: that development build.
  It never changes.

These images analyse songs on the processor. Each one also has a CUDA image for an NVIDIA
graphics card, tagged with `-cuda` added: `stable-cuda`, `dev-cuda`, `0.22.0-cuda` and
`sha-0db0a92-cuda`. There is no `latest-cuda`. See [Use an NVIDIA GPU](#use-an-nvidia-gpu).

To choose one, set `AUTODJ_IMAGE` in `.env` beside `compose.yaml`, then pull and start it:

```dotenv
AUTODJ_IMAGE=ghcr.io/blindndangerous/autodj:dev
```

```bash
docker compose pull
docker compose up -d
```

The image tells AutoDJ its update channel. An image published as `stable` follows Stable, and
one published as `development` follows Development, whichever checkout holds `compose.yaml`.
A channel chosen in **Settings**, **Updates** still wins; the commands `autodj check-update`
prints then say which image to name in `.env`.

The published image is for 64-bit x86 computers (`linux/amd64`). On an ARM computer, such as a
Raspberry Pi or an ARM NAS, [build the image yourself](#build-the-image-yourself).

To check that an image was built by AutoDJ's own workflow, verify its build provenance with the
[GitHub CLI](https://cli.github.com/manual/gh_attestation_verify):

```bash
gh attestation verify oci://ghcr.io/blindndangerous/autodj:stable --repo blindndangerous/AutoDJ
```

## Build the image yourself

Build the image from the checkout instead of pulling it, for example to run your own changes.
`compose.build.yaml` adds the build to `compose.yaml` and names the image `autodj:local`, which
Compose never pulls. To use it for every Compose command in this folder, add this line to `.env`:

```dotenv
COMPOSE_FILE=compose.yaml:compose.build.yaml
```

Then build and start AutoDJ, passing the [build stamp](#build-stamp):

```bash
AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Compose builds the image when it is missing or when you pass `--build`; otherwise `up` starts the
image you built last. podman-compose 1.0 does not read `COMPOSE_FILE` from `.env`. With it, run
`export COMPOSE_FILE=compose.yaml:compose.build.yaml` in the shell first, or name both files on
every command, for example
`podman compose -f compose.yaml -f compose.build.yaml up -d --build`. Without either, Compose
runs the published image instead.

An image you build follows the channel of the checkout it is built from: `master` is
Development, anything else is Stable.

## Use an NVIDIA GPU

Only song analysis uses a graphics card; playout and streaming run the same either way. The
default image analyses on the processor. The CUDA image, the same image with NVIDIA's CUDA build
of PyTorch, analyses on an NVIDIA card. It is about 7 GB unpacked, against about 2 GB for the
default image. Use it on the computer that runs AutoDJ, or keep the default image and run an
[analysis worker](gpu-worker.md#using-a-gpu-machine) on another computer that has the card.

The computer that runs the container needs:

- An NVIDIA card that CUDA 13.0 supports (Turing, the GeForce 16 and RTX 20 series, or newer),
  and an NVIDIA driver for CUDA 13.0, version 580 or newer. `nvidia-smi` shows the driver
  version.
- The [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
  set up for Docker as its guide says. Docker Desktop on Windows, with the WSL 2 backend, has GPU
  support built in and needs only the Windows NVIDIA driver.

`compose.gpu.yaml` runs the CUDA image's `stable-cuda` tag and asks Docker for every NVIDIA card.
To use it:

1. Add this line to `.env` beside `compose.yaml`:

   ```dotenv
   COMPOSE_FILE=compose.yaml:compose.gpu.yaml
   ```

   To follow development or stay on one release, also set `AUTODJ_IMAGE` to a `-cuda` tag, such
   as `ghcr.io/blindndangerous/autodj:dev-cuda`.
2. Pull and start the image:

   ```bash
   docker compose pull
   docker compose up -d
   ```

3. Check it: `docker compose exec autodj autodj doctor --offline` should report
   `gpu-image: CUDA image; song analysis uses the NVIDIA GPU`.

Doctor also says when the image and the card do not match. In the default image with a card
passed in, it suggests the CUDA image or a worker. In the CUDA image with no card visible, it says
that analysis falls back to the processor; the image still works there, only slower to analyse.

To go back to the default image, remove `compose.gpu.yaml` from `COMPOSE_FILE`, set
`AUTODJ_IMAGE` back to a tag without `-cuda` if you set one, then pull and start it again.

The CUDA image's tags follow the same rules as the default image's, and an update keeps the
`-cuda` suffix. When a release or a commit on `master` is published, its CUDA image can follow
by the length of its longer build, about two hours at most, or later if that build has to run
again. Until then `stable-cuda` and `dev-cuda` still pull the previous image, and a version or
`sha-` tag with `-cuda`, which `autodj check-update` may already name, makes `docker compose pull`
say `manifest unknown`. Try again later.

Podman reaches NVIDIA cards through the toolkit's CDI specification (`nvidia-ctk cdi generate`)
instead. This setup has not been tested with Podman. If your podman-compose does not pass the
card in, copy `compose.gpu.yaml` to a file of your own, replace its `deploy` section with
`devices: ["nvidia.com/gpu=all"]`, and name that file in `COMPOSE_FILE` in place of
`compose.gpu.yaml`. Another override added after `compose.gpu.yaml` cannot remove its `deploy`
section.

To build the CUDA image yourself, add `AUTODJ_TORCH_VARIANT=cuda` to `.env` and name
`compose.build.yaml` last, so the local image replaces the published one:

```dotenv
AUTODJ_TORCH_VARIANT=cuda
COMPOSE_FILE=compose.yaml:compose.gpu.yaml:compose.build.yaml
```

On a computer without an NVIDIA GPU, for example to build the CUDA image for another computer,
leave `compose.gpu.yaml` out of `COMPOSE_FILE`, and out of the update commands that
`autodj check-update` prints for a CUDA build: Docker cannot start a container that asks for a GPU
the computer does not have.

## Update the container

AutoDJ in a container checks for updates like any installation and says in **Settings**,
**Updates** when one is available, with a link to its release notes and to this section. It
cannot update itself from the page yet, and nothing in the Compose file gets the Docker or Podman
socket. `autodj check-update` in the container prints the commands for its image. Read
the release notes, make a backup, then, on the computer that runs the container, in the folder
with `compose.yaml`, run:

```bash
docker compose pull
docker compose up -d
```

`pull` downloads the newest image for the tag in `AUTODJ_IMAGE`, and `up` starts a new container
from it. The library, settings and models stay in their folders. With Podman, use
`podman compose` in place of `docker compose`. `docker image prune` removes the replaced image.

A version or `sha-` tag never changes, so pulling it changes nothing: set `AUTODJ_IMAGE` in `.env`
to the new tag first. To switch channels, set `AUTODJ_IMAGE` to the `stable` or `dev` tag
the same way. For the CUDA image, use the same tags with `-cuda` added; the commands
`autodj check-update` prints name them.

Pulling updates AutoDJ itself. Keep the checkout matched to the image too, so `compose.yaml` and
the example settings fit it: for a release, run `git fetch --tags origin` and
`git switch --detach vX.Y.Z` with the new version; for the development image, `git pull` on
`master`.

If you [build the image yourself](#build-the-image-yourself), update the checkout and rebuild
instead. For a release:

```bash
git fetch --tags origin
git switch --detach vX.Y.Z
AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

The rebuild replaces the running container, which gets the
[time to stop](#stop-the-container) set in `compose.yaml`.

For the development channel, replace the two `git` lines with `git fetch origin`,
`git switch master` and `git merge --ff-only origin/master`.

### Move an installation that builds its own image

Before AutoDJ published images, `compose.yaml` built `autodj:local` with
`docker compose up -d --build`. Once the checkout has the new `compose.yaml`, that command starts
the published Stable image instead. If you followed the development channel, Stable can be older
than what you ran, and an older AutoDJ may read newer settings differently (see
[Going back to an older release](upgrade.md#going-back-to-an-older-release)). Choose one way
before you run it:

- **Use the published image.** Make a backup and update the checkout. For the development
  channel, add `AUTODJ_IMAGE=ghcr.io/blindndangerous/autodj:dev` to `.env`. Then run
  `docker compose pull` and `docker compose up -d`, and remove the old image with
  `docker image rm autodj:local`.
- **Keep building it yourself.** Add `COMPOSE_FILE=compose.yaml:compose.build.yaml` to `.env`,
  then update as before.

**Restart AutoDJ** works in a container when the Compose file says how the container restarts.
`compose.yaml` sets `restart: unless-stopped` and, beside it, the environment variable
`AUTODJ_RESTART_POLICY: unless-stopped`; AutoDJ exits and the restart policy starts it again. A
container cannot see its own restart policy, so without that variable (`always`,
`unless-stopped` or `on-failure`) the page offers no Restart and says how to turn it on. If you
copied an older `compose.yaml`, add the variable with the same value as `restart:`.

## Build stamp

The footer of the web page names the commit the server runs, linked to that commit on GitHub,
once the browser is paired. A bare-metal checkout reads it from git. A published image carries
the commit it was built from. An image you build has no git, and its build leaves out `.git`, so
pass the commit when you [build it](#build-the-image-yourself):

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
