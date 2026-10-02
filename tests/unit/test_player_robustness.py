"""Player regressions: failed picks, history locking, set-stop races, shutdown."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.indexer import IndexEntry
from autodj.mixbus import RenderedTrack
from autodj.player import Player, PlayerState
from autodj.similarity import SimilarityError, SimilarityIndex
from tests.unit._fakes import make_cfg_mock, make_entry, make_sim_index

WAIT = 5.0


def _player(n: int = 6, **kwargs) -> Player:
    sim = make_sim_index(n)
    for i, entry in enumerate(sim.entries):
        entry.artist, entry.album, entry.title = f"Artist {i}", f"Album {i}", f"Title {i}"
    player = Player(make_cfg_mock(), sim, **kwargs)
    player.load_lyrics_in_background = MagicMock()  # type: ignore[method-assign]
    player._render_track = _stub_render  # type: ignore[method-assign]
    return player


def _stub_render(current, nxt, offset):
    return RenderedTrack(current, np.zeros((10, 2), np.float32), nxt, 33, "", start_offset=offset)


def _rendered(entry: IndexEntry, next_entry: IndexEntry | None = None, **fields) -> RenderedTrack:
    track = RenderedTrack(entry, np.zeros((10, 2), np.float32), next_entry, 0, "")
    return replace(track, **fields)


# ---------------------------------------------------------------------------
# A failed pick never stops the music
# ---------------------------------------------------------------------------


class TestFailedPickRecovers:
    def test_no_candidates_after_hard_filters_falls_back_to_a_random_track(self) -> None:
        player = _player()
        starting, current = player._sim.entries[:2]
        player._sim.find_next_for_path = MagicMock(  # type: ignore[method-assign]
            side_effect=SimilarityError("No candidates satisfy hard filters: bpm_range.")
        )
        player._set_render_cursor(current, 0, "similarity", previous=starting)

        rendered = player._next_rendered()

        assert rendered is not None and rendered.entry is current
        assert rendered.next_entry is not None
        assert rendered.next_entry.path not in {current.path, starting.path}
        assert rendered.next_pick_mode == "fallback"
        assert player._pending_entry is rendered.next_entry

    def test_pending_track_gone_from_the_index_still_gets_a_successor(self) -> None:
        player = _player()
        gone = make_entry(99)  # not in the index any more (reloaded)
        player.reset_render_ahead(gone, 0)

        rendered = player._next_rendered()

        assert rendered is not None and rendered.entry is gone
        assert rendered.next_entry is not None
        assert rendered.next_entry.path in {entry.path for entry in player._sim.entries}
        assert rendered.next_pick_mode == "fallback"

    def test_fallback_ignores_the_bpm_range_and_the_harmonic_mode(self) -> None:
        player = _player(4)
        player._bpm_range = (200.0, 210.0)  # no track qualifies
        player._cfg.djmix.harmonic_mode = "strict"
        current = player._sim.entries[0]
        entry, mode = player._choose_next(current, player._pick_context(current))
        assert mode == "fallback"
        assert entry.path != current.path

    def test_pure_shuffle_with_nothing_in_the_bpm_range_falls_back(self) -> None:
        player = _player(4)
        player._pure_shuffle = True
        player._bpm_range = (200.0, 210.0)
        current = player._sim.entries[0]
        entry, mode = player._choose_next(current, player._pick_context(current))
        assert mode == "fallback"
        assert entry.path != current.path

    def test_an_unexpected_search_error_falls_back_too(self) -> None:
        player = _player()
        player._sim.find_next_for_path = MagicMock(  # type: ignore[method-assign]
            side_effect=KeyError("row")
        )
        current = player._sim.entries[0]
        entry, mode = player._choose_next(current, player._pick_context(current))
        assert (mode, entry.path != current.path) == ("fallback", True)

    def test_one_warning_per_episode(self, caplog: pytest.LogCaptureFixture) -> None:
        player = _player()
        current = player._sim.entries[0]
        failing = MagicMock(side_effect=SimilarityError("no candidates"))
        working = player._sim.find_next_for_path
        context = player._pick_context(current)

        def warnings() -> int:
            return sum(
                1
                for record in caplog.records
                if record.levelno == logging.WARNING and "picking one at random" in record.message
            )

        with caplog.at_level(logging.DEBUG, logger="autodj.player"):
            player._sim.find_next_for_path = failing  # type: ignore[method-assign]
            for _ in range(3):
                player._choose_next(current, context)
            assert warnings() == 1
            player._sim.find_next_for_path = working  # type: ignore[method-assign]
            assert player._choose_next(current, context)[1] == "similarity"
            assert "works again" in caplog.text
            player._sim.find_next_for_path = failing  # type: ignore[method-assign]
            player._choose_next(current, context)
            assert warnings() == 2

    def test_fallback_avoids_the_protected_tracks_while_it_can(self) -> None:
        player = _player(5)
        player._state = PlayerState(no_repeat_window=10, artist_repeat_window=0)
        a, b, c, d, e = player._sim.entries
        for entry in (a, b, c, d, e):
            player._state.record_played(entry)  # everything was played recently
        player._state.current_track = d
        player._sim.find_next_for_path = MagicMock(  # type: ignore[method-assign]
            side_effect=SimilarityError("no candidates")
        )
        # The pick follows C while the bus starts B; D plays, E was recorded last.
        context = player._pick_context(c, starting=b)
        for _ in range(20):
            entry, _mode = player._choose_next(c, context)
            assert entry.path == a.path

    def test_a_one_track_library_repeats_its_only_track(self) -> None:
        player = _player(1)
        only = player._sim.entries[0]
        player._sim.find_next_for_path = MagicMock(  # type: ignore[method-assign]
            side_effect=SimilarityError("no candidates")
        )
        entry, mode = player._choose_next(only, player._pick_context(only))
        assert (entry.path, mode) == (only.path, "fallback")

    def test_an_empty_library_plays_the_track_out_and_parks(self) -> None:
        player = _player()
        current = player._sim.entries[0]
        player._sim = SimilarityIndex.empty()
        player.reset_render_ahead(current, 0)

        rendered = player._next_rendered()

        assert rendered is not None and rendered.entry is current
        assert rendered.next_entry is None
        assert player._pending_entry is None  # parked: nothing to retry

    def test_render_ahead_worker_does_not_retry_a_failed_pick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("autodj.render_ahead._RETRY_SECONDS", 0.01)
        player = _player()
        current = player._sim.entries[0]
        player._sim.find_next_for_path = MagicMock(  # type: ignore[method-assign]
            side_effect=SimilarityError("Track not in index")
        )
        player.reset_render_ahead(current, 0)
        player._render_ahead.start()
        try:
            assert player._render_ahead.wait_ready(WAIT)
        finally:
            player._render_ahead.stop(timeout=WAIT)
        track = player._render_ahead.pop()
        assert track is not None and track.next_pick_mode == "fallback"


# ---------------------------------------------------------------------------
# Relaxed picks still avoid the tracks around the pick
# ---------------------------------------------------------------------------


class TestRelaxedPicksKeepTheirHistory:
    def test_relaxed_similarity_retry_excludes_starting_pending_and_playing(self) -> None:
        player = _player()
        playing, starting, pending, pick = player._sim.entries[:4]
        player._state.current_track = playing
        find = MagicMock(side_effect=[SimilarityError("window covers the index"), pick])
        player._sim.find_next_for_path = find  # type: ignore[method-assign]
        player._set_render_cursor(pending, 0, "similarity", previous=starting)

        player._next_rendered()

        relaxed = set(find.call_args.kwargs["recently_played"])
        assert {playing.path, starting.path, pending.path} <= relaxed

    def test_relaxed_pure_shuffle_never_returns_to_the_pending_or_starting_track(
        self,
    ) -> None:
        player = _player(4)
        player._state = PlayerState(no_repeat_window=4, artist_repeat_window=0)
        player._pure_shuffle = True
        pending, starting, _c, _d = player._sim.entries
        for entry in player._sim.entries:
            player._state.record_played(entry)  # every track is recent
        player._set_render_cursor(pending, 0, "similarity", previous=starting)
        with patch("random.choice", side_effect=lambda pool: pool[0]):
            rendered = player._next_rendered()
        assert rendered is not None and rendered.next_entry is not None
        assert rendered.next_entry.path not in {pending.path, starting.path}

    def test_index_reload_recomputes_the_no_repeat_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autodj._bridge import PlayerBridge

        player = Player(make_cfg_mock(), SimilarityIndex.empty(), dry_run=True)
        assert player._state.no_repeat_window == 50  # empty library: unclamped
        grown = make_sim_index(10)

        def reload(self: SimilarityIndex, _index_dir: Path, music_dir: Path | None = None) -> int:
            self.entries = grown.entries
            return len(self.entries)

        monkeypatch.setattr(SimilarityIndex, "reload_from_disk", reload)
        bridge = PlayerBridge(player=player, sim=player._sim)

        assert bridge.reload_index_from_disk() == 10
        assert player._state.no_repeat_window == 6
        assert player._state.recently_played.maxlen == 6


# ---------------------------------------------------------------------------
# Cursor moves keep the track the bus is starting
# ---------------------------------------------------------------------------


class TestCursorMovesKeepThePreviousTrack:
    def test_fallback_pop_excludes_the_fallback_from_the_next_pick(self) -> None:
        """With no replacement render in flight, the bus took the fallback
        and the worker picked what follows it before the fallback was
        recorded, so it could pick the fallback's own predecessor."""
        player = _player()
        fallback, following, pick = player._sim.entries[1:4]
        find = MagicMock(return_value=pick)
        player._sim.find_next_for_path = find  # type: ignore[method-assign]

        player._skip_past_render(_rendered(fallback, following))
        player._next_rendered()

        assert player._pending_previous is following
        recent = list(find.call_args.kwargs["recently_played"])
        assert recent[-2:] == [fallback.path, following.path]

    def test_rewind_keeps_the_track_before_the_rerendered_one(self) -> None:
        player = _player()
        before, track, after = player._sim.entries[:3]
        player._rewind_render_cursor(_rendered(track, after, previous_entry=before))
        assert (player._pending_entry, player._pending_previous) == (track, before)

    def test_a_render_remembers_the_track_before_it(self) -> None:
        player = _player()
        before, track = player._sim.entries[:2]
        player._set_render_cursor(track, 0, "similarity", previous=before)
        rendered = player._next_rendered()
        assert rendered is not None and rendered.previous_entry is before


