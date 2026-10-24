"""The station name on the air (#472) as listeners and the operator see it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests._server import create_app

from ._helpers import patch_station_settings

NORMAL = "Example FM"
NIGHT = "Example FM Night Shift"
LIVE = "Example FM Live"


def test_the_name_on_the_air_reaches_the_feed_and_status_page_but_not_settings(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0009-08: published names follow the programming; the saved name stays the operator's."""
    bridge.player._cfg.stream.station_name = NORMAL
    client = TestClient(create_app(bridge))
    station = client.app.state.stations.default
    wheel = {"name": NIGHT}
    show = {"name": ""}
    bridge.player.clock_station_name = lambda: wheel["name"]
    monkeypatch.setattr(station.show_runner, "on_air_station_name", lambda: show["name"])

    def published() -> tuple[str, str, str]:
        feed = client.get("/api/nowplaying").json()["station_name"]
        page = client.get("/status").text
        saved = client.get("/api/settings").json()["values"]["stream"]["station_name"]
        return feed, NIGHT if NIGHT in page else LIVE if LIVE in page else NORMAL, saved

    assert published() == (NORMAL, NORMAL, NORMAL)
    assert station.sync_station_name() is True
    assert published() == (NIGHT, NIGHT, NORMAL)
    # A show on the air comes before the clockwheel.
    show["name"] = LIVE
    assert station.sync_station_name() is True
    assert published() == (LIVE, LIVE, NORMAL)
    # Both blank: the normal name, with nothing else to do.
    show["name"] = wheel["name"] = ""
    assert station.sync_station_name() is True
    assert published() == (NORMAL, NORMAL, NORMAL)

    # Saving another normal name while a wheel names the station keeps the
    # wheel's name on the air and saves the operator's.
    wheel["name"] = NIGHT
    station.sync_station_name()
    saved = patch_station_settings(client, {"stream": {"station_name": "Example FM Two"}})
    assert saved.status_code == 200, saved.text
    assert published() == (NIGHT, NIGHT, "Example FM Two")
    wheel["name"] = ""
    station.sync_station_name()
    assert client.get("/api/nowplaying").json()["station_name"] == "Example FM Two"
