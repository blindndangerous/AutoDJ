# Back up and recover Practice safely

You will make a backup of Practice, inspect it without extracting it, and
restore it into a separate data folder. You will then test the recovered
rotation without starting another server. Your original station stays intact.

## Before you begin

Complete [Build your first station](first-station.md). Use its folder of at
least five basically analysed songs, the folder-based `Practice` category, and
the one-step `Practice` wheel with repeat rules off. Start with **Browser**
output and no listeners. Advanced analysis is unnecessary.

This worked example uses Bash on Linux, macOS, or WSL, a source checkout with
its dependencies installed, and `uv`. Run commands from that checkout. Keep
the same AutoDJ version throughout: restore accepts only the major and minor
version that made the archive. For other installation styles, see the
[native PowerShell and Bash procedures](../guides/core-operations.md).

You need enough free disk space for the ZIP, restored data, and restore's
temporary files. A **backup archive** is one ZIP containing saved application
state. It is not a copy of your music collection. Keep the original music
folder available at its existing path for the verification below.

The lesson creates a new folder under your home directory, containing the
archive, a small configuration file, and isolated restored data. It does not
copy configuration secrets or restore over the original station. Do not use
`--force` in this lesson. No server, indexing job, download, or paid request is
started by the recovery commands.

Pause with **Play or Pause** in **Now Playing**; check that elapsed time stops.
If another lesson left a show active, use **Stop show** and wait for it to stop.
Disable its scheduled events. Activate the original Practice wheel and check
**Settings**, **Song picking**, **Pick mode** is **Clockwheel**. The
[keyboard guide](../guides/keyboard-and-screen-reader.md) covers UI navigation.

## Record the starting state

Before making the copy, note three things in the browser:

1. Practice appears in **Categories** and its count is at least five.
2. Practice is active in **Clockwheels**, with one unrestricted category step
   that ignores repeat rules.
3. **History** contains the songs you heard in the first lesson.

These are concrete facts you can compare with recovered data. The backup covers
the whole configured installation, including other stations if present; this
lesson assumes the fresh practice installation from the first tutorial.

Find the configuration file used to start that installation. In the terminal,
set the variable below to its absolute path, replacing the example:

```bash
practice_config=/absolute/path/to/your/config.toml
test -f "$practice_config"
```

If the test exits unsuccessfully, correct the path before continuing. If your
installation uses defaults without a configuration file, leave out
`--config "$practice_config"` from the backup command only. Later commands must
keep their separate recovery configuration.

## Make a new archive

Create a unique folder, print its location, then make the backup:

```bash
recovery_dir=$(mktemp -d "$HOME/autodj-practice-recovery.XXXXXX")
printf '%s\n' "$recovery_dir"
uv run --frozen --no-sync autodj --config "$practice_config" \
  backup "$recovery_dir/practice.zip"
```

Record the printed folder path so you can find it after closing this shell.
Expect `Backup written:` followed by the archive path. AutoDJ can make this
snapshot while serving; database backup includes committed SQLite changes.

If backup reports that the library is absent, check that this is the same
configuration, working directory, and environment used by the Practice server.
Correct those inputs and retry into a fresh recovery folder. Do not create a
new empty library to silence the error. If the destination already exists,
choose a new folder rather than overwriting the archive.

The archive includes the catalog, station settings, history, categories,
clockwheels, schedules, shows, and declared uploaded station assets. It excludes
music files, external playlists, recordings, model weights, `config.toml`,
`config.local.toml`, and credentials such as pairing and relay secrets. Real
recovery needs separate secure copies of those excluded resources. We will
reuse the original music in place and create a minimal, secret-free configuration.

## Preview the copy without restoring

Run both read-only checks:

```bash
uv run --frozen --no-sync python -m zipfile -l "$recovery_dir/practice.zip"
uv run --frozen --no-sync python -m zipfile -t "$recovery_dir/practice.zip"
```

The first lists archive members. Look for `manifest.json`, a `library.db`,
station settings/history, categories, and clockwheel files. ZIP member paths
are archive names; they need not match the current on-disk folder layout.
The second should report `Done testing` without a corrupt-member error.
Neither command extracts or installs anything.

