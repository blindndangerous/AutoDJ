# Run core operations in PowerShell or Bash

Use this guide for native source-checkout installation, diagnosis, backup,
recovery rehearsal and updating. Choose the block for your shell; run commands
one at a time and stop on failure. You need Git, disk space for the runtime and
an archive, and the version/configuration used by your station. Bash here means
Linux/macOS or WSL; PowerShell means native Windows, without WSL or Bash tools.
See [installation](install.md) for platform prerequisites and containers.

## Install and diagnose

Clone into a new folder. Replace `vX.Y.Z` with your chosen published release tag.
Setup can download Python/packages; review its plan. It does not download models
unless you explicitly choose a test import. Do not choose that option merely to
collect diagnostics.

```bash
git clone https://github.com/blindndangerous/AutoDJ AutoDJ
autodj_release='vX.Y.Z'
cd AutoDJ
git switch --detach "$autodj_release"
./autodj setup
./autodj doctor --no-fix
echo "$?"
```

```powershell
git clone https://github.com/blindndangerous/AutoDJ AutoDJ
$autodjRelease = 'vX.Y.Z'
Set-Location AutoDJ
git switch --detach $autodjRelease
.\autodj.cmd setup
.\autodj.cmd doctor --no-fix
$LASTEXITCODE
```

Windows setup offers missing uv/FFmpeg installation through WinGet. On Bash
platforms install uv/FFmpeg first using [setup instructions](../setup.md).
A verified runtime and a Doctor report are the expected result; a new empty
library may still have findings. Review them rather than running repairs blindly.
Start with `./autodj serve` or `.\autodj.cmd serve` when ready. Open the printed
loopback URL. Stop with Ctrl+C in that terminal and wait for shutdown. If a service
manager starts AutoDJ, stop that service and verify it has exited.

## Capture diagnostics and create a backup

These commands assume the checkout's normal configuration. If serving uses an
explicit file, put `--config 'PATH'` before `doctor`, `backup` and later `restore`.
See [diagnostic collection](collect-diagnostics.md) before sharing output.

```bash
./autodj doctor --json > doctor.json
mkdir -p ../autodj-backups
autodj_stamp=$(date +%Y%m%d-%H%M%S)
autodj_archive="$(cd ../autodj-backups && pwd)/station-$autodj_stamp.zip"
./autodj backup "$autodj_archive"
```

```powershell
.\autodj.cmd doctor --json | Set-Content -Encoding utf8 doctor.json
New-Item -ItemType Directory -Force ..\autodj-backups | Out-Null
$autodjStamp = Get-Date -Format yyyyMMdd-HHmmss
$autodjArchive = Join-Path (Resolve-Path ..\autodj-backups).Path "station-$autodjStamp.zip"
.\autodj.cmd backup $autodjArchive
```

Backup prints its archive path and refuses an existing file without `--force`.
Keep this no-overwrite default. The ZIP does not include music, configuration,
recordings, model downloads, external playlists/tools or credentials. Preserve
those separately, with restricted access for secrets. See [backup scope](backups-and-updates.md).

## Rehearse recovery into a new destination

Use the same AutoDJ major/minor version as the archive, preferably the exact
release. Stop serving and analysis/indexing before rehearsal. Keep the old data
and archive. The following creates a unique scratch folder and a minimal recovery
configuration; it does not use your production configuration or `--force`.

First inspect your active `AUTODJ_*` environment overrides locally. Do not print
or share secrets. Use a clean terminal without production overrides, especially
index, station paths, relays and credentials. Check archived station definitions
for external time-check folders and other external paths. Those must point to
separate rehearsal copies before use. If you cannot establish isolation, rehearse
on a separate machine without write access to production storage instead.

