"""Station timing through the real player, including queue reservations."""

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.config import PlaybackConfig
from autodj.mixbus import RenderedTrack
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._fakes import make_cfg_mock, make_entry, make_sim_index


@pytest.fixture
def station(monkeypatch):
    entries = [make_entry(i) for i in range(4)]
    for entry in entries:
        entry.length = 20.0
    a, b, voice, jingle = entries
    voice.length, jingle.length = 4.0, 8.8
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="saved_points",
        crossfade_seconds=1.5,
        loudness_processing="off",
        pick_mode="random",
    )
    player = Player(cfg, make_sim_index(2), dry_run=True)
    points = {
        a.path: {"start": 0, "outro": 8, "segue": 10, "end": 15},
        b.path: {"start": 0.25, "intro": 2.25, "segue": 18, "end": 20},
        voice.path: {"start": 0, "end": 4, "overlap_start": 0},
        jingle.path: {"start": 0, "end": 8.8, "overlap_start": 5.3},
    }
    audio = {}
    for entry, value in zip(entries, (1, 0.25, 0.5, 0.5), strict=True):
        array = np.zeros((round(entry.length * SR), 2), dtype=np.float32)
        array[:, 0 if entry in (a, b) else 1] = value
        audio[entry.path] = array
    monkeypatch.setattr(player, "_load_playout_audio", lambda entry: audio[entry.path].copy())
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(
            points=lambda path: points[path],
            record_for_path=lambda path: {"allow_outro": path == voice.path},
        ),
        entries_snapshot=lambda kind="music": tuple(
            entries[2:] if kind == "liner" else entries[:2]
        ),
        ledger=SimpleNamespace(
            remember_play=MagicMock(),
            remember_setting=MagicMock(),
            last_played=lambda: {},
        ),
    )
    return player, entries, points


def test_voice_fits_outro_intro_with_one_fade(station):
    player, (a, b, voice, _), _points = station
    result = player._render_track(a, b, 0, liners=((voice, True),))
    assert result is not None
    assert result.liner_events == ((8 * SR, voice, True),)
    assert result.next_entry_offset == round(0.25 * SR)
    assert result.next_start_offset == round(2.25 * SR)
    assert len(result.audio) == 12 * SR
    assert result.audio[round(9.4 * SR), 0] == 1
    assert result.audio[10 * SR, 0] > 1  # full incoming level, no pre-master clipping
    assert result.audio[11 * SR, 0] == 0.25
    assert result.audio[11 * SR, 1] == 0.5


def test_cold_intro_waits_for_complete_voice(station):
    player, (a, b, voice, _), points = station
    points[b.path]["intro"] = 0.25
    result = player._render_track(a, b, 0, liners=((voice, False),))
    assert len(result.audio) == 12 * SR
    assert result.next_start_offset == round(0.25 * SR)
    assert result.audio[11 * SR, 0] == 0  # no stretched music fade
    assert result.audio[11 * SR, 1] == 0.5


@pytest.mark.parametrize("standalone", [False, True])
def test_liner_segue_starts_cold_music_while_tail_keeps_playing(station, standalone):
    player, (a, b, voice, _), points = station
    points[b.path]["intro"] = 0.25
    points[voice.path].update(start=0.5, segue=2.5)
    result = player._render_track(
        voice if standalone else a, b, 0, liners=() if standalone else ((voice, True),)
    )
    entry = 2 if standalone else 10
    if standalone:
        assert result.audio[round((entry - 0.01) * SR), 0] == 0
    else:
        assert result.liner_events == ((8 * SR, voice, True),)
    assert result.overlap_frames == round(1.5 * SR)
    assert len(result.audio) == round((entry + 1.5) * SR)
    assert result.audio[entry * SR, 0] >= 0.25
    assert result.audio[-1, 1] == 0.5
    assert result.next_start_offset == round(1.75 * SR)


def test_positive_release_keeps_entire_jingle_tail(station):
    player, (a, b, _, jingle), _points = station
    result = player._render_track(a, b, 0, liners=((jingle, False),))
    assert result.liner_events[0][0] == 10 * SR
    assert result.overlap_frames == round(3.5 * SR)
    assert result.next_start_offset == round(3.75 * SR)
    assert result.audio[round(15.3 * SR), 0] == 0.25
    assert result.audio[-1, 1] == 0.5


def test_retained_liner_tail_does_not_play_music_past_saved_end(station):
    player, (_, b, voice, _), points = station
    points[b.path].update(start=1.0, intro=1.0, end=2.0)
    points[voice.path].update(start=0.5, segue=2.5)

    result = player._render_track(voice, b, 0)

    assert result.next_entry_offset == SR
    assert result.next_start_offset == 2 * SR
    assert len(result.audio) == round(3.5 * SR)
    assert np.all(result.audio[2 * SR : 3 * SR, 0] == 0.25)
    assert np.all(result.audio[3 * SR :, 0] == 0)
    assert np.all(result.audio[:, 1] == 0.5)


