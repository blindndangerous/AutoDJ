"""Independent catalogs and station history across library changes."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from autodj._bridge import LibraryServices
from autodj.clockwheel_store import ClockwheelStore
from autodj.config import station_config
from autodj.library import LibraryManager
from autodj.library_runtime import Libraries
from autodj.library_switch import library_warnings
from autodj.stations import StationPaths
from tests.unit.test_library import Similarity, make_config, make_entry


@pytest.fixture
def libraries(tmp_path):
    cfg = make_config(tmp_path)
    sim = Similarity()
    manager = LibraryManager(cfg, sim)
    pool = Libraries(LibraryServices(cfg, sim, manager))
    pool.registry.create("other", "Other")
    pool.get("other")
    yield pool
    for services in pool.all():
        services.library.close()


def test_shared_physical_folder_has_independent_catalogs(libraries):
    """ADR-0023-02: preserve the named-library ownership boundary."""
    main = libraries.get("main").library
    other = libraries.get("other").library
    folder = main.cfg.library.music_dir
    song = folder / "song.flac"
    song.write_bytes(b"audio")
    for manager, title in ((main, "Main title"), (other, "Other title")):
        manager.catalog.add_source(folder)
        manager.catalog.put_entry(make_entry(song, title=title))
        manager.refresh()
    main.catalog.set_points(str(song), {"intro": 7.0})
    assert main.entry_for_path(song).title == "Main title"
    assert other.entry_for_path(song).title == "Other title"
    assert main.catalog.points(str(song)) != other.catalog.points(str(song))
    assert main.catalog.path != other.catalog.path


def test_offline_simulation_uses_the_saved_library_and_rejects_a_missing_one(libraries):
    from autodj.rotation_sim import SimulationError, open_station

    song = libraries.root.library.music_dir / "song.flac"
    song.write_bytes(b"audio")
    for library_id, title in (("main", "Main title"), ("other", "Other title")):
        manager = libraries.get(library_id).library
        manager.catalog.add_source(song.parent)
        manager.catalog.put_entry(make_entry(song, title=title))
    libraries.root.station.library_id = "other"
    with open_station(libraries.root) as source:
        assert source.library.entries_snapshot()[0].title == "Other title"
        assert source.player._cfg.index.library_id == "other"
    assert libraries.root.index.library_id == "main"
    libraries.root.station.library_id = "missing"
    with (
        pytest.raises(SimulationError, match="Cannot open library 'missing'"),
        open_station(libraries.root),
    ):
        pass


def test_library_preflight_reports_missing_references_without_changing_programming(libraries):
    paths = StationPaths.for_station(libraries.root, "default")
    store = ClockwheelStore(paths.clockwheels)
    store.save(
        {
            "version": 1,
            "name": "Morning",
            "steps": [
                {"type": "settings", "preset": "Normal"},
                {"type": "ad_break"},
                {"type": "category", "category": "missing"},
                {"type": "music", "source": "missing-source"},
            ],
            "resync_minutes": [],
        }
    )
    saved = store.load("Morning")
    queued = [SimpleNamespace(path="missing.flac")]
    station = SimpleNamespace(paths=paths, bridge=SimpleNamespace(_queue_snapshot=lambda: queued))
    warnings = library_warnings(station, libraries.get("other").library)
    assert warnings[0] == "1 queued track is absent from this library."
    assert any("step 3: category is unavailable" in warning for warning in warnings)
    assert any("step 4:" in warning and "not registered" in warning for warning in warnings)
    assert len(warnings) == 3
    assert store.load("Morning") == saved
    assert queued[0].path == "missing.flac"


def test_preflight_checks_saved_shows_and_requests_without_repairing_files(libraries):
    from autodj.listener_requests import PENDING, STATE_VERSION
    from autodj.shows import ShowSpec, ShowStore, with_songs

    paths = StationPaths.for_station(libraries.root, "default")
    shows = ShowStore(paths.shows)
    show_id, _ = shows.add(ShowSpec(name="Evening", source="category", category="missing"))
    shows.update(show_id, lambda show: with_songs(show, ["missing.flac"]))
    paths.listener_requests.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "requests": [
                    {
                        "id": "request",
                        "path": "missing.flac",
                        "title": "Missing",
                        "artist": "Artist",
                        "album": "",
                        "name": "",
                        "status": PENDING,
                        "requested_at": 1,
                    }
                ],
            }
        )
    )
    originals = {path: path.read_bytes() for path in (paths.shows, paths.listener_requests)}
    station = SimpleNamespace(paths=paths, bridge=SimpleNamespace())
    warnings = library_warnings(station, libraries.get("other").library)
    assert any("Evening: category is unavailable" in warning for warning in warnings)
    assert any("Evening: 1 saved track" in warning for warning in warnings)
    assert any("1 pending request" in warning for warning in warnings)
    assert all(path.read_bytes() == contents for path, contents in originals.items())
    paths.shows.write_text("{bad")
    before = set(paths.state_dir.rglob("*"))
    warnings = library_warnings(station, libraries.get("other").library)
    assert any("Could not check saved shows" in warning for warning in warnings)
    assert paths.shows.read_text() == "{bad"
    assert set(paths.state_dir.rglob("*")) == before


def test_library_change_transfers_buffered_history_and_station_settings(libraries):
    """ADR-0023-03: preserve the named-library ownership boundary."""
    cfg = station_config(libraries.root, libraries.root)
    main = libraries.bind("default", cfg).library
    ledger = main.station_ledger("default")
    ledger.remember_play("song.flac", 1234.0)
    ledger.select_clock("Morning", cursor=2)
    saved = replace(cfg, station=replace(cfg.station, library_id="other"))
    # A saved choice does not mutate the running configuration or binding.
    assert cfg.index.library_id == "main"
    other = libraries.bind("default", saved).library
    assert other.station_ledger("default") is ledger
    assert ledger.last_played()
    assert ledger.store.get_setting("clock")["name"] == "Morning"
    assert ledger not in main._ledgers
    assert saved.index.library_id == "other"
    assert libraries.root.index.library_id == "main"
    libraries.bind("default", cfg)
    assert main.station_ledger("default") is ledger
    assert ledger not in other._ledgers


def test_secondary_library_starts_without_station_sources_or_ledgers(libraries):
    other = libraries.get("other").library
    assert other.catalog.sources() == []
    assert other._ledgers == []
    assert other.cfg.library.music_dir is None


def test_unknown_library_never_falls_back(libraries):
    """ADR-0023-04: preserve the named-library ownership boundary."""
    with pytest.raises(KeyError):
        libraries.get("missing")
    cfg = replace(libraries.root, station=replace(libraries.root.station, library_id="missing"))
    with pytest.raises(KeyError):
        libraries.bind("default", cfg)
    assert libraries.get("main").library._ledgers


def test_failed_ledger_flush_keeps_previous_owner(libraries, monkeypatch):
    cfg = station_config(libraries.root, libraries.root)
    main = libraries.bind("default", cfg).library
    ledger = main.station_ledger("default")
    saved = replace(cfg, station=replace(cfg.station, library_id="other"))
    with monkeypatch.context() as patch:

        def failed_flush():
            raise OSError("disk unavailable")

        patch.setattr(ledger, "flush", failed_flush)
        with pytest.raises(OSError, match="disk unavailable"):
            libraries.bind("default", saved)
    assert main.attached_ledger("default") is ledger
    assert libraries.get("other").library.attached_ledger("default") is None
    assert saved.index.library_id == "main"


def test_preflight_does_not_move_invalid_categories(libraries):
    from autodj.categories import FILE_NAME

    paths = StationPaths.for_station(libraries.root, "default")
    path = paths.state_dir / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad", encoding="utf-8")
    before = set(paths.state_dir.rglob("*"))
    station = SimpleNamespace(paths=paths, bridge=SimpleNamespace())
    warnings = library_warnings(station, libraries.get("other").library)
    assert any("Could not check categories" in warning for warning in warnings)
    assert path.read_text(encoding="utf-8") == "{bad"
    assert set(paths.state_dir.rglob("*")) == before