```bash
autodj_recovery=$(mktemp -d "${TMPDIR:-/tmp}/autodj-recovery.XXXXXX")
cat > "$autodj_recovery/recovery.toml" <<EOFCONFIG
[index]
index_dir = '$autodj_recovery/index'
model_dir = '$autodj_recovery/models'
EOFCONFIG
./autodj --config "$autodj_recovery/recovery.toml" restore "$autodj_archive"
./autodj --config "$autodj_recovery/recovery.toml" doctor --no-fix
```

```powershell
$autodjRecovery = Join-Path ([IO.Path]::GetTempPath()) ('autodj-recovery-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $autodjRecovery -ErrorAction Stop | Out-Null
$autodjRecoveryToml = $autodjRecovery.Replace('\', '/')
$autodjRecoveryConfig = @"
[index]
index_dir = '$autodjRecoveryToml/index'
model_dir = '$autodjRecoveryToml/models'
"@
[IO.File]::WriteAllText((Join-Path $autodjRecovery recovery.toml), $autodjRecoveryConfig, [Text.UTF8Encoding]::new($false))
.\autodj.cmd --config (Join-Path $autodjRecovery recovery.toml) restore $autodjArchive
.\autodj.cmd --config (Join-Path $autodjRecovery recovery.toml) doctor --no-fix
```

These generated TOML literal paths assume the scratch path has no apostrophe;
choose another scratch location or write correctly quoted TOML if it does.
The PowerShell example writes UTF-8 without a BOM, including non-ASCII paths.
Do not paste Bash substitutions or trailing
backslashes into PowerShell. PowerShell uses `$env:NAME` for process environment
values; Bash uses `export NAME=value`. Prefer editing the isolated config here.

Restore validates the archive and prints its restored-file count, then runs
Doctor. Missing original paths/models may still fail checks. Stop on a refusal;
do not add `--force` to get past existing destinations. Verify the configuration
and [restore restrictions](../operations.md#restore-and-validate) first. For an
interrupted restore, retain the recovery folder and journal, then review Doctor's
interactive rollback offer; never delete its journal to bypass startup refusal.

Before serving recovered data, inspect restored station settings for output,
external paths, schedules and local commands; reconnect only intended dependencies.
Restore access using [manage access](manage-access.md). A loopback start alone can
still use configured server audio or external integrations. Rehearse only in an
isolated configuration/environment. Do not remove the scratch directory until
you have recorded its result and confirmed it contains no sole copy of needed data.

## Update and keep a rollback path

A checkout started with its launcher can do these steps from **Settings**, **Global**, **Update and restart** with
**Update AutoDJ**, which goes back by itself when the new version fails; see
[Update from the page](backups-and-updates.md#update-from-the-page). By hand:

1. Stop serving and analysis/indexing, make a final backup, and keep the old
   version/configuration and separate assets/credentials. Read release changes
   since your version in [CHANGELOG.md](../../CHANGELOG.md).
2. Run `git status --short`. If any paths appear, save/reconcile those changes
   before switching; do not reset them. Replace the tag variable below.
3. Fetch and select the release, then use launcher setup to verify/adopt its
   runtime. Do not sync an existing GPU runtime against the CPU development lock.

   ```bash
   git fetch --tags origin
   autodj_release='vX.Y.Z'
   git switch --detach "$autodj_release"
   ./autodj setup
   ./autodj doctor --no-fix
   ```

   ```powershell
   git fetch --tags origin
   $autodjRelease = 'vX.Y.Z'
   git switch --detach $autodjRelease
   .\autodj.cmd setup
   .\autodj.cmd doctor --no-fix
   ```

4. Review any configuration/schema migration finding before applying a repair.
   Follow [backup and update guidance](backups-and-updates.md), then rehearse
   playback and verify the page-footer version before returning to normal use.

To go back, select the old release, set up its runtime and restore a backup made
by that major/minor version into a safe destination. Older releases may reject or
misinterpret newer settings: keep the pre-update backup and configuration rather
than opening newer data with the older program. These procedures have been
source-reviewed; this documentation change does not establish manual execution
on Windows, macOS or physical audio hardware.

AutoDJ and this page were developed with AI assistance.