def test_pending_liner_and_music_are_reserved_without_leaving_queue(station):
    player, (a, b, voice, _), _points = station
    player._state.queue[:] = [voice, b]
    player._set_render_cursor(a, 0, "seed")
    result = player._next_rendered()
    assert result.next_entry is b
    assert result.next_reserved_liners == (voice,)
    assert player._state.queue == [voice, b]
    assert player._station_queue(b, (voice,)) == ([], None)
    assert not player._queue_pick_is_stale(result)
    player._on_track_start(result)
    player._on_position(8 * SR - 1)
    assert player._state.queue == [voice, b]
    player._on_position(8 * SR + 882)
    assert player._state.queue == [b]
    player._library.ledger.remember_play.assert_called_once_with(voice.path)


def test_saved_start_survives_next_rendered_metadata(station):
    player, (a, b, *_), points = station
    points[a.path]["start"] = 0.25
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed")
    result = player._next_rendered()
    assert result.start_offset == round(0.25 * SR)
    assert result.start_offset + len(result.audio) == 11 * SR


def test_clock_step_provenance_follows_each_rendered_track(station, monkeypatch):
    player, (a, b, voice, _), _points = station
    steps = iter(
        (
            {"number": 2, "step": {"type": "music", "selection": "random"}},
            {"number": 4, "step": {"type": "category", "selection": "least_recent"}},
        )
    )

    def choose(_current, _context):
        player._chosen_clock_step = next(steps)
        return b if player._pending_entry is a else voice, "clock", (), None, ""

    monkeypatch.setattr(player, "_choose_station_next", choose)
    monkeypatch.setattr(
        player,
        "_render_track",
        lambda current, upcoming, *_args, **_kwargs: RenderedTrack(
            current, np.zeros((10, 2), np.float32), upcoming, 0, "none"
        ),
    )
    first_step = {"number": 1, "step": {"type": "liner", "selection": "random"}}
    player._set_render_cursor(a, 0, "clock", clock_step=first_step)

    first = player._next_rendered()
    assert first.clock_step == first_step
    assert first.next_clock_step["number"] == 2
    player._start_playing(first)
    assert player._last_clock_step == first_step

    second = player._next_rendered()
    assert second.clock_step["number"] == 2
    assert second.next_clock_step["number"] == 4
    player._rewind_render_cursor(second)
    assert player._pending_clock_step == second.clock_step
    player._skip_past_render(second)
    assert player._pending_clock_step == second.next_clock_step


def test_queue_edit_revalidates_liner_reservation(station):
    player, (a, b, voice, jingle), _points = station
    player._state.queue[:] = [voice, b]
    player._set_render_cursor(a, 0, "seed")
    result = player._next_rendered()
    player._state.queue[0] = jingle
    assert player._queue_pick_is_stale(result)


@pytest.mark.parametrize("mode", ["smooth", "stereotool"])
def test_master_processing_does_not_stack_replaygain(station, mode):
    player, *_ = station
    player._cfg.playback.loudness_processing = mode
    player._cfg.replaygain.enabled = True
    audio = np.ones((10, 2), dtype=np.float32)
    assert player._apply_replaygain(audio, "missing.mp3") is audio


@pytest.mark.parametrize("mode", ["smooth", "stereotool"])
@pytest.mark.parametrize("kind", ["music", "liner"])
def test_master_processing_uses_originals_and_cached_measurements(
    station, mode, kind, tmp_path, monkeypatch
):
    player, (entry, *_), _points = station
    player._cfg.playback.loudness_processing = mode
    prepared = tmp_path / "reference.wav"
    prepared.write_bytes(b"reference")
    measurement = {"lufs": -20}
    player._library.catalog.record_for_path = lambda _path: {
        "prepared_path": str(prepared),
        "kind": kind,
        "loudness": measurement,
        "intro_lift_db": 4,
    }
    audio = np.ones((100, 2), dtype=np.float32)
    load = MagicMock(return_value=audio)
    measure = MagicMock()
    monkeypatch.setattr("autodj.processing.loudness_matches", lambda *args: True)
    monkeypatch.setattr("autodj.processing.analyze_loudness", measure)
    monkeypatch.setattr("autodj.processing.load_processed_audio", load)
    monkeypatch.setattr(player, "_lift_intro", MagicMock(side_effect=AssertionError("double lift")))
    assert Player._load_playout_audio(player, entry) is audio
    load.assert_called_once_with(Path(entry.path), measurement, kind, player._max_render_seconds())
    measure.assert_not_called()
    assert prepared.read_bytes() == b"reference"
    result = player._render_track(entry, None, 0)
    assert result.loudness_processing == mode


