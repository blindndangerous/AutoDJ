"""Repeat-rule accounting for songs picked when a rotation starts fresh."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from autodj.player import Player
from autodj.rotation_sim import SimulationRequest, simulate
from tests.unit._rotation_station import category_step, liner, liner_step, make_station, song

START = datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp()


def _clock_station(root: Path, *, enforce_rules: bool = True, with_liner: bool = False):
    track = song(1, "Solo")
    music = [track, *(song(number, f"Artist {number}") for number in range(2, 7))]
    steps = ([liner_step()] if with_liner else []) + [
        category_step("only", enforce_rules=enforce_rules)
    ]
    return make_station(
        root,
        music,
        liners=[liner(1)] if with_liner else (),
        categories=[{"id": "only", "name": "Only", "members": music[:2]}],
        clockwheels=[{"name": "Only", "steps": steps}],
        active_clock="Only",
        playback={
            "pick_mode": "clock",
            "repeat_rules": {"track": {"tracks": 50}, "artist": {"tracks": 0}},
        },
    )


def test_opening_repeat_is_counted_when_the_clock_step_enforces_rules(tmp_path: Path) -> None:
    source = _clock_station(tmp_path)
    music = source.library.entries_snapshot()
    source.player._state.recently_played.extend([music[0].path, music[1].path])

    report = simulate(source, SimulationRequest(picks=1, start=START, seed=3))

    assert report["relaxed"]["track repeat window"] == 1
    assert "track repeat window" not in report["ignored_by_steps"]


def test_opening_repeat_is_counted_as_ignored_when_the_clock_step_has_no_rules(
    tmp_path: Path,
) -> None:
    source = _clock_station(tmp_path, enforce_rules=False)
    music = source.library.entries_snapshot()
    source.player._state.recently_played.extend([music[0].path, music[1].path])

    report = simulate(source, SimulationRequest(picks=1, start=START, seed=3))

    assert report["ignored_by_steps"]["track repeat window"] == 1
    assert "track repeat window" not in report["relaxed"]


def test_fresh_restart_counts_the_opening_repeat_too(tmp_path: Path, monkeypatch) -> None:
    source = _clock_station(tmp_path)
    music = source.library.entries_snapshot()
    source.player._state.recently_played.extend([music[0].path, music[1].path])
    choose = Player._choose_station_next
    calls = 0

    def fail_first_pick(self, current, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("forced pick failure")
        return choose(self, current, context)

    monkeypatch.setattr(Player, "_choose_station_next", fail_first_pick)

    report = simulate(source, SimulationRequest(picks=2, start=START, seed=3))

    assert report["dead_air"]["failed_picks"] == 1
    assert report["relaxed"]["track repeat window"] == 2


def test_a_clean_opening_song_does_not_add_a_repeat_violation(tmp_path: Path) -> None:
    report = simulate(_clock_station(tmp_path), SimulationRequest(picks=1, start=START, seed=3))

    assert "track repeat window" not in report["relaxed"]
    assert "track repeat window" not in report["ignored_by_steps"]


def test_an_opening_liner_is_not_counted_as_a_repeated_song(tmp_path: Path) -> None:
    source = _clock_station(tmp_path, with_liner=True)
    voice = source.library.entries_snapshot("liner")[0]
    other = source.library.entries_snapshot()[1]
    source.player._state.recently_played.extend([voice.path, other.path])

    report = simulate(source, SimulationRequest(picks=1, start=START, seed=3))

    assert report["liners"] >= 1
    assert "track repeat window" not in report["relaxed"]
    assert "track repeat window" not in report["ignored_by_steps"]
