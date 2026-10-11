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
from autodj.catalog import LibraryCatalog, _path
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
    prepare_source,
    prepared_source_matches,
    remove_stale_prepared,
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
        self._catalog_mtime = 0
        self._entries: tuple[IndexEntry, ...] = ()
        self._liner_entries: tuple[IndexEntry, ...] = ()
        self._by_path: dict[str, IndexEntry] = {}
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
        """Seed new legacy generations into the catalog, without changing them."""
        items = list(entries)
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.shape != (len(items), FEATURE_DIM):
            raise ValueError("legacy vectors do not match the index entries")
        self.catalog.seed_entries(items)
        self.catalog.seed_vectors(items, matrix)
        self.refresh()

    def _mtime(self) -> int:
        try:
            return self.catalog.path.stat().st_mtime_ns
        except OSError:
            return 0

    def refresh(self) -> tuple[IndexEntry, ...]:
        with self._refresh_lock:
            entries = self.catalog.entries_snapshot("music")
            liner_entries = self.catalog.entries_snapshot("liner")
            vector_entries, vectors = self.catalog.vectors_snapshot()
            replace = getattr(self.sim, "replace_catalog_index", None)
            if replace is not None:
                replace(vector_entries, vectors)
            with self._lock:
                self._entries = entries
                self._liner_entries = liner_entries
                self._by_path = {_path(entry.path): entry for entry in (*entries, *liner_entries)}
                self._tracks_ready = len(entries)
                self._tracks_indexed = len(vector_entries)
                self._last_played.update(self.catalog.last_played())
                self._last_played.update(self._pending_plays)
                self._catalog_mtime = self._mtime()
                return self._entries

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
        if self._mtime() != self._catalog_mtime:
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
            current = getattr(self, attr)
            if not any(_path(item.path) == key for item in current):
                setattr(self, attr, (*current, entry))
            else:
                setattr(
                    self,
                    attr,
                    tuple(entry if _path(item.path) == key else item for item in current),
                )
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

    @staticmethod
    def _entry_from_record(record: dict) -> IndexEntry:
        return IndexEntry(
            path=record["path"],
            **{
                key: record[key]
                for key in (
                    "title",
                    "artist",
                    "album",
                    "genre",
                    "bpm",
                    "year",
                    "length",
                    "energy",
                    "key",
                    "mode",
                    "tempo_confidence",
                    "embedded_at",
                    "size",
                    "fingerprint",
                )
            },
        )

    def _reconcile_moves(self, found: list[tuple[str, Path]]) -> None:
        old = list(self.catalog.all_entries())
        old_paths = {_path(record["path"]) for record in old}
        missing = {_path(record["path"]) for record in old if not Path(record["path"]).is_file()}
        candidates_by_kind: dict[str, list[Track]] = {"music": [], "liner": []}
        for kind, path in found:
            if _path(path) not in old_paths:
                candidates_by_kind[kind].append(Track(path, path.stem, "", "", "", 0.0, 0, 0.0))
        for kind in ("music", "liner"):
            kind_entries = [self._entry_from_record(row) for row in old if row["kind"] == kind]
            kind_missing = {
                path
                for path in missing
                if any(row["kind"] == kind and _path(row["path"]) == path for row in old)
            }
            moves = _detect_moves(kind_entries, kind_missing, candidates_by_kind[kind])
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
            self._reconcile_moves(found)
            cache_dir = self.cfg.index.active_dir / "prepared"
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
            self._set_state(phase="preparing audio", done=0, total=len(found), current="")
            for kind, path in found:
                if self._stop.is_set():
                    break
                self._set_state(current=str(path))
                try:
                    record = self.catalog.record_for_path(path)
                    if record is None:
                        continue
                    prepared_path = record["prepared_path"]
                    quiet_boost_db = self.cfg.playback.quiet_boost_db
                    if prepared_path and prepared_source_matches(
                        prepared_path, kind, quiet_boost_db
                    ):
                        continue
                    prepared = prepare_source(path, cache_dir, kind, quiet_boost_db)
                    entry = self._entry_from_record(record)
                    self.catalog.put_entry(entry, kind, prepared_path=str(prepared))
                except Exception as exc:
                    logger.warning("Audio preparation failed for %s: %s", path, exc)
                    with self._lock:
                        self._import_errors.append({"path": str(path), "error": str(exc)})
                finally:
                    with self._lock:
                        self._state["done"] += 1
            if not self._stop.is_set():
                self._remove_stale_prepared(cache_dir)
            self._set_state(phase="complete")
        finally:
            self.refresh()
            self._set_state(running=False, current="")

    def _remove_stale_prepared(self, cache_dir: Path) -> None:
        """Drop prepared audio that no catalog record uses after a settings change."""
        keep: set[str] = set()
        for record in self.catalog.all_entries():
            keep.add(record["path"])
            if record["prepared_path"]:
                keep.add(record["prepared_path"])
        try:
            removed, freed = remove_stale_prepared(
                cache_dir, keep, self.cfg.playback.quiet_boost_db
            )
        except OSError as exc:
            logger.warning("Could not clean prepared audio in %s: %s", cache_dir, exc)
            return
        if removed:
            logger.info(
                "Removed %d outdated prepared audio file(s), freeing %.1f MB",
                removed,
                freed / 1_000_000,
            )

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
            if self._scan_lock.locked() and phase != "preparing audio":
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