@pytest.mark.parametrize("record_exists", [True, False])
def test_missing_or_stale_measurement_is_measured_in_the_decode(
    station, record_exists, monkeypatch
):
    """A first play decodes and measures in one FFmpeg run, then caches the measurement."""
    player, (entry, *_), _points = station
    player._cfg.playback.loudness_processing = "smooth"
    player._library.catalog.record_for_path = lambda _path: (
        {"kind": "music", "loudness": {"version": "old"}} if record_exists else None
    )
    update = MagicMock()
    player._library.catalog.set_loudness = update
    measurement = {"lufs": -20}
    audio = np.ones((100, 2), dtype=np.float32)
    measure = MagicMock(return_value=(audio, measurement))
    monkeypatch.setattr("autodj.processing.loudness_matches", lambda *args: False)
    monkeypatch.setattr("autodj.processing.analyze_loudness", MagicMock(side_effect=AssertionError))
    monkeypatch.setattr("autodj.processing.load_and_measure_audio", measure)
    load = MagicMock(side_effect=AssertionError("second decode"))
    monkeypatch.setattr("autodj.processing.load_processed_audio", load)
    assert Player._load_playout_audio(player, entry) is audio
    measure.assert_called_once_with(Path(entry.path), "music", player._max_render_seconds())
    if record_exists:
        update.assert_called_once_with(Path(entry.path), measurement)
    else:
        update.assert_not_called()


def test_early_skip_restores_unheard_intro_but_late_skip_does_not(station, monkeypatch):
    player, (a, b, *_), _points = station
    current = player._render_track(a, b, 0)
    following = player._render_track(b, a, current.next_start_offset)
    assert following.start_offset > following.source_start_offset
    player._start_playing(current)
    player.bus = MagicMock()
    monkeypatch.setattr(player._render_ahead, "pop", lambda: following)

    player._on_position(2 * SR)
    player.skip_current()
    restored = player._take_render()
    assert restored.start_offset == round(0.25 * SR)
    assert restored.audio is following.full_audio
    player.bus.skip.assert_called_once_with()

    player._on_position(len(current.audio) - 1)
    player.skip_current()
    assert player._take_render() is following


def test_clock_and_history_advance_only_when_incoming_audio_is_heard(station):
    player, (a, b, *_), _points = station
    player._clock = {"cursor": 1}
    current = replace(
        player._render_track(a, b, 0), next_clock_cursor=3, next_clock_boundary=3600.0
    )
    player._start_playing(current)
    player._on_position(10 * SR)
    player._library.ledger.remember_play.assert_not_called()
    assert player._clock["cursor"] == 1
    player._on_position(10 * SR + 882)
    player._on_position(10 * SR + 1764)
    player._library.ledger.remember_play.assert_called_once_with(b.path)
    assert player._clock["cursor"] == 3
    assert player._clock_next_boundary == 3600.0


@pytest.mark.parametrize("rendered_in, kept", [("Night", True), ("Morning", False)])
def test_only_places_in_the_loaded_clockwheel_move_it(station, rendered_in, kept):
    # #133: a track rendered before Night was selected carries Morning's
    # places, which must not become Night's.
    player, (a, b, *_), _points = station
    player._clock = {"name": "Night", "cursor": 1}
    current = replace(
        player._render_track(a, b, 0), clock_cursor=3, next_clock_cursor=4, clock_name=rendered_in
    )
    player._start_playing(current)
    assert player._clock["cursor"] == (3 if kept else 1)
    player._on_position(10 * SR + 882)
    player._on_position(10 * SR + 1764)
    assert player._clock == {"name": "Night", "cursor": 4 if kept else 1}
    saved = player._library.ledger.remember_setting
    assert saved.call_count == (2 if kept else 0)


@pytest.mark.parametrize("pending_in, cursor", [("Night", 2), ("Morning", None)])
def test_render_cursor_from_another_clockwheel_is_not_carried(
    station, monkeypatch, pending_in, cursor
):
    from autodj.clockwheel import ClockPick

    player, (a, b, *_), _points = station
    player._clock = {"name": "Night", "cursor": 0}
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed", clock_cursor=2, clock_name=pending_in)
    result = player._next_rendered()
    assert (result.clock_cursor, result.clock_name) == (cursor, "Night")
    # Nor does the next clock pick start from the other clockwheel's place.
    player._cfg.playback.pick_mode = "clock"
    player._state.queue.clear()
    player._set_render_cursor(a, 0, "clock", clock_cursor=2, clock_name=pending_in)
    picked = MagicMock(return_value=ClockPick(b, None, 3, []))
    monkeypatch.setattr(player, "_clock_pick", picked)
    player._choose_station_next(a, player._pick_context(pending=a, peek_queue=True))
    assert picked.call_args.args[1] == cursor


