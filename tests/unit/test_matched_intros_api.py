"""The matched intros routes end to end (#127): upload, list, listen and remove.

The real app with a saved-point station (``tests/unit/_break_station.py``)
sits behind the routes; the library is stood in for with two songs by one
artist, so a song intro and an artist intro can both be made and listed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autodj.config import ServerConfig
from tests._server import create_app
from tests.unit._break_station import make_station
from tests.unit.test_breaks_api import LOCAL, wav
from tests.unit.test_library import make_config

ORIGIN = {"Origin": LOCAL["base_url"]}
SONG = "a" * 32
OTHER = "b" * 32
UNTAGGED = "c" * 32


class Station:
    """The app and a stood-in library: two songs by one artist and one with no artist."""

    def __init__(
        self, tmp_path: Path, *, liner_upload_max_mib: int | None = None, **server: object
    ) -> None:
        cfg = make_config(tmp_path)
        if liner_upload_max_mib is not None:
            cfg.station.liner_upload_max_mib = liner_upload_max_mib
        if server:
            cfg.server = ServerConfig(**server)  # type: ignore[arg-type]
        self.player, self.entries, _points, _desk = make_station(tmp_path, cfg)
        self.player.bus = object()  # the server mixes the audio
        bridge = SimpleNamespace(player=self.player, library=None)
        self.app = create_app(bridge)
        self.client = TestClient(self.app, **LOCAL)
        self.station = self.app.state.stations.default
        self.store = self.station.intros
        self.records = {
            SONG: {
                "id": SONG,
                "kind": "music",
                "title": "New Single",
                "artist": "Example Artist",
                "path": "/music/new.flac",
            },
            OTHER: {
                "id": OTHER,
                "kind": "music",
                "title": "Old Hit",
                "artist": "Example Artist",
                "path": "/music/old.flac",
            },
            UNTAGGED: {
                "id": UNTAGGED,
                "kind": "music",
                "title": "Unknown",
                "artist": "",
                "path": "/music/unknown.flac",
            },
        }
        self.gone: set[str] = set()
        # Registered library folders, for intro rules' liners and folder conditions.
        self.sources: list[dict] = []

        def record_for_path(path: str) -> dict | None:
            return next((r for r in self.records.values() if r["path"] == str(path)), None)

        bridge.library = SimpleNamespace(
            record_for_id=self.records.get,
            catalog=SimpleNamespace(
                record_for_id=self.records.get,
                record_for_path=record_for_path,
                sources=lambda kind=None: [
                    s for s in self.sources if kind is None or s["kind"] == kind
                ],
            ),
            entries_snapshot=lambda kind="music": tuple(
                SimpleNamespace(path=r["path"], genre=r.get("genre", ""))
                for r in self.records.values()
                if r["kind"] == kind
            ),
            is_gone=lambda path: path in self.gone,
        )

    def upload(self, *, body: bytes | None = None, name: str = "intro.wav", **form):
        fields = {"track_id": SONG, **form}
        return self.client.post(
            "/api/intros",
            data={key: str(value) for key, value in fields.items()},
            files={"file": (name, body if body is not None else wav(1.5), "audio/wav")},
            headers=ORIGIN,
        )

    def listing(self, track_id: str | None = SONG) -> dict:
        params = {"track_id": track_id} if track_id else {}
        reply = self.client.get("/api/intros", params=params)
        assert reply.status_code == 200, reply.text
        assert reply.headers["cache-control"] == "no-store"
        return reply.json()


@pytest.fixture
def station(tmp_path: Path) -> Station:
    return Station(tmp_path)


def test_an_intro_is_uploaded_for_a_song_and_listed_with_its_rules(station) -> None:
    reply = station.upload(
        label="New single", chance="50", cooldown_minutes="90", not_after="2026-12-26"
    )
    assert reply.status_code == 201, reply.text
    made = reply.json()
    assert made["applies_to"] == "song" and made["track_id"] == SONG
    assert made["song_title"] == "New Single" and made["target_text"] == "This song"
    assert made["speech_s"] == pytest.approx(1.5, abs=0.1)
    assert made["rules_text"] == (
        "Plays about 50% of the times this song comes up, at most once every 1 hour 30 minutes."
    )
    assert made["dates_text"] == "Plays until 26 December 2026"
    # The audio is kept under a name the server made up.
    (stored,) = sorted(station.store.folder.iterdir())
    assert stored.name == f"intro-{made['id']}.wav"
    listing = station.listing()
    assert listing["active"] is True and listing["reason"] is None
    assert listing["track"] == {"id": SONG, "title": "New Single", "artist": "Example Artist"}
    assert [item["id"] for item in listing["song"]] == [made["id"]]
    assert listing["artist"] == []
    assert listing["limits"]["max_seconds"] == 60.0
    # The same station by name, and every intro without a song.
    named = station.client.get("/api/stations/default/intros").json()
    assert [item["id"] for item in named["intros"]] == [made["id"]]


def test_an_artist_intro_shows_on_every_song_by_the_artist(station) -> None:
    made = station.upload(applies_to="artist").json()
    assert made["applies_to"] == "artist" and made["artist"] == "Example Artist"
    assert made["target_text"] == "Any song by Example Artist"
    listing = station.listing(OTHER)
    assert listing["song"] == [] and [item["id"] for item in listing["artist"]] == [made["id"]]


def test_intro_rule_crud_uses_station_routes_and_limited_json(station) -> None:
    from tests.unit._break_station import write_voice

    station.store.folder.parent.mkdir(parents=True, exist_ok=True)
    liner = write_voice(station.store.folder.parent / "liner.wav", 1)
    station.records["liner-1"] = {"id": "liner-1", "kind": "liner", "path": str(liner)}
    body = {
        "conditions": [{"field": "genre", "operator": "is", "value": "Rock"}],
        "clip_ids": ["liner-1"],
        "chance": 50,
    }
    made = station.client.post("/api/stations/default/intros/rules", json=body, headers=ORIGIN)
    assert made.status_code == 201, made.text
    key = made.json()["id"]
    listed = station.client.get("/api/intros/rules")
    assert listed.status_code == 200
    assert [rule["id"] for rule in listed.json()["rules"]] == [key]
    edited = station.client.put(
        f"/api/intros/rules/{key}", json={**body, "chance": 75}, headers=ORIGIN
    )
    assert edited.status_code == 200 and edited.json()["chance"] == 75
    assert (
        station.client.post(
            "/api/intros/rules", json={**body, "chance": 0}, headers=ORIGIN
        ).status_code
        == 422
    )
    assert (
        station.client.post("/api/intros/rules", content=b"x" * 65537, headers=ORIGIN).status_code
        == 413
    )
    assert (
        station.client.post(
            "/api/intros/rules", json={**body, "priority": "unknown"}, headers=ORIGIN
        ).status_code
        == 422
    )
    gone = station.client.delete(f"/api/intros/rules/{key}", headers=ORIGIN)
    assert gone.status_code == 200
    assert station.client.get("/api/intros/rules").json()["rules"] == []


def test_a_song_with_no_artist_takes_only_a_song_intro(station) -> None:
    reply = station.upload(track_id=UNTAGGED, applies_to="artist")
    assert reply.status_code == 422
    assert reply.json()["field"] == "applies_to"
    assert not station.store.folder.exists() or not list(station.store.folder.iterdir())


@pytest.mark.parametrize(
    ("form", "status", "field"),
    [
        ({"chance": "0"}, 422, "chance"),
        ({"cooldown_minutes": "x"}, 422, "cooldown_minutes"),
        ({"not_before": "2026-12-26", "not_after": "2026-12-01"}, 422, "not_after"),
        ({"applies_to": "album"}, 422, "applies_to"),
        ({"track_id": "../x"}, 422, "track_id"),
        ({"surprise": "1"}, 422, "surprise"),
        ({"track_id": "d" * 32}, 404, "track_id"),
    ],
)
def test_a_refused_field_is_named_and_nothing_is_kept(station, form, status, field) -> None:
    reply = station.upload(**form)
    assert reply.status_code == status, reply.text
    assert reply.json()["field"] == field
    assert station.store.all() == {}
    assert not station.store.folder.exists() or not list(station.store.folder.iterdir())


def test_a_file_that_is_not_an_audio_type_or_not_audio_is_refused(station) -> None:
    reply = station.upload(name="intro.txt")
    assert reply.status_code == 415 and reply.json()["field"] == "file"
    reply = station.upload(body=b"not audio at all")
    assert reply.status_code == 422
    assert station.store.all() == {}
    assert not list(station.store.folder.iterdir())


def test_an_intro_past_the_length_limit_is_refused(station, monkeypatch) -> None:
    monkeypatch.setattr("autodj.matched_intros_api.MAX_INTRO_SECONDS", 1.0)
    reply = station.upload()
    assert reply.status_code == 422
    assert reply.json()["detail"] == "An intro may be at most 1 seconds long"
    assert not list(station.store.folder.iterdir())


def test_an_upload_over_the_liner_cap_is_refused(tmp_path: Path) -> None:
    mib = 1024 * 1024
    station = Station(tmp_path, liner_upload_max_mib=1)
    # Far over: refused on its declared length, before the body is read.
    assert station.upload(body=b"\0" * (mib + 200_000)).status_code == 413
    # Just over, inside the room left for the form: refused as it streams in.
    reply = station.upload(body=b"\0" * (mib + 10_000))
    assert reply.status_code == 413
    assert reply.json()["detail"] == "An intro file may be at most 1 MB"
    assert station.store.all() == {}
    assert not station.store.folder.exists() or not list(station.store.folder.iterdir())


def test_listen_serves_the_audio_and_remove_deletes_it(station) -> None:
    made = station.upload().json()
    audio = station.client.get(f"/api/intros/{made['id']}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.content.startswith(b"RIFF")
    gone = station.client.delete(f"/api/intros/{made['id']}", headers=ORIGIN)
    assert gone.status_code == 200 and gone.json() == {"deleted": made["id"]}
    assert station.store.all() == {}
    assert not list(station.store.folder.iterdir())
    assert station.client.delete(f"/api/intros/{made['id']}", headers=ORIGIN).status_code == 404
    assert station.client.get(f"/api/intros/{made['id']}/audio").status_code == 404
    assert station.client.get("/api/intros/not-an-id/audio").status_code == 404


def test_a_station_without_saved_points_says_why_it_does_not_play_them(station) -> None:
    station.player.bus = None
    listing = station.listing()
    assert listing["active"] is False
    assert "the server mixes the audio" in listing["reason"]
    station.player._cfg.playback.transition_mode = "full_intro_outro"
    listing = station.listing()
    assert listing["active"] is False
    assert "saved segue playback" in listing["reason"]


def test_an_unknown_song_is_404(station) -> None:
    reply = station.client.get("/api/intros", params={"track_id": "e" * 32})
    assert reply.status_code == 404


def test_a_clean_scan_removes_intros_for_songs_that_are_gone(station) -> None:
    from autodj.stations import prune_matched_intros

    kept = station.upload().json()
    gone = station.upload(track_id=OTHER).json()
    by_artist = station.upload(track_id=OTHER, applies_to="artist").json()
    station.gone.add("/music/old.flac")
    assert prune_matched_intros(station.station) == [gone["id"]]
    assert set(station.store.all()) == {kept["id"], by_artist["id"]}
    assert not (station.store.folder / f"intro-{gone['id']}.wav").exists()
    # A song with no record (its folder removed, say) keeps its intro.
    del station.records[SONG]
    assert prune_matched_intros(station.station) == []
    assert kept["id"] in station.store.all()


def test_every_intro_route_needs_a_session(tmp_path: Path) -> None:
    station = Station(tmp_path, access_token="intro-test-token-" + "x" * 32)
    intro = "f" * 32
    for method, path in (
        ("GET", "/api/intros"),
        ("POST", "/api/intros"),
        ("GET", f"/api/intros/{intro}/audio"),
        ("DELETE", f"/api/intros/{intro}"),
        ("POST", "/api/stations/default/intros"),
        ("GET", "/api/intros/rules"),
        ("POST", "/api/intros/rules"),
        ("PUT", f"/api/intros/rules/{intro}"),
        ("DELETE", f"/api/intros/rules/{intro}"),
    ):
        reply = station.client.request(method, path, headers=ORIGIN)
        assert reply.status_code == 401, (method, path, reply.text)


def test_the_listing_says_when_intros_in_the_file_are_skipped(station) -> None:
    import json

    made = station.upload().json()
    document = json.loads(station.store.path.read_text(encoding="utf-8"))
    document["intros"]["broken"] = {"chance": 0}
    station.store.path.write_text(json.dumps(document), encoding="utf-8")
    listing = station.listing()
    assert [item["id"] for item in listing["song"]] == [made["id"]]
    assert listing["problem"].startswith("1 matched intro in matched_intros.json is not valid")
