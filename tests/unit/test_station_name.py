"""The station name while a clockwheel or show is on the air (#472)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from autodj._bridge import PlayerBridge
from autodj.clockwheel_store import ClockwheelStore, validate_clockwheel
from autodj.nowplaying import NowPlaying
from autodj.player import Player
from autodj.repeat_rules import PlayRecord
from autodj.scheduler_api import _builtin_actions
from autodj.shows import ShowError, ShowRunner, ShowStore, parse_spec, with_songs
from autodj.station_name import listener_name, sync_name
from autodj.stations import StationPaths
from tests.unit._rotation_station import category_step, make_station, song

NORMAL = "Example FM"
NIGHT = "Example FM Night Shift"
LIVE = "Example FM Live"


def _clock(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "Night",
        "steps": [{"type": "category", "category": "hits"}],
        "resync_minutes": [0],
        **changes,
    }


class _Published:
    """Counts the feed refreshes and relay reconnects a name change makes."""

    def __init__(self) -> None:
        self.refreshed = 0
        self.reconnected = 0

    def refresh(self) -> None:
        self.refreshed += 1

    def reconnect(self) -> None:
        self.reconnected += 1


def _station(tmp_path: Path) -> tuple[Any, PlayerBridge, list[Any]]:
    """Day (no station name) and Night (its own) clockwheels; Day picks the songs."""
    hits = [song(n, f"Hits {n}", length=180.0) for n in range(12)]
    source = make_station(
        tmp_path,
        hits,
        categories=[{"id": "hits", "name": "Hits", "members": hits}],
        clockwheels=[
            {"name": "Day", "steps": [category_step("hits")]},
            {"name": "Night", "steps": [category_step("hits")], "station_name": NIGHT},
        ],
        active_clock="Day",
        playback={"pick_mode": "clock"},
    )
    source.player._cfg.stream.station_name = NORMAL
    bridge = PlayerBridge(
        player=source.player, sim=source.player._sim, library=source.library, ledger=source.ledger
    )
    return source, bridge, hits


def _select(source: Any, name: str | None) -> None:
    """What Activate in the clockwheel editor does once the choice is saved."""
    source.library.catalog.settings["clock"] = {"name": name, "cursor": 0}
    source.player.configure_clock_after_selection()


def _playing(source: Any, entry: Any) -> None:
    state = source.player._state
    state.current_track = entry
    state.record_played(entry)
    state.play_history[-1] = PlayRecord.of(entry, time.time())


def _settle(runner: ShowRunner) -> None:
    async def run() -> None:
        runner.track_changed()
        await asyncio.sleep(0)
        for task in list(runner._tasks):
            await task

    asyncio.run(run())


# -- saving the name ------------------------------------------------------------


def test_a_clockwheel_saves_its_station_name_only_when_it_has_one(tmp_path: Path) -> None:
    """ADR-0009-08: a blank name is not written, so older wheels and older AutoDJs agree."""
    store = ClockwheelStore(tmp_path / "clockwheels")
    saved = store.save(_clock(station_name=f"  {NIGHT}  "))
    assert saved["station_name"] == NIGHT
    assert store.load("Night") == saved
    blank = store.save(_clock(station_name="   "))
    assert "station_name" not in blank
    assert "station_name" not in json.loads((store.root / "Night.json").read_text("utf-8"))
    # A wheel saved before the field existed still loads.
    assert "station_name" not in validate_clockwheel(_clock())
    for value, message in (
        ("x" * 101, "at most 100 characters"),
        ("Night\tShift", "control characters"),
        (7, "must be text"),
    ):
        with pytest.raises(ValueError, match=f"^Station name while active.*{message}"):
            validate_clockwheel(_clock(station_name=value))
    with pytest.raises(ValueError, match="unknown"):
        validate_clockwheel(_clock(station=NIGHT))


def test_a_show_saves_its_station_name_and_names_the_field_it_refuses(tmp_path: Path) -> None:
    """ADR-0009-08: the same rules as the clockwheel's, and a blank name is not written."""
    spec = {"name": "Late show", "source": "category", "category": "hits"}
    assert parse_spec({**spec, "station_name": f" {LIVE} "}).station_name == LIVE
    assert parse_spec(spec).station_name == ""
    for value in ("Live\nnow", "x" * 101, 5):
        with pytest.raises(ShowError) as refused:
            parse_spec({**spec, "station_name": value})
        assert refused.value.field == "station_name"
        assert refused.value.message.startswith("Station name while active")
    store = ShowStore(tmp_path / "shows.json")
    named, _show = store.add(parse_spec({**spec, "station_name": LIVE}))
    plain, _show = store.add(parse_spec({**spec, "name": "Morning show"}))
    saved = json.loads((tmp_path / "shows.json").read_text("utf-8"))["shows"]
    assert saved[named]["station_name"] == LIVE
    assert "station_name" not in saved[plain]


# -- on the air -------------------------------------------------------------------


def test_activating_a_clockwheel_by_hand_names_the_station(tmp_path: Path) -> None:
    """ADR-0009-08: the wheel's name is published everywhere; the normal name stays saved."""
    source, bridge, _hits = _station(tmp_path)
    published = _Published()
    feed = NowPlaying(bridge, timezone="UTC")

    def sync() -> bool:
        return sync_name(bridge, None, published.refresh, published.reconnect)

    # Day has no name of its own: nothing changes.
    assert sync() is False
    assert listener_name(bridge) == NORMAL
    _select(source, "Night")
    assert sync() is True
    assert listener_name(bridge) == NIGHT
    assert feed.refresh()["station_name"] == NIGHT
    assert (published.refreshed, published.reconnected) == (1, 1)
    # Once published, the same name is not published again.
    assert sync() is False
    assert published.reconnected == 1
    # The operator's saved name is untouched.
    assert source.player._cfg.stream.station_name == NORMAL
    assert bridge.get_settings()["playback"]["stream_station_name"] == NORMAL
    # A blank wheel restores the normal name with no second event.
    _select(source, "Day")
    assert sync() is True
    assert listener_name(bridge) == NORMAL
    assert feed.refresh()["station_name"] == NORMAL
    # Turning the clockwheel off does too.
    _select(source, "Night")
    sync()
    _select(source, None)
    assert sync() is True and listener_name(bridge) == NORMAL