@pytest.mark.parametrize("pending_in, picks", [("Night", [4, 0]), ("Morning", [None])])
def test_resync_time_from_another_clockwheel_is_not_used(station, monkeypatch, pending_in, picks):
    # #133: Morning's next handoff must not restart Night.
    from autodj.clockwheel import ClockPick

    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "clock"
    now = 1_791_621_000.0
    player._clock = {"name": "Night", "cursor": 0, "resync_minutes": [0]}
    player._clock_next_boundary = now + 3600
    player._set_render_cursor(
        a, 0, "clock", clock_cursor=4, clock_boundary=now + 14, clock_name=pending_in
    )
    monkeypatch.setattr("autodj.player.time.time", lambda: now)
    picked = MagicMock(side_effect=[ClockPick(b, None, 5, []), ClockPick(b, None, 1, [])])
    monkeypatch.setattr(player, "_clock_pick", picked)
    player._choose_station_next(a, player._pick_context(pending=a, peek_queue=True))
    assert [call.args[1] for call in picked.call_args_list] == picks


@pytest.mark.parametrize("path", ["reset", "worker"])
@pytest.mark.parametrize("seeded_in, cursor", [("Night", 3), ("Morning", None)])
def test_seed_place_from_another_clockwheel_is_not_carried(
    station, monkeypatch, path, seeded_in, cursor
):
    # #133: a seed picked in Morning must not label its place as Night's.
    from autodj.clockwheel import ClockPick

    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"name": seeded_in, "cursor": 0}
    monkeypatch.setattr(player, "_clock_pick", MagicMock(return_value=ClockPick(a, None, 3, [])))
    assert player._random_start_entry() is a
    assert (player._seed_clock_cursor, player._seed_clock_name) == (3, seeded_in)
    player._clock = {"name": "Night", "cursor": 0}
    player._cfg.playback.pick_mode = "random"
    player._state.queue[:] = [b]
    if path == "reset":
        player.reset_render_ahead(a, 0, "seed")
    else:
        # The render worker seeds a playing bus with nothing to render.
        player.bus = MagicMock(playing=True)
        player._pending_entry = None
        monkeypatch.setattr(player, "_random_start_entry", lambda: a)
    result = player._next_rendered()
    assert (result.clock_cursor, result.clock_name) == (cursor, "Night")


def test_clock_resync_drops_unheard_pick_without_recording_it(station, monkeypatch):
    from autodj.clockwheel import ClockPick

    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 4, "resync_minutes": [0]}
    now = 1_791_621_000.0
    player._set_render_cursor(a, 0, "clock", clock_cursor=4, clock_boundary=now + 14)
    monkeypatch.setattr("autodj.player.time.time", lambda: now)
    picked = MagicMock(side_effect=[ClockPick(b, None, 5, []), ClockPick(b, None, 1, [])])
    monkeypatch.setattr(player, "_clock_pick", picked)
    context = player._pick_context(pending=a, peek_queue=True)
    entry, mode, liners, cursor, _key = player._choose_station_next(a, context)
    assert (entry, mode, liners, cursor) == (b, "clock", (), 1)
    assert [call.args[1] for call in picked.call_args_list] == [4, 0]
    assert player._clock["cursor"] == 4
    player._library.ledger.remember_play.assert_not_called()
    player._library.ledger.remember_setting.assert_not_called()


