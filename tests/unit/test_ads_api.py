"""The Ads routes end to end (#51): spots, breaks, settings, Run now and the report.

The real app and a real saved-point station (``tests/unit/_break_station.py``)
sit behind the routes, so a spot goes from the upload through the slot plan
into a render.  Requests come as a browser's do: with the page's ``Origin``.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autodj.ads import AIR_OFF, AdLog, PlayRecord, latest
from autodj.breaks import AD_SCHEDULE, QUEUED
from autodj.config import AdsConfig, BreaksConfig
from autodj.server import create_app
from tests.unit._break_station import make_station, start_station
from tests.unit.test_breaks_api import LOCAL, wav
from tests.unit.test_library import make_config

ORIGIN = {"Origin": LOCAL["base_url"]}


class AdStation:
    """The app and a saved-point station with ads on and announcer breaks off."""

    def __init__(self, tmp_path, *, enabled: bool = True) -> None:
        cfg = make_config(tmp_path)
        cfg.ads = AdsConfig(enabled=enabled, max_upload_mb=4, max_spot_seconds=10.0, max_spots=3)
        self.player, self.entries, _points, _desk = make_station(tmp_path, cfg)
        cfg.breaks = BreaksConfig(enabled=False)
        self.app = create_app(SimpleNamespace(player=self.player, library=None))
        self.client = TestClient(self.app, **LOCAL)
        self.station = self.app.state.stations.default
        self.ads = self.station.ads
        self.player.bus = object()  # the server mixes the audio

    def upload(self, *, body: bytes | None = None, name: str = "spot.wav", **form):
        fields = {"title": "Fall sale", "advertiser": "Example Motors", **form}
        return self.client.post(
            "/api/ads/spots",
            data={key: str(value) for key, value in fields.items()},
            files={"file": (name, body if body is not None else wav(), "audio/wav")},
            headers=ORIGIN,
        )

    def add_break(self, **fields):
        body = {"name": "Quarter past", "minutes": [15, 45], **fields}
        return self.client.post("/api/ads/breaks", json=body, headers=ORIGIN)

    def listing(self, path: str = "/api/ads") -> dict:
        reply = self.client.get(path)
        assert reply.status_code == 200, reply.text
        assert reply.headers["cache-control"] == "no-store"
        return reply.json()


@pytest.fixture
def station(tmp_path):
    return AdStation(tmp_path)


def test_the_tab_lists_the_limits_and_why_ads_do_not_air(tmp_path):
    off = AdStation(tmp_path, enabled=False)
    view = off.listing()
    assert view["enabled"] is False and view["reason"] == AIR_OFF
    assert view["limits"]["max_bytes"] == 4 * 1024 * 1024
    assert view["limits"]["max_seconds"] == 10.0
    assert view["spots"] == [] and view["breaks"] == [] and view["problem"] is None
    assert view["settings"] == {
        "separation_minutes": 15,
        "late_minutes": 10,
        "miss_after_minutes": 30,
    }
    reply = off.add_break()
    assert reply.status_code == 201
    run = off.client.post(f"/api/ads/breaks/{reply.json()['id']}/run", headers=ORIGIN)
    assert run.status_code == 409 and run.json()["detail"] == AIR_OFF


def test_a_spot_is_uploaded_decoded_listed_and_served(station):
    reply = station.upload(campaign="Autumn", category="Car dealers", max_per_day="4")
    assert reply.status_code == 201, reply.text
    spot = reply.json()
    assert spot["seconds"] == pytest.approx(2.0, abs=0.1)
    assert (spot["advertiser"], spot["campaign"], spot["max_per_day"]) == (
        "Example Motors",
        "Autumn",
        4,
    )
    stored = list(station.ads.store.folder.iterdir())
    assert [path.name for path in stored] == [f"spot-{spot['id']}.wav"]
    view = station.listing()
    assert [item["id"] for item in view["spots"]] == [spot["id"]]
    assert view["spots"][0]["missing"] is False and view["spots"][0]["in_flight"] is True
    audio = station.client.get(f"/api/ads/spots/{spot['id']}/audio")
    assert audio.status_code == 200 and audio.content == stored[0].read_bytes()
    # The same station by name.
    named = station.listing("/api/stations/default/ads")
    assert [item["id"] for item in named["spots"]] == [spot["id"]]


@pytest.mark.parametrize(
    ("form", "name", "body", "status", "field"),
    [
        ({}, "spot.txt", None, 415, "file"),
        ({}, "spot.wav", b"not audio at all", 422, None),
        ({"advertiser": " "}, "spot.wav", None, 422, "advertiser"),
        ({"end_date": "2026-01-01", "start_date": "2026-02-01"}, "spot.wav", None, 422, "end_date"),
        ({"max_per_hour": "lots"}, "spot.wav", None, 422, "max_per_hour"),
        ({"colour": "red"}, "spot.wav", None, 422, "colour"),
    ],
)
def test_a_bad_upload_is_refused_and_leaves_no_file(station, form, name, body, status, field):
    reply = station.upload(name=name, body=body, **form)
    assert reply.status_code == status, reply.text
    if field is not None:
        assert reply.json()["field"] == field
    folder = station.ads.store.folder
    assert not folder.is_dir() or not list(folder.iterdir())


def test_a_spot_longer_than_the_limit_or_file_too_large_is_refused(station):
    long = station.upload(body=wav(seconds=11.0))
    assert long.status_code == 422 and "at most 10 seconds" in long.json()["detail"]
    big = station.client.post(
        "/api/ads/spots",
        content=b"x" * (5 * 1024 * 1024),
        headers={**ORIGIN, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert big.status_code == 413


def test_the_station_keeps_at_most_max_spots(station):
    for number in range(3):
        assert station.upload(title=f"Spot {number}").status_code == 201
    reply = station.upload(title="One too many")
    assert reply.status_code == 409 and "at most 3 spots" in reply.json()["detail"]


def test_changes_need_the_pages_origin(station):
    reply = station.client.post("/api/ads/breaks", json={"name": "A"})
    assert reply.status_code == 403


def test_spots_are_changed_and_deleted_with_their_audio(station):
    spot = station.upload().json()
    reply = station.client.put(
        f"/api/ads/spots/{spot['id']}",
        json={"title": "Winter sale", "advertiser": "Example Motors", "enabled": False},
        headers=ORIGIN,
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["title"] == "Winter sale" and reply.json()["enabled"] is False
    bad = station.client.put(
        f"/api/ads/spots/{spot['id']}",
        json={"title": "x", "advertiser": "y", "max_per_day": 9999},
        headers=ORIGIN,
    )
    assert bad.status_code == 422 and bad.json()["field"] == "max_per_day"
    huge = station.client.put(
        f"/api/ads/spots/{spot['id']}",
        content=b"{" + b" " * 20000 + b"}",
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    gone = station.client.delete(f"/api/ads/spots/{spot['id']}", headers=ORIGIN)
    assert gone.status_code == 200
    assert not list(station.ads.store.folder.iterdir())
    again = station.client.delete(f"/api/ads/spots/{spot['id']}", headers=ORIGIN)
    assert again.status_code == 404


def test_breaks_are_added_changed_named_once_and_removed(station):
    reply = station.add_break(days=[0, 1, 2, 3, 4], from_hour=6, until_hour=20)
    assert reply.status_code == 201, reply.text
    item = reply.json()
    assert item["when_text"] == (
        "At :15, :45 past the hour, from 06:00 until 20:00, on Monday, Tuesday, Wednesday, "
        "Thursday and Friday"
    )
    assert item["next_text"]
    same = station.add_break(name="quarter PAST")
    assert same.status_code == 409 and same.json()["field"] == "name"
    wrong = station.add_break(name="Other", minutes=[75])
    assert wrong.status_code == 422 and wrong.json()["field"] == "minutes"
    changed = station.client.put(
        f"/api/ads/breaks/{item['id']}",
        json={"name": "Half past", "minutes": [30], "enabled": False},
        headers=ORIGIN,
    )
    assert changed.status_code == 200 and changed.json()["next_at"] is None
    assert station.client.delete(f"/api/ads/breaks/{item['id']}", headers=ORIGIN).status_code == 200
    assert station.listing()["breaks"] == []


def test_settings_are_checked_and_saved(station):
    reply = station.client.put(
        "/api/ads/settings",
        json={"separation_minutes": 20, "late_minutes": 5, "miss_after_minutes": 45},
        headers=ORIGIN,
    )
    assert reply.status_code == 200
    assert station.listing()["settings"]["separation_minutes"] == 20
    bad = station.client.put(
        "/api/ads/settings",
        json={"separation_minutes": 20, "late_minutes": 50, "miss_after_minutes": 45},
        headers=ORIGIN,
    )
    assert bad.status_code == 422 and bad.json()["field"] == "miss_after_minutes"


def test_run_now_books_the_break_on_the_open_song_change(station):
    spot = station.upload().json()
    item = station.add_break(minutes=[]).json()
    renders = start_station(station.player, station.entries)
    assert renders[2] is None  # no break is due: no song change is planned
    reply = station.client.post(f"/api/ads/breaks/{item['id']}/run", headers=ORIGIN)
    assert reply.status_code == 202, reply.text
    assert station.listing()["pending"][0]["source_text"] == "Run now"
    # The desk asked the render worker for the open change; it plans it.
    station.player._plan_slot(renders[1])
    assert station.player._slot_plan is not None
    station.ads.tick()
    desk = station.station.breaks
    (booked,) = [entry for entry in desk.listing() if entry.source == AD_SCHEDULE]
    assert booked.state == QUEUED and booked.label == "Example Motors: Fall sale"
    view = station.listing()
    assert view["pending"] == [] and view["spots"][0]["booked"] is True
    refused = station.client.delete(f"/api/ads/spots/{spot['id']}", headers=ORIGIN)
    assert refused.status_code == 409


def test_the_schedule_offers_an_ad_break_action_that_books_it(station):
    station.upload()
    item = station.add_break(name="Top of the hour", minutes=[]).json()
    actions = station.client.get("/api/schedule").json()["actions"]
    (action,) = [entry for entry in actions if entry["name"] == "ad_break"]
    assert action["fields"][0]["options"] == [
        {"value": "Top of the hour", "label": "Top of the hour"}
    ]
    start_station(station.player, station.entries)
    import asyncio

    asyncio.run(station.station.schedule.actions["ad_break"].handle({"name": "Top of the hour"}))
    (run,) = station.ads.pending()
    assert run.break_id == item["id"] and run.source == "schedule"


def _record(**fields) -> PlayRecord:
    values = {
        "id": "c" * 32,
        "run": "d" * 32,
        "break_name": "Quarter past",
        "scheduled": datetime(2026, 9, 1, 12, 15, tzinfo=UTC),
        "spot_id": "a" * 32,
        "title": "Fall sale",
        "advertiser": "Example Motors",
        "campaign": "Autumn",
        "seconds": 30.0,
        "status": "aired",
        "aired_at": datetime(2026, 9, 1, 12, 19, tzinfo=UTC),
        "logged": datetime(2026, 9, 1, 12, 20, tzinfo=UTC),
        **fields,
    }
    return PlayRecord.model_validate(values)


def test_a_change_too_deeply_nested_is_refused_in_words(station):
    deep = b"[" * 100000 + b"]" * 100000
    reply = station.client.put(
        "/api/ads/settings",
        content=deep[:16000],
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert reply.status_code == 422 and reply.json()["detail"] == "Send the change as JSON"
    nested = b"[" * 8000 + b"]" * 8000
    reply = station.client.put(
        "/api/ads/settings", content=nested, headers={**ORIGIN, "Content-Type": "application/json"}
    )
    assert reply.status_code == 422


def test_every_upload_shares_one_decode_gate():
    import asyncio

    from autodj import ads_api, breaks_api, matched_intros_api

    assert ads_api.decode_gate is breaks_api.decode_gate
    assert matched_intros_api.decode_gate is breaks_api.decode_gate

    async def gates() -> tuple:
        return breaks_api.decode_gate(), breaks_api.decode_gate()

    first, second = asyncio.run(gates())
    assert first is second


def test_the_report_and_its_csv_exports(station):
    log = AdLog(station.station.paths.ad_plays)
    log.append(
        [
            _record(),
            _record(id="e" * 32, status="missed", aired_at=None, reason="skipped before it aired"),
        ]
    )
    reply = station.client.get("/api/ads/report?from=2026-09-01&to=2026-09-01")
    assert reply.status_code == 200, reply.text
    view = reply.json()
    assert view["totals"]["aired"] == 1 and view["totals"]["missed"] == 1
    assert [group["advertiser"] for group in view["summary"]] == ["Example Motors"]
    assert {row["status_text"] for row in view["rows"]} == {"Aired", "Missed"}
    export = station.client.get(
        "/api/ads/report/export?from=2026-09-01&to=2026-09-01&format=affidavit"
    )
    assert export.status_code == 200
    assert export.headers["content-disposition"] == (
        'attachment; filename="autodj-ads-2026-09-01-to-2026-09-01.csv"'
    )
    rows = list(csv.reader(io.StringIO(export.text, newline="")))
    assert rows[0][:3] == ["scheduled", "aired", "status"] and len(rows) == 3
    summary = station.client.get(
        "/api/ads/report/export?from=2026-09-01&to=2026-09-01&format=summary"
    )
    assert summary.text.splitlines()[1] == "Example Motors,Autumn,1,0,1,0,0,30.0"
    for query, status in (
        ("from=2026-09-02&to=2026-09-01", 422),
        ("from=soon&to=2026-09-01", 422),
        ("from=2020-01-01&to=2026-09-01", 422),
    ):
        assert station.client.get(f"/api/ads/report?{query}").status_code == status
    assert latest(log.read(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))[0])
