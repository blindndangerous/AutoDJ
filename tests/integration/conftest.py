"""Pytest fixtures shared across the integration test files.

Pytest auto-loads this; tests that need ``client`` or ``bridge`` only
have to declare them as a parameter.  Helper builders live in
``_helpers.py`` so test bodies can also call them directly when they
need to override a default before bridge construction.
"""

from __future__ import annotations

import pytest

from autodj.server import PlayerBridge, create_app

from ._helpers import _make_player_mock, _make_sim_mock


@pytest.fixture
def client():
    """TestClient wired to a PlayerBridge with a fresh mock Player + sim."""
    from fastapi.testclient import TestClient

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    return TestClient(create_app(bridge))


@pytest.fixture
def bridge():
    """Raw PlayerBridge for tests that drive bridge methods directly."""
    player = _make_player_mock()
    sim = _make_sim_mock()
    return PlayerBridge(player=player, sim=sim)