@pytest.mark.parametrize(
    "boundary, required, include",
    [
        (30, True, False),
        (33.05, True, True),
        (30, False, True),
        (None, True, True),
    ],
)
def test_clock_omits_only_liner_when_pair_will_not_fit(
    station, monkeypatch, boundary, required, include
):
    from autodj.clockwheel import ClockPick

    player, (a, b, _, jingle), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 4, "resync_minutes": [0]}
    player._set_render_cursor(a, 0, "clock", clock_cursor=4, clock_boundary=boundary)
    monkeypatch.setattr("autodj.player.time.time", lambda: 0)
    pick = MagicMock(return_value=ClockPick(b, jingle, 2, [], liner_requires_following=required))
    monkeypatch.setattr(player, "_clock_pick", pick)
    context = player._pick_context(pending=a, peek_queue=True)
    entry, mode, liners, cursor, _ = player._choose_station_next(a, context)
    assert (entry, mode, cursor) == (b, "clock", 2)
    assert liners == (((jingle, False),) if include else ())
    pick.assert_called_once()
    player._library.ledger.remember_play.assert_not_called()
    player._library.ledger.remember_setting.assert_not_called()


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize(
    "overlap, intro, allow_outro, liner_segue",
    [
        (None, 2.25, False, None),
        (0, 2.25, False, None),
        (0, 0.25, True, None),
        (0, 2.25, True, None),
        (3, 2.25, True, None),
        (None, 2.25, False, 2.5),
        (3, 2.25, False, 2.5),
    ],
)
def test_clock_pair_estimate_matches_saved_point_render(
    station, standalone, overlap, intro, allow_outro, liner_segue
):
    player, (a, b, voice, _), points = station
    points[voice.path].update(start=0.5, overlap_start=overlap, segue=liner_segue)
    points[b.path]["intro"] = intro
    player._library.catalog.record_for_path = lambda _: {"allow_outro": allow_outro}
    rendered = player._render_track(
        voice if standalone else a, b, 0, liners=() if standalone else ((voice, False),)
    )
    incoming_time = (len(rendered.audio) - rendered.overlap_frames) / SR
    boundary = incoming_time + 18 - 0.25
    current, when = (None, 0) if standalone else (a, 10)
    assert player._clock_liner_fits(current, voice, b, when, boundary)
    assert not player._clock_liner_fits(current, voice, b, when, boundary - 0.01)


def test_clock_checks_the_actual_song_after_dmca_replacement(station, monkeypatch):
    from autodj.clockwheel import ClockPick

    player, (a, b, voice, jingle), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._set_render_cursor(a, 0, "clock", clock_boundary=30)
    monkeypatch.setattr("autodj.player.time.time", lambda: 0)
    monkeypatch.setattr(
        player,
        "_clock_pick",
        lambda *_: ClockPick(voice, jingle, 2, [], liner_requires_following=True),
    )
    monkeypatch.setattr(player, "_dmca_checked", lambda *_: b)
    entry, _, liners, _, _ = player._choose_station_next(a, player._pick_context())
    assert entry is b
    assert liners == ()


def test_clock_new_cycle_liner_uses_next_reset(station, monkeypatch):
    from autodj.clockwheel import ClockPick

    player, (a, b, _, jingle), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 4, "resync_minutes": [0]}
    now = 1_791_621_000.0
    player._set_render_cursor(a, 0, "clock", clock_cursor=4, clock_boundary=now + 14)
    monkeypatch.setattr("autodj.player.time.time", lambda: now)
    monkeypatch.setattr(
        player,
        "_clock_pick",
        MagicMock(
            side_effect=[
                ClockPick(b, None, 5, []),
                ClockPick(b, jingle, 2, [], liner_requires_following=True),
            ]
        ),
    )
    context = player._pick_context(pending=a, peek_queue=True)
    entry, _, liners, cursor, _ = player._choose_station_next(a, context)
    assert (entry, liners, cursor) == (b, ((jingle, False),), 2)
    assert player._pending_clock_boundary > now + 33.05


@pytest.mark.parametrize("boundary, include", [(20, False), (23.05, True)])
def test_clock_start_checks_pair_and_keeps_the_song_it_checked(
    station, monkeypatch, boundary, include
):
    from autodj.clockwheel import ClockPick

    player, (_, b, _, jingle), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock_slots = [None] * 4
    player._clock_next_boundary = boundary
    monkeypatch.setattr("autodj.player.time.time", lambda: 0)
    pick = MagicMock(return_value=ClockPick(b, jingle, 3, [], liner_requires_following=True))
    monkeypatch.setattr(player, "_clock_pick", pick)
    current = player._random_start_entry()
    assert current is (jingle if include else b)
    assert player._seed_clock_cursor == (2 if include else 3)
    if include:
        player._set_render_cursor(jingle, 0, "clock", clock_cursor=2, clock_boundary=boundary)
        for _ in range(2):  # A render retry must not choose a different song.
            entry, _, liners, cursor, _ = player._choose_station_next(
                jingle, player._pick_context()
            )
            assert (entry, liners, cursor) == (b, (), 3)
    pick.assert_called_once()
    player._library.ledger.remember_play.assert_not_called()


def test_clock_music_span_uses_end_without_cues_and_preserves_zero(station):
    player, (a, *_), points = station
    points[a.path] = {"start": 2, "end": 5}
    assert player._clock_music_span(a) == (2, 5)
    points[a.path] = {"segue": 0, "outro": 10, "end": 15}
    assert player._clock_music_span(a) == (0, 0)


