# Move music files and migrate legacy indexes

Rescan moved files or preview and apply a legacy import with recovery copies. Use a separate catalog for each machine.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Clean up an existing configuration

Registered catalog folders determine the available music. Add or remove sources in Library;
changing `[library] music_dir` does not add music or move catalog paths. Doctor's `configuration`
check names the catalog and library storage paths, and `music-path` checks registered music
folders. An empty catalog needs folders added in Library, not a `music_dir` value.

Current shipped examples and setup defaults start without a music root. An older installation's
`config.toml` may still contain comments about `metadata.json`, a single music root, or the
former `index/<name>/` layout. Updates do not replace those comments. The current catalog is
`<index_dir>/libraries/main/library.db`, with `dj_meta.db` beside it. Shared files live in
`<index_dir>/shared/`; station settings and state live in `<index_dir>/stations/<slug>/`.
Old metadata and vector files are legacy artifacts, not the current library. Only supported
published-index formats can be imported with `migrate-index`, as described below.

Before editing, back up `config.toml` and any `config.local.toml`. Review
`AUTODJ_LIBRARY_MUSIC_DIR` in the server environment too; it overrides both files.
Keep `music_dir` if any of these supported uses still depends on it:

- Beets relative paths for categories, clock conditions, enrichment, lyrics or tag updates.
  Categories and clock conditions can infer a base in limited cases; other Beets lookups still
  need an explicit base for relative paths. See [Beets paths](categories.md#beets-paths-and-music_dir).
- Relative M3U/M3U8 lines tried from that base after the playlist's own folder. To stop using
  this fallback, use full paths or paths relative to the playlist file. Lines only match songs
  already in the catalog; a playlist does not import music.
- Old relative keys in `dj_meta.db`. Keep the base so saved cues remain readable during playback,
  analysis, moved-file handling and programme reviews. New cache entries use full paths.
- Mixxx, Rekordbox or Traktor cue exports stored directly in that folder: `mixxx.db`,
  `mixxxdb.sqlite`, `Library.xml` or `collection.nml`. These supply cues for existing tracks,
  not new music. Default per-user DJ software locations are checked separately.
- Relative listener-request `blocked_folders`. With `music_dir`, these resolve from that base;
  without it, they resolve from every registered music folder. Convert them to full paths first
  if you need to preserve exactly the existing exclusions.
- Explicit legacy index migration. Prefer `migrate-index --music-dir PATH` for the one-time
  import rather than keeping a setting solely for that command.

If none applies, manually remove `music_dir` and its environment override. Update obsolete
comments using the current examples and [settings reference](../../settings-reference.md).
Do not replace the whole configuration or delete old data merely because a comment is outdated.
Restart AutoDJ after configuration changes, then run `autodj doctor --no-fix` with the same
configuration and environment. Check the registered folders and any Beets findings.
Doctor provides guidance; it does not automatically remove this supported setting, rewrite
comments, or change catalog contents.

## Moved files

When a file moves between registered folders in the same catalog, a scan can recognize it by its
size and a fingerprint of 64 KiB from the middle of the file. The existing vector, metadata,
analysis and cues stay with that track. A track is re-pathed only when exactly one missing file and
exactly one new file match. A copy, two identical files, or a file whose size or fingerprint changed
is handled as a changed or new song. Register the destination folder before rescanning. This does
not make a copied catalog portable to paths on another machine. A rescan also checks modification
time, so an in-place tag edit is read even if size and fingerprint stay the same.
The first rescan after upgrading re-reads existing files once to record their modification times.

Use `autodj migrate-index` to move a published index and supported old state into the current
`library.db`. It accepts schema-3 and schema-4 indexes, imports their vectors unchanged, and does not
reanalyze audio. It also upgrades version-1 `web_state.json`, imports legacy play-history JSON, and
preserves existing folder registrations. When upgrading older folder state, it registers the
configured liner folder and an explicitly supplied legacy music folder, if any.
You can run it when the index itself is current but only saved settings, history, or folder
registration still needs upgrading. The preview lists the work and writes nothing.

Run it with the same configuration as your server:

```bash
# Current catalog: upgrade old settings/history without a music root
uv run autodj migrate-index
uv run autodj migrate-index --apply
# Old published index: supply the base for its relative paths
uv run autodj migrate-index --music-dir /path/to/music --apply
# With another configuration:
uv run autodj --config station.toml migrate-index --apply
```

Applying saves a recovery copy of the index generation, `dj_meta.db`, `library.db`, and saved web
settings before changing them. The recovery folder is under `index/.migration-backups/`; the exact
path is printed. SQLite databases are copied with committed changes, including changes still in
the write-ahead log. The recovery copy is retained. It is not an `autodj restore` ZIP archive and
does not copy music, external liner folders or configuration files. The import is idempotent: if it
is interrupted, run `migrate-index --apply` again. Existing catalog records and vectors are kept;
missing records and vectors are filled from the published generation. Unsupported older index
schemas still require rebuilding.

Applying holds the index's serve lock and refuses to run while `autodj serve` is using that index.
Stop the server and rerun the command after it releases the lock.

The command validates the published files before importing them. Missing music files keep their
existing vectors and metadata, but cannot be recognized as moved until their files can be
fingerprinted. Old index imports use `--music-dir` (or an existing legacy `[library] music_dir`).
Settings/history-only upgrades need no music root. Existing folder registrations and saved
catalog settings are preserved.

For files that moved, `--map moves.json` accepts old to new paths relative to `music_dir`, such as
`{"Old/a.flac": "New/a.flac"}`. Paths must use forward slashes, and destinations must be unique
and not already indexed. If the move has not happened yet, the old file is used for fingerprinting.
The original script remains available for callers that already use it:

```bash
uv run python scripts/convert_index_v3_to_v4.py --index-dir index --music-dir /music --map moves.json
uv run python scripts/convert_index_v3_to_v4.py --index-dir index --music-dir /music --map moves.json --apply
```

The first command is a dry run for the schema-3 to schema-4 conversion; the `migrate-index` command
then imports the published vectors into `library.db`.

## More than one machine

Architecture and verification context: [Proposed catalog and migration ADR](../../adr/0006-use-a-catalog-with-explicit-migrations.md).

The current `library.db` stores absolute paths. Each machine should use its own catalog and
register folders using paths visible on that machine. Changing `music_dir` or copying a catalog
does not rewrite those paths. Older published indexes store paths relative to `music_dir` and
remain portable as a one-time import when the destination has no catalog.

## One catalog per machine

Each machine should keep its own catalog and register its local music folders in Library tools.
After adding tracks, run `autodj index` on that machine to import them and embed eligible songs.
The catalog stores absolute file paths, so pointing `music_dir` at the same files from another
mount point does not relocate an existing catalog. Create a catalog using the paths visible on
that machine; do not copy a populated `library.db` expecting it to adapt to a different mount.

## Importing a legacy index on another machine

Older published indexes store paths relative to `music_dir` and can be imported on the destination
without re-embedding. Place the legacy index files in `index/libraries/main/`, then run
`autodj migrate-index --music-dir /path/to/old/music --apply` using the destination
folder that contains the old relative paths. The catalog stores the
destination's absolute paths and the command keeps a recovery copy.

For this one-time legacy import, stop AutoDJ and any `index` or `analyse` run on both machines.
Copy only the files needed by the old format from the old index folder:

- The generation files named in `index-manifest.json`: `tracks.g<number>.db` and
  `vectors.g<number>.index`. The number is the manifest's `generation` written with 20 digits.
- `dj_meta.db`, the intro, outro, beat grid and cue cache.
- `index-manifest.json` last, after the files it names are in place. The migration command checks
  both files against the SHA-256 values in the manifest and refuses missing or half-copied files.

Do not copy machine-specific data:

- `web_state.json`, the settings chosen in the web page.
- `liners/`, the voice liners uploaded on that machine.
- `profiles/`, the saved profiles.
- `index/.access-token`, the LAN pairing secret. Copying it would let every browser paired with
  one machine use the other.
- `index/.paired-devices.sqlite3`, the paired browsers and their sessions.
- `index/.stream-secret`, if it is there. Older versions made it for keyed stream addresses;
  AutoDJ no longer uses it, and you can delete it.
- Any `-wal`, `-shm`, `.lock` or `.tmp` file. The index lock file is made when it is needed.

Do not copy `library.db` as part of this legacy import. Run `uv run autodj doctor` on the
destination after applying `migrate-index` explicitly. Startup and indexing do not import
the old published format. This procedure does not relocate an existing catalog.
