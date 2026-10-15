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
        jingle.path: {"start": 0, "segue": 5.3, "end": 8.8, "overlap_start": 5.3},
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


@pytest.mark.parametrize("speed_percent", [-10.0, 10.0])
def test_regular_music_to_liner_keeps_voice_native_and_music_rate(station, speed_percent):
    player, (music, _next, voice, _), _points = station
    player._cfg.playback.transition_mode = "regular"
    player._cfg.playback.music_speed_percent = speed_percent
    rate = 1.0 + speed_percent / 100.0
    result = player._render_track(music, voice, 0)

    assert result is not None
    assert result.playback_rate == pytest.approx(rate)
    assert result.next_playback_rate == 1.0
    assert result.overlap_frames == round(1.5 * SR)
    assert result.next_start_offset == round(1.5 * SR)
    # The voice is still exactly the native-duration, native-pitch source.
    assert result.audio[-1, 1] == pytest.approx(0.5)
    # The source cursor represented by the rendered tail must remain within
    # the outgoing track even when its fade needs an earlier source window.
    assert result.start_offset + round(len(result.audio) * rate) <= round(music.length * SR)


def test_regular_speed_transition_does_not_shift_incoming_voice_pitch(station, monkeypatch):
    player, (music, _next, voice, _), _points = station
    player._cfg.playback.transition_mode = "regular"
    player._cfg.playback.music_speed_percent = 10.0
    load_audio = player._load_playout_audio
    voice_audio = np.zeros((round(voice.length * SR), 2), dtype=np.float32)
    times = np.arange(len(voice_audio), dtype=np.float64) / SR
    voice_audio[:, 1] = 0.5 * np.sin(2 * np.pi * 440 * times)
    monkeypatch.setattr(
        player,
        "_load_playout_audio",
        lambda entry: voice_audio.copy() if entry.path == voice.path else load_audio(entry),
    )

    result = player._render_track(music, voice, 0)
    assert result is not None
    vocal_tail = result.audio[-round(1.2 * SR) : -round(0.2 * SR), 1]
    spectrum = np.abs(np.fft.rfft(vocal_tail * np.hanning(len(vocal_tail))))
    peak_hz = np.fft.rfftfreq(len(vocal_tail), 1 / SR)[int(np.argmax(spectrum))]
    assert peak_hz == pytest.approx(440.0, abs=1.0)


@pytest.mark.parametrize("transition_mode", ["regular", "saved_points"])
def test_speed_change_invalidates_prepared_incoming_music(station, transition_mode):
    player, (_music, next_music, voice, _), _points = station
    player._cfg.playback.transition_mode = transition_mode
    player._cfg.playback.music_speed_percent = 5.0
    rendered = player._render_track(voice, next_music, 0)
    assert rendered is not None
    assert rendered.next_playback_rate == pytest.approx(1.05)
    assert not player._queue_pick_is_stale(rendered)

    player._cfg.playback.music_speed_percent = -5.0
    assert player._queue_pick_is_stale(rendered)


@pytest.mark.parametrize("booked_break", [True, False])
def test_speed_change_keeps_committed_break_and_pinned_render(station, booked_break):
    player, (music, next_music, *_), _points = station
    player._cfg.playback.music_speed_percent = 10.0
    track = RenderedTrack(
        music,
        np.zeros((SR, 2), dtype=np.float32),
        next_music,
        0,
        "none",
        overlap_frames=SR,
        break_events=((0, "booked-break", SR),) if booked_break else (),
        playback_rate=1.1,
        next_playback_rate=1.0,
    )
    if not booked_break:
        player._slot_plan = SimpleNamespace(pinned=True, ending=next_music)

    assert not player._queue_pick_is_stale(track)


def test_speed_change_without_incoming_overlap_is_not_stale(station):
    player, (music, next_music, *_), _points = station
    player._cfg.playback.transition_mode = "regular"
    player._cfg.playback.music_speed_percent = 10.0
    track = RenderedTrack(
        music,
        np.zeros((SR, 2), dtype=np.float32),
        next_music,
        0,
        "none",
        playback_rate=1.1,
        next_playback_rate=1.0,
    )

    assert not player._queue_pick_is_stale(track)


def test_break_auto_placement_uses_rate_adjusted_song_intro(tmp_path):
    from tests.unit._break_station import make_break, make_station

    player, (music, next_music, *_), _points, desk = make_station(tmp_path)
    player._cfg.playback.music_speed_percent = 10.0
    item = make_break(desk, "speed-slot", seconds=1.0)
    desk.add(item)
    rendered_breaks = desk.take(item.slot_id, item.created_at)

    rendered = player._render_track(music, next_music, 0, breaks=tuple(rendered_breaks))

    assert rendered is not None
    assert desk.get(item.break_id).mixed_as == "standalone"