def test_hard_jump_clears_early_skip_flag(station):
    player, (a, *_), _points = station
    player._skip_next_intro = True
    player.reset_render_ahead(a)
    assert not player._skip_next_intro


def test_starting_with_a_jingle_keeps_its_tail_and_release(station):
    player, (_, b, _, jingle), _points = station
    rendered = player._render_track(jingle, b, 0)
    assert rendered.audio[0, 1] == 0.5
    assert rendered.audio[round(5.2 * SR), 0] == 0
    assert rendered.audio[round(5.3 * SR), 0] == 0.25
    assert rendered.audio[-1, 1] == 0.5
    assert rendered.next_start_offset == round(3.75 * SR)


def test_clock_start_keeps_a_preceding_liner_and_obeys_empty_pool(station, monkeypatch):
    from autodj.clockwheel import ClockPick

    player, (a, _b, voice, _jingle), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock_slots = [None] * 4
    monkeypatch.setattr(player, "_clock_pick", lambda *_: ClockPick(a, voice, 3, []))
    assert player._random_start_entry() is voice
    assert player._seed_clock_cursor == 2
    monkeypatch.setattr(player, "_clock_pick", lambda *_: ClockPick(None, None, 0, ["blocked"]))
    assert player._random_start_entry() is None


@pytest.mark.parametrize("overlap, music_entry", [(None, 14), (0, 12)])
def test_default_id_ignores_song_outro_and_distinguishes_blank_overlap(
    station, overlap, music_entry
):
    player, (a, b, voice, _), points = station
    player._library.catalog.record_for_path = lambda _path: {"allow_outro": False}
    points[voice.path]["overlap_start"] = overlap
    result = player._render_track(a, b, 0, liners=((voice, True),))
    assert result.liner_events[0][0] == 10 * SR
    assert len(result.audio) - result.overlap_frames == music_entry * SR


def test_voice_followed_by_id_never_starts_id_before_song_segue(station):
    player, (a, b, voice, jingle), points = station
    points[a.path]["outro"] = 2
    points[jingle.path].update(end=4, overlap_start=0)
    result = player._render_track(a, b, 0, liners=((voice, True), (jingle, True)))
    assert result.liner_events == ((6 * SR, voice, True), (10 * SR, jingle, True))
    assert len(result.audio) - result.overlap_frames == 12 * SR


@pytest.fixture
def decoded(station, tmp_path, monkeypatch):
    """The station on real files, counting each decode."""
    player, entries, points = station
    audio, loads = {}, []
    for index, entry in enumerate(entries):
        old = entry.path
        path = tmp_path / f"{index}.flac"
        path.write_bytes(b"x")
        entry.path = str(path)
        points[entry.path] = points[old]
        array = np.zeros((round(entry.length * SR), 2), dtype=np.float32)
        array[:, 0] = (index + 1) / 8
        audio[entry.path] = array

    def load(entry):
        loads.append(entry.path)
        return audio[entry.path].copy()

    monkeypatch.setattr(player, "_load_playout_audio", load)
    return player, entries, loads


@pytest.mark.parametrize("mode", ["saved_points", "full_intro_outro"])
def test_the_incoming_decode_is_reused_by_the_next_render(decoded, mode):
    """Each track is decoded once: the render mixing it in decodes it for the next one."""
    player, (a, b, *_), loads = decoded
    player._cfg.playback.transition_mode = mode
    first = player._render_track(a, b, 0)
    following = player._render_track(b, a, first.next_start_offset)
    assert sorted(loads) == sorted([a.path, b.path, a.path])

    player._playout_slot = None  # the same render from a fresh decode
    fresh = player._render_track(b, a, first.next_start_offset)
    np.testing.assert_array_equal(following.audio, fresh.audio)
    assert loads.count(b.path) == 2


def test_a_changed_file_is_decoded_again(decoded):
    player, (a, b, *_), loads = decoded
    first = player._render_track(a, b, 0)
    Path(b.path).write_bytes(b"replaced")
    player._render_track(b, a, first.next_start_offset)
    assert loads.count(b.path) == 2


def test_play_now_decodes_both_tracks_side_by_side(decoded, monkeypatch):
    """Neither track is cached on a jump, so both decodes run at once."""
    player, (a, b, *_), _loads = decoded
    load = player._load_playout_audio
    started = {a.path: threading.Event(), b.path: threading.Event()}

    def overlapping(entry):
        started[entry.path].set()
        other = b.path if entry.path == a.path else a.path
        assert started[other].wait(10), "the decodes ran one after the other"
        return load(entry)

    monkeypatch.setattr(player, "_load_playout_audio", overlapping)
    assert player._render_track(a, b, 0) is not None