# ---------------------------------------------------------------------------
# A set stop racing a track start
# ---------------------------------------------------------------------------


class TestSetStopRacingATrackStart:
    def _taken_queue_pick(self, player: Player) -> tuple[RenderedTrack, IndexEntry, IndexEntry]:
        queued, later = player._sim.entries[2:4]
        player._state.queue.extend([queued, later])
        track = _rendered(queued, later, from_queue=True, set_generation=player._set_generation)
        player._render_ahead._ready = track
        assert player._take_render() is track
        assert player._state.queue == [later]
        return track, queued, later

    def test_a_queue_pick_whose_set_stopped_goes_back_to_the_queue(self) -> None:
        player = _player(stream_mode=True)
        track, queued, later = self._taken_queue_pick(player)
        player.end_set()  # the idle grace stops the set before the start
        player._on_track_start(track)
        assert player._state.queue == [queued, later]
        assert player._state.current_track is None
        assert player._taken_queue_pick is None

    def test_a_queue_pick_that_starts_stays_out_of_the_queue(self) -> None:
        player = _player(stream_mode=True)
        track, queued, later = self._taken_queue_pick(player)
        player._on_track_start(track)
        assert player._state.queue == [later]
        assert player._state.current_track is queued
        assert player._taken_queue_pick is None  # its audio is not kept

    def test_a_pick_the_listener_had_removed_is_not_given_back(self) -> None:
        player = _player(stream_mode=True)
        queued = player._sim.entries[2]
        track = _rendered(queued, from_queue=True, set_generation=player._set_generation)
        player._render_ahead._ready = track
        player._take_render()  # already removed from the queue: nothing to commit
        player.end_set()
        player._on_track_start(track)
        assert player._state.queue == []


