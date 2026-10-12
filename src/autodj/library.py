"""Source scanning and playable-library orchestration."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import numpy as np

from autodj.beets import Track
from autodj.catalog import Coverage, LibraryCatalog, _path
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
    analyze_basic,
    analyze_loudness,
    loudness_matches,
)
from autodj.stereo import SAMPLE_RATE, DecodeReport

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.model import MuqWrapper


class _ImportState(TypedDict):
    running: bool
    phase: str
    done: int
    total: int
    current: str


class LibraryManager:
    """Keep a per-index catalog current without modifying legacy FAISS files."""

    def __init__(self, cfg: AutoDJConfig, sim: Any) -> None:
        self.cfg = cfg
        self.sim = sim
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_sources: set[str] | None = set()
        self._closed = False
        self._scan_generation = 0
        self._analysis_started_generation: int | None = None
        self._state: _ImportState = {
            "running": False,
            "phase": "idle",
            "done": 0,
            "total": 0,
            "current": "",
        }
        self._import_errors: list[dict[str, str]] = []
        self._pending_plays: dict[str, float] = {}
        self._pending_history: dict[str, dict] = {}
        self._interrupted_plays: set[str] = set()
        self._pending_settings: dict[str, Any] = {}
        self._last_played: dict[str, float] = {}
        # The catalog change counters the loaded snapshot was read at.
        self._versions: dict[str, int] | None = None
        # Paths a scan found gone from a present source folder; left out of
        # the playable library without a file-system call per track.
        self._missing: frozenset[str] = frozenset()
        self._loaded_missing: frozenset[str] = frozenset()
        # The music paths, in FAISS row order, of the vectors handed to sim.
        self._vector_paths: tuple[str, ...] | None = None
        self._entries: tuple[IndexEntry, ...] = ()
        self._liner_entries: tuple[IndexEntry, ...] = ()
        self._by_path: dict[str, IndexEntry] = {}
        # Normalized path -> (kind, position in that kind's entries tuple).
        self._positions: dict[str, tuple[str, int]] = {}
        self._tracks_ready = 0
        self._tracks_indexed = 0
        self.catalog = LibraryCatalog(cfg.index.active_dir / "library.db")
        liner_root = cfg.playback.liners_folder or str(cfg.index.active_dir / "liners")
        try:
            self.catalog.seed_sources(cfg.library.music_dir, liner_root)
            self.seed_index(sim.entries_snapshot(), self._legacy_vectors())
        except BaseException:
            self.catalog.close()
            raise

    def _legacy_vectors(self) -> np.ndarray:
        count = int(self.sim.faiss_index.ntotal)
        if count == 0:
            return np.empty((0, FEATURE_DIM), dtype=np.float32)
        return np.vstack([self.sim.faiss_index.reconstruct(i) for i in range(count)]).astype(
            np.float32, copy=False
        )

    def seed_index(self, entries: Iterable[IndexEntry], vectors: np.ndarray) -> None:
        """Seed new legacy generations into the catalog, without changing them.

        Also hands the catalog's vectors back to ``sim``, which a reload of
        the legacy generation from disk has just replaced.
        """
        items = list(entries)
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.shape != (len(items), FEATURE_DIM):
            raise ValueError("legacy vectors do not match the index entries")
        self.catalog.seed_entries(items)
        self.catalog.seed_vectors(items, matrix)
        self.refresh(force_vectors=True)

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
                played: dict[str, float] = {}
                if reload_entries:
                    coverage = reader.coverage()
                    entries = reader.entries("music", coverage, missing)
                    liner_entries = reader.entries("liner", coverage, missing)
                    played = reader.last_played()
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
                    self._last_played.update(played)
                    self._last_played.update(self._pending_plays)
                self._tracks_ready = len(self._entries)
                self._tracks_indexed = len(vector_paths or ())
                self._vector_paths = vector_paths
                self._versions = versions
                self._loaded_missing = missing
                return self._entries

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

    def record_for_id(self, record_id: str) -> dict | None:
        record = self.catalog.record_for_id(record_id)
        if record is None:
            return None
        with self._lock:
            return record if _path(record["path"]) in self._by_path else None

    def remember_play(self, path: str | Path, timestamp: float | None = None) -> None:
        value = time.time() if timestamp is None else float(timestamp)
        if not np.isfinite(value):
            return
        normalized = _path(path)
        with self._lock:
            self._pending_plays[normalized] = value
            self._last_played[normalized] = value

    def remember_setting(self, key: str, value: Any) -> None:
        """Buffer a runtime setting update without writing SQLite on an audio thread."""
        with self._lock:
            self._pending_settings[key] = value

    def remember_history(self, row: dict) -> None:
        """Queue a play-log entry without writing SQLite on the audio thread."""
        with self._lock:
            self._pending_history[row["event_id"]] = dict(row)

    def interrupt_history(self, event_id: str) -> None:
        with self._lock:
            self._interrupted_plays.add(event_id)

    def pending_history(self) -> list[dict]:
        """Plays not yet written to SQLite, oldest first, with each track's album.

        The rows :meth:`~autodj.catalog.LibraryCatalog.recent_plays` gives
        once they are written: a play a set stop cut short is left out, and
        a track no longer in the library has an empty album.
        """
        with self._lock:
            rows = [
                dict(row)
                for event_id, row in self._pending_history.items()
                if event_id not in self._interrupted_plays
            ]
            for row in rows:
                entry = self._by_path.get(_path(row.get("path") or ""))
                row["album"] = entry.album if entry is not None else ""
        return rows

    def last_played(self) -> dict[str, float]:
        with self._lock:
            return dict(self._last_played)

    def _flush_plays(self) -> None:
        with self._lock:
            pending = dict(self._pending_plays)
        for path, value in pending.items():
            self.catalog.remember_play(path, value)
            with self._lock:
                if self._pending_plays.get(path) == value:
                    self._pending_plays.pop(path, None)

    def _flush_settings(self) -> None:
        with self._lock:
            pending = dict(self._pending_settings)
        for key, value in pending.items():
            self.catalog.set_setting(key, value)
            with self._lock:
                if self._pending_settings.get(key) == value:
                    self._pending_settings.pop(key, None)

    def _flush_history(self) -> None:
        with self._lock:
            pending = dict(self._pending_history)
            interrupted = set(self._interrupted_plays)
        if not pending and not interrupted:
            return
        self.catalog.write_history(list(pending.values()), list(interrupted))
        with self._lock:
            for event_id, row in pending.items():
                if self._pending_history.get(event_id) == row:
                    self._pending_history.pop(event_id, None)
            self._interrupted_plays.difference_update(interrupted)

    def tick(self) -> None:
        self._flush_plays()
        self._flush_history()
        self._flush_settings()
        with self._lock:
            # A scan publishes its own tracks and reloads when it has read
            # them; while it measures loudness, analysis may add vectors.
            scanning = self._scan_lock.locked() and self._state["phase"] != "measuring loudness"
        if not scanning and self._changed():
            self.refresh()
        self.schedule_analysis()

    def snapshot(self) -> dict:
        with self._lock:
            state: dict[str, Any] = dict(self._state)
            errors = list(self._import_errors)
            ready, indexed = self._tracks_ready, self._tracks_indexed
        state["errors"] = [f"{item['path']}: {item['error']}" for item in errors]
        return {
            "folders": self.catalog.sources(),
            "import": state,
            "tracks_ready": ready,
            "tracks_indexed": indexed,
        }

    def _claim_scan(self, source_ids: set[str] | None) -> bool:
        """Queue a scope and claim the worker lock; caller holds ``_lock``."""
        if source_ids is None or self._pending_sources is None:
            self._pending_sources = None
        else:
            self._pending_sources.update(source_ids)
        if not self._scan_lock.acquire(blocking=False):
            return False
        self._stop.clear()
        return True

    def start_scan(self, source_ids: set[str] | None = None) -> bool:
        with self._lock:
            if self._closed:
                return False
            if not self._claim_scan(source_ids):
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

    def scan(self, source_ids: set[str] | None = None) -> None:
        with self._lock:
            if self._closed or not self._claim_scan(source_ids):
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
                    self._scan_generation += 1
                self._scan(source_ids)
                with self._lock:
                    repeat = self._pending_sources is None or bool(self._pending_sources)
                    repeat = repeat and not self._stop.is_set()
                    if not repeat:
                        self._scan_lock.release()
                        acquired = False
                        break
        finally:
            if acquired:
                with self._lock:
                    self._scan_lock.release()

    def _set_state(self, **values: Any) -> None:
        with self._lock:
            self._state = cast(_ImportState, {**self._state, **values})

    def _publish_entry(self, entry: IndexEntry, kind: str) -> None:
        """Publish newly readable metadata to cached lookups without a full reload."""
        with self._lock:
            key = _path(entry.path)
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
                continue
            allowed = formats
            if source["kind"] == "liner":
                from autodj.liners import LINER_EXTS

                allowed = set(LINER_EXTS)
            for current, directories, files in os.walk(root):
                directories.sort()
                if not source["recursive"]:
                    directories.clear()
                for name in sorted(files):
                    if Path(name).suffix.lower() not in allowed:
                        continue
                    path = Path(current, name)
                    try:
                        path.resolve(strict=True).relative_to(resolved_root)
                    except (OSError, ValueError):
                        # A file symlink may not expand the explicit source root.
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

    def _reconcile_moves(
        self, found: list[tuple[str, Path]], source_ids: set[str] | None = None
    ) -> None:
        """Re-key records whose files moved, and note which tracks are gone.

        A record counts as gone only when a present folder this scan covers
        holds it, the scan did not find it, and it is not a file: a share
        that is not mounted marks nothing gone.
        """
        coverage = Coverage(
            (source["path"], source["kind"], source["recursive"])
            for source in self.catalog.sources()
            if source_ids is None or source["id"] in source_ids
        )
        old = self.catalog.stored_entries()
        old_paths = {entry.path for _kind, entry in old}
        found_paths = {_path(path) for _kind, path in found}
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
        for kind in ("music", "liner"):
            kind_entries = [entry for entry_kind, entry in old if entry_kind == kind]
            moves = _detect_moves(kind_entries, missing, candidates_by_kind[kind])
            if not moves:
                continue
            for source, (track, _mtime) in moves.items():
                if not self.catalog.rekey(source, track.path):
                    continue
                try:
                    if not (self.cfg.index.active_dir / "dj_meta.db").is_file():
                        continue
                    from autodj.dj_meta import get_cache

                    cache = get_cache(
                        self.cfg.index.active_dir, music_dir=self.cfg.library.music_dir
                    )
                    if cache is not None:
                        cache.rekey_many([(source, str(track.path))])
                except ValueError:
                    # The legacy cache is rooted at one configured directory.
                    pass
                finally:
                    missing.discard(source)
        with self._lock:
            kept = {path for path in self._missing if not coverage.covered(path)}
            self._missing = frozenset(kept | missing)
            changed = self._missing != self._loaded_missing
        if changed:
            # Gone tracks leave the playable library now, not after the scan.
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
        if not (self.cfg.index.active_dir / "dj_meta.db").is_file():
            return {}
        try:
            from autodj.dj_meta import get_cache

            cache = get_cache(self.cfg.index.active_dir, music_dir=self.cfg.library.music_dir)
            meta = cache.get(str(path)) if cache is not None else None
        except ValueError:
            # The legacy cache is rooted at one configured directory.
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

    def _scan(self, source_ids: set[str] | None = None) -> None:
        with self._lock:
            self._import_errors = []
        self._set_state(running=True, phase="discovering", done=0, total=0, current="")
        try:
            found = self._discover(source_ids)
            self._set_state(total=len(found), phase="reading tracks")
            self._reconcile_moves(found, source_ids)
            for kind, path in found:
                if self._stop.is_set():
                    break
                self._set_state(current=str(path))
                try:
                    old = self.catalog.record_for_path(path)
                    unchanged = old is not None and self._same_identity(old, path)
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
                    if valid_existing_points and needs_liner_segue and old is not None:
                        _, liner_points = analyze_basic(path, kind="liner")
                        segue = liner_points.get("segue")
                        if segue is not None:
                            segue = min(max(segue, cast(float, old_start)), cast(float, old_end))
                        self.catalog.set_points(path, {"segue": segue}, source="analysis")
                    elif valid_existing_points:
                        pass
                    else:
                        fresh, points = analyze_basic(path, kind=kind)
                        fresh = self._basic_entry(fresh, old, path)
                        suggestions = self._legacy_point_suggestions(path)
                        for name, value in suggestions.items():
                            if points.get(name) is None:
                                points[name] = value
                        points = self._valid_basic_points(points, fresh)
                        self.catalog.put_entry(
                            fresh,
                            kind,
                            points=points,
                            point_source="analysis",
                        )
                        self._publish_entry(fresh, kind)
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
                finally:
                    with self._lock:
                        self._state["done"] += 1
            self.refresh()
            # Only Smooth and Stereo Tool use the stored measurement (and
            # measure a track themselves when it has none).
            measure = self.cfg.playback.loudness_processing in {"smooth", "stereotool"}
            if measure:
                self._set_state(phase="measuring loudness", done=0, total=len(found), current="")
            for kind, path in found if measure else ():
                if self._stop.is_set():
                    break
                self._set_state(current=str(path))
                try:
                    record = self.catalog.record_for_path(path)
                    if record is None:
                        continue
                    if loudness_matches(record.get("loudness"), path, kind):
                        continue
                    self.catalog.set_loudness(path, analyze_loudness(path, kind))
                except Exception as exc:
                    logger.warning("Loudness analysis failed for %s: %s", path, exc)
                    with self._lock:
                        self._import_errors.append({"path": str(path), "error": str(exc)})
                finally:
                    with self._lock:
                        self._state["done"] += 1
            self._set_state(phase="complete")
        finally:
            self.refresh()
            self._set_state(running=False, current="")

    def analysis_pending(self) -> list[dict[str, Any]]:
        pending = []
        for record in self.catalog.all_entries("music"):
            if not Path(record["path"]).is_file() or self.catalog.has_vector(record["path"]):
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
            generation = self._scan_generation
            if self._analysis_started_generation == generation:
                return False
        if not self.analysis_pending():
            return False
        from autodj.jobs import get_manager

        manager = get_manager()
        manager.configure(
            config_path=self.cfg.config_path,
            index_dir=self.cfg.index.index_dir,
            index_name=self.cfg.index.name,
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
        self._flush_plays()
        self._flush_history()
        self._flush_settings()
        self.catalog.close()


def analyse_library(cfg: AutoDJConfig, wrapper: MuqWrapper) -> int:
    """Embed pending catalog tracks without publishing a FAISS generation."""
    manager = LibraryManager(cfg, _EmptySimilarity())
    report = DecodeReport()
    try:
        pending = manager.analysis_pending()
        completed = 0
        for record in pending:
            if not any(
                source["advanced"]
                for source in manager.catalog.sources_for_path(record["path"], "music")
            ):
                continue
            track = Track(
                Path(record["path"]),
                record["title"],
                record["artist"],
                record["album"],
                record["genre"],
                record["bpm"],
                record["year"],
                record["length"],
            )

            def checkpoint(entries: list[IndexEntry], vectors: list[np.ndarray]) -> None:
                nonlocal completed
                if not entries:
                    return
                entry = entries[-1]
                if not any(
                    source["advanced"]
                    for source in manager.catalog.sources_for_path(entry.path, "music")
                ):
                    return
                manager.catalog.put_entry(entry)
                manager.catalog.set_vector(entry.path, vectors[-1])
                completed += 1

            with report:
                _embed_new_tracks(
                    [track],
                    wrapper,
                    workers=1,
                    checkpoint=checkpoint,
                    throttle_ms=cfg.index.throttle_ms,
                    report=report,
                )
        _finish_decode_report(report, cfg.index.active_dir / LIBRARY_DECODE_LOG)
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
