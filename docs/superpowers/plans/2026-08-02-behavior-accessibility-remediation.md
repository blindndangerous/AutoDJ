# Behavior and Accessibility Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct confirmed selection, persistence, HTTP-media, browser interaction, and screen-reader defects while preserving AutoDJ's existing playback architecture.

**Architecture:** Keep the Python selection and persistence work in their existing modules, extract only the HTTP range iterator and small browser controllers that need direct test seams, and leave the audio-effect graph unchanged. Treat BPM/genre/harmonic constraints as hard eligibility rules, typed persisted settings as an explicit versioned schema, and browser updates as request-owned state transitions with durable accessible text plus transient announcements.

**Execution order:** Execute `2026-08-02-security-data-integrity-remediation.md` first. This plan consumes its `SimilarityIndex.entries_snapshot()`, `entry_for_path()`, authentication dialog, and session middleware contracts and must not reintroduce mutable direct-entry reads. Execute `2026-08-02-delivery-maintenance-remediation.md` afterward; delivery owns final Node/action/dependency versions and may supersede this plan's provisional frontend CI wiring while retaining its behavioral gates.

**Tech Stack:** Python 3.14, NumPy, librosa, FAISS, FastAPI/Starlette, Pydantic, pytest, vanilla ES modules, Fetch/WebSocket APIs, Vitest with Happy DOM, Vite, ESLint, GitHub Actions.

---

## File structure and responsibility map

- `src/autodj/indexer.py` — weighted key estimation and librosa BPM fallback metadata.
- `tests/unit/test_indexer_more.py` — synthetic major/minor/insufficient-evidence and BPM fallback tests.
- `src/autodj/similarity.py` — normalized FAISS queries, global smart shuffle, progressive candidate expansion, and hard eligibility.
- `src/autodj/player.py` — preserve hard BPM eligibility for pure-shuffle selection and surface final selection errors.
- `tests/unit/test_similarity.py` and `tests/unit/test_player.py` — large-index distance, hard-filter, expansion, relaxation, and player propagation tests.
- `src/autodj/runtime_state.py` — versioned typed persisted-state schema and field-specific readers.
- `src/autodj/_bridge.py` — serialize every persisted playback/liner setting and mark derived session-only fields.
- `tests/unit/test_runtime_state.py` and `tests/integration/test_server.py` — schema validation and full settings round trips.
- `src/autodj/http_media.py` — pure single-range parser plus threadpool-compatible synchronous file iterator.
- `src/autodj/server.py` — HTTP-media route composition, ffmpeg preflight, and track-owned lyrics endpoint.
- `tests/unit/test_http_media.py` and `tests/integration/test_server.py` — range forms, 416 responses, nonblocking reads, disconnect cleanup, and missing-ffmpeg fallback.
- `src/autodj/static/modules/api-client.js` — JSON transport/application validation and disabled-control lifecycle.
- `src/autodj/static/modules/hotkeys.js` — shortcut scope that preserves native control keys.
- `src/autodj/static/modules/liners.js` — distinct-track cadence and accurately named delete controls.
- `src/autodj/static/modules/seek-controller.js` — pointer drag lifecycle independent of WebSocket state application.
- `src/autodj/static/modules/lyrics.js` — path-scoped, abortable, generation-owned lyric requests.
- `src/autodj/static/modules/queue.js` and `src/autodj/static/modules/search.js` — failure reporting, optimistic rollback, and stable focus.
- `src/autodj/static/modules/cues.js` and `src/autodj/static/modules/badges.js` — durable cue summaries plus controlled live announcements.
- `src/autodj/static/app.js`, `index.html`, and `app.css` — wire focused modules, persistent metadata, ARIA relationships, contrast, and reduced motion.
- `tests/jsmodules/*.test.js` — DOM behavior, request races, focus, live-region, semantic, motion, and contrast assertions.
- `.github/workflows/ci.yml` — make frontend lint, unit tests, and production build blocking CI gates.

### Task 1: Weighted key estimation and librosa BPM fallback

**Files:**
- Modify: `src/autodj/indexer.py:517-599,1835-1855`
- Modify: `tests/unit/test_indexer_more.py:531-598`

- [ ] **Step 1: Replace the dead minor-key proof with failing synthetic-profile tests**

Inside the existing `TestIndexerExtract`, replace only `test_minor_branch_is_lp_infeasible` with the first two methods below and add the third method. Keep `test_tempo_confidence_exception_fallback` and `test_extract_raises_on_empty_audio` unchanged. Paste these methods at class indentation:

```python
    def test_weighted_profiles_distinguish_c_major_and_a_minor(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        c_major = np.array(
            [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
            dtype=np.float32,
        )
        a_minor = np.roll(
            np.array(
                [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
                dtype=np.float32,
            ),
            9,
        )

        assert _estimate_key_from_chroma(c_major) == (0, 1)
        assert _estimate_key_from_chroma(a_minor) == (9, 0)

    def test_key_estimation_preserves_unknown_for_insufficient_evidence(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        assert _estimate_key_from_chroma(np.zeros(12, dtype=np.float32)) == (-1, -1)
        assert _estimate_key_from_chroma(np.full(12, np.nan, dtype=np.float32)) == (-1, -1)

    def test_key_estimation_rejects_weak_noisy_chroma(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        noisy = np.array(
            [1.00, 0.99, 1.01, 1.00, 0.98, 1.02, 1.00, 0.99, 1.01, 1.00, 0.98, 1.02],
            dtype=np.float32,
        )
        assert _estimate_key_from_chroma(noisy) == (-1, -1)

    def test_extract_returns_estimated_bpm_metadata(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        import numpy as np

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load, patch.object(indexer, "librosa") as lib:
            load.return_value = (np.ones(22050, dtype=np.float32), 22050)
            lib.feature.rms.return_value = np.array([[0.5]])
            lib.feature.spectral_centroid.return_value = np.array([[1000.0]])
            lib.feature.zero_crossing_rate.return_value = np.array([[0.1]])
            lib.feature.chroma_stft.return_value = np.tile(
                np.array([[6.35], [2.23], [3.48], [2.33], [4.38], [4.09], [2.52], [5.19], [2.39], [3.66], [2.29], [2.88]]),
                (1, 4),
            )
            lib.onset.onset_strength.return_value = np.array([0.5])
            lib.beat.beat_track.return_value = (np.array([123.0]), np.array([0, 10]))

            _, _, _, meta = indexer._extract_librosa_features(Path("dummy.flac"))

        assert meta["bpm"] == 123.0
        assert meta["key"] == 0
        assert meta["mode"] == 1
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run: `uv run pytest tests/unit/test_indexer_more.py::TestIndexerExtract -v`

Expected: FAIL because `_estimate_key_from_chroma` is not importable and `extra_meta` has no `bpm` key.

- [ ] **Step 3: Add weighted profile estimation and return tempo metadata**

Insert above `_extract_librosa_features` and replace the binary-template/tempo block inside it:

```python
_MAJOR_KEY_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float32,
)
_MINOR_KEY_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float32,
)


def _estimate_key_from_chroma(chroma: np.ndarray) -> tuple[int, int]:
    """Return ``(pitch_class, mode)`` or ``(-1, -1)`` when evidence is unusable."""
    values = np.asarray(chroma, dtype=np.float32).reshape(-1)
    if values.shape != (12,) or not np.isfinite(values).all() or float(values.sum()) <= 1e-6:
        return (-1, -1)
    peak_share = float(values.max() / values.sum())
    if peak_share < 0.12:
        return (-1, -1)
    centred = values - float(values.mean())
    norm = float(np.linalg.norm(centred))
    if norm <= 1e-6:
        return (-1, -1)
    centred /= norm

    scored: list[tuple[float, int, int]] = []
    for mode, profile in ((1, _MAJOR_KEY_PROFILE), (0, _MINOR_KEY_PROFILE)):
        profile_centred = profile - float(profile.mean())
        profile_centred /= float(np.linalg.norm(profile_centred))
        for key in range(12):
            scored.append((float(np.dot(np.roll(profile_centred, key), centred)), key, mode))
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, key, mode = scored[0]
    margin = best_score - scored[1][0]
    if best_score < 0.50 or margin < 0.08:
        return (-1, -1)
    return (key, mode)
```

```python
        key, mode = _estimate_key_from_chroma(chroma)

        tempo_val = 0.0
        try:
            tempo_arr, beat_frames = librosa.beat.beat_track(y=audio, sr=sr)
            tempo_val = float(np.atleast_1d(tempo_arr)[0])
            if not np.isfinite(tempo_val) or tempo_val <= 0:
                tempo_val = 0.0
            duration_sec = len(audio) / max(1, sr)
            expected = (tempo_val / 60.0) * duration_sec
            tempo_confidence = float(min(1.0, len(beat_frames) / max(1.0, expected)))
        except Exception:
            tempo_confidence = 0.0

    extra_meta = {
        "energy": float(energy),
        "key": key,
        "mode": mode,
        "bpm": tempo_val,
        "tempo_confidence": tempo_confidence,
    }
```

In `_extract_librosa_features`'s docstring, add `- ``bpm`` — librosa beat-tracker tempo, or 0.0 when unavailable` to Extra metadata and include `bpm` in the documented `extra_meta` key list.

- [ ] **Step 4: Assign estimated BPM only when trusted metadata is absent**

Add this helper beside `_estimate_key_from_chroma`:

```python
def _apply_analysis_metadata(entry: IndexEntry, extra_meta: dict[str, float | int]) -> None:
    entry.energy = float(extra_meta["energy"])
    entry.key = int(extra_meta["key"])
    entry.mode = int(extra_meta["mode"])
    entry.tempo_confidence = float(extra_meta["tempo_confidence"])
    if entry.bpm <= 0.0 and float(extra_meta["bpm"]) > 0.0:
        entry.bpm = float(extra_meta["bpm"])
```

Replace the four direct metadata assignments after `entry = IndexEntry.from_track(track)` in `_embed_new_tracks` with `_apply_analysis_metadata(entry, extra_meta)`. Add this focused test beside `TestIndexerExtract`:

```python
def test_index_entry_prefers_tag_bpm_and_falls_back_to_estimate() -> None:
    from pathlib import Path

    from autodj.indexer import IndexEntry, Track, _apply_analysis_metadata

    tagged = IndexEntry.from_track(Track(
        path=Path("tagged.flac"), title="Tagged", artist="Artist", album="Album",
        genre="Rock", bpm=128.0, year=2026, length=180.0,
    ))
    unknown = IndexEntry.from_track(Track(
        path=Path("unknown.flac"), title="Unknown", artist="Artist", album="Album",
        genre="Rock", bpm=0.0, year=2026, length=180.0,
    ))
    meta = {"energy": 0.4, "key": 9, "mode": 0, "tempo_confidence": 0.8, "bpm": 121.5}

    _apply_analysis_metadata(tagged, meta)
    _apply_analysis_metadata(unknown, meta)

    assert tagged.bpm == 128.0
    assert unknown.bpm == 121.5
```

- [ ] **Step 5: Run analysis tests and the indexer regression slice**

Run: `uv run pytest tests/unit/test_indexer_more.py::TestIndexerExtract tests/unit/test_indexer.py -q`

Expected: PASS; the previous exception fallback still reports `tempo_confidence == 0.0`, and weighted profiles reach both modes.

- [ ] **Step 6: Commit the analysis correction**

```bash
git add src/autodj/indexer.py tests/unit/test_indexer_more.py
git commit -m "fix: correct key and bpm analysis"
```

### Task 2: Global smart shuffle and hard-filter expansion

**Files:**
- Modify: `src/autodj/similarity.py:226-318,354-452`
- Modify: `src/autodj/player.py:1030-1039,1098-1180`
- Modify: `tests/unit/test_similarity.py:146-219,388-425`
- Modify: `tests/unit/test_player.py:675-684`
- Modify: `src/autodj/static/index.html:640-650`

- [ ] **Step 1: Write failing tests for global distance, unknown BPM exclusion, and progressive expansion**

Replace the tests that expect BPM relaxation/unknown admission and add the 257-track smart-shuffle fixture:

```python
def test_smart_shuffle_finds_global_farthest_beyond_first_200() -> None:
    n = 257
    query = np.zeros(FEATURE_DIM, dtype=np.float32)
    query[0] = 1.0
    vectors = np.zeros((n, FEATURE_DIM), dtype=np.float32)
    vectors[:, 0] = np.linspace(1.0, -1.0, n, dtype=np.float32)
    vectors[:, 1] = np.sqrt(np.maximum(0.0, 1.0 - vectors[:, 0] ** 2))
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    sim = SimilarityIndex(index, [_make_entry(i) for i in range(n)])

    entries = sim.entries_snapshot()
    result = sim.find_next(query, deque([entries[0].path]), invert=True, n_candidates=10)

    assert result.path == entries[-1].path


def test_unknown_bpm_is_excluded_by_hard_range() -> None:
    sim, vectors = _make_sim_with_bpms([110.0, 0.0, 125.0])

    result = sim.find_next(
        vectors[0],
        deque([sim.entries_snapshot()[0].path]),
        bpm_range=(120.0, 130.0),
        n_candidates=1,
    )

    assert result.bpm == 125.0


def test_empty_hard_range_raises_instead_of_relaxing() -> None:
    sim, vectors = _make_sim_with_bpms([80.0, 82.0, 0.0])

    with pytest.raises(SimilarityError, match="hard filters"):
        sim.find_next(
            vectors[0],
            deque([sim.entries_snapshot()[0].path]),
            bpm_range=(120.0, 130.0),
        )


def test_genre_filter_expands_until_match_outside_initial_window() -> None:
    sim, vectors = _make_similarity_index(80)
    entries = sim.entries_snapshot()
    for entry in entries:
        entry.genre = "Rock"
    entries[70].genre = "Ambient"

    result = sim.find_next(
        vectors[0],
        deque([entries[0].path]),
        n_candidates=5,
        genre_filter=["ambient"],
    )

    assert result.path == entries[70].path


def test_filter_expansion_collects_requested_pool_before_ranking() -> None:
    from unittest.mock import patch

    n = 80
    query = np.zeros(FEATURE_DIM, dtype=np.float32)
    query[0] = 1.0
    vectors = np.zeros((n, FEATURE_DIM), dtype=np.float32)
    vectors[:, 0] = np.linspace(1.0, -1.0, n, dtype=np.float32)
    vectors[:, 1] = np.sqrt(np.maximum(0.0, 1.0 - vectors[:, 0] ** 2))
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    entries = [_make_entry(i) for i in range(n)]
    for entry in entries:
        entry.genre = "Rock"
    for idx in (20, 40, 70):
        entries[idx].genre = "Ambient"
    sim = SimilarityIndex(index, entries)

    with patch(
        "autodj.similarity._softmax_pick",
        side_effect=lambda candidates, _top_k, _temperature: candidates[0][1],
    ) as choose:
        sim.find_next(
            query,
            deque([entries[0].path]),
            n_candidates=3,
            genre_filter=["ambient"],
        )

    assert len(choose.call_args.args[0]) >= 3
```

- [ ] **Step 2: Run selection tests and verify the current violations**

Run: `uv run pytest tests/unit/test_similarity.py -k "global_farthest or unknown_bpm or empty_hard_range or expands_until or collects_requested_pool" -v`

Expected: FAIL because smart shuffle only inspects 200 hits, unknown BPM passes, hard filters relax, and the first candidate window is never expanded.

- [ ] **Step 3: Make the predicate enforce hard BPM and add a progressive FAISS search helper**

Replace the BPM clause in `_build_predicate`, delete `_relax_filters`, and add `_search_with_expansion`:

```python
            if bpm_range is not None:
                lo, hi = bpm_range
                if entry.bpm <= 0 or not (lo <= entry.bpm <= hi):
                    return False
```

```python
    def _search_with_expansion(
        self,
        query: np.ndarray,
        predicate: Callable[[IndexEntry], bool],
        initial_k: int,
        required_candidates: int,
    ) -> list[tuple[float, IndexEntry]]:
        """Double the FAISS window until the requested pool or full index is reached."""
        if self.ntotal == 0:
            return []
        k = min(max(1, initial_k), self.ntotal)
        while True:
            scores_2d, indices_2d = self.faiss_index.search(query, k)
            candidates = self._filter_candidates(scores_2d[0], indices_2d[0], predicate)
            if len(candidates) >= required_candidates or k == self.ntotal:
                return candidates
            next_k = min(self.ntotal, max(k + 1, k * 2))
            logger.info("Expanding candidate search from %d to %d tracks", k, next_k)
            k = next_k
```

- [ ] **Step 4: Replace `find_next` query/filter selection with normalized inversion and explicit preference relaxation**

Replace lines from `excluded = set(recently_played)` through the empty-candidate branch with:

```python
        excluded = set(recently_played)
        query = query_vector.reshape(1, -1).astype(np.float32)
        norm = float(np.linalg.norm(query))
        if not np.isfinite(norm) or norm <= 0.0:
            raise SimilarityError("Query vector is empty or non-finite.")
        query /= norm
        if invert:
            query = -query

        initial_k = self.ntotal if invert else min(
            self.ntotal,
            self._fetch_size(n_candidates, invert, target_bpm, bpm_range)
            + len(excluded)
            + 1,
        )
        predicate = self._build_predicate(
            excluded,
            bpm_range,
            genre_filter,
            harmonic_from,
            harmonic_mode,
            excluded_artists,
            excluded_albums,
            excluded_titles,
        )
        candidates = self._search_with_expansion(
            query, predicate, initial_k, n_candidates,
        )

        if not candidates and any((excluded_artists, excluded_albums, excluded_titles)):
            logger.warning(
                "No candidates after full-index preference search; relaxing artist/album/title exclusions"
            )
            predicate = self._build_predicate(
                excluded,
                bpm_range,
                genre_filter,
                harmonic_from,
                harmonic_mode,
                None,
                None,
                None,
            )
            candidates = self._search_with_expansion(
                query, predicate, self.ntotal, n_candidates,
            )

        if not candidates:
            active = []
            if bpm_range is not None:
                active.append(f"BPM {bpm_range[0]:g}-{bpm_range[1]:g}, known values only")
            if genre_filter:
                active.append("genre " + ", ".join(genre_filter))
            if harmonic_from is not None:
                active.append("harmonic mode " + harmonic_mode)
            detail = "; ".join(active) or "recent-track exclusion"
            raise SimilarityError(f"No candidates satisfy hard filters: {detail}.")
```

Replace the invert branch with:

```python
        if invert:
            candidates.sort(key=lambda item: item[0], reverse=True)
            best = candidates[0][1]
            logger.debug("Smart-shuffle next: %s", best.display_name)
            return best
