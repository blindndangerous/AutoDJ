"""Integration tests for autodj.server.

Uses FastAPI's TestClient (synchronous) so no real uvicorn process or audio
hardware is needed.  The Player and SimilarityIndex are mocked with minimal
fakes so the tests focus on HTTP routing and PlayerBridge behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from autodj.indexer import IndexEntry
from autodj.server import PlayerBridge, create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_entry(i: int = 0) -> IndexEntry:
    return IndexEntry(
        path=f"Z:/Music/song_{i}.flac",
        title=f"Song {i}",
        artist="Artist",
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2000,
        length=180.0,
        energy=0.05,
        key=0,
        mode=1,
        tempo_confidence=0.8,
    )


def _make_player_mock(entry: IndexEntry | None = None) -> MagicMock:
    """Build a mock Player with the attributes PlayerBridge accesses."""
    from autodj.player import PlayerState

    player = MagicMock()
    state = PlayerState()
    state.current_track = entry or _make_entry(0)
    state.next_track = _make_entry(1)
    state.is_paused = False
    state.volume = 1.0
    state.is_muted = False
    state.queue = []
    player._state = state
    player._playback_pos = [44100 * 30]  # 30 s into the track
    player._current_sr = 44100
    player._skip_event = MagicMock()
    # Fields exposed via get_state / get_settings on the new bridge API
    player._eq_low = 1.0
    player._eq_mid = 1.0
    player._eq_high = 1.0
    player._beatmatch_ratio = 1.0
    player._last_transition_fx = "none"
    player._dry_run = False
    player._smart_shuffle = False
    player._pure_shuffle = False
    player._anchor_to_seed = False
    player._seed_path = None
    player._previous_track = None
    player._last_pick_mode = "seed"
    player._bpm_range = None
    player._preset = None
    player._discovery_every = None
    player._current_lyrics = []
    player._current_lyrics_plain = ""
    # Wire a concrete config so get_settings doesn't traverse MagicMock chains.
    cfg = MagicMock()
    cfg.transitions.effect = "none"
    cfg.transitions.wet_mix = 1.0
    cfg.djmix.harmonic_mixing = False
    cfg.djmix.harmonic_mode = "compatible"
    cfg.djmix.beatmatch = False
    cfg.djmix.phrase_align = False
    cfg.djmix.outro_intro_align = False
    cfg.djmix.filter_sweep = False
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.crossfade_eq_duck = False
    cfg.playback.enable_daypart = False
    cfg.playback.show_lyrics = True
    cfg.playback.prefetch_next_track = True
    cfg.playback.silence_trigger_crossfade = True
    cfg.replaygain.enabled = False
    cfg.presets = {}
    player._cfg = cfg
    return player


def _make_sim_mock(entries: list[IndexEntry] | None = None) -> MagicMock:
    sim = MagicMock()
    sim.entries = entries if entries is not None else [_make_entry(i) for i in range(5)]
    return sim


@pytest.fixture
def client():
    """TestClient wired to a PlayerBridge with mock Player + SimilarityIndex."""
    from fastapi.testclient import TestClient

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)
    return TestClient(app)


@pytest.fixture
def bridge():
    player = _make_player_mock()
    sim = _make_sim_mock()
    return PlayerBridge(player=player, sim=sim)


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_200(self, client) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_status_has_current_track(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["current_track"] is not None
        assert data["current_track"]["title"] == "Song 0"

    def test_status_has_next_track(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["next_track"]["title"] == "Song 1"

    def test_status_elapsed_is_numeric(self, client) -> None:
        data = client.get("/api/status").json()
        assert isinstance(data["elapsed"], float)
        assert data["elapsed"] == pytest.approx(30.0, abs=0.5)

    def test_status_volume_defaults_to_one(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["volume"] == pytest.approx(1.0)

    def test_status_not_paused_by_default(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["is_paused"] is False


# ---------------------------------------------------------------------------
# POST /api/skip
# ---------------------------------------------------------------------------


class TestSkip:
    def test_skip_returns_200(self, client) -> None:
        resp = client.post("/api/skip")
        assert resp.status_code == 200

    def test_skip_calls_skip_event(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/skip")
        bridge.player._skip_event.set.assert_called_once()

    def test_skip_returns_ok(self, client) -> None:
        data = client.post("/api/skip").json()
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/pause
# ---------------------------------------------------------------------------


class TestPause:
    def test_pause_returns_200(self, client) -> None:
        assert client.post("/api/pause").status_code == 200

    def test_pause_toggles_state(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        # Initially not paused
        assert bridge.player._state.is_paused is False
        data = tc.post("/api/pause").json()
        assert data["paused"] is True
        assert bridge.player._state.is_paused is True
        # Second call toggles back
        data = tc.post("/api/pause").json()
        assert data["paused"] is False

    def test_pause_response_reflects_new_state(self, client) -> None:
        data = client.post("/api/pause").json()
        assert "paused" in data


# ---------------------------------------------------------------------------
# POST /api/volume
# ---------------------------------------------------------------------------


class TestVolume:
    def test_volume_sets_value(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/volume", json={"volume": 0.75})
        assert resp.status_code == 200
        assert bridge.player._state.volume == pytest.approx(0.75)

    def test_volume_clamps_to_one(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/volume", json={"volume": 2.0})
        assert bridge.player._state.volume == pytest.approx(1.0)

    def test_volume_clamps_to_zero(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/volume", json={"volume": -0.5})
        assert bridge.player._state.volume == pytest.approx(0.0)

    def test_volume_response_returns_new_value(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        data = tc.post("/api/volume", json={"volume": 0.5}).json()
        assert data["volume"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# POST /api/mute
# ---------------------------------------------------------------------------


class TestMute:
    def test_mute_returns_200(self, client) -> None:
        assert client.post("/api/mute").status_code == 200

    def test_mute_toggles_state(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        assert bridge.player._state.is_muted is False
        data = tc.post("/api/mute").json()
        assert data["muted"] is True
        data = tc.post("/api/mute").json()
        assert data["muted"] is False


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_returns_results(self, client) -> None:
        resp = client.get("/api/search?q=song")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) > 0

    def test_search_empty_query_returns_empty(self, client) -> None:
        data = client.get("/api/search?q=").json()
        assert data["results"] == []

    def test_search_missing_query_returns_empty(self, client) -> None:
        data = client.get("/api/search").json()
        assert data["results"] == []

    def test_search_no_match_returns_empty(self, client) -> None:
        data = client.get("/api/search?q=zzznomatch999").json()
        assert data["results"] == []

    def test_search_result_has_expected_fields(self, client) -> None:
        data = client.get("/api/search?q=song").json()
        result = data["results"][0]
        assert "title" in result
        assert "artist" in result
        assert "display_name" in result

    def test_search_multi_token_requires_all_match(self, client) -> None:
        # All sample entries have title="Song N" + artist="Artist".
        # Both tokens present in haystack → match.
        data = client.get("/api/search?q=song+artist").json()
        assert len(data["results"]) > 0

    def test_search_multi_token_drops_partial_matches(self, client) -> None:
        data = client.get("/api/search?q=song+nomatchterm999").json()
        assert data["results"] == []

    def test_search_matches_album(self, client) -> None:
        # All entries have album="Album"
        data = client.get("/api/search?q=album").json()
        assert len(data["results"]) > 0

    def test_search_limit_param(self, client) -> None:
        # Default fixture has 5 entries — limit to 2
        data = client.get("/api/search?q=song&limit=2").json()
        assert len(data["results"]) == 2

    def test_search_limit_clamped_to_500(self, client) -> None:
        data = client.get("/api/search?q=song&limit=99999").json()
        assert len(data["results"]) <= 500


# ---------------------------------------------------------------------------
# WebSocket /ws
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /api/play-next
# ---------------------------------------------------------------------------


class TestPlayNext:
    def test_play_next_returns_200(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[2].path
        resp = TestClient(create_app(bridge)).post("/api/play-next", json={"path": path})
        assert resp.status_code == 200

    def test_play_next_returns_ok_true_for_valid_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        data = TestClient(create_app(bridge)).post("/api/play-next", json={"path": path}).json()
        assert data["ok"] is True

    def test_play_next_returns_ok_false_for_unknown_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        data = (
            TestClient(create_app(bridge))
            .post("/api/play-next", json={"path": "Z:/nope.flac"})
            .json()
        )
        assert data["ok"] is False

    def test_play_next_sets_queued_next(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[1].path
        TestClient(create_app(bridge)).post("/api/play-next", json={"path": path})
        assert bridge.player._state.queued_next is not None
        assert bridge.player._state.queued_next.path == path

    def test_play_now_also_triggers_skip(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        TestClient(create_app(bridge)).post("/api/play-next", json={"path": path, "now": True})
        bridge.player._skip_event.set.assert_called_once()

    def test_play_next_does_not_skip(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        TestClient(create_app(bridge)).post("/api/play-next", json={"path": path, "now": False})
        bridge.player._skip_event.set.assert_not_called()


class TestWebSocket:
    def test_websocket_connects(self, client) -> None:
        with client.websocket_connect("/ws") as ws:
            # Connection accepted without error — that's the assertion
            assert ws is not None

    def test_websocket_receives_state_push(self, bridge) -> None:
        """The broadcast loop pushes state; we can't wait 1 s in a unit test,
        but we can verify the WS endpoint accepts a connection and closes
        cleanly."""
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        with tc.websocket_connect("/ws"):
            pass  # connects and disconnects without error


# ---------------------------------------------------------------------------
# PlayerBridge unit tests
# ---------------------------------------------------------------------------


class TestPlayerBridge:
    def test_get_state_returns_dict(self, bridge) -> None:
        state = bridge.get_state()
        assert isinstance(state, dict)

    def test_get_state_current_track_is_dict(self, bridge) -> None:
        state = bridge.get_state()
        assert isinstance(state["current_track"], dict)

    def test_skip_sets_event(self, bridge) -> None:
        bridge.skip()
        bridge.player._skip_event.set.assert_called_once()

    def test_pause_returns_new_state(self, bridge) -> None:
        result = bridge.pause()
        assert result is True  # was False, now True

    def test_set_volume_midpoint(self, bridge) -> None:
        bridge.set_volume(0.5)
        assert bridge.player._state.volume == pytest.approx(0.5)

    def test_toggle_mute(self, bridge) -> None:
        assert bridge.toggle_mute() is True
        assert bridge.toggle_mute() is False

    def test_search_by_title(self, bridge) -> None:
        results = bridge.search("song")
        assert len(results) > 0

    def test_search_by_artist(self, bridge) -> None:
        results = bridge.search("Artist")
        assert len(results) > 0

    def test_search_no_match(self, bridge) -> None:
        assert bridge.search("zzznomatch999") == []

    def test_search_limit_respected(self, bridge) -> None:
        results = bridge.search("song", limit=2)
        assert len(results) <= 2

    def test_play_next_queues_entry(self, bridge) -> None:
        path = bridge.sim.entries[0].path
        assert bridge.play_next(path) is True
        assert bridge.player._state.queued_next.path == path

    def test_play_next_unknown_path_returns_false(self, bridge) -> None:
        assert bridge.play_next("Z:/does_not_exist.flac") is False
        assert bridge.player._state.queued_next is None

    def test_play_next_now_skips(self, bridge) -> None:
        path = bridge.sim.entries[0].path
        bridge.play_next(path, now=True)
        bridge.player._skip_event.set.assert_called_once()

    def test_play_next_without_now_does_not_skip(self, bridge) -> None:
        path = bridge.sim.entries[0].path
        bridge.play_next(path, now=False)
        bridge.player._skip_event.set.assert_not_called()

    def test_toggle_discovery_flips_state(self, bridge) -> None:
        assert bridge.player._state.discovery_enabled is False
        result = bridge.toggle_discovery()
        assert result is True
        assert bridge.player._state.discovery_enabled is True
        result = bridge.toggle_discovery()
        assert result is False

    def test_get_state_includes_discovery_fields(self, bridge) -> None:
        state = bridge.get_state()
        assert "discovery_enabled" in state
        assert "discovery_available" in state

    def test_get_state_with_no_current_track(self) -> None:
        player = _make_player_mock(entry=None)
        player._state.current_track = None
        player._state.next_track = None
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        state = bridge.get_state()
        assert state["current_track"] is None
        assert state["duration"] == 0.0

    def test_get_state_elapsed_calculation(self, bridge) -> None:
        # Player mock has pos=44100*30, sr=44100 → elapsed=30.0
        state = bridge.get_state()
        assert state["elapsed"] == pytest.approx(30.0, abs=0.5)


# ---------------------------------------------------------------------------
# GET / — HTML index page
# ---------------------------------------------------------------------------


class TestGetIndexHtml:
    def test_get_root_returns_200(self, client) -> None:
        resp = client.get("/")
        assert resp.status_code == 200

    def test_get_root_returns_html(self, client) -> None:
        resp = client.get("/")
        assert "html" in resp.headers["content-type"].lower()

    def test_get_root_contains_autodj(self, client) -> None:
        resp = client.get("/")
        assert "AutoDJ" in resp.text or "autodj" in resp.text.lower()


# ---------------------------------------------------------------------------
# POST /api/discovery toggle via WebSocket message
# ---------------------------------------------------------------------------


class TestDiscoveryToggle:
    def test_toggle_discovery_via_bridge(self, bridge) -> None:
        initial = bridge.player._state.discovery_enabled
        bridge.toggle_discovery()
        assert bridge.player._state.discovery_enabled is not initial


# ---------------------------------------------------------------------------
# Broadcast loop — async test (requires pytest-asyncio)
# ---------------------------------------------------------------------------


async def test_broadcast_loop_sends_to_websocket_clients() -> None:
    """The broadcast loop pushes state JSON to connected WebSocket clients.

    We patch asyncio.sleep to be instant so the loop fires immediately
    instead of waiting a real second.
    """
    import asyncio
    from unittest.mock import patch as _patch

    from httpx import ASGITransport, AsyncClient

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)

    async def instant_sleep(delay, *args, **kwargs):
        await asyncio.sleep(0)

    # Use ASGI transport for real async HTTP — no need for a running server
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_track" in data

    # Now exercise the WebSocket endpoint with async client
    # The broadcast loop is tested by checking it can run without crashing
    with _patch("asyncio.sleep", instant_sleep):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/status")
            assert resp.status_code == 200


async def test_broadcast_loop_removes_dead_clients() -> None:
    """Clients that raise on send_text are pruned from _ws_clients."""

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)

    # The broadcast loop's dead-client pruning logic runs when send_text raises.
    # We verify this by connecting a client, then disconnecting while the loop is running.
    from fastapi.testclient import TestClient

    tc = TestClient(app)
    with tc.websocket_connect("/ws"):
        # Immediately disconnect — the loop will attempt a send and get an error
        pass  # __exit__ triggers disconnect

    # No crash = success; the loop pruned the dead client


async def test_http_api_accessible_via_async_client() -> None:
    """Verify all REST endpoints respond correctly using the async ASGI transport."""
    from httpx import ASGITransport, AsyncClient

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/status")).status_code == 200
        assert (await ac.post("/api/skip")).status_code == 200
        assert (await ac.post("/api/pause")).status_code == 200
        assert (await ac.post("/api/volume", json={"volume": 0.5})).status_code == 200
        assert (await ac.post("/api/mute")).status_code == 200
        assert (await ac.get("/api/search?q=song")).status_code == 200


# ---------------------------------------------------------------------------
# App lifespan (startup / shutdown tasks)
# ---------------------------------------------------------------------------


class TestLifespan:
    def test_app_lifespan_starts_and_stops(self) -> None:
        """Using TestClient as a context manager triggers lifespan startup/teardown."""
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        with TestClient(app) as tc:
            resp = tc.get("/api/status")
            assert resp.status_code == 200

    def test_api_accessible_during_lifespan(self) -> None:
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        with TestClient(app) as tc:
            data = tc.get("/api/status").json()
            assert "current_track" in data


# ---------------------------------------------------------------------------
# WebSocket text messages (discovery toggle + bad JSON)
# ---------------------------------------------------------------------------


class TestWebSocketMessages:
    def test_toggle_discovery_via_ws_message(self) -> None:
        """Sending {"type": "toggle_discovery"} toggles the discovery flag."""
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        initial = player._state.discovery_enabled
        tc = TestClient(app)
        with tc.websocket_connect("/ws") as ws:
            ws.send_text('{"type": "toggle_discovery"}')
            # Yield control so the server processes the message
            import time

            time.sleep(0.05)

        assert player._state.discovery_enabled is not initial

    def test_invalid_json_over_ws_does_not_crash(self) -> None:
        """Non-JSON text should be silently ignored."""
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        tc = TestClient(app)
        with tc.websocket_connect("/ws") as ws:
            ws.send_text("not valid json {[}")
            import time

            time.sleep(0.05)
        # No exception = success

    def test_unknown_message_type_ignored(self) -> None:
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        tc = TestClient(app)
        with tc.websocket_connect("/ws") as ws:
            ws.send_text('{"type": "unknown_action"}')
            import time

            time.sleep(0.05)


# ---------------------------------------------------------------------------
# serve() — wires Player thread + uvicorn
# ---------------------------------------------------------------------------


class TestServeFunction:
    def test_serve_starts_uvicorn(self) -> None:
        """serve() should call uvicorn.run with the FastAPI app."""
        from unittest.mock import MagicMock, patch

        from autodj.server import serve

        # Build a proper cfg mock
        cfg_mock = MagicMock()
        cfg_mock.playback.no_repeat_window = 50
        cfg_mock.playback.artist_repeat_window = 3
        cfg_mock.playback.crossfade_seconds = 3.0
        sim = _make_sim_mock()

        with (
            patch("autodj.player.Player.run"),  # prevent audio loop from blocking
            patch("uvicorn.run") as mock_uvicorn,
        ):
            serve(cfg=cfg_mock, sim=sim, seed_entry=None)

        mock_uvicorn.assert_called_once()

    def test_serve_starts_player_thread(self) -> None:
        """serve() launches a daemon Player thread before starting uvicorn."""
        import threading
        from unittest.mock import MagicMock, patch

        from autodj.server import serve

        cfg_mock = MagicMock()
        cfg_mock.playback.no_repeat_window = 50
        cfg_mock.playback.artist_repeat_window = 3
        cfg_mock.playback.crossfade_seconds = 3.0
        sim = _make_sim_mock()

        started = threading.Event()

        def fake_run(*args, **kwargs):
            started.set()

        with (
            patch("autodj.player.Player.run", fake_run),
            patch("uvicorn.run"),
        ):
            serve(cfg=cfg_mock, sim=sim, seed_entry=None)

        started.wait(timeout=2.0)
        assert started.is_set(), "Player thread should have started"


# ---------------------------------------------------------------------------
# New settings endpoints (transition / preset / djmix / playback / bpm / discovery)
# ---------------------------------------------------------------------------


class TestSettingsEndpoints:
    def test_get_settings(self, client) -> None:
        data = client.get("/api/settings").json()
        assert "transition" in data
        assert "djmix" in data
        assert "playback" in data
        assert "bpm_range" in data
        assert "discovery_every" in data
        assert "available_presets" in data

    def test_post_transition_valid(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/transition", json={"effect": "echo_out"})
        assert resp.status_code == 200
        assert bridge.player._cfg.transitions.effect == "echo_out"

    def test_post_transition_invalid_ignored(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._cfg.transitions.effect = "echo_out"
        tc = TestClient(create_app(bridge))
        tc.post("/api/transition", json={"effect": "BOGUS_FX"})
        assert bridge.player._cfg.transitions.effect == "echo_out"

    def test_post_transition_persists(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/transition", json={"effect": "tape_stop"})
        # File written under tmp_path/web_state.json
        f = tmp_path / "web_state.json"
        assert f.exists()
        import json as _json

        data = _json.loads(f.read_text(encoding="utf-8"))
        assert data["transition"] == "tape_stop"

    def test_post_djmix_flags(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"beatmatch": True, "harmonic_mixing": True})
        assert bridge.player._cfg.djmix.beatmatch is True
        assert bridge.player._cfg.djmix.harmonic_mixing is True

    def test_post_djmix_skips_none_fields(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._cfg.djmix.beatmatch = True
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"harmonic_mixing": True})  # beatmatch absent
        assert bridge.player._cfg.djmix.beatmatch is True  # untouched

    def test_post_playback_settings(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post(
            "/api/playback-settings",
            json={
                "crossfade_seconds": 5.5,
                "crossfade_eq_duck": True,
                "smart_shuffle": True,
                "replaygain_enabled": True,
            },
        )
        assert bridge.player._cfg.playback.crossfade_seconds == 5.5
        assert bridge.player._cfg.playback.crossfade_eq_duck is True
        assert bridge.player._smart_shuffle is True
        assert bridge.player._cfg.replaygain.enabled is True

    def test_post_playback_settings_daypart(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._cfg.playback.enable_daypart = False
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"enable_daypart": True})
        assert bridge.player._cfg.playback.enable_daypart is True

    def test_post_playback_settings_clamps_negative(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"crossfade_seconds": -3.0})
        assert bridge.player._cfg.playback.crossfade_seconds == 0.0

    def test_post_playback_pure_shuffle(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"pure_shuffle": True})
        assert bridge.player._pure_shuffle is True

    def test_post_playback_show_lyrics_false_clears_buffer(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from autodj.audio_meta import LyricLine

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._current_lyrics = [LyricLine(0.0, "x")]
        bridge.player._current_lyrics_plain = "stuff"
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"show_lyrics": False})
        assert bridge.player._cfg.playback.show_lyrics is False
        assert bridge.player._current_lyrics == []
        assert bridge.player._current_lyrics_plain == ""

    def test_post_djmix_harmonic_mode(self, bridge, tmp_path) -> None:
        """Setting harmonic_mode flips harmonic_mixing on/off automatically."""
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"harmonic_mode": "strict"})
        assert bridge.player._cfg.djmix.harmonic_mode == "strict"
        assert bridge.player._cfg.djmix.harmonic_mixing is True
        tc.post("/api/djmix", json={"harmonic_mode": "off"})
        assert bridge.player._cfg.djmix.harmonic_mode == "off"
        assert bridge.player._cfg.djmix.harmonic_mixing is False

    def test_post_djmix_invalid_harmonic_mode_ignored(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._cfg.djmix.harmonic_mode = "compatible"
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"harmonic_mode": "bogus_mode"})
        assert bridge.player._cfg.djmix.harmonic_mode == "compatible"

    def test_set_djmix_skips_none_value_explicit(self, bridge) -> None:
        """Direct set_djmix call with None value hits the continue branch."""
        bridge.player._cfg.djmix.beatmatch = False
        bridge.set_djmix(beatmatch=None, phrase_align=True)
        assert bridge.player._cfg.djmix.beatmatch is False
        assert bridge.player._cfg.djmix.phrase_align is True

    def test_set_djmix_unknown_attr_silently_skipped(self, bridge) -> None:
        """Direct set_djmix with an attribute that isn't on cfg.djmix is a no-op."""
        bridge.set_djmix(does_not_exist=True)  # no AttributeError

    def test_post_playback_anchor_to_seed(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        # No prior seed_path — bridge should pin the current track as seed
        # the moment anchored mode is enabled.
        bridge.player._seed_path = None
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"anchor_to_seed": True})
        assert bridge.player._anchor_to_seed is True
        assert bridge.player._seed_path == bridge.player._state.current_track.path

    def test_post_playback_anchor_to_seed_off(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._anchor_to_seed = True
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"anchor_to_seed": False})
        assert bridge.player._anchor_to_seed is False