def test_seek_maps_source_position_to_rate_adjusted_output(station):
    player, (music, *_), _points = station
    player._cfg.playback.music_speed_percent = 10.0
    rendered = player._render_track(music, None, 0)
    assert rendered is not None
    player._playing_render = rendered
    player._playback_len = round(len(rendered.audio) * rendered.playback_rate)
    player.bus = MagicMock()

    assert player.seek_to(5.0) == pytest.approx(5.0)
    output_frame = round(5 * SR / rendered.playback_rate)
    player.bus.seek.assert_called_once_with(output_frame)
    player._on_position(output_frame)
    assert abs(player._playback_pos[0] - round(5 * SR)) <= 1


def test_saved_points_speed_keeps_liner_voice_pitch_native(station, monkeypatch):
    player, (music, next_music, voice, _), _points = station
    player._cfg.playback.music_speed_percent = 10.0
    load_audio = player._load_playout_audio
    voice_audio = np.zeros((round(voice.length * SR), 2), dtype=np.float32)
    times = np.arange(len(voice_audio), dtype=np.float64) / SR
    voice_audio[:, 1] = 0.5 * np.sin(2 * np.pi * 440 * times)
    monkeypatch.setattr(
        player,
        "_load_playout_audio",
        lambda entry: voice_audio.copy() if entry.path == voice.path else load_audio(entry),
    )

    result = player._render_track(music, next_music, 0, liners=((voice, True),))
    assert result is not None
    event_at = round(8 * SR / 1.1)
    assert result.liner_events[0][0] == event_at
    vocal = result.audio[event_at + round(0.1 * SR) : event_at + round(1.1 * SR), 1]
    spectrum = np.abs(np.fft.rfft(vocal * np.hanning(len(vocal))))
    peak_hz = np.fft.rfftfreq(len(vocal), 1 / SR)[int(np.argmax(spectrum))]
    assert peak_hz == pytest.approx(440.0, abs=1.0)


def test_matched_intro_keeps_native_voice_rate_and_wall_vocal_protection(station, tmp_path):
    import soundfile as sf

    from autodj.matched_intros import Chosen

    player, (music, next_music, *_), _points = station
    player._cfg.playback.music_speed_percent = 10.0
    intro_audio = np.zeros((round(2.0 * SR), 2), dtype=np.float32)
    times = np.arange(round(1.5 * SR), dtype=np.float64) / SR
    intro_audio[: len(times), 1] = 0.5 * np.sin(2 * np.pi * 440 * times)
    intro_path = tmp_path / "matched-intro.wav"
    sf.write(intro_path, intro_audio, SR, subtype="FLOAT")

    rendered = player._render_track(
        music, next_music, 0, intro=Chosen("intro-id", intro_path, "New single")
    )

    assert rendered is not None and rendered.intro_events
    at, _intro_id, end, _label = rendered.intro_events[0]
    assert (end - at) / SR == pytest.approx(1.5, abs=0.06)
    incoming = (len(rendered.audio) - rendered.overlap_frames) / SR
    assert end / SR <= incoming + 2.0 / 1.1 + 1e-3
    vocal = rendered.audio[at + round(0.1 * SR) : at + round(1.1 * SR), 1]
    spectrum = np.abs(np.fft.rfft(vocal * np.hanning(len(vocal))))
    peak_hz = np.fft.rfftfreq(len(vocal), 1 / SR)[int(np.argmax(spectrum))]
    assert peak_hz == pytest.approx(440.0, abs=1.0)


def test_matched_intro_events_restore_at_output_frame_after_early_skip(
    station, tmp_path, monkeypatch
):
    import soundfile as sf

    from autodj.matched_intros import Chosen

    player, (music, next_music, *_), points = station
    player._cfg.playback.music_speed_percent = 10.0
    points[music.path]["start"] = 1.0
    intro_audio = np.zeros((round(2.0 * SR), 2), dtype=np.float32)
    intro_audio[: round(1.5 * SR), 1] = 0.5
    intro_path = tmp_path / "matched-intro.wav"
    sf.write(intro_path, intro_audio, SR, subtype="FLOAT")
    intro = Chosen("intro-id", intro_path, "New single")
    rendered = player._render_track(music, next_music, 2 * SR, intro=intro)
    assert rendered is not None and rendered.intro_events
    expected_prefix = round((rendered.start_offset - rendered.source_start_offset) / 1.1)
    monkeypatch.setattr(player._render_ahead, "pop", lambda: rendered)
    player._skip_next_intro = True

    restored = player._take_render()

    assert restored is not None and restored.audio is rendered.full_audio
    assert restored.intro_events[0][0] == rendered.intro_events[0][0] + expected_prefix
    assert restored.start_offset == rendered.source_start_offset


