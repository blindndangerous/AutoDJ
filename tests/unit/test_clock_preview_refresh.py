"""Clockwheel forecasts keep eligible picks across timing changes (#604)."""

from dataclasses import replace

import pytest

from autodj import hour_preview as hp
from tests.unit._rotation_station import category_step
from tests.unit.test_hour_preview import NOW, _playing, _station


@pytest.mark.parametrize("selection", ["random", "least_recent"])
def test_a_step_returns_with_its_song_after_the_hour_restart_omits_it(
    tmp_path, monkeypatch, selection
):
    """ADR-0009-11."""
    steps = [dict(category_step(name), selection=selection) for name in ("power", "gold")]
    source, power, gold = _station(
        tmp_path, clockwheels=[{"name": "Main", "resync_minutes": [0], "steps": steps}]
    )
    player = source.player
    now = NOW + 2700  # 12:55 UTC; the restart is at 13:00.
    _playing(source, power[0], now - 50)
    boundary = now + 300
    player._clock = {**player._clock, "cursor": 1}
    player._clock_next_boundary = boundary
    decided = replace(
        hp.read_station(player, now=now),
        handoff=boundary - 150,
        clock_cursor=1,
        clock_name="Main",
        clock_boundary=boundary,
    )
    monkeypatch.setattr(hp, "read_station", lambda *args, **kwargs: decided)
    previews = hp.HourPreviews()

    def forecast():
        previews.get(source, 15, force=True)
        return previews._clock_choices[(source.station, 15)]

    original = forecast()
    position, path, _ = original.realized[0]
    assert position.step == 1 and position.boundary == boundary
    assert path in {entry.path for entry in gold}

    # Lengthening the current song moves its handoff too close to the hour.
    decided = replace(decided, handoff=boundary - 50)
    for _ in range(2):
        omitted = forecast()
        assert omitted.paths[position] == path
        assert all(key != position for key, _, _ in omitted.realized)
        assert omitted.realized[0][0].step == 0
        assert omitted.realized[0][0].boundary != boundary

    decided = replace(decided, handoff=boundary - 150)
    returned = forecast()
    assert returned.realized[0][:2] == (position, path)

    # A returning preference still has to satisfy the current step membership.
    monkeypatch.setattr(
        player,
        "playable_entries",
        lambda: [entry for entry in [*power, *gold] if entry.path != path],
    )
    # Snapshot selection reads the shared library, so remove the entry there too.
    entries_snapshot = source.library.entries_snapshot
    monkeypatch.setattr(
        source.library,
        "entries_snapshot",
        lambda kind="music": [entry for entry in entries_snapshot(kind) if entry.path != path],
    )
    changed = forecast()
    assert changed.realized[0][1] != path


@pytest.mark.parametrize("selection", ["random", "least_recent"])
def test_fixed_prefix_advancement_keeps_the_next_lap_distinct(tmp_path, monkeypatch, selection):
    """ADR-0009-11."""
    steps = [dict(category_step(name), selection=selection) for name in ("power", "gold")]
    source, power, _gold = _station(
        tmp_path, clockwheels=[{"name": "Main", "resync_minutes": [], "steps": steps}]
    )
    player = source.player
    _playing(source, power[0], NOW - 50)
    player._clock = {**player._clock, "cursor": 1}
    decided = replace(hp.read_station(player, now=NOW), clock_cursor=1, clock_name="Main")
    monkeypatch.setattr(hp, "read_station", lambda *args, **kwargs: decided)
    previews = hp.HourPreviews()
    previews.get(source, 30, force=True)
    original = previews._clock_choices[(source.station, 30)]
    first, second = original.realized[:2]
    assert first[0].lap == 0 and second[0].lap == 1
    fixed = source.library.entry_for_path(first[1])
    decided = replace(
        decided,
        items=(*decided.items, hp.Item("song", decided.handoff, 200, "picked", fixed)),
        unplayed=((fixed, decided.handoff),),
        last=fixed,
        handoff=decided.handoff + 200,
        clock_cursor=0,
    )
    previews.get(source, 30, force=True)
    refreshed = previews._clock_choices[(source.station, 30)]
    assert refreshed.realized[0][:2] == second[:2]
    assert first[0] not in refreshed.paths
