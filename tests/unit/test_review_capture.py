"""Review capture keeps the exact isolated forecast choices and decoded audio."""

from dataclasses import replace
from datetime import UTC, datetime, time
from types import SimpleNamespace

import numpy as np
import pytest

from autodj import hour_preview
from autodj.liner_scheduler import LinerScheduler
from autodj.rotation_sim import snapshot_player
from tests.unit.test_hour_preview import NOW, _event, _playing, _station
from tests.unit.test_liner_scheduler import _cfg, _folder
from tests.unit.test_time_checks import _checks, _settings


def test_capture_retains_selected_liners_without_changing_forecast(tmp_path):
    source, _, gold = _station(tmp_path)
    _playing(source, gold[0], NOW - 50)
    decided = hour_preview.read_station(source.player, now=NOW)
    snapshot = snapshot_player(source.player)
    plain, _, _ = hour_preview.predict(source, snapshot, decided, NOW + 500, seed=1, budget=30)
    captured, _, _ = hour_preview.predict(
        source, snapshot, decided, NOW + 500, seed=1, budget=30, capture_audio=True
    )
    assert [(r.kind, r.starts, r.seconds, r.entry) for r in captured] == [
        (r.kind, r.starts, r.seconds, r.entry) for r in plain
    ]
    songs = [r for r in captured if r.kind == "song"]
    assert any(r.incoming_handoff.liners for r in songs)
    assert all(r.playback_config is not source.player._cfg for r in songs)
    assert source.player._state.track_number == 0


def test_unknown_time_check_is_retained_in_capture_without_stopping(tmp_path):
    source, _, gold = _station(tmp_path, liners=[])
    _playing(source, gold[0], NOW - 50)
    source.player.liner_scheduler = LinerScheduler(source.player._cfg.playback, tmp_path, None)
    source.time_checks = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            forecast_seconds=lambda *a, **kw: None, tick=lambda *a: None
        )
    )
    source.events = [_event("Time", action="time_check", params={}, time=time(12, 11))]
    items, _, stopped = hour_preview.predict(
        source,
        snapshot_player(source.player),
        hour_preview.read_station(source.player, now=NOW),
        NOW + 500,
        seed=1,
        budget=30,
        capture_audio=True,
    )
    assert stopped is None
    assert any(row.duration_unknown and row.audio is None for row in items)
    assert any(row.kind == "song" for row in items)


def test_time_check_callback_captures_same_render_once(tmp_path, monkeypatch):
    checks = _checks(tmp_path)
    checks.settings = _settings(enabled=True)
    pcm = np.ones((441, 2), dtype=np.float32)
    calls = []
    forecast = checks.snapshot()
    monkeypatch.setattr(
        forecast, "render", lambda now: calls.append(now) or SimpleNamespace(audio=pcm)
    )
    captured = []
    assert (
        forecast.forecast_seconds(
            datetime(2026, 10, 11, 3, 5, tzinfo=UTC), scheduled=True, on_audio=captured.append
        )
        == 0.01
    )
    assert len(calls) == len(captured) == 1
    assert np.array_equal(captured[0], pcm)
    assert not np.shares_memory(captured[0], pcm)


def test_liner_callback_captures_same_selected_pcm_without_playing_live(tmp_path):
    _folder(tmp_path)
    pcm = np.ones((4410, 2), dtype=np.float32)
    scheduler = LinerScheduler(_cfg(liners_every_n_songs=1), tmp_path, None, decoder=lambda *_: pcm)
    captured = []
    events = scheduler.forecast(
        100, 109, on_audio=lambda at, name, audio: captured.append((at, name, audio))
    )
    assert len(events) == len(captured) == 1
    assert events[0][:2] == captured[0][:2]
    assert np.array_equal(captured[0][2], pcm)
    assert not np.shares_memory(captured[0][2], pcm)
    assert scheduler._fires == 0


def test_fixed_capture_owns_pcm_and_preserves_incoming_continuation(tmp_path):
    from tests.unit.test_hour_preview import _render

    source, _, gold = _station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    track = _render(gold[0], 200, gold[1], 5)
    track = replace(track, audio=np.ones((200, 2), np.float32), overlap_frames=50)
    player.bus = object()
    player._playing_render = track
    player._playback_pos[0] = 50
    player._pending_entry = gold[1]
    rows = hour_preview.read_station(player, now=NOW, capture_audio=True).items
    first, following = [r for r in rows if r.kind == "song"][:2]
    assert np.array_equal(first.audio, track.audio)
    assert not np.shares_memory(first.audio, track.audio)
    assert first.protected[-1][1] - first.protected[-1][0] == pytest.approx(50 / 44100, abs=1e-6)
    assert following.incoming_offset == track.next_start_offset