@pytest.mark.parametrize("speed_percent", [-10.0, 10.0])
def test_regular_liner_to_music_maps_incoming_cursor_to_source_frames(station, speed_percent):
    player, (music, next_music, voice, _), _points = station
    player._cfg.playback.transition_mode = "regular"
    player._cfg.playback.music_speed_percent = speed_percent
    rate = 1.0 + speed_percent / 100.0
    result = player._render_track(voice, music, 0)

    assert result is not None
    assert result.playback_rate == 1.0
    assert result.next_playback_rate == pytest.approx(rate)
    assert result.next_entry_offset == 0
    assert result.next_start_offset == round(1.5 * SR * rate)
    assert result.overlap_frames == round(1.5 * SR)
    assert len(result.audio) == round((voice.length - 1.5) * SR) + round(1.5 * SR)
    assert result.next_start_offset <= round(next_music.length * SR)


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
    player._cfg.playback.pick_mode = "clock"
    steps = iter(
        (
            {"number": 2, "step": {"type": "music", "selection": "random"}},
            {"number": 4, "step": {"type": "category", "selection": "least_recent"}},
        )
    )

    def choose(_current, _context, **_kwargs):
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
        player._render_track(a, b, 0),
        next_clock_cursor=3,
        next_clock_boundary=3600.0,
        next_clock_revision=player._clock_revision,
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


def test_same_name_clock_edit_invalidates_old_buffer_without_moving_live_cursor(station):
    player, (a, b, *_), _points = station
    player._clock = {"name": "Morning", "cursor": 2, "resync_minutes": []}
    player._clock_definition = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    player._clock_slots = [object()]
    player._clock_steps = [{"old": True}]
    player._clock_revision = 4
    player._pending_clock_name = "Morning"
    player._pending_clock_cursor = 2
    player._pending_clock_cursor_revision = 4
    player._pending_clock_boundary = None
    player._load_clockwheel = lambda _name: (
        {"name": "Morning", "steps": [{"new": True}], "resync_minutes": []},
        [object(), object()],
    )

    old = replace(
        player._render_track(a, b, 0),
        next_pick_mode="clock",
        clock_name="Morning",
        next_clock_name="Morning",
        clock_cursor=1,
        next_clock_cursor=2,
        clock_revision=4,
        next_clock_revision=4,
        next_clock_boundary=None,
    )
    with player._render_ahead._cond:
        player._render_ahead._ready = old
    player.apply_clockwheel_update("Morning", "immediate")

    assert player._clock_revision == 5
    assert player._clock["cursor"] == 0
    assert player._render_ahead._ready is None
    assert player._render_ahead._fallback is old
    assert player._pending_entry is a
    # The committed outgoing track still starts; its stale buffered cursor
    # and boundary cannot overwrite the newly applied same-name revision.
    player._start_playing(old)
    player._on_position(10 * SR + 1764)
    assert player._clock["cursor"] == 0
    player._rewind_render_cursor(old)
    player._forget_other_clock_place()
    assert player._pending_clock_cursor is None


def test_immediate_clock_edit_marks_inflight_render_for_revalidation(station):
    player, _entries, _points = station
    player._clock = {"name": "Morning", "cursor": 0, "resync_minutes": []}
    player._clock_definition = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    player._clock_revision = 2
    player._load_clockwheel = lambda _name: (
        {"name": "Morning", "steps": [{"new": True}], "resync_minutes": []},
        [object()],
    )
    with player._render_ahead._cond:
        player._render_ahead._rendering = True
    player.apply_clockwheel_update("Morning", "immediate")
    assert player._render_ahead._revalidate
    with player._render_ahead._cond:
        player._render_ahead._rendering = False


