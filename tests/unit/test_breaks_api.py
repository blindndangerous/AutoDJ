"""The breaks API end to end: tokens, the slot, uploads, status and airing (#88).

A real player in saved-point station mode (``tests/unit/_break_station.py``)
sits behind the real app, so a break goes from the upload through the slot
plan into a render and on the air.  Requests come from 127.0.0.1 over plain
HTTP, as an announcer program's do on the station host.
"""

from __future__ import annotations

import io
import logging
from dataclasses import replace
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
        self.player._dry_run = False
        self.app = create_app(SimpleNamespace(player=self.player, library=None))
        self.client = TestClient(self.app, **LOCAL)
        self.station = self.app.state.stations.default
        self.desk = self.station.breaks
        _token, self.secret = self.app.state.machine_tokens().create(
            "announcer", [SCOPE_ANNOUNCER], "default"
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


def test_room_is_checked_before_the_body_is_read(tmp_path):
    station = Station(tmp_path, max_queued=1)
    # An upload still arriving holds the station's only place.
    station.desk.reserve("f" * 32, "", 10, "arriving")
    full = station.upload()
    assert full.status_code == 429 and full.json()["reason"] == "queue_full"
    assert files(station.desk) == []
    station.desk.release("f" * 32)
    assert station.upload().status_code == 201


def test_a_declared_length_over_the_stations_own_cap_is_refused_unread(tmp_path):
    # The server-wide cap is the largest station's (4 MB here); this station takes 1 MB.
    station = Station(tmp_path, max_upload_mb=4)
    own = replace(station.desk.config, max_upload_mb=1)
    station.desk._config = lambda: own
    reply = station.call(
        "POST",
        "/api/announcer/breaks",
        content=b"x",
        headers={
            "Content-Length": str(2 * 1024 * 1024),
            "Content-Type": "multipart/form-data; boundary=x",
        },
    )
    assert reply.status_code == 413
    assert "at most 1 MB" in reply.json()["detail"]


@pytest.mark.parametrize(
    ("data", "files_", "status"),
    [
        ({"kind": "dj"}, None, 422),
        ({"kind": "dj", "unknown": "x"}, "file", 422),
        ({"kind": "dj"}, "two files", 422),
    ],
    ids=["no file", "unknown field", "two files"],
)
def test_a_malformed_form_is_refused(station, data, files_, status):
    slot = station.context()["slot"]
    form = {
        "slot_id": slot["slot_id"],
        "ending_id": slot["ending"]["id"],
        "upcoming_id": slot["upcoming"]["id"],
        **data,
    }
    if files_ == "file":
        sent = {"file": ("b.wav", wav(), "audio/wav")}
    elif files_ == "two files":
        sent = [("file", ("b.wav", wav(), "audio/wav")), ("other", ("c.wav", wav(), "audio/wav"))]
    else:
        sent = None
    reply = station.call("POST", "/api/announcer/breaks", data=form, files=sent)
    assert reply.status_code == status, reply.text
    assert files(station.desk) == []
    # Nothing stays reserved.
    station.desk.reserve("probe", slot["slot_id"], 0, "probe")
    station.desk.release("probe")


def test_an_eight_channel_upload_is_refused_before_it_is_decoded(station):
    sound = np.zeros((SR // 10, 8), dtype=np.float32)
    out = io.BytesIO()
    sf.write(out, sound, 48000, format="WAV")
    reply = station.upload(body=out.getvalue())
    assert reply.status_code == 422
    assert "mono or stereo" in reply.json()["detail"]
    assert files(station.desk) == []


def test_a_token_behind_a_plain_http_proxy_is_refused(station):
    reply = station.call(
        "GET", "/api/announcer/context", headers={"X-Forwarded-For": "192.168.1.9"}
    )
    assert reply.status_code == 403
    assert reply.json()["detail"] == "Machine tokens need HTTPS"


def test_the_decode_gate_opens_when_the_decode_ends_not_when_the_client_leaves(tmp_path):
    import asyncio
    import threading

    from autodj.breaks_api import _gated_decode

    started, finish = threading.Event(), threading.Event()

    def slow(_path, _seconds):
        started.set()
        assert finish.wait(5)
        return "cues"

    async def scenario() -> list[bool]:
        gate = asyncio.Semaphore(1)
        task = asyncio.create_task(_gated_decode(gate, tmp_path / "b.wav", 1.0, slow))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()  # the client hung up
        with pytest.raises(asyncio.CancelledError):
            await task
        locked_while_decoding = gate.locked()
        finish.set()
        for _ in range(100):
            if not gate.locked():
                break
            await asyncio.sleep(0.01)
        return [locked_while_decoding, gate.locked()]

    assert asyncio.run(scenario()) == [True, False]


def test_a_busy_decoder_answers_503(tmp_path, monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from autodj import breaks_api

    monkeypatch.setattr(breaks_api, "DECODE_WAIT_SECONDS", 0.01)

    async def scenario() -> HTTPException:
        gate = asyncio.Semaphore(0)
        with pytest.raises(HTTPException) as busy:
            await breaks_api._gated_decode(gate, tmp_path / "b.wav", 1.0)
        return busy.value

    refused = asyncio.run(scenario())
    assert refused.status_code == 503 and refused.headers == {"Retry-After": "5"}


def test_a_decode_cancelled_before_it_starts_gives_the_gate_back(tmp_path):
    import asyncio
    import threading

    from autodj import breaks_api

    hold, holding = threading.Event(), threading.Event()

    def occupy(_path, _seconds):
        holding.set()
        assert hold.wait(5)
        return "first"

    def never(_path, _seconds):
        raise AssertionError("a cancelled decode ran")

    async def scenario() -> int:
        gate = asyncio.Semaphore(2)
        first = asyncio.create_task(breaks_api._gated_decode(gate, tmp_path / "a", 1.0, occupy))
        await asyncio.to_thread(holding.wait, 5)
        second = asyncio.create_task(breaks_api._gated_decode(gate, tmp_path / "b", 1.0, never))
        await asyncio.sleep(0.05)  # it holds the gate, queued behind the first decode
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        hold.set()
        assert await first == "first"
        for _ in range(100):
            if gate._value == 2:
                break
            await asyncio.sleep(0.01)
        return gate._value

    assert asyncio.run(scenario()) == 2


def test_an_upload_that_arrives_too_slowly_is_refused_and_gives_its_room_back(
    tmp_path, monkeypatch
):
    from autodj import breaks_api

    station = Station(tmp_path, max_queued=1)
    monkeypatch.setattr(breaks_api, "BODY_DEADLINE_SECONDS", -1.0)
    monkeypatch.setattr(breaks_api, "MIN_BODY_BYTES_PER_SECOND", 10**15)
    slow = station.upload()
    assert slow.status_code == 408
    assert files(station.desk) == []
    monkeypatch.setattr(breaks_api, "BODY_DEADLINE_SECONDS", 60.0)
    assert station.upload().status_code == 201  # the only place was given back


def test_a_part_with_too_many_or_too_long_headers_is_refused(station):
    slot = station.context()["slot"]
    boundary = "brk"

    def body(extra_headers: list[str]) -> bytes:
        fields = {
            "kind": "dj",
            "slot_id": slot["slot_id"],
            "ending_id": slot["ending"]["id"],
            "upcoming_id": slot["upcoming"]["id"],
        }
        chunks = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            for key, value in fields.items()
        ]
        head = "".join(f"{line}\r\n" for line in extra_headers)
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="b.wav"\r\n'
            f"{head}Content-Type: audio/wav\r\n\r\n"
        )
        return "".join(chunks).encode() + wav() + f"\r\n--{boundary}--\r\n".encode()

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    many = body([f"X-Extra-{n}: x" for n in range(10)])
    long = body(["X-Long: " + "x" * 8000])
    for sent in (many, long):
        reply = station.call("POST", "/api/announcer/breaks", content=sent, headers=headers)
        assert reply.status_code in {400, 422}, reply.text
    assert files(station.desk) == []
    assert (
        station.call("POST", "/api/announcer/breaks", content=body([]), headers=headers).status_code
        == 201
    )
