# How AutoDJ picks the next track

AutoDJ never opens an audio file to decide what plays next. Every track is
reduced once, during `autodj index`, to 1040 floating-point numbers. Choosing a
follow-up is one inner product against that matrix plus a little arithmetic on
tempo, energy, and key. This page is the reference for that path.

## The vector: 1024 + 16

`src/autodj/indexer.py` defines `FEATURE_DIM = EMBEDDING_DIM + _LIBROSA_DIM`,
which is 1024 + 16.

- The 1024 come from MuQ (`OpenMuQ/MuQ-large-msd-iter`, about 300M
  parameters). `src/autodj/model.py` resamples to 24 kHz, runs 30-second
  chunks at most two per batch in fp32, mean-pools over time, averages across
  chunks, and L2-normalises.
- The 16 come from librosa, in this order: RMS energy, spectral centroid (in
  Hz), zero-crossing rate, 12 chroma bins, onset strength.

`_combine_features` normalises each half to unit length, concatenates, and
normalises again. After that each half has norm 1/√2, so the 16 librosa numbers
carry as much of the cosine score as the 1024 MuQ dimensions. Because the
centroid is in Hz (roughly 900 to 5800) next to values near 0.05, the librosa
half points almost the same direction for every track and contributes a
near-constant 0.5 to every pair. The discrimination comes from MuQ. Measure it
on your own index before relying on this:

```bash
uv run python -X utf8 -c "
from pathlib import Path
import numpy as np
from autodj.similarity import SimilarityIndex
sim = SimilarityIndex.from_index_dir(Path('index'))
half = sim.faiss_index.reconstruct_n(0, sim.ntotal)[:, 1024:]
unit = half / np.linalg.norm(half, axis=1, keepdims=True)
print('min pairwise cosine of the librosa half:', float((unit @ unit.T).min()))
"
```

## The search: exact, by path

The FAISS index is `IndexFlatIP`, an exact inner-product scan
(`docs/adr/0002-faiss-for-vector-search.md`). On unit vectors, inner product
is cosine similarity. Flat indexes support `reconstruct`, which is how
`SimilarityIndex.find_next_for_path` re-queries by file path without loading
the model: look the path up in `_path_to_idx`, reconstruct the stored vector,
search with it.

A track's cosine with itself is 1.0, so a query with an empty
`recently_played` returns the track that is already playing. The exclusion set
is not optional.

## The filter, then the ranking

`find_next_for_path` builds one predicate (`_build_predicate`) and applies it before
any scoring. In order:

1. Path is not in `recently_played`, and the track is not silent: its
   `energy` (the mean RMS measured at index time) is at least
   `SILENT_ENERGY`, 0.0001, about -80 dBFS. The quietest music sits around
   0.001 (-60 dBFS), well above it. Every indexed track has a measured
   energy, because a track whose analysis fails is not indexed.
2. If a hard `bpm_range` is set: `entry.bpm` is known (greater than 0) and
   inside the range. Unknown tempo is rejected.
3. If `harmonic_mode` is not `"off"` (the player then passes
   `harmonic_only=True`): `dj_meta.harmonic_compatible` accepts the pair under
   that mode.
   Unknown key or mode (-1) is accepted, not rejected.
4. The player's repeat filter lets the track through: no artist, related
   artist, album, or song title it names played too recently, and no track
   related to it did (see "Repeat avoidance" below). The older `excluded_artists`, `excluded_albums` and
   `excluded_titles` sets still work for other callers.

Note the asymmetry between 2 and 3. An un-analysed track passes harmonic
mixing and fails a BPM range. If you set a BPM range and half your library
has no tempo tag, half your library is invisible until `autodj analyse` runs.

`_search_with_expansion` asks FAISS for `k` neighbours, applies the predicate,
and doubles `k` until `n_candidates` survive or the whole index has been
scanned. Filters compose multiplicatively, so on a large library this
expansion fires routinely.

If nothing survives and the repeat filter or the artist, album, or title
exclusions were active, those are dropped and the search runs once more over
the full index. If still
nothing survives, `SimilarityError` is raised naming the active hard filters.
`Player._pick_next` catches that and retries with `recently_played` reduced
to just the current track.

## The score

Without a preset there is no `target_bpm`, and candidates are sorted by
cosine and handed to `_softmax_pick`. With a preset, `target_bpm` comes from
its tempo curve and `_rerank` blends:

```
score = cosine * max(0, 1 - bpm_weight)
      + _bpm_score(entry.bpm, target_bpm) * bpm_weight
```

The weights partition 1.0. They are not bonuses. `_bpm_score` is a Gaussian
with sigma 15 BPM: 1.0 at an exact match, about 0.17 at 28 BPM off. It
returns 0.0 when the entry's tempo is unknown, so an un-analysed track can
score at most `cosine * (1 - bpm_weight)` and sits level with a track whose
tempo is maximally wrong.

Built-in preset `bpm_weight` values in `src/autodj/presets.py` range from 0.10
(`focus`) to 0.40 (`workout`). At 0.30 (`wakeup`, `winddown`, `party`) a perfect tempo
match in a different genre can outrank every track in the current track's own
sonic cluster.

## The pick

`_softmax_pick(scored, pick_top_k, pick_temperature)` returns the top entry
when `pick_top_k <= 1` or `pick_temperature <= 0`. The config defaults in
`src/autodj/config.py` are `pick_top_k = 1` and `pick_temperature = 0.3`, so
the default pick is deterministic. Raise `pick_top_k` to sample from the top
entries with softmax weights.

## Hour fitting

Tick **Fit songs to the hour** in Settings > Song picking (`[playback] hour_fit = true`) to
prefer a song that lands near the next top of hour, clockwheel restart or hard schedule event.
It starts considering lengths when that fixed point is within **Minutes before to start
fitting** (`hour_fit_window_minutes`) of the next song's estimated start. The window defaults to
10 minutes and accepts 1 to 30; fitting is off by default.

Fitting changes only selection among songs that the current pick path already accepts. A
clockwheel stays in its selected music step and tests at most 20 eligible songs. Random picking
samples at most 20 eligible songs. Least recently played tests the first 20 eligible songs in age
order. Similarity tests the first 20 filtered, ranked candidates. Discovery samples at most 20
songs from its eligible distant quarter. Repeat, category, daypart,
harmonic, BPM and DMCA limits retain their usual order and relaxation; fitting never adds an
ineligible song. Queued tracks, specific-track clock steps and fallback picks keep their existing
choice. The duration estimate uses saved start and segue points in saved-point mode, or track
length less crossfade otherwise. Missing or unusable lengths lose to known lengths. Preview and
rotation tests use the same player choice code, so they show fitted picks as the station makes them.

How a song is scored: one that hands off at or after the fixed point scores how late it is. One
that hands off before it scores how well whole songs can fill the rest, taking the candidates'
average length as a typical song, so a song that would end 30 seconds short of the hour, leaving
a gap the next song overruns by minutes, is a poor fit, and far from the hour a song that leaves
room for two typical songs is preferred over the longest one. Up to 10 seconds early counts as on
time for a top of hour or clockwheel restart; a hard event allows none, since a song started just
before it would be cut a few seconds in, and a hard event's **Wait up to seconds** covers a song
that runs a little over. Among equal scores the picker's own order decides: the least recently
played or the most similar song first.

Cost, measured on the Windows development machine with a synthetic 77,000-song library in random
pick mode (31 paired picks per setting after three warmup pairs): the target lookup, the 20-song
sample and the fitting choice add a median of 0.14 ms per pick (1,001 repetitions). Whole picks
took a median of 477 ms with fitting off and 454 ms with it on, a paired difference of minus
3.5 ms, within run-to-run noise; an earlier run measured plus 2.55 ms. The full pick
includes existing library-wide repeat checks, so its difference varies between runs, and other
libraries and pick modes will differ. Only the existing picker worker does this work, never the
audio thread.

## Repeat avoidance and discovery

New installations start with a 50-track limit for the same track and an 8-track
limit for the same artist. Album, title and all minute limits start at zero.
Saved settings take precedence over these defaults.

Edit the rules under **Settings > Picking > Repeats**:

- Artist: any artist credited on a recent track.
- Related artists: an artist linked to a recent one in the related artists file, when
  **Use related artists** is on.
- Album: the same album name, independent of the artist limit.
- Track: the same audio file, or any version of the same song when **Count versions as one
  song** is on.
- Title: the same song title across versions, such as a radio edit and a live recording.
- Related tracks: a track grouped with a recent one in the related tracks file, such as a song
  and one that samples it, when **Use related tracks** is on.
- Same daypart and same quarter hour: a song that played at the same time of day before (see
  "Time of day" below). These two have their own boxes.

