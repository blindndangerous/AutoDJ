"""Server-side liner triggers, picking and decoding."""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from autodj.liner_scheduler import LinerDecodeError, LinerScheduler, decode_liner


def _cfg(**over):
    base = {
        "liners_enabled": True,
        "liners_every_n_songs": 2,
        "liners_every_minutes": None,
        "liners_random_min_minutes": None,
        "liners_random_max_minutes": None,
        "liners_pick_mode": "sequential",
        "liners_duck_db": -10.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _folder(tmp_path: Path) -> Path:
    for name in ("a.wav", "b.wav"):
        sf.write(tmp_path / name, np.zeros(441, np.float32), 44100)
    return tmp_path


def test_decode_wav_is_stereo(tmp_path: Path) -> None:
    sf.write(tmp_path / "x.wav", np.full(441, 0.25, np.float32), 22050)
    out = decode_liner(tmp_path, "x.wav")
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(len(out) - 882) <= 2


def test_decode_falls_back_to_ffmpeg(tmp_path: Path) -> None:
    (tmp_path / "x.m4a").write_bytes(b"not really m4a")
    pcm = np.full((10, 2), 0.5, np.float32).tobytes()
    runner = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=pcm, stderr=b""))
    out = decode_liner(tmp_path, "x.m4a", runner=runner)
    assert out.shape == (10, 2)
    args = runner.call_args.args[0]
    assert args[0] == "ffmpeg" and "pipe:0" in args


def test_undecodable_liner_raises(tmp_path: Path) -> None:
    (tmp_path / "x.m4a").write_bytes(b"junk")
    runner = MagicMock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad"))
    with pytest.raises(LinerDecodeError):
        decode_liner(tmp_path, "x.m4a", runner=runner)


def test_every_n_songs_fires_and_rotates_through_the_library(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus, clock=lambda: 0.0, rng=random.Random(1))
    sched.on_track_start()
    assert bus.play_liner.call_count == 0
    sched.on_track_start()
    assert bus.play_liner.call_count == 1
    assert bus.play_liner.call_args.kwargs["duck_db"] == -10.0

    # The automatic fire above already played "a.wav" (sequential mode
    # starts at cursor 0). Rescanning the folder from disk on every
    # automatic pick must not reset that rotation back to the start —
    # successive fires should keep walking the library: b.wav, a.wav, ...
    assert sched.fire() == "b.wav"
    assert sched.fire() == "a.wav"
    assert bus.play_liner.call_count == 3


def test_registered_liner_provider_rotates_duplicate_basenames_by_id(tmp_path: Path) -> None:
    first = tmp_path / "one" / "same.wav"
    second = tmp_path / "two" / "same.wav"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    decoded: list[Path] = []
    bus = MagicMock()
    scheduler = LinerScheduler(
        _cfg(liners_pick_mode="sequential"),
        tmp_path / "flat",
        bus,
        registered_liners=lambda: [("record-one", first), ("record-two", second)],
        path_decoder=lambda path: decoded.append(path) or np.zeros((100, 2), np.float32),
    )

    assert scheduler.fire() == "record-one"
    assert scheduler.fire() == "record-two"
    assert scheduler.fire("record-one") == "record-one"
    assert decoded == [first, second, first]
    assert bus.play_liner.call_count == 3


