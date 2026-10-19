"""Prepared joins must affect the timeline used by contextual review."""

from datetime import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from autodj.hour_preview import Decided, Item, predict
from autodj.player import Player
from autodj.rotation_sim import snapshot_player
from autodj.shows import prepared_break
from tests.unit.test_hour_preview import NOW, _event, _station


@pytest.mark.parametrize("placement,delay", [("standalone", 4.0), ("talk_up", 0.0)])
@pytest.mark.parametrize("mode", ["saved_points", "fixed"])
def test_prepared_join_changes_forecast_before_next_automatic_speech(
    tmp_path, monkeypatch, placement, delay, mode
):
    source, _, songs = _station(tmp_path, liners=[], playback={"transition_mode": mode})
    first, second = songs[:2]
    monkeypatch.setattr(
        source.library.catalog,
        "points",
        lambda _path: {
            "start": 0,
            "segue": 100,
            "outro": 100,
            "end": 200,
            "intro": 20,
        },
    )
    take = prepared_break(
        SimpleNamespace(
            take_id="a" * 32,
            path=Path("take.wav"),
            seconds=4,
            placement=placement,
            allow_outro=False,
            label="Prepared take",
        )
    )
    calls = []
    source.player.prepared_handoff = lambda before, after: (
        (take,) if before is first and after is second else ()
    )
    source.player.prepared_preview_handoff = source.player.prepared_handoff
    source.player.prepared_handoff = lambda *_: pytest.fail("Preview called the live handoff")
    monkeypatch.setattr(
        Player,
        "_load_voice_audio",
        lambda self, path, limit: (
            calls.append((path, limit)) or np.full((4 * 44100, 2), 0.2, np.float32)
        ),
    )
    # A scheduled time check dispatched at the next handoff observes the actual
    # prepared placement, rather than the pre-speech song boundary.
    checks = []
    source.events = [_event("Check", action="time_check", params={}, time=time(12, 11))]
    monkeypatch.setattr(
        "autodj.hour_preview._scheduled_audio",
        lambda source, player, bridge, event, at, capture_audio=False: checks.append(at),
    )
    decided = Decided(
        now=NOW,
        handoff=NOW,
        waiting=tuple(Item("song", NOW, 0, "queued", entry) for entry in (first, second)),
    )
    rows, _, _ = predict(
        source,
        snapshot_player(source.player),
        decided,
        NOW + 500,
        seed=1,
        limit=2,
        capture_audio=True,
    )
    music = [row for row in rows if row.kind == "song"]
    assert music[1].starts == pytest.approx(NOW + 100 + delay)
    assert music[1].incoming_handoff.breaks == (take,)
    speech = next(row for row in rows if row.label == "Prepared take")
    assert speech.in_handoff and speech.seconds == pytest.approx(4)
    assert calls == [(Path("take.wav"), 4.0)]
    assert checks == [NOW + 100]
    assert source.player._state.track_number == 0


@pytest.mark.parametrize("prefix", ["edition:", "edition-confirmed:"])
def test_scheduled_prepared_edition_stops_honestly(tmp_path, prefix):
    source, _, songs = _station(tmp_path, liners=[])
    source.events = [
        _event(
            "Prepared programme",
            action="show",
            params={"show": prefix + "a" * 32},
            time=time(12, 11),
        )
    ]
    decided = Decided(now=NOW, last=songs[0], handoff=NOW + 100)
    rows, _, stopped = predict(
        source, snapshot_player(source.player), decided, NOW + 500, seed=1, capture_audio=True
    )
    assert rows == []
    assert "scheduled prepared edition" in stopped
    assert "cannot yet be predicted" in stopped


def test_preview_without_read_only_callback_never_calls_live_handoff(tmp_path):
    source, _, songs = _station(tmp_path, liners=[])
    source.player.prepared_handoff = lambda *_: pytest.fail("Preview called live handoff")
    decided = Decided(
        now=NOW,
        handoff=NOW,
        waiting=tuple(Item("song", NOW, 0, "queued", entry) for entry in songs[:2]),
    )
    rows, _, _ = predict(
        source,
        snapshot_player(source.player),
        decided,
        NOW + 500,
        seed=1,
        limit=2,
        capture_audio=True,
    )
    assert len([row for row in rows if row.kind == "song"]) == 2
