"""Station routes: both addresses, the route classes and their security (#70)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from autodj.config import ServerConfig
from autodj.security import station_relative
from autodj.server import PlayerBridge, create_app
from autodj.station_routes import STATION_PREFIX

from ._helpers import _make_player_mock, _make_sim_mock

_TOKEN = "station-routes-test-token-is-32-bytes"

# Routes every station shares: the page, pairing, the library, search,
# audio files, Stereo Tool presets, categories and the analysis workers.
SHARED = frozenset(
    {
        "/",
        "/app.css",
        "/app.js",
        "/bitcrusher-worklet.js",
        "/stutter-worklet.js",
        "/freeze-worklet.js",
        "/glitch-worklet.js",
        "/modules",
        "/healthz",
        "/api/version",
        "/api/auth/status",
        "/api/pair",
        "/api/logout",
        "/api/devices",
        "/api/devices/{device_id}",
        "/api/pairing-code",
        "/api/search",
        "/api/art",
        "/api/lyrics",
        "/api/audio",
        "/api/stereo-tool-presets",
        "/api/stereo-tool-presets/upload",
        "/api/stereo-tool-presets/{name}",
        "/api/library/tracks",
        "/api/library/folders",
        "/api/library/folders/{source_id}",
        "/api/library/rescan",
        "/api/library/points",
        "/api/library/tags",
        "/api/library/job",
        "/api/library/run",
        "/api/library/stop",
        "/api/library/stats",
        "/api/categories",
        "/api/categories/check",
        "/api/categories/{category_id}",
        "/api/machine-tokens",
        "/api/machine-tokens/{token_id}",
        "/api/analysis/workers",
        "/api/worker/claim",
        "/api/worker/audio",
        "/api/worker/result",
        "/api/worker/failed",
        "/api/worker/heartbeat",
        "/api/worker/release",
        # The station list, and starting and stopping a station by name.
        "/api/stations",
        "/api/stations/{slug}/start",
        "/api/stations/{slug}/stop",
    }
)
# Public listening addresses: the default station's, and any station's
# stream and playlist (/stations/<slug>.mp3 and .m3u), status page
# (/stations/<slug>/status), player page, embeddable player and request page
# by name.
DEFAULT_STATION_PUBLIC = frozenset(
    {
        "/status",
        "/player",
        "/embed",
        "/requests",
        "/stream.{ext}",
        "/stations/{station_file}",
        "/stations/{slug}/status",
        "/stations/{slug}/player",
        "/stations/{slug}/embed",
        "/stations/{slug}/requests",
    }
)
# Routes that act on one station, relative to the API root.  Each answers
# at /api<route> for the default station (/ws for the socket) and at
# /api/stations/{slug}<route>.
STATION = frozenset(
    {
        "/status",
        "/nowplaying",
        "/history",
        "/history/export",
        "/skip",
        "/seek",
        "/pause",
        "/volume",
        "/mute",
        "/play-next",
        "/queue/add",
        "/queue/remove",
        "/queue/reorder",
        "/advance",
        "/repick-next",
        "/random-track",
        "/preset",
        "/transition",
        "/djmix",
        "/playback-settings",
        "/bpm-range",
        "/discovery",
        "/discovery/toggle",
        "/eq",
        "/stream",
        "/stream/relays",
        "/stream/relays/{name}",
        "/stream/listeners",
        "/stream/listeners/{ident}",
        "/stream/bans",
        "/stream/settings",
        "/recording",
        "/recording/start",
        "/recording/stop",
        "/recording/split",
        "/recording/settings",
        "/webhooks",
        "/webhooks/{index}/test",
        "/profiles",
        "/profiles/{name}",
        "/profiles/{name}/apply",
        "/liners",
        "/liners/upload",
        "/liners/file/{name}",
        "/liners/test",
        "/liners/dates/{liner_id}",
        "/library/clock",
        "/library/clock/active",
        "/library/clock/import",
        "/library/clock/check",
        "/library/clock/{name}",
        "/library/clock/{name}/duplicate",
        "/library/clock/{name}/rename",
        "/schedule",
        "/schedule/upcoming",
        "/schedule/{event_id}",
        "/schedule/{event_id}/enable",
        "/schedule/{event_id}/run",
        "/rotation-test",
        "/hour-preview",
        "/carts",
        "/carts/pages/{page}/slots/{slot}",
        "/carts/pages/{page}/slots/{slot}/play",
        "/carts/pages/{page}/name",
        "/carts/active-page/{page}",
        "/carts/upload",
        "/carts/uploads/{name}",
        "/carts/stop",
        "/time-checks",
        "/time-checks/test",
        "/time-checks/preview",
        "/time-checks/packs/{name}/dates",
        # The announcer's breaks (#88): machine routes, by station.
        "/announcer/context",
        "/announcer/breaks",
        "/announcer/breaks/{break_id}",
        # The voice tracker (#128): browser routes, by station.
        "/voice-tracks",
        "/voice-tracks/{break_id}",
        # Listener requests (#49): the operator's list and decisions.
        "/requests",
        "/requests/{request_id}/approve",
        "/requests/{request_id}/play-next",
        "/requests/{request_id}/reject",
        # Matched intros (#127): browser routes, by station.
        "/intros",
        "/intros/{intro_id}/audio",
        "/intros/{intro_id}",
        # Shows (#106): browser routes, by station.
        "/shows",
        "/shows/on-air",
        "/shows/{show_id}",
        "/shows/{show_id}/songs",
        "/shows/{show_id}/generate",
        "/shows/{show_id}/start",
        "/ws",
    }
)


def _default_path(route: str) -> str:
    return "/ws" if route == "/ws" else "/api" + route


def _app(tmp_path: Path | None = None, **server: object) -> tuple[TestClient, PlayerBridge]:
    player = _make_player_mock()
    if tmp_path is not None:
        player._cfg.index.index_dir = tmp_path
        player._cfg.index.active_dir = tmp_path / "main"
        player._cfg.server.station_timezone = "UTC"
    if server:
        player._cfg.server = ServerConfig(
            allowed_hosts=["testserver"], allowed_origins=["http://testserver"], **server
        )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    return TestClient(create_app(bridge)), bridge


def test_every_route_is_classified_as_shared_or_station() -> None:
    """A new route must say whether it acts on one station; this test fails until it does."""
    client, _bridge = _app()
    paths = {route.path for route in client.app.routes}
    unclassified = []
    station_forms: set[str] = set()
    default_forms: set[str] = set()
    for route in client.app.routes:
        path = route.path
        assert isinstance(route, Mount) or not path.endswith("/") or path == "/", path
        if path in SHARED or path in DEFAULT_STATION_PUBLIC:
            continue
        if path.startswith(STATION_PREFIX + "/"):
            relative = path.removeprefix(STATION_PREFIX)
            station_forms.add(relative)
        elif path == "/ws":
            relative = "/ws"
            default_forms.add(relative)
        elif path.startswith("/api/"):
            relative = path.removeprefix("/api")
            default_forms.add(relative)
        else:
            unclassified.append(path)
            continue
        if relative not in STATION:
            unclassified.append(path)
    assert unclassified == []
    # Every station route answers at both addresses, and no shared route has a station form.
    assert station_forms == STATION
    assert default_forms == STATION
    assert paths >= SHARED
    assert paths >= DEFAULT_STATION_PUBLIC
    assert not {STATION_PREFIX + path.removeprefix("/api") for path in SHARED} & paths


@pytest.mark.parametrize(
    ("method", "route", "kwargs"),
    [
        ("get", "/status", {}),
        ("get", "/nowplaying", {}),
        ("get", "/history", {}),
        ("get", "/carts", {}),
        ("get", "/schedule", {}),
        ("get", "/time-checks", {}),
        ("get", "/profiles", {}),
        ("get", "/liners", {}),
        ("post", "/volume", {"json": {"volume": 0.5}}),
        ("post", "/eq", {"json": {"low": 1.0, "mid": 1.0, "high": 1.0}}),
    ],
)
def test_station_routes_answer_the_same_at_both_addresses(
    method: str, route: str, kwargs: dict[str, object], tmp_path: Path
) -> None:
    client, _bridge = _app(tmp_path)
    by_default = getattr(client, method)("/api" + route, **kwargs)
    by_name = getattr(client, method)(STATION_PREFIX.replace("{slug}", "default") + route, **kwargs)
    assert by_default.status_code == 200, by_default.text
    assert by_name.status_code == 200, by_name.text
    if method == "get" and route not in {"/status"}:
        assert by_name.json() == by_default.json()


def test_a_named_station_route_acts_on_that_station(tmp_path: Path) -> None:
    client, bridge = _app(tmp_path)
    bridge.skip = MagicMock()
    assert client.post("/api/stations/default/skip").status_code == 200
    assert client.post("/api/skip").status_code == 200
    assert bridge.skip.call_count == 2
    created = client.post(
        "/api/stations/default/schedule",
        json={
            "name": "Morning",
            "action": "preset",
            "params": {"name": "winddown"},
            "repeat": "daily",
            "time": "09:00",
        },
    )
    assert created.status_code == 201, created.text
    listed = client.get("/api/schedule").json()["events"]
    assert [event["id"] for event in listed] == [created.json()["id"]]
    assert (tmp_path / "main" / "schedule.json").is_file()


def test_an_unknown_station_is_404_and_a_stopped_one_409() -> None:
    client, _bridge = _app()
    unknown = client.get("/api/stations/nope/status")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "No station has that name"
    assert client.post("/api/stations/nope/skip").status_code == 404

    client.app.state.stations.default.stopped = True
    assert client.get("/api/stations/default/status").status_code == 409
    assert client.get("/api/status").status_code == 409
    # Shared routes do not depend on a station running.
    assert client.get("/api/version").status_code == 200


def test_each_station_has_its_own_socket_address() -> None:
    client, _bridge = _app()
    with client.websocket_connect("/api/stations/default/ws"):
        assert len(client.app.state.stations.default.ws_clients) == 1
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/stations/nope/ws"),
    ):
        pass
    assert closed.value.code == 4404
    client.app.state.stations.default.stopped = True
    with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect("/ws"):
        pass
    assert closed.value.code == 4409


def test_station_relative_maps_only_configured_stations() -> None:
    slugs = frozenset({"default", "nightowl"})
    assert station_relative("/api/stations/nightowl/skip", slugs) == "/api/skip"
    assert station_relative("/api/stations/default/carts/upload", slugs) == "/api/carts/upload"
    assert station_relative("/api/stations/other/skip", slugs) is None
    assert station_relative("/api/stations/nightowl", slugs) is None
    assert station_relative("/api/stations//skip", slugs) is None
    assert station_relative("/api/skip", slugs) is None
    assert station_relative("/stations/nightowl/skip", slugs) is None


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        # The public feed stays public at the station's address ...
        ("get", "/api/stations/default/nowplaying", 200),
        ("head", "/api/stations/default/nowplaying", 200),
        # ... but only for a configured station,
        ("get", "/api/stations/nope/nowplaying", 401),
        # and nothing else under a station is public, even where the
        # default-station path of the same name would be.
        ("get", "/api/stations/default/status", 401),
        ("post", "/api/stations/default/skip", 401),
        ("get", "/api/stations/default/version", 401),
        ("post", "/api/stations/default/pair", 401),
        ("get", "/api/stations/default/auth/status", 401),
        ("get", "/api/stations/default/nowplaying/x", 401),
        # A configured station's stream and playlist are public (this
        # default station is browser-driven, so they are not found) ...
        ("get", "/stations/default.mp3", 404),
        ("head", "/stations/default.m3u", 404),
        # ... any other name or extension needs a session first,
        ("get", "/stations/nope.mp3", 401),
        ("get", "/stations/default.ogg", 401),
        ("get", "/stations/default", 401),
        ("get", "/stations/default.mp3/x", 401),
        # A configured station's status page is public; any other name, or
        # anything else under a station's name, needs a session first.
        ("get", "/stations/default/status", 200),
        ("head", "/stations/default/status", 200),
        ("get", "/stations/nope/status", 401),
        ("get", "/stations/default/status/x", 401),
        ("get", "/stations/default/nowplaying", 401),
        ("get", "/stations//status", 401),
        # Its request page is public too, and answers 404 while requests
        # are off (the default); its paired list never is.
        ("get", "/stations/default/requests", 404),
        ("post", "/stations/default/requests", 404),
        ("get", "/stations/nope/requests", 401),
        ("get", "/api/stations/default/requests", 401),
        ("get", "/api/requests", 401),
        # Its player pages are public too, and answer 404 while they are
        # off (the default); under any other name they need a session.
        ("get", "/stations/default/player", 404),
        ("head", "/stations/default/embed", 404),
        ("get", "/stations/nope/player", 401),
        # and so do the station list and starting or stopping a station.
        ("get", "/api/stations", 401),
        ("post", "/api/stations/default/start", 401),
        ("post", "/api/stations/default/stop", 401),
    ],
)
def test_station_paths_security_matrix(method: str, path: str, status: int) -> None:
    client, _bridge = _app(access_token=_TOKEN)
    response = client.request(method.upper(), path)
    assert response.status_code == status, response.text


def test_station_paths_keep_their_body_limits() -> None:
    client, _bridge = _app()
    huge = {"name": "x" * 20_000}
    assert client.post("/api/stations/default/schedule", json=huge).status_code == 413
    assert client.post("/api/schedule", json=huge).status_code == 413
    # A name that is no station is capped as well, before it answers 404.
    assert client.post("/api/stations/nope/schedule", json=huge).status_code == 413
    assert client.post("/api/stations/nope/schedule", json={}).status_code == 404


def test_show_routes_cap_post_and_put_bodies_at_both_addresses() -> None:
    """Shows (#106): the caps hold for POST and PUT, with a show ID in the path."""
    client, _bridge = _app()
    show = "a" * 32
    small = {"name": "x" * 20_000}
    for prefix in ("/api", "/api/stations/default"):
        assert client.post(f"{prefix}/shows", json=small).status_code == 413
        assert client.put(f"{prefix}/shows/{show}", json=small).status_code == 413
        assert client.post(f"{prefix}/shows/{show}/generate", json=small).status_code == 413
        assert client.post(f"{prefix}/shows/{show}/start", json=small).status_code == 413
        songs = {"songs": ["/music/" + "x" * 4000] * 300}
        assert client.put(f"{prefix}/shows/{show}/songs", json=songs).status_code == 413
    # A list well under the cap reaches the route (which finds no such show).
    fits = client.put(f"/api/shows/{show}/songs", json={"songs": ["/music/a.flac"] * 500})
    assert fits.status_code == 404


def test_the_audit_log_names_the_station_route(caplog: pytest.LogCaptureFixture) -> None:
    import json
    import logging

    client, _bridge = _app()
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        assert client.post("/api/stations/default/mute").status_code == 200
    routes = [
        json.loads(record.message)["route"]
        for record in caplog.records
        if record.name == "autodj.audit"
    ]
    assert routes == [STATION_PREFIX + "/mute"]


@pytest.mark.parametrize("path", ["/api/stations/default/ws", "/api/stations/nope/ws"])
def test_a_station_socket_needs_a_session_before_its_name_is_checked(path: str) -> None:
    client, _bridge = _app(access_token=_TOKEN)
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect(path, headers={"Origin": "http://testserver"}),
    ):
        pass
    # 4401 for a known name and an unknown one alike: no session, no answer about names.
    assert closed.value.code == 4401


