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
   artist, album, or song title it names played too recently (see "Repeat
   avoidance" below). The older `excluded_artists`, `excluded_albums` and
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

## Repeat avoidance and discovery

`PlayerState` keeps `recently_played` as a deque bounded by
`no_repeat_window` (default 500), and `play_history`, the path, artist, album,
title and start time of each recent track. `src/autodj/repeat_rules.py` turns
that history into three rules:

- Track: the same file, within `no_repeat_window` tracks or
  `no_repeat_minutes` minutes.
- Artist: any artist the candidate credits, or a related one, was credited
  within `artist_repeat_window` tracks (default 3) or
  `artist_repeat_minutes` minutes. The same album is held back by the same
  window, as before.
- Title: the same song in any version, within `title_repeat_window` tracks
  or `title_repeat_minutes` minutes. Unset, `title_repeat_window` follows
  `artist_repeat_window`, which covered titles on its own before.

A count of 0 or minutes of 0 turns that half of a rule off. With both set, a
candidate is held back if either catches it. Minutes count from when a
track started, measured when the pick is made; in server audio and stream
mode that is about one track ahead of when the pick plays, so a minute
window holds back a little more than its length. The history lives only in
memory, so a restart starts every window afresh.

The track and artist windows can be changed while AutoDJ runs, under
Settings, Song picking ("Tracks before a song can repeat" and "Tracks before
an artist can repeat"); the new value applies to the next pick and is saved
in `web_state.json`. The other repeat settings are read from `config.toml`
for now. When the library holds no more tracks than `no_repeat_window`, the
player uses a smaller window so that at least a tenth of the library (and at
least 4 tracks) stays available.

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
  separator of multi-value tags always split. So does a "(feat. X)" or
  "(with X)" note in the artist or the title, so "Uptown Funk (feat. Bruno
  Mars)" counts for Bruno Mars.
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
- A band named like two multi-word artists is split. Add it to
  `single_acts`.
- Splitting only adds artists, so a wrong split holds back a few more
  tracks; it never lets one through.

Artist names ignore case, accents, punctuation and a leading "The".

### Related artists

`related_artists_file` names a TOML file whose `[related]` table maps an
artist to a list of artists it holds back. Links are one-way and are not
followed further: "Bruno Mars" = ["Silk Sonic"] holds back Silk Sonic after
Bruno Mars, but not Bruno Mars after Silk Sonic. `related_artists_starter =
true` adds a short built-in list of side projects, duos and aliases
(`STARTER_RELATED_ARTISTS`). Both are read when AutoDJ starts.

### Where the rules apply

- Similarity and anchored picks pass the rules to `find_next_for_path`.
- Discovery picks from the least similar quarter of the tracks the rules
  allow.
- Random walk and least recently played pick among the tracks the rules
  allow.
- Clock slots marked EnforceRules use the rules; NoRules slots ignore them,
  as before.
- When no track passes, the rules are relaxed rather than stopping the
  music. The fallback pick after a failed pick ignores them, as before.
- Tracks you queue, play now, or choose as the seed are never held back.

Each pick that held candidates back logs a line at info level, such as
"Repeat rules held back 3 candidates (1 same artist, 1 related artist, 1 same
title)". A similarity pick only counts the nearest candidates it looked at.

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