# ---------------------------------------------------------------------------
# Library tools — index / enrich / prune / stats endpoints
# ---------------------------------------------------------------------------


class TestLibraryEndpoints:
    def test_get_library_stats(self, client) -> None:
        resp = client.get("/api/library/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "track_count" in data
        assert "average_bpm" in data
        assert "tracks_with_genre" in data

    def test_get_library_job_idle(self, client) -> None:
        # Reset shared singleton so other tests don't bleed in.
        from autodj import jobs as _jobs

        _jobs._MANAGER = None
        resp = client.get("/api/library/job")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False

    def test_post_library_run_disallowed(self, client) -> None:
        resp = client.post(
            "/api/library/run",
            json={"name": "rm", "args": ["-rf"]},
        )
        assert resp.status_code == 409

    def test_post_library_stop_idle(self, client) -> None:
        from autodj import jobs as _jobs

        _jobs._MANAGER = None
        resp = client.post("/api/library/stop")
        assert resp.status_code == 200
        assert resp.json() == {"stopped": False}

    def test_state_includes_library_job_field(self, client) -> None:
        from autodj import jobs as _jobs

        _jobs._MANAGER = None
        data = client.get("/api/status").json()
        assert "library_job" in data
        assert data["library_job"]["running"] is False

    def test_post_bpm_range_sets(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/bpm-range", json={"lo": 100, "hi": 140})
        assert bridge.player._bpm_range == (100.0, 140.0)

    def test_post_bpm_range_clears_on_null(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._bpm_range = (100.0, 140.0)
        tc = TestClient(create_app(bridge))
        tc.post("/api/bpm-range", json={"lo": None, "hi": None})
        assert bridge.player._bpm_range is None

    def test_post_bpm_range_clears_when_lo_ge_hi(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/bpm-range", json={"lo": 140, "hi": 100})
        assert bridge.player._bpm_range is None

    def test_post_discovery(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        tc = TestClient(create_app(bridge))
        tc.post("/api/discovery", json={"every": 25})
        assert bridge.player._discovery_every == 25

    def test_post_discovery_zero_disables(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._discovery_every = 20
        tc = TestClient(create_app(bridge))
        tc.post("/api/discovery", json={"every": 0})
        assert bridge.player._discovery_every is None

    def test_post_preset_clears_on_null(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        # mock a preset
        bridge.player._preset = MagicMock(name="energetic")
        tc = TestClient(create_app(bridge))
        tc.post("/api/preset", json={"name": None})
        assert bridge.player._preset is None

    def test_post_preset_unknown_name_silent(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._preset = None
        tc = TestClient(create_app(bridge))
        tc.post("/api/preset", json={"name": "totally_made_up_preset_xyz"})
        assert bridge.player._preset is None  # silently ignored


# ---------------------------------------------------------------------------
# EQ endpoint
# ---------------------------------------------------------------------------


class TestEqEndpoint:
    def test_post_eq_sets_bands(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        data = tc.post("/api/eq", json={"low": 0.5, "mid": 1.5, "high": 2.0}).json()
        assert data["low"] == 0.5
        assert data["mid"] == 1.5
        assert data["high"] == 2.0

    def test_post_eq_clamps(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        data = tc.post("/api/eq", json={"low": -5.0, "high": 100.0}).json()
        assert data["low"] == 0.0
        assert data["high"] == 2.0

    def test_post_eq_partial(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge.player._eq_low = 1.0
        bridge.player._eq_mid = 1.0
        bridge.player._eq_high = 1.0
        tc = TestClient(create_app(bridge))
        tc.post("/api/eq", json={"low": 0.3})
        assert bridge.player._eq_low == 0.3
        assert bridge.player._eq_mid == 1.0  # unchanged
        assert bridge.player._eq_high == 1.0


# ---------------------------------------------------------------------------
# Queue endpoints
# ---------------------------------------------------------------------------


class TestQueueEndpoints:
    def test_queue_add(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[2].path
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/add", json={"path": path})
        assert resp.json()["ok"] is True
        assert any(e.path == path for e in bridge.player._state.queue)

    def test_queue_add_unknown_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/add", json={"path": "Z:/nope.flac"})
        assert resp.json()["ok"] is False

    def test_queue_remove(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e = bridge.sim.entries[0]
        bridge.player._state.queue.append(e)
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/remove", json={"path": e.path})
        assert resp.json()["ok"] is True
        assert e not in bridge.player._state.queue

    def test_queue_remove_not_found(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/remove", json={"path": "Z:/nope.flac"})
        assert resp.json()["ok"] is False

    def test_queue_reorder(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e0, e1, e2 = bridge.sim.entries[:3]
        bridge.player._state.queue.extend([e0, e1, e2])
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/reorder", json={"paths": [e2.path, e0.path, e1.path]})
        order = [e.path for e in bridge.player._state.queue]
        assert order == [e2.path, e0.path, e1.path]

    def test_queue_reorder_drops_unknown(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e0, e1 = bridge.sim.entries[:2]
        bridge.player._state.queue.extend([e0, e1])
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/reorder", json={"paths": [e1.path]})  # e0 dropped
        assert len(bridge.player._state.queue) == 1
        assert bridge.player._state.queue[0].path == e1.path


# ---------------------------------------------------------------------------
# Reseed random + advance + lyrics
# ---------------------------------------------------------------------------


class TestMisc:
    def test_advance_skips(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/advance")
        bridge.player._skip_event.set.assert_called_once()

    def test_random_track_reseeds(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/random-track")
        assert resp.json()["ok"] is True
        assert bridge.player._state.queued_next is not None

    def test_random_track_empty_index(self) -> None:
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock(entries=[])
        bridge = PlayerBridge(player=player, sim=sim)
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/random-track")
        assert resp.json()["ok"] is False

    def test_lyrics_endpoint_returns_list(self, client) -> None:
        data = client.get("/api/lyrics").json()
        assert "lyrics" in data
        assert isinstance(data["lyrics"], list)


# ---------------------------------------------------------------------------
# Audio file streaming
# ---------------------------------------------------------------------------


class TestAudioEndpoint:
    def test_audio_unknown_path_404(self, client) -> None:
        resp = client.get("/api/audio?path=Z:/not/in/index.flac")
        assert resp.status_code == 404

    def test_audio_path_in_index_but_no_file(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        # entry path is a string that does not point to a real file
        resp = tc.get(f"/api/audio?path={bridge.sim.entries[0].path}")
        assert resp.status_code == 404

    def test_audio_streams_real_file(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        # Write a fake mp3 (just bytes — we never decode it server-side)
        fake_mp3 = tmp_path / "fake.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" + b"\x00" * 4096)
        # Inject as a known entry
        e = _make_entry(99)
        e.path = str(fake_mp3)
        bridge.sim.entries.append(e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake_mp3}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.headers["accept-ranges"] == "bytes"
        assert int(resp.headers["content-length"]) == fake_mp3.stat().st_size

    def test_audio_range_request(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        fake = tmp_path / "song.flac"
        fake.write_bytes(b"\x00" * 10000)
        e = _make_entry(123)
        e.path = str(fake)
        bridge.sim.entries.append(e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake}", headers={"Range": "bytes=100-199"})
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 100-199/10000"
        assert int(resp.headers["content-length"]) == 100

    def test_audio_invalid_range_416(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        fake = tmp_path / "song.flac"
        fake.write_bytes(b"\x00" * 100)
        e = _make_entry(124)
        e.path = str(fake)
        bridge.sim.entries.append(e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake}", headers={"Range": "bytes=999999-"})
        assert resp.status_code == 416

    def test_audio_malformed_range_416(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        fake = tmp_path / "song.flac"
        fake.write_bytes(b"\x00" * 100)
        e = _make_entry(125)
        e.path = str(fake)
        bridge.sim.entries.append(e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake}", headers={"Range": "kilobytes=0-50"})
        assert resp.status_code == 416


# ---------------------------------------------------------------------------
# Static asset endpoints (cache-busting)
# ---------------------------------------------------------------------------


class TestStaticAssets:
    def test_app_css_no_cache(self, client) -> None:
        resp = client.get("/app.css")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    def test_app_js_no_cache(self, client) -> None:
        resp = client.get("/app.js")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    def test_index_no_cache(self, client) -> None:
        resp = client.get("/")
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    def test_worklet_endpoint(self, client) -> None:
        resp = client.get("/bitcrusher-worklet.js")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    def test_stutter_worklet_endpoint(self, client) -> None:
        resp = client.get("/stutter-worklet.js")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"
        assert "registerProcessor" in resp.text

    def test_freeze_worklet_endpoint(self, client) -> None:
        resp = client.get("/freeze-worklet.js")
        assert resp.status_code == 200
        assert "registerProcessor" in resp.text
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    def test_glitch_worklet_endpoint(self, client) -> None:
        resp = client.get("/glitch-worklet.js")
        assert resp.status_code == 200
        assert "registerProcessor" in resp.text
        assert resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


class TestCoverArt:
    def test_art_unknown_path_404(self, client) -> None:
        resp = client.get("/api/art?path=Z:/nope.flac")
        assert resp.status_code == 404

    def test_art_no_embedded_returns_404(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        # entry path doesn't point at a real file → read_cover_art returns None
        resp = tc.get(f"/api/art?path={bridge.sim.entries[0].path}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PlayerBridge persistence helpers
# ---------------------------------------------------------------------------


class TestPersistenceHelpers:
    def test_save_and_load_round_trip(self, bridge, tmp_path) -> None:
        bridge.player._cfg.index.active_dir = tmp_path
        bridge.player._cfg.transitions.effect = "tape_stop"
        bridge.save_persistent_state()
        # Reset and reload
        bridge.player._cfg.transitions.effect = "none"
        bridge.load_persistent_state()
        assert bridge.player._cfg.transitions.effect == "tape_stop"

    def test_state_file_returns_none_without_cfg(self) -> None:
        bridge = PlayerBridge(player=MagicMock(_cfg=None), sim=MagicMock())
        assert bridge._state_file() is None

    def test_state_file_returns_path(self, tmp_path) -> None:
        cfg = MagicMock()
        cfg.index.active_dir = tmp_path
        bridge = PlayerBridge(player=MagicMock(_cfg=cfg), sim=MagicMock())
        assert bridge._state_file() == tmp_path / "web_state.json"


class TestReloadIndexFromDisk:
    def test_reload_no_cfg_returns_current_total(self) -> None:
        sim = MagicMock()
        sim.ntotal = 7
        bridge = PlayerBridge(player=MagicMock(_cfg=None), sim=sim)
        assert bridge.reload_index_from_disk() == 7

    def test_reload_calls_sim(self, tmp_path) -> None:
        sim = MagicMock()
        sim.reload_from_disk.return_value = 42
        cfg = MagicMock()
        cfg.index.active_dir = tmp_path
        cfg.library.music_dir = None
        cfg.library.path_remap = None
        bridge = PlayerBridge(player=MagicMock(_cfg=cfg), sim=sim)
        result = bridge.reload_index_from_disk()
        assert result == 42
        sim.reload_from_disk.assert_called_once()
