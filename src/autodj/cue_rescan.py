"""Bounded, server-owned cue scan proposals for Track info."""

from __future__ import annotations

import hashlib
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from autodj.indexer import file_fingerprint
from autodj.processing import analyze_basic

if TYPE_CHECKING:
    from autodj.catalog import LibraryCatalog


class CueRescan:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, dict] = {}
        self._closed = threading.Event()

    @staticmethod
    def _identity(path: Path) -> tuple:
        audio_stat = path.stat()
        audio = (
            audio_stat.st_size,
            audio_stat.st_mtime_ns,
            audio_stat.st_ino,
            file_fingerprint(path),
        )
        lyrics = path.with_suffix(".lrc")
        try:
            lyrics_stat = lyrics.stat()
            sidecar = (
                lyrics_stat.st_size,
                lyrics_stat.st_mtime_ns,
                lyrics_stat.st_ino,
                hashlib.sha256(lyrics.read_bytes()).hexdigest(),
            )
        except FileNotFoundError:
            sidecar = None
        return audio, sidecar

    def scan(self, catalog: LibraryCatalog, path: str, station: str) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A cue scan is already running")
        try:
            if self._closed.is_set():
                raise RuntimeError("Cue scanning is shutting down")
            record = catalog.record_for_path(path)
            if record is None:
                raise KeyError(path)
            resolved = Path(record["path"])
            if not resolved.is_file():
                raise FileNotFoundError(record["path"])
            identity = self._identity(resolved)
            kind = record["kind"]
            from autodj.library import LibraryManager

            fresh, detected = analyze_basic(resolved, kind=kind)

            detected = LibraryManager._valid_basic_points(detected, fresh)
            if record["length"] > 0 and abs(fresh.length - record["length"]) > 0.1:
                raise RuntimeError(
                    "Recording length changed; rescan the library before scanning cues"
                )
            allowed = (
                ("start", "intro", "outro", "segue", "end")
                if kind == "music"
                else ("start", "segue", "end")
            )
            points = {name: detected.get(name) for name in allowed}
            sources = dict.fromkeys(points, "analysis")
            if kind == "music":
                from autodj.audio_meta import load_lrc_for
                from autodj.vocals import sung_spans

                lines = load_lrc_for(resolved)
                if lines and sung_spans(lines):
                    for name in ("intro", "outro"):
                        if points.get(name) is not None:
                            sources[name] = "lyrics"
            current = {key: record[key] for key in ("points", "point_sources", "length", "kind")}
            latest = catalog.record_for_path(path)
            if (
                latest is None
                or {key: latest[key] for key in current} != current
                or self._identity(resolved) != identity
            ):
                raise RuntimeError("Recording or saved cues changed during scan")
            token = secrets.token_urlsafe(24)
            self._tokens[token] = {
                "path": record["path"],
                "station": station,
                "identity": identity,
                "expected": current,
                "points": points,
                "sources": sources,
            }
            while len(self._tokens) > 8:
                self._tokens.pop(next(iter(self._tokens)))
            return {
                "token": token,
                "points": points,
                "point_sources": sources,
                "current_points": current["points"],
                "current_sources": current["point_sources"],
            }
        finally:
            self._lock.release()

    def apply(
        self, catalog: LibraryCatalog, path: str, station: str, token: str, fields: list[str]
    ) -> None:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A cue scan is already running")
        try:
            if self._closed.is_set():
                raise RuntimeError("Cue scanning is shutting down")
            proposal = self._tokens.get(token)
            record = catalog.record_for_path(path)
            if (
                proposal is None
                or record is None
                or proposal["path"] != record["path"]
                or proposal["station"] != station
            ):
                raise RuntimeError("Scan expired; scan the cues again")
            if (
                not fields
                or len(set(fields)) != len(fields)
                or set(fields) - proposal["points"].keys()
            ):
                raise ValueError("Select one or more detected cue fields")
            if self._identity(Path(record["path"])) != proposal["identity"]:
                raise RuntimeError("Recording changed; scan the cues again")
            values = {name: proposal["points"][name] for name in fields}
            sources = {name: proposal["sources"][name] for name in fields}
            catalog.apply_rescanned_points(
                path,
                proposal["expected"],
                values,
                sources,
                lambda: self._identity(Path(record["path"])) == proposal["identity"],
            )
            self._tokens.pop(token, None)
        finally:
            self._lock.release()

    def shutdown(self, timeout: float) -> bool:
        self._closed.set()
        if not self._lock.acquire(timeout=timeout):
            return False
        self._lock.release()
        return True