def test_continue_clock_edit_uses_baked_next_cursor_then_live_cursor(station):
    player, (a, b, *_), _points = station
    old = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    edited = {"name": "Morning", "steps": [{"new": True} for _ in range(5)], "resync_minutes": []}
    player._clock = {"name": "Morning", "cursor": 2, "resync_minutes": []}
    player._clock_definition = old
    player._clock_slots = [object()]
    player._clock_steps = old["steps"]
    player._clock_revision = 4
    player._load_clockwheel = lambda _name: (edited, [object()] * 5)
    player._playing_render = replace(
        player._render_track(a, b, 0),
        next_clock_name="Morning",
        next_clock_revision=4,
        next_clock_cursor=3,
    )

    player.apply_clockwheel_update("Morning", "continue")
    assert player._clock["cursor"] == 3
    assert player._clock_revision == 5

    player._clock_revision = 5
    player._clock_definition = old
    player._clock = {**player._clock, "cursor": 4}
    player._playing_render = replace(
        player._playing_render, next_clock_revision=4, next_clock_cursor=2
    )
    player.apply_clockwheel_update("Morning", "continue")
    assert player._clock["cursor"] == 4

    player._clock_definition = old
    player._clock = {**player._clock, "cursor": 5}
    player.apply_clockwheel_update("Morning", "continue")
    assert player._clock["cursor"] == 0


def test_clock_revision_snapshot_survives_edit_during_audio_render(station, monkeypatch):
    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"name": "Morning", "cursor": 0, "resync_minutes": []}
    player._clock_definition = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    player._clock_revision = 6
    player._set_render_cursor(a, 0, "clock", clock_cursor=0, clock_name="Morning", clock_revision=6)
    entered_render, finish_render = threading.Event(), threading.Event()

    def choose(*_args, **_kwargs):
        player._chosen_clock_revision = player._clock_revision
        player._chosen_clock_step = None
        return b, "clock", (), 1, ""

    def render(current, upcoming, *_args, **_kwargs):
        entered_render.set()
        assert finish_render.wait(5)
        return RenderedTrack(current, np.zeros((10, 2), np.float32), upcoming, 0, "none")

    monkeypatch.setattr(player, "_choose_station_next", choose)
    monkeypatch.setattr(player, "_render_track", render)
    player._load_clockwheel = lambda _name: (
        {"name": "Morning", "steps": [{"new": True}], "resync_minutes": []},
        [object()],
    )
    result = []
    with player._render_ahead._cond:
        player._render_ahead._rendering = True
    worker = threading.Thread(target=lambda: result.append(player._next_rendered()))
    worker.start()
    assert entered_render.wait(5)
    edit = threading.Thread(target=lambda: player.apply_clockwheel_update("Morning", "immediate"))
    edit.start()
    edit.join(0.1)
    assert edit.is_alive(), "clock edit should wait for the in-flight render"
    finish_render.set()
    worker.join(5)
    edit.join(5)
    with player._render_ahead._cond:
        player._render_ahead._rendering = False

    assert not worker.is_alive()
    assert not edit.is_alive()
    assert result[0].clock_revision == 6
    assert result[0].next_clock_revision == 6
    assert player._clock_revision == 7
    assert player._pending_clock_cursor_revision == 7
    assert player._queue_pick_is_stale(result[0])


def test_deferred_clock_edit_survives_configure_reload_and_promotes(station):
    player, _entries, _points = station
    old_definition = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    new_definition = {"name": "Morning", "steps": [{"new": True}], "resync_minutes": []}
    player._clock = {"name": "Morning", "cursor": 1, "resync_minutes": []}
    player._clock_definition = old_definition
    player._clock_slots = ["old"]
    player._clock_steps = old_definition["steps"]
    player._clock_revision = 8
    player._load_clockwheel = lambda _name: (new_definition, ["new"])
    saved_setting = {"name": "Morning", "cursor": 1}

    def remember_setting(_key, value):
        saved_setting.clear()
        saved_setting.update(value)

    player._ledger = SimpleNamespace(
        get_setting=lambda *_args: dict(saved_setting), remember_setting=remember_setting
    )
    player.apply_clockwheel_update("Morning", "next_restart")
    assert player._clock_definition == old_definition
    assert player._clock_slots == ["old"]
    assert saved_setting["pending_restart_reset"] is True
    player.configure_clock()
    assert player._clock_definition == old_definition
    player._promote_pending_clock()
    assert player._clock_definition == new_definition
    assert player._clock_slots == ["new"]
    assert player._clock["cursor"] == 0
    assert player._pending_clock_update is None
    assert "pending_restart_reset" not in saved_setting