```

- [ ] **Step 5: Keep pure shuffle inside a configured hard BPM range**

Replace `_pick_pure_shuffle` with:

```python
    def _pick_pure_shuffle(self) -> IndexEntry:
        """Random pick from the non-recent pool without violating hard BPM eligibility."""
        import random as _rnd

        from autodj.similarity import SimilarityError

        excluded = set(self._state.recently_played)

        def eligible(entry: IndexEntry) -> bool:
            if self._bpm_range is None:
                return True
            lo, hi = self._bpm_range
            return entry.bpm > 0 and lo <= entry.bpm <= hi

        entries = self._sim.entries_snapshot()
        pool = [e for e in entries if e.path not in excluded and eligible(e)]
        if not pool:
            logger.warning(
                "Pure shuffle exhausted eligible non-recent tracks; "
                "relaxing recent-track exclusion"
            )
            pool = [
                e for e in entries
                if e.path != self._state.current_track.path and eligible(e)
            ]
        if not pool:
            raise SimilarityError("No candidates satisfy hard filters for pure shuffle.")
        self._last_pick_mode = "pure_shuffle"
        return _rnd.choice(pool)  # nosec B311 -- non-security
```

Add to `TestPlayerPickNext`:

```python
    def test_pure_shuffle_does_not_admit_unknown_or_out_of_range_bpm(self) -> None:
        player = self._make_player(n=4, pure_shuffle=True, bpm_range=(120.0, 130.0))
        entries = player._sim.entries_snapshot()
        entries[0].bpm = 110.0
        entries[1].bpm = 0.0
        entries[2].bpm = 150.0
        entries[3].bpm = 125.0
        player._state.current_track = entries[0]

        assert player._pick_next(entries[0]).path == entries[3].path

    def test_pure_shuffle_logs_when_recent_exclusion_is_relaxed(self, caplog) -> None:
        import logging

        player = self._make_player(n=3, pure_shuffle=True)
        entries = player._sim.entries_snapshot()
        player._state.current_track = entries[0]
        player._state.recently_played = deque(entry.path for entry in entries)

        with caplog.at_level(logging.WARNING):
            selected = player._pick_next(entries[0])

        assert selected.path in {entries[1].path, entries[2].path}
        assert len([
            record for record in caplog.records
            if "relaxing recent-track exclusion" in record.message
        ]) == 1
```

- [ ] **Step 6: Explain unknown-BPM exclusion in the browser setting**

Replace the BPM description in `index.html` with:

```html
      <span class="setting-desc" id="bpm-desc">
        Hard filter on track tempo. Picks only fall within this range, and tracks
        with unknown BPM are excluded while the filter is active. Leave both fields
        blank to disable the filter.
      </span>
```

- [ ] **Step 7: Run selection and player regression tests**

Run: `uv run pytest tests/unit/test_similarity.py tests/unit/test_player.py -q`

Expected: PASS, including the updated tests that now require a clear `SimilarityError` when no hard-eligible track exists.

- [ ] **Step 8: Commit selection correctness**

```bash
git add src/autodj/similarity.py src/autodj/player.py src/autodj/static/index.html tests/unit/test_similarity.py tests/unit/test_player.py
git commit -m "fix: enforce global hard-filtered selection"
```

### Task 3: Versioned, field-specific runtime-state round trip

**Files:**
- Modify: `src/autodj/runtime_state.py:1-207`
- Modify: `src/autodj/_bridge.py:870-945`
- Modify: `tests/unit/test_runtime_state.py:1-259`

- [ ] **Step 1: Expand the fixture and write failing schema/round-trip tests**

Replace `_make_player` with a concrete `SimpleNamespace` tree so invalid-type tests cannot accidentally pass through `MagicMock` coercion. Add `from types import SimpleNamespace` and use:

```python
def _make_player() -> SimpleNamespace:
    playback = SimpleNamespace(
        crossfade_seconds=3.0,
        fade_in_seconds=3.0,
        crossfade_eq_duck=False,
        transition_mode="full_intro_outro",
        post_queue_seed="last_queued",
        key_notation="camelot",
        key_prefer_flats=False,
        show_lyrics=True,
        enable_daypart=False,
        enable_mood_arc=False,
        mood_arc_hours=3.0,
        import_external_cues=True,
        beat_sync_fx=True,
        key_sync_fx=True,
        beatmatch_on_skip=False,
        prefetch_next_track=True,
        silence_trigger_crossfade=True,
        liners_enabled=False,
        liners_every_n_songs=None,
        liners_every_minutes=None,
        liners_random_min_minutes=None,
        liners_random_max_minutes=None,
        liners_pick_mode="random",
        liners_duck_db=-12.0,
    )
    cfg = SimpleNamespace(
        transitions=SimpleNamespace(effect="none"),
        djmix=SimpleNamespace(
            harmonic_mixing=False,
            harmonic_mode="compatible",
            beatmatch=False,
            phrase_align=False,
            outro_intro_align=False,
            filter_sweep=False,
        ),
        playback=playback,
        replaygain=SimpleNamespace(enabled=False),
        presets={},
    )
    return SimpleNamespace(
        _cfg=cfg,
        _smart_shuffle=False,
        _pure_shuffle=False,
        _anchor_to_seed=False,
        _bpm_range=None,
        _preset=None,
        _discovery_every=None,
        _mood_arc=None,
        _state=SimpleNamespace(no_repeat_window=20),
        _sim=SimpleNamespace(entries_snapshot=lambda: (), ntotal=0),
    )
