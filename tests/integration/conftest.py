"""Pytest fixtures shared across the integration test files.

Pytest auto-loads this; tests that need ``client`` or ``bridge`` only
have to declare them as a parameter.  Helper builders live in
``_helpers.py`` so test bodies can also call them directly when they
need to override a default before bridge construction.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autodj.server import PlayerBridge, create_app

from ._helpers import _make_player_mock, _make_sim_mock


@pytest.fixture(autouse=True)
def _default_browser_security_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make legacy direct clients behave like same-origin browser requests."""
    original_init = TestClient.__init__

    def browser_init(self, *args, **kwargs) -> None:
        headers = kwargs.get("headers")
        if headers is None:
            kwargs["headers"] = {
                "Host": "testserver",
                "Origin": "http://testserver",
            }
        elif isinstance(headers, dict):
            kwargs["headers"] = {
                "Host": "testserver",
                "Origin": "http://testserver",
                **headers,
            }
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", browser_init)


@pytest.fixture
def client():
    """TestClient wired to a PlayerBridge with a fresh mock Player + sim."""
    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    return TestClient(
        create_app(bridge),
        headers={"Host": "testserver", "Origin": "http://testserver"},
    )


@pytest.fixture
def bridge():
    """Raw PlayerBridge for tests that drive bridge methods directly."""
    player = _make_player_mock()
    sim = _make_sim_mock()
    return PlayerBridge(player=player, sim=sim)
