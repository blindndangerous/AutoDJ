"""Raw date and text filtering for the unified station History view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from autodj.catalog import LibraryCatalog
from autodj.playlog import plays_between
from autodj.station_store import StationStore


def _row(event_id: str, minute: int, **fields) -> dict:
    return {
        "event_id": event_id,
        "path": f"/music/{event_id}.flac",
        "title": fields.pop("title", f"Track {event_id}"),
        "artist": fields.pop("artist", "The Example"),
        "album": fields.pop("album", "Album"),
        "duration": 60,
        "played_at": (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=minute)).isoformat(),
        **fields,
    }


def test_filtered_history_reads_full_station_range_and_applies_and_search(tmp_path: Path) -> None:
    """ADR-0006-10. Filtered History scans full station history and AND-matches text."""
    store = StationStore(tmp_path / "default" / "station.db", "default")
    rows = [_row(f"song-{index}", index) for index in range(501)]
    rows[4]["title"] = "Blue Harbor"
    rows[4]["artist"] = "The Example"
    store.write_history(rows, [])
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)

    selected = store.filtered_history(start, end, nonmusic=False, query="BLUE example")

    assert [row["event_id"] for row in selected] == ["song-4"]
    assert len(store.filtered_history(start, end, nonmusic=False)) == 501
    store.close()


def test_filtered_history_songs_only_excludes_live_and_liner_tracks(tmp_path: Path) -> None:
    """ADR-0006-10. Music-only filters include songs and exclude live/liner rows."""
    store = StationStore(tmp_path / "default" / "station.db", "default")
    rows = [
        _row("song", 0),
        _row("liner-track", 1, track_kind="liner"),
        _row("live", 2, kind="live"),
        _row("insert", 3, kind="liner"),
    ]
    store.write_history(rows, [])
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)

    songs = store.filtered_history(start, end, nonmusic=False)
    all_rows = store.filtered_history(start, end, nonmusic=True)

    assert [row["event_id"] for row in songs] == ["song"]
    assert {row["event_id"] for row in all_rows} == {"song", "liner-track", "live", "insert"}
    store.close()


def test_filtered_history_is_station_local(tmp_path: Path) -> None:
    """ADR-0006-10. A filtered history query reads only its station database."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    default = StationStore(tmp_path / "default" / "station.db", "default")
    nightowl = StationStore(tmp_path / "nightowl" / "station.db", "nightowl")
    default.write_history([_row("default-event", 0)], [])
    nightowl.write_history([_row("nightowl-event", 0)], [])

    assert [row["event_id"] for row in default.filtered_history(start, end, nonmusic=False)] == [
        "default-event"
    ]
    assert [row["event_id"] for row in nightowl.filtered_history(start, end, nonmusic=False)] == [
        "nightowl-event"
    ]
    default.close()
    nightowl.close()


def test_pending_play_duration_uses_next_durable_boundary_outside_range(tmp_path: Path) -> None:
    """ADR-0006-10. Pending export duration uses the next durable boundary."""
    store = StationStore(tmp_path / "default" / "station.db", "default")
    pending = _row("pending", 1439)
    pending["duration"] = 600
    next_play = _row("next-day", 1441)
    store.write_history([next_play], [])
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)

    with LibraryCatalog(tmp_path / "library.db") as catalog:
        plays = plays_between(
            catalog,
            start,
            end,
            station_store=store,
            pending_rows=[pending],
            read_tags=lambda _path: ("", ""),
        )

    assert len(plays) == 1
    assert plays[0].title == "Track pending"
    assert plays[0].duration == 120
    store.close()


def test_unflushed_interruption_does_not_cut_previous_exported_play(tmp_path: Path) -> None:
    """ADR-0006-10. An unflushed interruption is not a report boundary."""
    store = StationStore(tmp_path / "default" / "station.db", "default")
    path = "/music/shared.flac"
    store.write_history(
        [
            _row("previous", 720, path=path, duration=600),
            _row("interrupted", 722, path=path, duration=1),
            _row("following", 725, path=path, duration=600),
        ],
        [],
    )
    start = datetime(2026, 9, 1, 11, 50, tzinfo=UTC)
    end = datetime(2026, 9, 1, 12, 3, tzinfo=UTC)

    with LibraryCatalog(tmp_path / "library.db") as catalog:
        plays = plays_between(
            catalog,
            start,
            end,
            station_store=store,
            interrupted_ids={"interrupted"},
            read_tags=lambda _path: ("", ""),
        )

    assert [play.title for play in plays] == ["Track previous"]
    assert plays[0].duration == 300
    store.close()
