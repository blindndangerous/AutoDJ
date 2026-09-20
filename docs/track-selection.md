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

`find_next` builds one predicate (`_build_predicate`) and applies it before
any scoring. In order:

1. Path is not in `recently_played`.
2. If a hard `bpm_range` is set: `entry.bpm` is known (greater than 0) and
   inside the range. Unknown tempo is rejected.
3. If a `genre_filter` is set: genre matches.
4. If `harmonic_only` is on: `dj_meta.harmonic_compatible` accepts the pair.
   Unknown key or mode (-1) is accepted, not rejected.
5. Artist, album, and title are not in their exclusion sets.

Note the asymmetry between 2 and 4. An un-analysed track passes harmonic
mixing and fails a BPM range. If a preset sets a range and half your library
has no tempo tag, half your library is invisible until `autodj analyse` runs.

`_search_with_expansion` asks FAISS for `k` neighbours, applies the predicate,
and doubles `k` until `n_candidates` survive or the whole index has been
scanned. Filters compose multiplicatively, so on a large library this
expansion fires routinely.

If nothing survives and artist, album, or title exclusions were active, those
three are dropped and the search runs once more over the full index. If still
nothing survives, `SimilarityError` is raised naming the active hard filters.
`Player._pick_next` catches that and retries with `recently_played` reduced
to just the current track.

## The score

With no `target_bpm` and no `target_energy`, candidates are sorted by cosine
and handed to `_softmax_pick`. Otherwise `_rerank` blends:

```
cosine_w = max(0, 1 - bpm_weight [if target_bpm] - energy_weight [if target_energy])
score    = cosine * cosine_w
         + _bpm_score(entry.bpm, target_bpm) * bpm_weight
         + _energy_score(entry, target_energy) * energy_weight
```

The weights partition 1.0. They are not bonuses. `_bpm_score` is a Gaussian
with sigma 15 BPM: 1.0 at an exact match, about 0.17 at 28 BPM off. `_energy_score`
uses sigma 0.15. Both return 0.0 when the entry's value is unknown, so an
un-analysed track can score at most `cosine * (1 - bpm_weight)` and sits level
with a track whose tempo is maximally wrong.

Preset `bpm_weight` values in `src/autodj/presets.py` range from 0.15
(`morning`) to 0.40 (`workout`). At 0.30 (`wakeup`, `party`) a perfect tempo
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
`no_repeat_window` (default 500), plus artist, album, and title windows of
`artist_repeat_window` (default 3).

Two mechanisms deliberately leave similarity behind:

- `discovery_every = N` makes every Nth pick call `find_distant`, which
  chooses at random from the bottom quartile of the cosine ranking.
- `smart_shuffle` (`invert=True`) negates the query vector and returns the
  single farthest eligible track.

## Explaining a pick

`explain_pick` in `src/autodj/explain.py` reads the previous and picked
entries and describes their relationship: genre change, BPM delta, Camelot key
relationship, energy delta. It is a narrator, not a trace. It does not know
which term of the score decided the pick. There is no `autodj explain` CLI
command; the web UI reaches this function through `src/autodj/_bridge.py`.
