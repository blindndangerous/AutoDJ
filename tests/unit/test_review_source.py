"""Review snapshots use station content without starting or changing playout."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from autodj.review_source import capture_review, source_fingerprint
from autodj.shows import Show, ShowStore
from autodj.station_settings import StationSettings
from autodj.stations import StationPaths
from tests.unit._rotation_station import make_station, song


def station_content(tmp_path, *, seconds=0.1):
    entries = []
    for number in range(3):
        path = tmp_path / f"{number}.wav"
        sf.write(path, np.zeros((round(seconds * 44100), 2), np.float32), 44100)
        entries.append(replace(song(number, f"Artist {number}", length=seconds), path=str(path)))
    source = make_station(tmp_path / "index", entries, playback={"pick_mode": "random"})
    paths = StationPaths.default(source.player._cfg)
    settings = StationSettings(source.player._cfg, source.library, source.player._sim, paths)
    settings.entry_for_path = source.library.entry_for_path
    shows = ShowStore(paths.shows)
    show_id, _ = shows.add(Show(name="Review", source="category", category="review"))
    shows.update(
        show_id,
        lambda show: show.model_copy(
            update={"songs": [entries[0].path, entries[1].path, entries[0].path]}
        ),
    )
    station = SimpleNamespace(
        player=None,
        cfg=settings.cfg,
        paths=paths,
        slug="default",
        timezone="UTC",
        bridge=settings,
        ledger=source.ledger,
        shows=shows,
        schedule=None,
        time_checks=None,
        ads=None,
        carts=None,
        intros=None,
    )
    return station, show_id, entries


def test_saved_show_capture_while_stopped_preserves_occurrences_and_no_writes(tmp_path):
    station, identity, entries = station_content(tmp_path)
    before = station.ledger.writes
    request = {"source": "show", "show_id": identity, "start": "2026-10-18T12:00:00Z"}
    plan = capture_review(station, request)
    songs = [item for item in plan.items if item.kind == "song"]
    assert [item.entry.path for item in songs] == [
        entries[0].path,
        entries[1].path,
        entries[0].path,
    ]
    assert len({item.occurrence_id for item in songs}) == 3
    assert station.player is None and station.ledger.writes == before
    assert plan.source.player is not None
    assert plan.start == datetime(2026, 10, 18, 12, tzinfo=UTC).timestamp()


def test_fingerprint_changes_for_audio_and_config_but_not_wall_clock(tmp_path):
    station, identity, entries = station_content(tmp_path)
    request = {"source": "show", "show_id": identity}
    first = source_fingerprint(station, request)
    assert first == source_fingerprint(station, request)
    station.cfg.playback.music_speed_percent = 2
    assert first != source_fingerprint(station, request)
    second = source_fingerprint(station, request)
    with open(entries[0].path, "ab") as output:
        output.write(b"changed")
    assert second != source_fingerprint(station, request)


def test_play_during_capture_and_render_does_not_invalidate_saved_review(tmp_path, monkeypatch):
    from autodj import hour_preview
    from autodj.catalog import LibraryCatalog
    from autodj.review_render import render_review

    station, identity, entries = station_content(tmp_path)
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(tmp_path)
        for entry in entries:
            catalog.put_entry(entry)
        station.bridge.library.catalog = catalog
        request = {"source": "show", "show_id": identity}
        before = source_fingerprint(station, request)
        predict = hour_preview.predict

        def play_during_capture(*args, **kwargs):
            catalog.remember_play(entries[0].path, 100)
            return predict(*args, **kwargs)

        monkeypatch.setattr(hour_preview, "predict", play_during_capture)
        plan = capture_review(station, request)
        assert source_fingerprint(station, request) == before
        output = tmp_path / "result"
        output.mkdir()
        played = False

        def play_during_render():
            nonlocal played
            if not played:
                catalog.remember_play(entries[1].path, 200)
                played = True
            return False

        render_review(plan.source, plan.items, output, cancelled=play_during_render)
        assert played
        assert plan.fingerprint == source_fingerprint(station, plan.request)


def test_unrelated_analysis_during_render_keeps_saved_review_fresh(tmp_path):
    """ADR-0014-09: unrelated entry analysis preserves saved and running reviews."""
    from autodj.catalog import LibraryCatalog
    from autodj.indexer import FEATURE_DIM
    from autodj.review_api import _result_view
    from autodj.review_render import render_review

    station, identity, entries = station_content(tmp_path)
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(tmp_path)
        for entry in entries:
            catalog.put_entry(entry)
        station.bridge.library.catalog = catalog
        plan = capture_review(station, {"source": "show", "show_id": identity})
        result = {
            "id": "a" * 32,
            "created_at": 100,
            "metadata": {"_request": plan.request, "_fingerprint": plan.fingerprint},
        }
        assert _result_view(station, result)["stale"] is False
        before = source_fingerprint(station, plan.request, include_live_state=False)
        target = catalog.record_for_path(entries[2].path)
        written = False

        def analyze_unrelated_track():
            nonlocal written
            if not written:
                assert catalog.store_analysis(
                    target["path"],
                    target["size"],
                    target["fingerprint"],
                    {"energy": 0.25, "key": 5, "mode": 0, "bpm": 125, "tempo_confidence": 0.95},
                    np.ones(FEATURE_DIM, np.float32),
                )
                written = True
            return False

        output = tmp_path / "result"
        output.mkdir()
        metadata = render_review(plan.source, plan.items, output, cancelled=analyze_unrelated_track)
        assert written and metadata["duration"] > 0
        assert (output / "review.wav").is_file() and (output / "review.mp3").is_file()
        assert before == source_fingerprint(station, plan.request, include_live_state=False)
        assert _result_view(station, result)["stale"] is False


@pytest.mark.parametrize(
    "field, value",
    [("energy", 0.25), ("key", 5), ("mode", 0), ("bpm", 125), ("tempo_confidence", 0.95)],
)
def test_selected_track_analysis_marks_saved_review_stale(tmp_path, field, value):
    """ADR-0014-09: changes to selected entry analysis remain relevant."""
    from autodj.catalog import LibraryCatalog
    from autodj.indexer import FEATURE_DIM
    from autodj.review_api import _result_view

    station, identity, entries = station_content(tmp_path)
    entries[0].bpm = 0  # Analysis fills an unknown BPM; it preserves an existing source BPM.
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(tmp_path)
        for entry in entries:
            catalog.put_entry(entry)
        station.bridge.library.catalog = catalog
        plan = capture_review(station, {"source": "show", "show_id": identity})
        result = {
            "id": "a" * 32,
            "created_at": 100,
            "metadata": {"_request": plan.request, "_fingerprint": plan.fingerprint},
        }
        assert _result_view(station, result)["stale"] is False
        target = catalog.record_for_path(entries[0].path)
        analysis = {
            key: target[key] for key in ("energy", "key", "mode", "bpm", "tempo_confidence")
        }
        analysis[field] = value
        assert catalog.store_analysis(
            target["path"],
            target["size"],
            target["fingerprint"],
            analysis,
            np.ones(FEATURE_DIM, np.float32),
        )
        assert _result_view(station, result)["stale"] is True


@pytest.mark.parametrize("first_play", [False, True])
def test_loudness_cache_write_during_render_keeps_saved_review_fresh(tmp_path, first_play):
    """ADR-0014-09: unrelated and first-play analysis are not source edits."""
    from pathlib import Path

    from autodj.catalog import LibraryCatalog
    from autodj.processing import analyze_loudness
    from autodj.review_api import _result_view
    from autodj.review_render import render_review

    station, identity, entries = station_content(tmp_path)
    station.cfg.playback.loudness_processing = "smooth" if first_play else "off"
    samples = 0.1 * np.sin(2 * np.pi * 220 * np.arange(44100) / 44100)
    for entry in entries:
        sf.write(entry.path, np.repeat(samples[:, None], 2, axis=1), 44100)
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(tmp_path)
        for entry in entries:
            catalog.put_entry(entry)
        station.bridge.library.catalog = catalog
        target = entries[0] if first_play else entries[2]
        measurement = analyze_loudness(Path(target.path))
        plan = capture_review(station, {"source": "show", "show_id": identity})
        before = source_fingerprint(station, plan.request, include_live_state=False)
        written = False

        def write_measurement():
            nonlocal written
            if not written:
                catalog.set_loudness(target.path, measurement)
                written = True
            return False

        output = tmp_path / "result"
        output.mkdir()
        metadata = render_review(plan.source, plan.items, output, cancelled=write_measurement)
        assert written
        assert before == source_fingerprint(station, plan.request, include_live_state=False)
        metadata.update(_request=plan.request, _fingerprint=plan.fingerprint)
        result = {"id": "a" * 32, "created_at": 100, "metadata": metadata}
        assert _result_view(station, result)["stale"] is False
        catalog.set_points(entries[2].path, {"start": 0.01})
        assert _result_view(station, result)["stale"] is False
        catalog.set_points(entries[0].path, {"start": 0.01})
        assert _result_view(station, result)["stale"] is True


@pytest.mark.parametrize("mode, stale", [("off", False), ("smooth", True)])
def test_only_loudness_used_by_selected_audio_invalidates_review(tmp_path, mode, stale):
    """ADR-0014-09: selected cached gain matters only when the review uses it."""
    from pathlib import Path

    from autodj.catalog import LibraryCatalog
    from autodj.processing import PREPARATION_VERSION

    station, identity, entries = station_content(tmp_path)
    station.cfg.playback.loudness_processing = mode
    path = Path(entries[0].path)
    stamp = path.stat()
    measurement = {
        "version": PREPARATION_VERSION,
        "kind": "music",
        "size": stamp.st_size,
        "mtime_ns": stamp.st_mtime_ns,
        "lufs": -30,
        "gain_db": 2,
    }
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(tmp_path)
        for entry in entries:
            catalog.put_entry(entry)
        station.bridge.library.catalog = catalog
        catalog.set_loudness(path, measurement)
        plan = capture_review(station, {"source": "show", "show_id": identity})
        catalog.set_loudness(path, {**measurement, "lufs": -29, "gain_db": 1})
        assert (source_fingerprint(station, plan.request) != plan.fingerprint) is stale


@pytest.mark.parametrize("kind", ["show", "queue"])
@pytest.mark.parametrize("selection", [False, True])
def test_long_programme_is_rejected_before_decode_even_with_a_short_range(
    tmp_path, monkeypatch, kind, selection
):
    """ADR-0014-10: a range cannot bypass full-programme admission."""
    from autodj.player import Player
    from autodj.review_jobs import ReviewError

    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries) if kind == "queue" else None
    long_entry = replace(entries[0], length=61 * 60)
    library = station.bridge.library
    library._by_path[long_entry.path] = long_entry
    library._music = (long_entry, *entries[1:])
    station.shows.update(
        identity, lambda show: show.model_copy(update={"songs": [long_entry.path]})
    )
    if player is not None:
        player._state.queue[:] = [long_entry]

    def refuse_decode(*args, **kwargs):
        pytest.fail("oversized programme reached audio decoding")

    monkeypatch.setattr(Player, "_load_playout_audio", refuse_decode)
    parameters = {"source": kind, "show_id": identity, "start": "2026-10-18T12:00:00Z"}
    if selection:
        parameters.update(selection_start=0, selection_end=1)
    with pytest.raises(ReviewError, match=r"60 minutes.*shorter saved show or queue"):
        capture_review(station, parameters)


def test_fingerprint_only_stats_selected_audio_and_still_detects_edits(tmp_path, monkeypatch):
    from autodj import review_source

    station, identity, entries = station_content(tmp_path)
    signatures = []
    original = review_source._signature
    monkeypatch.setattr(
        review_source, "_signature", lambda path: (signatures.append(str(path)), original(path))[1]
    )
    request = {"source": "show", "show_id": identity}
    before = source_fingerprint(station, request)
    assert entries[2].path not in signatures
    assert entries[0].path in signatures
    with open(entries[0].path, "ab") as output:
        output.write(b"edit")
    assert source_fingerprint(station, request) != before


def test_queue_capture_rejects_media_changed_during_prediction(tmp_path, monkeypatch):
    from autodj import hour_preview

    station, identity, entries = station_content(tmp_path)
    _live(station, identity, entries)
    predict = hour_preview.predict

    def edit_media(*args, **kwargs):
        answer = predict(*args, **kwargs)
        with open(entries[0].path, "ab") as output:
            output.write(b"changed")
        return answer

    monkeypatch.setattr(hour_preview, "predict", edit_media)
    with pytest.raises(ValueError, match="changed"):
        capture_review(station, {"source": "queue", "start": "2026-10-18T12:00:00Z"})


def test_review_with_handoff_liner_is_fresh_until_its_audio_changes(tmp_path):
    from autodj.review_api import _result_view

    station, identity, entries = station_content(tmp_path)
    station.bridge.library._liners = (entries[1],)
    plan = capture_review(station, {"source": "show", "show_id": identity})
    assert any(row.in_handoff and row.entry == entries[1] for row in plan.items)
    metadata = {
        "_source_paths": [entries[0].path, entries[1].path],
        "_request": plan.request,
        "_fingerprint": plan.fingerprint,
    }
    result = {"id": "a" * 32, "created_at": 100, "metadata": metadata}
    assert _result_view(station, result)["stale"] is False
    with open(entries[1].path, "ab") as audio:
        audio.write(b"edited")
    assert _result_view(station, result)["stale"] is True


@pytest.mark.parametrize(
    "parameters, field",
    [
        ({"source": "show", "show_id": "0" * 32}, "show_id"),
        ({"source": "forecast", "start": "invalid"}, "start"),
    ],
)
def test_capture_error_keeps_field_in_worker_status(tmp_path, parameters, field):
    from autodj.review_jobs import ReviewJobs
    from tests.unit.test_review_jobs import wait

    station, _, _ = station_content(tmp_path)
    jobs = ReviewJobs()
    root = tmp_path / "reviews"
    job = jobs.submit(root, "", lambda stage, cancelled: capture_review(station, parameters))
    status = wait(jobs, root, job["id"])
    assert status["state"] == "failed"
    assert status["field"] == field
    assert jobs.shutdown()


@pytest.mark.parametrize("start", ["2026-03-08T02:30", "2026-11-01T01:30"])
def test_ambiguous_or_nonexistent_wall_time_is_rejected(tmp_path, start):
    station, identity, _ = station_content(tmp_path)
    station.timezone = "America/Chicago"
    with pytest.raises(ValueError, match=r"offset|ambiguous|exist"):
        capture_review(station, {"source": "show", "show_id": identity, "start": start})


def test_missing_saved_song_is_reported_without_silently_omitting_it(tmp_path):
    station, identity, entries = station_content(tmp_path)
    from pathlib import Path

    Path(entries[1].path).unlink()
    plan = capture_review(station, {"source": "show", "show_id": identity})
    assert len(plan.items) >= 3
    assert any(item.duration_unknown and "unavailable" in item.label.lower() for item in plan.items)
    assert plan.notes


def _live(station, identity, entries):
    from tests.unit.test_hour_preview import _playing

    player = capture_review(station, {"source": "show", "show_id": identity}).source.player
    player.bus = None
    station.player = player
    player._library = station.bridge.library
    station.bridge.player = player
    _playing(
        SimpleNamespace(player=player),
        entries[0],
        datetime(2026, 10, 18, 12, tzinfo=UTC).timestamp() - 0.03,
    )
    player._state.queue.extend([entries[1], entries[0]])
    return player


def test_queue_capture_keeps_exact_finite_order_without_picking_more(tmp_path):
    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries)
    before = list(player._state.queue)
    plan = capture_review(station, {"source": "queue", "start": "2026-10-18T12:00:00Z"})
    assert [r.entry.path for r in plan.items if r.kind == "song"] == [
        entries[0].path,
        entries[1].path,
        entries[0].path,
    ]
    assert list(player._state.queue) == before
    assert plan.start == min(row.starts for row in plan.items)


def test_forecast_capture_includes_random_continuation_without_live_mutation(tmp_path):
    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries)
    before = (list(player._state.queue), player._random.getstate())
    plan = capture_review(
        station, {"source": "forecast", "minutes": 1, "start": "2026-10-18T12:00:00Z"}
    )
    assert len([r for r in plan.items if r.kind == "song"]) > 3
    assert (list(player._state.queue), player._random.getstate()) == before


def test_capture_cancellation_and_changed_input_are_rejected(tmp_path, monkeypatch):
    from autodj import hour_preview

    station, identity, _ = station_content(tmp_path)
    request = {"source": "show", "show_id": identity}
    with pytest.raises(InterruptedError, match="interrupted"):
        capture_review(station, request, cancelled=lambda: True)
    original = hour_preview.predict

    def change(*args, **kwargs):
        answer = original(*args, **kwargs)
        station.cfg.playback.music_speed_percent = 1
        return answer

    monkeypatch.setattr(hour_preview, "predict", change)
    with pytest.raises(ValueError, match="changed"):
        capture_review(station, request)


def test_finite_queue_of_only_clips_does_not_pick_a_song(tmp_path):
    from autodj.review_source import _finite

    station, identity, entries = station_content(tmp_path)
    source = capture_review(station, {"source": "show", "show_id": identity}).source
    source.library._liners = (entries[1], entries[2])
    rows = _finite(
        source,
        [entries[1], entries[2]],
        datetime(2026, 10, 18, 12, tzinfo=UTC).timestamp(),
        lambda: False,
    )
    assert [row.entry.path for row in rows] == [entries[1].path, entries[2].path]
    assert all(row.kind == "liner" and row.audio is not None for row in rows)


def test_static_fingerprint_allows_live_progress_but_detects_content_changes(tmp_path):
    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries)
    request = {"source": "queue"}
    full = source_fingerprint(station, request)
    static = source_fingerprint(station, request, include_live_state=False)
    player._state.queue.pop(0)
    assert full != source_fingerprint(station, request)
    assert static == source_fingerprint(station, request, include_live_state=False)
    station.cfg.playback.music_speed_percent = 2
    assert static != source_fingerprint(station, request, include_live_state=False)


def test_live_capture_scheduler_uses_owned_decoder_and_cannot_persist_loudness(
    tmp_path, monkeypatch
):
    from autodj.liner_scheduler import LinerScheduler
    from autodj.review_source import _source

    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries)
    station.cfg.playback.loudness_processing = "smooth"
    player._cfg.playback.loudness_processing = "smooth"
    library = station.bridge.library
    library._liners = (entries[1],)
    library.catalog.record_for_path = lambda path: {"id": "clip", "kind": "liner", "loudness": {}}
    writes = []
    library.catalog.set_loudness = lambda *args: writes.append(args)
    live_calls = []
    player.liner_scheduler = LinerScheduler(
        player._cfg.playback,
        station.paths.liners,
        None,
        registered_liners=lambda: [("clip", __import__("pathlib").Path(entries[1].path))],
        path_decoder=lambda path: live_calls.append(path) or np.ones((4410, 2), np.float32),
    )
    monkeypatch.setattr(
        "autodj.processing.load_and_measure_audio",
        lambda *args, **kwargs: (np.ones((4410, 2), np.float32), {"lufs": -20}),
    )
    captured = _source(station, datetime(2026, 10, 18, 12, tzinfo=UTC).timestamp())
    assert captured.player is not player
    assert len(captured.player.liner_scheduler.decode("clip")) == 4410
    assert not writes and not live_calls


def test_queue_keeps_a_trailing_clip_without_an_invented_following_song(tmp_path):
    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries)
    station.bridge.library._liners = (entries[1],)
    player._state.queue[:] = [entries[1]]
    plan = capture_review(station, {"source": "queue", "start": "2026-10-18T12:00:00Z"})
    assert len(plan.items) == 2
    assert plan.items[1].entry.path == entries[1].path
    assert plan.items[1].kind == "liner" and plan.items[1].audio is not None


def test_queue_hard_event_marks_current_cut_and_preserves_known_next_songs(tmp_path):
    from datetime import time

    from tests.unit.test_hour_preview import _event

    station, identity, entries = station_content(tmp_path)
    _live(station, identity, entries)
    entries[0].length = 200
    event = _event("News", time=time(12, 1), timing="hard")
    station.schedule = SimpleNamespace(events={event.id: event}, actions={})
    plan = capture_review(station, {"source": "queue", "start": "2026-10-18T12:00:00Z"})
    songs = [row for row in plan.items if row.kind == "song"]
    assert songs[0].cut_at == datetime(2026, 10, 18, 12, 1, tzinfo=UTC).timestamp()
    assert [row.entry.path for row in songs] == [entries[0].path, entries[1].path, entries[0].path]
    assert any("News" in row.label for row in plan.items)


def test_fingerprint_tracks_effective_selected_dj_metadata_without_cache_bookkeeping(tmp_path):
    from autodj.dj_meta import DjMeta, DjMetaCache

    station, identity, entries = station_content(tmp_path)
    player = _live(station, identity, entries)
    with DjMetaCache(
        station.cfg.index.library_dir / "dj_meta.db", music_dir=station.cfg.library.music_dir
    ) as cache:
        player._dj_cache = cache
        request = {"source": "show", "show_id": identity}
        before = source_fingerprint(station, request)
        cache.set(entries[0].path, DjMeta(intro_end_s=1.25, analysed=True))
        changed = source_fingerprint(station, request)
        assert changed != before
        cache.flush(force=True)
        assert source_fingerprint(station, request) == changed
        cache.set(entries[2].path, DjMeta(intro_end_s=3.0, analysed=True))
        assert source_fingerprint(station, request) == changed


def test_stopped_fingerprint_reads_selected_dj_metadata_from_wal(tmp_path):
    from autodj.dj_meta import DjMeta, DjMetaCache

    station, identity, entries = station_content(tmp_path)
    database = station.cfg.index.library_dir / "dj_meta.db"
    with DjMetaCache(database, music_dir=station.cfg.library.music_dir) as writer:
        writer._conn.execute("PRAGMA journal_mode=WAL")
        writer._conn.execute("PRAGMA wal_autocheckpoint=0")
        writer.flush(force=True)
        writer._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        request = {"source": "show", "show_id": identity}
        before = source_fingerprint(station, request)
        disk_stat = database.stat().st_mtime_ns
        writer.set(entries[0].path, DjMeta(outro_start_s=2.5, analysed=True))
        writer.flush(force=True)
        assert database.stat().st_mtime_ns == disk_stat
        changed = source_fingerprint(station, request)
        assert changed != before
        writer.set(entries[2].path, DjMeta(outro_start_s=1.75, analysed=True))
        writer.flush(force=True)
        assert source_fingerprint(station, request) == changed


def test_capture_freezes_private_dj_paths_without_accepting_them_from_browser(tmp_path):
    from pydantic import ValidationError

    from autodj.review_api import ReviewBody

    station, identity, entries = station_content(tmp_path)
    request = {"source": "show", "show_id": identity}
    plan = capture_review(station, request)
    assert "_review_dj_paths" not in request
    assert plan.request["_review_dj_paths"] == sorted({entries[0].path, entries[1].path})
    assert plan.fingerprint == source_fingerprint(station, plan.request)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReviewBody.model_validate({**request, "_review_dj_paths": [entries[2].path]})


def test_fingerprinting_does_not_fill_live_metadata_cache(tmp_path):
    from autodj.dj_meta import DjMeta, DjMetaCache

    station, identity, entries = station_content(tmp_path)
    with DjMetaCache(station.cfg.index.library_dir / "dj_meta.db") as cache:
        for entry in entries:
            cache.set(entry.path, DjMeta(intro_end_s=1.0, analysed=True))
        cache.flush(force=True)
        cache._mem_cache.clear()
        station.player = SimpleNamespace(_dj_cache=cache)
        source_fingerprint(station, {"source": "show", "show_id": identity})
        assert cache._mem_cache == {}
        assert cache._misses == {}


def test_metadata_snapshot_preserves_a_live_cached_miss_until_it_expires(tmp_path):
    from autodj.dj_meta import DjMeta, DjMetaCache
    from autodj.review_render import _Metadata

    station, _, entries = station_content(tmp_path)
    clock = [100.0]
    database = station.cfg.index.library_dir / "dj_meta.db"
    with DjMetaCache(database, clock=lambda: clock[0]) as cache:
        assert cache.get(entries[0].path).analysed is False
        with DjMetaCache(database) as writer:
            writer.set(entries[0].path, DjMeta(intro_end_s=1.25, analysed=True))
        assert _Metadata(cache, entries[:1], station.cfg).get(entries[0].path).analysed is False
        clock[0] += cache.MISS_TTL_S + 1
        assert _Metadata(cache, entries[:1], station.cfg).get(entries[0].path).intro_end_s == 1.25
        assert cache._mem_cache == {}


def test_result_fingerprints_share_only_selected_catalog_reads(tmp_path, monkeypatch):
    station, identity, entries = station_content(tmp_path)
    catalog = station.bridge.library.catalog
    original = catalog.record_for_path
    reads = []
    monkeypatch.setattr(
        catalog, "record_for_path", lambda path: (reads.append(path), original(path))[1]
    )
    request = {"source": "show", "show_id": identity}
    shared = {}
    first = source_fingerprint(station, request, shared=shared)
    source_fingerprint(station, {**request, "_review_dj_paths": [entries[0].path]}, shared=shared)
    assert sorted(reads) == sorted({entries[0].path, entries[1].path})
    station.cfg.playback.music_speed_percent = 2
    assert source_fingerprint(station, request, shared={}) != first


def test_result_fingerprints_share_legacy_windows_metadata_lookup(tmp_path, monkeypatch):
    import os
    import sqlite3

    from autodj import dj_meta

    station, identity, entries = station_content(tmp_path)
    with dj_meta.DjMetaCache(station.cfg.index.library_dir / "dj_meta.db") as cache:
        for entry in entries:
            cache.set(entry.path, dj_meta.DjMeta(intro_end_s=1.0, analysed=True))
        cache.flush(force=True)
        cache._conn.execute("UPDATE dj_meta SET path=upper(path)")
    monkeypatch.setattr(dj_meta, "os", SimpleNamespace(name="nt", path=os.path, fspath=os.fspath))
    queries = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    shared = {}
    request = {"source": "show", "show_id": identity}
    source_fingerprint(station, request, shared=shared)
    source_fingerprint(station, {**request, "_review_dj_paths": [entries[0].path]}, shared=shared)
    assert queries.count("SELECT path FROM dj_meta") == 1


def test_capture_interruption_is_not_a_public_cancelled_error(tmp_path):
    station, identity, _ = station_content(tmp_path)
    with pytest.raises(InterruptedError):
        capture_review(station, {"source": "show", "show_id": identity}, cancelled=lambda: True)


@pytest.mark.parametrize("reason", ["cancelled", "timed out", "storage limit"])
def test_worker_reports_capture_interruption_reason_and_cleans_up(tmp_path, reason):
    import threading
    import time

    from autodj.review_jobs import ReviewJobs
    from tests.unit.test_review_jobs import wait

    station, identity, _ = station_content(tmp_path)
    entered, proceed = threading.Event(), threading.Event()
    jobs = ReviewJobs(
        max_seconds=0.02 if reason == "timed out" else 5,
        max_scratch_bytes=1 if reason == "storage limit" else 1024,
    )

    def render(stage, cancelled):
        if reason == "storage limit":
            (stage / "capture").write_bytes(b"over budget")
        entered.set()
        assert proceed.wait(1)
        return capture_review(station, {"source": "show", "show_id": identity}, cancelled=cancelled)

    root = tmp_path / "reviews"
    job = jobs.submit(root, "inputs", render)
    assert entered.wait(1)
    if reason == "cancelled":
        jobs.cancel(root, job["id"])
    elif reason == "timed out":
        time.sleep(0.03)
    proceed.set()
    status = wait(jobs, root, job["id"])
    assert reason in status["error"].lower()
    assert status["state"] == ("cancelled" if reason == "cancelled" else "failed")
    assert not list(root.glob(".staging-*"))
    assert jobs.shutdown()


@pytest.mark.parametrize("reason", ["cancelled", "timed out", "storage limit"])
def test_queue_prediction_interruption_keeps_worker_reason(tmp_path, monkeypatch, reason):
    from autodj import hour_preview
    from autodj.review_jobs import ReviewJobs
    from tests.unit.test_review_jobs import wait

    station, identity, entries = station_content(tmp_path)
    _live(station, identity, entries)
    jobs = ReviewJobs()

    def predict(*args, **kwargs):
        run = jobs._active
        assert run is not None
        if reason == "cancelled":
            run.cancel.set()
        elif reason == "timed out":
            run.deadline = 0
        else:
            run.storage_exceeded = True
        assert kwargs["cancelled"]()
        return (), 0, "review cancelled"

    monkeypatch.setattr(hour_preview, "predict", predict)
    root = tmp_path / "reviews"
    job = jobs.submit(
        root,
        "inputs",
        lambda stage, cancelled: capture_review(
            station, {"source": "queue", "start": "2026-10-18T12:00:00Z"}, cancelled=cancelled
        ),
    )
    status = wait(jobs, root, job["id"])
    assert reason in status["error"].lower()
    assert status["state"] == ("cancelled" if reason == "cancelled" else "failed")
    assert not list(root.glob(".staging-*"))
    assert jobs.shutdown()