# ---------------------------------------------------------------------------
# All history mutation and copying under one lock
# ---------------------------------------------------------------------------


class TestHistoryLock:
    def test_pick_lock_is_the_state_history_lock(self) -> None:
        player = _player()
        assert player._pick_lock is player._state.history_lock

    def test_resizing_the_windows_waits_for_a_history_copy(self) -> None:
        player = _player()
        state = player._state
        done = threading.Event()

        def resize() -> None:
            state.resize_repeat_windows(3, 1)
            done.set()

        with player._pick_lock:
            worker = threading.Thread(target=resize)
            worker.start()
            assert not done.wait(0.05)  # blocked: a pick is copying the history
            assert state.no_repeat_window != 3
        assert done.wait(WAIT)
        worker.join(WAIT)
        assert state.recently_played.maxlen == 3

    def test_repick_blacklist_is_appended_under_the_history_lock(self) -> None:
        from autodj._bridge import PlayerBridge

        player = _player(dry_run=True)
        bridge = PlayerBridge(player=player, sim=player._sim)
        player._state.current_track = player._sim.entries[0]
        bad = player._sim.entries[3].path
        worker = threading.Thread(target=bridge.repick_next, args=(bad,))
        with player._pick_lock:
            worker.start()
            worker.join(0.05)
            assert bad not in player._state.recently_played
        worker.join(WAIT)
        assert bad in player._state.recently_played

    def test_browser_advance_records_under_the_history_lock(self) -> None:
        from autodj._bridge import PlayerBridge

        player = _player(dry_run=True)
        player.analyse_track_in_background = MagicMock()  # type: ignore[method-assign]
        bridge = PlayerBridge(player=player, sim=player._sim)
        seed, nxt = player._sim.entries[:2]
        player._state.current_track, player._state.next_track = seed, nxt
        worker = threading.Thread(target=bridge.advance_now)
        with player._pick_lock:
            worker.start()
            worker.join(0.05)
            assert player._state.current_track is nxt
            assert nxt.path not in player._state.recently_played
        worker.join(WAIT)
        assert nxt.path in player._state.recently_played


