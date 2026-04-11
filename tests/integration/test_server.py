"""Integration tests for autodj.server.

Uses FastAPI's TestClient (synchronous) so no real uvicorn process or audio
hardware is needed.  The Player and SimilarityIndex are mocked with minimal
fakes so the tests focus on HTTP routing and PlayerBridge behaviour.
"""

from __future__ import annotations

from collections import deque
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
    )


def _make_player_mock(entry: IndexEntry | None = None) -> MagicMock:
    """Build a mock Player with the attributes PlayerBridge accesses."""
    from autodj.player import PlayerState

    player = MagicMock()
    state = PlayerState()
    state.current_track = entry or _make_entry(0)
    state.next_track    = _make_entry(1)
    state.is_paused     = False
    state.volume        = 1.0
    state.is_muted      = False
    player._state         = state
    player._playback_pos  = [44100 * 30]  # 30 s into the track
    player._current_sr    = 44100
    player._skip_event    = MagicMock()
    return player


def _make_sim_mock(entries: list[IndexEntry] | None = None) -> MagicMock:
    sim = MagicMock()
    sim.entries = entries or [_make_entry(i) for i in range(5)]
    return sim


@pytest.fixture
def client():
    """TestClient wired to a PlayerBridge with mock Player + SimilarityIndex."""
    from fastapi.testclient import TestClient

    player = _make_player_mock()
    sim    = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app    = create_app(bridge)
    return TestClient(app)


@pytest.fixture
def bridge():
    player = _make_player_mock()
    sim    = _make_sim_mock()
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


# ---------------------------------------------------------------------------
# WebSocket /ws
# ---------------------------------------------------------------------------


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
