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

**Settings**, **Updates** shows the version AutoDJ runs, its update channel and the latest version on that channel. **Stable** follows releases (tags such as `v0.21.0`); **Development** follows the newest code on `master`. **Automatic** follows what the installation runs: a release tag is Stable, a checkout of `master` is Development. Choose **Check now** to ask again; AutoDJ otherwise reuses an answer for six hours.

A source checkout asks its `origin` remote with `git ls-remote`, which changes nothing in the checkout. Other installations ask GitHub. If the computer is offline, the page says it could not check; AutoDJ keeps working. Turn off **Check for updates** for an offline or private installation: AutoDJ then never asks. `autodj check-update` gives the same answer in a terminal, and `autodj doctor` reports it as its `updates` check; `autodj doctor --offline` skips it.

AutoDJ does not update itself. When an update is available, the page and `autodj check-update` link its release notes (the release's GitHub page, or the changelog on `master` for Development) and list the commands that update this installation. Read the Removed and Changed sections first, make a backup, then run the commands yourself, following [Update a source checkout](#update-a-source-checkout) or, for Docker and Podman, [Update the container](operations/containers-and-windows.md#update-the-container).

**Restart AutoDJ** stops AutoDJ and starts it again. Every station's stream goes off the air until it is back, usually a few seconds plus the time AutoDJ takes to load the library. The button appears only when something will start AutoDJ again:

- the `./autodj` or `.\autodj.cmd` launcher of a source checkout, which starts `serve` again;
- a systemd service with `Restart=on-failure` or `Restart=always` whose main process is AutoDJ itself (not a wrapper such as `uv run`);
- a Docker or Podman container whose restart policy is declared in `AUTODJ_RESTART_POLICY`, as `compose.yaml` does (see [Update the container](operations/containers-and-windows.md#update-the-container)).

Otherwise the page says to restart AutoDJ the way you started it. AutoDJ exits with status 75 to ask for a restart; a service with `Restart=on-failure` treats that as a failure and starts it again. Restarts are limited to one a minute and six an hour. A running Library tools job is stopped, as its **Stop** button does, and Ctrl+C or a stop from the service manager during the restart stops AutoDJ instead.

## Update a source checkout

1. Make a backup and stop AutoDJ.
2. Check `git status --short`. Save local changes before switching versions.
3. Fetch the release and switch to its tag, then let launcher setup verify or adopt its runtime. Do not sync a GPU runtime against the CPU development lock:

```bash
git fetch --tags origin
git switch --detach vX.Y.Z
./autodj setup
./autodj doctor --no-fix
```

On Windows, use `.\autodj.cmd setup` and `.\autodj.cmd doctor --no-fix`. The [core operations guide](core-operations.md#update-and-keep-a-rollback-path) gives both shell paths and rollback precautions. Read the changes since your version in [CHANGELOG.md](../../CHANGELOG.md), especially removed settings. Start AutoDJ and confirm the version in the page footer. For a wheel installation, replace `X.Y.Z` with the new release and use either command in the same environment:

```bash
uv pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/download/vX.Y.Z/autodj-X.Y.Z-py3-none-any.whl"
python -m pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/download/vX.Y.Z/autodj-X.Y.Z-py3-none-any.whl"
```

See [Release artifacts](install.md#release-artifacts) for verification. For Docker, update the checkout, then run `docker compose up -d --build`; the Compose file gives the old container [time to stop](operations/containers-and-windows.md#stop-the-container). After a bind-mounted configuration change alone, run `docker compose restart autodj`.

On first start, the current catalog's older layout moves into shared and station folders. AutoDJ moves older station settings from `config.toml` into each `station.json`, and imports station history from `library.db` into station databases. A published schema-3 or schema-4 index needs `autodj migrate-index --apply` before serving; preview with `autodj migrate-index` first. Check the result with Doctor. The [migration notes](../operations.md#moved-files) cover older indexes.

Prepared editions and their source recordings are included in normal backups.
Their derived programme-review files are excluded; download exports that need
independent retention. See [edition retention](operations/prepared-editions.md#retention-and-backup).

## Uninstall

Stop AutoDJ and keep a backup if you might return. A source checkout can be removed with its `.venv`, optional `node_modules`, data folder `index`, model folder `models`, and `config.toml`, `config.local.toml`, or `.env`. Keep any music you copied there if it is your only copy. A Hugging Face model cache may also live under `~/.cache/huggingface` or `HF_HOME`.

For Docker, `docker compose down --volumes` removes containers and Compose named volumes, including `autodj-index` and `autodj-models` when present. Compose may prefix volume names with the project name. Bind-mounted music, index, and model folders remain on the host; remove them separately only if you no longer need them. Remove the local image with `docker image rm autodj:local` if desired. For a wheel install, run `uv pip uninstall autodj` or `pip uninstall autodj` in the environment where you installed it. AutoDJ installs no system service by default.

AutoDJ and this page were developed with AI assistance.
