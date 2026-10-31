"""Audit the complete queue-route exemption against real bridge reservation checks."""

import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from autodj.local_programme import Grant
from autodj.local_programme_audio import LocalPCM
from autodj.local_programme_playout import Occurrence
from tests._server import create_app


@pytest.mark.parametrize("prefix", ["/api", "/api/stations/default"])
@pytest.mark.parametrize(
    "action,expected",
    [
        ("append", 200),
        ("remove-reserved", 409),
        ("remove-tail", 200),
        ("reorder-reserved", 409),
        ("clear", 409),
        ("play-next", 409),
        ("play-now", 409),
    ],
)
def test_every_queue_write_preserves_local_reservations(bridge, prefix, action, expected):
    """ADR-0020-06: exempt queue routes reach the bridge guard before mutation."""
    bridge.player._cfg.server.local_programme_enabled = True
    app = create_app(bridge)
    station = app.state.stations.default
    performance = station.local_programme.performance
    entries = bridge.sim.entries[:3]
    state = bridge.player._state
    state.queue = list(entries)
    state.queued_next = None
    performance.audio = LocalPCM(
        Grant("epoch", 1, "capability", "identity", "socket"), now=time.monotonic()
    )
    performance.occurrences = [
        Occurrence(str(index), entry, (0, 0), True) for index, entry in enumerate(entries[:2])
    ]
    performance.validate_queue = Mock(wraps=performance.validate_queue)
    requests = {
        "append": ("/queue/add", {"path": entries[2].path}),
        "remove-reserved": ("/queue/remove", {"path": entries[0].path}),
        "remove-tail": ("/queue/remove", {"path": entries[2].path}),
        "reorder-reserved": ("/queue/reorder", {"paths": [entry.path for entry in entries[::-1]]}),
        "clear": ("/queue/reorder", {"paths": []}),
        "play-next": ("/play-next", {"path": entries[2].path}),
        "play-now": ("/play-next", {"path": entries[2].path, "now": True}),
    }
    path, body = requests[action]
    response = TestClient(app).post(prefix + path, json=body)
    assert response.status_code == expected, response.text
    assert state.queue[:2] == entries[:2]
    assert state.queued_next is None
    if action in {"play-next", "play-now"}:
        performance.validate_queue.assert_not_called()  # The station fence rejects earlier.
    else:
        performance.validate_queue.assert_called_once()
    if expected == 409:
        assert state.queue == entries
    # A new exempt route requires an explicit guard audit and a case above.
    assert {
        route.path.removeprefix("/api")
        for route in app.routes
        if getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}
        and route.path.startswith("/api/queue")
    } == {"/queue/add", "/queue/remove", "/queue/reorder"}