```

Replace `TestRoundTrip.test_save_then_load_preserves_settings` with this bridge-owned exhaustive round trip:

```python
def test_save_bridge_snapshot_then_load_preserves_every_persisted_field(tmp_path) -> None:
    from autodj._bridge import PlayerBridge

    p1 = _make_player()
    p1._cfg.djmix.harmonic_mixing = True
    p1._cfg.djmix.harmonic_mode = "strict"
    p1._cfg.djmix.beatmatch = True
    p1._cfg.djmix.phrase_align = True
    p1._cfg.djmix.outro_intro_align = True
    p1._cfg.djmix.filter_sweep = True
    p1._cfg.transitions.effect = "echo_out"
    p1._cfg.playback.crossfade_seconds = 6.0
    p1._cfg.playback.fade_in_seconds = 1.5
    p1._cfg.playback.crossfade_eq_duck = True
    p1._smart_shuffle = True
    p1._pure_shuffle = True
    p1._anchor_to_seed = True
    p1._cfg.replaygain.enabled = True
    p1._cfg.playback.transition_mode = "fixed"
    p1._cfg.playback.post_queue_seed = "pre_queue"
    p1._cfg.playback.key_notation = "musical"
    p1._cfg.playback.key_prefer_flats = True
    p1._cfg.playback.show_lyrics = False
    p1._cfg.playback.enable_daypart = True
    p1._cfg.playback.enable_mood_arc = True
    p1._cfg.playback.mood_arc_hours = 2.5
    p1._cfg.playback.import_external_cues = False
    p1._cfg.playback.beat_sync_fx = False
    p1._cfg.playback.key_sync_fx = False
    p1._cfg.playback.beatmatch_on_skip = True
    p1._cfg.playback.prefetch_next_track = False
    p1._cfg.playback.silence_trigger_crossfade = False
    p1._cfg.playback.liners_enabled = True
    p1._cfg.playback.liners_folder = "Z:/Station/Private/Liners"
    p1._cfg.playback.liners_every_n_songs = 3
    p1._cfg.playback.liners_every_minutes = None
    p1._cfg.playback.liners_random_min_minutes = 8.0
    p1._cfg.playback.liners_random_max_minutes = 14.0
    p1._cfg.playback.liners_pick_mode = "sequential"
    p1._cfg.playback.liners_duck_db = -9.0
    p1._bpm_range = (100.0, 132.0)
    p1._discovery_every = 9

    bridge1 = PlayerBridge(p1, p1._sim)
    saved = bridge1.get_settings()
    assert "liners_folder" not in saved["playback"]
    saved["playback"]["liners_folder"] = p1._cfg.playback.liners_folder
    save_from_player(saved, tmp_path)
    stored = json.loads((tmp_path / "web_state.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert "available_presets" not in stored
    assert "no_repeat_window" not in stored["playback"]
    assert "library_size" not in stored["playback"]
    assert "liners_folder" not in stored["playback"]

    p2 = _make_player()
    load_into_player(p2, tmp_path)
    restored = PlayerBridge(p2, p2._sim).get_settings()
    expected_playback = {
        key: value
        for key, value in saved["playback"].items()
        if key not in {"no_repeat_window", "library_size", "liners_folder"}
    }
    assert restored["transition"] == saved["transition"]
    assert restored["djmix"] == saved["djmix"]
    assert {
        key: restored["playback"][key] for key in expected_playback
    } == expected_playback
    assert restored["bpm_range"] == saved["bpm_range"]
    assert restored["discovery_every"] == saved["discovery_every"]
```

Add these exact rejection, forward-version, unknown-field, and null-clearing tests:

```python
def _write_state(tmp_path, data: dict) -> None:
    (tmp_path / "web_state.json").write_text(json.dumps(data), encoding="utf-8")


def test_string_false_is_rejected_instead_of_coerced(tmp_path, caplog) -> None:
    _write_state(tmp_path, {"schema_version": 1, "playback": {"prefetch_next_track": "false"}})
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.prefetch_next_track is True
    assert [record for record in caplog.records if "prefetch_next_track" in record.message]


def test_invalid_harmonic_mode_warns_once_and_keeps_default(tmp_path, caplog) -> None:
    _write_state(tmp_path, {"schema_version": 1, "djmix": {"harmonic_mode": "same_key"}})
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.djmix.harmonic_mode == "compatible"
    assert len([record for record in caplog.records if "harmonic_mode" in record.message]) == 1


def test_invalid_enable_mood_arc_warns_once_and_keeps_default(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "playback": {"enable_mood_arc": "false"}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.enable_mood_arc is False
    assert len([
        record for record in caplog.records
        if "enable_mood_arc" in record.message
    ]) == 1


def test_future_version_warns_but_restores_known_fields(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 99, "playback": {"prefetch_next_track": False}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.prefetch_next_track is False
    assert len([record for record in caplog.records if "schema_version 99" in record.message]) == 1


def test_unknown_future_field_is_ignored(tmp_path) -> None:
    _write_state(tmp_path, {"schema_version": 1, "playback": {"quantum_crossfade": True}})
    player = _make_player()

    load_into_player(player, tmp_path)

    assert not hasattr(player._cfg.playback, "quantum_crossfade")


def test_null_clears_every_nullable_liner_cadence(tmp_path) -> None:
    player = _make_player()
    player._cfg.playback.liners_every_n_songs = 2
    player._cfg.playback.liners_every_minutes = 5.0
    player._cfg.playback.liners_random_min_minutes = 7.0
    player._cfg.playback.liners_random_max_minutes = 12.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {
                "liners_every_n_songs": None,
                "liners_every_minutes": None,
                "liners_random_min_minutes": None,
                "liners_random_max_minutes": None,
            },
        },
    )

    load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_every_n_songs is None
    assert player._cfg.playback.liners_every_minutes is None
    assert player._cfg.playback.liners_random_min_minutes is None
    assert player._cfg.playback.liners_random_max_minutes is None


def test_null_bpm_range_clears_an_existing_range(tmp_path) -> None:
    player = _make_player()
    player._bpm_range = (90.0, 130.0)
    _write_state(tmp_path, {"schema_version": 1, "bpm_range": None})

    load_into_player(player, tmp_path)

    assert player._bpm_range is None
```

- [ ] **Step 2: Run persistence tests and verify the red state**

Run: `uv run pytest tests/unit/test_runtime_state.py -v`

Expected: FAIL because `harmonic_mode` is cast through `bool`, emitted fields are not restored, liner settings are not emitted, derived fields are saved, and no schema version exists.

- [ ] **Step 3: Declare the persisted schema and centralized field maps**

Add these definitions after `logger`:

```python
from typing import NotRequired, TypedDict

STATE_VERSION = 1
HARMONIC_MODES = frozenset(
    {"off", "compatible", "strict", "energy_boost", "mood_change", "neighbour"}
)
LINER_PICK_MODES = frozenset({"random", "sequential", "weighted"})
TRANSITION_EFFECTS = frozenset({
    "none", "echo_out", "reverb_tail", "highpass_sweep", "lowpass_sweep",
    "tape_stop", "gate_stutter", "noise_riser", "noise_drop", "backspin",
    "forward_spin", "cross_eq_swap", "bitcrusher", "flanger", "pitch_swell",
    "telephone", "chorus", "submerge", "vinyl_wow", "freeze", "glitch",
    "scratch", "beat_repeat", "sidechain_pump", "reverse_reverb", "air_horn",
    "random", "rotate",
})
SESSION_ONLY_PLAYBACK_FIELDS = frozenset({"no_repeat_window", "library_size"})
CONFIG_ONLY_PLAYBACK_FIELDS = frozenset({"liners_folder"})


class DJMixState(TypedDict, total=False):
    harmonic_mixing: bool
    harmonic_mode: str
    beatmatch: bool
    phrase_align: bool
    outro_intro_align: bool
    filter_sweep: bool


class PlaybackState(TypedDict, total=False):
    crossfade_seconds: float
    fade_in_seconds: float
    crossfade_eq_duck: bool
    smart_shuffle: bool
    pure_shuffle: bool
    anchor_to_seed: bool
    replaygain_enabled: bool
    transition_mode: str
    post_queue_seed: str
    key_notation: str
    key_prefer_flats: bool
    show_lyrics: bool
    enable_daypart: bool
    enable_mood_arc: bool
    mood_arc_hours: float
    import_external_cues: bool
    beat_sync_fx: bool
    key_sync_fx: bool
    beatmatch_on_skip: bool
    prefetch_next_track: bool
    silence_trigger_crossfade: bool
    liners_enabled: bool
    liners_every_n_songs: int | None
    liners_every_minutes: float | None
    liners_random_min_minutes: float | None
    liners_random_max_minutes: float | None
    liners_pick_mode: str
    liners_duck_db: float


class PersistedState(TypedDict):
    schema_version: int
    preset: NotRequired[str | None]
    transition: NotRequired[str]
    djmix: NotRequired[DJMixState]
    playback: NotRequired[PlaybackState]
    bpm_range: NotRequired[dict[str, float | None] | None]
    discovery_every: NotRequired[int | None]


DJMIX_BOOL_FIELDS = (
    "harmonic_mixing", "beatmatch", "phrase_align", "outro_intro_align", "filter_sweep",
)
PLAYBACK_CFG_BOOL_FIELDS = (
    "crossfade_eq_duck", "show_lyrics", "enable_daypart", "import_external_cues",
    "key_prefer_flats", "beat_sync_fx", "key_sync_fx", "beatmatch_on_skip",
    "prefetch_next_track", "silence_trigger_crossfade", "liners_enabled",
)
PLAYER_BOOL_FIELDS = {
    "smart_shuffle": "_smart_shuffle",
    "pure_shuffle": "_pure_shuffle",
    "anchor_to_seed": "_anchor_to_seed",
}
PERSISTED_PLAYBACK_FIELDS = frozenset({
    "crossfade_seconds", "fade_in_seconds", "crossfade_eq_duck",
    "smart_shuffle", "pure_shuffle", "anchor_to_seed", "replaygain_enabled",
    "transition_mode", "post_queue_seed", "key_notation", "key_prefer_flats",
    "show_lyrics", "enable_daypart", "enable_mood_arc", "mood_arc_hours",
    "import_external_cues", "beat_sync_fx", "key_sync_fx", "beatmatch_on_skip",
    "prefetch_next_track", "silence_trigger_crossfade", "liners_enabled",
    "liners_every_n_songs", "liners_every_minutes",
    "liners_random_min_minutes", "liners_random_max_minutes", "liners_pick_mode",
    "liners_duck_db",
})
```

Keep `enable_mood_arc` out of `PLAYBACK_CFG_BOOL_FIELDS`. `_restore_mood_arc` is its sole
reader because it must update both the config flag and the derived runtime arc. This
ensures one invalid value produces one warning and no generic-then-specialized double
restore.

- [ ] **Step 4: Replace coercion with typed readers and field-specific restoration**

Replace `_restore_djmix`, `_restore_playback_floats`, `_restore_playback_bools`, `_restore_mood_arc`, and `_restore_validated_strings`, and add the shared readers plus `_restore_liners`, with this block:

```python
def _warn(field: str, value: object) -> None:
    logger.warning("ignoring invalid %s in web_state.json: %r", field, value)


def _read_bool(data: dict, field: str) -> bool | None:
    if field not in data:
        return None
    value = data[field]
    if type(value) is bool:
        return value
    _warn(field, value)
    return None


def _read_number(data: dict, field: str, minimum: float) -> float | None:
    if field not in data:
        return None
    value = data[field]
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= minimum:
        return float(value)
    _warn(field, value)
    return None


def _restore_preset(player: Any, data: dict) -> None:
    if "preset" not in data:
        return
    value = data["preset"]
    if value is None or value == "":
        player._preset = None
        return
    if not isinstance(value, str):
        _warn("preset", value)
        return
    from autodj.presets import get_preset

    try:
        player._preset = get_preset(value, player._cfg.presets)
    except ValueError:
        _warn("preset", value)


def _restore_djmix(cfg: Any, data: dict) -> None:
    djmix = data.get("djmix")
    if not isinstance(djmix, dict):
        return
    for field in DJMIX_BOOL_FIELDS:
        value = _read_bool(djmix, field)
        if value is not None:
            setattr(cfg.djmix, field, value)
    if "harmonic_mode" in djmix:
        value = djmix["harmonic_mode"]
        if isinstance(value, str) and value in HARMONIC_MODES:
            cfg.djmix.harmonic_mode = value
        else:
            _warn("harmonic_mode", value)


def _restore_playback_floats(cfg: Any, pb: dict) -> None:
    for field, minimum in (
        ("crossfade_seconds", 0.0),
        ("fade_in_seconds", 0.0),
        ("mood_arc_hours", 0.25),
    ):
        value = _read_number(pb, field, minimum)
        if value is not None:
            setattr(cfg.playback, field, value)


def _restore_playback_bools(cfg: Any, player: Any, pb: dict) -> None:
    for field in PLAYBACK_CFG_BOOL_FIELDS:
        value = _read_bool(pb, field)
        if value is not None:
            setattr(cfg.playback, field, value)
    for field, attribute in PLAYER_BOOL_FIELDS.items():
        value = _read_bool(pb, field)
        if value is not None:
            setattr(player, attribute, value)
    replaygain = _read_bool(pb, "replaygain_enabled")
    if replaygain is not None:
        cfg.replaygain.enabled = replaygain


def _restore_mood_arc(cfg: Any, player: Any, pb: dict) -> None:
    enabled = _read_bool(pb, "enable_mood_arc")
    if enabled is None:
        return
    cfg.playback.enable_mood_arc = enabled
    if not enabled:
        player._mood_arc = None
        return
    from autodj.mood_arc import make_default_arc

    player._mood_arc = make_default_arc(
        duration_hours=cfg.playback.mood_arc_hours,
    )


def _restore_validated_strings(cfg: Any, pb: dict) -> None:
    from autodj.config import (
        _validate_key_notation,
        _validate_post_queue_seed,
        _validate_transition_mode,
    )

    validators = {
        "transition_mode": _validate_transition_mode,
        "post_queue_seed": _validate_post_queue_seed,
        "key_notation": _validate_key_notation,
    }
    for field, validator in validators.items():
        if field not in pb:
            continue
        value = pb[field]
        if not isinstance(value, str):
            _warn(field, value)
            continue
        try:
            setattr(cfg.playback, field, validator(value))
        except ValueError:
            _warn(field, value)


def _restore_nullable_number(target: Any, pb: dict, field: str, *, integer: bool = False) -> None:
    if field not in pb:
        return
    value = pb[field]
    if value is None:
        setattr(target, field, None)
        return
    valid_type = type(value) is int if integer else (
        not isinstance(value, bool) and isinstance(value, (int, float))
    )
    if not valid_type or value <= 0:
        _warn(field, value)
        return
    setattr(target, field, int(value) if integer else float(value))


def _restore_liners(cfg: Any, pb: dict) -> None:
    playback = cfg.playback
    _restore_nullable_number(playback, pb, "liners_every_n_songs", integer=True)
    _restore_nullable_number(playback, pb, "liners_every_minutes")
    _restore_nullable_number(playback, pb, "liners_random_min_minutes")
    _restore_nullable_number(playback, pb, "liners_random_max_minutes")
    if "liners_pick_mode" in pb:
        value = pb["liners_pick_mode"]
        if isinstance(value, str) and value in LINER_PICK_MODES:
            playback.liners_pick_mode = value
        else:
            _warn("liners_pick_mode", value)
    if "liners_duck_db" in pb:
        value = pb["liners_duck_db"]
        if not isinstance(value, bool) and isinstance(value, (int, float)) and -60 <= value <= 0:
            playback.liners_duck_db = float(value)
        else:
            _warn("liners_duck_db", value)
```

After JSON parsing in `load_into_player`, replace its restore dispatch with this exact version/type gate and call list:

```python
    if not isinstance(data, dict):
        logger.warning("web_state.json root is not an object, ignoring")
        return
    version = data.get("schema_version", 0)
    if type(version) is not int or version < 0:
        _warn("schema_version", version)
        return
    if version > STATE_VERSION:
        logger.warning(
            "web_state.json schema_version %d is newer than supported version %d; applying known fields",
            version,
            STATE_VERSION,
        )

    cfg = player._cfg
    _restore_preset(player, data)
    _restore_transition(cfg, data)
    _restore_djmix(cfg, data)
    playback = data.get("playback")
    if isinstance(playback, dict):
        _restore_playback_floats(cfg, playback)
        _restore_playback_bools(cfg, player, playback)
        _restore_mood_arc(cfg, player, playback)
        _restore_validated_strings(cfg, playback)
        _restore_liners(cfg, playback)
    _restore_bpm_range(player, data)
    _restore_discovery(player, data)
```

Replace `_restore_transition`, `_restore_bpm_range`, and `_restore_discovery` with exact-type validation rather than `str`/`int` coercion:

```python
def _restore_transition(cfg: Any, data: dict) -> None:
    if "transition" not in data:
        return
    value = data["transition"]
    if isinstance(value, str) and value.lower() in TRANSITION_EFFECTS:
        cfg.transitions.effect = value.lower()
    else:
        _warn("transition", value)


def _restore_bpm_range(player: Any, data: dict) -> None:
    if "bpm_range" not in data:
        return
    value = data["bpm_range"]
    if value is None:
        player._bpm_range = None
        return
    if not isinstance(value, dict):
        _warn("bpm_range", value)
        return
    lo, hi = value.get("lo"), value.get("hi")
    if lo is None and hi is None:
        player._bpm_range = None
    elif all(not isinstance(v, bool) and isinstance(v, (int, float)) for v in (lo, hi)) and lo < hi:
        player._bpm_range = (float(lo), float(hi))
    else:
        _warn("bpm_range", value)


def _restore_discovery(player: Any, data: dict) -> None:
    if "discovery_every" not in data:
        return
    value = data["discovery_every"]
    if value is None:
        player._discovery_every = None
    elif type(value) is int and value >= 0:
        player._discovery_every = value or None
    else:
        _warn("discovery_every", value)
```

- [ ] **Step 5: Emit non-sensitive liner cadence fields and save an allowlisted payload**

Append these keys to the `playback` object in `PlayerBridge.get_settings`:

```python
                "liners_enabled": bool(getattr(cfg.playback, "liners_enabled", False)),
                "liners_every_n_songs": getattr(cfg.playback, "liners_every_n_songs", None),
                "liners_every_minutes": getattr(cfg.playback, "liners_every_minutes", None),
                "liners_random_min_minutes": getattr(cfg.playback, "liners_random_min_minutes", None),
                "liners_random_max_minutes": getattr(cfg.playback, "liners_random_max_minutes", None),
                "liners_pick_mode": getattr(cfg.playback, "liners_pick_mode", "random"),
                "liners_duck_db": getattr(cfg.playback, "liners_duck_db", -12.0),
```

Do not add `liners_folder` to `PlayerBridge.get_settings`, `PlaybackState`, or `PERSISTED_PLAYBACK_FIELDS`. It is an operator-configured absolute path and remains intentionally config/session-only; duplicating it into browser settings or `web_state.json` would create a new path-disclosure surface. Add this sentence to `runtime_state.py`'s module docstring: `The liner source folder remains config-owned and is never copied into browser-owned state.` The existing authenticated `/api/liners` inventory route is not expanded by this task.

Replace the payload construction in `save_from_player` so version and session-only handling are explicit:

```python
    playback = settings.get("playback", {})
    persisted_playback = {
        key: value
        for key, value in playback.items()
        if key in PERSISTED_PLAYBACK_FIELDS
    } if isinstance(playback, dict) else {}
    payload: PersistedState = {
        "schema_version": STATE_VERSION,
        "preset": settings.get("preset"),
        "transition": settings.get("transition", "none"),
        "djmix": settings.get("djmix", {}),
        "playback": persisted_playback,
        "bpm_range": settings.get("bpm_range", {"lo": None, "hi": None}),
        "discovery_every": settings.get("discovery_every"),
    }
```

- [ ] **Step 6: Run persistence tests and type/lint checks**

Run: `uv run pytest tests/unit/test_runtime_state.py tests/integration/test_server.py -k "settings or runtime or round_trip" -q`

Run: `uv run ruff check src/autodj/runtime_state.py src/autodj/_bridge.py tests/unit/test_runtime_state.py`

Expected: both commands PASS; every bridge-emitted non-derived field either restores or is explicitly excluded as session-only.

- [ ] **Step 7: Commit runtime-state correctness**

```bash
git add src/autodj/runtime_state.py src/autodj/_bridge.py tests/unit/test_runtime_state.py
git commit -m "fix: version and validate web settings state"
```

### Task 4: Nonblocking, RFC-shaped HTTP audio ranges and ALAC preflight

**Files:**
- Create: `src/autodj/http_media.py`
- Create: `tests/unit/test_http_media.py`
- Modify: `src/autodj/server.py:279-354,811-829,893-1002`
- Modify: `tests/integration/test_server.py:2002-2084`

- [ ] **Step 1: Write failing pure parser and iterator-lifecycle tests**

Create `tests/unit/test_http_media.py` exactly as follows:

```python
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from autodj.http_media import (
    ByteRange,
    RangeNotSatisfiable,
    iter_file_chunks,
    parse_single_range,
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-99", (0, 99)),
        ("BYTES=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-100", (900, 999)),
    ],
)
def test_parse_single_range_forms(header: str, expected: tuple[int, int]) -> None:
    assert parse_single_range(header, 1000) == ByteRange(*expected)


@pytest.mark.parametrize(
    "header",
    ["bytes=", "bytes=10-5", "bytes=1000-", "bytes=0-1,4-5", "kilobytes=0-5"],
)
def test_invalid_or_unsupported_ranges_are_unsatisfiable(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range(header, 1000)


def test_partial_consumer_close_closes_file_handle(monkeypatch) -> None:
    handle = BytesIO(bytes(range(100)))
    monkeypatch.setattr(Path, "open", lambda _self, _mode: handle)
    stream = iter_file_chunks(
        Path("track.bin"),
        ByteRange(10, 29),
        chunk_size=10,
    )

    assert next(stream) == bytes(range(10, 20))
    stream.close()

    assert handle.closed is True
```

- [ ] **Step 2: Run the new unit test and verify the red state**

Run: `uv run pytest tests/unit/test_http_media.py -v`

Expected: collection ERROR because `autodj.http_media` does not exist.

- [ ] **Step 3: Create the pure parser and synchronous iterator**

Create `src/autodj/http_media.py` exactly as follows:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class RangeNotSatisfiable(ValueError):
    """Raised when a Range header cannot identify one satisfiable byte range."""


@dataclass(frozen=True)
class ByteRange:
    """Inclusive byte offsets selected from a representation."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_single_range(header: str, size: int) -> ByteRange:
    """Parse one RFC 9110 byte range against a representation of *size*."""
    unit, separator, spec = header.partition("=")
    if size <= 0 or not separator or unit.strip().lower() != "bytes":
        raise RangeNotSatisfiable(header)
    spec = spec.strip()
    if not spec or "," in spec or spec.count("-") != 1:
        raise RangeNotSatisfiable(header)
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise RangeNotSatisfiable(header)
            return ByteRange(max(0, size - suffix), size - 1)
        start = int(start_text)
        if start < 0 or start >= size:
            raise RangeNotSatisfiable(header)
        end = size - 1 if not end_text else min(int(end_text), size - 1)
    except ValueError as exc:
        raise RangeNotSatisfiable(header) from exc
    if end < start:
        raise RangeNotSatisfiable(header)
    return ByteRange(start, end)


def iter_file_chunks(
    path: Path,
    byte_range: ByteRange | None = None,
    chunk_size: int = 256 * 1024,
) -> Generator[bytes, None, None]:
    """Yield a whole file or inclusive range and close it on iterator close."""
    with path.open("rb") as handle:
        remaining: int | None = None
        if byte_range is not None:
            handle.seek(byte_range.start)
            remaining = byte_range.length
        while remaining is None or remaining > 0:
            amount = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = handle.read(amount)
            if not chunk:
                break
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)
```

Replace `from typing import Iterator` with the imports below and add the explicit async
ownership adapter after `iter_file_chunks`. It keeps every blocking `next()` in
Starlette's threadpool but closes the underlying synchronous generator deterministically
when ASGI cancellation stops consumption:

```python
from collections.abc import AsyncIterator, Generator, Iterator

from starlette.concurrency import run_in_threadpool


def _next_chunk(iterator: Iterator[bytes]) -> bytes | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


async def stream_file_chunks(
    path: Path,
    byte_range: ByteRange | None = None,
    chunk_size: int = 256 * 1024,
) -> AsyncIterator[bytes]:
    iterator = iter_file_chunks(path, byte_range, chunk_size)
    try:
        while (chunk := await run_in_threadpool(_next_chunk, iterator)) is not None:
            yield chunk
    finally:
        iterator.close()
```

- [ ] **Step 4: Replace async blocking reads and preflight ALAC before response construction**

Add `import shutil` and these imports at module scope:

```python
from autodj.http_media import (
    RangeNotSatisfiable,
    parse_single_range,
    stream_file_chunks,
)
```

Delete the nested `_MIME_BY_SUFFIX`, `_audio_mime`, and `_is_alac` definitions and add this module-level block before `create_app`. This gives the route a deterministic monkeypatch seam:

```python
_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}


def _audio_mime(path: Path) -> str:
    """Return the MIME type associated with *path*'s audio suffix."""
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def _is_alac(path: Path) -> bool:
    """Return whether an MP4-family file reports Apple Lossless audio."""
    if path.suffix.lower() not in (".m4a", ".mp4"):
        return False
    try:
        from mutagen.mp4 import MP4

        codec = getattr(MP4(str(path)).info, "codec", None) or ""
        return codec.lower() == "alac"
    except (OSError, ValueError, ImportError):
        return False
```

Move `_transcode_alac_to_mp3` to module scope as well, retaining its process-owned `finally` exactly so cancellation after headers kills and reaps ffmpeg:

```python
async def _transcode_alac_to_mp3(path: Path) -> AsyncGenerator[bytes]:
    """Yield transcoded MP3 bytes and always reap the ffmpeg subprocess."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        if process.stdout is None:
            return
        while chunk := await process.stdout.read(64 * 1024):
            yield chunk
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
```

Then replace the entire `/api/audio` route with:

```python
    @app.get("/api/audio")
    async def api_audio(path: str, request: Request) -> Response:
        if bridge.sim.entry_for_path(path) is None:
            raise HTTPException(status_code=404, detail="Track not in index")
        audio_path = Path(path)
        try:
            is_file = await asyncio.to_thread(audio_path.is_file)
            if not is_file:
                raise OSError("not a regular file")
            file_size = (await asyncio.to_thread(audio_path.stat)).st_size
        except OSError:
            raise HTTPException(status_code=404, detail="File not found on disk") from None

        if await asyncio.to_thread(_is_alac, audio_path):
            if shutil.which("ffmpeg") is None:
                logger.warning(
                    "ffmpeg unavailable; serving ALAC source bytes for %s",
                    audio_path,
                )
            else:
                return StreamingResponse(
                    _transcode_alac_to_mp3(audio_path),
                    media_type="audio/mpeg",
                    headers={"Accept-Ranges": "none"},
                )

        mime = _audio_mime(audio_path)
        range_header = request.headers.get("range")
        if range_header is None:
            return StreamingResponse(
                stream_file_chunks(audio_path),
                media_type=mime,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                },
            )

        try:
            requested_range = parse_single_range(range_header, file_size)
        except RangeNotSatisfiable:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        return StreamingResponse(
            stream_file_chunks(audio_path, requested_range),
            status_code=206,
            media_type=mime,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": (
                    f"bytes {requested_range.start}-{requested_range.end}/{file_size}"
                ),
                "Content-Length": str(requested_range.length),
            },
        )
```

Keep `iter_file_chunks` synchronous and use only `stream_file_chunks` at the route. The
adapter contains no `open()` or `read()`; it delegates each `next()` to the threadpool and
exists solely to make cancellation close ownership explicit.

- [ ] **Step 5: Add route-level range, loop-responsiveness, and fallback tests**

Add these imports to `tests/integration/test_server.py`:

```python
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, MagicMock
```

Add this controlled-fixture helper beside the imports. It mutates only the mock's test data while configuring the same post-security APIs production consumes:

```python
def _register_index_entry(bridge, entry) -> None:
    entries = [*bridge.sim.entries_snapshot(), entry]
    bridge.sim.entries_snapshot.side_effect = lambda: tuple(entries)
    bridge.sim.entry_for_path.side_effect = lambda path: next(
        (candidate for candidate in entries if candidate.path == path),
        None,
    )
```

Add the following tests to `TestAudioEndpoint`:

```python
    def test_audio_suffix_range_returns_final_bytes(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "suffix.flac"
        audio.write_bytes(bytes(range(100)))
        entry = _make_entry(126)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)

        response = TestClient(create_app(bridge)).get(
            f"/api/audio?path={audio}",
            headers={"Range": "bytes=-10"},
        )

        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 90-99/100"
        assert response.content == bytes(range(90, 100))

    @pytest.mark.parametrize("header", ["bytes=100-", "bytes=0-1,4-5"])
    def test_audio_unsatisfiable_or_multi_range_has_size_header(
        self, tmp_path, bridge, header
    ) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "range.flac"
        audio.write_bytes(b"x" * 100)
        entry = _make_entry(127)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)

        response = TestClient(create_app(bridge)).get(
            f"/api/audio?path={audio}", headers={"Range": header}
        )

        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */100"

    def test_missing_ffmpeg_falls_back_before_alac_headers(
        self, tmp_path, bridge, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "lossless.m4a"
        audio.write_bytes(b"raw-alac-fixture")
        entry = _make_entry(128)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)
        monkeypatch.setattr("autodj.server._is_alac", lambda _path: True)
        monkeypatch.setattr("autodj.server.shutil.which", lambda _name: None)

        response = TestClient(create_app(bridge)).get(f"/api/audio?path={audio}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mp4"
        assert response.content == b"raw-alac-fixture"
```

Add this module-level async test. It uses the repository's existing `httpx2` transport and fails deterministically on the current route because that route never advances the new `Path.open` iterator seam:

```python
async def test_audio_read_runs_off_event_loop(tmp_path, bridge, monkeypatch) -> None:
    from httpx2 import ASGITransport, AsyncClient

    audio = tmp_path / "slow.flac"
    audio.write_bytes(b"x" * (512 * 1024))
    entry = _make_entry(129)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    opened = threading.Event()
    release = threading.Event()
    real_open = Path.open

    class SlowFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.handle.close()

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            opened.set()
            assert release.wait(2.0)
            return self.handle.read(size)

    def slow_open(path, *args, **kwargs):
        return SlowFile(real_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", slow_open)
    app = create_app(bridge)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request_task = asyncio.create_task(client.get(f"/api/audio?path={audio}"))
        try:
            opened_in_time = await asyncio.to_thread(opened.wait, 1.0)
            if opened_in_time:
                loop_was_responsive = asyncio.Event()
                asyncio.get_running_loop().call_soon(loop_was_responsive.set)
                await asyncio.wait_for(loop_was_responsive.wait(), timeout=0.2)
        finally:
            release.set()
        response = await request_task

    assert opened_in_time is True
    assert response.status_code == 200
    assert len(response.content) == 512 * 1024


async def test_alac_transcoder_reaps_process_when_consumer_closes(
    tmp_path, monkeypatch
) -> None:
    from autodj.server import _transcode_alac_to_mp3

    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"mp3-chunk")),
        kill=MagicMock(),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    stream = _transcode_alac_to_mp3(tmp_path / "source.m4a")

    assert await anext(stream) == b"mp3-chunk"
    await stream.aclose()

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()
```

Add this ASGI cancellation driver and two route-level disconnect tests. Unlike direct
generator `close()`/`aclose()`, these start the real `StreamingResponse`, consume one
body chunk through ASGI `send`, and then deliver `http.disconnect` through `receive`:

```python
async def _cancel_asgi_after_first_body(
    app, *, path: str, expected_prefix: bytes
) -> None:
    first_body = asyncio.Event()
    never = asyncio.Event()
    request_sent = False
    response_status = None
    first_chunk = b""

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_body.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal response_status, first_chunk
        if message["type"] == "http.response.start":
            response_status = message["status"]
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk = message["body"]
            first_body.set()
            await never.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/audio",
        "raw_path": b"/api/audio",
        "query_string": urlencode({"path": path}).encode("ascii"),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=1.0)
    assert response_status == 200
    assert first_chunk.startswith(expected_prefix)


async def test_partial_asgi_file_response_closes_open_handle(
    bridge, tmp_path, monkeypatch
) -> None:
    audio = tmp_path / "large.flac"
    audio.write_bytes(b"x" * (256 * 1024))
    entry = _make_entry(128)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    real_open = Path.open
    closed = threading.Event()

    class TrackedFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.handle.close()
            closed.set()

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def tracked_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return TrackedFile(handle) if path == audio else handle

    monkeypatch.setattr(Path, "open", tracked_open)

    await _cancel_asgi_after_first_body(
        create_app(bridge), path=str(audio), expected_prefix=b"x"
    )

    assert closed.wait(1.0) is True


async def test_partial_asgi_alac_response_cancels_and_reaps_ffmpeg(
    bridge, tmp_path, monkeypatch
) -> None:
    audio = tmp_path / "source.m4a"
    audio.write_bytes(b"alac")
    entry = _make_entry(129)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"mp3-chunk")),
        kill=MagicMock(),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr("autodj.server._is_alac", lambda _path: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    await _cancel_asgi_after_first_body(
        create_app(bridge), path=str(audio), expected_prefix=b"mp3-chunk"
    )

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()
```

Keep the direct generator tests as unit seams, but treat the two ASGI tests as the
disconnect proof: Starlette cancellation must reach iterator closure so partially
consumed bodies close the file and kill/reap ffmpeg without waiting for normal EOF.

- [ ] **Step 6: Run HTTP-media unit and integration slices**

Run: `uv run pytest tests/unit/test_http_media.py tests/integration/test_server.py -k "audio or lyrics" -q`

Expected: PASS; all three valid single-range forms work, unsupported/malformed forms produce correct 416 metadata, slow reads do not block the event loop, early disconnect closes the file, and ALAC never fails after headers are sent merely because ffmpeg is absent.

- [ ] **Step 7: Commit HTTP-media correction**

```bash
git add src/autodj/http_media.py src/autodj/server.py tests/unit/test_http_media.py tests/integration/test_server.py
git commit -m "fix: stream audio ranges without blocking"
```

### Task 5: Shared browser API validation and recoverable mutations

**Files:**
- Create: `src/autodj/static/modules/api-client.js`
- Create: `src/autodj/static/modules/latest-request.js`
- Create: `tests/jsmodules/api-client.test.js`
- Create: `tests/jsmodules/latest-request.test.js`
- Modify: `src/autodj/static/modules/auth.js` (created by the security plan)
- Modify: `src/autodj/static/modules/search.js`
- Modify: `src/autodj/static/modules/queue.js`
- Modify: `src/autodj/static/modules/settings-panel.js`
- Modify: `src/autodj/static/modules/library-jobs.js`
- Modify: `src/autodj/static/modules/liners.js`
- Modify: `src/autodj/static/modules/media-session.js`
- Modify: `src/autodj/static/modules/audio-engine.js`
- Modify: `src/autodj/static/app.js`
- Modify: `tests/jsmodules/auth.test.js` (created by the security plan)
- Modify: `tests/jsmodules/settings-panel.test.js`
- Create: `tests/jsmodules/search.test.js`
- Create: `tests/jsmodules/queue.test.js`
- Create: `tests/jsmodules/library-jobs.test.js`
- Create: `tests/jsmodules/media-session.test.js`
- Create: `tests/jsmodules/app-source.test.js`
- Create: `tests/jsmodules/fetch-audit.test.js`

- [ ] **Step 1: Write failing API, disabled-state, and queue-rollback tests**

Create `tests/jsmodules/api-client.test.js`:

```javascript
// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AuthenticationRequiredError,
  makeSingleFlight,
  postJsonBestEffort,
  requestBinary,
  requestJson,
  requestJsonBestEffort,
  setAuthRequiredHandler,
  withDisabled,
} from
  "../../src/autodj/static/modules/api-client.js";

afterEach(() => {
  setAuthRequiredHandler(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("requestJson", () => {
  it("rejects an HTTP error with server detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "broken" }, 500)));
    await expect(requestJson("/api/fail")).rejects.toThrow("broken");
  });

  it("rejects a successful HTML response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response("<html></html>", {
      status: 200,
      headers: { "Content-Type": "text/html" },
    })));
    await expect(requestJson("/api/html")).rejects.toThrow("non-JSON");
  });

  it("rejects an application-level failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      jsonResponse({ ok: false, detail: "No candidate" }),
    ));
    await expect(requestJson("/api/rejected")).rejects.toThrow("No candidate");
  });

  it("routes a mid-session 401 to the shared sign-in handler", async () => {
    const requireAuth = vi.fn();
    setAuthRequiredHandler(requireAuth);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      jsonResponse({ detail: "Authentication required" }, 401),
    ));

    await expect(requestJson("/api/status")).rejects.toBeInstanceOf(
      AuthenticationRequiredError,
    );
    expect(requireAuth).toHaveBeenCalledTimes(1);
  });
});

it("best-effort JSON requests report failure instead of swallowing it", async () => {
  const onError = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "offline" }, 503)));

  await expect(requestJsonBestEffort("/api/volume", {}, onError)).resolves.toBeNull();

  expect(onError).toHaveBeenCalledOnce();
  expect(onError.mock.calls[0][0].message).toContain("offline");
});

it("posts the exact absolute seek payload to the endpoint", async () => {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
  vi.stubGlobal("fetch", fetchMock);

  await postJsonBestEffort("/api/seek", { seconds: 42.5 }, vi.fn());

  expect(fetchMock).toHaveBeenCalledWith("/api/seek", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds: 42.5 }),
  });
});

it("requestBinary validates HTTP status before returning bytes", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new globalThis.Response("missing", { status: 404 }),
  ));

  await expect(requestBinary("/api/audio?path=missing.flac")).rejects.toThrow("HTTP 404");
});

it("single-flight clears its pending flag after a rejected request", async () => {
  const operation = vi.fn()
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce("retried");
  const run = makeSingleFlight(operation);

  await expect(run()).rejects.toThrow("offline");
  await expect(run()).resolves.toBe("retried");

  expect(operation).toHaveBeenCalledTimes(2);
});

it("withDisabled reenables the control when the operation rejects", async () => {
  const button = document.createElement("button");
  const operation = vi.fn().mockRejectedValue(new Error("offline"));

  await expect(withDisabled(button, operation)).rejects.toThrow("offline");

  expect(button.disabled).toBe(false);
});
```

Create `tests/jsmodules/latest-request.test.js`:

```javascript
import { expect, it } from "vitest";
import { createLatestRequestOwner } from
  "../../src/autodj/static/modules/latest-request.js";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function guardedRender(owner, pending, element) {
  const ticket = owner.begin();
  const value = await pending;
  if (ticket.isCurrent()) element.textContent = value;
  ticket.finish();
  return ticket;
}

for (const feature of ["cover art", "paginated history"]) {
  it(`${feature} aborts its predecessor and cannot render a stale response`, async () => {
    const owner = createLatestRequestOwner();
    const element = { textContent: "" };
    const first = deferred();
    const second = deferred();
    const firstLoad = guardedRender(owner, first.promise, element);
    const secondLoad = guardedRender(owner, second.promise, element);

    second.resolve("new");
    const secondTicket = await secondLoad;
    first.resolve("old");
    const firstTicket = await firstLoad;

    expect(firstTicket.signal.aborted).toBe(true);
    expect(firstTicket.isCurrent()).toBe(false);
    expect(secondTicket.isCurrent()).toBe(true);
    expect(element.textContent).toBe("new");
  });
}
```

Create `tests/jsmodules/search.test.js`:

```javascript
// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { installSearch } from "../../src/autodj/static/modules/search.js";

afterEach(() => vi.restoreAllMocks());

function response(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("installSearch", () => {
  it("reenables and refocuses a result button after a failed mutation", async () => {
    document.body.innerHTML = `
      <input id="query"><button id="search">Search</button>
      <div id="count"></div><div id="announce"></div><ul id="results"></ul>`;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({
        results: [{ path: "a.flac", artist: "Artist", title: "Track" }],
      }))
      .mockResolvedValueOnce(response({ detail: "No candidate" }, 409));
    vi.stubGlobal("fetch", fetchMock);
    const els = {
      searchInput: document.querySelector("#query"),
      btnSearch: document.querySelector("#search"),
      searchResults: document.querySelector("#results"),
      searchCount: document.querySelector("#count"),
      queueAnnounce: document.querySelector("#announce"),
    };
    installSearch(els);
    els.searchInput.value = "track";
    els.btnSearch.click();
    await vi.waitFor(() => expect(els.searchResults.querySelector(".result-btn")).not.toBeNull());
    const resultButton = els.searchResults.querySelector(".result-btn");

    resultButton.click();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("No candidate"));

    expect(resultButton.disabled).toBe(false);
    expect(document.activeElement).toBe(resultButton);
  });
});
```

Create `tests/jsmodules/queue.test.js`:

```javascript
// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { installQueueButtons, renderQueue } from
  "../../src/autodj/static/modules/queue.js";

afterEach(() => vi.restoreAllMocks());

it("rolls back a failed optimistic removal and restores focus", async () => {
  document.body.innerHTML = `
    <div id="announce"></div><span id="count"></span>
    <ol id="queue" tabindex="-1"></ol>`;
  const els = {
    queueList: document.querySelector("#queue"),
    queueCount: document.querySelector("#count"),
    queueAnnounce: document.querySelector("#announce"),
  };
  renderQueue([
    { path: "a.flac", artist: "A", title: "One" },
    { path: "b.flac", artist: "B", title: "Two" },
  ], els);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
    JSON.stringify({ detail: "queue locked" }),
    { status: 409, headers: { "Content-Type": "application/json" } },
  )));
  installQueueButtons(els);
  const remove = els.queueList.querySelector(
    'li[data-path="a.flac"] [data-action="remove"]',
  );

  remove.click();
  await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("Could not remove"));

  expect(Array.from(els.queueList.querySelectorAll("li[data-path]")).map(
    (item) => item.dataset.path,
  )).toEqual(["a.flac", "b.flac"]);
  expect(document.activeElement).toBe(els.queueList.querySelector(
    'li[data-path="a.flac"] [data-action="remove"]',
  ));
});
```

Create `tests/jsmodules/app-source.test.js`:

```javascript
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync("src/autodj/static/app.js", "utf8");

describe("transport request ownership", () => {
  it("uses finally-owned disabled lifecycles for transport requests", () => {
    expect(source).toContain("withDisabled(btnPause");
    expect(source).toContain("withDisabled(btnSkip");
    expect(source).toContain("withDisabled(btnShuffle");
    expect(source).not.toMatch(/setTimeout\(\(\) => \{ btn(Skip|Shuffle)\.disabled = false/);
  });

  it("registers the security-plan auth dialog for later 401 responses", () => {
    expect(source).toContain("setAuthRequiredHandler(() => auth.show())");
  });

  it("sends the absolute seek value under the server-owned seconds key", () => {
    expect(source).toContain(
      'postJsonBestEffort("/api/seek", { seconds }, reportSeekFailure)',
    );
    expect(source).not.toContain("seekSeconds");
    expect(source).not.toContain("position: seconds");
  });
});
```

Append to the security plan's `tests/jsmodules/auth.test.js`:

```javascript
it("show is idempotent when several requests lose authentication", () => {
  document.body.innerHTML = `<dialog id="auth-dialog"><form id="auth-form">
    <input id="auth-token"><p id="auth-error"></p><button>Log in</button>
  </form></dialog>`;
  const dialog = document.querySelector("#auth-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  const auth = initAuthDialog({ document });

  auth.show();
  auth.show();

  expect(dialog.showModal).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(document.querySelector("#auth-token"));
});
```

Create `tests/jsmodules/library-jobs.test.js`:

```javascript
// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { installLibraryJobs } from
  "../../src/autodj/static/modules/library-jobs.js";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

it("reenables the run control and announces a failed library mutation", async () => {
  document.body.innerHTML = `
    <button id="run-library">Run library scan</button>
    <div id="library-status" role="status"></div>`;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    jsonResponse({ detail: "scanner unavailable" }, 503),
  ));
  const run = document.querySelector("#run-library");
  installLibraryJobs({
    runIndex: run,
    jobStatus: document.querySelector("#library-status"),
  });

  run.click();
  await vi.waitFor(() => expect(document.querySelector("#library-status").textContent)
    .toContain("scanner unavailable"));

  expect(run.disabled).toBe(false);
});
```

Create `tests/jsmodules/media-session.test.js`:

```javascript
// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { installMediaActionHandlers } from
  "../../src/autodj/static/modules/media-session.js";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("reports a failed fallback media action", async () => {
  const handlers = {};
  Object.defineProperty(navigator, "mediaSession", {
    configurable: true,
    value: { setActionHandler: (name, handler) => { handlers[name] = handler; } },
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
    JSON.stringify({ detail: "transport offline" }),
    { status: 503, headers: { "Content-Type": "application/json" } },
  )));
  const onRequestError = vi.fn();
  installMediaActionHandlers({ onRequestError });

  handlers.pause();
  await vi.waitFor(() => expect(onRequestError).toHaveBeenCalledOnce());

  expect(onRequestError.mock.calls[0][0]).toBe("Pause");
  expect(onRequestError.mock.calls[0][1].message).toContain("transport offline");
});
```

Add `afterEach`, `vi`, and `postSettings` to the existing imports in
`tests/jsmodules/settings-panel.test.js`, reset globals/mocks after each case, and append:

```javascript
it("reenables the triggering control and announces a rejected save", async () => {
  const save = document.createElement("button");
  const status = document.createElement("div");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
    JSON.stringify({ detail: "settings locked" }),
    { status: 503, headers: { "Content-Type": "application/json" } },
  )));

  await expect(postSettings("/api/settings", { bpm_min: 90 }, {
    settingsStatus: status,
    control: save,
  })).resolves.toBe(false);

  expect(save.disabled).toBe(false);
  expect(status.textContent).toContain("settings locked");
});
```

This is the behavior-level check for the shared request contract in an existing feature
module.

Create `tests/jsmodules/fetch-audit.test.js`:

```javascript
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const moduleDir = "src/autodj/static/modules";
const audited = ["src/autodj/static/app.js", ...readdirSync(moduleDir)
  .filter((name) => name.endsWith(".js"))
  .map((name) => join(moduleDir, name))];

function executableSource(path) {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
}

describe("browser request ownership", () => {
  it("makes api-client the only global raw-fetch owner", () => {
    const owners = audited.filter((path) => /\bfetch\s*\(/.test(executableSource(path)));
    expect(owners).toEqual([join(moduleDir, "api-client.js")]);
    expect(executableSource(join(moduleDir, "api-client.js")).match(/\bfetch\s*\(/g))
      .toHaveLength(1);
  });

  it("permits auth's injected bootstrap transport without global fetch calls", () => {
    const auth = executableSource(join(moduleDir, "auth.js"));
    expect(auth).toMatch(/\bfetchImpl\s*\(/);
    expect(auth).not.toMatch(/\bfetch\s*\(/);
  });

  it("keeps streaming binary requests on status-checking helpers", () => {
    const audio = executableSource("src/autodj/static/modules/audio-engine.js");
    const liners = executableSource("src/autodj/static/modules/liners.js");
    expect(audio).toContain("requestBinary(url)");
    expect(audio).toContain("probeResource(url)");
    expect(liners).toContain("requestBinary(");
  });

  it("uses retryable single-flight ownership for advance and repick", () => {
    const audio = executableSource("src/autodj/static/modules/audio-engine.js");
    expect(audio).toContain("const requestAdvance = makeSingleFlight(");
    expect(audio).toContain("const requestRepick = makeSingleFlight(");
    expect(audio).toContain("void requestAdvance()");
    expect(audio).toContain("void requestRepick(path)");
  });

  it("uses abortable latest-request ownership for cover art and history", () => {
    const app = executableSource("src/autodj/static/app.js");
    const audio = executableSource("src/autodj/static/modules/audio-engine.js");
    expect(app).toContain("const historyRequests = createLatestRequestOwner()");
    expect(app).toContain("signal: ticket.signal");
    expect(audio).toContain("const coverArtRequests = createLatestRequestOwner()");
    expect(audio).toContain("probeResource(url, { signal: ticket.signal })");
  });
});
```

The audit includes both transport modules: it proves `api-client.js` owns the sole global
raw `fetch`, while security-plan `auth.js` uses only its injected `fetchImpl` for the
unauthenticated `/api/login` and `/api/auth/status` bootstrap exceptions. Assigning an
`<audio>.src` or `<img>.src` is also a safe streaming/browser-resource exception; when
code needs to inspect its response, it must use `requestBinary` or `probeResource` so
HTTP and `401` handling still applies.

- [ ] **Step 2: Run the browser mutation slice and verify the red state**

Run: `npm test -- --run tests/jsmodules/api-client.test.js tests/jsmodules/latest-request.test.js tests/jsmodules/auth.test.js tests/jsmodules/search.test.js tests/jsmodules/queue.test.js tests/jsmodules/library-jobs.test.js tests/jsmodules/media-session.test.js tests/jsmodules/settings-panel.test.js tests/jsmodules/app-source.test.js tests/jsmodules/fetch-audit.test.js`

Expected: FAIL because responses are decoded without status/content-type/application checks,
queue failure has no rollback, mid-session `401` is not centralized, controls can remain
disabled, and the audited request owners still contain direct unchecked `fetch` calls.

- [ ] **Step 3: Create the shared JSON request helper**

Create `src/autodj/static/modules/api-client.js`:

```javascript
export class ApiError extends Error {}
export class AuthenticationRequiredError extends ApiError {}

let authRequiredHandler = null;

export function setAuthRequiredHandler(handler) {
  authRequiredHandler = typeof handler === "function" ? handler : null;
}

async function checkedResponse(url, options) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    authRequiredHandler?.();
    throw new AuthenticationRequiredError("Authentication required.");
  }
  return response;
}

export async function requestJson(url, options = {}) {
  const response = await checkedResponse(url, options);
  const type = response.headers.get("content-type") || "";
  if (!type.toLowerCase().includes("application/json")) {
    throw new ApiError(`Server returned ${response.status} with non-JSON content.`);
  }
  let body;
  try {
    body = await response.json();
  } catch (_) {
    throw new ApiError("Server returned malformed JSON.");
  }
  const detail = body && typeof body === "object"
    ? body.detail || body.error
    : null;
  if (!response.ok) {
    throw new ApiError(detail || `Request failed with HTTP ${response.status}.`);
  }
  if (body && (body.ok === false || body.success === false)) {
    throw new ApiError(detail || "The server rejected the request.");
  }
  return body;
}

export async function requestJsonBestEffort(url, options = {}, onError) {
  if (typeof onError !== "function") {
    throw new TypeError("Best-effort requests require an error reporter.");
  }
  try {
    return await requestJson(url, options);
  } catch (error) {
    onError(error);
    return null;
  }
}

export function postJsonBestEffort(url, body, onError) {
  return requestJsonBestEffort(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, onError);
}

export function makeSingleFlight(operation) {
  let pending = false;
  return async (...args) => {
    if (pending) return null;
    pending = true;
    try {
      return await operation(...args);
    } finally {
      pending = false;
    }
  };
}

export async function requestBinary(url, options = {}) {
  const response = await checkedResponse(url, options);
  if (!response.ok) {
    throw new ApiError(`Request failed with HTTP ${response.status}.`);
  }
  return response.arrayBuffer();
}

export async function probeResource(url, options = {}) {
  const response = await checkedResponse(url, options);
  if (response.status === 404) return false;
  if (!response.ok) {
    throw new ApiError(`Request failed with HTTP ${response.status}.`);
  }
  return true;
}

export async function withDisabled(control, operation) {
  control.disabled = true;
  try {
    return await operation();
  } finally {
    control.disabled = false;
  }
}
```

Create `src/autodj/static/modules/latest-request.js`:

```javascript
export function createLatestRequestOwner() {
  let generation = 0;
  let activeController = null;

  return {
    begin() {
      const ownedGeneration = ++generation;
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      return {
        signal: controller.signal,
        isCurrent: () => ownedGeneration === generation,
        finish() {
          if (ownedGeneration === generation) activeController = null;
        },
      };
    },
    cancel() {
      generation += 1;
      activeController?.abort();
      activeController = null;
    },
  };
}
```

`requestJsonBestEffort` is reserved for high-frequency or advisory requests where the
caller cannot block playback (seek/volume synchronization, EQ persistence, advance, and
version display). Its mandatory `onError` callback prevents silent `.catch(() => {})`
paths. `requestBinary` and `probeResource` are the explicit safe helpers for audio bytes
and cover-art existence probes.

- [ ] **Step 4: Connect authentication and route search/queue mutations through the client**

In the security plan's `auth.js`, make `show` public and idempotent, then return it with
`submit`:

```javascript
  function show() {
    if (!dialog.open) dialog.showModal();
    token.focus();
  }

  return { show, submit };
```

Keep the dependency-injected raw fetch only for unauthenticated `/api/login` and
`/api/auth/status`. Import `AuthenticationRequiredError` and `requestJson` in `auth.js`, add
`requestState = requestJson` to `bootstrapAuthenticatedApp`'s options, and replace its
protected `/api/status` response block with:

```javascript
    const initialState = await requestState("/api/status");
    startAuthenticatedApp(initialState);
    return true;
```

At the top of its catch, add:

```javascript
    if (errorValue instanceof AuthenticationRequiredError) return false;
```

Update the existing authenticated-bootstrap test to inject
`requestState: vi.fn().mockResolvedValue({ paused: false })`; assert `fetchImpl` was
called only with `/api/auth/status`, and `requestState` only with `/api/status`. The
shared request helper owns a protected-state `401`.

In `app.js`, import and register the shared handler immediately after the security plan
initializes the dialog and before calling `bootstrapAuthenticatedApp`:

```javascript
import {
  postJsonBestEffort,
  requestJson,
  requestJsonBestEffort,
  setAuthRequiredHandler,
  withDisabled,
} from "./modules/api-client.js";
import { createLatestRequestOwner } from "./modules/latest-request.js";

const auth = initAuthDialog({ document });
setAuthRequiredHandler(() => auth.show());
```

When protected bootstrap rejects with `AuthenticationRequiredError`, the shared handler
has already opened the dialog; `bootstrapAuthenticatedApp` returns `false` through its
existing catch. Announce other failures in the existing status region. Every later
`401` from JSON, binary, or probe helpers follows the identical dialog path, including
requests made after a session expires.

Add this import to `search.js`:

```javascript
import { requestJson, withDisabled } from "./api-client.js";
```

Add `let searchGeneration = 0;` inside `installSearch`, then replace `doSearch` with:

```javascript
  async function doSearch() {
    const query = searchInput.value.trim();
    const generation = ++searchGeneration;
    if (!query) {
      searchResults.innerHTML = "";
      searchInput.setAttribute("aria-expanded", "false");
      if (searchCount) searchCount.textContent = "";
      return;
    }
    const operation = async () => {
      try {
        const data = await requestJson(`/api/search?q=${encodeURIComponent(query)}`);
        if (generation !== searchGeneration) return;
        const results = Array.isArray(data.results) ? data.results : [];
        if (results.length === 0) {
          searchResults.innerHTML =
            `<li><span class="no-results">No results for "${escHtml(query)}".</span></li>`;
          searchInput.setAttribute("aria-expanded", "true");
          if (searchCount) searchCount.textContent = "No results found.";
          return;
        }
        searchResults.innerHTML = results.map((track) => {
          const name = escHtml(fmtTrack(track));
          const path = escHtml(track.path);
          return `<li><span class="result-name" title="${name}">${name}</span>
            <button class="result-btn" aria-label="Play ${name} now"
                    data-path="${path}" data-now="true">Now</button>
            <button class="result-btn" aria-label="Queue ${name} as next track"
                    data-path="${path}" data-now="false">Next</button></li>`;
        }).join("");
        searchInput.setAttribute("aria-expanded", "true");
        if (searchCount) searchCount.textContent =
          `${results.length} result${results.length === 1 ? "" : "s"} found.`;
      } catch (error) {
        if (generation !== searchGeneration) return;
        searchResults.innerHTML = "";
        searchInput.setAttribute("aria-expanded", "false");
        if (searchCount) searchCount.textContent = `Search failed: ${error.message}`;
      } finally {
        if (searchCount && searchCount.textContent) clearLiveRegionLater(searchCount);
      }
    };
    if (btnSearch) await withDisabled(btnSearch, operation);
    else await operation();
  }
```

Replace the result-button request portion with:

```javascript
    try {
      await withDisabled(btn, async () => {
        await requestJson(now ? "/api/play-next" : "/api/queue/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(now ? { path, now: true } : { path }),
        });
      });
      if (queueAnnounce) {
        queueAnnounce.textContent = now
          ? `Playing ${name} now.`
          : `Added ${name} to queue.`;
        clearLiveRegionLater(queueAnnounce);
      }
    } catch (error) {
      if (queueAnnounce) {
        queueAnnounce.textContent = `Request failed for ${name}: ${error.message}`;
        clearLiveRegionLater(queueAnnounce);
      }
    } finally {
      btn.disabled = false;
      btn.focus();
    }
```

Replace the complete Pause and Skip listeners with:

```javascript
btnPause.addEventListener("click", async () => {
  if (!playbackEnabled && _lastBrowserPlayback) {
    try {
      await unlockAndPlay();
    } catch (_) {
      // unlockAndPlay owns its audio-context error announcement.
    }
    return;
  }
  try {
    await withDisabled(btnPause, async () => {
      const data = await requestJson("/api/pause", { method: "POST" });
      const isPaused = data.paused;
      btnPause.innerHTML = isPaused
        ? '<span aria-hidden="true">▶</span> Resume'
        : '<span aria-hidden="true">⏸</span> Pause';
      btnPause.setAttribute("aria-pressed", isPaused ? "false" : "true");
    });
  } catch (error) {
    if (settingsStatus) settingsStatus.textContent = `Pause failed: ${error.message}`;
  }
});

btnSkip.addEventListener("click", async () => {
  try {
    await withDisabled(btnSkip, async () => {
      if (
        _lastBrowserPlayback && playbackEnabled && _ctx &&
        _nextTrackPathCache && !crossfading
      ) {
        if (_beatmatchOnSkip && _outBpmCache > 0 && _inBpmCache > 0) {
          const ratio = _outBpmCache / _inBpmCache;
          const clamped = Math.max(0.85, Math.min(1.15, ratio));
          const audio = decks[activeIdx ^ 1].audio;
          const previousPitch = audio.preservesPitch;
          const previousRate = audio.playbackRate;
          try { audio.preservesPitch = true; } catch (_) {}
          try { audio.playbackRate = clamped; } catch (_) {}
          setTimeout(() => {
            try { audio.playbackRate = previousRate; } catch (_) {}
            try { audio.preservesPitch = previousPitch; } catch (_) {}
          }, _crossfadeSecondsCache * 1000 + 200);
        }
        startCrossfade(_nextTrackPathCache, _crossfadeSecondsCache);
      } else {
        await requestJson("/api/skip", { method: "POST" });
      }
    });
  } catch (error) {
    if (settingsStatus) settingsStatus.textContent = `Skip failed: ${error.message}`;
  }
});
```

Replace the Shuffle handler with:

```javascript
  btnShuffle.addEventListener("click", async () => {
    try {
      await withDisabled(btnShuffle, () => requestJson(
        "/api/random-track", { method: "POST" },
      ));
    } catch (error) {
      if (settingsStatus) settingsStatus.textContent = `Shuffle failed: ${error.message}`;
    }
  });
```

Delete the old 800 ms Skip/Shuffle `setTimeout` re-enable paths. Replace the Discovery listener with this WebSocket-specific error handling; it does not use `withDisabled` because it has no asynchronous response lifecycle:

```javascript
btnDiscovery.addEventListener("click", () => {
  try {
    if (!_ws || _ws.readyState !== WebSocket.OPEN) {
      throw new Error("not connected");
    }
    _ws.send(JSON.stringify({ type: "toggle_discovery" }));
  } catch (error) {
    if (settingsStatus) {
      settingsStatus.textContent = `Discovery toggle failed: ${error.message}`;
    }
  }
});
```

- [ ] **Step 5: Migrate every remaining user-facing request owner**

Use the following exhaustive endpoint ownership list. Do not leave a raw `fetch` in any
audited file, and do not convert a failure into an empty catch:

- `app.js`: use `requestJson` for `/api/pause`, `/api/skip`, `/api/random-track`,
  `/api/mute`, `/api/history`, `/api/status`, and `/api/version`. Use
  `requestJsonBestEffort` for high-frequency `/api/seek` and `/api/volume`. Wrap the
  pause, skip, shuffle, and mute controls in `withDisabled`; their `finally` paths own
  re-enablement. The history/status/version paths must render their existing failure or
  unavailable text instead of silently retaining stale content.
- `settings-panel.js`: implement `postSettings` with `requestJson`; accept the triggering
  control in its options and use `withDisabled` when present. Keep the existing
  `Could not save: ...` live-region announcement. Return `true` on success and `false`
  after an announced failure so callers do not assume persistence succeeded.
- `library-jobs.js`: use `requestJson` for stop, run, and stats. Pass each clicked run,
  stop, or refresh button to `withDisabled`. Put all failures in `jobStatus`, including
  stop and stats refresh; never retain the current silent catches.
- `liners.js`: use `requestJson` for inventory, delete, and upload, and `requestBinary`
  for liner playback bytes. Extend delete/upload callbacks to receive their triggering
  controls, wrap mutations in `withDisabled`, restore the controls, and report all
  failures through the module's status live region.
- `media-session.js`: import `requestJsonBestEffort`, accept an `onRequestError` callback
  in `installMediaActionHandlers`, and use it for fallback pause/skip actions. The app
  passes the same visible announcement callback used by transport controls.
- `audio-engine.js`: use `requestJsonBestEffort` for EQ persistence and every
  `/api/advance` call; use `requestJson` for `/api/repick-next` and the unlock-time
  `/api/status`; use `requestBinary` in `_decodeFor`; and use `probeResource` for cover
  art. Advance and repick errors must be announced and must clear/reset any relevant
  pending flag so the next transition can retry. A `404` art probe hides the image;
  other probe failures hide it and announce the failure.

Use this exact `postSettings` ownership shape after importing `requestJson` and
`withDisabled`:

```javascript
export async function postSettings(
  url,
  body,
  { settingsStatus, control } = {},
) {
  const operation = () => requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  try {
    if (control) await withDisabled(control, operation);
    else await operation();
    return true;
  } catch (error) {
    if (settingsStatus) {
      settingsStatus.textContent = `Could not save: ${error.message}`;
    }
    return false;
  }
}
```

In `app.js`, replace its wrapper with:

```javascript
function postSettings(url, body, control) {
  return _postSettingsModule(url, body, { settingsStatus, control });
}
```

For every settings `change` listener, accept `event` and pass
`event.currentTarget` as the third argument. For example, replace the preset listener
with this exact shape and apply the identical third-argument change to transition,
DJ-mix, playback, BPM-range, and discovery listeners:

```javascript
presetSelect.addEventListener("change", (event) => {
  void postSettings(
    "/api/preset",
    { name: presetSelect.value || null },
    event.currentTarget,
  );
});
```

The affected controls are `presetSelect`, `transitionSelect`, every entry in
`_djToggleMap`, `harmonicMode`, `pbEqDuck`, `pbPickMode`, `pbShowLyrics`,
`pbAnchorSeed`, `pbDaypart`, `pbMoodArc`, `pbMoodArcHours`, `pbImportCues`,
`pbBeatSyncFx`, `pbKeySyncFx`, `pbBeatmatchSkip`, `pbReplayGain`,
`pbTransitionMode`, `pbPostQueueSeed`, `keyNotation`, `keyPreferFlats`,
`pbCrossfade`, `pbFadeIn`, `bpmLo`, `bpmHi`, `discEnabled`, and `discEvery`.
Prefix fire-and-forget calls with `void`; the module owns failure reporting and control
recovery.

In `library-jobs.js`, import `requestJson` and `withDisabled`, then replace
`installLibraryJobs`, `_run`, and `refreshLibStats` with:

```javascript
export function installLibraryJobs(els) {
  const {
    runIndex, runEnrich, runPrune, runStats, runStop,
    indexLimit, statsRefresh, statCount,
  } = els;
  const start = (control, name, args = []) => void _run(els, control, name, args);

  runIndex?.addEventListener("click", () => {
    const limit = parseInt(indexLimit?.value, 10);
    const args = !Number.isNaN(limit) && limit > 0 ? ["--limit", String(limit)] : [];
    start(runIndex, "index", args);
  });
  runEnrich?.addEventListener("click", () => start(runEnrich, "enrich"));
  runPrune?.addEventListener("click", () => start(runPrune, "prune"));
  runStats?.addEventListener("click", () => start(runStats, "stats"));
  runStop?.addEventListener("click", () => void withDisabled(runStop, async () => {
    try {
      await requestJson("/api/library/stop", { method: "POST" });
      if (els.jobStatus) els.jobStatus.textContent = "Library job stopped.";
    } catch (error) {
      if (els.jobStatus) els.jobStatus.textContent = `Could not stop job: ${error.message}`;
    }
  }));
  statsRefresh?.addEventListener("click", () => void refreshLibStats(els, statsRefresh));
  if (statCount) void refreshLibStats(els);
}

async function _run(els, control, name, args = []) {
  try {
    await withDisabled(control, () => requestJson("/api/library/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, args }),
    }));
    if (els.jobStatus) els.jobStatus.textContent = `${name} started…`;
  } catch (error) {
    if (els.jobStatus) {
      els.jobStatus.textContent = `Could not start ${name}: ${error.message}`;
    }
  }
}

export async function refreshLibStats(els, control = null) {
  const { statCount, statAvgBpm, statWithKey, statWithGenre, statWithEnergy } = els;
  if (!statCount) return;
  const load = () => requestJson("/api/library/stats");
  try {
    const stats = control ? await withDisabled(control, load) : await load();
    statCount.textContent = stats.track_count;
    statAvgBpm.textContent = stats.average_bpm
      ? `${stats.average_bpm} (${stats.tracks_with_bpm} tracks)` : "—";
    statWithKey.textContent = stats.tracks_with_key;
    statWithGenre.textContent = stats.tracks_with_genre;
    statWithEnergy.textContent = stats.tracks_with_energy;
  } catch (error) {
    if (els.jobStatus) {
      els.jobStatus.textContent = `Could not refresh library stats: ${error.message}`;
    }
  }
}
```

In `app.js`, define one visible reporter and reuse it for seek, volume, media-session,
and audio-engine best-effort callbacks. Throttle repeated high-frequency text updates,
but never suppress the first failure:

```javascript
let lastBackgroundRequestErrorAt = 0;
function reportBackgroundRequestError(action, error) {
  const now = Date.now();
  if (now - lastBackgroundRequestErrorAt < 4000) return;
  lastBackgroundRequestErrorAt = now;
  if (settingsStatus) {
    settingsStatus.textContent = `${action} failed: ${error.message}`;
  }
}

const reportSeekFailure = (error) => reportBackgroundRequestError("Seek", error);
const reportVolumeFailure = (error) => reportBackgroundRequestError("Volume", error);
```

Replace `_seekRelative`'s request with:

```javascript
void postJsonBestEffort("/api/seek", { delta: deltaSec }, reportSeekFailure);
```

Replace `_seekToFrac`'s request after its existing `const seconds = f * dur` computation
with:

```javascript
void postJsonBestEffort("/api/seek", { seconds }, reportSeekFailure);
```

Replace the debounced volume request with:

```javascript
void postJsonBestEffort(
  "/api/volume",
  { volume: _sliderToGain(val) },
  reportVolumeFailure,
);
```

Replace the complete mute listener with:

```javascript
btnMute.addEventListener("click", async () => {
  try {
    await withDisabled(btnMute, async () => {
      const data = await requestJson("/api/mute", { method: "POST" });
      const muted = data.muted;
      btnMute.setAttribute("aria-pressed", muted ? "true" : "false");
      btnMute.innerHTML = muted
        ? '<span aria-hidden="true">🔇</span> Unmute'
        : '<span aria-hidden="true">🔊</span> Mute';
    });
  } catch (error) {
    reportBackgroundRequestError("Mute", error);
  }
});
```

Create `const historyRequests = createLatestRequestOwner();` beside `_histPage`. At the
top of `fetchHistory`, replace `_histPage = page` and the raw response/decode lines with:

```javascript
async function fetchHistory(page) {
  _histPage = page;
  const ticket = historyRequests.begin();
  try {
    const data = await requestJson(`/api/history?page=${page}&per_page=50`, {
      signal: ticket.signal,
    });
    if (!ticket.isCurrent()) return;
```

Keep the existing render body inside that `try`. Replace its catch/finally tail with:

```javascript
  } catch (error) {
    if (error.name === "AbortError" || !ticket.isCurrent()) return;
    const empty = document.getElementById("history-empty");
    if (empty) empty.textContent = `Could not load history: ${error.message}`;
  } finally {
    ticket.finish();
  }
}
```

This makes page selection last-request-wins; an older page can neither overwrite rows
nor replace the current page's error state. Replace the footer request chain with:

```javascript
requestJson("/api/version", { cache: "no-cache" })
  .then((data) => {
    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el && value) el.textContent = value;
    };
    setText("ver-version", data.version);
    setText("ver-commit", data.commit);
    const built = document.getElementById("ver-built");
    if (built && data.built_at) {
      built.setAttribute("datetime", data.built_at);
      built.textContent = data.built_at;
    }
  })
  .catch((error) => {
    const version = document.getElementById("ver-version");
    if (version) version.textContent = "Version unavailable";
    reportBackgroundRequestError("Version", error);
  });
```

Replace the media-session caller with:

```javascript
installMediaActionHandlers({
  onPlay: () => {
    if (!playbackEnabled && _lastBrowserPlayback) {
      void unlockAndPlay().catch((error) =>
        reportBackgroundRequestError("Play", error));
      return true;
    }
    return false;
  },
  onPauseOrSkipNext: null,
  onRequestError: (action, error) => reportBackgroundRequestError(action, error),
});
```

Returning `false` delegates ordinary Play/Pause to the module's checked `/api/pause`
fallback; returning `true` means the caller handled browser unlock. Do not issue another
request from `app.js`.

In `media-session.js`, add the API-client import and replace
`installMediaActionHandlers` with:

```javascript
import { requestJsonBestEffort } from "./api-client.js";

export function installMediaActionHandlers({
  onPlay,
  onPauseOrSkipNext,
  onRequestError = () => {},
} = {}) {
  if (!("mediaSession" in navigator)) return;
  const fallback = (action, url) => void requestJsonBestEffort(
    url,
    { method: "POST" },
    (error) => onRequestError(action, error),
  );
  navigator.mediaSession.setActionHandler("play", () => {
    if (typeof onPlay === "function" && onPlay() === true) return;
    fallback("Play", "/api/pause");
  });
  navigator.mediaSession.setActionHandler("pause", () => {
    fallback("Pause", "/api/pause");
  });
  navigator.mediaSession.setActionHandler("nexttrack", () => {
    if (typeof onPauseOrSkipNext === "function" && onPauseOrSkipNext() === true) return;
    fallback("Skip", "/api/skip");
  });
  navigator.mediaSession.setActionHandler("previoustrack", null);
}
```

In `liners.js`, import `requestBinary`, `requestJson`, and `withDisabled`. Make these
exact replacements:

At the top of `_refreshLibrary`'s `try`, replace the raw fetch/status/decode statements
with this single statement, leaving the render body immediately after it:

```javascript
    const body = await requestJson("/api/liners");
```

Replace its delete-button listener and catch body exactly:

```javascript
        btn.addEventListener("click", () => void _deleteLiner(els, name, btn));
```

```javascript
  } catch (error) {
    _setStatus(els, `Could not refresh liners: ${error.message}`);
  }
```

Replace `_deleteLiner` itself with:

```javascript
async function _deleteLiner(els, name, control) {
  if (!confirm(`Delete liner "${name}"?`)) return;
  try {
    await withDisabled(control, () => requestJson(
      `/api/liners/file/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ));
    _setStatus(els, `Deleted ${name}`);
    await _refreshLibrary(els);
  } catch (error) {
    _setStatus(els, `Delete failed: ${error.message}`);
  } finally {
    if (control.isConnected) control.focus();
  }
}
```

Replace `_playByName` with:

```javascript
async function _playByName(els, deps, name) {
  try {
    const bytes = await requestBinary(
      `/api/liners/file/${encodeURIComponent(name)}`,
    );
    const duckDb = (state.lib.config && state.lib.config.duck_db) || -12;
    const played = await deps.playLiner(bytes, duckDb);
    if (!played) {
      _setStatus(els, "Liner playback skipped (audio context not ready).");
      return;
    }
    state.lastFireAt = _now();
    state.trackCount = 0;
    state.randomTarget = _rollRandomTarget();
    _setStatus(els, `Liner playing: ${name}`);
  } catch (error) {
    _setStatus(els, `Liner playback failed: ${error.message}`);
  }
}
```

Replace the upload listener body with:

```javascript
  if (els.lnUploadSubmit) {
    els.lnUploadSubmit.addEventListener("click", () => void withDisabled(
      els.lnUploadSubmit,
      async () => {
        if (!els.lnUpload?.files?.length) {
          _setStatus(els, "Pick a file first.");
          return;
        }
        const file = els.lnUpload.files[0];
        const form = new FormData();
        form.append("file", file, file.name);
        _setStatus(els, `Uploading ${file.name}...`);
        try {
          await requestJson("/api/liners/upload", { method: "POST", body: form });
          _setStatus(els, `Uploaded ${file.name}`);
          els.lnUpload.value = "";
          await _refreshLibrary(els);
        } catch (error) {
          _setStatus(els, `Upload failed: ${error.message}`);
        }
      },
    ));
  }