def test_saved_render_is_built_in_one_buffer(decoded):
    """The incoming head is added into the handoff's buffer, not into a new copy."""
    player, (a, b, *_), _loads = decoded
    result = player._render_track(a, b, 0)
    assert result.full_audio is not None
    assert np.shares_memory(result.audio, result.full_audio)
    assert result.audio[-1, 0] == pytest.approx(2 / 8)  # the incoming head, in place


@pytest.mark.parametrize("known", [True, False])
def test_play_path_refuses_a_long_track_before_decoding(station, tmp_path, monkeypatch, known):
    """A reliable source header refuses it regardless of the catalog length."""
    from autodj.stereo import TrackTooLongError

    player, (entry, *_), _points = station
    player._cfg.playback.loudness_processing = "smooth"
    entry.length = 3 * 3600.0 if known else 0.0
    monkeypatch.setattr("autodj.processing.probe_seconds", lambda _path: 3 * 3600.0)
    for name in ("load_and_measure_audio", "load_processed_audio", "analyze_loudness"):
        monkeypatch.setattr(f"autodj.processing.{name}", MagicMock(side_effect=AssertionError))
    with pytest.raises(TrackTooLongError):
        Player._load_playout_audio(player, entry)


@pytest.mark.parametrize("change", ["processing", "source"])
def test_prefetch_does_not_stamp_old_audio_with_changes_during_decode(decoded, monkeypatch, change):
    player, (entry, *_), _loads = decoded
    player._library = None
    monkeypatch.setattr(
        player, "_load_playout_audio", lambda entry: Player._load_playout_audio(player, entry)
    )
    monkeypatch.setattr("autodj.processing.probe_seconds", lambda _path: None)
    entered, release = threading.Event(), threading.Event()
    decodes = []

    def raw(*args):
        sample = 0.1 if not decodes else 0.3
        decodes.append(sample)
        if len(decodes) == 1:
            entered.set()
            assert release.wait(5)
        return np.full((100, 2), sample, np.float32)

    monkeypatch.setattr("autodj.player.load_stereo", raw)
    monkeypatch.setattr(
        "autodj.processing.load_and_measure_audio",
        lambda *_: (np.full((100, 2), 0.3, np.float32), {}),
    )
    future = player._prefetch_playout_audio(entry)
    try:
        assert entered.wait(5)
        if change == "processing":
            player._cfg.playback.loudness_processing = "stereotool"
        else:
            Path(entry.path).write_bytes(b"new source")
    finally:
        release.set()
    assert future.result(timeout=5)[0] is None
    actual = player._playout_audio(entry, player._take_prefetched(entry))
    np.testing.assert_array_equal(actual, np.full((100, 2), 0.3, np.float32))


def test_first_loudness_measurement_keeps_the_prefetched_decode(decoded, monkeypatch):
    from autodj.processing import PREPARATION_VERSION

    player, (entry, *_), _loads = decoded
    player._cfg.playback.loudness_processing = "stereotool"
    stat = Path(entry.path).stat()
    measured = {
        "version": PREPARATION_VERSION,
        "kind": "music",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": -20.0,
        "gain_db": -8.0,
    }
    record = {"kind": "music", "loudness": {}}
    player._library.catalog.record_for_path = lambda _path: record
    player._library.catalog.set_loudness = lambda _path, measurement: record.update(
        loudness=measurement
    )
    monkeypatch.setattr(
        player, "_load_playout_audio", lambda entry: Player._load_playout_audio(player, entry)
    )
    monkeypatch.setattr("autodj.processing.probe_seconds", lambda _path: None)
    audio = np.full((100, 2), 0.3, np.float32)
    measure = MagicMock(return_value=(audio, measured))
    monkeypatch.setattr("autodj.processing.load_and_measure_audio", measure)
    monkeypatch.setattr(
        "autodj.processing.load_processed_audio",
        MagicMock(side_effect=AssertionError("second decode")),
    )
    future = player._prefetch_playout_audio(entry)
    assert future.result(timeout=5)[0] is not None
    assert player._playout_audio(entry, player._take_prefetched(entry)) is audio
    measure.assert_called_once()


def test_catalog_length_estimate_does_not_reject_a_playable_track(station, monkeypatch):
    player, (entry, *_), _points = station
    entry.length = 3 * 3600
    player._cfg.playback.loudness_processing = "off"
    monkeypatch.setattr("autodj.processing.probe_seconds", lambda _path: None)
    audio = np.full((100, 2), 0.3, np.float32)
    decode = MagicMock(return_value=audio)
    monkeypatch.setattr("autodj.player.load_stereo", decode)
    assert Player._load_playout_audio(player, entry) is audio
    decode.assert_called_once_with(entry.path, SR, player._max_render_seconds())