# ---------------------------------------------------------------------------
# Shutdown waits for the render-ahead worker
# ---------------------------------------------------------------------------


class TestShutdownWaitsForTheRenderWorker:
    @staticmethod
    def _rendering_player() -> tuple[Player, threading.Event]:
        """A player whose render-ahead worker is stuck in a render until released."""
        player = _player()
        rendering, release = threading.Event(), threading.Event()

        def slow_render() -> RenderedTrack | None:
            rendering.set()
            release.wait(WAIT)
            return None

        player._next_rendered = slow_render  # type: ignore[method-assign]
        player._render_ahead.start()
        assert rendering.wait(WAIT)
        return player, release

    def test_background_wait_includes_a_render_in_flight(self) -> None:
        player, release = self._rendering_player()
        try:
            assert player.wait_for_background_analysis(timeout=0.05) is False
        finally:
            release.set()
        assert player.wait_for_background_analysis(timeout=WAIT) is True
        assert player._render_ahead._thread is None

    def test_quiesce_waits_for_the_render_worker_before_the_cache_closes(self) -> None:
        from autodj._bridge import PlayerBridge
        from autodj.server import _quiesce_cache_writers

        player, release = self._rendering_player()
        bridge = PlayerBridge(player=player, sim=player._sim)
        try:
            assert _quiesce_cache_writers(bridge, None, 0.05) is False
        finally:
            release.set()
        assert _quiesce_cache_writers(bridge, None, WAIT) is True


# ---------------------------------------------------------------------------
# Opening the DJ-meta cache
# ---------------------------------------------------------------------------