def test_saved_points_plans_one_liner_until_heard_or_released(tmp_path: Path) -> None:
    first = tmp_path / "one" / "same.wav"
    second = tmp_path / "two" / "same.wav"
    first.parent.mkdir()
    second.parent.mkdir()
    first.touch()
    second.touch()
    bus = MagicMock()
    provider_calls = [0]

    def registered():
        provider_calls[0] += 1
        return [("id-one", first), ("id-two", second)]

    scheduler = LinerScheduler(
        _cfg(transition_mode="saved_points", liners_talk_up=True),
        tmp_path / "flat",
        bus,
        registered_liners=registered,
        path_decoder=lambda _path: np.zeros((8, 2), np.float32),
        vocal_start=lambda _entry: 1.0,
        can_fire=lambda: False,
    )

    # Legacy trigger callbacks only count in planner mode.
    scheduler.on_track_start()
    scheduler.tick()
    bus.play_liner.assert_not_called()
    assert scheduler.plan_for_handoff("handoff-1", tracks_ahead=0, seconds_ahead=0) is None
    planned = scheduler.plan_for_handoff("handoff-1", tracks_ahead=2, seconds_ahead=0)
    assert planned == "id-one"
    assert scheduler.plan_for_handoff("handoff-1", tracks_ahead=2, seconds_ahead=0) == planned
    assert scheduler.plan_for_handoff("handoff-2", tracks_ahead=2, seconds_ahead=0) is None
    bus.play_liner.assert_not_called()

    assert not scheduler.mark_planned_fired("wrong-key")
    calls_before_commit = provider_calls[0]
    assert scheduler.mark_planned_fired("handoff-1")
    assert provider_calls[0] == calls_before_commit
    assert not scheduler.mark_planned_fired("handoff-1")
    assert scheduler.plan_for_handoff("handoff-2", tracks_ahead=2, seconds_ahead=0) == "id-two"


def test_releasing_discarded_plan_keeps_sequential_pick(tmp_path: Path) -> None:
    first = tmp_path / "a" / "one.wav"
    second = tmp_path / "b" / "two.wav"
    first.parent.mkdir()
    second.parent.mkdir()
    first.touch()
    second.touch()
    scheduler = LinerScheduler(
        _cfg(transition_mode="saved_points", liners_talk_up=True),
        tmp_path / "flat",
        MagicMock(),
        registered_liners=lambda: [("one", first), ("two", second)],
        path_decoder=lambda _path: np.zeros((8, 2), np.float32),
        vocal_start=lambda _entry: 1.0,
    )
    assert scheduler.plan_for_handoff("old", 2, 0) == "one"
    assert not scheduler.release_plan("other")
    assert scheduler.release_plan("old")
    assert scheduler.plan_for_handoff("new", 2, 0) == "one"


def test_clock_mode_disables_automatic_trigger_but_keeps_manual_fire(tmp_path: Path) -> None:
    bus = MagicMock()
    scheduler = LinerScheduler(
        _cfg(pick_mode="clock", liners_every_n_songs=1), _folder(tmp_path), bus
    )
    scheduler.on_track_start()
    scheduler.tick()
    bus.play_liner.assert_not_called()
    assert scheduler.fire("a.wav") == "a.wav"
    bus.play_liner.assert_called_once()


def test_timed_trigger_fires_on_tick(tmp_path: Path) -> None:
    now = [0.0]
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_every_n_songs=None, liners_every_minutes=1.0),
        _folder(tmp_path),
        bus,
        clock=lambda: now[0],
    )
    sched.tick()
    assert bus.play_liner.call_count == 0
    now[0] = 61.0
    sched.tick()
    assert bus.play_liner.call_count == 1


def test_automatic_triggers_wait_until_the_bus_can_play(tmp_path: Path) -> None:
    now = [0.0]
    playing = [False]
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_every_n_songs=1, liners_every_minutes=1.0),
        _folder(tmp_path),
        bus,
        clock=lambda: now[0],
        can_fire=lambda: playing[0],
    )
    now[0] = 120.0
    sched.tick()
    sched.on_track_start()
    bus.play_liner.assert_not_called()
    # A test fire by hand is not an automatic trigger: the route decides.
    assert sched.fire("a.wav") == "a.wav"
    bus.play_liner.reset_mock()
    now[0] = 300.0
    playing[0] = True
    sched.tick()
    bus.play_liner.assert_called_once()


def test_bad_liner_is_skipped_and_music_carries_on(tmp_path: Path) -> None:
    bus = MagicMock()

    def broken(_root: Path, _name: str) -> np.ndarray:
        raise LinerDecodeError("corrupt")

    sched = LinerScheduler(_cfg(liners_every_n_songs=1), _folder(tmp_path), bus, decoder=broken)
    sched.on_track_start()
    bus.play_liner.assert_not_called()


