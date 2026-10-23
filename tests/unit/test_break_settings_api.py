"""Settings, Breaks end to end (#88): saved per station and used at once.

The real app and a real saved-point station (``tests/unit/_break_station.py``)
sit behind the routes, so turning breaks off is seen by the announcer's
own routes, as an announcer program would see it.  The settings routes are browser
routes: the page's ``Origin``, never a machine token.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from autodj._bridge import PlayerBridge
from autodj.breaks import BREAKS_OFF, DROPPED, QUEUED
from autodj.breaks_api import NEEDS_SAVED_POINTS
from autodj.config import BreaksConfig
from autodj.machine_tokens import SCOPE_ANNOUNCER
from autodj.runtime_state import load_into_bridge
from tests._server import create_app
from tests.unit._break_station import make_station, start_station
from tests.unit.test_breaks_api import LOCAL, Station
from tests.unit.test_library import make_config

ORIGIN = {"Origin": LOCAL["base_url"]}


def settings(station: Station, path: str = "/api/breaks/settings") -> dict:
    reply = station.client.get(path)
    assert reply.status_code == 200, reply.text
    assert reply.headers["cache-control"] == "no-store"
    return reply.json()


def save(station: Station, body: dict, path: str = "/api/breaks/settings"):
    return station.client.post(path, json=body, headers=ORIGIN)


def stored(station: Station) -> dict:
    path = station.station.bridge.settings.state_dir / "web_state.json"
    return json.loads(path.read_text(encoding="utf-8"))["playback"]


class SavingStation(Station):
    """:class:`Station` behind a real :class:`PlayerBridge`, which saves ``web_state.json``."""

    def __init__(self, tmp_path) -> None:
        cfg = make_config(tmp_path)
        self.player, self.entries, self.points, _desk = make_station(tmp_path, cfg)
        bridge = PlayerBridge(player=self.player, sim=self.player._sim)
        self.app = create_app(bridge)
        self.client = TestClient(self.app, **LOCAL)
        self.station = self.app.state.stations.default
        self.desk = self.station.breaks
        _token, self.secret = self.app.state.machine_tokens().create(
            "announcer", [SCOPE_ANNOUNCER], "default"
        )
        self.renders = start_station(self.player, self.entries)


@pytest.fixture
def station(tmp_path):
    return SavingStation(tmp_path)


def test_the_settings_say_how_the_station_takes_breaks(station):
    assert settings(station) == {
        "enabled": True,
        "dj_placement": "auto",
        "min_intro_seconds": 2.0,
        "duck_db": None,
        "liners_duck_db": 0.0,
        "reason": None,
    }
    assert settings(station, "/api/stations/default/breaks/settings")["enabled"] is True


def test_a_saved_change_is_used_at_once_and_kept(station):
    reply = save(station, {"dj_placement": "standalone", "min_intro_seconds": 4, "duck_db": -9})
    assert reply.status_code == 200, reply.text
    view = reply.json()
    assert (view["dj_placement"], view["min_intro_seconds"], view["duck_db"]) == (
        "standalone",
        4.0,
        -9.0,
    )
    assert view["dropped"] == 0
    config = station.player._cfg.breaks
    assert (config.dj_placement, config.min_intro_seconds, config.duck_db) == (
        "standalone",
        4.0,
        -9.0,
    )
    # The announcer and the voice tracker see the new placement now.
    assert station.context()["limits"]["dj_placement"] == "standalone"
    playback = stored(station)
    assert playback["breaks_dj_placement"] == "standalone"
    assert (playback["breaks_min_intro_seconds"], playback["breaks_duck_db"]) == (4.0, -9.0)
    assert playback["breaks_enabled"] is True


def test_a_null_duck_follows_the_voice_liners_and_survives_a_restart(station):
    assert save(station, {"duck_db": -9}).status_code == 200
    assert save(station, {"duck_db": None}).json()["duck_db"] is None
    assert station.player._cfg.breaks.duck_db is None
    assert stored(station)["breaks_duck_db"] is None
    # Leaving duck_db out leaves it as it is.
    assert save(station, {"duck_db": -6}).status_code == 200
    assert save(station, {"min_intro_seconds": 3}).json()["duck_db"] == -6.0


def test_turning_breaks_off_tells_the_announcer_and_drops_what_waits(station):
    booked = station.upload()
    assert booked.status_code == 201, booked.text
    break_id = booked.json()["break_id"]

    off = save(station, {"enabled": False})
    assert off.status_code == 200, off.text
    assert (off.json()["enabled"], off.json()["dropped"]) == (False, 1)
    status = station.call("GET", f"/api/announcer/breaks/{break_id}").json()
    assert status["state"] == DROPPED
    assert status["reason"] == "breaks were turned off in Settings"
    context = station.context()
    assert context["accepting"] is False and context["reason"] == BREAKS_OFF
    refused = station.upload({"slot_id": "a" * 32, "ending": {"id": "x"}, "upcoming": {"id": "y"}})
    assert refused.status_code == 409 and refused.json()["detail"] == BREAKS_OFF
    assert stored(station)["breaks_enabled"] is False

    on = save(station, {"enabled": True})
    assert on.status_code == 200 and on.json()["dropped"] == 0
    again = station.context()
    assert again["accepting"] is True and again["reason"] is None
    assert station.upload().json()["state"] == QUEUED


def test_an_upload_in_flight_when_breaks_go_off_is_not_booked(station):
    """An upload let in before breaks were turned off is refused at booking, not queued."""
    book = station.player.book_break

    def turned_off_first(item):
        # Settings, Breaks saves "off" while this upload is being decoded.
        station.player._cfg.breaks.enabled = False
        station.desk.drop_waiting_breaks("breaks were turned off in Settings")
        return book(item)

    station.player.book_break = turned_off_first
    reply = station.upload()
    assert reply.status_code == 409, reply.text
    assert reply.json()["detail"] == BREAKS_OFF
    assert reply.json()["reason"] == "disabled"
    assert [item for item in station.desk.listing() if item.state == QUEUED] == []
    station.desk.maintain()
    assert [path.name for path in station.desk.folder.iterdir()] == []


def test_the_ad_schedule_still_books_while_breaks_are_off(station):
    from dataclasses import replace

    from autodj.breaks import AD_SCHEDULE
    from tests.unit._break_station import make_break

    station.player._cfg.breaks.enabled = False
    station.desk.folder.mkdir(parents=True, exist_ok=True)
    spot = replace(
        make_break(station.desk, "s" * 32, kind="ad"), source=AD_SCHEDULE, keep_file=True
    )
    station.desk.add(spot)
    assert station.desk.get(spot.break_id).state == QUEUED


def test_the_settings_say_when_saved_point_playback_is_missing(station):
    station.player._cfg.playback.transition_mode = "full_intro_outro"
    assert settings(station)["reason"] == NEEDS_SAVED_POINTS
    # Off, there is nothing to explain.
    assert save(station, {"enabled": False}).json()["reason"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": "yes"},
        {"enabled": 1},
        {"dj_placement": "middle"},
        {"min_intro_seconds": 61},
        {"min_intro_seconds": True},
        {"duck_db": 1},
        {"duck_db": -31},
        {"duck_db": "loud"},
        # Upload limits and quotas stay in config.toml.
        {"max_upload_mb": 10},
        {"quota_mb": 10},
    ],
)
def test_bad_settings_are_refused_and_change_nothing(station, body):
    reply = save(station, body)
    assert reply.status_code == 422, body
    assert settings(station)["dj_placement"] == "auto"
    assert station.player._cfg.breaks.enabled is True


def test_saved_break_settings_win_over_config_after_a_restart(station):
    assert save(station, {"enabled": False, "dj_placement": "talk_up"}).status_code == 200
    # config.toml still says on and auto: the saved values win at the next start.
    station.player._cfg.breaks = BreaksConfig(enabled=True, dj_placement="auto", duck_db=-3.0)
    load_into_bridge(station.station.bridge, station.station.bridge.settings.state_dir)
    restored = station.player._cfg.breaks
    assert (restored.enabled, restored.dj_placement, restored.duck_db) == (False, "talk_up", None)


def test_an_unrelated_save_leaves_breaks_to_config(station):
    station.station.bridge.set_volume(0.5)
    station.station.bridge.save_persistent_state()
    playback = stored(station)
    assert playback["volume"] == 0.5
    assert not [key for key in playback if key.startswith("breaks_")]
    # Settings, Breaks' own save keeps all four from then on.
    assert save(station, {"min_intro_seconds": 3}).status_code == 200
    station.station.bridge.save_persistent_state()
    assert {key for key in stored(station) if key.startswith("breaks_")} == {
        "breaks_enabled",
        "breaks_dj_placement",
        "breaks_min_intro_seconds",
        "breaks_duck_db",
    }