def test_deferred_clock_restart_marker_resets_to_step_one_after_server_restart(station):
    player, _entries, _points = station
    old = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    latest = {"name": "Morning", "steps": [{"new": True}], "resync_minutes": []}
    saved_setting = {"name": "Morning", "cursor": 3}
    player._clock = {"name": "Morning", "cursor": 3, "resync_minutes": []}
    player._clock_definition = old
    player._clock_revision = 2
    player._ledger = SimpleNamespace(
        get_setting=lambda *_args: dict(saved_setting),
        remember_setting=lambda _key, value: (saved_setting.clear(), saved_setting.update(value)),
    )
    player._load_clockwheel = lambda _name: (latest, ["new"])
    player.apply_clockwheel_update("Morning", "next_restart")
    assert saved_setting["pending_restart_reset"] is True

    # A new Player has no in-memory pending definition; the persisted intent
    # still makes startup load the current saved wheel at step one.
    player._pending_clock_update = None
    player._clock = None
    player.configure_clock()
    assert player._clock_definition == latest
    assert player._clock["cursor"] == 0
    assert "pending_restart_reset" not in saved_setting


def test_deferred_edit_keeps_last_old_step_then_starts_new_step_one(station, monkeypatch):
    from autodj.clockwheel import ClockPick

    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "clock"
    old_definition = {
        "name": "Morning",
        "steps": [{"type": "track"} for _ in range(4)],
        "resync_minutes": [],
    }
    new_definition = {
        "name": "Morning",
        "steps": [{"type": "track"} for _ in range(2)],
        "resync_minutes": [],
    }
    player._clock = {"name": "Morning", "cursor": 3, "resync_minutes": []}
    player._clock_definition = old_definition
    player._clock_steps = old_definition["steps"]
    player._clock_slots = [object()] * 4
    player._clock_revision = 3
    player._pending_clock_name = "Morning"
    player._pending_clock_cursor = 3
    player._pending_clock_cursor_revision = 3
    player._pending_clock_update = (new_definition, [object()] * 2, 4)
    picks = iter((ClockPick(b, None, 0, [], step_index=3), ClockPick(a, None, 1, [], step_index=0)))
    monkeypatch.setattr(player, "_clock_pick", lambda *_args: next(picks))
    context = player._pick_context(pending=a, peek_queue=True)

    assert player._choose_station_next(a, context)[0] is b
    assert player._clock_definition == old_definition
    player._pending_clock_cursor = 0
    player._clock = {**player._clock, "cursor": 0}
    assert player._choose_station_next(b, context)[0] is a
    assert player._clock_definition == new_definition
    assert player._clock_revision == 4


@pytest.mark.parametrize("rendered_in, kept", [("Night", True), ("Morning", False)])
def test_only_places_in_the_loaded_clockwheel_move_it(station, rendered_in, kept):
    # #133: a track rendered before Night was selected carries Morning's
    # places, which must not become Night's.
    player, (a, b, *_), _points = station
    player._clock = {"name": "Night", "cursor": 1}
    current = replace(
        player._render_track(a, b, 0),
        clock_cursor=3,
        next_clock_cursor=4,
        clock_name=rendered_in,
        clock_revision=0,
        next_clock_revision=0,
    )
    player._start_playing(current)
    assert player._clock["cursor"] == (3 if kept else 1)
    player._on_position(10 * SR + 882)
    player._on_position(10 * SR + 1764)
    assert player._clock == {"name": "Night", "cursor": 4 if kept else 1}
    saved = player._library.ledger.remember_setting
    assert saved.call_count == (2 if kept else 0)


def test_clock_render_without_revision_cannot_move_saved_cursor(station):
    player, (a, b, *_), _points = station
    player._clock = {"name": "Morning", "cursor": 1}
    player._clock_revision = 4
    current = replace(
        player._render_track(a, b, 0),
        clock_cursor=3,
        clock_name="Morning",
        clock_revision=None,
    )
    player._start_playing(current)
    assert player._clock["cursor"] == 1


def test_pending_restart_marker_survives_audio_cursor_updates(station):
    player, (a, b, *_), _points = station
    player._clock = {
        "name": "Morning",
        "cursor": 0,
        "resync_minutes": [],
        "pending_restart_reset": True,
    }
    player._clock_revision = 2
    current = replace(
        player._render_track(a, b, 0),
        clock_cursor=1,
        clock_name="Morning",
        clock_revision=2,
    )
    player._start_playing(current)
    assert player._clock["cursor"] == 1
    assert player._clock["pending_restart_reset"] is True
    assert (
        player._library.ledger.remember_setting.call_args.args[1]["pending_restart_reset"] is True
    )