```

Change each liner config listener to pass its control, and wrap the Test button:

```javascript
    el.addEventListener("change", () => _postConfig(els, deps.postSettings, el));

  els.lnTestBtn?.addEventListener("click", () => void withDisabled(
    els.lnTestBtn,
    async () => {
      const name = _pickLiner();
      if (!name) {
        _setStatus(els, "No liner files in folder.");
        return;
      }
      await _playByName(els, deps, name);
    },
  ));
```

Change `_postConfig`'s signature to `_postConfig(els, postSettings, control)` and its
call from `postSettings(url, body)` to `postSettings(url, body, control)`. In the existing
app `installLiners` dependency object, replace only the `postSettings` property with:

```javascript
  postSettings: (url, body, control) => postSettings(url, body, control),
```

The app wrapper already adds `{ settingsStatus, control }` when it calls
`_postSettingsModule`; passing an options object here would double-wrap the control and
make `withDisabled` receive a plain object instead of the DOM element.

In `audio-engine.js`, add this import and request ownership beside `_applyState`:

```javascript
import {
  makeSingleFlight,
  requestBinary,
  requestJson,
  requestJsonBestEffort,
  probeResource,
} from "./api-client.js";
import { createLatestRequestOwner } from "./latest-request.js";

function announceRequestError(action, error) {
  if (npAnnounce) npAnnounce.textContent = `${action} failed: ${error.message}`;
}

