"""The listener request page and the operator's request routes over HTTP (#49)."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodj.config import DmcaConfig, RequestsConfig, ServerConfig
from autodj.indexer import IndexEntry
from autodj.repeat_rules import PlayRecord, TrackKeyCache
from autodj.server import create_app

from ._helpers import _make_entry

_TOKEN = "request-page-test-access-token-32-bytes"
ORIGIN = {"Origin": "http://testserver"}


def _client(bridge, tmp_path: Path, *, token: bool = True, **settings: object) -> TestClient:
    cfg = bridge.player._cfg
    cfg.server = ServerConfig(
        access_token=_TOKEN if token else None,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    cfg.index.active_dir = tmp_path
    cfg.library.music_dir = tmp_path / "music"
    cfg.requests = RequestsConfig(**settings)  # type: ignore[arg-type]
    bridge.player._track_keys.return_value = TrackKeyCache()
    bridge.sim.similarity.return_value = None
    # Each song by its own artist, so the artist rule does not hold them back.
    bridge.sim.entries = [_song(n) for n in range(6)]
    return TestClient(create_app(bridge), client=("192.0.2.4", 50000))


def _song(n: int) -> IndexEntry:
    return replace(_make_entry(n), artist=f"Artist {n}") if n else _make_entry(0)


def _token(html: str) -> str:
    match = re.search(r'name="token" value="([^"]+)"', html)
    assert match, html
    return match.group(1)


def _shown(client: TestClient, n: int, page: str = "/requests") -> str:
    """Song n's id, as a search on the page shows it."""
    found = client.get(page, params={"q": f"song {n}"})
    match = re.search(rf'\?track=([0-9a-f]{{24}})">Song {n} by', found.text)
    assert match, found.text
    return match.group(1)


def _confirm(client: TestClient, n: int, page: str = "/requests") -> tuple[str, str]:
    track = _shown(client, n, page)
    response = client.get(page, params={"track": track})
    assert response.status_code == 200, response.text
    return track, _token(response.text)


def _send(client: TestClient, track: str, token: str, name: str = "", page: str = "/requests"):
    return client.post(
        page,
        data={"token": token, "track": track, "name": name},
        headers=ORIGIN,
    )


@pytest.mark.parametrize("path", ["/requests", "/stations/default/requests"])
def test_the_request_page_is_off_until_turned_on(bridge, tmp_path: Path, path: str) -> None:
    client = _client(bridge, tmp_path)
    # Public, so not 401, but not there either.
    assert client.get(path).status_code == 404
    assert client.post(path, data={}, headers=ORIGIN).status_code == 404
    assert client.get("/api/requests").status_code == 401


def test_a_listener_searches_confirms_and_sends(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, enabled=True)
    page = client.get("/requests")
    assert page.status_code == 200
    headers = page.headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert "form-action 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "<title>AutoDJ: request a song</title>" in page.text
    assert '<label for="req-q">Title, artist or album</label>' in page.text

    found = client.get("/requests", params={"q": "song 3"})
    assert "<title>Search results: AutoDJ: request a song</title>" in found.text
    assert "1 song found." in found.text
    track = _shown(client, 3)
    assert f'<a href="/requests?track={track}">Song 3 by Artist 3, from the album Album.</a>' in (
        found.text
    )
    for text in (page.text, found.text):
        assert "Z:/Music" not in text and _TOKEN not in text

    track, token = _confirm(client, 3)
    sent = _send(client, track, token, "  Sam ")
    assert sent.status_code == 200, sent.text
    assert "<title>Request received: AutoDJ</title>" in sent.text
    assert "Thanks, Sam." in sent.text
    # It waits for the operator: nothing is queued yet, and the operator's
    # list needs a paired browser.
    assert bridge.player._state.queue == []
    assert client.get("/api/requests").status_code == 401