def test_prefetch_rejects_changes_to_an_existing_loudness_measurement(decoded, monkeypatch):
    from autodj.processing import PREPARATION_VERSION

    player, (entry, *_), _loads = decoded
    stat = Path(entry.path).stat()
    measured = {
        "version": PREPARATION_VERSION,
        "kind": "music",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": -20.0,
        "gain_db": -8.0,
    }
    record = {"kind": "music", "loudness": measured}
    player._library.catalog.record_for_path = lambda _path: record

    def decode(_entry):
        record["loudness"] = {**measured, "lufs": -22.0, "gain_db": -6.0}
        return np.full((100, 2), 0.3, np.float32)

    monkeypatch.setattr(player, "_load_playout_audio", decode)
    assert player._prefetch_playout_audio(entry).result(timeout=5)[0] is None


def test_clock_conditions_stay_in_force_when_repeat_rules_relax(station):
    from autodj.clockwheel import ClockSlot

    player, (a, b, *_), _ = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 0, "resync_minutes": []}
    b.year = 1995
    a.year = 2005
    player._clock_slots = [
        ClockSlot(conditions=({"field": "year", "operator": "lt", "value": 2000},))
    ]
    context = replace(player._pick_context(), recent=frozenset({b.path}))
    assert player._clock_pick(context, 0).entry is b


def test_missing_beets_step_skips_to_music_and_does_not_broaden_fallback(station):
    from autodj.clockwheel import ClockSlot

    player, (a, b, *_), _ = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 0, "resync_minutes": []}
    broken = ClockSlot(
        conditions=({"field": "beets", "name": "mood", "operator": "is", "value": "happy"},)
    )
    player._clock_slots = [broken, ClockSlot(kind="track", path=b.path)]
    player._library.clock_beets = SimpleNamespace(
        snapshot=lambda _fields: (frozenset(), {}, "Beets unavailable")
    )
    selected = player._clock_pick(player._pick_context(), 0)
    assert selected.entry is b
    assert "Beets unavailable" in player._clock_status
    player._clock_slots = [broken, ClockSlot(kind="category", category="empty")]
    player._fallback_pick = MagicMock(return_value=a)
    assert player._empty_category_fallback(a, player._pick_context()) is None
    player._fallback_pick.assert_not_called()


def test_clock_conditions_are_worked_out_once_per_library_generation(station, monkeypatch):
    from autodj import clock_conditions
    from autodj.clockwheel import ClockSlot

    player, (a, b, voice, jingle), _ = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 0, "resync_minutes": []}
    a.year, b.year = 2005, 1995
    music, liners = (a, b), (voice, jingle)
    player._library.entries_snapshot = lambda kind="music": liners if kind == "liner" else music
    player._clock_slots = [
        ClockSlot(conditions=({"field": "year", "operator": "lt", "value": 2000},))
    ]
    built = []
    real = clock_conditions.condition_matcher
    monkeypatch.setattr(
        clock_conditions,
        "condition_matcher",
        lambda condition: built.append(condition) or real(condition),
    )
    assert player._clock_pick(player._pick_context(), 0).entry is b
    assert player._clock_pick(player._pick_context(), 0).entry is b
    assert len(built) == 1
    music = (a, b)  # The library published new entries.
    assert player._clock_pick(player._pick_context(), 0).entry is b
    assert len(built) == 2


def test_clock_save_during_pick_keeps_original_slots_and_step(station, monkeypatch):
    import autodj.clockwheel as clockwheel

    player, (a, b, *_), _ = station
    player._clock = {"cursor": 0}
    player._clock_slots = [clockwheel.ClockSlot(kind="track", path=a.path)]
    old_steps = [{"type": "track", "path": a.path}]
    player._clock_steps = old_steps
    monkeypatch.setattr(player, "playable_entries", lambda: (a, b))
    original_pick = clockwheel.pick_slot

    def save_then_pick(*args, **kwargs):
        player._clock = {"cursor": 0}
        player._clock_slots = [clockwheel.ClockSlot(kind="track", path=b.path)]
        player._clock_steps = [{"type": "track", "path": b.path}]
        return original_pick(*args, **kwargs)

    monkeypatch.setattr(clockwheel, "pick_slot", save_then_pick)
    picked = player._clock_pick(player._pick_context(), None)
    assert picked.entry is a
    assert picked.step_snapshot["step"] == {"type": "track", "path": a.path}
    old_steps[0]["path"] = b.path
    assert picked.step_snapshot["step"]["path"] == a.path
