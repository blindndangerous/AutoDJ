# Import, analyse and maintain a music library

Register folders, import and analyse tracks, and inspect skipped files before changing the library.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Indexing and DJ analysis

`autodj index` scans the registered folders and saves basic song records in `library.db` before
loading the model. It then embeds eligible songs that do not yet have vectors. By default it also
refreshes metadata from beets and runs the DJ-meta backfill (`autodj analyse`) for tracks in
registered music folders with advanced analysis enabled. This backfill adds intro,
outro, beat-grid and cue data; `--no-analyse` skips it, while advanced embedding still runs.
In a terminal, analysis commands show completed-track progress. Redirected output and web job
logs keep plain progress messages.

Doctor checks the local model cache only when model support is installed and enabled and a
music folder uses similarity analysis. Basic/Audio-only libraries and installations without
local model support do not need model weights. Model-directory checks are part of that same
conditional diagnostic. When the cache is needed, `autodj download-model` downloads or validates
the configured model without loading it for inference or analyzing songs; it also works when
there are no pending similarity jobs. Doctor offers this command through its normal recovery
confirmation.

In the web page, add, remove and rescan music and voice liner folders in the **Library tools**
tab. Its folder table shows each folder's type, path and scan options. Automatic rescan every N hours
and M minutes runs the same scan as the Rescan button, and its setting is kept in `library.db`.
Setting 0 hours and 0 minutes (the default) turns it off; the shortest interval is 5 minutes. Time
counts from the end of the last startup, manual or automatic scan; if a scan or cleanup is running,
the next tick tries again. **Queue & Search** uses one paginated list: choose Music or Voice liners,
choose all folders or one folder, and optionally
search by artist or title. Each result has Play now, Play next, Add to queue and Track info.
One song plays next: a second Play next takes the place of the first, and the page names the song
it replaced.
Manage liner files, including Delete and Play dates, under **Settings > Voice liners** (see
[Liner and cart play dates](carts-and-liners.md#liner-and-cart-play-dates)). Track info's Return button puts focus
back on the result that opened it. **Music duck (dB)** sets the music drop under overlays and
scheduled IDs, from 0 to 30 dB (default 3). Zero leaves music unchanged. Existing saved depths
are preserved. The config/API value `liners_duck_db` remains negative: -3 means a 3 dB drop.

An uploaded voice liner must be audio AutoDJ can play, or the upload is refused with "<name> is
not an audio file AutoDJ can play." AutoDJ reads the file's header: libsndfile reads MP3, WAV, FLAC
and Ogg, and FFmpeg's ffprobe reads M4A and AAC, and any MP3, WAV, FLAC or Ogg file libsndfile
cannot read. Without ffprobe, an M4A or AAC upload is checked by its first bytes only, so a damaged
one is found only when it plays. When ffprobe cannot run or takes longer than 30 seconds, the upload
is refused with "Could not check <name>, so it was not saved."

With `[playback] import_external_cues` on (the default), intro and outro markers set in Mixxx,
Rekordbox or Traktor take the place of the detected intro and outro, so the crossfade follows
them. A track analysed before AutoDJ read those markers gets them the first time it plays while
`autodj serve` runs; there is no need to analyse it again.

Indexing, analysis and server playback decode files the same way: FFmpeg decodes `.mp3`, `.m4a`,
`.mp4` and `.aac` files, and soundfile decodes the rest, with FFmpeg as the fallback when soundfile
cannot read a file. A file that indexes therefore also plays, at the same level.

FFmpeg can repair some damaged files while it decodes them. When it does, the terminal shows
"Decoded FILE with warnings" with FFmpeg's message, above the status line. A file that cannot be
decoded or analysed shows "Could not index FILE, skipped" or "Could not analyse FILE, skipped" with
the reason. At the end of the run, one line counts both kinds and names a log in the index
directory that lists each file and its messages: `decode-indexing.log` for embedding and
`decode-analysing.log` for DJ analysis and `decode-library.log` for `autodj analyse-library`. Each run replaces its log, and a run with nothing to
report removes it. FFmpeg's routine note that an MP3's duration is estimated from its bitrate is
left out, because the decode itself is complete.

A new library starts empty. Add folders in Library tools; setup and startup do not choose a
music folder for you. `autodj index` uses those same registered folders. `--limit` caps the advanced
embedding pass, not the basic import. Folders with advanced analysis disabled stay playable
without embedding. Each completed vector is saved in `library.db`, so an interrupted run resumes
with remaining songs. `--force` replaces vectors for eligible songs without deleting cues,
history or settings; a failed embedding leaves the previous vector in place. `--workers` controls
audio prefetch. `--no-enrich` skips the optional beets refresh.

The import status in Library tools gives counts. Files that could not be read or decoded are
errors; open **Import errors** below the summary for the list.

When the server starts, it rescans the library once every streaming or server-audio station has
started its first song and picked the one after it, or after 2 minutes, so the scan does not slow the first songs. Files added
while the server was stopped appear after that scan.

The old published-index format, including `index-manifest.json`, is deprecated. Stop AutoDJ
and run `autodj migrate-index` to preview the upgrade, then add `--apply` to import the old
data with a recovery copy. Startup and indexing use the catalog only; `index --force` does
not bypass migration. A leftover manifest is ignored when the current catalog already exists.

`analyse`, `enrich`, and `stats` use the
current catalog: Analyse backfills CPU intro/outro and beat data without MuQ, Enrich refreshes
beets metadata without re-embedding, and Stats prints the detailed catalog report. Analyse covers
all registered folders. New DJ-meta cache entries use full track paths; existing
relative cache entries remain readable with the old `music_dir` setting. That setting does not
limit which registered folders can be analysed. It also supports relative Beets and playlist paths,
DJ cue exports and legacy listener-request folder exclusions. New setups normally leave it unset;
review these uses before [cleaning up an existing setting](catalog-migration.md#clean-up-an-existing-configuration).
For a legacy index import, pass
`migrate-index --music-dir PATH` instead. Current-library settings/history upgrades need no root.

The Library panel's **Remove missing tracks** action is
available after a successful rescan; it previews catalog records missing from mounted sources,
requires confirmation, asks again above the 20% safety threshold, deletes saved catalog cues and
analysis data for those records, and preserves audio files and play history. Disconnected sources
and incomplete scans are excluded from cleanup.


To give network drives a rest during a long run, set a pause before each track, in milliseconds,
in `config.toml`:

```toml
[index]
throttle_ms = 500
```

It applies to the tracks `autodj index` embeds and the tracks `autodj index` and
`autodj analyse` analyse. The default, 0, means no pause.


## Rescan cue points in Track info

Open a song or registered voice liner in **Track info**, save any cue edits,
then choose **Rescan cue points**. Compare the saved and suggested times and
their sources, and preview the points using the track's preview player.
Changed automatic cues are selected initially; select a user or imported cue
explicitly to replace it. Choose **Apply selected** to save the selected cues,
or **Discard** to keep the saved cues.

The scan uses the same basic detectors as library analysis. Music offers Start,
Intro, Outro, Segue and End; liners offer Start, Segue and End. Lyric-derived
Intro and Outro describe vocal timing, which can differ from the acoustic ending.
Overlap preferences, play dates, tags and history stay unchanged. If the audio
file or saved cues change before applying, rescan again to get current suggestions.

**Library tools > Music fade drop (dB)** controls the relative drop required for
a music Segue suggestion, from 1 to 30 dB (default 10). A smaller drop can hand
off earlier; a larger drop waits for a quieter fade. The detector compares the
ending with the preceding audio, requires at least five seconds of quieter
audio, and checks for late bursts such as a final vocal. Without a qualifying
fade, Segue uses the final audible End. End trims near-silence below −60 dBFS.
Changing this shared setting affects new library analysis and cue rescans;
saved cues stay unchanged until explicitly replaced. Preview unusual endings
before applying: energy detection does not recognize speech or musical intent.

## Related tasks and contracts

- [How long tracks use memory and disk](../../explanation/operations/long-tracks.md)
- [How drop mixing and key shift work](../../explanation/operations/dj-transitions.md)
- [Move music files and migrate legacy indexes](catalog-migration.md)
- [Edit tags in the catalog, files and beets](tags.md)
