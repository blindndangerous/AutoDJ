"""Source scanning and playable-library orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import numpy as np

from autodj.analysis_queue import SERVER_HOLDER, SERVER_LEASE_SECONDS, AnalysisQueue
from autodj.beets import Track
from autodj.catalog import (
    Coverage,
    LibraryCatalog,
    _covered_by,
    _path,
    _prefix,
    _under,
)
from autodj.categories import CategoryIndex
from autodj.clock_condition_data import BeetsConditions
from autodj.indexer import (
    FEATURE_DIM,
    LIBRARY_DECODE_LOG,
    IndexEntry,
    _detect_moves,
    _embed_new_tracks,
    _finish_decode_report,
    file_fingerprint,
)
from autodj.processing import (
    TrackWindows,
    analyze_basic,
    analyze_loudness,
    load_for_analysis,
    loudness_matches,
)
from autodj.station_ledger import StationLedger
from autodj.station_store import (
    StationStore,
    discover_station_folders,
    discover_stations,
    migrate_legacy,
)
from autodj.stations import DEFAULT_SLUG, StationPaths
from autodj.stereo import SAMPLE_RATE, DecodeReport

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.model import MuqWrapper


def require_current_library(directory: Path, station_web_states: Iterable[Path] = ()) -> None:
    """Refuse legacy application data before creating or changing database files."""
    from autodj.index_manifest import IndexConsistencyError, has_legacy_index
    from autodj.index_migration import migration_required

    if not (directory / "library.db").is_file() and has_legacy_index(directory):
        reason = "an older published index is present"
    else:
        needed, reason = migration_required(directory, station_web_states)
        if not needed:
            return
    raise IndexConsistencyError(
        f"The library in {directory} needs migration ({reason}). Stop AutoDJ and run "
        "`autodj migrate-index` to preview, then `autodj migrate-index --apply`. "
        "Your original files will be kept."
    )


class _ImportState(TypedDict):
    running: bool
    phase: str
    done: int
    total: int
    current: str


_Indexed = tuple[IndexEntry, str]


def _browse_order(entries: tuple[IndexEntry, ...]) -> list[_Indexed]:
    """Entries sorted for browsing, each with its casefolded searchable text."""
    indexed = [
        (
            entry,
            " ".join(
                (entry.artist, entry.title, entry.album, os.path.basename(entry.path))
            ).casefold(),
        )
        for entry in entries
    ]
    indexed.sort(
        key=lambda item: (
            item[0].artist.casefold(),
            item[0].title.casefold(),
            item[0].path.casefold(),
        )
    )
    return indexed


class LibraryManager:
    """Keep the playable library and its similarity view current."""

    def __init__(
        self, cfg: AutoDJConfig, sim: Any, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        from autodj.config import station_config

        self.cfg = cfg
        self.sim = sim
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._cleanup_active = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_sources: set[str] | None = set()
        self._pending_override_tags = False
        # Cues read from DJ software, once per scan that analyses a song.
        self._dj_library_cues: dict[str, list[Any]] | None = None
        self._closed = False
        self._scan_generation = 0
        self._analysis_started_generation: int | None = None
        self._analysis_idle: tuple[int, int, int, int] | None = None
        self._state: _ImportState = {
            "running": False,
            "phase": "idle",
            "done": 0,
            "total": 0,
            "current": "",
        }
        self._import_errors: list[dict[str, str]] = []
        # The catalog change counters the loaded snapshot was read at.
        self._versions: dict[str, int] | None = None
        # Paths a scan found gone from a present source folder; left out of
        # the playable library without a file-system call per track.
        self._missing: frozenset[str] = frozenset()
        self._loaded_missing: frozenset[str] = frozenset()
        # Called on the scan thread when a scan has finished and the library
        # is refreshed, such as to forget the play dates of liners now gone.
        self.on_scan_done: Callable[[], None] | None = None
        # Whether a folder walk of the scan in progress hit an error (a
        # subfolder it could not read): its tracks are then not marked gone,
        # and the after-scan tidy-up does not run.
        self._walk_failed = False
        # Files a scan listed but could not resolve; never marked gone.
        self._seen_unresolved: set[str] = set()
        # Whether the scan in progress worked out which tracks are gone; the
        # after-scan tidy-up runs only then.
        self._reconciled = False
        # The music paths, in FAISS row order, of the vectors handed to sim.
        self._vector_paths: tuple[str, ...] | None = None
        self._entries: tuple[IndexEntry, ...] = ()
        self._liner_entries: tuple[IndexEntry, ...] = ()
        self._by_path: dict[str, IndexEntry] = {}
        # Normalized path -> (kind, position in that kind's entries tuple).
        self._positions: dict[str, tuple[str, int]] = {}
        # (name, kind) -> (the entries tuple it was built from, text index).
        self._text_indexes: dict[
            tuple[str, str], tuple[tuple[IndexEntry, ...], list[_Indexed]]
        ] = {}
        self._tracks_ready = 0
        self._tracks_indexed = 0
        # Rotation categories for clockwheel steps, worked out from these entries.
        # Each station's rotation categories, by its folder (see categories_for).
        self._category_indexes: dict[Path, CategoryIndex] = {}
        self._category_lock = threading.Lock()
        self.clock_beets = BeetsConditions(cfg, self.entries_snapshot)
        catalog_path = cfg.index.library_dir / "library.db"
        require_current_library(
            cfg.index.library_dir,
            [
                StationPaths.default(cfg).web_state,
                *(
                    StationPaths.for_station(station_config(spec.cfg, cfg), spec.slug).web_state
                    for spec in cfg.stations
                ),
            ],
        )
        existing_catalog = catalog_path.is_file()
        self.catalog = LibraryCatalog(catalog_path)
        default_store = self._station_store(DEFAULT_SLUG)
        stores = [default_store]
        try:
            # The global marker avoids consulting legacy station tables after one complete import.
            if default_store.migration_complete("catalog-stations-v1"):
                station_ids = discover_station_folders(cfg.index.stations_dir) | {DEFAULT_SLUG}
            else:
                station_ids = discover_stations(self.catalog) | {DEFAULT_SLUG}
            station_ids.update(spec.slug for spec in cfg.stations)
            self._station_ids = station_ids
            for station_id in sorted(station_ids):
                store = (
                    default_store if station_id == DEFAULT_SLUG else self._station_store(station_id)
                )
                if store is not default_store:
                    stores.append(store)
                migrate_legacy(self.catalog, store)
            default_store.mark_migration("catalog-stations-v1")
        except BaseException:
            for store in stores:
                store.close()
            self.catalog.close()
            raise
        for store in stores:
            store.close()
        self._clock = clock
        self._auto_rescan_minutes = self.catalog.get_setting("auto_rescan_minutes", 0)
        self._last_scan_end = self._clock()
        # What each station played, and its settings; the default station's
        # first.  tick() writes them all.
        self.ledger = StationLedger(
            self._station_store(DEFAULT_SLUG), self.entry_for_path, self.kind_for_path
        )
        self._ledgers: list[StationLedger] = [self.ledger]
        # Set while a remote analysis worker is connected, so the CPU job,
        # which waited for it, runs again once it has gone.
        self._yielded_to_worker = False
        # The configured folder is kept as written: it names a catalog source.
        liner_root = str(StationPaths.default(cfg).liners)
        try:
            self.analysis_queue = AnalysisQueue(catalog_path)
        except BaseException:
            self.ledger.store.close()
            self.catalog.close()
            raise
        try:
            if not existing_catalog:
                self.catalog.seed_sources(None, liner_root)
            self.refresh()
        except BaseException:
            self.analysis_queue.close()
            self.ledger.store.close()
            self.catalog.close()
            raise

    def categories_for(self, paths: Any = None) -> CategoryIndex:
        """One station's rotation categories; the default station's without *paths*."""
        from autodj.categories import FILE_NAME
        from autodj.stations import StationPaths

        state = Path((paths or StationPaths.default(self.cfg)).state_dir)
        with self._category_lock:
            found = self._category_indexes.get(state)
            if found is None:
                found = CategoryIndex(
                    self.cfg,
                    self.entries_snapshot,
                    lambda: self.catalog.sources(),
                    path=state / FILE_NAME,
                )
                self._category_indexes[state] = found
        return found

    @property
    def categories(self) -> CategoryIndex:
        """The default station's rotation categories."""
        return self.categories_for()

    def _changed(self) -> bool:
        """Whether records, vectors or sources changed since the last load."""
        with self._lock:
            seen = self._versions
            if self._missing != self._loaded_missing:
                return True
        if seen is None:
            return True
        versions = self.catalog.change_versions()
        return any(versions[kind] != seen[kind] for kind in ("entries", "vectors", "sources"))

    def refresh(self, *, force_vectors: bool = False) -> tuple[IndexEntry, ...]:
        """Load what changed in the catalog since the last refresh.

        Records are read again only when records or sources changed (or a
        scan found files gone), and vectors only when vectors changed, the
        set of playable tracks with a vector changed, or *force_vectors*.
        Plays, the play log, settings and loudness measurements change
        neither, so they never cause a reload.
        """
        with self._refresh_lock:
            with self._lock:
                seen = self._versions
                missing = self._missing
            with self.catalog.reader() as reader:
                versions = reader.versions()
                reload_entries = (
                    seen is None
                    or versions["entries"] != seen["entries"]
                    or versions["sources"] != seen["sources"]
                    or missing != self._loaded_missing
                )
                played: dict[StationLedger, dict[str, float]] = {}
                if reload_entries:
                    coverage = reader.coverage()
                    entries = reader.entries("music", coverage, missing)
                    liner_entries = reader.entries("liner", coverage, missing)
                    with self._lock:
                        ledgers = list(self._ledgers)
                    played = {}
                    for ledger in ledgers:
                        times = ledger.store.last_played()
                        if ledger.station == DEFAULT_SLUG:
                            for entry in (*entries, *liner_entries):
                                times.setdefault(_path(entry.path), 0.0)
                        played[ledger] = times
                else:
                    with self._lock:
                        entries, liner_entries = self._entries, self._liner_entries
                reload_vectors = (
                    force_vectors or seen is None or versions["vectors"] != seen["vectors"]
                )
                vector_paths = self._vector_paths
                if reload_entries or reload_vectors:
                    by_path = {entry.path: entry for entry in entries}
                    vector_paths = tuple(path for path in reader.vector_paths() if path in by_path)
                    vector_entries = [by_path[path] for path in vector_paths]
                    if reload_vectors or vector_paths != self._vector_paths:
                        self._replace_vectors(vector_entries, reader.vectors(set(vector_paths)))
                    else:
                        self._replace_vector_entries(vector_entries)
            with self._lock:
                if reload_entries:
                    self._entries = entries
                    self._liner_entries = liner_entries
                    self._index_entries()
                self._tracks_ready = len(self._entries)
                self._tracks_indexed = len(vector_paths or ())
                self._vector_paths = vector_paths
                self._versions = versions
                self._loaded_missing = missing
                loaded = self._entries
            for ledger, times in played.items():
                ledger.loaded(times)
            return loaded

    def _index_entries(self) -> None:
        """Rebuild the path lookups for the loaded entries; caller holds ``_lock``."""
        self._by_path = {}
        self._positions = {}
        for kind, items in (("music", self._entries), ("liner", self._liner_entries)):
            for position, entry in enumerate(items):
                key = _path(entry.path)
                self._by_path[key] = entry
                self._positions[key] = (kind, position)

    def _replace_vectors(self, entries: list[IndexEntry], chunks: Iterable[np.ndarray]) -> None:
        """Build an exact inner-product index from *chunks* and hand it to ``sim``."""
        import faiss

        index = faiss.IndexFlatIP(FEATURE_DIM)
        for chunk in chunks:
            index.add(chunk)
        replace_index = getattr(self.sim, "replace_catalog_faiss", None)
        if replace_index is not None:
            replace_index(entries, index)
            return
        replace = getattr(self.sim, "replace_catalog_index", None)
        if replace is not None:
            vectors = (
                index.reconstruct_n(0, index.ntotal)
                if index.ntotal
                else np.empty((0, FEATURE_DIM), dtype=np.float32)
            )
            replace(entries, vectors)

    def _replace_vector_entries(self, entries: list[IndexEntry]) -> None:
        """Give ``sim`` re-read metadata for the same vectors, without rebuilding FAISS."""
        replace = getattr(self.sim, "replace_catalog_entries", None)
        if replace is not None and replace(entries):
            return
        with self.catalog.reader() as reader:
            self._replace_vectors(entries, reader.vectors({entry.path for entry in entries}))

    def entries_snapshot(self, kind: str = "music") -> tuple[IndexEntry, ...]:
        if kind == "music":
            with self._lock:
                return self._entries
        if kind == "liner":
            with self._lock:
                return self._liner_entries
        return ()

    def entry_for_path(self, path: str | Path) -> IndexEntry | None:
        with self._lock:
            return self._by_path.get(_path(path))

    def kind_for_path(self, path: str | Path) -> str | None:
        with self._lock:
            position = self._positions.get(_path(path))
            return position[0] if position is not None else None

    def _text_index(
        self, name: str, kind: str, build: Callable[[tuple[IndexEntry, ...]], list[_Indexed]]
    ) -> list[_Indexed]:
        """*build* over one kind's entries, cached until those entries change."""
        with self._lock:
            entries = self._liner_entries if kind == "liner" else self._entries
            cached = self._text_indexes.get((name, kind))
            if cached is not None and cached[0] is entries:
                return cached[1]
        built = build(entries)
        with self._lock:
            self._text_indexes[(name, kind)] = (entries, built)
        return built

    def search_index(self) -> list[_Indexed]:
        """Music entries in path order, each with its lowercased title, artist and album."""
        return self._text_index(
            "search",
            "music",
            lambda entries: [
                (entry, f"{entry.title} — {entry.artist} — {entry.album}".lower())
                for entry in entries
            ],
        )

    def browse_tracks(
        self,
        kind: str = "music",
        source_id: str | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List playable tracks, optionally of one registered source, from memory."""
        if kind not in {"music", "liner"}:
            raise ValueError("kind must be 'music' or 'liner'")
        roots = None
        if source_id is not None:
            source = next((row for row in self.catalog.sources() if row["id"] == source_id), None)
            if source is None:
                raise KeyError(source_id)
            if source["kind"] != kind:
                raise ValueError("source kind does not match requested kind")
            roots = [(_prefix(source["path"]), bool(source["recursive"]))]
        tokens = q.casefold().split()
        matches = [
            entry
            for entry, text in self._text_index("browse", kind, _browse_order)
            if (roots is None or _covered_by(entry.path, roots))
            and all(token in text for token in tokens)
        ]
        return {
            "results": [
                {
                    "path": entry.path,
                    "title": entry.title,
                    "artist": entry.artist,
                    "album": entry.album,
                    "bpm": entry.bpm,
                    "length": entry.length,
                    "display_name": entry.display_name,
                    "kind": kind,
                }
                for entry in matches[offset : offset + limit]
            ],
            "total": len(matches),
            "offset": offset,
            "limit": limit,
        }

    def record_for_id(self, record_id: str) -> dict | None:
        record = self.catalog.record_for_id(record_id)
        if record is None:
            return None
        with self._lock:
            return record if _path(record["path"]) in self._by_path else None

    def is_gone(self, path: str | Path) -> bool:
        """Whether the latest scan found *path* gone from a folder that is there.

        A file on a share that is not mounted is not gone: the scan marks a
        file gone only when its folder is present and the file is not.
        """
        with self._lock:
            return _path(path) in self._missing

    def add_ledger(self, station: str) -> StationLedger:
        """Keep another station's plays; :meth:`tick` writes them with the rest."""
        store = self._station_store(station)
        ledger = StationLedger(store, self.entry_for_path, self.kind_for_path)
        ledger.loaded(ledger.store.last_played())
        with self._lock:
            self._station_ids.add(station)
            self._ledgers.append(ledger)
        return ledger

    def _station_store(self, station: str) -> StationStore:
        from autodj.stations import StationPaths

        paths = (
            StationPaths.default(self.cfg)
            if station == DEFAULT_SLUG
            else StationPaths.for_station(self.cfg, station)
        )
        return StationStore(paths.state_dir / "station.db", station)

    def station_ledger(self, station: str) -> StationLedger:
        """*station*'s ledger, added the first time it is asked for.

        A station that is stopped and started again keeps the same ledger,
        so plays it buffered before the stop are still written once.
        """
        with self._lock:
            for ledger in self._ledgers:
                if ledger.station == station:
                    return ledger
        return self.add_ledger(station)

    def seed_station_liners(self, station: str, root: Path | str) -> None:
        """Make *station*'s liner folder a liner source the first time the station runs.

        Like the default station's folder, it is added once: a folder the
        user later removes from Library folders stays removed.
        """
        source = self.catalog.seed_station_liner_source(station, root)
        if source is not None:
            self.start_scan({source})

    def _flush_ledgers(self) -> None:
        with self._lock:
            ledgers = list(self._ledgers)
        for ledger in ledgers:
            ledger.flush()

    def auto_rescan_minutes(self) -> int:
        return self._auto_rescan_minutes

    def set_auto_rescan_minutes(self, minutes: int) -> None:
        self.catalog.set_setting("auto_rescan_minutes", minutes)
        self._auto_rescan_minutes = minutes

    def dj_analysis_enabled(self) -> bool:
        """Whether scans find each song's beats and song structure (on by default)."""
        return self.catalog.get_setting("dj_analysis", True) is not False

    def set_dj_analysis_enabled(self, enabled: bool) -> None:
        self.catalog.set_setting("dj_analysis", enabled)

    def advanced_max_minutes(self) -> int:
        """Longest song the advanced analysis takes, in minutes; ``0`` takes every length."""
        return self.catalog.advanced_max_minutes()

    def set_advanced_max_minutes(self, minutes: int) -> None:
        """Save the advanced analysis length cutoff and look for work again.

        Songs it now admits are analysed without waiting for a scan; songs it
        now leaves out are simply not started.
        """
        from autodj.catalog import ADVANCED_MAX_MINUTES_KEY

        self.catalog.set_setting(ADVANCED_MAX_MINUTES_KEY, minutes)
        with self._lock:
            self._analysis_started_generation = None
            self._analysis_idle = None

    def tick(self) -> None:
        self._flush_ledgers()
        with self._lock:
            # A scan publishes its own tracks and reloads when it has read
            # them; while it measures loudness, analysis may add vectors.
            scanning = self._scan_lock.locked() and self._state["phase"] != "measuring loudness"
        if not scanning and self._changed():
            self.refresh()
        self.schedule_analysis()
        if (
            self._auto_rescan_minutes > 0
            and not self._scan_lock.locked()
            and self._clock() - self._last_scan_end >= self._auto_rescan_minutes * 60
            and self.start_scan(override_tags=False)
        ):
            logger.info("Automatic library rescan started")

    def snapshot(self) -> dict:
        with self._lock:
            state: dict[str, Any] = dict(self._state)
            errors = list(self._import_errors)
            ready, indexed = self._tracks_ready, self._tracks_indexed
        state["errors"] = [f"{item['path']}: {item['error']}" for item in errors]
        from autodj.beets_paths import check_library, page_view

        return {
            "folders": self.catalog.sources(),
            "import": state,
            "tracks_ready": ready,
            "tracks_indexed": indexed,
            # Left out of the advanced analysis for their length: not errors.
            "too_long_for_analysis": self.catalog.too_long_for_analysis_count(),
            "advanced_max_minutes": self.catalog.advanced_max_minutes(),
            # Whether beets' songs are the library's (None without beets).
            "beets_paths": page_view(check_library(self.cfg, self.entries_snapshot())),
        }

    def missing_removal_preview(
        self, *, scan_lock_held: bool = False, cleanup_lock_held: bool = False
    ) -> dict[str, Any]:
        """Describe missing records from the latest clean scan for explicit cleanup."""
        candidates, generation = self._missing_candidates(
            scan_lock_held=scan_lock_held, cleanup_lock_held=cleanup_lock_held
        )
        if candidates is None:
            # nosec B105 below: an absent cleanup confirmation token, not a password.
            return {"ready": False, "count": 0, "tracks": [], "token": None}  # nosec B105
        versions = self.catalog.change_versions()
        token_data = json.dumps(
            [
                generation,
                versions["entries"],
                versions["sources"],
                [item["path"] for item in candidates],
            ],
            separators=(",", ":"),
        )
        total = len(self.catalog.stored_entries())
        count = len(candidates)
        return {
            "ready": True,
            "count": count,
            "total": total,
            "percentage": 100 * count / total if total else 0.0,
            "requires_override": bool(total and count / total > 0.2),
            "tracks": candidates[:50],
            "token": hashlib.sha256(token_data.encode()).hexdigest(),
            "_versions": versions,
        }

    def _missing_candidates(
        self, *, scan_lock_held: bool = False, cleanup_lock_held: bool = False
    ) -> tuple[list[dict[str, str]], int] | tuple[None, int]:
        """Return missing files covered by sources with at least one reachable catalog file."""
        with self._lock:
            if (
                (self._scan_lock.locked() and not scan_lock_held)
                or (self._cleanup_active and not cleanup_lock_held)
                or not self._reconciled
                or self._walk_failed
            ):
                return None, self._scan_generation
            missing = set(self._missing)
            generation = self._scan_generation
        stored = {entry.path: (kind, entry) for kind, entry in self.catalog.stored_entries()}
        sources = self.catalog.sources()
        sources_with_files: set[str] = set()
        for stored_path, (kind, _entry) in stored.items():
            if not Path(stored_path).is_file():
                continue
            normalized = _path(stored_path)
            sources_with_files.update(
                source["id"]
                for source in sources
                if source["kind"] == kind
                and _under(normalized, source["path"], source["recursive"])
            )
        candidates: list[dict[str, str]] = []
        for path in sorted(missing):
            try:
                Path(path).stat()
            except FileNotFoundError:
                pass
            except OSError:
                # Permission and I/O errors do not prove that a file is gone.
                continue
            else:
                continue
            stored_item = stored.get(path)
            if stored_item is None:
                continue
            kind, entry = stored_item
            relevant = self.catalog.sources_for_path(path, kind)
            # A path covered by a disconnected source is never considered safe to remove.
            if not relevant or any(
                not Path(source["path"]).is_dir() or source["id"] not in sources_with_files
                for source in relevant
            ):
                continue
            candidates.append(
                {"path": path, "title": entry.title or Path(path).name, "artist": entry.artist}
            )
        return candidates, generation

    def remove_missing(self, token: str, *, confirmed: bool, allow_mass: bool = False) -> int:
        """Remove only candidates from a clean, unchanged preview after explicit confirmation."""
        if not confirmed:
            raise ValueError("Explicit confirmation is required")
        with self._lock:
            if self._scan_lock.locked() or self._cleanup_active:
                raise RuntimeError("A library scan or cleanup is running")
            self._cleanup_active = True
        try:
            preview = self.missing_removal_preview(cleanup_lock_held=True)
            if not preview["ready"] or preview["token"] != token:
                raise RuntimeError("The library changed; rescan and preview the removal again")
            if preview["requires_override"] and not allow_mass:
                raise ValueError(
                    "More than 20% of catalog tracks are missing; explicit override is required"
                )
            latest = self.missing_removal_preview(cleanup_lock_held=True)
            if latest["token"] != token:
                raise RuntimeError("The missing-track set changed; preview the removal again")
            candidates, _generation = self._missing_candidates(cleanup_lock_held=True)
            paths = [item["path"] for item in candidates or []]
            if any(os.path.isfile(path) for path in paths):
                raise RuntimeError("A missing track returned; preview the removal again")
            removed = self.catalog.remove_entries(paths, expected_versions=latest["_versions"])
            with self._lock:
                self._missing = frozenset(set(self._missing) - set(paths))
            self.refresh()
            return removed
        finally:
            with self._lock:
                self._cleanup_active = False

    def _claim_scan(self, source_ids: set[str] | None, override_tags: bool = False) -> bool:
        """Queue a scope and claim the worker lock; caller holds ``_lock``."""
        if self._cleanup_active:
            return False
        if source_ids is None or self._pending_sources is None:
            self._pending_sources = None
        else:
            self._pending_sources.update(source_ids)
        self._pending_override_tags = self._pending_override_tags or override_tags
        if not self._scan_lock.acquire(blocking=False):
            return False
        self._stop.clear()
        return True

    def start_scan(self, source_ids: set[str] | None = None, override_tags: bool = False) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._cleanup_active:
                return False
            if not self._claim_scan(source_ids, override_tags):
                return True
            self._thread = threading.Thread(
                target=self._scan_queued, name="autodj-library-scan", daemon=True
            )
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._scan_lock.release()
                raise
            return True

    def scan(self, source_ids: set[str] | None = None, override_tags: bool = False) -> None:
        with self._lock:
            if (
                self._closed
                or self._cleanup_active
                or not self._claim_scan(source_ids, override_tags)
            ):
                return
        self._scan_queued()

    def _scan_queued(self) -> None:
        acquired = True
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._closed:
                        break
                    source_ids = self._pending_sources
                    self._pending_sources = set()
                    override_tags = self._pending_override_tags
                    self._pending_override_tags = False
                    self._scan_generation += 1
                self._scan(source_ids, override_tags)
                with self._lock:
                    repeat = (
                        self._pending_sources is None
                        or bool(self._pending_sources)
                        or self._pending_override_tags
                    )
                    repeat = repeat and not self._stop.is_set()
                    if not repeat:
                        self._last_scan_end = self._clock()
                        self._scan_lock.release()
                        acquired = False
                        break
        finally:
            if acquired:
                with self._lock:
                    self._last_scan_end = self._clock()
                    self._scan_lock.release()

    def _set_state(self, **values: Any) -> None:
        with self._lock:
            self._state = cast(_ImportState, {**self._state, **values})

    def _publish_entry(self, entry: IndexEntry, kind: str) -> None:
        """Publish newly readable metadata to cached lookups without a full reload."""
        with self._lock:
            key = _path(entry.path)
            # Stored as a reload would give it: with the catalog's path.
            entry = replace(entry, path=key)
            self._by_path[key] = entry
            attr = "_liner_entries" if kind == "liner" else "_entries"
            current: tuple[IndexEntry, ...] = getattr(self, attr)
            place = self._positions.get(key)
            if place is not None and place[0] == kind:
                position = place[1]
                setattr(self, attr, (*current[:position], entry, *current[position + 1 :]))
            else:
                self._positions[key] = (kind, len(current))
                setattr(self, attr, (*current, entry))
            self._tracks_ready = len(self._entries)

    @staticmethod
    def _valid_basic_points(points: dict[str, Any], entry: IndexEntry) -> dict[str, Any]:
        """Keep imperfect automatic cue suggestions from rejecting a playable file."""
        length = max(float(entry.length), 0.001)
        start = points.get("start")
        end = points.get("end")
        start = 0.0 if start is None else float(start)
        end = length if end is None else float(end)
        if not 0 <= start < end <= length:
            start, end = 0.0, length
        result: dict[str, Any] = {"start": start, "end": end}
        for field in ("intro", "outro", "segue"):
            value = points.get(field)
            if value is None:
                result[field] = None
                continue
            value = float(value)
            result[field] = value if start <= value <= end else None
        return result

    def _discover(self, source_ids: set[str] | None = None) -> list[tuple[str, Path]]:
        formats = {f".{ext.lower().lstrip('.')}" for ext in self.cfg.library.supported_formats}
        found: dict[str, tuple[str, Path]] = {}
        for source in self.catalog.sources():
            if source_ids is not None and source["id"] not in source_ids:
                continue
            root = Path(source["path"])
            if not root.is_dir():
                with self._lock:
                    self._import_errors.append(
                        {"path": str(root), "error": "source directory is missing"}
                    )
                continue
            try:
                resolved_root = root.resolve(strict=True)
            except OSError as exc:
                with self._lock:
                    self._import_errors.append({"path": str(root), "error": str(exc)})
                    self._walk_failed = True
                continue
            allowed = formats
            if source["kind"] == "liner":
                from autodj.liners import LINER_EXTS

                allowed = set(LINER_EXTS)
            for current, directories, files in os.walk(root, onerror=self._walk_error):
                directories.sort()
                if not source["recursive"]:
                    directories.clear()
                for name in sorted(files):
                    if Path(name).suffix.lower() not in allowed:
                        continue
                    path = Path(current, name)
                    try:
                        path.resolve(strict=True).relative_to(resolved_root)
                    except ValueError:
                        # A file symlink may not expand the explicit source root.
                        continue
                    except OSError as exc:
                        # Listed but not resolvable just now (a share flapping):
                        # seen, so it is not marked gone, but not scanned.
                        logger.debug("Library scan could not resolve %s: %s", path, exc)
                        with self._lock:
                            self._seen_unresolved.add(_path(path))
                        continue
                    normalized = _path(path)
                    # A registered liner root owns paths nested under music roots.
                    if source["kind"] == "music" and self.catalog.sources_for_path(path, "liner"):
                        continue
                    found.setdefault(normalized, (source["kind"], path))
        return list(found.values())

    def _same_identity(self, record: dict[str, Any], path: Path) -> bool:
        size, fingerprint = file_fingerprint(path)
        return bool(
            size and fingerprint and size == record["size"] and fingerprint == record["fingerprint"]
        )

    def _walk_error(self, error: OSError) -> None:
        """A folder the scan could not read: noted, and nothing is marked gone."""
        logger.warning("Library scan could not read %s: %s", error.filename, error)
        with self._lock:
            self._import_errors.append({"path": str(error.filename), "error": str(error)})
            self._walk_failed = True

    def _reconcile_moves(
        self, found: list[tuple[str, Path]], source_ids: set[str] | None = None
    ) -> None:
        """Re-key records whose files moved, and note which tracks are gone.

        A record counts as gone only when a present folder this scan covers
        holds it, the scan did not find it, and it is not a file: a share
        that is not mounted marks nothing gone.  A scan whose walk could not
        read a folder marks nothing gone either, since the tracks in it were
        never seen.
        """
        with self._lock:
            walk_failed = self._walk_failed
        if walk_failed:
            logger.warning(
                "Library scan: a folder could not be read, so no track is marked gone this time"
            )
            return
        coverage = Coverage(
            (source["path"], source["kind"], source["recursive"])
            for source in self.catalog.sources()
            if source_ids is None or source["id"] in source_ids
        )
        old = self.catalog.stored_entries()
        old_paths = {entry.path for _kind, entry in old}
        found_paths = {_path(path) for _kind, path in found}
        with self._lock:
            found_paths |= self._seen_unresolved
        missing = {
            entry.path
            for kind, entry in old
            if entry.path not in found_paths
            and coverage.covered(entry.path, kind)
            and not os.path.isfile(entry.path)
        }
        candidates_by_kind: dict[str, list[Track]] = {"music": [], "liner": []}
        for kind, path in found:
            if _path(path) not in old_paths:
                candidates_by_kind[kind].append(Track(path, path.stem, "", "", "", 0.0, 0, 0.0))
        moved = False
        for kind in ("music", "liner"):
            kind_entries = [entry for entry_kind, entry in old if entry_kind == kind]
            moves = _detect_moves(kind_entries, missing, candidates_by_kind[kind])
            if not moves:
                continue
            for source, (track, _mtime) in moves.items():
                if not self.catalog.rekey(source, track.path):
                    continue
                with self._lock:
                    ledgers = {ledger.station: ledger for ledger in self._ledgers}
                    station_ids = set(self._station_ids)
                for station_id in station_ids:
                    ledger = ledgers.get(station_id)
                    if ledger is None:
                        store = self._station_store(station_id)
                        try:
                            store.rekey(source, track.path)
                        finally:
                            store.close()
                    else:
                        ledger.rekey(source, track.path)
                moved = True
                try:
                    if not (self.cfg.index.library_dir / "dj_meta.db").is_file():
                        continue
                    from autodj.dj_meta import get_cache

                    cache = get_cache(
                        self.cfg.index.library_dir, music_dir=self.cfg.library.music_dir
                    )
                    if cache is not None:
                        cache.rekey_many([(source, str(track.path))])
                except ValueError:
                    # Invalid legacy cache paths must not prevent library scanning.
                    pass
                finally:
                    missing.discard(source)
        with self._lock:
            kept = {path for path in self._missing if not coverage.covered(path)}
            self._missing = frozenset(kept | missing)
            changed = self._missing != self._loaded_missing
            self._reconciled = True
        if changed or moved:
            # Gone and moved tracks change now, not after the scan.
            self.refresh()

    def _basic_entry(self, fresh: IndexEntry, old: dict[str, Any] | None, path: Path) -> IndexEntry:
        size, fingerprint = file_fingerprint(path)
        fresh.size, fresh.fingerprint = size, fingerprint
        if old and old["size"] == size and old["fingerprint"] == fingerprint:
            for name in ("bpm", "year", "energy", "key", "mode", "tempo_confidence"):
                if getattr(fresh, name) in (0, -1) and old[name] not in (0, -1):
                    setattr(fresh, name, old[name])
        return fresh

    def _legacy_point_suggestions(self, path: Path) -> dict[str, float]:
        if not (self.cfg.index.library_dir / "dj_meta.db").is_file():
            return {}
        try:
            from autodj.dj_meta import get_cache

            cache = get_cache(self.cfg.index.library_dir, music_dir=self.cfg.library.music_dir)
            meta = cache.get(str(path)) if cache is not None else None
        except ValueError:
            # Invalid legacy cache paths must not prevent library scanning.
            return {}
        if meta is None:
            return {}
        return {
            name: value
            for name, value in (
                ("intro", meta.intro_end_s or None),
                ("outro", meta.outro_start_s or None),
            )
            if value is not None
        }

    def _scan_basic(
        self, source_ids: set[str] | None, override_tags: bool = False
    ) -> list[tuple[str, Path]]:
        self._set_state(phase="discovering", current="")
        found = self._discover(source_ids)
        self._set_state(phase="reading tracks", total=len(found), done=0)
        self._reconcile_moves(found, source_ids)
        # New music is listed from its tags first, then decoded below, so a
        # big new folder shows up at once.
        deferred: list[Path] = []
        for kind, path in found:
            if self._stop.is_set():
                break
            self._set_state(current=str(path))
            if (
                kind == "music"
                and self.catalog.record_for_path(path) is None
                and self._add_from_tags(path)
            ):
                deferred.append(path)
            else:
                self._read_track(kind, path, override_tags)
            with self._lock:
                self._state["done"] += 1
        if deferred:
            self._set_state(phase="analysing new songs", total=len(deferred), done=0)
        for path in deferred:
            if self._stop.is_set():
                break
            self._set_state(current=str(path))
            if not self._read_track("music", path, override_tags):
                # Not playable after all: it leaves the library, as before.
                self.catalog.remove_entries([str(path)])
            with self._lock:
                self._state["done"] += 1
        self.refresh()
        return found

    def _add_from_tags(self, path: Path) -> bool:
        """List a new song from its tags and length, without decoding it.

        It counts as silent until :meth:`_read_track` measures it, so
        automatic picks wait for it.  Returns ``False`` when the tags give no
        usable length; the song is then read the usual way.
        """
        from autodj.audio_meta import read_file_tags

        try:
            tags = read_file_tags(path)
            length = float(tags.length)
            if not length > 0:
                return False
            entry = IndexEntry(
                str(path),
                tags.title or path.stem,
                tags.artist,
                tags.album,
                tags.genre,
                tags.bpm,
                tags.year,
                length,
                0.0,
                -1,
                -1,
                0.0,
                embedded_at=path.stat().st_mtime,
            )
            entry = self._basic_entry(entry, None, path)
            self.catalog.put_entry(entry, "music")
        except Exception as exc:
            logger.debug("Could not list %s from its tags: %s", path, exc)
            return False
        self._publish_entry(entry, "music")
        return True

    def _read_track(self, kind: str, path: Path, override_tags: bool) -> bool:
        """Read *path*'s tags, cue points and analysis as needed; ``False`` on failure."""
        try:
            old = self.catalog.record_for_path(path)
            unchanged = old is not None and old["kind"] == kind and self._same_identity(old, path)
            mtime_changed = bool(
                unchanged and old is not None and path.stat().st_mtime_ns != old["mtime_ns"]
            )
            if (override_tags or mtime_changed) and unchanged:
                from autodj.audio_meta import read_file_tags

                tags = read_file_tags(path)
                embedded: dict[str, Any] = {
                    name: value
                    for name, value in (
                        ("title", tags.title),
                        ("artist", tags.artist),
                        ("album", tags.album),
                        ("genre", tags.genre),
                    )
                    if value
                }
                if tags.year > 0:
                    embedded["year"] = tags.year
                if np.isfinite(tags.bpm) and tags.bpm > 0:
                    embedded["bpm"] = tags.bpm
                self.catalog.retag(path, embedded, remember_manual=False)
            needs_liner_segue = bool(
                kind == "liner"
                and old is not None
                and old["points"].get("segue") is None
                and old["point_sources"].get("segue") not in {"user", "imported"}
            )
            old_points = old["points"] if old is not None else {}
            old_start = old_points.get("start")
            old_end = old_points.get("end")
            valid_existing_points = bool(
                unchanged
                and old is not None
                and isinstance(old_start, int | float)
                and not isinstance(old_start, bool)
                and isinstance(old_end, int | float)
                and not isinstance(old_end, bool)
                and np.isfinite(old_start)
                and np.isfinite(old_end)
                and 0 <= old_start < old_end <= old["length"]
            )
            if (
                valid_existing_points
                and needs_liner_segue
                and old is not None
                and not mtime_changed
            ):
                _, liner_points = analyze_basic(path, kind="liner")
                segue = liner_points.get("segue")
                if segue is not None:
                    segue = min(max(segue, cast(float, old_start)), cast(float, old_end))
                self.catalog.set_points(path, {"segue": segue}, source="analysis")
            elif not valid_existing_points and not mtime_changed:
                # One decode serves the cue points and the beats and cues.  A
                # long track's decode keeps only its start and end.
                dj = kind == "music" and self.dj_analysis_enabled() and self._dj_cache() is not None
                audio = load_for_analysis(path) if dj else None
                fresh, points = analyze_basic(path, kind=kind, audio=audio)
                fresh = self._basic_entry(fresh, old, path)
                suggestions = self._legacy_point_suggestions(path)
                for name, value in suggestions.items():
                    if points.get(name) is None:
                        points[name] = value
                points = self._valid_basic_points(points, fresh)
                self.catalog.put_entry(fresh, kind, points=points, point_source="analysis")
                self._publish_entry(fresh, kind)
                # New bytes are a new chance for a song held back after a
                # failed or stale analysis.
                self.analysis_queue.clear_holds(_path(path), fresh.size, fresh.fingerprint)
                if audio is not None:
                    self._dj_analyse(path, audio, force=True)
                    del audio
            if kind == "music":
                record = self.catalog.record_for_path(path)
                if record is not None and record["intro_lift_db"] is None:
                    from autodj.intro_lift import measure_source_intro

                    try:
                        lift = measure_source_intro(path, SAMPLE_RATE, record["energy"])
                    except (OSError, RuntimeError, ValueError) as exc:
                        logger.warning("Intro analysis unavailable for %s: %s", path, exc)
                        lift = 0.0
                    self.catalog.set_intro_lift(path, lift)
        except Exception as exc:
            logger.warning("Basic library scan failed for %s: %s", path, exc)
            with self._lock:
                self._import_errors.append({"path": str(path), "error": str(exc)})
            return False
        return True

    def _scan(self, source_ids: set[str] | None = None, override_tags: bool = False) -> None:
        queued = self._scan_lock.locked()
        with self._lock:
            self._import_errors = []
            self._walk_failed = False
            self._seen_unresolved = set()
            self._reconciled = False
        self._set_state(running=True, phase="discovering", done=0, total=0, current="")
        completed = False
        try:
            found = self._scan_basic(source_ids, override_tags)
            # Only Smooth and Stereo Tool use the stored measurement (and
            # measure a track themselves when it has none).
            measure = self.cfg.playback.loudness_processing in {"smooth", "stereotool"}
            loudness = {_path(path): (kind, path) for kind, path in found} if measure else {}
            done = 0
            if measure:
                self._set_state(phase="measuring loudness", done=0, total=len(loudness), current="")
            while not self._stop.is_set():
                if queued:
                    with self._lock:
                        pending = self._pending_sources
                        self._pending_sources = set()
                        pending_override_tags = self._pending_override_tags
                        self._pending_override_tags = False
                        if pending is None or pending:
                            self._scan_generation += 1
                    if pending is None or pending:
                        added = self._scan_basic(pending, pending_override_tags)
                        if measure:
                            prioritized = {}
                            for item in added:
                                key = _path(item[1])
                                loudness.pop(key, None)
                                prioritized[key] = item
                            loudness = {**prioritized, **loudness}
                            self._set_state(
                                running=True,
                                phase="measuring loudness",
                                done=done,
                                total=done + len(loudness),
                                current="",
                            )
                        continue
                if not loudness:
                    break
                key = next(iter(loudness))
                kind, path = loudness.pop(key)
                self._set_state(current=str(path))
                try:
                    record = self.catalog.record_for_path(path)
                    if record is not None and not loudness_matches(
                        record.get("loudness"), path, kind
                    ):
                        self.catalog.set_loudness(path, analyze_loudness(path, kind))
                except Exception as exc:
                    logger.warning("Loudness analysis failed for %s: %s", path, exc)
                    with self._lock:
                        self._import_errors.append({"path": str(path), "error": str(exc)})
                finally:
                    done += 1
                    with self._lock:
                        self._state["done"] = done
                        self._state["total"] = done + len(loudness)
            self._dj_backlog()
            self._set_state(phase="complete")
            completed = True
        finally:
            self.refresh()
            self._set_state(running=False, current="")
            done_hook = self.on_scan_done
            with self._lock:
                clean = completed and not self._walk_failed and self._reconciled
            if done_hook is not None and not clean:
                logger.info(
                    "Library scan: a folder could not be read or the scan stopped early; "
                    "skipping the tidy-up"
                )
            elif done_hook is not None:
                try:
                    done_hook()
                except Exception:
                    logger.exception("Library: the after-scan tidy-up failed")

    def _dj_cache(self) -> Any:
        """The DJ-meta cache (beat grid and cues), or ``None`` when it cannot open."""
        from autodj.dj_meta import get_cache

        try:
            return get_cache(self.cfg.index.library_dir, music_dir=self.cfg.library.music_dir)
        except (OSError, ValueError) as exc:
            logger.warning("DJ analysis unavailable: %s", exc)
            return None

    def _dj_analyse(
        self, path: Path, audio: np.ndarray | TrackWindows | None = None, force: bool = False
    ) -> None:
        """Store *path*'s beat grid and cues, placed from its saved Intro and Outro.

        *audio* is the song already decoded at ``SAMPLE_RATE`` by
        :func:`~autodj.processing.load_for_analysis`: all of it, or a long
        song's start and end, which are analysed on their own
        (:func:`~autodj.dj_meta.analyse_windows`).  It is decoded here when
        ``None``.  Without *force* a song already analysed is left alone.  A
        failure is logged and the scan goes on.
        """
        if not self.dj_analysis_enabled():
            return
        cache = self._dj_cache()
        if cache is None:
            return
        try:
            if not force and cache.get(str(path)).analysed:
                return
            if audio is None:
                audio = load_for_analysis(path)
            from autodj.dj_meta import analyse_decoded

            points = self.catalog.points(path)
            length = audio.seconds if isinstance(audio, TrackWindows) else len(audio) / SAMPLE_RATE
            meta = analyse_decoded(
                audio,
                SAMPLE_RATE,
                intro_end_s=points.get("intro") or 0.0,
                outro_start_s=points.get("outro") or length,
            )
            if self.cfg.playback.import_external_cues:
                from autodj.dj_cues_import import auto_import_cues, merge_imported_cues

                if self._dj_library_cues is None:
                    self._dj_library_cues = auto_import_cues(
                        library_root=self.cfg.library.music_dir
                    )
                merge_imported_cues(meta, str(path), self._dj_library_cues)
            cache.set(str(path), meta)
            cache.flush()
        except Exception as exc:
            logger.warning("DJ analysis failed for %s: %s", path, exc)

    def _dj_backlog(self) -> None:
        """Analyse the beat grid and cues of music songs that still lack them.

        Stops early for a scan waiting its turn; the rest is picked up by the
        next scan.
        """
        if not self.dj_analysis_enabled():
            return
        cache = self._dj_cache()
        if cache is None:
            return
        backlog = [
            Path(record["path"])
            for record in self.catalog.all_entries("music")
            if not cache.get(record["path"]).analysed and Path(record["path"]).is_file()
        ]
        self._set_state(phase="analysing beats", done=0, total=len(backlog), current="")
        for done, path in enumerate(backlog, start=1):
            with self._lock:
                waiting = self._pending_sources is None or bool(self._pending_sources)
            if self._stop.is_set() or waiting:
                break
            self._set_state(current=str(path))
            self._dj_analyse(path)
            self._set_state(done=done)
        cache.flush(force=True)
        self._dj_library_cues = None

    def analysis_pending(self, force: bool = False) -> list[dict[str, Any]]:
        if not force:
            return self.catalog.unembedded_music()
        # Force re-embeds songs that already have a vector, so it lists every eligible song.
        # Songs over the length cutoff stay out, as they do without force.
        limit = self.catalog.advanced_max_minutes() * 60.0
        pending = []
        for record in self.catalog.all_entries("music"):
            if limit and record["length"] > limit:
                continue
            if not Path(record["path"]).is_file():
                continue
            if any(
                source["advanced"]
                for source in self.catalog.sources_for_path(record["path"], "music")
            ) and not self.catalog.sources_for_path(record["path"], "liner"):
                pending.append(record)
        return pending

    def schedule_analysis(self) -> bool:
        with self._lock:
            phase = self._state["phase"]
            if self._scan_lock.locked() and phase != "measuring loudness":
                return False
        # A connected analysis worker takes the whole queue; the CPU job
        # waits, and runs again for whatever is left once it has gone.
        if self.analysis_queue.worker_active():
            with self._lock:
                self._yielded_to_worker = True
            return False
        from autodj.model_identity import model_analysis_available

        if not model_analysis_available():
            return False
        with self._lock:
            if self._yielded_to_worker:
                self._yielded_to_worker = False
                self._analysis_started_generation = None
                self._analysis_idle = None
            generation = self._scan_generation
            if self._analysis_started_generation == generation:
                return False
        # Nothing to embed stays so until a scan or a change to the records,
        # vectors or sources (not cues or loudness): remember that instead of
        # checking every tick.
        versions = self.catalog.change_versions()
        state = (generation, versions["entries"], versions["vectors"], versions["sources"])
        with self._lock:
            if self._analysis_idle == state:
                return False
        if not self.analysis_pending():
            with self._lock:
                self._analysis_idle = state
            return False
        from autodj.jobs import get_manager

        manager = get_manager()
        manager.configure(
            config_path=self.cfg.config_path,
            index_dir=self.cfg.index.index_dir,
        )
        if not manager.start("analyse-library"):
            return False
        with self._lock:
            self._analysis_started_generation = generation
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._scan_lock:
            pass
        self._flush_ledgers()
        self.analysis_queue.close()
        with self._lock:
            ledgers = list(self._ledgers)
        for ledger in ledgers:
            ledger.store.close()
        self.catalog.close()


def analyse_library(
    cfg: AutoDJConfig,
    wrapper: MuqWrapper,
    *,
    limit: int | None = None,
    force: bool = False,
    workers: int | None = None,
) -> int:
    """Embed pending catalog tracks without publishing a FAISS generation.

    Each song is leased (see :mod:`autodj.analysis_queue`) before it is
    decoded, so a remote worker never gets a song this run is analysing.
    Unless *force* is set, the run stops taking songs once a remote worker
    connects and leaves the rest to it.
    """
    manager = LibraryManager(cfg, _EmptySimilarity())
    report = DecodeReport()
    queue = manager.analysis_queue
    yielded = False

    def gate(track: Track) -> bool | None:
        nonlocal yielded
        if not force and queue.worker_active():
            if not yielded:
                yielded = True
                logger.info("An analysis worker is connected; leaving the remaining songs to it.")
            return None
        record = manager.catalog.record_for_path(track.path)
        if record is None:
            return False
        return bool(
            queue.claim(
                SERVER_HOLDER,
                [(record["path"], record["size"], record["fingerprint"])],
                SERVER_LEASE_SECONDS,
            )
        )

    try:
        pending = manager.analysis_pending(force=force)
        if limit is not None:
            pending = pending[: max(0, limit)]
        completed = 0
        tracks = [
            Track(
                Path(record["path"]),
                record["title"],
                record["artist"],
                record["album"],
                record["genre"],
                record["bpm"],
                record["year"],
                record["length"],
            )
            for record in pending
        ]

        def checkpoint(entries: list[IndexEntry], vectors: list[np.ndarray]) -> None:
            nonlocal completed
            if not entries:
                return
            entry = entries[-1]
            if not any(
                source["advanced"]
                for source in manager.catalog.sources_for_path(entry.path, "music")
            ) or manager.catalog.sources_for_path(entry.path, "liner"):
                return
            stored = manager.catalog.store_analysis(
                entry.path,
                entry.size,
                entry.fingerprint,
                {
                    "energy": entry.energy,
                    "key": entry.key,
                    "mode": entry.mode,
                    "bpm": entry.bpm,
                    "tempo_confidence": entry.tempo_confidence,
                },
                vectors[-1],
            )
            queue.release_path(SERVER_HOLDER, _path(entry.path))
            completed += int(stored)

        if tracks:
            with report:
                _embed_new_tracks(
                    tracks,
                    wrapper,
                    workers=workers,
                    checkpoint=checkpoint,
                    throttle_ms=cfg.index.throttle_ms,
                    report=report,
                    gate=gate,
                )
        _finish_decode_report(report, cfg.index.library_dir / LIBRARY_DECODE_LOG)
        return completed
    finally:
        manager.close()


class _EmptySimilarity:
    """Minimal empty similarity source for the standalone analysis worker."""

    class _Vectors:
        ntotal = 0

    faiss_index = _Vectors()

    def entries_snapshot(self) -> tuple[IndexEntry, ...]:
        return ()


__all__ = ["LibraryManager", "analyse_library"]
