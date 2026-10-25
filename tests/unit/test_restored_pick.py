"""A song picked before a restart starts the set at once; its successor follows (#584)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from datetime import time as clock_time
from types import SimpleNamespace

import numpy as np
import pytest

from autodj import restored_pick
from autodj.categories import Category
from autodj.mixbus import BusEvents, MixBus
from tests.unit._break_station import LEVELS, make_station
from tests.unit.test_live_playout import _UnconsumedDeck


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _restored_station(tmp_path, monkeypatch):
    player, entries, _points, _desk = make_station(tmp_path, stream_mode=True)
    decks = {
        entry.path: _UnconsumedDeck(round(entry.length * 44_100), level)
        for entry, level in zip(entries, LEVELS, strict=True)
    }
    monkeypatch.setattr(
        player,
        "_live_file",
        lambda entry, **_kwargs: _UnconsumedDeck(len(decks[entry.path]), decks[entry.path].value),
    )
    monkeypatch.setattr(player, "_live_source_is_current", lambda *_args: True)
    monkeypatch.setattr(
        "autodj.live_transitions._read_window",
        lambda _player, _entry, deck, first, last: np.full(
            (max(0, last - first), 2), deck.value, np.float32
        ),
    )
    player.bus = MixBus(
        BusEvents(player._on_track_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    player._upcoming_checked = True
    return player, entries


def test_a_restored_song_starts_alone_and_its_successor_is_spliced_in(tmp_path, monkeypatch):
    """ADR-0004-16. The first render picks nothing; the next song mixes in as usual."""
    player, (a, b, c, _d) = _restored_station(tmp_path, monkeypatch)
    player._state.queue[:] = [b, c]
    picks = []
    choose = player._choose_station_next
    monkeypatch.setattr(
        player,
        "_choose_station_next",
        lambda current, *args, **kwargs: picks.append(current) or choose(current, *args, **kwargs),
    )
    player._restored_first = a
    player.begin_set(a, "seed")
    player._playout.start()
    player.bus.start_set()
    try:
        assert player._playout.wait_ready(2)
        first = player._playout.ready()
        assert first.entry is a and first.next_entry is None
        assert picks == []  # nothing waited for a pick before the first audio
        assert np.mean(player.bus.render_block()[:, 0]) > 0.8
        assert _wait_for(
            lambda: player._playing_render is not first and player._playing_render.next_entry is b
        )
        assert picks == [a]
        # The startup scan waits until the successor is picked, not just the first audio.
        assert player.first_song_started.is_set()
        spliced = player._playing_render
        assert spliced.overlap_frames > 0
        mixed = silent = 0
        for _ in range(3000):
            block = player.bus.render_block()
            level = float(np.mean(block[:, 0]))
            current = player.bus.current_render()
            if current is not None and current.entry is b and level > 0:
                break
            mixed += 0.3 < level < 0.95  # A fading out under B: the crossfade
            silent += level == 0
            time.sleep(0.001)
        assert player._playing_render.entry is b
        assert mixed > 0
        # The block in which the bus promotes B, as in an ordinary handoff here.
        assert silent <= 1
    finally:
        player._playout.stop(timeout=2)
        player.end_set()


def test_a_successor_too_late_for_the_restored_song_hands_over_to_a_fresh_pick(
    tmp_path, monkeypatch
):
    """ADR-0004-16. When the song ends first, the usual start pick plays the next song."""
    player, (a, b, c, _d) = _restored_station(tmp_path, monkeypatch)
    player._state.queue[:] = [b, c]
    held, release = threading.Event(), threading.Event()
    prepare = player._next_rendered_serial

    def slow_successor():
        if player._pending_entry is a and player.bus.current_render() is not None:
            held.set()
            assert release.wait(5)
        return prepare()

    monkeypatch.setattr(player, "_next_rendered_serial", slow_successor)
    player._restored_first = a
    player.begin_set(a, "seed")
    player._playout.start()
    player.bus.start_set()
    try:
        assert player._playout.wait_ready(2)
        player.bus.render_block()
        assert held.wait(2)
        first = player._playing_render
        while player.bus.current_render() is first:
            player.bus.render_block()
        release.set()
        silent = 0

        def playing_again():
            nonlocal silent
            if np.any(player.bus.render_block()):
                return True
            silent += 1
            return False

        assert _wait_for(playing_again)
        fresh = player._playing_render
        assert fresh.entry is not a
        # A few blocks of the bus, while the fresh pick renders: not a long silence.
        assert silent * player.bus.BLOCK / 44_100 < 1.0

        # And the set goes on past that song.
        followed = []
        for n in range(4000):
            player.bus.render_block()
            current = player.bus.current_render()
            if current is not None and current is not fresh and current.entry is not fresh.entry:
                followed.append(current.entry)
                break
            if n % 20 == 0:
                time.sleep(0.005)
        assert followed
    finally:
        release.set()
        player._playout.stop(timeout=2)
        player.end_set()


# ---------------------------------------------------------------------------
# Which saved song may start the set
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 25, 9, 0).timestamp()


class _Ledger:
    def __init__(self, saved):
        self.settings = {restored_pick.SETTING: saved}
        self.played: dict[str, float] = {}

    def get_setting(self, name, default=None):
        return self.settings.get(name, default)

    def remember_setting(self, name, value):
        self.settings[name] = value

    def last_played(self):
        return dict(self.played)


def _guarded(tmp_path, **saved_fields):
    song = tmp_path / "song.flac"
    song.write_bytes(b"music")
    entry = SimpleNamespace(path=str(song), is_silent=False, display_name="Artist - Song")
    saved = {
        "path": entry.path,
        "picked_at": NOW - 120,
        "starts_at": NOW - 10,
        "clock": "main",
        "clock_hash": "wheel",
        "cursor": 4,
        "boundary": NOW + 1800,
        "step": {"number": 3},
        "show": False,
        **saved_fields,
    }
    context = SimpleNamespace(
        recent=deque(), protected=frozenset(), repeat=lambda _e: True, allows=lambda _e: True
    )
    player = SimpleNamespace(
        _library=SimpleNamespace(
            entry_for_path=lambda path: entry if path == entry.path else None,
            kind_for_path=lambda _path: "music",
        ),
        _clock={"name": "main", "cursor": 2},
        _clock_revision=7,
        _cfg=SimpleNamespace(playback=SimpleNamespace(pick_mode="clock")),
        live_mixing=True,
        _ledger=_Ledger(saved),
        _clock_fingerprint=lambda: "wheel",
        _clock_name=lambda: "main",
        _station_now=lambda at: datetime.fromtimestamp(at),
        _category_index=lambda: SimpleNamespace(categories=lambda: ()),
        _clock_step_hours=(),
        _pick_context=lambda dayparts: context,
        show_holds_air=lambda: False,
    )
    return player, entry, context


def test_a_fresh_clock_pick_starts_the_set_once(tmp_path, caplog):
    """ADR-0004-16. The saved song starts the set, its place in the wheel after it."""
    player, entry, _context = _guarded(tmp_path)
    with caplog.at_level(logging.INFO, logger="autodj.restored_pick"):
        assert restored_pick.restore(player, NOW) is entry
    assert "Restart: starting with Artist - Song" in caplog.text
    assert player._seed_clock_cursor == 4
    assert player._seed_clock_step == {"number": 3}
    assert player._restored_first is entry
    assert player._ledger.settings[restored_pick.SETTING] is None  # used once
    assert restored_pick.restore(player, NOW) is None


def _saved(player):
    return player._ledger.settings[restored_pick.SETTING]


def _morning_category(player):
    morning = Category(
        id="am",
        name="Morning",
        source="query",
        query="genre:rock",
        hours_from=clock_time(6),
        hours_until=clock_time(9),
    )
    player._category_index = lambda: SimpleNamespace(categories=lambda: (morning,))


CHANGES = {
    "nothing usable saved": (
        lambda p, e, c, t: p._ledger.settings.update({restored_pick.SETTING: {"x": 1}}),
        "none was saved",
    ),
    "too old": (lambda p, e, c, t: _saved(p).update(picked_at=NOW - 3600), "30 minutes"),
    "file gone": (lambda p, e, c, t: (t / "song.flac").unlink(), "no longer in the library"),
    "silent": (lambda p, e, c, t: setattr(e, "is_silent", True), "no longer in the library"),
    "played since": (lambda p, e, c, t: p._ledger.played.update({e.path: NOW - 60}), "played"),
    "wheel edited": (
        lambda p, e, c, t: setattr(p, "_clock_fingerprint", lambda: "edited"),
        "clockwheel changed",
    ),
    "other wheel": (lambda p, e, c, t: p._clock.update(name="other"), "clockwheel changed"),
    "restart passed": (
        lambda p, e, c, t: _saved(p).update(boundary=NOW - 1),
        "restart time has passed",
    ),
    "show before": (lambda p, e, c, t: _saved(p).update(show=True), "show is on the air"),
    "show now": (
        lambda p, e, c, t: setattr(p, "show_holds_air", lambda: True),
        "show is on the air",
    ),
    "hours changed": (lambda p, e, c, t: _morning_category(p), "opened or closed since"),
    "track rule": (lambda p, e, c, t: c.recent.append(e.path), "track rule"),
    "repeat rules": (lambda p, e, c, t: setattr(c, "repeat", lambda _e: False), "repeat rules"),
    "dmca": (lambda p, e, c, t: setattr(c, "allows", lambda _e: False), "DMCA"),
    "dry run": (lambda p, e, c, t: setattr(p, "live_mixing", False), None),
    "not a clockwheel": (
        lambda p, e, c, t: setattr(p._cfg.playback, "pick_mode", "random"),
        None,
    ),
}


@pytest.mark.parametrize("case", list(CHANGES))
def test_a_saved_song_that_no_longer_fits_falls_back_to_the_usual_pick(tmp_path, caplog, case):
    """ADR-0004-16. Any failed check leaves the start to the usual pick, and says why."""
    change, reason = CHANGES[case]
    player, entry, context = _guarded(tmp_path)
    change(player, entry, context, tmp_path)
    with caplog.at_level(logging.INFO, logger="autodj.restored_pick"):
        assert restored_pick.restore(player, NOW) is None
    assert getattr(player, "_restored_first", None) is None
    if reason is None:
        assert "Restart:" not in caplog.text  # a dry run or another pick mode: not asked
    else:
        assert "does not start the set" in caplog.text
        assert reason in caplog.text


def _render(**fields):
    values = {
        "next_entry": SimpleNamespace(path="/music/next.flac"),
        "next_pick_mode": "clock",
        "next_from_queue": False,
        "next_clock_name": "main",
        "next_clock_revision": 7,
        "next_clock_cursor": 5,
        "next_clock_boundary": NOW + 600,
        "next_clock_step": {"number": 4},
        "audio": np.zeros((44_100 * 10, 2), np.float32),
        "overlap_frames": 44_100 * 2,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def test_only_a_clockwheel_pick_is_kept_for_a_restart(tmp_path):
    """ADR-0004-16. A queued song (a request or a show) is never kept."""
    player, _entry, _context = _guarded(tmp_path)
    restored_pick.remember(player, _render(), NOW)
    kept = _saved(player)
    assert kept["path"] == "/music/next.flac" and kept["cursor"] == 5
    assert kept["starts_at"] == pytest.approx(NOW + 8)
    assert kept["clock_hash"] == "wheel" and kept["show"] is False
    restored_pick.remember(player, _render(next_pick_mode="queue", next_from_queue=True), NOW)
    assert _saved(player) is None
    restored_pick.remember(player, _render(next_clock_revision=6), NOW)
    assert _saved(player) is None