const requestAdvance = makeSingleFlight(() => requestJsonBestEffort(
  "/api/advance",
  { method: "POST" },
  (error) => announceRequestError("Advance", error),
));

const requestRepick = makeSingleFlight(async (path) => {
  try {
    const state = await requestJson("/api/repick-next", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: path ? JSON.stringify({ blacklist: path }) : "{}",
    });
    if (_applyState) _applyState(state);
  } catch (error) {
    announceRequestError("Pick next track", error);
  } finally {
    _nextTrackPathCache = null;
  }
});
```

`makeSingleFlight` owns the pending flag in `finally`; the test above proves a rejected
operation can be retried. Replace all three `/api/advance` fetch statements (crossfade,
active-deck ended, and active-deck error) with:

```javascript
void requestAdvance();
```

Replace the complete standby `/api/repick-next` promise chain and its adjacent cache
clear with:

```javascript
      void requestRepick(path);
```

Keep the following standby-deck source teardown. Replace `postEq`'s debounced request
with:

```javascript
    void requestJsonBestEffort("/api/eq", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        low: parseInt(eqLow.value, 10) / 100,
        mid: parseInt(eqMid.value, 10) / 100,
        high: parseInt(eqHigh.value, 10) / 100,
      }),
    }, (error) => announceRequestError("EQ save", error));