def test_slow_clock_pick_does_not_block_bus_callback_and_serializes_edit(station, monkeypatch):
    player, (a, b, *_), _points = station
    old = {"name": "Morning", "steps": [{"old": True}], "resync_minutes": []}
    new = {"name": "Morning", "steps": [{"new": True}], "resync_minutes": []}
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"name": "Morning", "cursor": 0, "resync_minutes": []}
    player._clock_definition = old
    player._clock_steps = old["steps"]
    player._clock_slots = [object()]
    player._clock_revision = 5
    player._pending_clock_name = "Morning"
    player._pending_clock_cursor_revision = 5
    player._set_render_cursor(a, 0, "clock", clock_cursor=0, clock_name="Morning", clock_revision=5)
    entered_pick, release_pick, edit_loaded = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def slow_pick(*_args, **_kwargs):
        entered_pick.set()
        assert release_pick.wait(5)
        player._chosen_clock_revision = 5
        player._chosen_clock_step = None
        return b, "clock", (), 1, ""

    monkeypatch.setattr(player, "_choose_station_next", slow_pick)

    def load_new(_name):
        edit_loaded.set()
        return new, [object()]

    monkeypatch.setattr(player, "_load_clockwheel", load_new)
    worker = threading.Thread(target=player._next_rendered)
    edit = threading.Thread(target=lambda: player.apply_clockwheel_update("Morning", "immediate"))
    worker.start()
    try:
        assert entered_pick.wait(2)
        edit.start()
        assert edit_loaded.wait(2)
        assert edit.is_alive(), "clock edit did not wait for the in-flight selection"
        started = replace(
            player._render_track(a, b, 0),
            clock_cursor=1,
            clock_name="Morning",
            clock_revision=5,
        )
        callback = threading.Thread(target=player._on_track_start, args=(started,))
        callback.start()
        callback.join(1)
        assert not callback.is_alive(), "audio callback waited for the slow selection"
        assert player._clock["cursor"] == 1
    finally:
        release_pick.set()
        worker.join(5)
        if edit.ident is not None:
            edit.join(5)
    assert not worker.is_alive()
    assert not edit.is_alive()
    assert player._clock_revision == 6


@pytest.mark.parametrize("pending_in, cursor", [("Night", 2), ("Morning", None)])
def test_render_cursor_from_another_clockwheel_is_not_carried(
    station, monkeypatch, pending_in, cursor
):
    from autodj.clockwheel import ClockPick

    player, (a, b, *_), _points = station
    player._clock = {"name": "Night", "cursor": 0}
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed", clock_cursor=2, clock_name=pending_in, clock_revision=0)
    result = player._next_rendered()
    assert (result.clock_cursor, result.clock_name) == (cursor, "Night")
    # Nor does the next clock pick start from the other clockwheel's place.
    player._cfg.playback.pick_mode = "clock"
    player._state.queue.clear()
    player._set_render_cursor(
        a, 0, "clock", clock_cursor=2, clock_name=pending_in, clock_revision=0
    )
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
        a,
        0,
        "clock",
        clock_cursor=4,
        clock_boundary=now + 14,
        clock_name=pending_in,
        clock_revision=0,
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