async def test_a_failed_close_audits_the_station_socket_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json
    import logging

    from autodj.server import _close_websocket_client, _WebSocketClient

    class BrokenSocket:
        async def close(self, *, code: int) -> None:
            raise RuntimeError(code)

    client = _WebSocketClient(BrokenSocket(), request_id="b" * 32, route=STATION_PREFIX + "/ws")
    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        closed = await _close_websocket_client(
            client, code=1013, timeout_seconds=0.1, failure_action="broadcast"
        )
    assert not closed
    routes = [
        json.loads(record.message)["route"]
        for record in caplog.records
        if record.name == "autodj.audit"
    ]
    assert routes == [STATION_PREFIX + "/ws"]


def test_the_page_sends_every_station_route_to_the_station_it_controls() -> None:
    """modules/stations.js routes each of these to /api/stations/<slug>/ (#70).

    A station route missing there would act on the default station while
    the page shows another one.
    """
    import re

    source = (
        Path(__file__).parents[2] / "src" / "autodj" / "static" / "modules" / "stations.js"
    ).read_text(encoding="utf-8")
    block = source.split("export const STATION_ROUTES = Object.freeze([", 1)[1].split("]);", 1)[0]
    listed = re.findall(r'"([^"]+)"', block)
    machine = {route for route in STATION if route.startswith("/announcer")}
    assert len(listed) == len(set(listed))
    assert set(listed) == STATION - machine - {"/ws"}


