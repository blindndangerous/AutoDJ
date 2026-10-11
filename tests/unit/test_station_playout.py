"""Station timing through the real player, including queue reservations."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.config import PlaybackConfig
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
        catalog=SimpleNamespace(points=lambda path: points[path]),
        entries_snapshot=lambda kind="music": tuple(
            entries[2:] if kind == "liner" else entries[:2]
        ),
        remember_play=MagicMock(),
        remember_setting=MagicMock(),
        last_played=lambda: {},
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
    player._library.remember_play.assert_called_once_with(voice.path)


def test_saved_start_survives_next_rendered_metadata(station):
    player, (a, b, *_), points = station
    points[a.path]["start"] = 0.25
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed")
    result = player._next_rendered()
    assert result.start_offset == round(0.25 * SR)
    assert result.start_offset + len(result.audio) == 11 * SR


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
def test_master_processing_reuses_prepared_sources(station, mode, tmp_path, monkeypatch):
    player, (entry, *_), _points = station
    player._cfg.playback.loudness_processing = mode
    prepared = tmp_path / "type-b-v1-music-9-prepared.wav"
    prepared.touch()
    player._library.catalog.record_for_path = lambda _path: {
        "prepared_path": str(prepared),
        "kind": "music",
    }
    audio = np.ones((100, 2), dtype=np.float32)
    load = MagicMock(return_value=audio)
    monkeypatch.setattr("autodj.player.load_stereo", load)
    assert Player._load_playout_audio(player, entry) is audio
    assert load.call_args.args[0] == str(prepared)
    result = player._render_track(entry, None, 0)
    assert result.loudness_processing == mode


def test_playback_reprepares_existing_music_cache_for_requested_boost(
    station, tmp_path, monkeypatch
):
    player, (entry, *_), _points = station
    player._cfg.playback.loudness_processing = "smooth"
    player._cfg.index.active_dir = tmp_path
    old_prepared = tmp_path / "old-music-prepared.wav"
    old_prepared.touch()
    player._library.catalog.record_for_path = lambda _path: {
        "prepared_path": str(old_prepared),
        "kind": "music",
    }
    update = MagicMock()
    player._library.catalog.put_entry = update
    new_prepared = tmp_path / "type-b-v1-music-9-new.wav"
    prepare = MagicMock(return_value=new_prepared)
    monkeypatch.setattr("autodj.processing.prepare_source", prepare)
    audio = np.ones((100, 2), dtype=np.float32)
    load = MagicMock(return_value=audio)
    monkeypatch.setattr("autodj.player.load_stereo", load)

    assert Player._load_playout_audio(player, entry) is audio

    prepare.assert_called_once_with(
        Path(entry.path),
        player._cfg.index.active_dir / "prepared",
        kind="music",
        quiet_boost_db=9.0,
    )
    update.assert_called_once_with(entry, kind="music", prepared_path=str(new_prepared))
    assert load.call_args.args[0] == str(new_prepared)


@pytest.mark.parametrize("mode", ["smooth", "stereotool"])
def test_prepared_liner_gets_one_playout_gain_without_mutating_cache(
    station, mode, tmp_path, monkeypatch
):
    player, (entry, *_), _points = station
    player._cfg.playback.loudness_processing = mode
    prepared = tmp_path / "prepared-liner.wav"
    prepared.touch()
    player._library.catalog.record_for_path = lambda _path: {
        "prepared_path": str(prepared),
        "kind": "liner",
    }
    cached_audio = np.ones((100, 2), dtype=np.float32)
    monkeypatch.setattr("autodj.player.load_stereo", lambda *_args: cached_audio)

    first = Player._load_playout_audio(player, entry)
    second = Player._load_playout_audio(player, entry)

    expected = 10 ** (-1.5 / 20)
    np.testing.assert_allclose(first, expected)
    np.testing.assert_allclose(second, expected)
    np.testing.assert_array_equal(cached_audio, 1.0)


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
    player._library.remember_play.assert_not_called()
    assert player._clock["cursor"] == 1
    player._on_position(10 * SR + 882)
    player._on_position(10 * SR + 1764)
    player._library.remember_play.assert_called_once_with(b.path)
    assert player._clock["cursor"] == 3
    assert player._clock_next_boundary == 3600.0


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
    player._library.remember_play.assert_not_called()
    player._library.remember_setting.assert_not_called()


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
