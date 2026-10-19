"""Stopped-station edition production and trusted review approval over real routes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autodj.prepared_api import install_prepared
from autodj.prepared_editions import edition_store, relevant_signature
from autodj.station_routes import StationRouter, editable_station
from tests.unit.test_prepared_editions import voice
from tests.unit.test_review_source import station_content


def production(tmp_path):
    station, show_id, _entries = station_content(tmp_path)
    proofs = {}
    active = []
    app = FastAPI()
    app.dependency_overrides[editable_station] = lambda: station
    install_prepared(
        StationRouter(app.router),
        receipt=lambda station, identity: proofs.get(identity, {"receipts": []}),
        active=lambda station, identity: identity in active,
    )
    return TestClient(app), station, show_id, proofs, active


def test_stopped_create_record_review_approve_and_asset_download(tmp_path):
    client, station, show_id, proofs, _ = production(tmp_path)
    created = client.post("/api/editions", json={"show_id": show_id})
    assert created.status_code == 201, created.text
    edition = created.json()
    identity = edition["id"]
    recorded = client.post(
        f"/api/editions/{identity}/takes",
        params={
            "expected_revision": edition["revision"],
            "filename": "voice.wav",
            "position": "opening",
            "after_id": edition["occurrences"][0]["id"],
        },
        content=voice(tmp_path).read_bytes(),
        headers={"Content-Type": "audio/wav"},
    )
    assert recorded.status_code == 201, recorded.text
    take = recorded.json()["takes"][0]
    assert take["status"] == "needs_review"
    assert client.get(
        f"/api/stations/default/editions/{identity}/assets/" + take["asset_id"]
    ).content.startswith(b"RIFF")
    review_id = "a" * 32
    assert (
        client.post(
            f"/api/editions/{identity}/takes/{take['id']}/approve",
            json={"review_id": review_id, "keep_complete": True},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/editions/{identity}/takes/{take['id']}/approve", json={"review_id": review_id}
        ).status_code
        == 409
    )
    saved = edition_store(station).get(identity)
    proofs[review_id] = {
        "receipts": [
            {
                "edition_id": identity,
                "take_id": take["id"],
                "asset_id": take["asset_id"],
                "review_id": review_id,
                "reviewed_revision": saved.revision,
                "dependency_hash": relevant_signature(saved, saved.takes[0], station),
                "keep_complete": True,
                "omitted": False,
            }
        ]
    }
    approved = client.post(
        f"/api/editions/{identity}/takes/{take['id']}/approve", json={"review_id": review_id}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["takes"][0]["status"] == "ready"
    assert station.player is None


def test_stale_save_bad_upload_and_active_delete_preserve_edition(tmp_path):
    client, station, show_id, _, active = production(tmp_path)
    edition = client.post("/api/editions", json={"show_id": show_id}).json()
    identity = edition["id"]
    changed = client.put(
        f"/api/editions/{identity}",
        json={"expected_revision": edition["revision"], "name": "Changed"},
    )
    assert changed.status_code == 200
    assert (
        client.put(
            f"/api/editions/{identity}",
            json={"expected_revision": edition["revision"], "name": "Stale"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/editions/{identity}/takes",
            params={
                "expected_revision": changed.json()["revision"],
                "filename": "bad.wav",
                "position": "closing",
                "before_id": edition["occurrences"][-1]["id"],
            },
            content=b"not audio",
        ).status_code
        == 400
    )
    assert edition_store(station).get(identity).name == "Changed"
    active.append(identity)
    assert (
        client.delete(
            f"/api/editions/{identity}", params={"expected_revision": changed.json()["revision"]}
        ).status_code
        == 409
    )
    assert client.get(f"/api/editions/{identity}").status_code == 200


def test_adding_song_without_library_returns_conflict(tmp_path):
    from autodj.prepared_editions import Occurrence

    client, station, show_id, _, _ = production(tmp_path)
    edition = client.post("/api/editions", json={"show_id": show_id}).json()
    station.bridge.library = None
    response = client.put(
        f"/api/editions/{edition['id']}",
        json={
            "expected_revision": edition["revision"],
            "occurrences": [
                *(
                    row.model_dump()
                    for row in edition_store(station).get(edition["id"]).occurrences
                ),
                Occurrence(path="new.wav").model_dump(),
            ],
        },
    )
    assert response.status_code == 409
    assert "library" in response.json()["detail"].lower()


@pytest.mark.parametrize("change", ["reorder", "remove"])
def test_existing_songs_can_be_edited_without_library(tmp_path, change):
    client, station, show_id, _, _ = production(tmp_path)
    store = edition_store(station)
    edition = store.create(show_id, station.shows.get(show_id))
    rows = (
        list(reversed(edition.occurrences))
        if change == "reorder"
        else list(edition.occurrences[1:])
    )
    station.bridge.library = None
    response = client.put(
        f"/api/editions/{edition.id}",
        json={
            "expected_revision": edition.revision,
            "occurrences": [row.model_dump() for row in rows],
        },
    )
    assert response.status_code == 200, response.text
    assert store.get(edition.id).occurrences == tuple(rows)


@pytest.mark.parametrize("prefix", ["/api", "/api/stations/default"])
def test_reattach_route_keeps_source_and_rejects_stale_revision(tmp_path, prefix):
    """ADR-0015-01 ADR-0015-02: explicit reattachment retains audio and validates revisions."""
    from tests.unit.test_prepared_editions import add_take

    client, station, show_id, _, _ = production(tmp_path)
    store = edition_store(station)
    edition = store.create(show_id, station.shows.get(show_id))
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    edition = store.edit(
        edition.id, edition.revision, occurrences=list(reversed(edition.occurrences))
    )
    url = f"{prefix}/editions/{edition.id}/takes/{take.id}/reattach"
    body = {
        "expected_revision": edition.revision,
        "before_id": edition.occurrences[0].id,
        "after_id": edition.occurrences[1].id,
    }
    result = client.post(url, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["takes"][0]["asset_id"] == take.asset_id
    assert result.json()["takes"][0]["status"] == "needs_review"
    original = store.path.read_bytes()
    assert client.post(url, json=body).status_code == 409
    assert store.path.read_bytes() == original
    assert store.asset_path(take.asset_id).is_file()


def test_closing_approval_succeeds_while_opening_clip_plays(tmp_path, monkeypatch):
    """ADR-0015-08: committed opening speech does not lock a future closing approval."""
    from types import SimpleNamespace

    from autodj.identity_map import attached
    from tests.unit.test_prepared_editions import receipt
    from tests.unit.test_prepared_tail import _current_handoff

    client, station, show_id, proofs, _ = production(tmp_path)
    store = edition_store(station)
    edition = store.create(show_id, station.shows.get(show_id))
    edition = store.import_take(
        edition.id,
        edition.revision,
        voice(tmp_path),
        position="closing",
        before_id=edition.occurrences[-1].id,
    )
    take = edition.takes[0]
    proof = receipt(station, store, edition, take)
    proofs[proof.review_id] = {"receipts": [proof.model_dump()]}
    live = tmp_path / "live"
    live.mkdir()
    runner, source, _, track, _, _, _ = _current_handoff(live)
    track.entry._prepared_clip = True
    attached(source.player, "_edition_occurrences")[track.entry] = SimpleNamespace(
        occurrence_id="opening",
        take_id="opening",
    )
    runner._on_air.edition_id = edition.id
    station.show_runner = runner
    monkeypatch.setattr(runner, "prepared_changed", lambda: None)
    approved = client.post(
        f"/api/editions/{edition.id}/takes/{take.id}/approve",
        json={"review_id": proof.review_id},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["takes"][0]["status"] == "ready"


def test_live_mutation_rechecks_boundary_and_notifies_only_after_success(tmp_path):
    from types import SimpleNamespace

    client, station, show_id, _, _ = production(tmp_path)
    edition = client.post("/api/editions", json={"show_id": show_id}).json()
    identity = edition["id"]
    allowed = [True]
    notifications = []
    running = {"edition_id": identity, "committed_audio_notice": None}

    def notify_changed():
        notifications.append(edition_store(station).get(identity).revision)
        running["committed_audio_notice"] = (
            "Previously approved audio continues for this occurrence; edits apply to future playback"
        )

    station.show_runner = SimpleNamespace(
        prepared_editable=lambda requested, **_changes: requested == identity and allowed[0],
        prepared_changed=notify_changed,
        status=lambda: dict(running),
    )
    changed = client.put(
        f"/api/editions/{identity}",
        json={
            "expected_revision": edition["revision"],
            "name": "Edited live",
        },
    )
    assert changed.status_code == 200, changed.text
    assert notifications == [changed.json()["revision"]]
    assert changed.json()["committed_audio_notice"] == running["committed_audio_notice"]
    running["edition_id"] = "f" * 32
    assert client.get(f"/api/editions/{identity}").json()["committed_audio_notice"] is None
    stale = client.put(
        f"/api/editions/{identity}",
        json={
            "expected_revision": edition["revision"],
            "name": "Stale",
        },
    )
    assert stale.status_code == 409
    assert len(notifications) == 1
    allowed[0] = False
    original = edition_store(station).path.read_bytes()
    blocked = client.put(
        f"/api/editions/{identity}",
        json={
            "expected_revision": changed.json()["revision"],
            "name": "Too late",
        },
    )
    assert blocked.status_code == 409 and "committed" in blocked.json()["detail"]
    assert edition_store(station).path.read_bytes() == original
    assert len(notifications) == 1


def test_interrupted_edition_status_is_visible_and_isolated_from_other_runs(tmp_path):
    from types import SimpleNamespace

    from autodj.shows import ShowRunner

    client, station, show_id, _, _ = production(tmp_path)
    edition = client.post("/api/editions", json={"show_id": show_id}).json()
    identity = edition["id"]
    occurrence = edition["occurrences"][1]["id"]
    interrupted = ShowRunner._interrupted_status(
        {
            "edition_id": identity,
            "name": edition["name"],
            "songs": ["one", "two"],
            "resume_occurrence": occurrence,
            "occurrences": [row["id"] for row in edition["occurrences"]],
            "played": 1,
        }
    )
    status = [interrupted]
    station.show_runner = SimpleNamespace(status=lambda: status[0])
    response = client.get(f"/api/editions/{identity}")
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["interrupted"] is True and run["pending_resume"] is False
    assert run["resume_occurrence"] == occurrence
    assert run["played"] == 1 and run["edition_id"] == identity
    # A different on-air edition must not hide the separately interrupted run.
    status[0] = {
        "edition_id": "f" * 32,
        "interrupted_edition": interrupted,
        "committed_audio_notice": "Another edition notice",
    }
    response = client.get(f"/api/editions/{identity}").json()
    assert response["run"] == run
    assert response["committed_audio_notice"] is None
    status[0] = {"edition_id": "f" * 32, "interrupted_edition": None}
    assert client.get(f"/api/editions/{identity}").json()["run"] is None
    # Matching active status exposes that run, without another edition's history.
    status[0] = {
        "edition_id": identity,
        "played": 2,
        "interrupted_edition": {"edition_id": "f" * 32, "interrupted": True},
    }
    assert client.get(f"/api/editions/{identity}").json()["run"] == {
        "edition_id": identity,
        "played": 2,
    }


def test_start_route_queues_and_resumes_real_runner_on_both_station_aliases(tmp_path):
    from autodj.shows import ShowRunner
    from autodj.station_routes import current_station
    from tests.unit.test_prepared_playout import setup

    runner, source, bridge, editions, station = setup(tmp_path)
    for index, row in enumerate(editions.plan.items):
        row.occurrence_id = f"{index + 1:032x}"
    station.show_runner = runner
    app = FastAPI()
    app.dependency_overrides[current_station] = lambda: station
    install_prepared(StationRouter(app.router), receipt=lambda *_: {"receipts": []})
    with TestClient(app) as client:
        url = f"/api/editions/{editions.plan.edition_id}/start"
        assert client.post(url, json={"now": "false"}).status_code == 422
        response = client.post(url, json={})
        assert response.status_code == 200, response.text
        assert "message" in response.json()
        assert (
            source.player._state.queue[0]._prepared_occurrence_id
            == editions.plan.items[0].occurrence_id
        )
        # Simulate a process restart: ordinary automatic resume must not run.
        source.player._state.queue.clear()
        restarted = ShowRunner(
            bridge,
            runner._store,
            runner._publisher,
            state_path=tmp_path / "show_on_air.json",
            edition_store=editions,
            edition_station=station,
        )
        station.show_runner = restarted
        alias = f"/api/stations/default/editions/{editions.plan.edition_id}/start"
        refused = client.post(alias, json={})
        assert refused.status_code == 409 and "Choose an occurrence" in refused.json()["detail"]
        resumed = client.post(alias, json={"from_occurrence": editions.plan.items[1].occurrence_id})
        assert resumed.status_code == 200, resumed.text
        assert (
            source.player._state.queue[0]._prepared_occurrence_id
            == editions.plan.items[1].occurrence_id
        )
        source.player._state.queue.clear()
        bridge.stream = None
        source.player.bus = None
        refused = client.post(alias, json={"confirm_reair": True})
        assert (
            refused.status_code == 409 and "server audio or streaming" in refused.json()["detail"]
        )
        assert source.player._state.queue == []


@pytest.mark.parametrize("known_size", [True, False])
def test_upload_deadline_scales_with_body_and_excludes_decode(tmp_path, monkeypatch, known_size):
    import contextlib

    import autodj.prepared_api as module
    from autodj.breaks_api import BODY_DEADLINE_SECONDS, MIN_BODY_BYTES_PER_SECOND

    client, station, show_id, _, _ = production(tmp_path)
    edition = client.post("/api/editions", json={"show_id": show_id}).json()
    body = voice(tmp_path).read_bytes()
    deadlines = []
    active = False
    timeout = module.asyncio.timeout

    @contextlib.asynccontextmanager
    async def tracked(seconds):
        nonlocal active
        deadlines.append(seconds)
        async with timeout(seconds):
            active = True
            try:
                yield
            finally:
                active = False

    store = edition_store(station)
    original = store.import_take

    def decode(*args, **kwargs):
        assert not active, "body deadline must not include decoding"
        return original(*args, **kwargs)

    monkeypatch.setattr(module.asyncio, "timeout", tracked)
    monkeypatch.setattr(store, "import_take", decode)
    response = client.post(
        f"/api/editions/{edition['id']}/takes",
        params={
            "expected_revision": edition["revision"],
            "filename": "voice.wav",
            "position": "opening",
            "after_id": edition["occurrences"][0]["id"],
        },
        content=body,
        headers={} if known_size else {"content-length": ""},
    )
    assert response.status_code == 201, response.text
    size = len(body) if known_size else module.MAX_UPLOAD_BYTES
    assert deadlines == [BODY_DEADLINE_SECONDS + size / MIN_BODY_BYTES_PER_SECOND]
