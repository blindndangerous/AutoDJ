"""The breaks API end to end: tokens, the slot, uploads, status and airing (#88).

A real player in saved-point station mode (``tests/unit/_break_station.py``)
sits behind the real app, so a break goes from the upload through the slot
plan into a render and on the air.  Requests come from 127.0.0.1 over plain
HTTP, as radiodj's do on the station host.
"""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from autodj.breaks import AIRED, DROPPED, QUEUED, RENDERED
from autodj.machine_tokens import SCOPE_ANALYSIS_WORKER, SCOPE_ANNOUNCER
from autodj.server import create_app
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._break_station import make_station, start_station
from tests.unit.test_library import make_config

LOCAL = {"base_url": "http://127.0.0.1:8080", "client": ("127.0.0.1", 5)}


def files(desk) -> list:
    """The files in the desk's folder, none when it was never made."""
    return sorted(desk.folder.iterdir()) if desk.folder.is_dir() else []


def wav(seconds: float = 2.0, level: float = 0.4, silence: float = 1.0) -> bytes:
    sound = np.zeros((round((seconds + silence) * SR), 2), dtype=np.float32)
    sound[: round(seconds * SR), 1] = level
    out = io.BytesIO()
    sf.write(out, sound, SR, format="WAV", subtype="PCM_16")
    return out.getvalue()


class Station:
    """The app, its player, and an announcer token for the default station."""

    def __init__(self, tmp_path, **breaks: object) -> None:
        cfg = make_config(tmp_path)
        self.player, self.entries, self.points, _desk = make_station(tmp_path, cfg, **breaks)
        self.app = create_app(SimpleNamespace(player=self.player, library=None))
        self.client = TestClient(self.app, **LOCAL)
        self.station = self.app.state.stations.default
        self.desk = self.station.breaks
        _token, self.secret = self.app.state.machine_tokens().create(
            "radiodj", [SCOPE_ANNOUNCER], "default"
        )
        self.renders = start_station(self.player, self.entries)

    def call(self, method: str, path: str, secret: str | None = None, **kwargs):
        headers = {"Authorization": f"Bearer {secret or self.secret}", **kwargs.pop("headers", {})}
        return self.client.request(method, path, headers=headers, **kwargs)

    def context(self) -> dict:
        reply = self.call("GET", "/api/announcer/context")
        assert reply.status_code == 200, reply.text
        return reply.json()

    def upload(
        self, slot: dict | None = None, *, body: bytes | None = None, name="break.wav", **form
    ):
        slot = slot or self.context()["slot"]
        fields = {
            "kind": "dj",
            "slot_id": slot["slot_id"],
            "ending_id": slot["ending"]["id"],
            "upcoming_id": slot["upcoming"]["id"],
            **form,
        }
        return self.call(
            "POST",
            "/api/announcer/breaks",
            data={key: str(value) for key, value in fields.items()},
            files={"file": (name, body if body is not None else wav(), "audio/wav")},
        )


def _token_id(station: Station) -> str:
    return station.secret.split(".")[1]


@pytest.fixture
def station(tmp_path):
    return Station(tmp_path)


def test_context_names_the_open_slot_and_what_is_playing(station):
    context = station.context()
    a, b, c, d = station.entries
    assert context["station"] == "default"
    assert context["accepting"] is True and context["reason"] is None
    assert context["now"]["title"] == a.title
    assert context["now"]["remaining_s"] > 0
    assert context["next"]["title"] == b.title
    slot = context["slot"]
    assert (slot["ending"]["title"], slot["upcoming"]["title"]) == (c.title, d.title)
    assert slot["ending"]["id"] == "song-song_2"
    assert slot["upcoming_intro_s"] == pytest.approx(2.0)
    assert slot["closes_in_s"] > 0 and slot["airs_in_s"] > slot["closes_in_s"]
    assert slot["breaks_booked"] == 0 and slot["pinned"] is False
    assert [song["title"] for song in context["requests"]] == [b.title, c.title, d.title]
    assert [row["title"] for row in context["history"]] == [a.title]
    assert context["limits"]["kinds"] == ["dj", "news", "weather", "music_news"]
    assert context["limits"]["max_bytes"] == 40 * 1024 * 1024
    assert context["limits"]["dj_placement"] == "auto"
    # The same station by name.
    by_name = station.call("GET", "/api/stations/default/announcer/context")
    assert by_name.json()["slot"]["slot_id"] == slot["slot_id"]