def test_each_push_lists_the_stations_when_there_are_several() -> None:
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    from autodj.server import _broadcast_state_once, _BroadcastFailureLog, _share_station

    sent: list[str] = []

    async def capture(_clients, _lock, payload, **_kwargs):
        sent.append(payload)
        return set()

    bridge = SimpleNamespace(get_state=lambda: {"queue": []})
    entries = [{"slug": "default", "name": "AutoDJ"}, {"slug": "nightowl", "name": "Night Owl"}]
    with patch("autodj.server._broadcast_and_prune", capture):
        for station_list in (None, lambda: entries):
            asyncio.run(
                _broadcast_state_once(
                    bridge,  # type: ignore[arg-type]
                    {object()},  # type: ignore[arg-type]
                    asyncio.Lock(),
                    _BroadcastFailureLog(),
                    station_list=station_list,
                )
            )
    assert "stations" not in json.loads(sent[0])
    assert json.loads(sent[1])["stations"] == entries

    def station() -> SimpleNamespace:
        return SimpleNamespace(slug="default", bridge=SimpleNamespace(), station_list=None)

    for slugs, listed in (({"default"}, False), ({"default", "nightowl"}, True)):
        registry = SimpleNamespace(
            slugs=lambda slugs=slugs: frozenset(slugs),
            summaries=lambda: entries,
            stereo_tool_user=lambda exclude: None,
            stereo_tool_lock=None,
            owns_liner=lambda slug, path: True,
        )
        runtime = station()
        _share_station(registry, runtime)  # type: ignore[arg-type]
        assert (runtime.station_list is not None) is listed