The others each have a Tracks box and a Minutes box. Zero turns off that limit. When both
are set, a candidate must clear both limits. Minutes count from when a track
started to when the next pick is made. Server playout picks ahead, so a song
may be held back a little longer than the chosen time.

Save applies the rules to future picks and stores them in `web_state.json`.
The current song and already prepared queue are left alone. Saved play history
counts after a restart and when you increase a limit. A play interrupted by a
set stop does not count, as in the history viewer. Future-dated saved plays are
skipped if the clock has moved backwards. Minute limits use up to 50,000 recent
plays; artist, album and title track limits allow at most 5000. If the library is too
small for the track rule, AutoDJ reduces its effective count to leave at least
a tenth of the library (and at least four tracks) available.

Old repeat keys in `config.toml` and `web_state.json` are ignored with a warning.
Re-enter any limits that still use those keys in Settings > Picking > Repeats.
Current repeat rules are preserved, and Doctor offers optional cleanup with a backup.
Optional config defaults use
`[playback.repeat_rules]`, with `track`, `artist`, `related_artist`, `album`, `title` and
`related_track` tables containing `tracks` and `minutes`, the `use_related_artists`,
`use_related_tracks` and `versions_as_one_song` switches, a `daypart` table with `days` and
`start_hours`, and an `hourpart` table with `plays`; saved web settings take precedence. An
unknown key in any of these tables is ignored with a warning.

### Titles

`title_key` ignores case, accents and punctuation, reads `&` as "and", and
drops version notes: a bracketed part that names a version, a year or a
credit ("(Live)", "[Radio Edit]", "(feat. Someone)", "(From Frozen)"), a
trailing " - " part that does ("- 2011 Remaster"), and an inline "feat."
to the end. Other brackets keep their words, so "(I Can't Get No)
Satisfaction" is not shortened to "Satisfaction".

### Artists

`ArtistSplitter` turns one credit into the artists it names, and each one
counts for the artist rule. The full credit always counts as one artist.

- "feat.", "ft.", "featuring", "vs.", "versus", a semicolon and the
  separator of multi-value tags always split. So does a "(feat. X)",
  "(with X)" or "(w/ X)" note in the artist or the title, so "Uptown Funk
  (feat. Bruno Mars)" counts for Bruno Mars.
- "&", "and", "+", "x", "with", "/" and commas split only when every part
  looks like a separately credited artist: it has at least two words, or it
  is the whole artist credit of another track in the library. Parts such as
  "His Orchestra" or "Friends" are left out first. Credits on a short
  built-in list of single acts, and on the `single_acts` list of the related
  artists file, never split.

So "Simon & Garfunkel", "Earth, Wind & Fire" and "Mumford & Sons" stay one
artist, "Calvin Harris & Dua Lipa" counts for both, and "Tom Petty and the
Heartbreakers" also counts for Tom Petty. The limits:

- A duo of one-word names splits only when the library also has a track
  credited to each name alone ("Rihanna & Drake").
- A band named like two multi-word artists, such as "Edward Sharpe and the
  Magnetic Zeros", is split. Add it to `single_acts`.
- Splitting only adds artists, so a wrong split holds back a few more
  tracks; it never lets one through.

Artist names ignore case, accents, punctuation and a leading "The".

### Related artists

The **Use related artists** checkbox holds back the artists linked in a TOML file
to an artist that played within the Related artists Tracks and Minutes limits, which
start at the artist rule's 8 tracks. Settings saved before related artists had their own
limits keep using the artist limits. The file is `index/<name>/related-artists.toml`; AutoDJ copies the
shipped sample there only when it is absent. Upgrades never replace an existing
user copy. The Settings page shows this fixed location. The browser cannot
choose another path. The file must be at most 1 MiB.
Edit the file, then save repeat settings to reload it without restarting playback.

The `[related]` table maps an artist to the artists it holds back. Links are
one-way and are not followed further. For example, `"Bruno Mars" = ["Silk Sonic"]`
holds back Silk Sonic after Bruno Mars; add the reverse entry for both directions.
The optional `single_acts` list identifies credits that should never be split
while **Use related artists** is enabled.

An invalid file is refused when you save with **Use related artists** on,
leaving the previous rules in use. At startup, an invalid file produces a
warning and the other repeat rules still work. The Repeats panel and
`autodj doctor` report the problem.

### Related tracks

