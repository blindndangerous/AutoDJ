"""Edge cases on the read-only web endpoints.

``/api/history`` divided by an unchecked ``per_page`` and answered 500 for
``per_page=0``; ``/api/liners`` walked an operator-configured directory tree
with ``rglob`` directly inside the coroutine, blocking every other request for
as long as the walk took.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj.server import create_app


class TestHistoryPagination:
    @pytest.mark.parametrize("query", ["per_page=0", "per_page=-5", "page=0", "page=-1"])
    def test_out_of_range_pagination_is_rejected(self, client, query: str) -> None:
        assert client.get(f"/api/history?{query}").status_code == 422

    def test_per_page_above_the_cap_is_rejected(self, client) -> None:
        assert client.get("/api/history?per_page=501").status_code == 422

    def test_valid_pagination_still_answers(self, client) -> None:
        resp = client.get("/api/history?page=1&per_page=1")
        assert resp.status_code == 200
        assert resp.json()["page"] == 1


class TestLinerListingRunsOffTheEventLoop:
    def test_folder_walk_happens_in_a_worker_thread(
        self, bridge, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autodj import liners

        folder = tmp_path / "liners"
        folder.mkdir()
        (folder / "station-id.mp3").write_bytes(b"0" * 16)
        bridge.player._cfg.playback.liners_folder = str(folder)

        recorded: list[str] = []
        original = liners.LinerLibrary.from_folder

        def recording_from_folder(path):
            recorded.append(threading.current_thread().name)
            return original(path)

        monkeypatch.setattr(liners.LinerLibrary, "from_folder", recording_from_folder)

        payload = TestClient(create_app(bridge)).get("/api/liners").json()

        assert payload["files"] == ["station-id.mp3"]
        # asyncio's default executor names its workers "asyncio_N"; the thread
        # running the event loop never carries that prefix.
        assert recorded and all(name.startswith("asyncio_") for name in recorded)


class TestLinerTestRoute:
    def test_fires_the_scheduler_when_one_is_set(self, bridge) -> None:
        """Server-mixed modes wire a LinerScheduler; the route delegates to it."""
        bridge.liner_scheduler = MagicMock()
        bridge.liner_scheduler.fire.return_value = "station-id.mp3"

        resp = TestClient(create_app(bridge)).post(
            "/api/liners/test", json={"name": "station-id.mp3"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"played": "station-id.mp3"}
        bridge.liner_scheduler.fire.assert_called_once_with("station-id.mp3")

    def test_no_scheduler_is_a_no_op(self, bridge) -> None:
        """Browser-driven mode leaves ``liner_scheduler`` unset; the route no-ops."""
        assert bridge.liner_scheduler is None

        resp = TestClient(create_app(bridge)).post("/api/liners/test", json={})

        assert resp.status_code == 200
        assert resp.json() == {"played": None}

    @pytest.mark.parametrize(
        ("state", "message"),
        [
            ("idle", "Nobody is listening, so the liner was not played."),
            ("paused", "Playback is paused, so the liner was not played."),
        ],
    )
    def test_stream_station_not_playing_refuses_with_a_reason(
        self, bridge, state: str, message: str
    ) -> None:
        """Nothing is rendered while idle or paused, so success would be a lie."""
        bridge.liner_scheduler = MagicMock()
        bridge.station = MagicMock(state=state)

        resp = TestClient(create_app(bridge)).post("/api/liners/test", json={"name": "a.wav"})

        assert resp.json() == {"played": None, "message": message}
        bridge.liner_scheduler.fire.assert_not_called()

    def test_stream_station_playing_fires(self, bridge) -> None:
        bridge.liner_scheduler = MagicMock()
        bridge.liner_scheduler.fire.return_value = "a.wav"
        bridge.station = MagicMock(state="playing")

        resp = TestClient(create_app(bridge)).post("/api/liners/test", json={"name": "a.wav"})

        assert resp.json() == {"played": "a.wav"}

    def test_rejects_unknown_fields(self, bridge) -> None:
        """Extra body fields are refused rather than silently ignored."""
        resp = TestClient(create_app(bridge)).post(
            "/api/liners/test", json={"name": "a.wav", "bogus": True}
        )

        assert resp.status_code == 422