def test_a_break_is_booked_rendered_aired_and_cleaned_up(station, caplog):
    caplog.set_level(logging.DEBUG)
    reply = station.upload(label="Weather at three\n", expires_s=600)
    assert reply.status_code == 201, reply.text
    assert reply.headers["cache-control"] == "no-store"
    booked = reply.json()
    assert booked["state"] == QUEUED and booked["label"] == "Weather at three"
    assert booked["speech_s"] == pytest.approx(2.0, abs=0.06)
    assert booked["airs_in_s"] > 0
    assert booked["expires_at"] - booked["created_at"] == pytest.approx(600)
    assert station.context()["slot"]["pinned"] is True
    break_id = booked["break_id"]
    (stored,) = files(station.desk)
    assert stored.name == f"brk-{break_id}.wav"

    _render_a, render_b, _plan = station.renders
    station.player._on_track_start(render_b)
    render_c = station.player._next_rendered()
    status = station.call("GET", f"/api/announcer/breaks/{break_id}").json()
    assert status["state"] == RENDERED and status["mixed_as"] == "talk_up"
    first, _id, end = render_c.break_events[0]
    station.player._on_track_start(render_c)
    station.player._on_position(first + 1)
    station.player._on_position(end)
    assert station.call("GET", f"/api/announcer/breaks/{break_id}").json()["state"] == AIRED
    station.desk.maintain()
    assert files(station.desk) == []
    listed = station.call("GET", "/api/announcer/breaks").json()["breaks"]
    assert [item["break_id"] for item in listed] == [break_id]
    # The token never reaches the log or a reply.
    assert station.secret not in caplog.text
    assert station.secret.split(".")[2] not in str(listed)


def test_a_waiting_break_can_be_cancelled(station):
    booked = station.upload().json()
    gone = station.call("DELETE", f"/api/announcer/breaks/{booked['break_id']}")
    assert gone.status_code == 200
    assert gone.json()["state"] == DROPPED and gone.json()["reason"] == "cancelled by the announcer"
    again = station.call("DELETE", f"/api/announcer/breaks/{booked['break_id']}")
    assert again.status_code == 409 and again.json()["reason"] == DROPPED
    assert station.call("DELETE", "/api/announcer/breaks/nope").status_code == 404
    assert station.call("GET", "/api/announcer/breaks/" + "0" * 32).status_code == 404


def test_a_closed_slot_or_changed_pair_is_409_with_the_open_slot(station):
    slot = station.context()["slot"]
    closed = station.upload({**slot, "slot_id": "f" * 32})
    assert closed.status_code == 409
    assert closed.json()["reason"] == "slot_closed"
    assert closed.json()["slot"]["slot_id"] == slot["slot_id"]
    changed = station.upload({**slot, "upcoming": {"id": "someone-else"}})
    assert changed.status_code == 409 and changed.json()["reason"] == "pair_changed"
    # The slot closes once its change is rendered.
    _render_a, render_b, _plan = station.renders
    station.player._on_track_start(render_b)
    station.player._next_rendered()
    late = station.upload(slot)
    assert late.status_code == 409 and late.json()["reason"] == "slot_closed"
    assert files(station.desk) == []


@pytest.mark.parametrize(
    ("form", "body", "name", "status", "words"),
    [
        ({"kind": "news", "placement": "talk_up"}, None, "b.wav", 422, "Only a DJ break"),
        ({"kind": "jingle"}, None, "b.wav", 422, ""),
        ({"placement": "over"}, None, "b.wav", 422, ""),
        ({"expires_s": 10}, None, "b.wav", 422, ""),
        ({"expires_s": 99999}, None, "b.wav", 422, "expires_s must be at most"),
        ({}, None, "b.exe", 415, "file types"),
        ({}, b"RIFF not really audio", "b.wav", 422, "not audio AutoDJ can play"),
        ({}, wav(seconds=0.2), "b.wav", 422, "unusable"),
        ({}, wav(seconds=0.0, silence=2.0), "b.wav", 422, "unusable"),
    ],
    ids=[
        "news talk-up",
        "unknown kind",
        "unknown placement",
        "expiry too short",
        "expiry too long",
        "not an audio type",
        "not audio",
        "too short",
        "silent",
    ],
)
def test_bad_uploads_are_refused_and_leave_no_file(station, form, body, name, status, words):
    reply = station.upload(body=body, name=name, **form)
    assert reply.status_code == status, reply.text
    assert words in str(reply.json()["detail"])
    assert files(station.desk) == []


