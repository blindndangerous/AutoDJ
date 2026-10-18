# Back up the installation and its stations

Create a current-format archive, inspect station coverage and exclusions, and preserve credentials and external assets separately.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Backups and doctor

Backup refuses file links that point outside their configured backup folder and linked
subfolders (including Windows junctions). This prevents unrelated files from entering an
archive through a link. File links to another file inside the same folder are saved as
ordinary files. Hard-linked files are refused because their other names may be outside the
backup folder; use an independent copy instead. Uploaded Stereo Tool preset links are still excluded. Configure a folder
directly if it is an intended backup source. Restore refuses linked archive entries and
destinations that point outside their configured folder.

`autodj backup` includes every configured station. The default station's files keep their names
in the archive, including the current catalog at `index/library.db`; another station's are under
`stations/<slug>/`, for example `stations/nightowl/index/web_state.json` and
`stations/nightowl/liners/...`. New backups omit legacy index manifests and generation files.
Each station's `relays.json`, which holds relay source passwords, stays out of the backup, and a
restore leaves the station's own `relays.json` in place (it skips one found in an archive made by
an older build). Keep a separate private copy of `relays.json` if you need one. A folder two
stations share, such as one liner folder for both, is stored once, under the default station's
name, and the archive notes that it was shared. If you restore it with a configuration that gives
that station a folder of its own, the folder is restored to the default station's place only, and
restore prints a warning naming both folders: copy the files across yourself if the station
should have them. Folders that hold one another (one station's liner folder inside another's, or
a folder that holds a station's own folder) are refused, because restoring one would replace the
other; give each station folders side by side. Take a new backup after adding a station, and
check that its `stations/<slug>/` files are in it:

```bash
uv run autodj backup backups/after-nightowl.zip
unzip -l backups/after-nightowl.zip | grep stations/nightowl
```

Archives containing old index generations are refused, since those files may contain vectors
that have not been imported into the catalog. Use current-format backups for restore.

Restore puts each station's files back in that station's folder. Only current-format backups are
supported. A station absent from the destination can be restored from its valid archived
`station.json` definition. A removed station's ledger-only `station.db` is also preserved
without creating a station definition. Other unknown-station contents are refused before
anything changes; supply a valid definition and restore again if that station is intended. Removing a station definition does not delete
its folder; doctor names such a folder, and backups leave it out.

`autodj doctor` checks the server once and every station separately. The default station's
lines look as before; another station's start with its slug:

```text
[PASS] Station nightowl, station-folder: /srv/autodj/index/stations/nightowl
[PASS] Station nightowl, saved-settings: saved settings valid
[PASS] Station nightowl, stream: stream mode on at 192 kbps
[WARN] Station nightowl, relay-main: Icecast at radio.example.net:8000/owl: cannot reach the server
```

Each station is checked for its folder (can it be written), its saved settings, its stream
(ffmpeg), its relays (from its own `relay-status.json`), its time checks and voice packs, DMCA
mode, its recording folder and its liner folder. `autodj doctor --station nightowl` checks only
that station beside the server; `--station default` checks only the default station. In
`--json`, a station's checks carry `"station": "nightowl"`.

Doctor changes nothing unless you agree. In a terminal it offers to make a missing station
folder (a station also makes it when it first starts) and to clean up a station's saved settings
the same way as the default station's, saving a backup beside the file first. `--fix` agrees to
each; `--no-fix` and `--json` only report. A configuration with a reserved or badly formed
station name, two stations sharing a recording folder or relay target, or a station's webhooks in
`config.toml` does not load at all, so doctor reports it as a configuration failure and names the
problem.

## Backup

Architecture and verification context: [Proposed state-recovery ADR](../../adr/0010-preserve-state-through-atomic-writes-and-recovery.md).

`autodj backup` writes one ZIP file holding:

- `dj_meta.db`, the intro, outro, beat grid and cue cache.
- `web_state.json`, the settings chosen in the web page.
- `related-artists.toml` and `related-tracks.toml` in the library folder.
- `recording.json`, the broadcast recording settings and whether it was running. The recorded
  MP3 files are not included; archive the recordings folder yourself.
- `library.db`, the shared registered folders, track data and cues, and matching vectors.
- Each station's `station.json` definition and `station.db`, including its last-played
  values, clock state, and complete play history. Database snapshots include committed WAL data.
- Every file in the station's liners and profiles folders (`index/stations/default/liners`
  and `profiles`), plus uploaded presets in `index/shared/stereo-tool-presets`.
- `time_checks.json` and every recorded clip in `[time_checks] clips_folder` (or the default
  shared `time_checks` folder). Generated TTS cache files are rebuilt when needed.
- Each station's `categories.json`, its rotation categories. Their playlist files are not included; the script
  that writes them can write them again.
- `liner_dates.json`, the voice liners' play dates.
- `ads.json`, the ad spots, ad breaks and their rules, with the spots' audio in the `ads`
  folder and the ad play log in the `ad_plays` folder (see [Ad breaks](ad-breaks.md#ad-breaks)).
- `listener_stats.json`, the daily listener counts (no addresses or player names).
- `shows.json`, the shows and their lists of songs.
- Every other station's state files and folders, under `stations/<slug>/` (see
  [Backups and doctor](#backups-and-doctor)).
- `manifest.json`, which records the AutoDJ version that made the archive, lists its files and
  names the stations it holds.

Backups do not include `config.toml` or `config.local.toml`. Copy those yourself
and keep them with the archive. Backups also leave out the pairing secret and the paired browsers, so
after a restore on a new machine you pair your browsers again.
Use the new machine's address followed by `/stream.mp3` to listen.

Backup copies databases through SQLite's backup API, so saved records and vectors form a
consistent database even while AutoDJ is serving. It refuses to overwrite an archive unless you
pass `--force`. It requires `library.db` and does not include old manifests or generation files.
An archive without `library.db` must be restored with a compatible older release, then upgraded
with `autodj migrate-index --apply` and create a new backup. Archives containing old generation
files are refused even if they also contain a catalog, to avoid losing unimported vectors.

```bash
# Linux/WSL2 Bash
uv run autodj backup backups/autodj-$(date +%F).zip
```

```powershell
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```


## Related tasks and contracts

- [Restore an archive and recover interrupted restores](restore.md)
- [Upgrade or return to an older release](upgrade.md)