def test_disabled_never_fires(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_enabled=False, liners_every_n_songs=1), _folder(tmp_path), bus
    )
    sched.on_track_start()
    sched.tick()
    bus.play_liner.assert_not_called()


def test_fire_by_name_plays_that_file(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus)
    assert sched.fire("b.wav") == "b.wav"
    assert bus.play_liner.call_count == 1


def test_fire_with_no_files_returns_none(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), tmp_path, bus)
    assert sched.fire() is None
    bus.play_liner.assert_not_called()


def test_nested_liner_name_is_skipped_gracefully(tmp_path: Path) -> None:
    """A clip in a subfolder cannot open under the plain root;

    firing it by name must be swallowed rather than raising into the mix bus.
    """
    bus = MagicMock()
    sub = tmp_path / "sub"
    sub.mkdir()
    sf.write(sub / "jingle.wav", np.zeros(441, np.float32), 44100)
    sched = LinerScheduler(_cfg(), tmp_path, bus)
    assert sched.fire("jingle.wav") is None
    bus.play_liner.assert_not_called()


def test_random_trigger_does_not_fire_before_first_liner(tmp_path: Path) -> None:
    """Matches ``liners.js``'s ``state.randomTarget`` starting ``null``:

    a random-only config must not auto-fire until a liner has actually
    played once (by another trigger, or the test route), even long after
    the configured random window would otherwise have elapsed.
    """
    now = [0.0]
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(
            liners_every_n_songs=None,
            liners_random_min_minutes=1.0,
            liners_random_max_minutes=1.0,
        ),
        _folder(tmp_path),
        bus,
        clock=lambda: now[0],
        rng=random.Random(1),
    )
    now[0] = 3600.0
    sched.tick()
    bus.play_liner.assert_not_called()

    # Once a liner has fired (here, an explicit test-route style fire),
    # the random window arms and later ticks can fire from it.
    sched.fire("a.wav")
    bus.play_liner.reset_mock()
    now[0] += 61.0
    sched.tick()
    assert bus.play_liner.call_count == 1


def test_concurrent_fires_are_mutually_exclusive(tmp_path: Path) -> None:
    """Two threads racing on_track_start()/fire() for one due window must

    not both decode + play — the work lock serializes the whole
    check-and-fire sequence, including the decode.
    """
    bus = MagicMock()
    release = threading.Event()
    probe_lock = threading.Lock()
    concurrent = [0]
    max_concurrent = [0]

    def decoder(_root: Path, _name: str) -> np.ndarray:
        with probe_lock:
            concurrent[0] += 1
            max_concurrent[0] = max(max_concurrent[0], concurrent[0])
        release.wait(timeout=2)
        with probe_lock:
            concurrent[0] -= 1
        return np.zeros((4, 2), np.float32)

    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus, decoder=decoder)

    t1 = threading.Thread(target=sched.fire, args=("a.wav",))
    t2 = threading.Thread(target=sched.fire, args=("b.wav",))
    t1.start()
    t2.start()
    # Give t1 time to enter the decoder while holding the work lock;
    # t2 must be blocked waiting for that lock, never inside the decoder
    # at the same time.
    time.sleep(0.1)
    assert max_concurrent[0] == 1
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert max_concurrent[0] == 1
    assert bus.play_liner.call_count == 2