The **Use related tracks** checkbox, off by default, holds back tracks that sound alike on
air, such as a song and a later one that samples it, for as long as the Related tracks
Tracks and Minutes limits say (20 tracks to start with). The groups are in
`index/<name>/related-tracks.toml`; AutoDJ copies the shipped sample there only when it is
absent, and never replaces your copy. The file must be at most 1 MiB. Edit it, then save
repeat settings to reload it.

Each entry in the `[related]` table is one group, a track and a list of tracks, written as
"Artist - Title" with the lead artist:

```toml
[related]
"Rick James - Super Freak" = ["MC Hammer - U Can't Touch This", "Nicki Minaj - Super Freaky Girl"]
"Snow - Informer" = ["Daddy Yankee - Con Calma"]
```

- Every track in a group holds back every other one, in both directions, so a pair needs one
  line and three tracks that share a sample go in one group.
- Groups are not joined: a track in two groups holds back the tracks of both, but they do not
  hold each other back.
- "Artist - Title" is split at the first " - ". The title is compared like the title rule
  (case, punctuation and version notes such as "(Radio Edit)" and "(feat. ...)" do not
  matter), and the artist like the artist rule. A track matches when any artist it credits,
  featured artists included, and its title match an entry.
- Tracks not in the library are ignored.

An invalid file is refused when you save with **Use related tracks** on, leaving the previous
rules in use. At startup, an invalid file produces a warning and the other repeat rules still
work. The Repeats panel and `autodj doctor` report the problem.

### Versions of a song

A library can hold more than one version of a song, such as a radio edit and a remix. Each
version is its own file, so a song with two versions comes up about twice as often as a song
with one. The track rule only knows files, so it also lets the remix play soon after the
radio edit.

**Count versions as one song**, a checkbox under the same track limits, fixes both. It is off
by default.

- A song is its lead artist plus its title, compared the way the title rule compares titles.
  The lead artist is the credit before any "feat." or "(with ...)" note. "Mariah Carey -
  Fantasy" and "Mariah Carey feat. ODB - Fantasy (Remix)" are one song. Brandy's and Ariana
  Grande's "The Boy Is Mine" are two, because the lead artists differ.
- The track rule holds back every version of a song that played, for the same Tracks and
  Minutes.
- Random picks choose a song first and then one of its versions. A song with three versions
  comes up as often as a song with one.
- Least recently played dates a song by the latest play of any of its versions, then plays its
  least recently played version, so the versions take turns. Clock steps that use least
  recently played work the same way.
- Similarity picks keep only the best-scoring version of each song among their candidates.
- Versions count together wherever they are, in any folder or category.

The title rule does not change. It still holds back songs that share a title across artists,
such as two recordings of the same Christmas song. With the setting on, the rotation tester
counts a song's versions as one song.

Grouping depends on tags: reversed artist credits such as "A & B" and "B & A" stay separate,
while identical titles by the same lead artist (such as "Intro" on different albums) group
together. DMCA limits still count every actual play against its credited artists and album;
grouping never reduces those counts. Related-track rules continue to use their existing
artist/title matching, including recognised version labels.

### Time of day

Two rules, both off to start with, keep a song from playing at the same time every day, as
StationPlaylist Creator's daypart and hourpart separation do.

**Prevent a song from playing in the same daypart** holds back a song that played in the
daypart the next song starts in, earlier today or on any of the **Days** before (1 to 7; 0 turns
it off). The dayparts are set by **Daypart start hours**, hours from 0 to 23 separated by commas.
Each daypart lasts until the next start, and the last one runs past midnight to the first. The
default, 0, 6, 10, 15, 19, makes five: overnight, morning drive (6 to 10), midday, afternoon drive
and evening. So with 1 day, a song that played at 8:40 yesterday morning cannot play between 6
and 10 today, but it can at noon. With the starts 6 and 19, a song at 2 in the morning is in
the evening daypart that began at 19 the day before. One start makes the whole day one daypart;
all 24 hours keep a song out of the hour it played in the day before.

**Prevent a song from playing in the same quarter of the hour** holds back a song when one of
its last **Plays** plays (1 to 3; 0 turns it off) in the past week started in the quarter of the
hour the next song starts in: :00 to :14, :15 to :29, :30 to :44 or :45 to :59. At 1 a song
never starts in the same quarter twice running; at 3 it moves round all four. Plays more than a
week old do not count.

- The hours are the station's (`[server] station_timezone`, else the computer's), and a daypart
  keeps its hours across a clock change. Quarters of the hour compare exactly in every time
  zone, except across Lord Howe Island's half-hour clock change.