def test_search_input_is_checked_and_limited(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, enabled=True)
    short = client.get("/requests", params={"q": " a "})
    assert short.status_code == 400
    assert "<title>Error: Type at least 2 letters to search. AutoDJ: request a song</title>" in (
        short.text
    )
    assert 'aria-invalid="true" aria-describedby="req-q-error"' in short.text
    assert client.get("/requests", params={"q": "x" * 101}).status_code == 400
    assert "No songs found." in client.get("/requests", params={"q": "nothing like it"}).text
    for _ in range(29):
        client.get("/requests", params={"q": "song"})
    limited = client.get("/requests", params={"q": "song"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert client.get("/requests", params={"track": "../etc/passwd"}).status_code == 404


def test_a_form_needs_its_token_and_this_sites_origin(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, enabled=True)
    track, token = _confirm(client, 2)
    # Another site cannot post the form for a visitor.
    other = client.post(
        "/requests",
        data={"token": token, "track": track},
        headers={"Origin": "https://evil.example"},
    )
    assert other.status_code == 403
    # (The test client sends this site's Origin unless told otherwise.)
    del client.headers["Origin"]
    assert client.post("/requests", data={"token": token, "track": track}).status_code == 403
    forged = _send(client, track, "1." + "0" * 32)
    assert forged.status_code == 403
    assert "This form has expired" in forged.text
    # Nothing else is accepted: unknown fields, repeats, other encodings, big bodies.
    assert (
        client.post(
            "/requests", data={"token": token, "track": track, "path": "x"}, headers=ORIGIN
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/requests",
            content=f"token={token}&track={track}&track={track}",
            headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 400
    )
    assert (
        client.post("/requests", json={"token": token, "track": track}, headers=ORIGIN).status_code
        == 415
    )
    assert (
        client.post(
            "/requests",
            content="name=" + "x" * 5000,
            headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    long_name = _send(client, track, token, "x" * 41)
    assert long_name.status_code == 400
    assert bridge.player._state.queue == []


def test_auto_approval_places_the_request_and_says_roughly_when(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, enabled=True, approval="auto", per_listener=1)
    bridge.player._state.queue.append(_make_entry(4))
    track, token = _confirm(client, 2)
    sent = _send(client, track, token)
    assert sent.status_code == 200, sent.text
    assert "should play within the next half hour" in sent.text
    assert _make_entry(2).path in [entry.path for entry in bridge.player._state.queue]
    track, token = _confirm(client, 3)
    again = _send(client, track, token)
    assert again.status_code == 429
    assert again.headers["retry-after"]
    assert "up to 1 song every hour" in again.text


def test_dmca_limits_hold_for_requests(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, enabled=True, approval="auto")
    bridge.player._cfg.dmca = DmcaConfig(enabled=True)
    state = bridge.player._state
    now = time.time()
    state.play_history.extend(
        PlayRecord.of(_make_entry(n), now - 400 + position * 100)
        for position, n in enumerate((7, 8, 0))
    )
    track, token = _confirm(client, 4)
    refused = _send(client, track, token)
    assert refused.status_code == 409
    assert "<title>Not right now: AutoDJ</title>" in refused.text
    assert state.queue == []


def test_the_operator_lists_approves_and_turns_down(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, token=False, enabled=True)
    for n in (2, 3):
        track, token = _confirm(client, n)
        client.cookies.clear()
        assert _send(client, track, token, f"Fan {n}").status_code == 200
    listing = client.get("/api/requests").json()
    assert listing["enabled"] is True and listing["today"] == 2
    first, second = listing["pending"]
    assert first["name"] == "Fan 2" and "path" not in first
    assert first["can_approve"] is True
    assert first["fit"].startswith("Fits after")
    approved = client.post(f"/api/requests/{first['id']}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    assert approved.json()["request"]["status"] == "queued"
    assert _make_entry(2).path in [e.path for e in bridge.player._state.queue]
    rejected = client.post(f"/api/requests/{second['id']}/reject", headers=ORIGIN)
    assert rejected.json()["pending"] == []
    assert client.post(f"/api/requests/{second['id']}/reject", headers=ORIGIN).status_code == 404
    by_name = client.get("/api/stations/default/requests").json()
    assert [row["status"] for row in by_name["recent"]] == ["rejected", "queued"]


def test_requests_off_lists_nothing_for_the_operator(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, token=False)
    assert client.get("/api/requests").json() == {"enabled": False, "pending": [], "recent": []}
    assert client.post("/api/requests/" + "0" * 32 + "/approve", headers=ORIGIN).status_code == 404


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/stations/default/requests", 200),
        ("/stations/nope/requests", 401),
        ("/stations/default/requests/x", 401),
        ("/requests/x", 401),
        ("/api/stations/default/requests", 401),
    ],
)
def test_station_request_pages_are_public_only_for_a_configured_station(
    bridge, tmp_path: Path, path: str, status: int
) -> None:
    client = _client(bridge, tmp_path, enabled=True)
    assert client.get(path).status_code == status


def test_play_next_counts_the_play_next_song_toward_dmca_limits(bridge, tmp_path: Path) -> None:
    """The request plays after the "play next" song, so that song counts too."""
    client = _client(bridge, tmp_path, token=False, enabled=True)
    track, token = _confirm(client, 4)
    assert _send(client, track, token).status_code == 200
    (waiting,) = client.get("/api/requests").json()["pending"]
    bridge.player._cfg.dmca = DmcaConfig(enabled=True)
    state = bridge.player._state
    # The playing song is from "Album": the request after it makes two in a
    # row, which is allowed, but a "play next" song from it in between makes
    # three.
    state.queued_next = _song(5)
    refused = client.post(f"/api/requests/{waiting['id']}/play-next", headers=ORIGIN)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"].startswith("DMCA mode: playing this song next would mean")
    assert state.queue == []
    state.queued_next = None
    queued = client.post(f"/api/requests/{waiting['id']}/play-next", headers=ORIGIN)
    assert queued.status_code == 200, queued.text
    assert [entry.path for entry in state.queue] == [_song(4).path]


def test_a_token_for_one_song_cannot_send_another(bridge, tmp_path: Path) -> None:
    client = _client(bridge, tmp_path, enabled=True)
    _track, token = _confirm(client, 2)
    other = _shown(client, 3)
    refused = _send(client, other, token)
    assert refused.status_code == 403
    assert "This form has expired" in refused.text


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9"),  # one IPv6 /64
        ("::ffff:192.0.2.9", "192.0.2.9"),  # IPv4 written as IPv6
    ],
)
def test_one_listener_cannot_rotate_addresses_past_the_limit(
    bridge, tmp_path: Path, first: str, second: str
) -> None:
    _client(bridge, tmp_path, enabled=True, approval="auto", per_listener=1)
    app = create_app(bridge)
    one = TestClient(app, client=(first, 50000))
    track, token = _confirm(one, 2)
    assert _send(one, track, token).status_code == 200
    two = TestClient(app, client=(second, 50001))
    track, token = _confirm(two, 3)
    limited = _send(two, track, token)
    assert limited.status_code == 429, limited.text