class TestEnsureDjCache:
    def _player(self) -> Player:
        player = _player()
        player._cfg.index.active_dir = Path("index")
        return player

    def test_a_caller_arriving_while_the_cache_opens_waits_for_it(self) -> None:
        player = self._player()
        cache = MagicMock()
        opening, release = threading.Event(), threading.Event()

        def slow_get_cache(*_args, **_kwargs):
            opening.set()
            release.wait(WAIT)
            return cache

        seen: list[object] = []
        with patch("autodj.dj_meta.get_cache", side_effect=slow_get_cache):
            first = threading.Thread(target=player._ensure_dj_cache)
            first.start()
            assert opening.wait(WAIT)
            second = threading.Thread(
                target=lambda: (player._ensure_dj_cache(), seen.append(player._dj_cache))
            )
            second.start()
            second.join(0.05)
            assert seen == []  # waiting, not carrying on without a cache
            release.set()
            first.join(WAIT)
            second.join(WAIT)
        assert seen == [cache]
        assert player._dj_cache_initialised is True

    def test_an_sqlite_error_is_caught_and_retried_later(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        player = self._player()
        cache = MagicMock()
        failing = patch(
            "autodj.dj_meta.get_cache", side_effect=sqlite3.OperationalError("database is locked")
        )
        with caplog.at_level(logging.DEBUG, logger="autodj.player"), failing as get_cache:
            player._ensure_dj_cache()
            player._ensure_dj_cache()  # too soon: not tried again
            assert get_cache.call_count == 1
            player._dj_cache_retry_at = 0.0001  # the retry delay has passed
            player._ensure_dj_cache()
            assert get_cache.call_count == 2
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "DJ cache unavailable" in r.message
        ]
        assert len(warnings) == 1  # logged once, not on every retry
        assert player._dj_cache is None and player._dj_cache_initialised is False
        player._dj_cache_retry_at = 0.0001
        with patch("autodj.dj_meta.get_cache", return_value=cache):
            player._ensure_dj_cache()
        assert player._dj_cache is cache and player._dj_cache_initialised is True


# ---------------------------------------------------------------------------
# Browser-mode startup racing an advance
# ---------------------------------------------------------------------------


class TestHeadlessStartupRacingAnAdvance:
    def test_an_advance_during_the_first_pick_keeps_its_state(self) -> None:
        from autodj._bridge import PlayerBridge

        player = _player(dry_run=True)
        player.analyse_track_in_background = MagicMock()  # type: ignore[method-assign]
        seed, advanced_to = player._sim.entries[:2]
        seeds_next = make_entry(77)  # what the slow startup pick returns
        advanced_to.length = 240.0
        player._record_seed(seed)
        player._state.should_stop = True  # leave the park loop at once
        picking, release = threading.Event(), threading.Event()
        real_choose = player._choose_next

        def choose(current: IndexEntry, context: object):
            if current is seed and threading.current_thread() is not threading.main_thread():
                picking.set()
                release.wait(WAIT)
                return seeds_next, "similarity"
            return real_choose(current, context)  # type: ignore[arg-type]

        player._choose_next = choose  # type: ignore[method-assign]
        startup = threading.Thread(target=player._run_headless, args=(seed,))
        startup.start()
        assert picking.wait(WAIT)
        player._state.next_track = advanced_to
        PlayerBridge(player=player, sim=player._sim).advance_now()
        after_advance = player._state.next_track
        release.set()
        startup.join(WAIT)

        assert player._state.current_track is advanced_to
        assert player._state.next_track is after_advance
        assert after_advance is not seeds_next
        assert player._playback_len == int(240.0 * 44_100)

    def test_the_first_pick_fills_next_track_when_nothing_raced_it(self) -> None:
        player = _player(dry_run=True)
        player.analyse_track_in_background = MagicMock()  # type: ignore[method-assign]
        seed = player._sim.entries[0]
        player._record_seed(seed)
        player._state.should_stop = True
        player._run_headless(seed)
        assert player._state.next_track is not None
        assert player._playback_len == int(seed.length * 44_100)

    def test_a_failed_first_pick_leaves_next_track_empty(self) -> None:
        player = _player(dry_run=True)
        player.analyse_track_in_background = MagicMock()  # type: ignore[method-assign]
        seed = player._sim.entries[0]
        player._sim = SimilarityIndex.empty()
        player._state.should_stop = True
        player._run_headless(seed)
        assert player._state.current_track is seed
        assert player._state.next_track is None