```

Replace `_decodeFor` with:

```javascript
async function _decodeFor(path) {
  if (_bufferCache.has(path)) return _bufferCache.get(path);
  const url = "/api/audio?path=" + encodeURIComponent(path);
  const bytes = await requestBinary(url);
  const buffer = await _ctx.decodeAudioData(bytes);
  if (_bufferCache.size >= 4) {
    const oldest = _bufferCache.keys().next().value;
    _bufferCache.delete(oldest);
  }
  _bufferCache.set(path, buffer);
  return buffer;
}
```

In `unlockAndPlay`, replace the raw status request/decode inside its existing try with:

```javascript
    state = await requestJson("/api/status");
```

Keep its existing catch/announcement and rethrow. Add this helper and replace
`loadCoverArt` completely:

```javascript
function hideCoverArt() {
  coverArt.hidden = true;
  coverArt.removeAttribute("src");
}

const coverArtRequests = createLatestRequestOwner();

export function clearCoverArt() {
  coverArtRequests.cancel();
  hideCoverArt();
}

export async function loadCoverArt(trackPath) {
  const ticket = coverArtRequests.begin();
  const url = `/api/art?path=${encodeURIComponent(trackPath)}`;
  hideCoverArt();
  try {
    const exists = await probeResource(url, { signal: ticket.signal });
    if (!ticket.isCurrent()) return;
    if (!exists) {
      hideCoverArt();
      return;
    }
    coverArt.src = url;
    coverArt.hidden = false;
  } catch (error) {
    if (error.name === "AbortError" || !ticket.isCurrent()) return;
    hideCoverArt();
    announceRequestError("Cover art", error);
  } finally {
    ticket.finish();
  }
}
```

Because `loadCoverArt` now returns a promise, import `clearCoverArt` beside it and change
the track-change branch to:

```javascript
if (trackKey) {
  void loadCoverArt(trackKey);
  loadLyrics(_lyricEls);
} else {
  clearCoverArt();
}
```

Do not await the art probe from the WebSocket state render path. The explicit clear
cancels an in-flight art probe when playback becomes empty. Task 7 later upgrades the
lyrics call and empty-state clearing with its path-owned request contract.

`requestJsonBestEffort` still invokes the shared `401` handler before the visible
reporter. Therefore an expired session both opens the sign-in dialog and announces the
failed action consistently, even during playback-owned background requests.

The media element's direct `audio.src = "/api/audio?..."` assignment remains valid:
the browser owns that streaming response and emits its existing media error event. It
is not an unchecked JavaScript fetch and must not be replaced with full-buffer loading.

- [ ] **Step 6: Make queue optimism transactional**

Add `import { requestJson } from "./api-client.js";`. Immediately after `items` is computed, capture this immutable snapshot:

```javascript
    const oldQueue = items.map((item) => ({
      path: item.dataset.path,
      display_name: item.querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, ""),
    }));
```

Replace the request/announce/focus tail of the click listener with:

```javascript
    try {
      btn.disabled = true;
      if (action === "remove") {
        await requestJson("/api/queue/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
      } else {
        await requestJson("/api/queue/reorder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paths: newPaths }),
        });
      }
      if (queueAnnounce) {
        queueAnnounce.textContent = announceMsg;
        clearLiveRegionLater(queueAnnounce);
      }
      if (focusPath) {
        const target = queueList.querySelector(
          `li[data-path="${CSS.escape(focusPath)}"] .queue-btn[data-action="${focusAction}"]`,
        );
        if (target && !target.disabled) target.focus();
      } else {
        queueList.focus();
      }
    } catch (error) {
      renderQueue(oldQueue, els);
      _lastKey = _queueKey(oldQueue);
      if (queueAnnounce) {
        queueAnnounce.textContent = `Could not ${action} ${niceName}: ${error.message}`;
        clearLiveRegionLater(queueAnnounce);
      }
      const restored = queueList.querySelector(
        `li[data-path="${CSS.escape(path)}"] .queue-btn[data-action="${action}"]`,
      );
      if (restored) restored.focus();
    } finally {
      const current = queueList.querySelector(
        `li[data-path="${CSS.escape(path)}"] .queue-btn[data-action="${action}"]`,
      );
      if (current) current.disabled = false;
    }
```

- [ ] **Step 7: Run browser mutation tests**

Run: `npm test -- --run tests/jsmodules/api-client.test.js tests/jsmodules/latest-request.test.js tests/jsmodules/auth.test.js tests/jsmodules/search.test.js tests/jsmodules/queue.test.js tests/jsmodules/library-jobs.test.js tests/jsmodules/media-session.test.js tests/jsmodules/settings-panel.test.js tests/jsmodules/app-source.test.js tests/jsmodules/fetch-audit.test.js`

Expected: PASS for HTTP failure, non-JSON success, application rejection, mid-session
authentication recovery, safe binary errors, explicit best-effort reporting, control
recovery, rollback, announcements, focus, and the no-unchecked-fetch source audit.

- [ ] **Step 8: Commit recoverable browser requests**

```bash
git add src/autodj/static/modules/api-client.js src/autodj/static/modules/latest-request.js src/autodj/static/modules/auth.js src/autodj/static/modules/search.js src/autodj/static/modules/queue.js src/autodj/static/modules/settings-panel.js src/autodj/static/modules/library-jobs.js src/autodj/static/modules/liners.js src/autodj/static/modules/media-session.js src/autodj/static/modules/audio-engine.js src/autodj/static/app.js tests/jsmodules/api-client.test.js tests/jsmodules/latest-request.test.js tests/jsmodules/auth.test.js tests/jsmodules/search.test.js tests/jsmodules/queue.test.js tests/jsmodules/library-jobs.test.js tests/jsmodules/media-session.test.js tests/jsmodules/settings-panel.test.js tests/jsmodules/app-source.test.js tests/jsmodules/fetch-audit.test.js
git commit -m "fix: validate and recover browser requests"
```

### Task 6: Native-control hotkeys and distinct-track liner cadence

**Files:**
- Modify: `src/autodj/static/modules/hotkeys.js`
- Modify: `src/autodj/static/modules/liners.js`
- Modify: `src/autodj/static/app.js:110-116,1357`
- Create: `tests/jsmodules/hotkeys.test.js`
- Create: `tests/jsmodules/liners.test.js`
- Modify: `tests/jsmodules/app-source.test.js`

- [ ] **Step 1: Write failing native-control and liner-counter tests**

Create `tests/jsmodules/hotkeys.test.js`:

```javascript
// @vitest-environment happy-dom
import { expect, it, vi } from "vitest";
import { installHotkeys } from "../../src/autodj/static/modules/hotkeys.js";

function press(target, key) {
  const down = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  target.dispatchEvent(down);
  target.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
  return down;
}

it("preserves native widget keys but handles shortcuts from plain content", () => {
  document.body.innerHTML = `
    <section id="panel-now"><div id="plain" tabindex="0"></div>
      <button id="native-button">Native</button>
      <input id="native-range" type="range" value="50">
      <select id="native-select"><option value="s">S</option></select>
    </section>`;
  const pause = { click: vi.fn() };
  const skip = { click: vi.fn() };
  const shuffle = { click: vi.fn() };
  const mute = { click: vi.fn() };
  const volume = document.querySelector("#native-range");
  installHotkeys({
    btnPause: pause,
    btnSkip: skip,
    btnShuffle: shuffle,
    btnMute: mute,
    volSlider: volume,
    seekDelta: vi.fn(),
    getBpm: () => 120,
    getTrack: () => null,
    getNextTrack: () => null,
    getRemaining: () => null,
  });

  expect(press(document.querySelector("#native-button"), " ").defaultPrevented).toBe(false);
  expect(press(volume, "ArrowUp").defaultPrevented).toBe(false);
  expect(press(volume, "ArrowDown").defaultPrevented).toBe(false);
  expect(press(document.querySelector("#native-select"), "s").defaultPrevented).toBe(false);
  expect(pause.click).not.toHaveBeenCalled();
  expect(shuffle.click).not.toHaveBeenCalled();
  expect(volume.value).toBe("50");

  const pageShortcut = press(document.querySelector("#plain"), "n");
  expect(pageShortcut.defaultPrevented).toBe(true);
  expect(skip.click).toHaveBeenCalledOnce();
});
```

Create `tests/jsmodules/liners.test.js`:

```javascript
// @vitest-environment happy-dom
import { beforeEach, expect, it } from "vitest";
import {
  bumpLinerTrackCount,
  getLinerTrackCountForTest,
  resetLinerStateForTest,
} from "../../src/autodj/static/modules/liners.js";

beforeEach(() => resetLinerStateForTest());

it("counts completed distinct-track changes once", () => {
  for (const path of ["a.flac", "a.flac", "b.flac", "b.flac", "c.flac"]) {
    bumpLinerTrackCount({ current_track: { path } });
  }

  expect(getLinerTrackCountForTest()).toBe(2);
});
```

Append this wiring regression to `tests/jsmodules/app-source.test.js`:

```javascript
it("calls the imported liner counter without a stale alias guard", () => {
  expect(source).toContain("bumpLinerTrackCount(s);");
  expect(source).not.toContain("_bumpLinerTrackCount");
});
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run: `npm test -- --run tests/jsmodules/hotkeys.test.js tests/jsmodules/liners.test.js`

Expected: FAIL because global handlers intercept native controls, the new liner test seam is absent, and `app.js` checks `_bumpLinerTrackCount` but calls `bumpLinerTrackCount`.

- [ ] **Step 3: Add an explicit interactive-target boundary before the key latch**

Add and use this helper before `_pressed.add(e.key)`:

```javascript
export function ownsNativeKeyboardBehavior(target) {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest(
    "button, input, select, textarea, a[href], summary, [contenteditable='true'], " +
    "[role='button'], [role='slider'], [role='spinbutton'], [role='combobox'], " +
    "[role='listbox'], [role='menuitem'], [role='option'], [role='switch'], [role='tab']",
  ));
}
```

Replace the first four statements inside the keydown listener with this order so native controls return before the key latch mutates:

```javascript
    if (e.repeat) return;
    if (isTypingTarget(e.target) || ownsNativeKeyboardBehavior(e.target)) return;
    if (_pressed.has(e.key)) return;
    _pressed.add(e.key);
```

The shortcuts dialog's own Close button and tablist therefore retain Space/arrow behavior automatically.

- [ ] **Step 4: Make liner cadence observable and call it exactly once per distinct path**

Replace the liner counter function with:

```javascript
export function bumpLinerTrackCount(s) {
  const currentPath = s && s.current_track ? s.current_track.path : null;
  if (!currentPath || currentPath === state.lastSeenPath) return false;
  if (state.lastSeenPath !== null) state.trackCount += 1;
  state.lastSeenPath = currentPath;
  return true;
}

export function getLinerTrackCountForTest() {
  return state.trackCount;
}

export function resetLinerStateForTest() {
  state.trackCount = 0;
  state.lastSeenPath = null;
}
```

In `applyState`, replace the typo-guarded invocation with one unconditional imported call:

```javascript
  bumpLinerTrackCount(s);
```

- [ ] **Step 5: Run hotkey and liner regressions**

Run: `npm test -- --run tests/jsmodules/hotkeys.test.js tests/jsmodules/liners.test.js tests/jsmodules/app-source.test.js`

Expected: PASS; native widgets keep their keys, page-level shortcuts still work, repeated WebSocket ticks do not inflate liner cadence, and every distinct track transition increments once.

- [ ] **Step 6: Commit interaction scoping**

```bash
git add src/autodj/static/modules/hotkeys.js src/autodj/static/modules/liners.js src/autodj/static/app.js tests/jsmodules/hotkeys.test.js tests/jsmodules/liners.test.js tests/jsmodules/app-source.test.js
git commit -m "fix: scope hotkeys and liner cadence"
```

### Task 7: Complete seek lifecycle and race-proof path-owned lyrics

**Files:**
- Create: `src/autodj/static/modules/seek-controller.js`
- Create: `tests/jsmodules/seek-controller.test.js`
- Modify: `src/autodj/static/modules/lyrics.js`
- Create: `tests/jsmodules/lyrics.test.js`
- Modify: `tests/jsmodules/app-source.test.js`
- Modify: `src/autodj/static/app.js:139-155,214-255,941-1040`
- Modify: `src/autodj/_bridge.py:800-807`
- Modify: `src/autodj/server.py:811-814`
- Modify: `tests/integration/test_server.py:1990-2012`

- [ ] **Step 1: Write failing tests for cancellation, unrelated state, and lyric races**

Create `tests/jsmodules/seek-controller.test.js`:

```javascript
// @vitest-environment happy-dom
import { describe, expect, it, vi } from "vitest";
import { installSeekController } from
  "../../src/autodj/static/modules/seek-controller.js";

function pointer(type, id = 7) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "pointerId", { value: id });
  Object.defineProperty(event, "button", { value: 0 });
  return event;
}

describe("installSeekController", () => {
  for (const terminal of ["pointerup", "pointercancel", "lostpointercapture"]) {
    it(`clears dragging on ${terminal}`, () => {
      const track = document.createElement("div");
      track.setPointerCapture = vi.fn();
      track.hasPointerCapture = vi.fn().mockReturnValue(terminal !== "lostpointercapture");
      track.releasePointerCapture = vi.fn();
      const previewAt = vi.fn();
      const commitAt = vi.fn();
      const controller = installSeekController(track, { previewAt, commitAt });

      track.dispatchEvent(pointer("pointerdown"));
      expect(controller.isDragging()).toBe(true);
      track.dispatchEvent(pointer(terminal));

      expect(controller.isDragging()).toBe(false);
      expect(commitAt).toHaveBeenCalledTimes(terminal === "pointerup" ? 1 : 0);
    });
  }

  it("clears state even when commit and release throw", () => {
    const track = document.createElement("div");
    track.setPointerCapture = vi.fn();
    track.hasPointerCapture = vi.fn().mockReturnValue(true);
    track.releasePointerCapture = vi.fn(() => { throw new Error("release failed"); });
    const controller = installSeekController(track, {
      previewAt: vi.fn(),
      commitAt: vi.fn(() => { throw new Error("commit failed"); }),
    });
    track.dispatchEvent(pointer("pointerdown"));

    expect(() => controller.finish(pointer("pointerup"), true)).toThrow("commit failed");
    expect(controller.isDragging()).toBe(false);
  });

  it("cannot re-enter finishing through synchronous lost capture", () => {
    const track = document.createElement("div");
    track.setPointerCapture = vi.fn();
    track.hasPointerCapture = vi.fn().mockReturnValue(true);
    track.releasePointerCapture = vi.fn(() => {
      track.dispatchEvent(pointer("lostpointercapture"));
    });
    const commitAt = vi.fn();
    const controller = installSeekController(track, { previewAt: vi.fn(), commitAt });
    track.dispatchEvent(pointer("pointerdown"));

    controller.finish(pointer("pointerup"), true);

    expect(commitAt).toHaveBeenCalledOnce();
    expect(controller.isDragging()).toBe(false);
  });
});
```

Create `tests/jsmodules/lyrics.test.js`:

```javascript
// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { applyLyricsState, getCachedLyrics, loadLyrics, resetLyricState } from
  "../../src/autodj/static/modules/lyrics.js";

afterEach(() => {
  resetLyricState();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

it("aborts the old path and ignores its late response", async () => {
  document.body.innerHTML = `<details id="card"></details><ol id="list"></ol>`;
  const elements = {
    lyricsCard: document.querySelector("#card"),
    lyricsList: document.querySelector("#list"),
  };
  const first = deferred();
  const second = deferred();
  const calls = [];
  vi.stubGlobal("fetch", vi.fn((url, options) => {
    calls.push({ url, signal: options.signal });
    return calls.length === 1 ? first.promise : second.promise;
  }));

  const loadA = loadLyrics("A folder/a.flac", elements);
  const loadB = loadLyrics("b.flac", elements);
  expect(calls[0].signal.aborted).toBe(true);
  expect(calls[0].url).toBe("/api/lyrics?path=A%20folder%2Fa.flac");
  expect(calls[1].url).toBe("/api/lyrics?path=b.flac");
  second.resolve(new globalThis.Response(JSON.stringify({
    path: "b.flac", lyrics: [{ time_s: 1, text: "B line" }],
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  await loadB;
  first.resolve(new globalThis.Response(JSON.stringify({
    path: "A folder/a.flac", lyrics: [{ time_s: 1, text: "A line" }],
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  await loadA;

  expect(getCachedLyrics()).toEqual([{ time_s: 1, text: "B line" }]);
  expect(elements.lyricsList.textContent).toContain("B line");
  expect(elements.lyricsList.textContent).not.toContain("A line");
});

it("clears old lyrics and exposes loading state at generation change", async () => {
  document.body.innerHTML = `
    <details id="card"></details><ol id="list"><li>Old line</li></ol>
    <div id="announce"></div>`;
  const elements = {
    lyricsCard: document.querySelector("#card"),
    lyricsList: document.querySelector("#list"),
    lyricAnnounce: document.querySelector("#announce"),
  };
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn().mockReturnValue(response.promise));

  const loading = loadLyrics("new.flac", elements);

  expect(getCachedLyrics()).toEqual([]);
  expect(elements.lyricsList.textContent).not.toContain("Old line");
  expect(elements.lyricAnnounce.textContent).toBe("Loading lyrics.");
  response.resolve(new globalThis.Response(JSON.stringify({
    path: "new.flac", lyrics: [{ time_s: 1, text: "New line" }],
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  await loading;
});

it("announces only the current generation failure", async () => {
  document.body.innerHTML = `
    <details id="card"></details><ol id="list"></ol><div id="announce"></div>`;
  const elements = {
    lyricsCard: document.querySelector("#card"),
    lyricsList: document.querySelector("#list"),
    lyricAnnounce: document.querySelector("#announce"),
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
    JSON.stringify({ detail: "lyrics service unavailable" }),
    { status: 503, headers: { "Content-Type": "application/json" } },
  )));

  await loadLyrics("current.flac", elements);

  expect(elements.lyricAnnounce.textContent)
    .toContain("Lyrics unavailable: lyrics service unavailable");
  expect(elements.lyricsList.children).toHaveLength(0);
});

it("uses instant lyric scrolling when reduced motion is requested", async () => {
  document.body.innerHTML = `
    <details id="card"></details><ol id="list"></ol><div id="announce"></div>`;
  const elements = {
    lyricsCard: document.querySelector("#card"),
    lyricsList: document.querySelector("#list"),
    lyricAnnounce: document.querySelector("#announce"),
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
    JSON.stringify({ path: "track.flac", lyrics: [{ time_s: 1, text: "Line" }] }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  )));
  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: true });
  const scrollIntoView = vi.fn();
  window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
  await loadLyrics("track.flac", elements);

  applyLyricsState(
    { has_lyrics: true, lyric_index: 0, lyric_text: "Line" },
    elements,
  );

  expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "auto", block: "center" });
});
```

Append this block to the `app-source.test.js` created in Task 5; reuse its existing `source`, `describe`, `expect`, and `it` bindings. This makes the application-level continuation contract deterministic without executing the audio engine:

```javascript
describe("applyState seek isolation", () => {
  it("does not return from applyState while the seek controller is dragging", () => {
    expect(source).not.toMatch(/if\s*\(_seekDragging\)\s*return/);
    const guard = source.indexOf("if (!_seekController.isDragging())");
    const followingState = source.indexOf("setLastBrowserPlayback", guard);
    expect(guard).toBeGreaterThan(-1);
    expect(followingState).toBeGreaterThan(guard);
    expect(source.slice(guard, followingState)).not.toMatch(/\breturn\b/);
  });
});
```

- [ ] **Step 2: Run seek/lyrics tests and verify the red state**

Run: `npm test -- --run tests/jsmodules/seek-controller.test.js tests/jsmodules/lyrics.test.js tests/jsmodules/app-source.test.js`

Expected: FAIL because dragging is cleared only on `pointerup`, `applyState` returns before unrelated UI work, and lyrics are current-global rather than request/path-owned.

- [ ] **Step 3: Create a pointer lifecycle controller**

Create `seek-controller.js` with this complete public contract:

```javascript
export function installSeekController(track, { previewAt, commitAt }) {
  let pointerId = null;
  const finish = (event, commit) => {
    if (pointerId === null || (event.pointerId != null && event.pointerId !== pointerId)) return;
    const finishingPointer = pointerId;
    pointerId = null;
    try {
      if (commit) commitAt(event);
    } finally {
      if (track.hasPointerCapture && track.hasPointerCapture(finishingPointer)) {
        try { track.releasePointerCapture(finishingPointer); } catch (_) {}
      }
    }
  };
  track.addEventListener("pointerdown", (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    pointerId = event.pointerId;
    try { if (track.setPointerCapture) track.setPointerCapture(pointerId); } catch (_) {}
    previewAt(event);
    event.preventDefault();
  });
  track.addEventListener("pointermove", (event) => {
    if (pointerId !== null && event.pointerId === pointerId) previewAt(event);
  });
  track.addEventListener("pointerup", (event) => finish(event, true));
  track.addEventListener("pointercancel", (event) => finish(event, false));
  track.addEventListener("lostpointercapture", (event) => finish(event, false));
  return {
    finish,
    isDragging: () => pointerId !== null,
  };
}
```

Add this import beside the lyrics import in `app.js`:

```javascript
import { installSeekController } from "./modules/seek-controller.js";
```

Delete `let _seekDragging = false;` and all four existing pointer listeners. Keep the keyboard listener. Immediately before that keyboard listener, install the controller with the existing `_seekToFrac` function:

```javascript
const _seekController = _seekTrack
  ? installSeekController(_seekTrack, {
      previewAt: (event) => {
        const rect = _seekTrack.getBoundingClientRect();
        _seekToFrac((event.clientX - rect.left) / rect.width);
      },
      commitAt: (event) => {
        const rect = _seekTrack.getBoundingClientRect();
        _seekToFrac((event.clientX - rect.left) / rect.width, { force: true });
      },
    })
  : { isDragging: () => false };
```

In `applyState`, replace the current `if (_seekDragging) return;` through the progress ARIA updates with this block; the next existing statement must remain `setLastBrowserPlayback(s.browser_playback)`:

```javascript
  if (!_seekController.isDragging()) {
    progressFill.style.width = pct.toFixed(1) + "%";
    const timeText = `${fmtTime(elapsed)} / ${fmtTime(dur)}`;
    progressLbl.textContent = timeText;
    const progressTrack = document.getElementById("progress-track");
    if (progressTrack) {
      progressTrack.setAttribute("aria-valuenow", pct.toFixed(0));
      progressTrack.setAttribute(
        "aria-valuetext",
        `${fmtTime(elapsed)} of ${fmtTime(dur)}`,
      );
    }
  }
```

- [ ] **Step 4: Make the lyrics endpoint path-owned**

Add this method beside `current_lyrics`, then change the route:

```python
    def lyrics_for(self, path: str) -> list[dict]:
        """Read timed lyrics owned by *path*, independent of current-track state."""
        lyrics, _plain = self.player._read_lyrics_for_path(path)
        return [{"time_s": line.time_s, "text": line.text} for line in lyrics]
```

Change the route to:

```python
    @app.get("/api/lyrics")
    async def api_lyrics(path: str) -> dict[str, object]:
        if bridge.sim.entry_for_path(path) is None:
            raise HTTPException(status_code=404, detail="Track not in index")
        lyrics = await asyncio.to_thread(bridge.lyrics_for, path)
        return {"path": path, "lyrics": lyrics}
```

Replace `test_lyrics_endpoint_returns_list` and add the unknown-path test:

```python
    def test_lyrics_endpoint_is_owned_by_requested_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        entries = bridge.sim.entries_snapshot()
        first = entries[0].path
        second = entries[1].path
        bridge.lyrics_for = MagicMock(
            side_effect=lambda path: [{"time_s": 1.0, "text": f"lyrics:{path}"}]
        )
        client = TestClient(create_app(bridge))

        first_data = client.get("/api/lyrics", params={"path": first}).json()
        second_data = client.get("/api/lyrics", params={"path": second}).json()

        assert first_data == {
            "path": first,
            "lyrics": [{"time_s": 1.0, "text": f"lyrics:{first}"}],
        }
        assert second_data == {
            "path": second,
            "lyrics": [{"time_s": 1.0, "text": f"lyrics:{second}"}],
        }

    def test_lyrics_endpoint_rejects_unknown_path(self, client) -> None:
        response = client.get("/api/lyrics", params={"path": "not-indexed.flac"})
        assert response.status_code == 404
```

- [ ] **Step 5: Add request generation plus abort ownership in `lyrics.js`**

Add `import { requestJson } from "./api-client.js";`. Replace `loadLyrics` and reset state with:

```javascript
let requestGeneration = 0;
let activeController = null;

export function resetLyricState(elements = null) {
  requestGeneration += 1;
  if (activeController) activeController.abort();
  activeController = null;
  state.lastIndex = null;
  state.cached = [];
  if (elements) renderLyricsList(elements);
}

export async function loadLyrics(path, elements) {
  const generation = ++requestGeneration;
  if (activeController) activeController.abort();
  const controller = new AbortController();
  activeController = controller;
  state.lastIndex = null;
  state.cached = [];
  renderLyricsList(elements);
  if (elements.lyricAnnounce) {
    elements.lyricAnnounce.textContent = "Loading lyrics.";
  }
  try {
    const data = await requestJson(`/api/lyrics?path=${encodeURIComponent(path)}`, {
      signal: controller.signal,
    });
    if (generation !== requestGeneration || data.path !== path) return;
    state.cached = Array.isArray(data.lyrics) ? data.lyrics : [];
    renderLyricsList(elements);
    if (elements.lyricAnnounce) {
      elements.lyricAnnounce.textContent = state.cached.length
        ? "Lyrics loaded."
        : "No lyrics available.";
    }
  } catch (error) {
    if (error.name === "AbortError" || generation !== requestGeneration) return;
    state.cached = [];
    renderLyricsList(elements);
    if (elements.lyricAnnounce) {
      elements.lyricAnnounce.textContent = `Lyrics unavailable: ${error.message}`;
    }
  } finally {
    if (generation === requestGeneration) activeController = null;
  }
}
```

Replace `loadLyrics(_lyricEls)` with `void loadLyrics(trackKey, _lyricEls)` in
`app.js`. In the track-empty branch, call `resetLyricState(_lyricEls)` immediately after
`clearCoverArt()` so the prior list disappears even though no replacement request starts.
Replace the smooth-scroll call in `applyLyricsState` with:

```javascript
    const reduceMotion = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    li.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
```

- [ ] **Step 6: Run Python and browser lyric/seek slices**

Run: `uv run pytest tests/integration/test_server.py -k lyrics -q`

Run: `npm test -- --run tests/jsmodules/seek-controller.test.js tests/jsmodules/lyrics.test.js tests/jsmodules/app-source.test.js`

Expected: PASS; cancellation never leaves drag mode stuck, state outside progress keeps updating, stale/aborted lyrics never render, and reduced-motion users do not receive smooth auto-scroll.

- [ ] **Step 7: Commit seek and lyrics ownership**

```bash
git add src/autodj/static/modules/seek-controller.js src/autodj/static/modules/lyrics.js src/autodj/static/app.js src/autodj/_bridge.py src/autodj/server.py tests/jsmodules/seek-controller.test.js tests/jsmodules/lyrics.test.js tests/jsmodules/app-source.test.js tests/integration/test_server.py
git commit -m "fix: own seek and lyric request lifecycles"
```

### Task 8: Durable metadata/cues, accurate names, and stable queue focus

**Files:**
- Modify: `src/autodj/static/index.html:57-116,745-760`
- Modify: `src/autodj/static/modules/badges.js`
- Modify: `src/autodj/static/modules/cues.js`
- Modify: `src/autodj/static/modules/liners.js:56-75`
- Modify: `src/autodj/static/modules/queue.js`
- Modify: `src/autodj/static/app.js:162-195`
- Modify: `tests/jsmodules/cues.test.js`
- Create: `tests/jsmodules/badges.test.js`
- Modify: `tests/jsmodules/liners.test.js`
- Modify: `tests/jsmodules/queue.test.js`
- Modify: `tests/jsmodules/app-source.test.js`

- [ ] **Step 1: Write failing durable-text, accessible-name, live-region, and focus tests**

Create `tests/jsmodules/badges.test.js`:

```javascript
import { expect, it } from "vitest";
import { formatPersistentMetadata } from
  "../../src/autodj/static/modules/badges.js";

it("formats every persistent field in a fixed order", () => {
  expect(formatPersistentMetadata({
    album: "Album", bpm: 127.6, key_label: "8A", energy: 0.734,
  })).toBe("Album Album · BPM 128 · Key 8A · Energy 0.73");
});

it("keeps explicit unknown placeholders", () => {
  expect(formatPersistentMetadata({
    album: "", bpm: 0, key_label: "--", energy: 0,
  })).toBe("Album unknown · BPM unknown · Key unknown · Energy unknown");
});
```

Add `// @vitest-environment happy-dom` as the first line of `tests/jsmodules/cues.test.js`, append this test, and add `applyCueSummary` to its import:

```javascript
it("writes a durable non-live cue description", () => {
  const summary = document.createElement("p");
  applyCueSummary({ cues: [{ type: "drop", time_s: 30 }] }, summary);

  expect(summary.textContent).toContain("1 cue point");
  expect(summary.textContent).toContain("drop at 30 seconds");
  expect(summary.hasAttribute("aria-live")).toBe(false);
});
```

The liner test file already has the Happy DOM directive from Task 6. Add `vi` to its
Vitest import and `renderLinerFileList` to its module import, then append:

```javascript
it("names liner deletion with action and filename", () => {
  const list = document.createElement("ul");
  renderLinerFileList(list, ["station-id.wav"], () => {});

  const button = list.querySelector("button");
  expect(button.textContent).toBe("Delete");
  expect(button.getAttribute("aria-label")).toBe("Delete station-id.wav");
});

it("passes the delete button through so failure recovery can reenable and refocus it", async () => {
  const list = document.createElement("ul");
  const onDelete = vi.fn(async (_name, control) => {
    control.disabled = true;
    try {
      throw new Error("delete failed");
    } catch (_) {
      // `_deleteLiner` announces the failure; this seam verifies control ownership.
    } finally {
      control.disabled = false;
      control.focus();
    }
  });
  renderLinerFileList(list, ["station-id.wav"], onDelete);
  document.body.appendChild(list);
  const button = list.querySelector("button");

  button.click();
  await vi.waitFor(() => expect(onDelete).toHaveBeenCalledOnce());

  expect(onDelete).toHaveBeenCalledWith("station-id.wav", button);
  expect(button.disabled).toBe(false);
  expect(document.activeElement).toBe(button);
});
```

Append this success-path focus test to `tests/jsmodules/queue.test.js`:

```javascript
it("focuses the queue after the final successful removal", async () => {
  document.body.innerHTML = `
    <div id="announce"></div><span id="count"></span>
    <ol id="queue" tabindex="-1"></ol>`;
  const els = {
    queueList: document.querySelector("#queue"),
    queueCount: document.querySelector("#count"),
    queueAnnounce: document.querySelector("#announce"),
  };
  renderQueue([{ path: "last.flac", artist: "A", title: "Last" }], els);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
    JSON.stringify({ ok: true }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  )));
  installQueueButtons(els);

  els.queueList.querySelector('[data-action="remove"]').click();
  await vi.waitFor(() => expect(els.queueList.querySelectorAll("li[data-path]")).toHaveLength(0));

  expect(document.activeElement).toBe(els.queueList);
  expect(els.queueAnnounce.textContent).toContain("Removed A — Last");
});
```

Append this source contract to `tests/jsmodules/app-source.test.js`, reusing its existing `readFileSync`, `describe`, `expect`, and `it` imports:

```javascript
const markup = readFileSync("src/autodj/static/index.html", "utf8");
const tag = (id) => markup.match(new RegExp(`<[^>]+id="${id}"[^>]*>`))?.[0] || "";

describe("durable playback markup", () => {
  it("exposes metadata, cue relationship, status role, and queue focus target", () => {
    expect(tag("now-playing-meta")).not.toContain("aria-hidden");
    expect(tag("progress-track")).toContain('aria-describedby="cue-summary"');
    expect(tag("cue-summary")).not.toBe("");
    expect(tag("cue-summary")).not.toContain("aria-live");
    expect(tag("queue-announce")).toContain('role="status"');
    expect(tag("queue-list")).toContain('tabindex="-1"');
  });
});
```

- [ ] **Step 2: Run semantic component tests and verify the red state**

Run: `npm test -- --run tests/jsmodules/cues.test.js tests/jsmodules/badges.test.js tests/jsmodules/liners.test.js tests/jsmodules/queue.test.js`

Expected: FAIL because metadata is `aria-hidden`, cues are visual-only, Delete is hidden from the accessible name, Energy is not persistent, and final-row focus is dropped.

- [ ] **Step 3: Expose one persistent now-playing summary and one persistent cue description**

Add this export to `badges.js`:

```javascript
export function formatPersistentMetadata(track) {
  if (!track) return "";
  const key = track.key_label && track.key_label !== "--"
    ? track.key_label
    : "unknown";
  return [
    `Album ${track.album || "unknown"}`,
    `BPM ${track.bpm ? Math.round(track.bpm) : "unknown"}`,
    `Key ${key}`,
    `Energy ${track.energy > 0 ? track.energy.toFixed(2) : "unknown"}`,
  ].join(" · ");
}
```

Import it with `applyBadges` in `app.js`, replace the hand-built `parts` block with `npMeta.textContent = formatPersistentMetadata(s.current_track)`, and remove `aria-hidden="true"` from `#now-playing-meta`.

Add this export to `cues.js`:

```javascript
export function applyCueSummary(track, element) {
  if (!element) return;
  const cues = track && Array.isArray(track.cues) ? track.cues : [];
  element.textContent = cues.length ? summariseCues(cues) : "No cue points.";
}
```

Import `applyCueSummary` beside `_renderCueStripModule` in `app.js`, add `const _cueSummary = document.getElementById("cue-summary");`, and replace the local wrapper with:

```javascript
function renderCueStrip(track) {
  _renderCueStripModule(_cueStrip, track);
  applyCueSummary(track, _cueSummary);
}
```

In `index.html`, add `aria-describedby="cue-summary"` to `#progress-track` and insert this paragraph immediately after the closing `#progress-track` div, outside the slider role:

```html
      <p id="cue-summary">No cue points.</p>
```

- [ ] **Step 4: Keep live announcements event-scoped**

Replace the queue announcer opening tag with this exact status-region contract:

```html
    <div id="queue-announce"
         role="status"
         aria-live="polite"
         aria-atomic="true"
         class="visually-hidden"></div>
```

Add `aria-atomic="true"` to the existing `#settings-status` tag so all three user-operation regions share the same contract. Leave `#now-playing-announce`, `#badges-announce`, `#lyric-announce`, and `#ln-status` as their existing event-scoped regions. Do not add `aria-live` or `role="status"` to `#now-playing-meta`, `#cue-summary`, `#queue-list`, or the surrounding cards. This exact absence is asserted in Task 9.

- [ ] **Step 5: Give liner deletion and final queue state explicit focus targets**

Add this exported renderer to `liners.js`:

```javascript
export function renderLinerFileList(fileList, names, onDelete) {
  fileList.innerHTML = "";
  for (const name of names) {
    const item = document.createElement("li");
    const filename = document.createElement("span");
    filename.textContent = name;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Delete";
    button.setAttribute("aria-label", `Delete ${name}`);
    button.addEventListener("click", () => onDelete(name, button));
    item.append(filename, document.createTextNode(" "), button);
    fileList.appendChild(item);
  }
}
```

Change the module import to `import { dbg } from "./dom-helpers.js";` because the renderer no longer constructs HTML with `escHtml`. Replace the inline `for (const name of body.files || [])` block in `_refreshLibrary` with:

```javascript
      renderLinerFileList(
        els.lnFileList,
        body.files || [],
        (name, button) => void _deleteLiner(els, name, button),
      );
```

Change the queue markup to `<ol id="queue-list" role="list" aria-label="Queued tracks" tabindex="-1">`. The Task 5 success branch already calls `queueList.focus()` when `focusPath` is null; do not add a second focus move. Its catch branch restores and refocuses the original Remove button.

- [ ] **Step 6: Run semantic component regressions**

Run: `npm test -- --run tests/jsmodules/cues.test.js tests/jsmodules/badges.test.js tests/jsmodules/liners.test.js tests/jsmodules/queue.test.js tests/jsmodules/app-source.test.js`

Expected: PASS; current information remains queryable after announcements expire, cue descriptions are connected to seek, names include action plus filename, and focus remains stable through final removal.

- [ ] **Step 7: Commit accessible state semantics**

```bash
git add src/autodj/static/index.html src/autodj/static/modules/badges.js src/autodj/static/modules/cues.js src/autodj/static/modules/liners.js src/autodj/static/modules/queue.js src/autodj/static/app.js tests/jsmodules/cues.test.js tests/jsmodules/badges.test.js tests/jsmodules/liners.test.js tests/jsmodules/queue.test.js tests/jsmodules/app-source.test.js
git commit -m "fix: expose durable playback semantics"
```

### Task 9: Contrast, reduced motion, and static accessibility contracts

**Files:**
- Modify: `src/autodj/static/app.css:70-195,500-705`
- Create: `tests/jsmodules/a11y-contract.test.js`
- Create: `docs/accessibility-testing.md`

- [ ] **Step 1: Add failing source-level contrast, motion, and relationship tests**

Create `tests/jsmodules/a11y-contract.test.js`:

```javascript
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

const html = readFileSync("src/autodj/static/index.html", "utf8");
const css = readFileSync("src/autodj/static/app.css", "utf8");
const document = new JSDOM(html).window.document;

function rgb(hex) {
  const value = hex.replace("#", "");
  return [0, 2, 4].map((offset) => parseInt(value.slice(offset, offset + 2), 16) / 255);
}

function luminance(hex) {
  const channels = rgb(hex).map((channel) =>
    channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(first, second) {
  const light = Math.max(luminance(first), luminance(second));
  const dark = Math.min(luminance(first), luminance(second));
  return (light + 0.05) / (dark + 0.05);
}

describe("WCAG color contracts", () => {
  it("gates connected text at 4.5:1", () => {
    const rule = css.match(/#conn-status\.connected\s*\{([^}]*)\}/)?.[1] || "";
    expect(rule).toContain("background: #4ade80");
    expect(rule).toContain("color: #081f12");
    expect(contrast("#081f12", "#4ade80")).toBeGreaterThanOrEqual(4.5);
  });

  it("gates control boundaries at 3:1", () => {
    expect(css).toMatch(/#progress-track,[\s\S]*?border-color:\s*#8aa4d6/);
    expect(css).toMatch(/--bg-card:\s*#16213e/);
    expect(contrast("#8aa4d6", "#16213e")).toBeGreaterThanOrEqual(3);
  });
});

describe("reduced motion", () => {
  it("disables smooth scrolling and nonessential motion", () => {
    const start = css.indexOf("@media (prefers-reduced-motion: reduce)");
    const media = start < 0 ? "" : css.slice(start, start + 600);
    expect(media).toContain("scroll-behavior: auto");
    expect(media).toContain("animation-duration: 0.01ms !important");
    expect(media).toContain("transition-duration: 0.01ms !important");
  });
});

describe("static semantic relationships", () => {
  it("keeps metadata and cue summary durable rather than live", () => {
    const metadata = document.querySelector("#now-playing-meta");
    const cueSummary = document.querySelector("#cue-summary");
    expect(metadata).not.toBeNull();
    expect(cueSummary).not.toBeNull();
    expect(metadata.hasAttribute("aria-hidden")).toBe(false);
    expect(metadata.hasAttribute("aria-live")).toBe(false);
    expect(cueSummary.hasAttribute("aria-live")).toBe(false);
    expect(cueSummary.hasAttribute("role")).toBe(false);
  });

  it("connects the seek control to the cue description", () => {
    const describedBy = document.querySelector("#progress-track")
      .getAttribute("aria-describedby");
    expect(describedBy).not.toBeNull();
    const tokens = describedBy.split(/\s+/);
    expect(tokens).toContain("cue-summary");
  });

  it("uses scoped status regions for user operations", () => {
    for (const id of ["queue-announce", "ln-status", "settings-status"]) {
      const region = document.querySelector(`#${id}`);
      expect(region.getAttribute("role")).toBe("status");
      expect(region.getAttribute("aria-live")).toBe("polite");
      expect(region.getAttribute("aria-atomic")).toBe("true");
    }
    expect(document.querySelector("#queue-list").hasAttribute("aria-live")).toBe(false);
  });
});
```

- [ ] **Step 2: Run the contract test and verify the red state**

Run: `npm test -- --run tests/jsmodules/a11y-contract.test.js`

Expected: FAIL because connected text is low contrast, track/control boundaries are indistinct, and reduced-motion coverage is limited to the Camelot wheel.

- [ ] **Step 3: Apply tested palette tokens and visible boundaries**

Use these exact pairs in `app.css`:

```css
#conn-status.connected {
  background: #4ade80;
  color: #081f12;
}

#progress-track,
input:not([type="checkbox"]):not([type="radio"]),
select,
textarea {
  border-color: #8aa4d6;
}
```

Do not communicate connected/disconnected state by color alone: retain the visible status text and existing status icon/text update.

- [ ] **Step 4: Expand the reduced-motion override and coordinate lyric scrolling**

Replace the existing media query with:

```css
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
}
```

The Task 7 `matchMedia` branch is the JS half of this contract and must remain covered by `lyrics.test.js`.

- [ ] **Step 5: Document the automated/assistive-technology boundary**

Create `docs/accessibility-testing.md` with this exact policy:

```markdown
# Accessibility testing

CI verifies DOM semantics, accessible relationships and names, keyboard event ownership, focus restoration, live-region update policy, reduced-motion behavior, and WCAG contrast calculations.

Automated DOM tests cannot prove what a specific screen reader and browser combination speaks. Before a release, manually sample the Now Playing, seek/cue, queue-removal, liner-delete, lyrics, and request-error flows with the supported NVDA/Firefox or NVDA/Chrome combination. Record defects, not a blanket claim that assistive-technology behavior is verified.
```

- [ ] **Step 6: Run accessibility contracts and the frontend suite**

Run: `npm test -- --run tests/jsmodules/a11y-contract.test.js tests/jsmodules/lyrics.test.js tests/jsmodules/hotkeys.test.js tests/jsmodules/queue.test.js`

Expected: PASS with computed contrast thresholds, reduced-motion assertions, keyboard ownership, focus, relationships, and live-region policy enforced.

- [ ] **Step 7: Commit accessibility contracts**

```bash
git add src/autodj/static/app.css tests/jsmodules/a11y-contract.test.js docs/accessibility-testing.md
git commit -m "fix: enforce contrast and motion preferences"
```

### Task 10: Blocking frontend behavior CI and whole-remediation verification

**Ownership boundary:** this task owns the requirement that frontend install, lint, unit
tests, and build block merges. The later delivery-maintenance plan owns final Node,
GitHub Action, dependency, lockfile, dead-code, and audit versions. If its Task 6 has
already run, preserve that job and add only missing behavior gates. If it runs later,
its exact versions and command variants supersede the toolchain snapshot below; do not
downgrade `actions/*`, Node, or dependencies while executing this behavior plan.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/jsmodules/a11y-contract.test.js`

- [ ] **Step 1: Add a failing workflow-source assertion**

Add this constant and test to `tests/jsmodules/a11y-contract.test.js`:

```javascript
const workflow = readFileSync(".github/workflows/ci.yml", "utf8");

describe("frontend CI gate", () => {
  it("runs install, lint, unit tests, and build as blocking steps", () => {
    const frontend = workflow.match(/\n  frontend:\n([\s\S]*?)(?=\n  [a-zA-Z0-9_-]+:\n|$)/)?.[1] || "";
    expect(frontend).toContain("run: npm ci --ignore-scripts");
    expect(frontend).toContain("run: npm run lint");
    expect(frontend).toMatch(/run: npm test(?: -- --run)?/);
    expect(frontend).toContain("run: npm run build");
    expect(frontend).not.toContain("continue-on-error: true");
  });
});
```

- [ ] **Step 2: Run the workflow contract and verify the red state**

Run: `npm test -- --run tests/jsmodules/a11y-contract.test.js`

Expected: FAIL because the current workflow does not run frontend unit tests and build as blocking gates.

- [ ] **Step 3: Add the behavior gates without taking delivery ownership**

When no frontend job exists yet, add this delivery-compatible snapshot without weakening
the existing Python jobs:

```yaml
  frontend:
    name: Frontend lint, test, build
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with:
          node-version: "24.6.0"
          cache: npm
      - run: npm ci --ignore-scripts
      - run: npm run lint
      - run: npm test
      - run: npm run build
```

When a frontend job already exists, do not replace its setup or install steps. Add only
missing `npm run lint`, `npm test` (or `npm test -- --run`), and `npm run build` blocking
steps. The delivery plan will add its owned `deadcode` and audit gates and remains the
source of truth for `actions/checkout@v6`, `actions/setup-node@v6`, Node `24.6.0`, and
`npm ci --ignore-scripts`.

- [ ] **Step 4: Run every local gate from a clean dependency state**

Run: `uv run ruff check .`

Run: `uv run mypy src/autodj`

Run: `uv run pytest -q`

Run: `npm ci --ignore-scripts`

Run: `npm run lint`

Run: `npm test -- --run`

Run: `npm run build`

Expected: every command exits 0. Do not report completion based only on focused slices; resolve any regression introduced by Tasks 1-9 and rerun the failing command plus this complete gate list.

Do not add `npm run deadcode`, `npm audit`, lockfile regeneration, or dependency upgrades
in this task. Those belong to delivery-maintenance Task 6. After that plan runs, its
larger frontend job must retain these lint/test/build gates.

- [ ] **Step 5: Review requirements and persistence exhaustiveness**

Compare `PlayerBridge.get_settings()["playback"]` to the runtime-state allowlist: every key must be restored and tested or appear in `SESSION_ONLY_PLAYBACK_FIELDS`. Re-read the design document and check off key/BPM analysis, global selection, hard-filter expansion, versioned restore/null clearing, streaming/ranges/ALAC preflight, fetch recovery, hotkeys/liners, seek/lyrics races, persistent metadata/cues/names/focus, live regions, contrast, reduced motion, CI, and the AT-testing limitation.

- [ ] **Step 6: Commit the CI gate**

```bash
git add .github/workflows/ci.yml tests/jsmodules/a11y-contract.test.js
git commit -m "ci: gate frontend behavior and accessibility"
```

## Execution handoff

Execute task-by-task with `superpowers:subagent-driven-development` for isolated review after each commit, or `superpowers:executing-plans` when running the checklist in a separate session. Preserve the red/green order and do not collapse the final full-suite gates into focused-test evidence.
