# Back up and update AutoDJ

Make a backup before an update, a move, or a change to station files. Keep it outside the AutoDJ data folder and test that AutoDJ can read it. The [operations reference](../operations.md#backup) lists every archived file and restore restriction.

## Make a backup

Run one command while AutoDJ is installed:

```bash
./autodj backup backups/night-owl.zip
```

On Windows, use `.\autodj.cmd backup backups\night-owl.zip`. If you run the installed command directly, use `autodj backup backups/night-owl.zip`. Backup copies the shared library, analysis, saved cue points, station settings and histories, schedules, shows, categories, carts, liners, ad spots, and uploaded presets. It uses a consistent database snapshot even when AutoDJ is playing. An existing archive is not overwritten unless you pass `--force`.

The archive does not include music files, recordings, external playlist files, downloaded model weights, `config.toml`, or `config.local.toml`. It also leaves out `secrets.json`, `relays.json`, Stereo Tool licence keys, pairing secrets, live DJ passwords, and other credentials. Keep separate secure copies of configuration, passwords, music, playlists, and recordings that you need to restore.

Derived [programme review snapshots](../operations.md#programme-review-and-export)
are also excluded. Download MP3/WAV exports separately when they must survive a
restore; a completed review otherwise remains in its station folder until you
explicitly delete it.

## Restore a backup

1. Install the same AutoDJ major and minor version that made the backup. A backup from 0.19.x restores on 0.19.x, for example.
2. Stop AutoDJ and any library jobs started from another terminal. Restore refuses while AutoDJ is serving.
3. First rehearse the restore in a new, isolated destination using the [PowerShell or Bash recovery procedure](core-operations.md#rehearse-recovery-into-a-new-destination). Restore with `autodj restore backups/night-owl.zip`; on a source checkout, use `./autodj restore backups/night-owl.zip` or `.\autodj.cmd restore backups\night-owl.zip` on Windows. Keep the default refusal to overwrite existing files.
4. Restore your music, configuration files, external playlists, and secrets separately. Check that their paths still match the new computer.
5. Run `autodj doctor`, then start AutoDJ. Re-pair browsers on a new machine. Set live DJ passwords again. Check a station's settings, queue, liners, and playback before discarding the archive.

Only add `--force` when deliberately replacing known destination data after preserving its backup and configuration. It replaces files held in the archive. A station absent from the archive stays in place. Recordings stay outside the backup. If you use Docker, stop the Compose service before restoring and bring it back afterward. Run backup and restore against the same data mount.

## Check for updates and restart

**Settings**, **Updates** shows the version AutoDJ runs, its update channel and the latest version on that channel. **Stable** follows releases (tags such as `v0.21.0`); **Development** follows the newest code on `master`. **Automatic** follows what the installation runs: a release tag is Stable, a checkout of `master` is Development, and a published container image follows the tag it was published as (`stable` or `development`). Choose **Check now** to ask again; AutoDJ otherwise reuses an answer for six hours.

A source checkout asks its `origin` remote with `git ls-remote`, which changes nothing in the checkout. Other installations ask GitHub. If the computer is offline, the page says it could not check; AutoDJ keeps working. Turn off **Check for updates** for an offline or private installation: AutoDJ then never asks. `autodj check-update` gives the same answer in a terminal, and `autodj doctor` reports it as its `updates` check; `autodj doctor --offline` skips it.

When an update is available, the page says **AutoDJ X is available** and links its release notes: the release's GitHub page, or for Development GitHub's list of the changes on `master` since the commit you run. Read the Removed and Changed sections first. A source checkout started with its launcher can then install it with **Update AutoDJ**; see [Update from the page](#update-from-the-page). Any other installation gets one sentence, "Update it the way you installed it", and a link to its guide: [Update a source checkout](#update-a-source-checkout), [Update a wheel installation](#update-a-wheel-installation) or, for Docker and Podman, [Update the container](operations/containers-and-windows.md#update-the-container). `autodj check-update` still prints the commands that update this installation by hand.

AutoDJ never updates without someone pressing **Update AutoDJ**.

**Restart AutoDJ** stops AutoDJ and starts it again. Every station's stream goes off the air until it is back, usually a few seconds plus the time AutoDJ takes to load the library. The button appears only when something will start AutoDJ again:

- the `./autodj` or `.\autodj.cmd` launcher of a source checkout, which starts `serve` again;
- a systemd service with `Restart=on-failure` or `Restart=always` whose main process is AutoDJ itself (not a wrapper such as `uv run`);
- a Docker or Podman container whose restart policy is declared in `AUTODJ_RESTART_POLICY`, as `compose.yaml` does (see [Update the container](operations/containers-and-windows.md#update-the-container)).

Otherwise the page says to restart AutoDJ the way you started it. AutoDJ exits with status 75 to ask for a restart; a service with `Restart=on-failure` treats that as a failure and starts it again. Restarts are limited to one a minute and six an hour. A running Library tools job is stopped, as its **Stop** button does, and Ctrl+C or a stop from the service manager during the restart stops AutoDJ instead.

## Update from the page

**Update AutoDJ** appears in **Settings**, **Updates** while an update is available, when AutoDJ is a source checkout started with `./autodj serve` (Linux and macOS) or `.\autodj.cmd serve` (Windows). A container cannot update from the page yet.

1. Read the release notes the page links.
2. Choose **Update AutoDJ**. The confirmation says every station's stream goes off the air while AutoDJ updates, usually for a few minutes. Choose **Update**.
3. Wait. The page says once how it went: "Updated to X", or "The update failed, so AutoDJ went back to Y" and the reason. **Last update** keeps that sentence for a day.

While no server runs, the launcher follows the [upgrade checklist](operations/upgrade.md#upgrade-checklist) for you, in the window AutoDJ runs in, where it prints each step:

1. It checks again for your channel. It refuses an older version, or the same one.
2. It refuses when `git status --porcelain` lists anything: a changed file, or a file git does not track. Save those changes on a branch, or move the files out of the AutoDJ folder, then try again.
3. It makes a backup with `autodj backup` in `.uv/update/backups` in the AutoDJ folder. It keeps the three newest of these.
4. For Stable it runs `git fetch --tags origin` and `git switch --detach vX.Y.Z`. For Development it runs `git fetch origin master`, `git switch master` and `git merge --ff-only origin/master`.
5. It runs `setup --yes`, which checks the saved runtime without changing its packages.
6. It runs Doctor. If the library needs a migration, it does not migrate: it goes back, and the page lists the commands that migrate by hand.
7. It starts the new version. The new version must answer `/healthz` within 3 minutes, or AutoDJ goes back.

Going back puts the checkout on the commit it was on, puts the runtime selection back, runs setup for it, and starts the old version. If the new version had already started, AutoDJ also restores the backup it made just before, since the new version may have changed the data as it started; changes made while the new version ran are lost, and **Last update** says so. Ctrl+C during that time does not restore: **Last update** gives the `restore` command to run before starting AutoDJ. If even that fails, AutoDJ does not start; the window and, after the next start, **Last update** say where the checkout is and list the commands to put it right. Ctrl+C or a stop from the service manager during an update puts the checkout back and stops AutoDJ. Updates are limited to one a minute and six an hour.

A release that needs new Python packages may not start after setup, since setup keeps the runtime's packages; AutoDJ then goes back. Run `./autodj setup` by hand, as in [Update a source checkout](#update-a-source-checkout), to see why.

### Move from Development to Stable

Choose **Stable** under **Update channel** and save. AutoDJ does not move back to an older release, because a newer version's changes to your library may not be reversible. The page says "You'll move to Stable when the next release includes your version", with the version you run and the latest release. When a release includes your commit, it is the update, and **Update AutoDJ** moves you to it. Doctor and `autodj check-update` say the same.

From Stable to Development, choose **Development** and update: `master` is newer than the release.

### Go back to the release you came from

When **Update AutoDJ** moved this installation from a release to Development, it kept the backup it made just before. On the Stable channel, **Go back to Stable now** offers to go back to that release with that backup. The confirmation names the version and the day of the backup: settings, play history and library changes made since that day are lost. AutoDJ backs up the data as it is now, switches to the release, runs its setup, restores the backup with `autodj restore`, runs Doctor and starts it, and goes back to Development with the new backup if that fails.

The page offers it only for that backup. When the backup is missing or cannot be read, the page says so in one sentence and does not offer it. A later release reached from Development forgets it.

## Update a source checkout

These are the steps by hand: for a checkout not started with its launcher, or to finish what the page could not.

1. Make a backup and stop AutoDJ.
2. Check `git status --short`. Save local changes before switching versions.
3. Fetch the release and switch to its tag, then let launcher setup verify or adopt its runtime. Do not sync a GPU runtime against the CPU development lock:

```bash
git fetch --tags origin
git switch --detach vX.Y.Z
./autodj setup
./autodj doctor --no-fix
```

On Windows, use `.\autodj.cmd setup` and `.\autodj.cmd doctor --no-fix`. The [core operations guide](core-operations.md#update-and-keep-a-rollback-path) gives both shell paths and rollback precautions. Read the changes since your version in [CHANGELOG.md](../../CHANGELOG.md), especially removed settings. Start AutoDJ and confirm the version in the page footer.

## Update a wheel installation

Make a backup and stop AutoDJ. Replace `X.Y.Z` with the new release and use either command in the same environment:

```bash
uv pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/download/vX.Y.Z/autodj-X.Y.Z-py3-none-any.whl"
python -m pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/download/vX.Y.Z/autodj-X.Y.Z-py3-none-any.whl"
```

See [Release artifacts](install.md#release-artifacts) for verification. For Docker, run `docker compose pull`, then `docker compose up -d`; the Compose file gives the old container [time to stop](operations/containers-and-windows.md#stop-the-container), and [Update the container](operations/containers-and-windows.md#update-the-container) covers pinned versions, channels and images you build yourself. After a bind-mounted configuration change alone, run `docker compose restart autodj`.

On first start, the current catalog's older layout moves into shared and station folders. AutoDJ moves older station settings from `config.toml` into each `station.json`, and imports station history from `library.db` into station databases. A published schema-3 or schema-4 index needs `autodj migrate-index --apply` before serving; preview with `autodj migrate-index` first. Check the result with Doctor. The [migration notes](../operations.md#moved-files) cover older indexes.

Prepared editions and their source recordings are included in normal backups.
Their derived programme-review files are excluded; download exports that need
independent retention. See [edition retention](operations/prepared-editions.md#retention-and-backup).

## Uninstall

Stop AutoDJ and keep a backup if you might return. A source checkout can be removed with its `.venv`, optional `node_modules`, data folder `index`, model folder `models`, and `config.toml`, `config.local.toml`, or `.env`. Keep any music you copied there if it is your only copy. A Hugging Face model cache may also live under `~/.cache/huggingface` or `HF_HOME`.

For Docker, `docker compose down --volumes` removes containers and Compose named volumes, including `autodj-index` and `autodj-models` when present. Compose may prefix volume names with the project name. Bind-mounted music, index, and model folders remain on the host; remove them separately only if you no longer need them. Remove the image with `docker image rm ghcr.io/blindndangerous/autodj:stable` (or the tag you chose), or `docker image rm autodj:local` if you built it yourself. For a wheel install, run `uv pip uninstall autodj` or `pip uninstall autodj` in the environment where you installed it. AutoDJ installs no system service by default.

AutoDJ and this page were developed with AI assistance.
