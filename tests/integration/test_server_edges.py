"""Edge cases on the read-only web endpoints.

``/api/history`` divided by an unchecked ``per_page`` and answered 500 for
``per_page=0``; ``/api/liners`` walked an operator-configured directory tree
with ``rglob`` directly inside the coroutine, blocking every other request for
as long as the walk took.
"""

from __future__ import annotations

import threading

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
