"""The break desk, speech trimming, placement and the [breaks] settings (#88)."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest

from autodj.breaks import (
    AIRED,
    DROPPED,
    EXPIRED,
    ON_AIR,
    QUEUED,
    RENDERED,
    BreakDesk,
    BreakError,
    file_name,
    new_id,
    placement_for,
    speech_cues,
    valid_id,
)
from autodj.config import BreaksConfig
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._break_station import make_break


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def desk(tmp_path: Path):
    clock = Clock()
    config = BreaksConfig(enabled=True, max_queued=3, max_per_slot=2)
    made = BreakDesk(lambda: config, tmp_path / "announcements", clock=clock)
    made.folder.mkdir()
    return made, clock, config


# ---------------------------------------------------------------------------
# Speech cues
# ---------------------------------------------------------------------------


def test_speech_cues_trim_the_silence_around_the_speech_only():
    audio = np.zeros((5 * SR, 2), dtype=np.float32)
    audio[SR : 2 * SR, 0] = 0.3
    audio[round(2.5 * SR) : 3 * SR, 1] = 0.3  # a pause inside the speech stays
    cues = speech_cues(audio)
    assert cues.start == pytest.approx(0.95, abs=0.021)
    assert cues.segue == pytest.approx(3.0, abs=0.021)
    assert cues.end == pytest.approx(cues.segue + 0.4, abs=1e-3)
    assert cues.length == 5.0
    assert cues.speech == pytest.approx(2.05, abs=0.03)
    assert cues.points(True)["overlap_start"] == 0.0
    assert cues.points(False)["overlap_start"] is None


def test_speech_cues_keep_the_end_inside_the_file():
    audio = np.full((2 * SR, 2), 0.2, dtype=np.float32)
    cues = speech_cues(audio)
    assert (cues.start, cues.segue, cues.end) == (0.0, 2.0, 2.0)


@pytest.mark.parametrize(
    "audio",
    [
        np.zeros((SR, 2), dtype=np.float32),
        np.zeros((0, 2), dtype=np.float32),
        np.full((SR // 10, 2), 0.5, dtype=np.float32),
        np.full((SR, 2), np.nan, dtype=np.float32),
    ],
    ids=["silent", "empty", "too short", "not finite"],
)
def test_speech_cues_refuse_unusable_audio(audio):
    with pytest.raises(ValueError):
        speech_cues(audio)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "requested", "setting", "intro", "expected"),
    [
        ("dj", "auto", "auto", 5.0, "talk_up"),
        ("dj", "auto", "auto", 1.0, "standalone"),
        ("dj", "auto", "auto", None, "standalone"),
        ("dj", "auto", "standalone", 9.0, "standalone"),
        ("dj", "auto", "talk_up", 1.0, "talk_up"),
        ("dj", "talk_up", "standalone", 1.0, "talk_up"),
        ("dj", "talk_up", "auto", None, "standalone"),
        ("dj", "talk_up", "auto", 0.0, "standalone"),
        ("dj", "standalone", "auto", 9.0, "standalone"),
        ("news", "auto", "talk_up", 9.0, "standalone"),
        ("weather", "auto", "auto", 9.0, "standalone"),
        ("music_news", "auto", "auto", 9.0, "standalone"),
    ],
)
def test_placement_follows_the_request_the_setting_and_the_intro(
    kind, requested, setting, intro, expected
):
    placement, note = placement_for(kind, requested, setting, intro, 2.0)
    assert placement == expected
    assert bool(note) == (
        kind == "dj"
        and expected == "standalone"
        and requested != "standalone"
        and setting != "standalone"
    )


# ---------------------------------------------------------------------------
# The desk
# ---------------------------------------------------------------------------


def test_ids_and_file_names():
    made = new_id()
    assert valid_id(made) and not valid_id("x" * 32) and not valid_id(None)
    assert file_name(made, ".WAV") == f"brk-{made}.wav"


def test_a_break_passes_through_its_states_and_its_file_goes(desk, caplog):
    desk, clock, _config = desk
    slot = new_id()
    item = make_break(desk, slot)
    desk.add(item)
    assert desk.get(item.break_id).state == QUEUED
    assert desk.waiting_in(slot) == 1
    taken = desk.take(slot, clock.now + 30)
    assert [t.break_id for t in taken] == [item.break_id]
    assert desk.get(item.break_id).state == RENDERED
    assert desk.get(item.break_id).eta == clock.now + 30
    desk.mixed_as(item.break_id, "talk_up")
    clock.now += 30
    desk.on_air(item.break_id)
    assert desk.get(item.break_id).state == ON_AIR
    desk.aired(item.break_id)
    finished = desk.get(item.break_id)
    assert finished.state == AIRED and finished.aired_at == clock.now
    view = finished.view()
    assert view["mixed_as"] == "talk_up"
    assert "path" not in view and str(item.path) not in str(view)
    assert item.path.exists()
    assert desk.needs_maintenance()
    with caplog.at_level(logging.INFO, logger="autodj.breaks"):
        desk.maintain()
    assert not item.path.exists()
    assert not desk.needs_maintenance()


def test_cancel_only_while_waiting(desk):
    desk, clock, _config = desk
    slot = new_id()
    first, second = make_break(desk, slot), make_break(desk, slot)
    desk.add(first)
    desk.add(second)
    cancelled = desk.cancel(first.break_id)
    assert cancelled.state == DROPPED and cancelled.reason == "cancelled by the announcer"
    desk.take(slot, clock.now)
    with pytest.raises(BreakError) as mixed:
        desk.cancel(second.break_id)
    assert mixed.value.status == 409 and "mixed into the stream" in mixed.value.detail
    with pytest.raises(BreakError) as missing:
        desk.cancel(new_id())
    assert missing.value.status == 404
    with pytest.raises(BreakError) as finished:
        desk.cancel(first.break_id)
    assert finished.value.status == 409


def test_room_is_capped_per_slot_per_station_and_by_quota(desk):
    desk, _clock, config = desk
    slot = new_id()
    desk.add(make_break(desk, slot))
    desk.add(make_break(desk, slot))
    with pytest.raises(BreakError) as full_slot:
        desk.check_room(slot)
    assert (full_slot.value.status, full_slot.value.reason) == (409, "slot_full")
    desk.add(make_break(desk, new_id()))
    with pytest.raises(BreakError) as full:
        desk.add(make_break(desk, new_id()))
    assert (full.value.status, full.value.reason) == (429, "queue_full")
    with pytest.raises(BreakError) as quota:
        BreakDesk(lambda: config, desk.folder).check_room(new_id(), config.quota_bytes + 1)
    assert (quota.value.status, quota.value.reason) == (507, "quota")


def test_a_late_break_expires_with_a_reason(desk):
    desk, clock, _config = desk
    slot = new_id()
    late = make_break(desk, slot, expires_at=clock.now + 60)
    waiting = make_break(desk, new_id(), expires_at=clock.now + 10)
    desk.add(late)
    desk.add(waiting)
    # Its song change would air after it expires.
    assert desk.take(slot, clock.now + 61) == []
    assert desk.get(late.break_id).state == EXPIRED
    assert "would air after it expires" in desk.get(late.break_id).reason
    clock.now += 11
    assert desk.needs_maintenance()
    desk.maintain()
    assert desk.get(waiting.break_id).state == EXPIRED
    assert not late.path.exists() and not waiting.path.exists()


def test_take_puts_standalone_kinds_before_a_dj_break(desk):
    desk, clock, _config = desk
    slot = new_id()
    dj = make_break(desk, slot, kind="dj", created_at=1.0)
    news = make_break(desk, slot, kind="news", created_at=2.0)
    desk.add(dj)
    desk.add(news)
    assert [item.kind for item in desk.take(slot, clock.now)] == ["news", "dj"]


def test_drops_and_their_reasons(desk):
    desk, clock, _config = desk
    earlier, later = new_id(), new_id()
    playing, waiting, dropped = (
        make_break(desk, earlier),
        make_break(desk, later),
        make_break(desk, later),
    )
    desk.add(playing)
    desk.take(earlier, clock.now)
    assert desk.drop_slot(earlier, "gone") == 0  # nothing waits in it any more
    desk.on_air(playing.break_id)
    desk.add(waiting)
    desk.add(dropped)
    assert desk.drop_slot(later, "the song before it changed") == 2
    assert desk.get(dropped.break_id).reason == "the song before it changed"
    desk.add(make_break(desk, new_id()))
    assert desk.drop_unaired("the station stopped") == 2
    assert desk.get(playing.break_id).reason == "cut short: the station stopped"
    desk.drop(waiting.break_id, "again")  # finished breaks stay as they are
    assert desk.get(waiting.break_id).reason == "the song before it changed"
    desk.on_air(waiting.break_id)
    desk.aired(waiting.break_id)
    assert desk.get(waiting.break_id).state == DROPPED
    assert {item.state for item in desk.listing()} == {DROPPED}


def test_the_first_upkeep_sweeps_files_a_previous_run_left(desk):
    desk, _clock, _config = desk
    leftover = desk.folder / f"brk-{new_id()}.mp3"
    part = desk.folder / f".brk-{new_id()}.wav.0123456789abcdef.part"
    other = desk.folder / "keep-me.wav"
    for path in (leftover, part, other):
        path.write_bytes(b"x")
    item = make_break(desk, new_id())
    desk.add(item)
    assert desk.needs_maintenance()
    desk.maintain()
    assert not leftover.exists() and not part.exists()
    assert other.exists() and item.path.exists()


def test_history_is_bounded(desk, monkeypatch):
    desk, _clock, _config = desk
    monkeypatch.setattr("autodj.breaks.HISTORY", 2)
    for _ in range(4):
        item = make_break(desk, new_id())
        desk.add(item)
        desk.cancel(item.break_id)
    desk.maintain()
    assert len(desk.listing()) == 2


def test_disabled_unless_exactly_true(tmp_path):
    assert not BreakDesk(lambda: BreaksConfig(), tmp_path).enabled
    assert BreakDesk(lambda: BreaksConfig(enabled=True), tmp_path).enabled


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_breaks_settings_defaults_and_conversions():
    config = BreaksConfig.from_dict({"enabled": True, "min_intro_seconds": 3, "duck_db": -6})
    assert config.enabled and config.min_intro_seconds == 3.0 and config.duck_db == -6.0
    assert config.max_upload_bytes == 40 * 1024 * 1024
    assert config.quota_bytes == 400 * 1024 * 1024
    assert BreaksConfig().dj_placement == "auto"


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"enabled": "yes"}, TypeError),
        ({"dj_placement": "over"}, ValueError),
        ({"min_intro_seconds": "2"}, TypeError),
        ({"min_intro_seconds": 61}, ValueError),
        ({"max_seconds": float("nan")}, ValueError),
        ({"duck_db": 3}, ValueError),
        ({"duck_db": True}, TypeError),
        ({"max_queued": 0}, ValueError),
        ({"max_queued": 2.5}, TypeError),
        ({"max_per_slot": 6}, ValueError),
        ({"default_expires_seconds": 9000}, ValueError),
        ({"quota_mb": 10}, ValueError),
    ],
)
def test_breaks_settings_are_checked(values, error):
    with pytest.raises(error):
        BreaksConfig(**values)