If testing fails, retain the failed archive for diagnosis and make another
backup in a new folder. Test that one before continuing. A readable ZIP proves
neither complete recovery nor compatibility with AutoDJ; restore performs its
own validation. The CLI has no restore dry-run option. This inspection is our
read-only preview, followed by a real restore into an empty destination.

## Prepare an isolated destination

Stop the original AutoDJ process with Ctrl+C in its terminal and wait until
the terminal prompt returns. Stop any separate indexing or analysis jobs too.
Keep the original data folder and configuration unchanged.

Create the recovery configuration. This command writes only inside the new
recovery folder and quotes paths for TOML:

```bash
uv run --frozen --no-sync python - "$recovery_dir" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
with (root / "recovery.toml").open("x", encoding="utf-8") as config:
    config.write("[index]\n")
    config.write("index_dir = " + json.dumps(str(root / "index")) + "\n")
    config.write("model_dir = " + json.dumps(str(root / "models")) + "\n")
print(root / "recovery.toml")
PY
```

Check the effective destination without repairing anything:

```bash
AUTODJ_INDEX_DIR="$recovery_dir/index" AUTODJ_MODEL_DIR="$recovery_dir/models" \
  uv run --frozen --no-sync autodj --config "$recovery_dir/recovery.toml" \
  doctor --json
```

The command may fail because this destination has no library yet. Read the
configuration details: `index_dir` and `model_dir` must be inside the printed
recovery folder. The explicit environment values prevent an existing path
override from redirecting this exercise. The config's separate directory also
keeps your usual adjacent `config.local.toml` out of this recovery.

If either path points at your original data, stop and correct the command.
Do not proceed just because Doctor produced a report.

## Restore and check the recovered rotation

Run the real restore, keeping the same isolated paths:

```bash
AUTODJ_INDEX_DIR="$recovery_dir/index" AUTODJ_MODEL_DIR="$recovery_dir/models" \
  uv run --frozen --no-sync autodj --config "$recovery_dir/recovery.toml" \
  restore "$recovery_dir/practice.zip"
```

Expect `Restored` with a file count, followed by Doctor's report. A missing-model
warning is expected here: weights were excluded and this folder is new. Resolve
required failures before proceeding. If restore refuses existing destination
files, do not add `--force`; create a new recovery folder and repeat the
configuration and preview steps with the archive you already made.

Now run a short **simulation**, an offline test using copied picker state:

```bash
AUTODJ_INDEX_DIR="$recovery_dir/index" AUTODJ_MODEL_DIR="$recovery_dir/models" \
  uv run --frozen --no-sync autodj --config "$recovery_dir/recovery.toml" \
  simulate --station default --picks 6 --seed 42
```

Read the report for the Practice clockwheel, Practice category, six picks,
and any silence or skipped-step findings. It uses the restored catalog and
station settings without loading model weights or changing on-air state.
If Practice is empty or the wheel is missing, compare the source configuration
and the backup's members before making another copy. If the music path is
unavailable, reconnect the original folder; do not prune missing songs.

This establishes that the recovered picker can read its saved rotation. It does
not establish audible playback, browser pairing, or recovery of excluded files.

## Try it: test a different random seed

Predict whether changing only `--seed 42` to `--seed 43` can change the original
station's history. Repeat the simulation with that one change and inspect its
report.

Expected answer: neither simulation changes the original station. They target
the isolated data and run on private picker state. The song order may differ
where choices tie; it need not differ. Explain the result using the destination
paths and the distinction between a simulated pick and an actual play.

## Stop and retain your recovery copy

All recovery commands have finished; no recovery server is running. Keep the
printed recovery folder and its ZIP. The original station retains its settings
and history. Restart it with its original launch command and configuration when
you are ready, then confirm Practice is still active and pause browser playback.
The restored copy has no new plays from these simulations.

No cleanup is required. If you later remove the recovery folder, you remove
both this exercise's archive and its restored copy; keep another verified backup
first. Do not remove your original data or music folder.

You have practised inspecting an archive and proving a separate recovery can
read its rotation. Use [Back up and update AutoDJ](../guides/backups-and-updates.md)
for real migrations and [restore operations](../operations.md#restore-and-validate)
for validation, interrupted restores, and exclusions.

AutoDJ and this page were developed with AI assistance.