def test_the_schedule_switching_clockwheels_names_the_station(tmp_path: Path) -> None:
    """ADR-0009-08: "Change song picking" carries the wheel's name and leaves it for others."""
    source, bridge, _hits = _station(tmp_path)
    published = _Published()

    async def select(name: str) -> None:
        _select(source, name)

    async def profile(_name: str) -> None:
        return None

    actions = {
        action.name: action
        for action in _builtin_actions(
            bridge, profile, list, list, lambda: ["Day", "Night"], select
        )
    }
    picking = actions["pick_mode"]

    def run(mode: str) -> str:
        asyncio.run(picking.handle({"mode": mode}))
        sync_name(bridge, None, published.refresh, published.reconnect)
        return listener_name(bridge)

    assert run("clock:Night") == NIGHT
    assert run("clock:Day") == NORMAL
    assert run("clock:Night") == NIGHT
    # Another way of picking songs takes the wheel off the air.
    assert run("similarity") == NORMAL
    # The active wheel again: its name comes back.
    assert run("clock") == NIGHT
    assert published.reconnected == 5
    saved = json.loads(StationPaths.default(source.player._cfg).web_state.read_text("utf-8"))
    assert saved["playback"]["stream_station_name"] == NORMAL


def test_saving_only_the_name_of_the_wheel_on_the_air_keeps_its_place(tmp_path: Path) -> None:
    """ADR-0009-08: the name applies at once and does not restart the wheel."""
    source, _bridge, _hits = _station(tmp_path)
    player = source.player
    _select(source, "Night")
    player.activate_clock("Night", 1)
    revision = player._clock_revision
    store = ClockwheelStore(StationPaths.default(player._cfg).clockwheels)
    store.save({**store.load("Night"), "station_name": "Example FM After Dark"})
    # Even "at the next clock restart" is for the steps, not the name.
    player.apply_clockwheel_update("Night", "next_restart")
    assert player.clock_station_name() == "Example FM After Dark"
    assert player._clock["cursor"] == 1
    assert player._clock_revision == revision
    assert player._pending_clock_update is None
    assert "station_name" not in player._clock_definition


def test_a_show_on_the_air_names_the_station_before_the_clockwheel(tmp_path: Path) -> None:
    """ADR-0009-08: show first, then the wheel; a blank show name leaves the wheel's."""
    source, bridge, hits = _station(tmp_path)
    published = _Published()
    _select(source, "Night")
    _playing(source, hits[0])
    store = ShowStore(tmp_path / "shows.json")
    show_id, _show = store.add(
        parse_spec(
            {"name": "Late show", "source": "category", "category": "hits", "station_name": LIVE}
        )
    )
    store.update(show_id, lambda show: with_songs(show, [entry.path for entry in hits[4:8]]))

    async def publish(_message: dict) -> None:
        return None

    runner = ShowRunner(bridge, store, publish, state_path=tmp_path / "show_on_air.json")

    def sync() -> str:
        sync_name(bridge, runner, published.refresh, published.reconnect)
        return listener_name(bridge)

    assert sync() == NIGHT
    asyncio.run(runner.start(show_id))
    assert sync() == LIVE
    # A name saved while it airs applies at once; a blank one leaves the wheel's.
    store.update(show_id, lambda show: show.model_copy(update={"station_name": ""}))
    assert sync() == NIGHT
    store.update(show_id, lambda show: show.model_copy(update={"station_name": LIVE}))
    assert sync() == LIVE
    # Off the air, the wheel's name is back.
    asyncio.run(runner.stop())
    assert runner.on_air_station_name() == ""
    assert sync() == NIGHT


def test_after_a_restart_the_name_matches_what_is_on_the_air(tmp_path: Path) -> None:
    """ADR-0009-08: the saved wheel and the resumed show name the station again."""
    source, bridge, hits = _station(tmp_path)
    _select(source, "Night")
    _playing(source, hits[0])
    store = ShowStore(tmp_path / "shows.json")
    show_id, _show = store.add(
        parse_spec(
            {"name": "Late show", "source": "category", "category": "hits", "station_name": LIVE}
        )
    )
    store.update(show_id, lambda show: with_songs(show, [entry.path for entry in hits[4:8]]))

    async def publish(_message: dict) -> None:
        return None

    state = tmp_path / "show_on_air.json"
    asyncio.run(ShowRunner(bridge, store, publish, state_path=state).start(show_id))
    # A restart: a new player loads the saved wheel; a new bridge has no name yet.
    player = Player(source.player._cfg, source.player._sim, dry_run=True)
    player._library = source.library
    player.configure_clock()
    _playing(SimpleSource(player), hits[1])
    again = PlayerBridge(player=player, sim=player._sim, library=source.library)
    runner = ShowRunner(again, store, publish, state_path=state)
    published = _Published()
    sync_name(again, runner, published.refresh, published.reconnect)
    # The show waits for the next song to resume; until then the wheel names it.
    assert listener_name(again) == NIGHT
    _settle(runner)
    sync_name(again, runner, published.refresh, published.reconnect)
    assert listener_name(again) == LIVE


class SimpleSource:
    """Just the player, for :func:`_playing`."""

    def __init__(self, player: Any) -> None:
        self.player = player
