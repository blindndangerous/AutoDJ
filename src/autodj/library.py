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
    IndexEntry,
    _detect_moves,
    _embed_new_tracks,
    file_fingerprint,
)
from autodj.processing import analyze_basic, prepare_source

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
        self._rescan_requested = False
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

    def tick(self) -> None:
        self._flush_plays()
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

    def start_scan(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._thread is not None and self._thread.is_alive():
                self._rescan_requested = True
                return True
            if self._scan_lock.locked():
                self._rescan_requested = True
                return True
            self._stop.clear()
            self._rescan_requested = False
            self._thread = threading.Thread(
                target=self.scan, name="autodj-library-scan", daemon=True
            )
            self._thread.start()
            return True

    def scan(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            with self._lock:
                if not self._closed:
                    self._rescan_requested = True
            return
        acquired = True
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._closed:
                        break
                    self._scan_generation += 1
                self._scan()
                with self._lock:
                    repeat = self._rescan_requested and not self._stop.is_set()
                    self._rescan_requested = False
                    if not repeat:
                        self._scan_lock.release()
                        acquired = False
                        break
        finally:
            if acquired:
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

    def _discover(self) -> list[tuple[str, Path]]:
        formats = {f".{ext.lower().lstrip('.')}" for ext in self.cfg.library.supported_formats}
        found: dict[str, tuple[str, Path]] = {}
        for source in self.catalog.sources():
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

    def _scan(self) -> None:
        with self._lock:
            self._import_errors = []
        self._set_state(running=True, phase="discovering", done=0, total=0, current="")
        try:
            found = self._discover()
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
                    if (
                        unchanged
                        and old is not None
                        and old["points"].get("start") is not None
                        and old["points"].get("end") is not None
                        and old["points"]["end"] > old["points"]["start"]
                    ):
                        pass
                    else:
                        fresh, points = analyze_basic(path)
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
                    if prepared_path and Path(prepared_path).is_file():
                        continue
                    prepared = prepare_source(path, cache_dir, kind)
                    entry = self._entry_from_record(record)
                    self.catalog.put_entry(entry, kind, prepared_path=str(prepared))
                except Exception as exc:
                    logger.warning("Audio preparation failed for %s: %s", path, exc)
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
            index_dir=self.cfg.index.active_dir,
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
        self._flush_settings()
        self.catalog.close()


def analyse_library(cfg: AutoDJConfig, wrapper: MuqWrapper) -> int:
    """Embed pending catalog tracks without publishing a FAISS generation."""
    manager = LibraryManager(cfg, _EmptySimilarity())
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

            _embed_new_tracks(
                [track],
                wrapper,
                workers=1,
                checkpoint=checkpoint,
                throttle_ms=cfg.index.throttle_ms,
            )
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