@pytest.mark.parametrize("deferred", [False, True])
def test_clock_resync_drops_unheard_pick_without_recording_it(
    station, monkeypatch, deferred, caplog
):
    from autodj.clockwheel import ClockPick

    caplog.set_level("INFO", logger="autodj.player")

    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"cursor": 4, "resync_minutes": [0]}
    now = 1_791_621_000.0
    player._set_render_cursor(
        a, 0, "clock", clock_cursor=4, clock_boundary=now + 14, clock_revision=0
    )
    if deferred:
        definition = {"steps": [{"type": "music"}], "resync_minutes": [30]}
        player._pending_clock_update = (definition, [object()], 1)
    monkeypatch.setattr("autodj.player.time.time", lambda: now)
    picked = MagicMock(side_effect=[ClockPick(b, None, 5, []), ClockPick(b, None, 1, [])])
    monkeypatch.setattr(player, "_clock_pick", picked)
    context = player._pick_context(pending=a, peek_queue=True)
    entry, mode, liners, cursor, _key = player._choose_station_next(a, context)
    assert (entry, mode, liners, cursor) == (b, "clock", (), 1)
    assert [call.args[1] for call in picked.call_args_list] == [4, 0]
    # The decision is logged with the pick.
    assert any("restarted the wheel" in record.getMessage() for record in caplog.records)
    assert player._clock["cursor"] == (0 if deferred else 4)
    if deferred:
        assert player._pending_clock_update is None
        assert player._clock_revision == player._chosen_clock_revision == 1
        assert player._clock["resync_minutes"] == [30]
        assert player._pending_clock_boundary > now + 14
    player._library.ledger.remember_play.assert_not_called()
    if deferred:
        player._library.ledger.remember_setting.assert_called_once_with("clock", player._clock)
    else:
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
    player._clock = {"name": "Morning", "cursor": 4, "resync_minutes": [0]}
    player._set_render_cursor(
        a,
        0,
        "clock",
        clock_cursor=4,
        clock_boundary=boundary,
        clock_name="Morning",
        clock_revision=player._clock_revision,
    )
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
        (-3, 5.25, False, None),
        (-3, 2.25, False, None),
        (None, 2.25, False, 2.5),
        (3, 2.25, False, 2.5),
        (3.3, 0.25, False, None),
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


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("intro", [0.0, 1.0, 1.3])
@pytest.mark.parametrize("start_offset", [0, SR])
def test_positive_liner_overlap_keeps_main_content_before_incoming_vocals(
    station, standalone, intro, start_offset
):
    player, (a, b, voice, _), points = station
    points[b.path]["intro"] = intro
    points[voice.path].update(start=0, segue=4, end=4, overlap_start=3.3)
    result = player._render_track(
        voice if standalone else a,
        b,
        start_offset,
        liners=() if standalone else ((voice, False),),
    )

    music_entry = (len(result.audio) - result.overlap_frames) / SR
    main_end = (4 if standalone else 12) - start_offset / SR
    assert music_entry + max(0, intro - points[b.path]["start"]) >= main_end


def test_clock_checks_the_actual_song_after_dmca_replacement(station, monkeypatch):
    from autodj.clockwheel import ClockPick

    player, (a, b, voice, jingle), _points = station
    player._cfg.playback.pick_mode = "clock"
    player._clock = {"name": "Morning", "cursor": 0, "resync_minutes": [0]}
    player._set_render_cursor(
        a, 0, "clock", clock_boundary=30, clock_name="Morning", clock_revision=0
    )
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
    player._clock = {"name": "Morning", "cursor": 4, "resync_minutes": [0]}
    now = 1_791_621_000.0
    player._set_render_cursor(
        a,
        0,
        "clock",
        clock_cursor=4,
        clock_boundary=now + 14,
        clock_name="Morning",
        clock_revision=player._clock_revision,
    )
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


def test_later_negative_liner_cue_does_not_delay_an_earlier_zero_liner(station):
    player, (a, b, voice, jingle), points = station
    points[b.path]["intro"] = 6
    points[voice.path].update(start=0, segue=1, end=1, overlap_start=0)
    points[jingle.path].update(start=0, segue=1, end=1, overlap_start=-3)
    result = player._render_track(a, b, 0, liners=((voice, False), (jingle, False)))
    assert result.liner_events == ((10 * SR, voice, False), (11 * SR, jingle, False))
    assert len(result.audio) - result.overlap_frames == 10 * SR


def test_first_negative_attached_liner_starts_after_music_and_keeps_tail(station):
    player, (a, b, voice, _), points = station
    points[b.path]["intro"] = 6
    points[voice.path].update(start=0.5, segue=2, end=4, overlap_start=-3)
    result = player._render_track(a, b, 0, liners=((voice, False),))
    assert result.liner_events == ((13 * SR, voice, False),)
    assert len(result.audio) - result.overlap_frames == 10 * SR
    assert result.full_audio[round(14 * SR), 1] == pytest.approx(0.5)
    assert result.full_audio[round(16.25 * SR), 1] == pytest.approx(0.5)


def test_seed_liner_negative_cue_starts_it_after_incoming_music(station):
    player, (_, b, voice, jingle), points = station
    points[b.path]["intro"] = 6
    points[voice.path].update(start=0.5, segue=2, end=4, overlap_start=-3)
    points[jingle.path].update(start=0, segue=1, end=1, overlap_start=0)
    result = player._render_track(voice, b, 0, liners=((jingle, False),))
    assert result.full_audio[0, 0] == pytest.approx(0.25)
    assert result.full_audio[round(1.2 * SR), 1] == pytest.approx(0)
    assert result.full_audio[round(1.25 * SR), 1] == pytest.approx(0.5)
    assert result.liner_events == ((round(4.75 * SR), jingle, False),)


