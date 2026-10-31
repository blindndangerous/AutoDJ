"""Station HTTP route round trip for next-song intro rules."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from autodj.clip_dates import ClipDates
from autodj.matched_intros import Intro, file_name
from tests.unit._break_station import write_voice
from tests.unit.test_matched_intros_api import ORIGIN, Station


def test_rule_round_trip_through_named_station_route(tmp_path: Path) -> None:
    station = Station(tmp_path)
    liner = write_voice(tmp_path / "liner.wav", 1)
    station.records["liner-1"] = {"id": "liner-1", "kind": "liner", "path": str(liner)}
    body = {
        "conditions": [{"field": "artist", "operator": "contains", "value": "Example"}],
        "clip_ids": ["liner-1"],
        "chance": 100,
    }
    created = station.client.post(
        "/api/stations/default/intros/rules",
        json=body,
        headers={**ORIGIN, "Host": "127.0.0.1:8080"},
    )
    assert created.status_code == 201, (created.text, dict(created.request.headers))
    key = created.json()["id"]
    state = station.station.paths.state_dir / "intro_rules.json"
    assert state.is_file()
    listed = station.client.get(
        "/api/stations/default/intros/rules", headers={"Host": "127.0.0.1:8080"}
    )
    assert [item["id"] for item in listed.json()["rules"]] == [key]
    player_library = station.player._library
    player_library.record_for_id = station.records.get
    player_library.entry_for_path = lambda path: SimpleNamespace(path=path)
    player_library.catalog.record_for_path = lambda path: {
        "id": "song-a" if path == "/music/new.flac" else "song-b",
        "kind": "music",
        "artist": "Example Artist",
    }
    incoming = SimpleNamespace(path="/music/new.flac", artist="Example Artist")
    chosen = station.player._matched_intro(incoming, (), 1_800_000_000.0)
    assert chosen is not None and chosen.intro_id == f"rule:{key}"
    station.store.release((chosen.intro_id,))
    song_intro_id = "f" * 32
    station.store.folder.mkdir(parents=True, exist_ok=True)
    write_voice(station.store.folder / file_name(song_intro_id, ".wav"), 1)
    station.store.add(
        song_intro_id, Intro(file=file_name(song_intro_id, ".wav"), track_id="song-a")
    )
    raised = station.client.put(
        f"/api/intros/rules/{key}",
        json={**body, "priority": "before_song"},
        headers={**ORIGIN, "Host": "127.0.0.1:8080"},
    )
    assert raised.status_code == 200, raised.text
    chosen = station.player._matched_intro(incoming, (), 1_800_003_600.0)
    assert chosen is not None and chosen.intro_id == f"rule:{key}"
    station.store.release((chosen.intro_id,))
    # The station's liner filter (its own liners, within their play dates)
    # judges a rule's clips at the time they would air.
    seen = []
    station.player.liner_filter = lambda entries, at=None: seen.append(at) or ()
    chosen = station.player._matched_intro(incoming, (), 1_800_007_200.0)
    assert chosen is not None and chosen.intro_id == song_intro_id  # the song's own, next
    assert seen == [1_800_007_200.0]
    station.store.release((chosen.intro_id,))
    station.player.liner_filter = None
    artist_intro_id = "e" * 32
    write_voice(station.store.folder / file_name(artist_intro_id, ".wav"), 1)
    station.store.add(
        artist_intro_id,
        Intro(file=file_name(artist_intro_id, ".wav"), artist="Example Artist"),
    )
    middle = station.client.put(
        f"/api/intros/rules/{key}",
        json={**body, "priority": "before_artist"},
        headers={**ORIGIN, "Host": "127.0.0.1:8080"},
    )
    assert middle.status_code == 200, middle.text
    other = SimpleNamespace(path="/music/old.flac", artist="Example Artist")
    chosen = station.player._matched_intro(other, (), 1_800_005_400.0)
    assert chosen is not None and chosen.intro_id == f"rule:{key}"
    station.store.release((chosen.intro_id,))
    lowered = station.client.put(
        f"/api/intros/rules/{key}",
        json={**body, "priority": "after_artist"},
        headers={**ORIGIN, "Host": "127.0.0.1:8080"},
    )
    assert lowered.status_code == 200, lowered.text
    chosen = station.player._matched_intro(incoming, (), 1_800_007_200.0)
    assert chosen is not None and chosen.intro_id == song_intro_id
    station.store.release((chosen.intro_id,))
    chosen = station.player._matched_intro(other, (), 1_800_010_800.0)
    assert chosen is not None and chosen.intro_id == artist_intro_id
    assert (
        station.client.delete(
            f"/api/stations/default/intros/rules/{key}",
            headers={**ORIGIN, "Host": "127.0.0.1:8080"},
        ).status_code
        == 200
    )
    assert (
        station.client.get("/api/intros/rules", headers={"Host": "127.0.0.1:8080"}).json()["rules"]
        == []
    )


# This folder's app checks the Host header, as a real browser sends it.
HOST = {"Host": "127.0.0.1:8080"}
SENT = {**ORIGIN, **HOST}


def _liner_station(tmp_path: Path) -> tuple[Station, Path]:
    station = Station(tmp_path)
    folder = tmp_path / "liners"
    folder.mkdir()
    liner = write_voice(folder / "rock.wav", 1)
    station.records["liner-1"] = {"id": "liner-1", "kind": "liner", "path": str(liner)}
    station.sources.append({"id": "lin", "kind": "liner", "path": str(folder), "recursive": True})
    station.sources.append({"id": "mus", "kind": "music", "path": "/music", "recursive": True})
    return station, folder


RULE = {
    "conditions": [{"field": "genre", "operator": "is", "value": "Rock"}],
    "clip_ids": ["liner-1"],
}


def test_listing_offers_the_stations_liners_and_liner_folders(tmp_path: Path) -> None:
    station, folder = _liner_station(tmp_path)
    listed = station.client.get("/api/intros/rules", headers=HOST).json()
    assert listed["liners"] == [{"id": "liner-1", "label": "rock.wav"}]
    assert listed["liner_folders"] == [str(folder)]
    assert listed["max_rules"] == 2000 and listed["problem"] is None
    assert listed["sources"] == [{"id": "mus", "path": "/music"}]


def test_a_refused_rule_names_the_field_at_fault(tmp_path: Path) -> None:
    station, folder = _liner_station(tmp_path)

    def refused(body: dict) -> dict:
        reply = station.client.post("/api/intros/rules", json=body, headers=SENT)
        assert reply.status_code == 422, reply.text
        return reply.json()

    assert refused({**RULE, "chance": 0})["field"] == "chance"
    assert refused({**RULE, "clip_ids": []}) == {
        "detail": "Choose at least one voice liner or a liner folder",
        "field": "clip_ids",
    }
    assert refused({**RULE, "clip_ids": ["nope"]})["field"] == "clip_ids"
    assert refused({**RULE, "liner_folder": str(tmp_path / "elsewhere")})["field"] == (
        "liner_folder"
    )
    dates = refused({**RULE, "not_before": "2026-10-10", "not_after": "2026-10-01"})
    assert dates["field"] == "not_after"
    bad = refused({**RULE, "conditions": [{"field": "genre", "operator": "between"}]})
    assert bad["field"] == "conditions" and bad["detail"].startswith("Condition 1: ")
    unknown = refused(
        {
            **RULE,
            "conditions": [{"field": "folder", "source": "gone", "folder": "", "recursive": True}],
        }
    )
    assert unknown["field"] == "conditions"
    assert refused({**RULE, "last_aired": 1.0})["field"] == ""
    # A folder condition on a registered music folder and a liner subfolder are fine.
    sub = folder / "rock"
    sub.mkdir()
    made = station.client.post(
        "/api/intros/rules",
        json={
            **RULE,
            "liner_folder": str(sub),
            "conditions": [{"field": "folder", "source": "mus", "folder": "", "recursive": True}],
        },
        headers=SENT,
    )
    assert made.status_code == 201, made.text


def test_filename_pattern_preview_is_read_only_and_respects_liner_dates(tmp_path: Path) -> None:
    station, folder = _liner_station(tmp_path)
    song = station.records["a" * 32]
    song.update(year=2024, album="First", genre="Rock", bpm=101.6)
    for key, name in (
        ("d" * 32, "Example Artist - New Single.wav"),
        ("e" * 32, "Example Artist - Other.wav"),
        ("f" * 32, "Other Artist - New Single.wav"),
    ):
        path = folder / name
        write_voice(path, 1)
        station.records[key] = {"id": key, "kind": "liner", "path": str(path)}
    station.station.bridge.library.entry_for_path = lambda path: next(
        (
            SimpleNamespace(**record)
            for record in station.records.values()
            if record["path"] == str(path)
        ),
        None,
    )
    station.station.liner_gate.store.set("e" * 32, ClipDates(not_after=date(2000, 1, 1)))
    body = {
        "conditions": [{"field": "genre", "operator": "is", "value": "Rock"}],
        "clip_ids": [],
        "liner_folder": str(folder),
        "filename_pattern": "{artist} - *.wav",
    }
    dates_file = station.station.paths.state_dir / "liner_dates.json"
    saved_dates = dates_file.read_bytes()
    reply = station.client.post(
        "/api/intros/rules/test",
        params={"path": song["path"]},
        json=body,
        headers=SENT,
    )
    assert reply.status_code == 200, reply.text
    preview = reply.json()
    assert preview["expanded_pattern"] == "Example Artist - *.wav"
    assert preview["missing_fields"] == []
    assert {item["id"] for item in preview["matches"]} == {"d" * 32, "e" * 32}
    expired = next(item for item in preview["matches"] if item["id"] == "e" * 32)
    assert expired["eligible"] is False and expired["reason"].startswith("expired after")
    assert next(item for item in preview["matches"] if item["id"] == "d" * 32)["eligible"]
    assert "not rolled or consumed" in preview["note"]
    assert not station.station.paths.state_dir.joinpath("intro_rules.json").exists()
    assert dates_file.read_bytes() == saved_dates

    Path(station.records["d" * 32]["path"]).unlink()
    missing_file = station.client.post(
        "/api/intros/rules/test", params={"path": song["path"]}, json=body, headers=SENT
    )
    missing_clip = next(item for item in missing_file.json()["matches"] if item["id"] == "d" * 32)
    assert not missing_clip["eligible"] and "missing" in missing_clip["reason"]
    song.pop("album")
    missing = station.client.post(
        "/api/intros/rules/test",
        params={"path": song["path"]},
        json={**body, "filename_pattern": "{album}.wav"},
        headers=SENT,
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["missing_fields"] == ["album"]
    assert missing.json()["matches"] == []
    without_pattern = station.client.post(
        "/api/intros/rules/test",
        params={"path": song["path"]},
        json={key: value for key, value in body.items() if key != "filename_pattern"},
        headers=SENT,
    )
    assert without_pattern.status_code == 422
    assert without_pattern.json()["field"] == "filename_pattern"
    assert "cache-control" not in without_pattern.headers
    invalid_song = station.client.post(
        "/api/intros/rules/test",
        params={"path": "/music/gone.flac"},
        json=body,
        headers=SENT,
    )
    assert invalid_song.status_code == 404 and invalid_song.json()["field"] == "path"
    assert "cache-control" not in invalid_song.headers
    invalid_pattern = station.client.post(
        "/api/intros/rules/test",
        params={"path": song["path"]},
        json={**body, "filename_pattern": "{unknown}"},
        headers=SENT,
    )
    assert (
        invalid_pattern.status_code == 422 and invalid_pattern.json()["field"] == "filename_pattern"
    )
    assert invalid_pattern.headers["cache-control"] == "no-store"
    saved = station.client.post("/api/intros/rules", json=body, headers=SENT)
    assert saved.status_code == 201, saved.text
    listed = station.client.get("/api/intros/rules", headers=HOST).json()["rules"]
    assert listed[0]["filename_pattern"] == body["filename_pattern"]
