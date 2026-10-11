"""DMCA mode: queue warnings that need confirming, and no public next song."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj.config import DmcaConfig
from autodj.repeat_rules import PlayRecord, TrackKeyCache
from autodj.server import create_app

from ._helpers import _make_entry


@pytest.fixture
def dmca_bridge(bridge):
    """A bridge in DMCA mode whose last three plays were from one album."""
    bridge.player._cfg.dmca = DmcaConfig(enabled=True)
    bridge.player._track_keys.return_value = TrackKeyCache()
    state = bridge.player._state
    now = time.time()
    state.play_history.extend(
        PlayRecord.of(_make_entry(n), now - 400 + position * 100)
        for position, n in enumerate((7, 8, 0))
    )
    return bridge


ALBUM_LIMIT = "more than 3 songs from the album “Album” in 3 hours"


@pytest.mark.parametrize(
    ("url", "body", "where"),
    [
        ("/api/queue/add", {}, "at the end of the queue"),
        ("/api/play-next", {"now": False}, "next"),
        ("/api/play-next", {"now": True}, "now"),
    ],
)
def test_a_song_over_a_limit_needs_confirming(dmca_bridge, url, body, where, caplog) -> None:
    state = dmca_bridge.player._state
    song = {"path": _make_entry(4).path, **body}
    with TestClient(create_app(dmca_bridge)) as client:
        refused = client.post(url, json=song)
        assert refused.status_code == 409
        assert refused.json()["field"] == "confirm"
        detail = refused.json()["detail"]
        assert detail.startswith(f"DMCA mode: playing this song {where} would mean ")
        assert ALBUM_LIMIT in detail
        # Nothing was queued and nothing skipped: the operator decides.
        assert (state.queue, state.queued_next) == ([], None)
        dmca_bridge.player._skip_event.set.assert_not_called()
        with caplog.at_level(logging.WARNING, logger="autodj.server"):
            accepted = client.post(url, json={**song, "confirm": True})
    assert accepted.status_code == 200
    assert _make_entry(4).path in [
        entry.path for entry in [*state.queue, state.queued_next] if entry
    ]
    assert "the operator queued Artist — Song 4 although it breaks the limit" in caplog.text


def test_confirm_must_be_a_real_boolean(dmca_bridge) -> None:
    with TestClient(create_app(dmca_bridge)) as client:
        reply = client.post("/api/queue/add", json={"path": _make_entry(4).path, "confirm": "yes"})
    assert reply.status_code == 422


def test_the_queue_ahead_counts_toward_the_limits(dmca_bridge) -> None:
    state = dmca_bridge.player._state
    other = replace(_make_entry(5), artist="Someone", album="Elsewhere")
    state.queue.append(other)
    with TestClient(create_app(dmca_bridge)) as client:
        # Appended after another album, the run of one album is broken ...
        appended = client.post("/api/queue/add", json={"path": _make_entry(4).path})
        assert appended.status_code == 409
        assert "in a row" not in appended.json()["detail"]
        # ... while played next it would be the third in a row.
        assert (
            "in a row"
            in client.post("/api/play-next", json={"path": _make_entry(4).path}).json()["detail"]
        )


def test_plays_that_age_out_before_the_song_starts_do_not_count(dmca_bridge) -> None:
    state = dmca_bridge.player._state
    state.play_history.clear()
    started = time.time() - 3 * 60 * 60 + 60
    state.play_history.extend(PlayRecord.of(_make_entry(n), started) for n in (7, 8))
    state.play_history.append(PlayRecord.of(state.current_track, time.time()))
    with TestClient(create_app(dmca_bridge)) as client:
        # Playing now, the two old plays still count; after the 180-second
        # playing track they have aged out.
        assert (
            client.post(
                "/api/play-next", json={"path": _make_entry(4).path, "now": True}
            ).status_code
            == 409
        )
        assert client.post("/api/play-next", json={"path": _make_entry(4).path}).status_code == 200


def test_without_dmca_mode_nothing_is_checked(bridge) -> None:
    bridge.player._track_keys.side_effect = AssertionError("not checked")
    with TestClient(create_app(bridge)) as client:
        assert client.post("/api/queue/add", json={"path": _make_entry(4).path}).status_code == 200
        assert client.post("/api/queue/add", json={"path": "Z:/gone.flac"}).status_code == 404


def test_an_unknown_song_is_not_found_in_dmca_mode(dmca_bridge) -> None:
    with TestClient(create_app(dmca_bridge)) as client:
        assert client.post("/api/play-next", json={"path": "Z:/gone.flac"}).status_code == 404


def test_public_pages_never_show_the_next_song(dmca_bridge) -> None:
    dmca_bridge.player._cfg.nowplaying.show_next = True
    with TestClient(create_app(dmca_bridge)) as client:
        feed = client.get("/api/nowplaying").json()
        assert feed["next_track"] is None
        assert feed["current_track"]["album"] == "Album"
        assert feed["current_track"]["title"] == "Song 0"
        page = client.get("/status").text
    assert "Song 1" not in page
    assert "Song 0" in page


def test_show_next_cannot_be_turned_on_in_dmca_mode(dmca_bridge, tmp_path) -> None:
    dmca_bridge.player._cfg.index.active_dir = tmp_path
    dmca_bridge.player._cfg.stream.enabled = True
    dmca_bridge.attach_stream(stream=MagicMock(listener_count=0), station=MagicMock(state="idle"))
    dmca_bridge.save_persistent_state = MagicMock()
    with TestClient(create_app(dmca_bridge), client=("192.0.2.4", 50000)) as client:
        refused = client.post("/api/stream/settings", json={"show_next": True})
        assert refused.status_code == 409
        assert "DMCA mode is on" in refused.json()["detail"]
        assert dmca_bridge.player._cfg.nowplaying.show_next is False
        assert client.post("/api/stream/settings", json={"show_next": False}).status_code == 200
    assert dmca_bridge.get_settings()["playback"]["dmca_enabled"] is True
