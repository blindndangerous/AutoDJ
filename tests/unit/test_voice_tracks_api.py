"""The voice tracker's routes end to end (#128): a break recorded in the browser.

The real app and a real saved-point station (``tests/unit/_break_station.py``)
sit behind the routes, so a voice track goes from the upload through the
slot plan into a render, as an announcer's break does.  Requests come as a
browser's do: with the page's ``Origin`` and, where the server asks for one,
a session cookie, never a machine token.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autodj.breaks import ANNOUNCER, DROPPED, QUEUED, RENDERED, VOICE_TRACK
from autodj.config import ServerConfig
from autodj.machine_tokens import SCOPE_ANNOUNCER
from autodj.server import create_app
from tests.unit._break_station import make_station, start_station
from tests.unit.test_breaks_api import LOCAL, Station, files, wav
from tests.unit.test_library import make_config

ORIGIN = {"Origin": LOCAL["base_url"]}


def book(station: Station, slot: dict | None = None, *, body: bytes | None = None, **form):
    """Send a recording for *slot* (the open one) as the voice tracker page does."""
    slot = slot or voice(station)["slot"]
    fields = {
        "slot_id": slot["slot_id"],
        "ending_id": slot["ending"]["id"],
        "upcoming_id": slot["upcoming"]["id"],
        **form,
    }
    return station.client.post(
        "/api/voice-tracks",
        data={key: str(value) for key, value in fields.items()},
        files={"file": ("voice-track.wav", body if body is not None else wav(), "audio/wav")},
        headers=ORIGIN,
    )


def voice(station: Station, path: str = "/api/voice-tracks") -> dict:
    reply = station.client.get(path)
    assert reply.status_code == 200, reply.text
    assert reply.headers["cache-control"] == "no-store"
    return reply.json()


@pytest.fixture
def station(tmp_path):
    return Station(tmp_path)


def test_the_tracker_shows_the_open_slot_its_placement_and_the_limits(station):
    context = voice(station)
    _a, _b, c, d = station.entries
    assert context["accepting"] is True and context["reason"] is None
    slot = context["slot"]
    assert (slot["ending"]["title"], slot["upcoming"]["title"]) == (c.title, d.title)
    assert slot["upcoming_intro_s"] == pytest.approx(2.0)
    assert slot["closes_in_s"] > 0 and slot["airs_in_s"] > slot["closes_in_s"]
    # A 2-second intro meets min_intro_seconds (2), so auto talks it up.
    assert slot["auto_placement"] == "talk_up" and slot["auto_note"] is None
    assert context["limits"] == {
        "max_bytes": 64 * 1024 * 1024,
        "max_seconds": 600.0,
        "sample_rate": 44100,
        "dj_placement": "auto",
        "min_intro_s": 2.0,
        "max_per_slot": 2,
    }
    assert context["tracks"] == []
    assert "history" not in context and "requests" not in context
    # The same station by name.
    named = voice(station, "/api/stations/default/voice-tracks")
    assert named["slot"]["slot_id"] == slot["slot_id"]


def test_manual_voice_limits_do_not_change_announcer_limits(tmp_path, monkeypatch):
    import autodj.breaks_api as api
    from autodj.server import _voice_upload_cap

    station = Station(tmp_path, max_seconds=5.0, max_upload_mb=2)
    limits = voice(station)["limits"]
    assert (limits["max_bytes"], limits["max_seconds"]) == (64 * 1024 * 1024, 600.0)
    assert _voice_upload_cap(station.app.state.stations) >= 64 * 1024 * 1024
    checked = []

    async def decode(_gate, _path, max_seconds, _decode=None):
        checked.append(max_seconds)
        from autodj.breaks import BreakCues

        return BreakCues(0, 1, 1, 2)

    monkeypatch.setattr(api, "_gated_decode", decode)
    assert book(station).status_code == 201
    assert station.upload().status_code == 201
    assert checked == [600.0, 5.0]
    assert station.desk.config.max_upload_mb == 2
    assert station.desk.get(voice(station)["tracks"][0]["break_id"]).limit_s == 600.0


@pytest.mark.parametrize(("length", "accepted"), [(599.99, True), (600.01, False)])
def test_manual_upload_decoder_enforces_the_synthetic_ten_minute_boundary(
    tmp_path, monkeypatch, length, accepted
):
    """Exercise just below and above the installed cap without a ten-minute decode."""
    import numpy as np

    from autodj import break_audio, breaks_api
    from autodj.breaks import BreakCues
    from autodj.stereo import TrackTooLongError

    station = Station(tmp_path)
    station.player.voice_track_crosses_hard_boundary = lambda _at, _duration: False
    limits = []

    def decode(_path, max_seconds):
        limits.append(max_seconds)
        if length > max_seconds:
            raise TrackTooLongError("synthetic voice track", length)
        return np.zeros((2, 2), dtype=np.float32)

    monkeypatch.setattr(break_audio, "decode_break", decode)
    monkeypatch.setattr(breaks_api, "speech_cues", lambda _audio: BreakCues(0, 1, 1, length))
    reply = book(station, body=wav())

    assert limits == [600.0]
    if accepted:
        assert reply.status_code == 201, reply.text
        assert station.desk.get(reply.json()["break_id"]).limit_s == 600.0
        assert len(files(station.desk)) == 1
    else:
        assert reply.status_code == 422
        assert "at most 600 seconds" in reply.json()["detail"]
        assert voice(station)["tracks"] == []
        assert files(station.desk) == []


@pytest.mark.parametrize("placement", ["auto", "talk_up", "standalone"])
def test_hard_boundary_refusal_leaves_no_voice_track_or_file(station, placement):
    station.player.voice_track_crosses_hard_boundary = lambda _at, _duration: True
    reply = book(station, placement=placement)
    assert reply.status_code == 409
    assert reply.json()["reason"] == "scheduled_boundary"
    assert voice(station)["tracks"] == []
    assert files(station.desk) == []


def test_hard_boundary_check_fails_closed_when_event_access_raises(station):
    def unavailable(*_args):
        raise RuntimeError("scheduler unavailable")

    station.player.voice_track_crosses_hard_boundary = unavailable
    reply = book(station, placement="talk_up", allow_outro="true")
    assert reply.status_code == 409
    assert reply.json()["reason"] == "scheduled_boundary"
    assert "could not be checked" in reply.json()["detail"]
    assert files(station.desk) == []


def test_a_short_intro_makes_auto_standalone_and_says_why(tmp_path):
    station = Station(tmp_path, min_intro_seconds=5.0)
    slot = voice(station)["slot"]
    assert slot["auto_placement"] == "standalone"
    assert "shorter than 5 s" in slot["auto_note"]


def test_a_voice_track_is_booked_as_a_dj_break_and_rendered(station):
    reply = book(station, placement="talk_up", allow_outro="true")
    assert reply.status_code == 201, reply.text
    booked = reply.json()
    _a, _b, c, d = station.entries
    assert booked["state"] == QUEUED and booked["kind"] == "dj"
    assert booked["source"] == VOICE_TRACK and booked["allow_outro"] is True
    assert booked["label"] == f"{c.title} into {d.title}"
    assert booked["airs_in_s"] > 0
    (stored,) = files(station.desk)
    assert stored.name == f"brk-{booked['break_id']}.wav"
    # The slot is pinned for it, and the tracker lists it.
    context = voice(station)
    assert context["slot"]["pinned"] is True and context["slot"]["breaks_booked"] == 1
    assert [item["break_id"] for item in context["tracks"]] == [booked["break_id"]]

    _render_a, render_b, _plan = station.renders
    station.player._on_track_start(render_b)
    station.player._next_rendered()
    (track,) = voice(station)["tracks"]
    assert track["state"] == RENDERED and track["mixed_as"] == "talk_up"


def test_the_tracker_lists_only_voice_tracks_and_cancels_only_them(station):
    announcer = station.upload().json()
    assert announcer["source"] == ANNOUNCER
    assert voice(station)["tracks"] == []
    other = station.client.delete(f"/api/voice-tracks/{announcer['break_id']}", headers=ORIGIN)
    assert other.status_code == 404 and other.json()["detail"] == "No voice track has that id"
    assert station.desk.get(announcer["break_id"]).state == QUEUED

    booked = book(station).json()
    gone = station.client.delete(f"/api/voice-tracks/{booked['break_id']}", headers=ORIGIN)
    assert gone.status_code == 200, gone.text
    assert gone.json()["state"] == DROPPED
    assert gone.json()["reason"] == "cancelled in the voice tracker"
    again = station.client.delete(f"/api/voice-tracks/{booked['break_id']}", headers=ORIGIN)
    assert again.status_code == 409 and again.json()["reason"] == DROPPED
    assert station.client.delete("/api/voice-tracks/nope", headers=ORIGIN).status_code == 404


def test_an_announcer_token_neither_sees_nor_cancels_voice_tracks(station):
    booked = book(station).json()
    track_id = booked["break_id"]
    assert station.call("GET", "/api/announcer/breaks").json()["breaks"] == []
    assert station.call("GET", f"/api/announcer/breaks/{track_id}").status_code == 404
    gone = station.call("DELETE", f"/api/announcer/breaks/{track_id}")
    assert gone.status_code == 404
    assert station.desk.get(track_id).state == QUEUED
    # Its own breaks it still lists and cancels.
    own = station.upload().json()
    listed = station.call("GET", "/api/announcer/breaks").json()["breaks"]
    assert [item["break_id"] for item in listed] == [own["break_id"]]
    cancelled = station.call("DELETE", f"/api/announcer/breaks/{own['break_id']}")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == DROPPED


def test_voice_tracks_and_announcer_breaks_share_the_slot_and_the_quota(tmp_path):
    station = Station(tmp_path, max_per_slot=1)
    assert station.upload().status_code == 201
    full = book(station)
    assert full.status_code == 409 and full.json()["reason"] == "slot_full"
    assert len(files(station.desk)) == 1


def test_manual_uploads_obey_the_station_quota(tmp_path):
    station = Station(tmp_path, max_upload_mb=1, quota_mb=1)
    assert book(station).status_code == 201
    refused = station.upload()
    assert refused.status_code == 507 and refused.json()["reason"] == "quota"
    assert len(files(station.desk)) == 1


def test_a_closed_slot_is_409_with_the_slot_open_now(station):
    slot = voice(station)["slot"]
    closed = book(station, {**slot, "slot_id": "f" * 32})
    assert closed.status_code == 409 and closed.json()["reason"] == "slot_closed"
    assert closed.json()["slot"]["slot_id"] == slot["slot_id"]
    changed = book(station, {**slot, "upcoming": {"id": "someone-else"}})
    assert changed.status_code == 409 and changed.json()["reason"] == "pair_changed"
    assert files(station.desk) == []


@pytest.mark.parametrize(
    ("form", "body", "status", "words"),
    [
        ({"kind": "news"}, None, 422, ""),
        ({"placement": "over"}, None, 422, ""),
        ({}, b"RIFF not really audio", 422, "not audio AutoDJ can play"),
        ({}, wav(seconds=0.0, silence=2.0), 422, "unusable"),
    ],
    ids=["not a dj break", "unknown placement", "not audio", "silent"],
)
def test_bad_recordings_are_refused_and_leave_no_file(station, form, body, status, words):
    reply = book(station, body=body, **form)
    assert reply.status_code == status, reply.text
    assert words in str(reply.json()["detail"])
    assert files(station.desk) == []


def test_manual_duration_is_independent_but_oversize_bodies_are_refused(tmp_path):
    station = Station(tmp_path, max_seconds=5.0, max_upload_mb=2)
    accepted = book(station, body=wav(seconds=6.0))
    assert accepted.status_code == 201, accepted.text
    external = station.upload(body=wav(seconds=6.0))
    assert external.status_code == 422 and "at most 5 seconds" in external.json()["detail"]
    declared = station.client.post(
        "/api/voice-tracks",
        content=b"x",
        headers={
            **ORIGIN,
            "Content-Length": str(400 * 1024 * 1024),
            "Content-Type": "multipart/form-data; boundary=x",
        },
    )
    assert declared.status_code == 413
    assert len(files(station.desk)) == 1


def test_manual_voice_track_booking_does_not_need_external_breaks(tmp_path):
    station = Station(tmp_path)
    station.player._cfg.breaks.enabled = False
    context = voice(station)
    assert context["accepting"] is True and context["reason"] is None
    reply = book(station)
    assert reply.status_code == 201, reply.text


def test_voice_tracker_requests_slot_planning_only_when_supported_and_used(tmp_path):
    station = Station(tmp_path, enabled=False)
    assert station.player._voice_tracking_requested is False
    assert station.player._slot_plan is None
    context = voice(station)
    assert station.player._voice_tracking_requested is True
    assert context["accepting"] is False and "ask again" in context["reason"]
    station.player._plan_slot(station.renders[1])
    assert voice(station)["accepting"] is True


def test_voice_tracker_reports_browser_playback_limitation(station):
    station.player._dry_run = True
    context = voice(station)
    assert context["accepting"] is False and "server playback" in context["reason"]
    assert station.player._voice_tracking_requested is False


def test_turning_external_breaks_off_preserves_manual_voice_tracks(tmp_path):
    from tests.unit.test_break_settings_api import SavingStation

    station = SavingStation(tmp_path)
    station.player._dry_run = False
    track = book(station).json()
    assert station.upload().status_code == 201
    reply = station.client.post("/api/breaks/settings", json={"enabled": False}, headers=ORIGIN)
    assert reply.status_code == 200 and reply.json()["dropped"] == 1
    assert station.desk.get(track["break_id"]).state == QUEUED


def test_changes_need_the_pages_origin(station):
    slot = voice(station)["slot"]
    reply = station.client.post(
        "/api/voice-tracks",
        data={"slot_id": slot["slot_id"], "ending_id": "x", "upcoming_id": "y"},
        files={"file": ("voice-track.wav", wav(), "audio/wav")},
    )
    assert reply.status_code == 403
    other = station.client.post(
        "/api/voice-tracks",
        data={"slot_id": slot["slot_id"], "ending_id": "x", "upcoming_id": "y"},
        files={"file": ("voice-track.wav", wav(), "audio/wav")},
        headers={"Origin": "http://evil.example"},
    )
    assert other.status_code == 403
    assert station.client.delete("/api/voice-tracks/" + "0" * 32).status_code == 403
    assert files(station.desk) == []


def test_changes_are_rate_limited(tmp_path):
    station = Station(tmp_path, requests_per_minute=1)
    assert station.client.delete("/api/voice-tracks/" + "0" * 32, headers=ORIGIN).status_code == 404
    limited = station.client.delete("/api/voice-tracks/" + "0" * 32, headers=ORIGIN)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    # Reading what is booked is not counted.
    assert voice(station)["accepting"] is True


def test_the_tracker_needs_a_session_and_takes_no_machine_token(tmp_path):
    cfg = make_config(tmp_path)
    cfg.server = ServerConfig(
        access_token="voice-tracker-test-access-token-32b",
        allowed_hosts=["127.0.0.1"],
        allowed_origins=[LOCAL["base_url"]],
    )
    player, entries, _points, _desk = make_station(tmp_path, cfg)
    player._dry_run = False
    app = create_app(SimpleNamespace(player=player, library=None))
    client = TestClient(app, **LOCAL)
    start_station(player, entries)
    _token, secret = app.state.machine_tokens().create("announcer", [SCOPE_ANNOUNCER], "default")
    bearer = {"Authorization": f"Bearer {secret}"}
    assert client.get("/api/voice-tracks").status_code == 401
    assert client.get("/api/voice-tracks", headers=bearer).status_code == 401
    refused = client.post(
        "/api/voice-tracks",
        files={"file": ("voice-track.wav", wav(), "audio/wav")},
        headers={**ORIGIN, **bearer},
    )
    assert refused.status_code == 401
    code = app.state.request_pairing_code()[0]
    paired = client.post("/api/pair", json={"code": code, "device_name": "Studio"}, headers=ORIGIN)
    assert paired.status_code == 200, paired.text
    slot = client.get("/api/voice-tracks").json()["slot"]
    reply = client.post(
        "/api/voice-tracks",
        data={
            "slot_id": slot["slot_id"],
            "ending_id": slot["ending"]["id"],
            "upcoming_id": slot["upcoming"]["id"],
        },
        files={"file": ("voice-track.wav", wav(), "audio/wav")},
        headers=ORIGIN,
    )
    assert reply.status_code == 201, reply.text
    # A machine token never reaches the voice tracker by another address.
    assert app.state.stations.default.breaks.get(reply.json()["break_id"]).token_name == (
        "the voice tracker"
    )


def test_a_voice_track_for_the_change_wins_over_a_matched_intro(station):
    # A voice track is booked as a DJ break, so the intro matched to the
    # upcoming song is not added: the operator already talks the song up (#127).
    from pathlib import Path

    from tests.unit.test_matched_intros import _add

    _a, _b, _c, d = station.entries
    store = station.station.intros
    assert station.player.matched_intros is store
    intro_id = _add(store, track_id=f"song-{Path(d.path).stem}", cooldown_minutes=0)
    booked = book(station).json()
    assert booked["kind"] == "dj" and booked["source"] == VOICE_TRACK
    _render_a, render_b, _plan = station.renders
    station.player._on_track_start(render_b)
    render_c = station.player._next_rendered()
    assert render_c.next_entry is d
    assert [event[1] for event in render_c.break_events] == [booked["break_id"]]
    assert render_c.intro_events == ()
    assert intro_id not in store._reserved
    # Without the voice track the same intro would have been chosen.
    assert station.player._matched_intro(d, (), 0.0) is not None


def test_real_ten_minute_mono_wav_uses_manual_limits(tmp_path):
    import io
    import wave

    import numpy as np

    station = Station(tmp_path, max_seconds=5.0, max_upload_mb=2)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        block = np.full(44100, 6000, dtype="<i2").tobytes()
        for _ in range(600):
            audio.writeframesraw(block)
    payload = output.getvalue()
    assert len(payload) == 52_920_044
    reply = book(station, body=payload)
    assert reply.status_code == 201, reply.text
    item = station.desk.get(reply.json()["break_id"])
    assert item.cues.length == pytest.approx(600)
    assert item.cues.speech == pytest.approx(600)
    assert item.limit_s == 600
    # The unmodified announcer path rejects the same file using its own cap.
    assert station.upload(body=payload).status_code == 413


def test_real_overlong_wav_header_is_rejected_before_decode(station):
    import io
    import wave

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x10\x10" * 4_800_080)
    reply = book(station, body=output.getvalue())
    assert reply.status_code == 422, reply.text
    assert files(station.desk) == []
    assert voice(station)["tracks"] == []