@pytest.mark.parametrize("overlap,second_start", [(None, 2), (0, 2), (0.75, 2), (-3, 4)])
@pytest.mark.parametrize("start_offset", [0, SR])
def test_liner_chain_events_follow_audio_with_trimmed_start(
    station, monkeypatch, overlap, second_start, start_offset
):
    player, (_, b, voice, jingle), points = station
    points[b.path]["intro"] = 5.25
    points[voice.path].update(start=0.5, segue=2.5, end=2.5, overlap_start=overlap)
    points[jingle.path].update(start=0.2, segue=1.2, end=1.2, overlap_start=0)
    load = player._load_playout_audio

    def marked_audio(entry):
        audio = load(entry)
        if entry is jingle:
            audio[:, 1] = 0.75
        return audio

    monkeypatch.setattr(player, "_load_playout_audio", marked_audio)
    result = player._render_track(voice, b, start_offset, liners=((jingle, True),))
    at = round(second_start * SR) - max(0, start_offset - round(0.5 * SR))
    assert result.liner_events == ((at, jingle, True),)
    assert result.audio[at - 1, 1] == pytest.approx(0.5)
    assert result.audio[at, 1] == pytest.approx(0.75)


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("second_overlap,release", [(0, 2), (None, 7), (3, 4)])
def test_long_liner_after_negative_liner_still_respects_its_overlap(
    station, standalone, second_overlap, release
):
    player, (a, b, voice, jingle), points = station
    player._library.catalog.record_for_path = lambda _path: {"allow_outro": False}
    points[b.path]["intro"] = 5.25
    points[voice.path].update(start=0, segue=1, end=1, overlap_start=-3)
    points[jingle.path].update(start=0, segue=6, end=6, overlap_start=second_overlap)
    result = player._render_track(
        voice if standalone else a,
        b,
        0,
        liners=((jingle, True),) if standalone else ((voice, True), (jingle, True)),
    )
    first = 0 if standalone else 10
    assert result.liner_events[-1] == ((first + 1) * SR, jingle, True)
    assert len(result.audio) - result.overlap_frames == (first + release) * SR
    assert (len(result.audio) - result.overlap_frames) / SR + 5 >= first + 7


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


def test_nonclock_station_pick_does_not_hold_up_track_start(station, monkeypatch):
    player, (a, b, *_), _points = station
    player._cfg.playback.pick_mode = "least_recent"
    player._set_render_cursor(a, 0, "seed")
    started = player._render_track(b, a, 0)
    searching, release = threading.Event(), threading.Event()

    def choose(*_args, **_kwargs):
        searching.set()
        assert release.wait(5)
        return b, "least_recent", (), None, ""

    monkeypatch.setattr(player, "_choose_station_next", choose)
    picker = threading.Thread(target=player._next_rendered)
    starter = threading.Thread(target=player._start_playing, args=(started,))
    picker.start()
    try:
        assert searching.wait(2)
        starter.start()
        starter.join(1)
        assert not starter.is_alive(), "track start waited for a non-clock station pick"
    finally:
        release.set()
        picker.join(5)
        if starter.ident is not None:
            starter.join(5)


def test_clock_save_during_pick_keeps_original_slots_and_step(station, monkeypatch):
    import autodj.clockwheel as clockwheel
    from autodj.categories import parse_step_hours

    player, (a, b, *_), _ = station
    player._clock = {"cursor": 0}
    player._clock_slots = [clockwheel.ClockSlot(kind="track", path=a.path)]
    player._clock_step_hours = (None,)
    old_steps = [{"type": "track", "path": a.path}]
    player._clock_steps = old_steps
    context = player._pick_context()
    original_pick = clockwheel.pick_slot

    def save_then_entries():
        player._clock = {"cursor": 0}
        player._clock_slots = [clockwheel.ClockSlot(kind="track", path=b.path, selection="random")]
        player._clock_steps = [{"type": "track", "path": b.path}]
        player._clock_step_hours = (
            parse_step_hours({"days": [(player._station_now(context.at).weekday() + 1) % 7]}),
        )
        return (a, b)

    def check_history(*args, **kwargs):
        assert kwargs["normalized_history"] is not None
        return original_pick(*args, **kwargs)

    monkeypatch.setattr(player, "playable_entries", save_then_entries)
    monkeypatch.setattr(clockwheel, "pick_slot", check_history)
    picked = player._clock_pick(context, None)
    assert picked.entry is a
    assert picked.step_snapshot["step"] == {"type": "track", "path": a.path}
    old_steps[0]["path"] = b.path
    assert picked.step_snapshot["step"]["path"] == a.path