def test_carts_decode_like_fire_and_play_without_resetting_the_triggers(tmp_path: Path) -> None:
    clip = tmp_path / "library" / "id.wav"
    clip.parent.mkdir()
    clip.touch()
    bus = MagicMock()
    trimmed = np.zeros((300, 2), np.float32)
    scheduler = LinerScheduler(
        _cfg(liners_every_n_songs=2),
        _folder(tmp_path),
        bus,
        registered_liners=lambda: [("record-id", clip)],
        path_decoder=lambda path: trimmed,
    )
    assert scheduler.decode("record-id") is trimmed
    assert len(scheduler.decode("a.wav")) == 441
    with pytest.raises(LinerDecodeError):
        scheduler.decode("missing.wav")

    scheduler.on_track_start()
    scheduler.play_cart(trimmed, float("-inf"))
    bus.play_liner.assert_called_once_with(trimmed, duck_db=float("-inf"))
    scheduler.stop_cart()
    bus.stop_liner.assert_called_once_with()
    # A cart is not a liner: the every-2-songs count carries on.
    scheduler.on_track_start()
    assert bus.play_liner.call_count == 2


def _planner(tmp_path: Path, decoder) -> tuple[LinerScheduler, MagicMock]:
    """A saved-points scheduler with a reservation for "handoff" and a slow folder decoder."""
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(transition_mode="saved_points", liners_talk_up=True, liners_every_n_songs=1),
        _folder(tmp_path),
        bus,
        decoder=decoder,
        vocal_start=lambda _entry: 1.0,
    )
    assert sched.plan_for_handoff("handoff", tracks_ahead=1, seconds_ahead=0) == "a.wav"
    return sched, bus


def test_the_bus_thread_never_waits_for_a_liner_decode(tmp_path: Path) -> None:
    """mark_planned_fired runs on the mix bus thread; a decode once held its lock for seconds."""
    decoding, release = threading.Event(), threading.Event()

    def slow(_root: Path, _name: str) -> np.ndarray:
        decoding.set()
        release.wait(timeout=10)
        return np.zeros((4, 2), np.float32)

    sched, bus = _planner(tmp_path, slow)
    firing = threading.Thread(target=sched.fire, args=("b.wav",))
    firing.start()
    try:
        assert decoding.wait(timeout=5)
        started = time.perf_counter()
        assert sched.mark_planned_fired("handoff")  # committed, not left for later
        assert time.perf_counter() - started < 0.5
        sched.play_cart(np.zeros((4, 2), np.float32), duck_db=-6.0)  # carts do not wait either
        assert bus.play_liner.call_count == 1
    finally:
        release.set()
        firing.join(timeout=5)
    assert bus.play_liner.call_count == 2


def test_a_reservation_heard_while_the_state_is_busy_is_committed_by_the_holder(
    tmp_path: Path,
) -> None:
    now = [100.0]
    sched, _bus = _planner(tmp_path, lambda _root, _name: np.zeros((4, 2), np.float32))
    sched._clock = lambda: now[0]
    result: list[bool] = []
    with sched._state():
        heard = threading.Thread(target=lambda: result.append(sched.mark_planned_fired("handoff")))
        heard.start()
        heard.join(timeout=5)
        assert result == [False]  # did not wait
        assert sched._planned_key == "handoff"
        now[0] = 160.0
    assert sched._planned_key is None
    assert sched._last_fire == 100.0  # timed from when the bus heard it
    assert sched._library.cursor == 1


def test_a_liner_that_decoded_while_another_aired_is_dropped(tmp_path: Path) -> None:
    """The trigger is checked again after the decode: a time check aired meanwhile."""
    decoding, release = threading.Event(), threading.Event()

    def slow(_root: Path, _name: str) -> np.ndarray:
        decoding.set()
        release.wait(timeout=10)
        return np.zeros((4, 2), np.float32)

    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_every_n_songs=None, liners_every_minutes=1),
        _folder(tmp_path),
        bus,
        clock=lambda: 120.0,
        decoder=slow,
    )
    sched._last_fire = 0.0
    ticking = threading.Thread(target=sched.tick)
    ticking.start()
    assert decoding.wait(timeout=5)
    sched.play_audio(np.ones((4, 2), np.float32))  # does not wait for the decode
    release.set()
    ticking.join(timeout=5)
    assert bus.play_liner.call_count == 1
    np.testing.assert_array_equal(bus.play_liner.call_args.args[0], np.ones((4, 2)))
