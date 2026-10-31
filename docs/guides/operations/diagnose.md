# Diagnose an installation before serving

Run Doctor with the same environment and configuration as the server, review its findings, and check the result before serving.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Diagnose before serving

If `autodj serve` exits with an error in an interactive terminal, it offers to run
`autodj doctor` with the same Python environment and configuration.
Doctor still asks before repairs. After resolving the checks, run your serve command again.
In scripts, serve prints the suggestion without prompting. To run Doctor yourself, use the
same `--config` option as serve.

Application failures print `Error: ...` to standard error and return exit 1.
Normal command output remains on standard output. Usage and argument errors,
including incompatible options, return exit 2. Setup reports a failed subprocess's
status and preserves it, including 130 for interruption and 2 for argument errors.
Server restart/update requests retain their launcher control statuses.

Run `uv run autodj doctor`. Use `uv run autodj doctor --json` for automation. A required failed
check returns exit 1; the JSON report remains on standard output, with a short
failure message on standard error. In an interactive terminal, doctor offers to remove unsupported config
keys and create missing index/model directories. Doctor collects unsupported keys across all known
config sections and shows the proposed removals together for one confirmation. It removes them
in one edit per affected file, saving one uniquely named backup beside each original file,
preserving unrelated settings and formatting, then retrying validation.
Review the key names before accepting: a misspelled setting may need correcting instead of removing.
Complex TOML edits that cannot be made safely are left for manual correction.

Doctor also warns about obsolete repeat settings in `web_state.json` in the selected index.
Startup ignores those keys and loads the other settings. Doctor offers optional cleanup,
with a backup, while preserving current
repeat limits, EQ and other saved values. An empty old `related_artists_file` setting can be
removed this way. A nonempty path needs manual review so you can preserve its related-artist
data. Malformed files and unsupported saved-state versions are left unchanged.

Use `uv run autodj doctor --fix` to apply supported repairs automatically without prompting.
Without flags, doctor asks before each repair in an interactive terminal. Use `--no-fix`
for diagnostics without repair prompts. `--json` and noninteractive runs without `--fix`
remain read-only, except that the index check takes the index lock, as `serve` does.
`--json` cannot be combined with `--fix`. Doctor rechecks after repairs; remaining required
failures still return exit 1. Doctor also offers to run its recommended AutoDJ recovery commands:
indexing (including `index --force` for an outdated or inconsistent index), downloading missing
models through indexing, DJ metadata analysis, and `serve --lan` for network streaming.
These commands use the same Python environment and configuration. Doctor explains
the work before asking; indexing and analysis can take a long time. `--fix` approves these commands
too. Dependency installation remains manual.

Each recovery runs at most once, and doctor rechecks afterwards so it can skip commands that are
no longer needed. Failed commands are reported and cause exit 1. For an old or corrupt DJ metadata
cache, doctor first moves the cache and its SQLite sidecar files into a backup directory; stop
serving and indexing before accepting this repair. `serve --lan` runs until stopped and does not
change the saved configuration. Doctor redacts both server and Hugging Face tokens in its
configuration report.

The `configuration` check names the data root, library storage folder, `library.db` catalog
and `dj_meta.db` cue cache. Available music comes from registered catalog folders, listed by
`music-path` when readable. With no registered music folders, Doctor tells you to add them in
Library. A configured `music_dir` appears as `legacy_music_dir`, with its compatibility purposes
and manual cleanup guidance. It is not the general library location. The `beets-paths` check
labels its `music_dir` specifically as the base for relative Beets paths. Text and JSON reports
carry these descriptions. See [clean up an existing configuration](catalog-migration.md#clean-up-an-existing-configuration)
for old comments and supported settings that must not be removed blindly.

The `index` check reads the current catalog. It reports catalog counts, warns when the library
is empty, and directs older libraries to `migrate-index`. It does not load old generations. The `dj-meta-db`
check runs SQLite's integrity check on `dj_meta.db` without changing it. The `beets-db` check
warns when `[library] beets_db` names a file that does not exist: folder scanning still imports
basic song records, but optional beets enrichment cannot run. The `beets-paths` check, shown
when the beets database exists, counts the library songs that have a beets entry and warns when
not one does (see [Beets paths and `music_dir`](categories.md#beets-paths-and-music_dir)), naming the folder
to set. The `dependencies` check warns when
FFmpeg is not on the PATH: advanced analysis cannot process `.mp3`, `.m4a`, `.mp4` and `.aac`
files, and stream mode cannot start. The `categories` check warns about rotation categories with no
songs, playlist lines that match no library track (naming the first few), and a query or playlist
that cannot be read; its detail counts each category's songs and the songs placed by their release
year (see [Rotation categories](categories.md#rotation-categories)). The `config-keys` check lists the keys and
tables in `config.toml` and `config.local.toml` that AutoDJ ignores. The `recording` check warns
when the recording folder cannot be written or made, and the `liners` check when a configured
liner folder is missing. With several stations, each station is checked on its own lines (see
[Backups and doctor](backup.md#backups-and-doctor)).