def test_ad_forecast_retains_owned_selected_spots_without_booking(tmp_path):
    from tests.unit.test_ads import _clock_step, booked_spots, log_lines, station

    fixture = station.__wrapped__(tmp_path)
    _player, _, desk, ads, clock, _break_id, _ = fixture
    before = ads.heard_snapshot()
    forecast = ads.forecast_step(_clock_step(), clock.now, list(before))
    assert forecast is not None
    assert len(forecast.spots) == 2
    for key, spot in forecast.spots:
        original = ads.store.cached().spots[key]
        assert spot == original and spot is not original
        assert (ads.store.folder / spot.file).is_file()
    assert ads.heard_snapshot() == before
    assert not booked_spots(desk) and not log_lines(ads)


def test_capture_reports_unavailable_scheduled_audio_and_continues(tmp_path):
    from datetime import time

    source, _, gold = _station(tmp_path, liners=[])
    _playing(source, gold[0], NOW - 50)

    def missing(*args):
        raise FileNotFoundError("unavailable")

    source.player.liner_scheduler = LinerScheduler(
        source.player._cfg.playback, tmp_path, None, decoder=missing
    )
    source.events = [_event("News", time=time(12, 11))]
    items, _, stopped = hour_preview.predict(
        source,
        snapshot_player(source.player),
        hour_preview.read_station(source.player, now=NOW),
        NOW + 500,
        seed=1,
        budget=30,
        capture_audio=True,
    )
    assert stopped is None
    assert any(row.duration_unknown and "News" in row.label for row in items)


def test_booked_handoff_and_fixed_voice_rows_are_never_independent_overlays(tmp_path):
    from tests.unit._break_station import make_break, make_station, start_station

    player, entries, _points, desk = make_station(tmp_path)
    _playing, ready, plan = start_station(player, entries)
    assert plan is not None
    booked = make_break(desk, plan.slot_id)
    desk._breaks[booked.break_id] = booked
    player.bus = object()
    player._playout._ready = ready
    rows = hour_preview.read_station(player, desk, now=1000, capture_audio=True).items
    booked_rows = [row for row in rows if row.status == "booked"]
    assert len(booked_rows) == 1
    assert booked_rows[0].in_handoff
    incoming = next(row for row in rows if row.entry is entries[3])
    assert incoming.incoming_handoff.breaks == (booked,)


def test_fixed_voice_capture_protects_both_duck_slopes_and_uses_owned_allocator(
    tmp_path, monkeypatch
):
    from autodj import stereo
    from autodj.playout import DUCK_RAMP_SECONDS
    from tests.unit.test_hour_preview import _render

    source, _, gold = _station(tmp_path)
    ident = source.library.entries_snapshot("liner")[0]
    ident = replace(ident, length=0.5)
    track = replace(
        _render(gold[0], 2, None, 0),
        audio=np.ones((2 * stereo.SAMPLE_RATE, 2), np.float32),
        start_offset=123,
        glide_in=SimpleNamespace(marker="owned"),
        liner_events=((stereo.SAMPLE_RATE, ident, False),),
        break_events=((stereo.SAMPLE_RATE, "b", round(1.5 * stereo.SAMPLE_RATE)),),
        intro_events=((stereo.SAMPLE_RATE, "i", round(1.5 * stereo.SAMPLE_RATE), "intro"),),
    )
    allocations = []
    real_allocate = stereo.audio_buffer

    def allocate(frames, channels=2):
        allocations.append((frames, channels))
        return real_allocate(frames, channels)

    monkeypatch.setattr(stereo, "audio_buffer", allocate)
    captured = hour_preview._captured_render(track, 100, source.player)
    assert allocations == [(len(track.audio), 2)]
    assert captured["incoming_offset"] == track.start_offset
    assert captured["incoming_glide"] == track.glide_in
    assert captured["incoming_glide"] is not track.glide_in
    assert all(
        first <= 101 - DUCK_RAMP_SECONDS and last >= 101.5 + DUCK_RAMP_SECONDS
        for first, last in captured["protected"]
    )
    assert not np.shares_memory(captured["audio"], track.audio)


def test_hard_event_capture_marks_cut_in_fixed_and_predicted_song(tmp_path):
    from datetime import time

    source, _, gold = _station(tmp_path, liners=[])
    _playing(source, gold[0], NOW - 50)
    source.player.liner_scheduler = LinerScheduler(
        source.player._cfg.playback,
        tmp_path,
        None,
        decoder=lambda *_: np.ones((441, 2), np.float32),
    )
    source.events = [_event("News", time=time(12, 11), timing="hard")]
    fixed = hour_preview.read_station(source.player, now=NOW, capture_audio=True)
    cut = hour_preview._before_interrupt(source, fixed, NOW + 500)
    assert cut.items[0].cut_at == NOW + 60
    source.events = [_event("News", time=time(12, 14), timing="hard")]
    rows, _, _ = hour_preview.predict(
        source,
        snapshot_player(source.player),
        fixed,
        NOW + 500,
        seed=1,
        budget=30,
        capture_audio=True,
    )
    assert any(row.kind == "song" and row.cut_at == NOW + 240 for row in rows)
    assert all(row.absolute_timing for row in rows if row.label == "Scheduled liner: News")
