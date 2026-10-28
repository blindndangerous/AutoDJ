# 16. Check for updates and restart from the web page

Date: 2026-10-23
Status: Proposed
Decision authority: Project maintainer. The owner chose the two update channels and the
approach in [#552](https://github.com/blindndangerous/AutoDJ/issues/552); acceptance of this
record is pending.

## Context and scope

A user asked to update AutoDJ from the web page. The owner also wants a Restart button and a
Doctor check for updates. Today an operator updates a source checkout by hand with the
[upgrade checklist](../guides/operations/upgrade.md): stop, back up, check `git status`, fetch,
switch to the release tag, run the launcher's setup, run Doctor's migration check, start again.
A container is rebuilt with Compose. Nothing in AutoDJ can stop and start itself, and nothing says
when a newer version exists.

[#552](https://github.com/blindndangerous/AutoDJ/issues/552) follows other self-hosted programs:
the program reports that an update exists, and whatever installed it does the update. This record
covers the first two steps there: the update check and notice (Doctor, a CLI command and
Settings, Updates), and restarting from the web page. It changes how the source checkout's
launcher runs `serve` (an amendment to [ADR 12](0012-separate-runtime-setup-from-model-execution.md))
and adds browser routes inside the trust boundary of
[ADR 7](0007-separate-browser-worker-and-listener-access.md).

AutoDJ does not update itself under this record. Whether a source checkout started with the
launcher may update from the web page is a later decision in #552. A container gets the notice and
the exact commands; nothing gets the Docker socket. There are no user roles, no automatic updates
and no Windows service.

## Decision

### Channels and the check

- Two channels. **Stable** follows the release tags `vX.Y.Z`; **development** follows `master`.
- An installation infers its channel: a checkout of a release tag is stable, a checkout of
  `master` is development, and anything else is stable. A container reads the branch from the
  checkout its Compose file mounts; a published image follows its own channel instead (see the
  [2026-10-23 amendment](#amendment-2026-10-23-published-container-images)). A setting saved from Settings, Updates
  (`<index_dir>/shared/updates.json`) can name a channel instead.
- The setting can turn the check off. Then nothing contacts the network: not the page, not
  Doctor, not `autodj check-update`. `doctor --offline` skips the network for one run.
- A source checkout asks its `origin` remote with `git ls-remote`, which reads the same
  references `git fetch` would without changing the checkout, so Doctor still writes nothing.
  An installation without git asks GitHub's REST API, at an address fixed in the code. git runs
  without a terminal prompt, a credential window or an ssh prompt (the checkout's own
  `core.sshCommand` is kept, with `-o BatchMode=yes` added), with a 15-second timeout that kills
  its whole process tree: its own session on POSIX, its own job object on Windows. git's
  transport keeps the pipes open after git itself is killed, so after a timeout nothing waits for
  them. The API call has a 10-second timeout. Only one check runs at a time; page loads while
  it runs wait for it.
- A failure or no network is reported ("could not check"), never an error, and Doctor passes
  it: only an available update is a Doctor warning. The message names no remote address and
  repeats no git output, because a remote address can hold a password. Setup's and restore's
  Doctor runs do not check.
- The result says when the installation is ahead of the latest release, or on a branch other
  than `master`, which no channel follows. The web page caches a result for 6 hours, a failure
  for 10 minutes; Check now asks again, at most once in 10 seconds.

### The notice

- While an update is available, the page, `check-update` and Doctor say so. The page and
  `check-update` link its release notes, because the upgrade checklist asks operators to read the
  Removed and Changed sections: a release's GitHub page for a tag, the changelog on `master` for
  the development channel.
- They also list the commands that update this installation: the checklist's git and launcher
  commands for a checkout, the git and `docker compose` or `podman compose` commands for a
  container, and `uv pip install` of the release wheel for a package. AutoDJ never runs them.

### Restart

- The server exits with status 75 (`EX_TEMPFAIL`), the restart status. Something must start it
  again, and the page offers Restart only when something will:
  - the source checkout's launcher (`autodj`, `autodj.cmd`) now runs `serve` as a child process
    and starts it again when it exits with 75, on POSIX and on Windows. It passes SIGTERM on to
    the child, keeps waiting through Ctrl+C, and on Windows ties the child to the launcher with a
    job object. Five restarts within a minute stop the loop;
  - a systemd service whose `Restart=` is `always` or `on-failure` and whose main process is
    AutoDJ (or AutoDJ's own launcher, which marks its child), read with `systemctl show` for the
    unit in `/proc/self/cgroup`. A wrapper such as `uv run` may not pass on status 75;
  - a container that declares its restart policy in `AUTODJ_RESTART_POLICY`, as `compose.yaml`
    does beside `restart: unless-stopped`. A container cannot see its own policy, so a container
    without the variable gets no button.
- A stop signal always wins over a pending restart, in the server and in the launcher. On POSIX
  the server stays in the launcher's process group and the terminal's foreground, so Ctrl+C, a
  closed terminal and Ctrl+\\ reach it directly and it can still ask a question on the terminal.
  The launcher passes SIGINT, SIGHUP and SIGQUIT on only when they came to it alone (no terminal,
  or not in the foreground), since a second SIGINT makes uvicorn skip its graceful shutdown. It
  always passes SIGTERM on: uvicorn 0.54 treats a second SIGTERM like the first (only a repeated
  SIGINT forces an exit). Its handlers are in place before the server starts. After the server
  exits, the launcher ends with the same signal, so systemd sees a clean stop. A requested
  restart exits 75 without the failed-start hint or the Doctor offer. Shutting down stops a running
  Library tools job, so a restart does not leave one beside the next server.
- Anything else, including a Windows service, which AutoDJ does not support, gets no button and
  a sentence saying to restart AutoDJ the way it was started.
- The installed `autodj` command of a wheel does not loop; it is restarted only by a systemd
  service or a container.

### Who may do it

- These are browser routes like every settings change
  ([ADR 7](0007-separate-browser-worker-and-listener-access.md)): a paired browser session when
  pairing is on, an allowed Origin for writes, and no machine token. There are no user roles, so
  every paired browser may restart AutoDJ.
- Check and restart are audited under their own action names (`update-check`, `server-restart`)
  as well as by the request middleware.
- Restart is limited to one a minute and six an hour, counted in
  `<index_dir>/logs/restart-requests.json`, so a restart does not reset the count. The limit
  fails closed: when the file is there but cannot be read or parsed, or cannot be written, the
  restart is refused with a message that names the file.
- git and `systemctl` run from argument lists, never a shell string, and nothing from a request
  reaches their arguments. See the [threat model](../../THREAT_MODEL.md#updates-and-restart-from-the-web).

## Alternatives

- **Update from the web page.** Deferred to a later decision in #552.
- **`git fetch` for the check.** Gives a commit count, but writes to the checkout, which Doctor
  promises not to do.
- **Restart by sending the server a signal.** The server can stop itself through uvicorn's own
  shutdown; a signal reaches a launcher or a service manager differently on each platform.
- **Automatic updates.** Not asked for; a station should go off the air only when someone
  chooses it.

## Consequences

- A `serve` started through the checkout's launcher is now two processes: the launcher and the
  server. Stopping the launcher stops the server (SIGTERM is passed on; a Windows job object ends
  it with the launcher). A launcher killed with SIGKILL on POSIX leaves the server running.
- Every paired browser can take the station off the air for a moment with Restart. Pairing
  already grants playback control, which is at least as disruptive.

Revisit when AutoDJ gains user roles, a Windows service, or the later #552 decision on updating
from the web page.

## Amendment, 2026-10-23: published container images

Amendment status: Proposed; pending maintainer acceptance.
Decision authority: Project maintainer. The owner decided to publish the container image on
GitHub Container Registry. This replaces the line in
[#552](https://github.com/blindndangerous/AutoDJ/issues/552) that publishing prebuilt images was a
separate decision outside this work. The text above is kept; where it says a container is
rebuilt with Compose, this amendment applies to published images.

- The [image workflow](../../.github/workflows/image.yml) publishes
  `ghcr.io/<owner>/autodj`, with the owner taken from the repository. A push to `master` publishes
  `:dev` and `:sha-<short commit>`. A `vX.Y.Z` tag on `master`, after the release workflow
  has checked the tag, version and changelog, publishes `:X.Y.Z`, `:stable` and `:latest`; any
  other ref is refused, because no tag ruleset limits who may push a tag. `:latest` is the same
  image as `:stable`, so a pull without a tag never gets a development build. The GitHub release,
  which the update check reads, is published only after its image.
- A read-only job builds `linux/amd64` with a buildx layer cache, runs the container smoke test,
  writes an SBOM, fails on a fixable HIGH or CRITICAL Trivy finding and saves that image as an
  artifact. A second job, the only one with registry and attestation permissions and running no
  downloaded scanner, loads that exact image, pushes it and attests its digest with Sigstore, as
  the release workflow signs its other artifacts. Pull requests build and check the image
  without registry credentials.
- `compose.yaml` runs `${AUTODJ_IMAGE:-ghcr.io/<owner>/autodj:stable}` and passes that name to
  AutoDJ as `AUTODJ_IMAGE`. Building on the operator's computer is an opt-in override,
  `compose.build.yaml`, which names the image `autodj:local` with `pull_policy: never`. Neither
  file has both `build` and a published name, so `up` never builds over a published image or
  pulls one in place of a local build.
- A published image carries `AUTODJ_IMAGE_CHANNEL`, `stable` or `development`, and follows that
  channel rather than the mounted checkout's branch: the usual clone of `master` holding
  `compose.yaml` would otherwise make a stable image follow development. An image built locally
  leaves it empty and keeps the rule above. A saved channel still overrides both.
- For a published image the notice lists `docker compose pull && docker compose up -d` and the
  Podman equivalent. When pulling cannot reach the update (a version or `sha-` tag, a digest, or
  an image on the other channel), it first names the image to set in `.env`. An image built
  locally keeps the git commands, with both Compose files named. AutoDJ still runs none of them.
- Only `linux/amd64` is published. The CPU torch pins already list aarch64 wheels, but an arm64
  image needs either emulation, which is slow and cannot run the smoke test, or a native arm64
  runner and a manifest merge. ARM computers build the image themselves.

Consequences: an installation that built `autodj:local` and updates its checkout gets the
published Stable image from its old `docker compose up -d --build`. A development-channel
installation must set `AUTODJ_IMAGE` to `:dev` or opt into the build file first; the
[container guide](../guides/operations/containers-and-windows.md#move-an-installation-that-builds-its-own-image)
says so. The package must be public. GitHub's documentation says both that a package created by
a workflow inherits the repository's visibility and that a new package is private, so the
maintainer checks the package page after the first publish.

Verification limits: unit tests cover the channel and the commands. The workflow runs only on
`master` and on release tags, so its first push and attestation are checked after merge. The
Compose files were checked with `docker compose config` and `podman-compose config`; the switch
between them was run with podman-compose, not with Docker.

Revisit when ARM users need a published image, or if the development image should wait for the
full CI run.

## Amendment, 2026-10-27: update and go back from the web page

Amendment status: Proposed; pending maintainer acceptance.
Decision authority: Project maintainer. The owner decided step 3 of
[#552](https://github.com/blindndangerous/AutoDJ/issues/552): a source checkout started with its
launcher updates from the page, a container later through an opt-in helper on its host, and
the page and the routes stay the same for both. The text above is kept; where it says AutoDJ does
not update itself and the page lists the update commands, this amendment applies.

### Who updates, and when

- A person always starts an update; nothing updates on its own.
- The running server never replaces its own files: Windows locks them. Update makes the server
  write a request and exit with status 85 (`UPDATE_EXIT_CODE`, outside the `sysexits` range and
  below the shell's 126, so it means nothing else). The updater does the work while no server
  runs, then starts a server again.
- `update_support()` names the updater that applies, or says why none does. For now that is the
  checkout's launcher (`autodj`, `autodj.cmd`), which marks the server it supervises with
  `AUTODJ_UPDATER=launcher`; a launcher from before this amendment does not, so its server
  offers no Update. A wheel, a container and a checkout started any other way are told to update
  the way they were installed, with a link to the guide for their kind. A container helper later
  adds a mechanism; the page shows only the message and the link, so it does not change.
- The launcher counts an update as a restart for its five-a-minute limit.

### The steps

The launcher follows the [upgrade checklist](../guides/operations/upgrade.md#upgrade-checklist),
with the options given before `serve` (`--config`) passed to backup, restore and Doctor:

1. `check`: it checks again itself, for the channel the server follows. The target is never taken
   from the request. It refuses an older or the same version (`version_key`, and for
   Development that `HEAD` is an ancestor of `origin/master` and `master`'s own version is not
   lower), and a checkout where `git status --porcelain` prints anything, ignored nothing.
2. `backup`: `autodj backup` into `.uv/update/backups`; the three newest of its own are kept.
3. `fetch`: only the target, so a tag moved on the remote cannot stop it:
   `git fetch --no-tags origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z` for Stable (a target tag that
   differs from the checkout's own is named, with the commands to compare them), and
   `git fetch origin master` for Development.
4. `switch`: `git switch --detach vX.Y.Z`; or `git switch master` and
   `git merge --ff-only origin/master`. From Development to a release, only a release that
   includes `HEAD` (`git merge-base --is-ancestor`).
5. `setup`: the checkout's own `scripts/setup.py --yes`, run as `autodj setup` runs it. Since
   [ADR 12's 2026-10-27 amendment](0012-separate-runtime-setup-from-model-execution.md#amendment-2026-10-27-synchronize-setup-managed-runtimes),
   setup synchronizes a setup-managed runtime from the checked-out `uv.lock` and the selected
   backend's profile, so the new release's Python packages are installed, then verifies it,
   keeps an existing `config.toml` and runs Doctor offline. This is the documented unattended
   mode, so the launcher uses it rather than syncing packages itself: it never syncs a GPU
   runtime against the CPU lock. The rollback's setup synchronizes the runtime back to the old
   release's lock. A stop after this step does not run setup, so the result gives the setup
   command. An environment adopted with `--environment` keeps its packages unless
   `--manage-environment` opted it in; there a release that needs new packages fails its health
   check and is rolled back. On Windows, `--yes` may install a missing FFmpeg through WinGet, as
   it does by hand. Setup's time limit is an hour, since the sync may download packages.
6. `migration-check`: `autodj doctor --no-fix --json --offline`. A failed check whose repair is
   `migrate-index` stops the update and rolls back, with the commands to migrate by hand.
7. `start` and `health`: the new server starts. It is healthy when it still runs and answers
   `GET /healthz` with 200 and `{"status": "ok"}` within 180 seconds, asked on this computer
   (loopback for a server bound to every interface, the resolved address for a bound name) with
   a Host header the server's own policy accepts, its allowed hosts included, without a proxy,
   and over HTTPS without checking the certificate. The server writes that address into the
   request from its own bind address and policy; when there is none (a zone address, a name that
   does not resolve, no acceptable Host), the page offers no Update and says why. The updater
   accepts only loopback or an address this computer can bind. A shell script on a host can make
   the same request.
8. `rollback`, after any failure past the switch or an unhealthy start: the checkout goes back to
   its commit and branch (`git switch --force-create <branch> <commit>`, or `--detach`, with
   `--discard-changes` as a second try, since the tree was clean). A branch that moved from where
   the update left it, because someone committed on it meanwhile, is never reset: the checkout is
   detached at the old commit and the result says so, with the commands. Then `.uv/setup.json` is put back,
   setup runs for the old version, and the old version starts. Once the new version has
   started, it may have changed the data as it started (0.22.0 moved the index layout; schemas
   and settings files are rewritten), which Doctor's check before the start cannot see, so the old
   version also restores the backup from just before with `autodj restore --force`, and the
   result says the new version's changes to the data are gone. A rollback before the start leaves
   the data alone. When any of it fails, the launcher records where the checkout is and the
   commands to put it right, the restore included, and starts nothing.

Git runs with the update check's environment (no terminal prompt, no credential window,
`BatchMode=yes` ssh) and its messages are not passed on. Each step has a timeout that ends its
whole process tree; the launcher waits for it in half-second slices, so a stop is seen at once on
Windows too, and a step ended for a stop counts as failed. git commands that read or change the
checkout are never cut short. One update runs at a time on a checkout: the launcher holds an
operating-system lock on a file in the update folder, which ends with the launcher however it
ends; a launcher that finds it held leaves the running update's `state.json` alone and answers
only its own request, as refused, in `result.json`. Every supervising launcher also holds
`serving-<pid>.lock` and passes its process id to its server in `AUTODJ_LAUNCHER_PID`, since on
Windows a virtual environment's `python.exe` starts the real interpreter as a child of its own
and the server's parent is not the launcher. While another launcher serves from the same
checkout, the page offers no Update and the launcher refuses one, since that server would keep
running the old files. A lock file's owner deletes it as it stops, also before dying of a stop
signal; a launcher that starts deletes the ones nobody holds, whose launchers have ended, and
takes its own with a few short retries, checking on POSIX that the file it locked is still the one
at that path. A server's check never deletes one. A launcher started while an update runs from
its checkout refuses to serve the half-switched files and asks to be started again when the
update has finished. A name the server is bound to
resolves in the family uvicorn binds: IPv4 unless the host has a colon. A server that cannot even start counts as an unhealthy start. A stop signal during the update, or while the new version
starts, keeps the ADR's stop contract: the launcher lets `git switch` and `git merge` finish, stops
a longer step, puts the checkout and the runtime selection back, records it, and ends with that
signal. After the new version has started, it does not restore the data while stopping, since a
service manager could kill a restore half done; the result says the data may have changed and
gives the restore command. When it cannot put the checkout back, the result says where it is. A launcher killed
outright leaves `state.json` mid-step; the next start records that as a failure.

### From Development back to Stable

- A plain downgrade stays refused. With **Stable** saved as the channel and a version newer than
  the latest release, the check says `waiting-for-release` ("You'll move to Stable when the next
  release includes your version", with both versions). When a release includes the installed
  commit (`git merge-base --is-ancestor HEAD <tag>`; GitHub's compare view without git), it is
  `available`, even with the same version number. Doctor passes with the same sentence and
  `check-update` prints it.
- Go back is the one way to an older release. When an update moves a release to Development,
  the launcher keeps that update's backup as `autodj-go-back-<version>-<time>.zip` and records the
  tag, its commit, the backup's path and its time in `go-back.json`. On the Stable channel the
  page offers **Go back to Stable now**; its confirmation names the version and the date and says
  that settings, play history and library changes made since then are lost. It backs up the
  current data, switches to the tag, runs its setup, restores the backup with `autodj restore
  --force` (the version that made it), runs Doctor, and is watched and rolled back like an
  update; a rollback also restores the new backup. It is offered only for a backup this flow
  made, in its own folder, and only while `HEAD` is not itself a release and that release's
  commit is an ancestor of `HEAD`, so a checkout moved by hand drops it. Development that has
  passed a newer release still gets it: that is when someone waiting for Stable wants it. A
  missing or unreadable backup, or a moved checkout, is explained in one sentence. Using it, or
  the updater moving the checkout onto a release, forgets it.

### The contract

`autodj.update_contract`, standard library only, defines version 1 of the files a server and its
updater exchange. A helper on a container's host will read and write the same files.

- The update folder: the updater names it in `AUTODJ_UPDATE_DIR` for the server it starts. The
  launcher uses `.uv/update` in the checkout, since it cannot read the configuration to find the
  index folder. Without the variable the server uses `updates` in the index folder, which a
  container mounts.
- Every file is a JSON object of at most 64 KiB with `"contract": 1`, written atomically; a reader
  ignores a damaged file or another version.
- `request.json`, from the server: `id` (16 hex digits), `action` (`update` or `go-back`),
  `channel`, `requested_at`, and `health` (`scheme`, `address`, `port`, `host`, `path`
  `/healthz`). `target` and `from` are for the record; the updater never trusts them.
- `state.json`, from the updater after each step: the request's `id`, `action`, `channel`, `from`
  and `to` (`version`, `commit`, `label`), `backup`, `step` and `steps`.
- `result.json`, from the updater once: the same, plus `outcome` (`updated`, `rolled-back`,
  `refused`, `failed`, `stopped`), `reason`, `message` (the sentence the page says), `commands`
  (only with a failure) and `finished_at`.
- `go-back.json`, from the updater: `tag`, `version`, `commit`, `backup`, `made_at`.
- The steps, each `pending`, `running`, `done`, `skipped` or `failed`, always all listed: `check`,
  `backup`, `fetch` (an image pull for a container), `switch`, `setup`, `restore` (Go back only),
  `migration-check`, `start`, `health`, `rollback`.

### Who may do it

Update and Go back are browser routes like Restart: `POST /api/server/update` and
`POST /api/server/go-back`, a paired browser session when pairing is on, an allowed Origin, no
machine token, and no body but `{}`. They are audited as `server-update` and `server-go-back`,
share a limit of one a minute and six an hour in `<index_dir>/logs/update-requests.json` (which
fails closed like Restart's), and run one at a time: Restart, Update and Go back share one pending
claim, taken right after the check with no await between, and given back when the request is
refused. Update needs an update in the server's last check.

### The page

While an update is available and the installation can update, Settings, Updates says "AutoDJ X is
available", links the release notes and offers **Update AutoDJ**; it asks first in the Restart
dialog, saying the stream goes off the air, polls while AutoDJ is away for up to 4 hours (the
launcher's step time limits add up to less) with a quiet line saying how long it has worked, and
says the updater's sentence once ("Updated to X", or "The update failed, so AutoDJ went back to
Y" and the reason). After an update it offers to load the new version's page.
**Last update** keeps the sentence for a day, with the recovery commands only after a failure.
An installation that cannot update gets "Update it the way you installed it" and its guide, with
no link when there is no guide; the
default command list is gone from the page, and `check-update` keeps it. For the development
channel the release-notes link is GitHub's compare view from the installed commit to `master`,
since the changelog's Unreleased section stays empty between releases; without a full commit it is
`master`'s commit list.

### Alternatives

- **The server updates itself.** Windows locks the files of a running program, and a half-replaced
  server cannot roll itself back.
- **Sync the runtime's packages in the launcher.** Setup with a saved runtime deliberately does
  not, and syncing a GPU runtime against the CPU lock breaks it; setup's unattended mode is the
  documented path.
- **Migrate automatically.** Migrations may not be reversible; a person runs them.
- **Health from the process alone.** A server that runs but does not serve would pass, and a host
  script could not check it.
- **Move back to Stable at once.** It would run an older version on newer data; waiting for the
  next release, or Go back with the backup from before the move, does not.
- **Go back with any backup.** A backup from elsewhere may not match the release; only the one
  this flow made is offered.
- **Files in the checkout for every updater.** A container helper cannot see the checkout, but it
  can see the index folder, so the folder is named by the updater.

### Consequences

A station goes off the air for the length of an update, a few minutes plus setup and Doctor, and
longer when a failure rolls back. A server that keeps being asked to update counts toward the
launcher's restart limit. Two servers started from one checkout cannot both update it; the second
is refused while the first holds the lock, and a server still running from the old files is not
detected. Each update keeps a backup in the checkout's `.uv/update/backups`, three of them plus the
one kept for Go back; deleting `.uv` deletes them.

Verification limits: unit tests stand in for git, setup, Doctor, backup, restore and the new
server, with a real HTTP server for the health probe. No update has been run against a real
checkout, network or GPU runtime; Windows' job objects and Ctrl+C during an update, macOS, and the
Playwright audit and a screen reader on the new controls have not been exercised for this
amendment.

Revisit when the container helper lands, when AutoDJ gains user roles, or if setup learns to sync
a runtime's packages safely.

## Amendment, 2026-10-28: an opt-in CUDA image

Amendment status: Proposed; pending maintainer acceptance.
Decision authority: Project maintainer. The owner decided to keep the CPU image as the default and
to publish an opt-in CUDA image with `-cuda` tags, run with the NVIDIA Container Toolkit, with a
Doctor hint when the image and the GPU do not match. The text above is kept; where it lists the
published tags, this amendment adds the CUDA image's.

Only song analysis (MuQ) uses a GPU; playout does not. Bare-metal setup already installs CUDA
torch on NVIDIA machines, and a container could already use a GPU only through a remote analysis
worker (`autodj worker`). The CPU image stays the default because the CUDA image is about three
times its size and most container hosts, NAS boxes among them, have no NVIDIA card.

- One Containerfile takes `TORCH_VARIANT=cpu|cuda`. `cuda` runs the locked sync without skipping
  anything, so torch, the `nvidia-*` and `cuda-*` wheels and `triton` are the PyPI builds
  `uv.lock` pins and hash-checks. Those wheels carry the CUDA 13.0 runtime, so no `nvidia/cuda`
  base image is used, and `uv.lock` is the CUDA image's only pin: no second requirements file can
  drift from it. A torch upgrade still updates `container/torch-cpu.txt` for the CPU image. Both
  images now build with `UV_NO_CACHE`, so uv's download cache is no longer copied into the image.
  The image records its variant in `AUTODJ_IMAGE_VARIANT`.
- The CUDA image's tags are the CPU image's with `-cuda` added: `:dev-cuda` and
  `:sha-<short>-cuda` from `master`, `:X.Y.Z-cuda` and `:stable-cuda` from a release tag, under
  the same ref rules. There is no `:latest-cuda`: `:latest` exists only so that a pull without a
  tag gets a release, and such a pull can never name the suffix.
- [image.yml](../../.github/workflows/image.yml) takes a `variant` input and builds, smoke-tests,
  scans, saves, pushes and attests the CUDA image with the same two jobs. The smoke test runs on
  the CPU, since runners have no GPU: it checks that torch is the CUDA build and that AutoDJ falls
  back to the CPU. Its build frees runner disk first and fails before building or pushing when
  less than 35 GiB is free (20 GiB for the push job), and builds without the Actions layer cache,
  whose repository limit its layers would fill.
- A CUDA failure never blocks or withdraws the CPU image or the GitHub release.
  [image-cuda.yml](../../.github/workflows/image-cuda.yml) runs the CUDA image for `master` as a
  workflow of its own. The release workflow runs it as a job that nothing waits for: when it fails
  the run is marked failed, the CPU image and the release are still published, and the previous
  `:stable-cuda` stays until the maintainer reruns the failed jobs. `:stable-cuda` can therefore
  follow the release by its build time, during which the notice can offer an update that
  `docker compose pull` does not reach yet.
- A version or `sha-` tag never changes, for either image. The push job first asks the registry
  for it. When a rerun finds it, left by an attempt that stopped part way, the rebuilt image is
  not pushed, because a rebuild is not byte-identical: the moving tags (`:stable`, `:latest`,
  `:dev` and their `-cuda` forms) are pointed at the digest already there, with its manifest's
  exact bytes, and that digest is attested again, because the likeliest partial failure is the
  attestation itself. The warning in the run says it was reused. The token and manifest requests
  retry transient errors; any answer other than found or not found stops the job before it
  pushes.
- The moving tags never move backwards; the run notes each tag it leaves alone, and any error
  asking the registry or GitHub's API stops the job before it pushes. A release moves `:stable`,
  `:latest` and `:stable-cuda` only when its tag is the highest `vX.Y.Z` tag in git (by version,
  from GitHub's API). A newer tag pushed while an older release is still running, or a stray tag
  shaped like a release, therefore holds `:stable` back; the recovery is to rerun the release's
  image job once that tag has been released or deleted. A `master` run moves `:dev` or
  `:dev-cuda` when the tag does not exist yet, or when this commit is ahead of, or the same as,
  the commit the tag holds now: its image's `org.opencontainers.image.revision` label, compared
  through GitHub's compare API. Behind or diverged leaves it alone. So a slow CUDA build still
  moves `:dev-cuda` unless a newer commit's build already has, and a head commit that skipped CI
  does not hold it back.
- [compose.gpu.yaml](../../compose.gpu.yaml) is the opt-in override: it names
  `${AUTODJ_IMAGE:-ghcr.io/<owner>/autodj:stable-cuda}` and requests every NVIDIA GPU through
  `deploy.resources.reservations.devices`. It has no `build`, so the rule above holds; a local
  CUDA build sets `AUTODJ_TORCH_VARIANT=cuda` and names `compose.build.yaml` last.
- An update keeps the variant. `_image_switch` reads the variant from the installation, not the
  tag: a CUDA image is told a `-cuda` tag and a CPU image never is, whatever `.env` names. An image
  from before the variant was recorded keeps its tag's suffix. `AUTODJ_IMAGE_CHANNEL` still says
  only `stable` or `development`. When it names a CUDA version or `sha-` tag, a second line says
  that image may still be building and to try again later if pull says manifest unknown. A local
  CUDA build's rebuild command names `compose.gpu.yaml` too, because `-f` overrides `COMPOSE_FILE`.
  The update contract carries no image tag, so it is unchanged; a container helper that pulls must
  keep the running image's variant the same way.
- Doctor's `gpu-image` check runs only in a container and allocates nothing on a GPU. A GPU is
  visible when a `/dev/nvidia<N>` device exists or `nvidia-smi -L` answers within 5 seconds. A
  CPU image that sees one warns in one sentence to use the `-cuda` image or a GPU analysis
  worker; a CUDA image that sees none warns that analysis falls back to the CPU and says how to
  pass the GPU in; a CUDA image that sees one asks `torch.cuda.is_available()` in an isolated
  child process (`-I -W ignore`, so the working directory is not on its path) limited to 20
  seconds, and passes or warns about the driver. A timeout gets its own warning that the GPU check
  timed out; any other failure counts as a GPU PyTorch cannot use. Elsewhere it adds no line.

Measured on 2026-10-28 with rootless Podman 4.9 on `linux/amd64`, without a GPU: the CUDA image is
6.86 GB unpacked and 3.19 GB as the workflow's zstd artifact; the CPU image built the same way is
2.09 GB unpacked and 0.58 GB as the artifact, against 3.52 GB for the `:dev` image published
before `UV_NO_CACHE`.

Alternatives: an `nvidia/cuda` runtime base image duplicates the libraries the wheels already
carry and adds a second base to keep patched; PyTorch's `cu130` index would need a second pinned
file kept in step with the lock, for the same CUDA build; a matrix job in one workflow would let a
CUDA failure stop the CPU image's publish job.

Verification limits: unit tests cover the tags and Doctor's check. The CUDA image was built and
run on one Linux computer without a GPU: torch reported CUDA 13.0, fell back to the CPU, the server
answered `/healthz`, and Doctor gave the fallback warning. It has not been run with a GPU, the
NVIDIA Container Toolkit, Docker Compose's device request or Podman's CDI. The workflow runs only
on `master` and release tags, so the CUDA build's disk use on a runner, its first push and its
attestation are checked after merge.

Revisit when an arm64 CUDA image is wanted, when PyPI's torch moves off CUDA 13 (the driver and GPU
requirements in the container guide follow it), or if the CUDA image should be the default.

## Implementation and operational references

- [Image workflow](../../.github/workflows/image.yml), its
  [CUDA trigger](../../.github/workflows/image-cuda.yml), [compose.yaml](../../compose.yaml),
  [compose.build.yaml](../../compose.build.yaml) and [compose.gpu.yaml](../../compose.gpu.yaml).
- [Use an NVIDIA GPU](../guides/operations/containers-and-windows.md#use-an-nvidia-gpu) in the
  container guide.
- [Update check, notice and restart detection](../../src/autodj/updates.py),
  [web routes](../../src/autodj/updates_api.py), [launcher restart loop](../../src/autodj/launcher.py),
  [the launcher's update](../../src/autodj/checkout_update.py), the
  [update contract](../../src/autodj/update_contract.py),
  [Doctor's check](../../src/autodj/doctor.py) and the `check-update` command in
  [cli.py](../../src/autodj/cli.py).
- [Settings, Updates](../../src/autodj/static/modules/updates.js) in the
  [page](../../src/autodj/static/index.html).
- [Back up and update AutoDJ](../guides/backups-and-updates.md#check-for-updates-and-restart),
  [Update from the page](../guides/backups-and-updates.md#update-from-the-page) and the
  [upgrade checklist](../guides/operations/upgrade.md).

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression test is mapped;
structural validation does not execute it or accept this ADR.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0016-01` | A release-tag checkout follows stable, `master` follows development, a locally built container follows its mounted checkout, a published image follows the channel it was published for, a package follows stable; a saved channel overrides. | automated | `tests/unit/test_updates.py::test_a_tag_checkout_follows_stable_and_master_follows_development`; `tests/unit/test_updates.py::test_a_saved_channel_overrides_the_inferred_one`; `tests/unit/test_updates.py::test_detect_installation_reads_branch_and_release_tag_from_git`; `tests/unit/test_updates.py::test_a_container_follows_the_branch_of_its_mounted_checkout`; `tests/unit/test_updates.py::test_a_published_image_follows_the_channel_it_was_published_for`; `tests/unit/test_updates.py::test_a_package_without_git_follows_stable` |
| `ADR-0016-02` | With the check off in the settings, `doctor --offline` or `AUTODJ_UPDATE_CHECK=0`, nothing contacts git's remote or GitHub; setup's and restore's Doctor do not check; settings are validated and saved. | automated | `tests/unit/test_updates.py::test_settings_round_trip_and_damaged_files_give_defaults`; `tests/unit/test_updates.py::test_parse_settings_refuses_anything_else`; `tests/unit/test_updates.py::test_a_check_turned_off_contacts_nothing`; `tests/unit/test_updates.py::test_an_offline_check_contacts_nothing`; `tests/unit/test_doctor.py::test_doctor_offline_skips_the_update_check_network`; `tests/integration/test_updates_api.py::test_settings_are_saved_and_validated`; `tests/unit/test_doctor.py::test_restore_runs_doctor_offline`; `tests/unit/test_setup_flow.py::test_setup_runs_doctor_offline_with_a_time_limit` |
| `ADR-0016-03` | The check compares with the newest release tag or the head of `master`, says ahead and other-branch states, times out, kills git's whole process tree on timeout, reports a failure without the remote's address, runs one check at a time, and is cached. | automated | `tests/unit/test_updates.py::test_an_unreachable_remote_is_a_warning_without_its_address`; `tests/unit/test_updates.py::test_a_git_timeout_is_a_warning`; `tests/unit/test_updates.py::test_an_offline_package_check_is_a_warning`; `tests/unit/test_updates.py::test_stable_compares_with_the_newest_release_tag`; `tests/unit/test_updates.py::test_master_after_a_release_is_ahead_of_it_on_the_stable_channel`; `tests/unit/test_updates.py::test_development_compares_with_the_head_of_master`; `tests/unit/test_updates.py::test_a_branch_other_than_master_is_named_and_not_compared`; `tests/unit/test_updates.py::test_a_package_asks_githubs_latest_release`; `tests/unit/test_updates.py::test_a_container_compares_its_build_stamp_with_master`; `tests/unit/test_updates.py::test_the_api_address_cannot_leave_the_repository`; `tests/unit/test_updates.py::test_the_checker_reuses_a_result_until_forced`; `tests/unit/test_doctor.py::test_doctor_passes_an_unreachable_update_check_with_its_message`; `tests/unit/test_updates.py::test_a_timeout_ends_the_whole_process_tree_without_waiting_on_its_pipes`; `tests/unit/test_updates.py::test_git_never_asks_for_a_password`; `tests/unit/test_updates.py::test_the_checkouts_own_ssh_command_is_kept_and_told_not_to_ask`; `tests/unit/test_updates.py::test_the_operators_git_ssh_command_wins`; `tests/unit/test_updates.py::test_a_timeout_does_not_wait_for_a_pipe_holder_that_left_the_tree`; `tests/unit/test_updates.py::test_closing_a_checks_job_ends_exactly_the_processes_in_it`; `tests/unit/test_updates.py::test_callers_during_a_slow_check_wait_for_it_instead_of_starting_more`; `tests/integration/test_updates_api.py::test_page_loads_during_a_slow_check_share_it`; `tests/unit/test_doctor.py::test_check_update_cli_prints_the_result` |
| `ADR-0016-04` | Restart is offered only under the launcher loop, a systemd service that restarts on failure with AutoDJ as its main process, or a container that declares its restart policy; the checkout launcher restarts `serve` on status 75, never loops twice, and stops a server that keeps asking. | automated | `tests/unit/test_updates.py::test_restart_detection_prefers_the_launcher_loop`; `tests/unit/test_updates.py::test_restart_detection_trusts_a_container_only_with_its_restart_policy`; `tests/unit/test_updates.py::test_systemd_restart_needs_autodj_to_be_the_units_main_process`; `tests/unit/test_updates.py::test_systemd_counts_the_parent_only_when_it_is_autodjs_launcher`; `tests/unit/test_updates.py::test_restart_detection_reads_the_systemd_restart_policy`; `tests/unit/test_updates.py::test_restart_detection_without_a_supervisor_offers_nothing`; `tests/unit/test_updates.py::test_systemd_policy_comes_from_the_unit_in_the_cgroup`; `tests/unit/test_launcher.py::test_runs_serve_finds_the_command_after_global_options`; `tests/unit/test_launcher.py::test_the_restart_loop_starts_serve_again_on_the_restart_code`; `tests/unit/test_launcher.py::test_the_restart_loop_stops_a_server_that_keeps_asking`; `tests/unit/test_launcher.py::test_the_checkout_launcher_supervises_serve`; `tests/unit/test_launcher.py::test_serve_is_not_supervised_twice_or_outside_the_checkout_launcher`; `tests/unit/test_launcher.py::test_a_real_child_reports_its_exit_status`; `tests/integration/test_updates_api.py::test_restart_stops_the_server_and_is_audited`; `tests/integration/test_updates_api.py::test_restart_is_refused_when_nothing_would_start_autodj_again` |
| `ADR-0016-05` | A server that cannot stop itself offers no Restart. | automated | `tests/integration/test_updates_api.py::test_the_view_names_version_channel_and_restart` |
| `ADR-0016-06` | Only a paired browser with an allowed Origin reaches the routes; check and restart are audited; restart is limited to one a minute and six an hour across restarts, and Check now to one in 10 seconds; a restart is refused when its count cannot be read or saved. | automated | `tests/integration/test_updates_api.py::test_only_a_paired_browser_on_this_site_reaches_the_update_routes`; `tests/integration/test_updates_api.py::test_check_now_is_audited_and_rate_limited`; `tests/integration/test_updates_api.py::test_restart_stops_the_server_and_is_audited`; `tests/integration/test_updates_api.py::test_restart_is_rate_limited_across_a_restart`; `tests/integration/test_updates_api.py::test_the_restart_limiter_allows_six_an_hour`; `tests/integration/test_updates_api.py::test_restart_is_refused_when_its_count_cannot_be_saved`; `tests/integration/test_updates_api.py::test_a_restart_time_ahead_of_the_clock_still_counts`; `tests/integration/test_updates_api.py::test_an_unreadable_restart_count_refuses_and_names_the_file`; `tests/integration/test_updates_api.py::test_a_refusal_says_which_limit_applied_and_a_long_wait_in_minutes` |
| `ADR-0016-07` | Settings, Updates uses native labelled controls, quiet result lines and one status region; Restart asks first, says the stream goes off the air, keeps focus and says once whether AutoDJ came back. | automated | `tests/jsmodules/updates.test.js::uses native labelled controls, quiet result lines and one polite status region`; `tests/jsmodules/updates.test.js::asks before Restart, says it is going off the air, and says when it is back` |
| `ADR-0016-08` | Settings, Updates meets WCAG 2.2 AA in the browser audit and speaks as intended with a screen reader. | manual | Run the visual audit (it visits every Settings subtab) and sample the panel with NVDA; record browsers, screen reader, flows and limits in the pull request. Sampled on 2026-10-23 with NVDA 2026.2 and Firefox 158 on master e7b6ea5b: control names and states, Check now and its rate limit, saving the channel, the Restart dialog (focus on Cancel, Escape back to Restart) and a real restart through the launcher, ending with "AutoDJ is back". It found one wording defect ("1 seconds"), fixed with this row. No person listened; an agent read NVDA's speech. |
| `ADR-0016-10` | A stop signal wins over a pending restart in the server and the launcher; the server stays in the launcher's process group, the launcher passes SIGTERM on (other stop signals only when they came to it alone), has its handlers before the server starts and ends with the same signal; a requested restart exits 75 quietly; shutting down stops a running library job. | automated | `tests/integration/test_server.py::TestServeFunction::test_a_stop_signal_wins_over_a_pending_restart`; `tests/unit/test_launcher.py::test_a_stop_signal_ends_the_loop_even_when_the_server_asked_to_restart`; `tests/unit/test_launcher.py::test_a_signal_to_the_launcher_reaches_the_server_and_ends_the_launcher_with_it`; `tests/unit/test_launcher.py::test_a_stop_that_comes_while_the_server_starts_still_stops_it`; `tests/unit/test_serve_doctor_offer.py::test_a_requested_restart_exits_quietly_without_offering_doctor`; `tests/unit/test_jobs.py::TestJobShutdown::test_shutdown_stops_a_running_job_and_waits_for_it`; `tests/unit/test_jobs.py::TestJobShutdown::test_shutdown_kills_a_job_that_ignores_stop`; `tests/unit/test_jobs.py::TestJobShutdown::test_shutdown_when_idle_does_nothing`; `tests/integration/test_updates_api.py::test_shutting_down_stops_a_running_library_job` |
| `ADR-0016-09` | While an update is available, the notice links its release notes: a release tag's GitHub page, or for development GitHub's compare view from the installed commit to `master` (`master`'s commits when no full commit is known); `check-update` lists the commands for this kind of installation, which AutoDJ never runs, and the page lists none; a published image is pulled, and told which image to name when pulling cannot reach the update. | automated | `tests/unit/test_updates.py::test_an_available_release_links_its_release_notes`; `tests/unit/test_updates.py::test_the_notice_gives_the_commands_for_each_installation`; `tests/unit/test_updates.py::test_a_container_on_another_channel_or_pinned_is_told_which_image_to_name`; `tests/integration/test_updates_api.py::test_check_now_is_audited_and_rate_limited`; `tests/jsmodules/updates.test.js::links the release notes and offers Update, with no command list, while an update is available`; `tests/jsmodules/updates.test.js::names the release notes of a tag and the changes on master since this version` |
| `ADR-0016-11` | Update is offered only when an updater applies: for now a source checkout whose `serve` runs under a launcher that says it updates (`AUTODJ_UPDATER=launcher`), with a health check the updater will accept; `update_support()` names the mechanism or says why there is none, and every other installation is told to update the way it was installed, with a link to its guide. | automated | `tests/integration/test_updates_api.py::test_update_is_refused_when_this_installation_cannot_update`; `tests/integration/test_updates_api.py::test_update_is_not_offered_without_a_health_check_the_updater_accepts`; `tests/unit/test_checkout_update.py::test_without_an_updater_the_update_status_is_an_ordinary_exit`; `tests/unit/test_updates.py::test_only_a_checkout_under_an_updating_launcher_can_update` |
| `ADR-0016-12` | Server and updater exchange `request.json`, `state.json`, `result.json` and `go-back.json` (contract version 1) in the update folder the updater names, else `updates` in the index folder; each is written atomically and read only when valid and of this version; every step is reported, a step that does not apply as skipped; recovery commands come only with a failure; the page gets the last result for a day. | automated | `tests/integration/test_updates_api.py::test_the_page_gets_the_last_update_result_for_a_day`; `tests/integration/test_updates_api.py::test_without_an_updater_the_update_folder_is_in_the_index_folder`; `tests/unit/test_update_contract.py::test_the_update_folder_is_the_updaters_or_the_index_folders`; `tests/unit/test_update_contract.py::test_every_file_is_versioned_and_damage_reads_as_missing`; `tests/unit/test_update_contract.py::test_a_result_has_generic_steps_and_commands_only_on_failure`; `tests/unit/test_update_contract.py::test_an_id_is_lowercase_hex`; `tests/unit/test_update_contract.py::test_a_later_updaters_extra_steps_keep_its_sentence_and_commands` |
| `ADR-0016-13` | Healthy means the new server still runs and answers `GET /healthz` with 200 and `"ok"` within 180 seconds, asked at loopback or an address of this computer (a bound name is resolved in the family uvicorn binds, IPv4 unless the host has a colon; a zone address or another computer's is refused) with a Host header the server's own policy accepts, its allowed hosts included: plain HTTP a host script can make too. | automated | `tests/integration/test_updates_api.py::test_the_health_probe_passes_the_servers_own_host_check`; `tests/unit/test_checkout_update.py::test_a_new_version_that_is_not_healthy_is_rolled_back`; `tests/unit/test_update_contract.py::test_the_health_check_asks_this_computer_with_an_allowed_host`; `tests/unit/test_update_contract.py::test_the_health_probe_needs_200_and_ok_from_healthz`; `tests/unit/test_update_contract.py::test_a_name_resolves_the_way_uvicorn_binds_it` |
| `ADR-0016-14` | While no server runs, the launcher refuses a checkout with any change, backs up with `autodj backup`, fetches only the target (the one release tag, naming a tag that moved on the remote, or `master`) and switches (a detached release tag, or `master` fast-forwarded), runs `setup --yes` and Doctor's read-only check with the options given to `serve`, and stops and rolls back when the library needs a migration. | automated | `tests/unit/test_checkout_update.py::test_a_stable_update_follows_the_upgrade_checklist`; `tests/unit/test_checkout_update.py::test_a_development_update_fast_forwards_master`; `tests/unit/test_checkout_update.py::test_a_checkout_with_changes_is_refused_before_anything_changes`; `tests/unit/test_checkout_update.py::test_a_needed_migration_rolls_back_and_gives_the_commands`; `tests/unit/test_checkout_update.py::test_the_loop_updates_while_no_server_runs_then_starts_and_watches_it`; `tests/unit/test_checkout_update.py::test_global_options_are_the_words_before_serve`; `tests/unit/test_checkout_update.py::test_a_moved_release_tag_is_named_instead_of_blaming_the_network`; `tests/unit/test_checkout_update_git.py::test_a_release_updates_detached_even_when_another_remote_tag_moved`; `tests/unit/test_checkout_update_git.py::test_a_moved_target_tag_is_named`; `tests/unit/test_checkout_update_git.py::test_development_fast_forwards_master_and_a_rollback_resets_it`; `tests/unit/test_checkout_update_git.py::test_a_detached_commit_updates_to_the_release_that_includes_it_and_back` |
| `ADR-0016-15` | The launcher works out the target with its own check for the channel the server follows and never moves to an older or the same version; a request it cannot read changes nothing. | automated | `tests/unit/test_checkout_update.py::test_an_older_or_the_same_version_is_refused`; `tests/unit/test_checkout_update.py::test_development_older_than_the_installed_version_is_refused`; `tests/unit/test_checkout_update.py::test_a_request_it_cannot_read_changes_nothing`; `tests/unit/test_checkout_update_git.py::test_a_local_master_that_diverged_from_origin_is_refused_and_keeps_its_commit` |
| `ADR-0016-16` | A failure after the switch, or a new version that is not healthy, puts the checkout back on its previous commit and branch (never resetting a branch someone moved meanwhile), restores the runtime selection, runs setup for it and starts the old version; once the new version has started, the old version also restores the backup from just before (`autodj restore --force`) and the result says the new version's changes to the data are gone; when any of that fails, the result says where the checkout is, lists the commands including the restore, and nothing starts. | automated | `tests/unit/test_checkout_update.py::test_a_needed_migration_rolls_back_and_gives_the_commands`; `tests/unit/test_checkout_update.py::test_a_new_version_that_is_not_healthy_is_rolled_back`; `tests/unit/test_checkout_update.py::test_a_rollback_before_the_start_leaves_the_data_alone`; `tests/unit/test_checkout_update.py::test_a_restore_that_fails_after_the_start_reports_failed_with_the_commands`; `tests/unit/test_checkout_update.py::test_a_rollback_that_fails_says_where_the_checkout_is_and_starts_nothing`; `tests/unit/test_checkout_update.py::test_a_failed_rollback_ends_the_launcher_without_a_server`; `tests/unit/test_checkout_update.py::test_a_new_version_that_cannot_even_start_is_rolled_back`; `tests/unit/test_checkout_update.py::test_a_branch_moved_during_the_update_is_never_reset`; `tests/unit/test_checkout_update_git.py::test_development_fast_forwards_master_and_a_rollback_resets_it`; `tests/unit/test_checkout_update_git.py::test_a_rollback_never_resets_a_branch_someone_committed_on`; `tests/unit/test_checkout_update_git.py::test_a_detached_commit_updates_to_the_release_that_includes_it_and_back` |
| `ADR-0016-17` | A stop signal during the update, or while the new version starts, ends a running step within half a second (a step ended so is never a success, and reading the checkout is never cut short), puts the checkout back, records it and ends the launcher with that signal; after the new version started it does not restore while stopping, but the result says the data may have changed and gives the restore command; when it cannot put the checkout back, the result says where it is, and the next start records an update a killed launcher left, with its branch and a command to list that branch's commits before resetting it. | automated | `tests/unit/test_checkout_update.py::test_a_stop_signal_during_the_update_puts_the_checkout_back`; `tests/unit/test_checkout_update.py::test_a_stop_after_the_new_version_started_says_to_restore_the_data`; `tests/unit/test_checkout_update.py::test_a_stop_that_cannot_put_the_checkout_back_says_where_it_is`; `tests/unit/test_checkout_update.py::test_an_update_a_killed_launcher_left_is_recorded_at_the_next_start`; `tests/unit/test_checkout_update.py::test_a_stop_while_the_new_version_starts_puts_the_checkout_back`; `tests/unit/test_checkout_update.py::test_reading_the_checkout_is_never_cut_short_by_a_stop`; `tests/unit/test_checkout_update.py::test_an_interrupted_update_suggests_its_branch`; `tests/unit/test_checkout_update.py::test_a_stop_ends_a_running_step_within_a_moment` |
| `ADR-0016-18` | Update and Go back need a paired browser on an allowed Origin, take no options, are audited as `server-update` and `server-go-back`, share a limit of one a minute and six an hour in `<index_dir>/logs/update-requests.json`, and share one pending claim with Restart that is taken with no await after the check; one update runs per checkout, and a second request is answered as refused in `result.json` under its own id while the running update's `state.json` is left alone; none runs while another launcher serves from the same checkout, which a server tells apart from its own launcher by `AUTODJ_LAUNCHER_PID`; a launcher refuses to serve while an update runs from its checkout, takes its serving lock with a few retries, lets go of it before dying of a stop signal, and as it starts deletes serving lock files nobody holds; `serve` exits with status 85. | automated | `tests/integration/test_server.py::TestServeFunction::test_a_stop_signal_wins_over_a_pending_restart`; `tests/integration/test_updates_api.py::test_only_a_paired_browser_on_this_site_reaches_the_update_routes`; `tests/integration/test_updates_api.py::test_update_hands_the_launcher_a_request_and_exits_with_the_update_status`; `tests/integration/test_updates_api.py::test_update_takes_no_options`; `tests/integration/test_updates_api.py::test_update_is_refused_without_an_update_or_a_server_that_can_stop`; `tests/integration/test_updates_api.py::test_update_is_rate_limited_in_its_own_file`; `tests/integration/test_updates_api.py::test_update_is_not_offered_while_another_server_runs_from_this_checkout`; `tests/integration/test_updates_api.py::test_only_one_of_restart_and_update_is_ever_accepted`; `tests/integration/test_updates_api.py::test_a_refused_request_gives_the_claim_back`; `tests/integration/test_updates_api.py::test_a_refusal_says_which_limit_applied_and_a_long_wait_in_minutes`; `tests/unit/test_checkout_update.py::test_only_one_update_runs_on_a_checkout_at_a_time`; `tests/unit/test_checkout_update.py::test_an_update_is_refused_while_another_launcher_serves_from_this_checkout`; `tests/unit/test_checkout_update.py::test_a_starting_launcher_clears_serving_files_of_ended_launchers`; `tests/unit/test_checkout_update.py::test_the_serving_lock_is_tried_again_while_someone_looks_at_it`; `tests/unit/test_launcher.py::test_a_launcher_ended_by_a_signal_lets_go_of_its_serving_lock_first`; `tests/unit/test_launcher.py::test_a_launcher_refuses_to_serve_while_an_update_runs`; `tests/unit/test_update_contract.py::test_a_server_started_by_the_launcher_skips_its_own_launchers_lock` |
| `ADR-0016-19` | Development on a saved Stable channel is never moved back: the check says `waiting-for-release` until a release includes the installed commit (git ancestry, or GitHub's compare view without git), which is then the update; Doctor and `check-update` say the same. | automated | `tests/unit/test_checkout_update.py::test_development_moves_to_stable_only_with_a_release_that_includes_it`; `tests/unit/test_checkout_update.py::test_reaching_a_release_forgets_go_back`; `tests/unit/test_doctor.py::test_doctor_and_check_update_say_when_development_waits_for_a_release`; `tests/unit/test_updates.py::test_development_on_a_saved_stable_channel_waits_for_the_next_release`; `tests/unit/test_updates.py::test_an_installation_without_git_asks_github_whether_the_release_includes_it`; `tests/jsmodules/updates.test.js::says development waits for the next release on the Stable channel, with no Update` |
| `ADR-0016-20` | Go back is offered only on the Stable channel, only with the backup the updater made when it moved a release to development (recorded in `go-back.json`, kept in its own backups folder, forgotten when the updater moves onto a release), and only while `HEAD` is not a release and that release's commit is an ancestor of `HEAD`, development past a newer release included; otherwise, or when the backup is missing or unreadable, one sentence says why; it backs up the current data, switches to that release, runs its setup, restores the backup, runs Doctor and is watched and rolled back like an update. | automated | `tests/integration/test_updates_api.py::test_go_back_is_offered_with_its_date_and_hands_the_launcher_a_request`; `tests/integration/test_updates_api.py::test_go_back_without_its_backup_is_explained_and_refused`; `tests/integration/test_updates_api.py::test_go_back_is_not_offered_off_the_stable_channel_or_without_a_record`; `tests/integration/test_updates_api.py::test_go_back_moved_by_hand_is_explained`; `tests/unit/test_checkout_update.py::test_moving_a_release_to_development_keeps_its_backup_for_go_back`; `tests/unit/test_checkout_update.py::test_the_go_back_backup_is_kept_beside_three_ordinary_ones`; `tests/unit/test_checkout_update.py::test_go_back_restores_the_backup_with_the_release_it_came_from`; `tests/unit/test_checkout_update.py::test_a_failed_go_back_restores_the_data_from_before_it`; `tests/unit/test_checkout_update.py::test_go_back_without_its_backup_changes_nothing`; `tests/unit/test_checkout_update.py::test_reaching_a_release_forgets_go_back`; `tests/unit/test_checkout_update.py::test_go_back_is_refused_when_the_checkout_moved_by_hand`; `tests/unit/test_checkout_update.py::test_go_back_still_fits_development_past_a_newer_release`; `tests/unit/test_checkout_update_git.py::test_go_back_fits_development_but_not_a_release`; `tests/unit/test_update_contract.py::test_go_back_only_names_a_backup_this_flow_made`; `tests/unit/test_updates.py::test_go_back_fits_only_while_the_checkout_came_from_that_release` |
| `ADR-0016-21` | Settings, Updates offers Update only when an update is available and the installation can update; it asks first and says the stream goes off the air, polls for up to 4 hours while saying quietly how long it has worked, and says once how the update went, then offers to load the new version's page; commands appear only after a failure, under an h5 in Last update; Go back's confirmation names the version and the date and says what is lost; an installation that cannot update gets one sentence and its guide, with no link when there is no guide. | automated | `tests/jsmodules/updates.test.js::tells an installation that cannot update to update it the way it was installed`; `tests/jsmodules/updates.test.js::leaves the guide link out when there is no guide`; `tests/jsmodules/updates.test.js::says quietly that a long update is still working, and waits up to 4 hours`; `tests/jsmodules/updates.test.js::describes a refused restart on its button before the dialog closes`; `tests/jsmodules/updates.test.js::asks before Update, says the stream goes off the air, and says once how it went`; `tests/jsmodules/updates.test.js::says the update failed, why, and only then lists the commands for that failure`; `tests/jsmodules/updates.test.js::describes a refused update on its button before the dialog closes, until the next action`; `tests/jsmodules/updates.test.js::says a refusal aloud when focus did not come back to its button`; `tests/jsmodules/updates.test.js::keeps a refused Go back's note beside Go back, not Update`; `tests/jsmodules/updates.test.js::offers Go back with its version and date, and says what is lost before it starts`; `tests/jsmodules/updates.test.js::says in one sentence why Go back is not offered` |
| `ADR-0016-22` | The CUDA image records `AUTODJ_IMAGE_VARIANT`; an update keeps the variant: a CUDA image is told its own `-cuda` tag (`stable-cuda` for `latest`, which has no CUDA tag) and a CPU image never is, whatever `.env` names; an image without a recorded variant keeps its tag's suffix; a pinned CUDA tag comes with a line saying it may still be building; a local CUDA build's rebuild names `compose.gpu.yaml`. | automated | `tests/unit/test_updates.py::test_a_cuda_image_reads_its_variant_and_a_cpu_image_has_none_to_lose`; `tests/unit/test_updates.py::test_a_cuda_image_is_told_its_own_cuda_tag_and_a_cpu_image_never_is`; `tests/unit/test_updates.py::test_a_pinned_cuda_tag_may_still_be_building_and_a_local_cuda_build_keeps_its_gpu` |
| `ADR-0016-23` | Doctor's `gpu-image` check runs only in a container, allocates nothing on a GPU and asks torch only in a CUDA image that sees one, in a child process limited to 20 seconds whose failure or timeout counts as no usable GPU; a CPU image that sees a GPU is told in one sentence to use the `-cuda` image or a GPU analysis worker; a CUDA image without one is told it falls back to the CPU and how to pass the GPU in; a CPU image without a GPU gets no line. | automated | `tests/unit/test_doctor_gpu_image.py::test_outside_a_container_or_without_a_gpu_a_cpu_image_gets_no_line`; `tests/unit/test_doctor_gpu_image.py::test_a_cpu_image_that_sees_a_gpu_is_told_about_the_cuda_image_and_the_worker`; `tests/unit/test_doctor_gpu_image.py::test_a_cuda_image_without_a_gpu_falls_back_to_the_cpu_and_says_how_to_pass_it_in`; `tests/unit/test_doctor_gpu_image.py::test_a_cuda_image_with_a_gpu_asks_pytorch_whether_it_can_use_it`; `tests/unit/test_doctor_gpu_image.py::test_a_gpu_is_visible_by_its_device_file_or_nvidia_smi`; `tests/unit/test_doctor_gpu_image.py::test_pytorch_is_asked_in_a_child_with_a_time_limit_and_quietly`; `tests/unit/test_doctor_gpu_image.py::test_a_gpu_check_that_times_out_says_so_rather_than_blaming_the_driver`; `tests/unit/test_doctor_gpu_image.py::test_doctor_reports_the_gpu_image_check_inside_a_container` |
| `ADR-0016-24` | The CUDA image is published from `master` and release tags under the CPU image's ref rules with `-cuda` tags and no `latest-cuda`, after the same smoke test (on the CPU), SBOM, Trivy scan and attestation; it fails before building or pushing when the runner lacks disk, and its failure never blocks or withdraws the CPU image or the GitHub release; a rerun never moves an existing version or `sha-` tag of either image, only points the moving tags at its digest and attests it again; the moving tags never move backwards (a release only from the highest release tag, `:dev` only to a commit ahead of the one it holds); with the NVIDIA Container Toolkit and `compose.gpu.yaml`, analysis uses the GPU. | manual | After merge, read the first `CUDA container image` run on `master` and the first release run: the `-cuda` tags, the free-space line, the smoke, SBOM, Trivy and attestation steps, and that `publish` did not wait for `image-cuda`. When a run is rerun after a partial push, check that its version tag kept its digest. On a Linux computer with an NVIDIA GPU, the toolkit and Docker Compose, follow [Use an NVIDIA GPU](../guides/operations/containers-and-windows.md#use-an-nvidia-gpu) and check that Doctor passes `gpu-image`; record the GPU, driver, Compose version and results in the pull request. Not yet performed: this change was built and run only without a GPU. |

## Verification limits

Tests stand in for git, GitHub, systemd and the server's exit. They do not show that a real
systemd unit or a container runtime restarts AutoDJ. A restart through the Windows launcher was
run once by hand against a temporary server; record other runs separately when they are made.
No screen reader has sampled Settings, Updates. Update, Go back and Last update (the
2026-10-27 amendment) have not been through the browser audit or a screen reader yet.
