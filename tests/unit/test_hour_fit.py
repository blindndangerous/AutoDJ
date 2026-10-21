"""Hour fitting changes choice only inside each picker's eligible pool."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from autodj.hour_fit import Target, fit_chooser, next_target
from autodj.player import Player
from autodj.scheduler import EventSpec
from tests.unit._rotation_station import category_step, make_station, song

START = datetime(2026, 10, 12, 12, 56, tzinfo=UTC).timestamp()


def test_next_target_uses_nearest_hour_restart_or_hard_event() -> None:
    hard = EventSpec.model_validate(
        {"name": "News", "action": "noop", "repeat": "daily", "time": "12:58", "timing": "hard"}
    )
    soft = hard.model_copy(
        update={"time": datetime.fromtimestamp(START, UTC).time(), "timing": "soft"}
    )
    assert next_target(START, 10, "UTC", None, ()) == Target(START + 240)
    assert next_target(START, 10, "UTC", None, (hard,)) == Target(START + 120, hard=True)
    assert next_target(START, 10, "UTC", START + 60, (hard,)) == Target(START + 60)
    assert next_target(START, 10, "UTC", START + 120, (hard,)) == Target(START + 120, True)
    assert next_target(START, 2, "UTC", None, (soft,)) is None


def test_voice_tracks_cannot_cross_hard_schedule_or_clock_restart() -> None:
    hard = EventSpec.model_validate(
        {"name": "News", "action": "noop", "repeat": "daily", "time": "12:58", "timing": "hard"}
    )
    player = SimpleNamespace(
        _cfg=SimpleNamespace(
            station=SimpleNamespace(timezone="UTC"),
            playback=SimpleNamespace(pick_mode="clock"),
        ),
        _hour_fit_events=(hard,),
        _hour_fit_timezone="UTC",
        _pending_clock_boundary=None,
        _clock_next_boundary=START + 180,
    )
    assert Player.voice_track_crosses_hard_boundary(player, START, 90) is False
    assert Player.voice_track_crosses_hard_boundary(player, START, 240) is True
    player._hour_fit_events = ()
    assert Player.voice_track_crosses_hard_boundary(player, START, 240) is True


def test_repeated_top_of_hour_is_a_target() -> None:
    before_fall_back = datetime(2026, 11, 1, 1, 55, tzinfo=ZoneInfo("America/Denver"))
    assert next_target(before_fall_back.timestamp(), 10, "America/Denver", None, ()) == Target(
        before_fall_back.timestamp() + 300
    )


def _choose(needed: float, lengths: list[float], *, hard: bool = False) -> float:
    return fit_chooser(needed, float, hard=hard)(lengths)


def test_a_song_ending_just_before_the_target_is_not_a_fit() -> None:
    # 30 seconds short leaves a gap the next whole song overruns by minutes;
    # 40 seconds over is the nearer landing.
    assert _choose(240, [210, 280]) == 280


def test_early_handoff_lands_on_the_hour_but_not_on_a_hard_event() -> None:
    assert _choose(240, [234, 252]) == 234
    assert _choose(240, [234, 252], hard=True) == 252


def test_far_from_the_target_it_leaves_room_for_whole_songs() -> None:
    # 520 seconds out with 200 to 260 second songs: a 260 leaves 260 for one
    # more, where the longest fit alone would leave 140 that no song fills.
    assert _choose(520, [200, 380, 260]) == 260


def test_unknown_lengths_never_win_and_ties_keep_picker_order() -> None:
    assert _choose(240, [0, 240]) == 240
    assert _choose(240, [0, 1]) == 0
    assert _choose(240, [230, 250]) == 230


@pytest.mark.parametrize("mode", ["random", "similarity", "clock"])
def test_fit_chooses_closest_eligible_song(tmp_path: Path, mode: str) -> None:
    current = song(0, "Current", length=200)
    short = song(1, "Short", length=210)
    exact = song(2, "Exact", length=240)
    long = song(3, "Long", length=280)
    choices = [short, exact, long]
    wheel = [{"name": "Main", "steps": [category_step("pool")]}] if mode == "clock" else []
    source = make_station(
        tmp_path,
        [current, *choices],
        categories=[{"id": "pool", "name": "Pool", "members": choices}],
        clockwheels=wheel,
        active_clock="Main" if wheel else None,
        playback={"pick_mode": mode, "hour_fit": True},
        similarity=mode == "similarity",
    )
    player = source.player
    player._now = lambda: START - 200
    player._state.clock = player._now
    player._state.current_track = current
    context = player._pick_context(at=START)

    def choose(context):
        if mode == "clock":
            return player._choose_station_next(current, context)[0]
        return player._choose_next(current, context)[0]

    assert choose(context).path == exact.path
    blocked = replace(context, protected=context.protected | {exact.path})
    # Short would hand off 30 seconds before the hour and leave a gap.
    assert choose(blocked).path == long.path


def test_fit_scores_output_duration_at_music_speed(tmp_path: Path) -> None:
    short = song(1, "Short", length=100)
    exact = song(2, "Exact", length=110)
    source = make_station(
        tmp_path,
        [short, exact],
        playback={"pick_mode": "random", "hour_fit": True, "music_speed_percent": 10},
    )
    at = datetime(2026, 10, 12, 12, 58, 20, tzinfo=UTC).timestamp()
    fit = source.player._hour_fit_choice(SimpleNamespace(at=at))

    assert fit is not None
    assert source.player.music_playback_rate(exact) == 1.1
    assert fit([short, exact]) is exact


def test_discovery_fits_only_within_its_distant_pool(tmp_path: Path) -> None:
    music = [song(n, f"Artist {n}", length=150 + n * 15) for n in range(12)]
    source = make_station(tmp_path, music, similarity=True)
    sim = source.player._sim
    query = sim.faiss_index.reconstruct(0).reshape(1, -1)
    _scores, indices = sim.faiss_index.search(query, sim.ntotal)
    distant = [sim.entries[int(index)] for index in indices[0][int(sim.ntotal * 0.75) :]]
    expected = max(distant, key=lambda entry: entry.length)
    chosen = sim.find_distant(
        music[0].path,
        deque([music[0].path]),
        fit=lambda entries: max(entries, key=lambda entry: entry.length),
    )
    assert chosen.path == expected.path