- Both rules judge the time the next song is expected to start, as dayparting does, not the time
  the pick is made. Server playout picks ahead, so this is the time the song goes on air.
- A song is its file, or with **Count versions as one song** on, every version of it.
- They belong to the same group as the artist, related artist, album, title and related track
  rules, and give way together with them when nothing else fits. When that happens depends on
  the pick mode (see "Where the rules apply"): a clockwheel step relaxes the group first and the
  track window only after; Random walk and Least recently played relax the track window first
  and the group after; a similarity pick relaxes the group while keeping the track window, and
  if that still finds nothing, searches again with the track window relaxed and the group back
  on. The DMCA limits are never relaxed for them. The log and the rotation tester name them
  "same daypart" and "same quarter hour".
- They need plays from before a restart, so while either is on AutoDJ keeps the plays of the
  days they look at: the daypart days plus two, and a week for the quarter hour. At startup it
  loads as many saved plays as those days can hold at one minute a song (4,320 for one daypart
  day, 10,080 for the quarter hour), or up to 50,000 when a Minutes limit or DMCA mode is on.

Each pick works out the songs the two rules hold back once, from the play history, and then
checks each candidate with a set lookup. Measured on the Alienware test machine with a made-up
77,000-track library and a 50,000-play history, medians of 41 runs: building the pick's rules
took 10.3 ms with both rules off and 12.9 ms with both on, of which the new walk was 2.0 ms
(3.9 ms with Count versions as one song on). Checking all 77,000 tracks against the rules took
55 ms with them off and 61 ms with them on.

### Where the rules apply

- Similarity and anchored picks pass the rules to `find_next_for_path`. When
  nothing passes, the search drops the repeat rules but keeps the track
  window; if it still finds nothing, it searches again avoiding only the
  tracks around this one, with the repeat rules back on (dropped once more if
  needed).
- Discovery picks from the least similar quarter of the tracks the rules
  allow.
- Random walk picks among the tracks the rules allow. Least recently played
  checks songs from oldest play time to newest and stops at the first one
  allowed. When nothing passes, both relax the track window first, then the
  repeat rules. Never-played songs come first; ties are tried in random order.
  Clock slots using least recently played work the same way.
- Clock slots marked EnforceRules use the rules; NoRules slots ignore them,
  as before. When every matching track is held back, the clock relaxes the
  artist, album, title, related, daypart and quarter-hour rules for that pick, then the
  track window, and logs which one it relaxed.
- When no track passes, the rules are relaxed rather than stopping the
  music. The fallback pick after a failed pick ignores them, as before.
- Automatic picks exclude the playing, prepared and starting tracks even when
  every repeat limit is zero.
- Tracks you queue, play now, or choose as the seed are never held back.

Each pick that held candidates back logs a line at info level, such as
"Repeat rules held back 4 candidates (1 same artist, 1 related artist, 1 same
title, 1 same daypart)". A similarity pick only counts the nearest candidates it looked at.
Least-recent picks count only the candidates checked before finding an eligible
song. Random picks check the whole pool, so their counts may be higher.
At DEBUG level, `autodj.player.repeat` also records each candidate examined by
the artist, related-artist, album, title, related-track, daypart or quarter-hour rules, its
reason, and the selected
path. The row means the candidate was excluded from that pool by the repeat
rule; the rule can be relaxed before selection. These rows do not include
tracks excluded only by the separate track-count or minute windows.

Each track's keys (its split artists, album and title) are worked out once
per library snapshot and then looked up, so a pick that checks every track
in a large library costs a set lookup per track.

Two mechanisms deliberately leave similarity behind:

- `discovery_every = N` makes every Nth pick call `find_distant`, which
  chooses at random from the bottom quartile of the cosine ranking.
- Pure shuffle (the Random walk pick mode) picks at random from every eligible
  track; only the hard BPM range and the repeat rules apply.

Silent tracks are left out of every pick AutoDJ makes by itself: similarity,
anchored, pure shuffle, discovery, the first track of a set and the Shuffle
button (`SimilarityIndex.random_entry`). A silent track still plays when the
user picks it in search (Play now, Play next, Add to queue).

## Explaining a pick

`explain_pick` in `src/autodj/explain.py` reads the previous and picked
entries and describes their relationship: genre change, BPM delta, Camelot key
relationship, energy delta. It is a narrator, not a trace. It does not know
which term of the score decided the pick. There is no `autodj explain` CLI
command; the web UI reaches this function through `src/autodj/_bridge.py`.