def test_size_length_and_room_limits(tmp_path):
    station = Station(tmp_path, max_upload_mb=2, max_seconds=5.0, max_per_slot=1)
    too_big = station.upload(body=wav(seconds=4.0) + b"\0" * (2 * 1024 * 1024))
    assert too_big.status_code == 413
    too_long = station.upload(body=wav(seconds=6.0))
    assert too_long.status_code == 422, too_long.text
    assert "at most 5 seconds" in too_long.json()["detail"]
    assert station.upload().status_code == 201
    full = station.upload()
    assert full.status_code == 409 and full.json()["reason"] == "slot_full"
    assert len(files(station.desk)) == 1


def test_a_declared_oversize_body_is_refused_before_it_is_read(tmp_path):
    station = Station(tmp_path, max_upload_mb=1)
    reply = station.call(
        "POST",
        "/api/announcer/breaks",
        content=b"x",
        headers={"Content-Length": str(400 * 1024 * 1024), "Content-Type": "multipart/form-data"},
    )
    assert reply.status_code == 413


def test_only_an_announcer_token_for_this_station_gets_in(station):
    tokens = station.app.state.machine_tokens()
    _worker, worker = tokens.create("box", [SCOPE_ANALYSIS_WORKER])
    _other, other = tokens.create("night", [SCOPE_ANNOUNCER], "nightowl")
    assert station.call("GET", "/api/announcer/context", worker).status_code == 401
    assert station.call("GET", "/api/announcer/context", "adj1.nope").status_code == 401
    assert station.client.get("/api/announcer/context").status_code == 401
    wrong = station.call("GET", "/api/announcer/context", other)
    assert wrong.status_code == 403 and "another station" in wrong.json()["detail"]
    browser = station.call("GET", "/api/announcer/context", headers={"Origin": LOCAL["base_url"]})
    assert browser.status_code == 403
    remote = TestClient(station.app, base_url="http://127.0.0.1:8080", client=("192.168.1.9", 5))
    reply = remote.get(
        "/api/announcer/context", headers={"Authorization": f"Bearer {station.secret}"}
    )
    assert reply.status_code == 403 and reply.json()["detail"] == "Machine tokens need HTTPS"
    unknown = station.call("GET", "/api/stations/nope/announcer/context")
    assert unknown.status_code == 404
    # Revoked, the token stops working at once.
    tokens.revoke(_token_id(station))
    assert station.call("GET", "/api/announcer/context").status_code == 401


def test_a_token_is_rate_limited(tmp_path):
    station = Station(tmp_path, requests_per_minute=2)
    assert station.call("GET", "/api/announcer/breaks").status_code == 200
    assert station.call("GET", "/api/announcer/breaks").status_code == 200
    limited = station.call("GET", "/api/announcer/breaks")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_breaks_off_says_so(tmp_path):
    station = Station(tmp_path)
    station.player._cfg.breaks.enabled = False
    context = station.context()
    assert context["accepting"] is False and "off" in context["reason"]
    slot = {"slot_id": "a" * 32, "ending": {"id": "x"}, "upcoming": {"id": "y"}}
    reply = station.upload(slot)
    assert reply.status_code == 409 and "off" in reply.json()["detail"]


def test_breaks_need_saved_point_playback(tmp_path):
    station = Station(tmp_path)
    station.player._cfg.playback.transition_mode = "full_intro_outro"
    context = station.context()
    assert context["accepting"] is False and "saved-point" in context["reason"]


def test_a_station_with_no_open_slot_asks_to_try_again(station):
    station.player._slot_plan = None
    station.player._render_ahead.request_plan = lambda: None
    context = station.context()
    assert context["slot"] is None and context["accepting"] is False
    assert "ask again" in context["reason"]
