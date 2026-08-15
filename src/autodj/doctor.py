"""Read-only installation and runtime diagnostics for AutoDJ."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from autodj.config import is_loopback_bind
from autodj.index_manifest import (
    IndexConsistencyError,
    IndexManifest,
    legacy_artifacts_allowed,
    publication_is_tombstoned,
    read_manifest,
    sha256_file,
)
from autodj.version import current_version

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig, IndexConfig, ModelConfig
    from autodj.model import ModelCacheStatus


REQUIRED_BUILT_ASSETS = (
    "index.html",
    "app.js",
    "app.css",
    "bitcrusher-worklet.js",
    "stutter-worklet.js",
    "freeze-worklet.js",
    "glitch-worklet.js",
)
_TRACKS_SCHEMA_SIGNATURE = (
    ("vec_row", "INTEGER", 1, None, 0),
    ("path", "TEXT", 1, None, 0),
    ("title", "TEXT", 1, "''", 0),
    ("artist", "TEXT", 1, "''", 0),
    ("album", "TEXT", 1, "''", 0),
    ("genre", "TEXT", 1, "''", 0),
    ("bpm", "REAL", 1, "0", 0),
    ("year", "INTEGER", 1, "0", 0),
    ("length", "REAL", 1, "0", 0),
    ("energy", "REAL", 1, "0", 0),
    ("key", "INTEGER", 1, "-1", 0),
    ("mode", "INTEGER", 1, "-1", 0),
    ("tempo_confidence", "REAL", 1, "0", 0),
    ("embedded_at", "REAL", 1, "0", 0),
)
_DJ_META_SCHEMA_SIGNATURE = (
    ("path", "TEXT", 0, None, 1),
    ("intro_end_s", "REAL", 1, "0", 0),
    ("outro_start_s", "REAL", 1, "0", 0),
    ("analysed", "INTEGER", 1, "0", 0),
    ("beats", "TEXT", 0, None, 0),
    ("cues", "TEXT", 0, None, 0),
)
_TRACKS_COLUMNS = frozenset(column[0] for column in _TRACKS_SCHEMA_SIGNATURE)
_DJ_META_COLUMNS = frozenset(column[0] for column in _DJ_META_SCHEMA_SIGNATURE)
_SCHEMA_SIGNATURES = {
    "tracks": _TRACKS_SCHEMA_SIGNATURE,
    "dj_meta": _DJ_META_SCHEMA_SIGNATURE,
}
_TRACK_ROW_COLUMNS = (
    "vec_row",
    "path",
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
)
_TRACK_STORAGE_CLASSES = (
    "integer",
    "text",
    "text",
    "text",
    "text",
    "text",
    "real",
    "integer",
    "real",
    "real",
    "integer",
    "integer",
    "real",
    "real",
)
_TRACK_REAL_POSITIONS = (6, 8, 9, 12, 13)


def inspect_model_cache(model_cfg: ModelConfig, index_cfg: IndexConfig) -> ModelCacheStatus:
    """Lazy model-cache inspection keeps doctor startup free of Torch imports."""
    from autodj.model import inspect_model_cache as inspect

    return inspect(model_cfg, index_cfg)


class CheckStatus(StrEnum):
    """Severity of one doctor check."""

    PASS = "pass"  # nosec B105
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DoctorCheck:
    """One stable, render-independent diagnostic result."""

    name: str
    status: CheckStatus
    summary: str
    detail: str | dict[str, Any] = ""


@dataclass(frozen=True)
class DoctorReport:
    """Ordered collection of diagnostics and its process exit status."""

    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        """Return one exactly when at least one check failed."""
        return int(any(check.status is CheckStatus.FAIL for check in self.checks))

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable report."""
        return {"exit_code": self.exit_code, "checks": [asdict(check) for check in self.checks]}

    def to_json(self) -> str:
        """Serialize the report without exposing configuration secrets."""
        return json.dumps(self.to_dict(), indent=2, default=str)


def render_text(report: DoctorReport) -> str:
    """Render one screen-reader-friendly line per check."""
    lines: list[str] = []
    for check in report.checks:
        line = f"[{check.status.upper()}] {check.name}: {check.summary}"
        if check.detail:
            detail = (
                json.dumps(check.detail, sort_keys=True, default=str)
                if isinstance(check.detail, dict)
                else check.detail
            )
            line += f" {detail}"
        lines.append(line)
    return "\n".join(lines)


def _configuration_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Summarize effective non-secret configuration values."""
    effective = {
        "sources": list(cfg.config_sources),
        "host": cfg.server.host,
        "port": cfg.server.port,
        "music_dir": str(cfg.library.music_dir),
        "index_dir": str(cfg.index.index_dir),
        "model_dir": str(cfg.index.model_dir),
        "access_token": "<redacted>" if cfg.server.access_token else None,
        "huggingface_token": "<redacted>" if cfg.huggingface.token else None,
    }
    return DoctorCheck(
        "configuration",
        CheckStatus.PASS,
        " < ".join(cfg.config_sources),
        effective,
    )


def _python_check(version: tuple[int, int] | None = None) -> DoctorCheck:
    """Require the project's exact supported Python minor series."""
    actual = sys.version_info[:2] if version is None else version
    rendered = f"{actual[0]}.{actual[1]}"
    if actual == (3, 14):
        return DoctorCheck("python", CheckStatus.PASS, rendered, "AutoDJ requires Python ==3.14.*.")
    return DoctorCheck(
        "python",
        CheckStatus.FAIL,
        rendered,
        "AutoDJ requires Python ==3.14.*; use the project-managed interpreter.",
    )


def _nearest_existing_parent(path: Path) -> Path | None:
    """Find the nearest existing parent of a path."""
    candidate = path.parent
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def _path_check(name: str, path: Path, *, writable: bool) -> DoctorCheck:
    """Check one path without creating it or probing by writing."""
    if path.exists():
        readable = path.is_dir() and os.access(path, os.R_OK)
        write_ok = not writable or os.access(path, os.W_OK)
        if readable and write_ok:
            return DoctorCheck(name, CheckStatus.PASS, str(path))
        return DoctorCheck(
            name,
            CheckStatus.FAIL,
            str(path),
            "required directory permissions missing",
        )
    parent = _nearest_existing_parent(path)
    if writable and parent is not None and parent.is_dir() and os.access(parent, os.W_OK):
        return DoctorCheck(
            name,
            CheckStatus.WARN,
            str(path),
            f"missing; writable parent {parent} can create it",
        )
    return DoctorCheck(name, CheckStatus.FAIL, str(path), "path does not exist")


def _legacy_artifacts(index_dir: Path) -> tuple[bool, bool, bool]:
    """Report which legacy index artifacts exist."""
    tracks = (index_dir / "tracks.db").is_file()
    vectors = (index_dir / "vectors.index").is_file()
    generations = any(index_dir.glob("tracks.g*.db")) or any(index_dir.glob("vectors.g*.index"))
    return tracks, vectors, generations


def _legacy_index_counts(index_dir: Path) -> tuple[int, int]:
    """Read row and vector counts from a legacy index."""
    import faiss

    tracks_path = index_dir / "tracks.db"
    entries = _validate_sqlite(tracks_path, "tracks", _TRACKS_COLUMNS)
    vectors = int(faiss.read_index(str(index_dir / "vectors.index")).ntotal)
    return entries, vectors


def _validate_schema(
    conn: sqlite3.Connection,
    table: str,
    required: frozenset[str],
) -> None:
    """Validate a SQLite table against its runtime schema contract."""
    schema_query = {
        "tracks": "PRAGMA table_info(tracks)",
        "dj_meta": "PRAGMA table_info(dj_meta)",
    }[table]
    rows = conn.execute(schema_query).fetchall()
    columns = {str(row[1]) for row in rows}
    missing = sorted(required - columns)
    if missing:
        raise sqlite3.DatabaseError("missing required columns: " + ", ".join(missing))
    actual = tuple(
        (
            str(row[1]),
            str(row[2]).strip().upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in rows
    )
    if actual != _SCHEMA_SIGNATURES[table]:
        raise sqlite3.DatabaseError(f"{table} schema does not match the runtime contract")
    if table != "tracks":
        return
    unique_single_columns: set[str] = set()
    for index_row in conn.execute("PRAGMA index_list(tracks)"):
        if int(index_row[2]) != 1 or (len(index_row) > 4 and int(index_row[4]) != 0):
            continue
        index_name = str(index_row[1]).replace('"', '""')
        index_columns = [
            str(row[2])
            for row in conn.execute(  # nosec B608
                f'PRAGMA index_info("{index_name}")'
            )
        ]
        if len(index_columns) == 1:
            unique_single_columns.add(index_columns[0])
    if not {"vec_row", "path"}.issubset(unique_single_columns):
        raise sqlite3.DatabaseError("tracks schema is missing required unique identities")


def _published_index_counts(index_dir: Path, manifest: IndexManifest) -> tuple[int, int]:
    """Optimistically inspect one manifest-selected generation without locking."""
    tracks_path = index_dir / manifest.tracks_file
    vectors_path = index_dir / manifest.vectors_file
    before_sidecars = _sidecar_snapshots(tracks_path)
    if any(snapshot[0] for snapshot in before_sidecars):
        raise IndexConsistencyError(
            "published tracks WAL/SHM sidecars indicate unpublished database state"
        )
    before_hashes = (sha256_file(tracks_path), sha256_file(vectors_path))
    if before_hashes != (manifest.tracks_sha256, manifest.vectors_sha256):
        raise IndexConsistencyError("published artifact SHA-256 mismatch")

    with closing(_open_readonly_sqlite(tracks_path, immutable=True)) as conn:
        _validate_schema(conn, "tracks", _TRACKS_COLUMNS)
        rows = conn.execute(
            "SELECT vec_row, path, title, artist, album, genre, bpm, year, length, "
            "energy, key, mode, tempo_confidence, embedded_at, "
            "typeof(vec_row), typeof(path), typeof(title), typeof(artist), "
            "typeof(album), typeof(genre), typeof(bpm), typeof(year), typeof(length), "
            "typeof(energy), typeof(key), typeof(mode), typeof(tempo_confidence), "
            "typeof(embedded_at) FROM tracks ORDER BY vec_row"
        ).fetchall()
    for expected_row, row in enumerate(rows):
        values = row[: len(_TRACK_ROW_COLUMNS)]
        storage_classes = row[len(_TRACK_ROW_COLUMNS) :]
        for position, (name, actual, expected) in enumerate(
            zip(
                _TRACK_ROW_COLUMNS,
                storage_classes,
                _TRACK_STORAGE_CLASSES,
                strict=True,
            )
        ):
            if actual != expected:
                value_detail = ""
                if expected in {"integer", "real"}:
                    value_detail = f" ({values[position]!r})"
                raise ValueError(
                    f"{name} storage class must be {expected}, got {actual}{value_detail}"
                )
        if values[0] != expected_row:
            raise IndexConsistencyError("tracks vec_row identity is not canonical")
        for position in _TRACK_REAL_POSITIONS:
            if not math.isfinite(values[position]):
                raise ValueError(f"{_TRACK_ROW_COLUMNS[position]} must be finite")

    import faiss

    from autodj.indexer import FEATURE_DIM

    vectors_index = faiss.read_index(str(vectors_path))
    if type(vectors_index) is not faiss.IndexFlatIP:
        raise IndexConsistencyError(
            f"FAISS index type must be IndexFlatIP, got {type(vectors_index).__name__}"
        )
    if int(vectors_index.d) != FEATURE_DIM:
        raise IndexConsistencyError(f"FAISS dimension must be {FEATURE_DIM}, got {vectors_index.d}")
    if int(vectors_index.metric_type) != int(faiss.METRIC_INNER_PRODUCT):
        raise IndexConsistencyError("FAISS metric must be inner product")
    if not vectors_index.is_trained:
        raise IndexConsistencyError("FAISS IndexFlatIP must be trained")
    vector_count = int(vectors_index.ntotal)
    after_sidecars = _sidecar_snapshots(tracks_path)
    if any(snapshot[0] for snapshot in after_sidecars):
        raise IndexConsistencyError(
            "published tracks WAL/SHM sidecars appeared during inspection; retry"
        )
    after_hashes = (sha256_file(tracks_path), sha256_file(vectors_path))
    after_manifest = read_manifest(index_dir)
    if (
        after_manifest != manifest
        or after_hashes != before_hashes
        or after_sidecars != before_sidecars
    ):
        raise IndexConsistencyError("published generation changed during inspection; retry")
    if len(rows) != manifest.vector_count or vector_count != manifest.vector_count:
        raise IndexConsistencyError(
            f"index count mismatch: manifest={manifest.vector_count}, "
            f"sqlite={len(rows)}, faiss={vector_count}"
        )
    return len(rows), vector_count


def _index_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Validate one coherent index generation without migration or repair."""
    index_dir = cfg.index.active_dir
    if not index_dir.exists():
        return DoctorCheck(
            "index-coherence",
            CheckStatus.WARN,
            "empty index",
            f"{index_dir}; run `autodj index` before playback",
        )
    if not index_dir.is_dir():
        return DoctorCheck(
            "index-coherence",
            CheckStatus.FAIL,
            "partial published index",
            f"{index_dir} is not a directory; run `autodj index` to republish",
        )
    try:
        manifest = read_manifest(index_dir)
        if manifest is None:
            if publication_is_tombstoned(index_dir):
                return DoctorCheck(
                    "index-coherence",
                    CheckStatus.WARN,
                    "empty index",
                    "coherently tombstoned; run `autodj index` when music is available",
                )
            if not legacy_artifacts_allowed(index_dir):
                return DoctorCheck(
                    "index-coherence",
                    CheckStatus.FAIL,
                    "unreadable published generation",
                    "manifest missing despite publication history; run `autodj index --force`",
                )
            tracks, vectors, generations = _legacy_artifacts(index_dir)
            if generations:
                return DoctorCheck(
                    "index-coherence",
                    CheckStatus.FAIL,
                    "partial published index",
                    "generation files lack a manifest; run `autodj index --force`",
                )
            if not tracks and not vectors:
                return DoctorCheck(
                    "index-coherence",
                    CheckStatus.WARN,
                    "empty index",
                    f"{index_dir}; run `autodj index` before playback",
                )
            if not tracks or not vectors:
                return DoctorCheck(
                    "index-coherence",
                    CheckStatus.FAIL,
                    "partial published index",
                    "tracks.db and vectors.index must both exist; run `autodj index --force`",
                )
            entry_count, vector_count = _legacy_index_counts(index_dir)
        else:
            lock_path = index_dir / ".index-publication.lock"
            if not lock_path.is_file():
                return DoctorCheck(
                    "index-coherence",
                    CheckStatus.FAIL,
                    "unreadable published generation",
                    "publication lock missing; run `autodj index` to restore it",
                )
            entry_count, vector_count = _published_index_counts(index_dir, manifest)
        if entry_count != vector_count:
            raise IndexConsistencyError(f"tracks={entry_count}, vectors={vector_count}")
    except ImportError:
        return DoctorCheck(
            "index-coherence",
            CheckStatus.FAIL,
            "index dependencies unavailable",
            "install AutoDJ index dependencies and retry",
        )
    except (
        FileNotFoundError,
        IndexConsistencyError,
        OSError,
        RuntimeError,
        sqlite3.DatabaseError,
        ValueError,
    ) as exc:
        return DoctorCheck(
            "index-coherence",
            CheckStatus.FAIL,
            "unreadable published generation",
            f"{exc}; run `autodj index` to republish the index",
        )
    if entry_count == 0:
        return DoctorCheck(
            "index-coherence",
            CheckStatus.WARN,
            "empty index",
            "run `autodj index` after adding music",
        )
    if manifest is None:
        return DoctorCheck(
            "index-coherence",
            CheckStatus.WARN,
            f"{entry_count} legacy vectors and rows",
            "no generation manifest; run `autodj index` to publish one",
        )
    return DoctorCheck(
        "index-coherence",
        CheckStatus.PASS,
        f"generation {manifest.generation}: {entry_count} vectors and rows",
    )


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    """Build a read-only SQLite URI for a resolved path."""
    encoded = quote(str(path.resolve()), safe="/:\\")
    suffix = "&immutable=1" if immutable else ""
    return f"file:{encoded}?mode=ro{suffix}"


def _open_readonly_sqlite(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Open a query-only SQLite connection and close it if setup fails."""
    conn = sqlite3.connect(_sqlite_uri(path, immutable=immutable), uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    return conn


type _FileSnapshot = tuple[bool, int | None, int | None, str | None]
type _SidecarSnapshot = tuple[
    bool,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
]


def _file_snapshot(path: Path) -> _FileSnapshot:
    """Capture file presence, size, time, and content hash."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False, None, None, None
    return True, stat.st_size, stat.st_mtime_ns, sha256_file(path)


def _sidecar_snapshot(path: Path) -> _SidecarSnapshot:
    """Capture identity metadata for one SQLite sidecar path."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False, None, None, None, None, None
    return (
        True,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
    )


def _sidecar_snapshots(path: Path) -> tuple[_SidecarSnapshot, _SidecarSnapshot]:
    """Capture WAL and shared-memory sidecar identities."""
    return (
        _sidecar_snapshot(Path(f"{path}-wal")),
        _sidecar_snapshot(Path(f"{path}-shm")),
    )


def _validate_sqlite(
    path: Path,
    table: str,
    required: frozenset[str],
) -> int:
    """Validate an unchanged SQLite file and return its row count."""
    count_query = {
        "tracks": "SELECT COUNT(*) FROM tracks",
        "dj_meta": "SELECT COUNT(*) FROM dj_meta",
    }[table]
    before_sidecars = _sidecar_snapshots(path)
    if any(snapshot[0] for snapshot in before_sidecars):
        raise sqlite3.DatabaseError(
            "active SQLite WAL sidecars cannot be safely validated read-only; "
            "close writers and retry"
        )
    before_database = _file_snapshot(path)
    with closing(_open_readonly_sqlite(path, immutable=True)) as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise sqlite3.DatabaseError("integrity_check: " + "; ".join(integrity))
        _validate_schema(conn, table, required)
        count = int(conn.execute(count_query).fetchone()[0])
    after_sidecars = _sidecar_snapshots(path)
    if any(snapshot[0] for snapshot in after_sidecars):
        raise sqlite3.DatabaseError("SQLite files changed during validation; retry when idle")
    if _file_snapshot(path) != before_database or after_sidecars != before_sidecars:
        raise sqlite3.DatabaseError("SQLite files changed during validation; retry when idle")
    return count


def _tracks_database_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Validate the security-selected tracks database in read-only SQLite mode."""
    index_dir = cfg.index.active_dir
    try:
        manifest = read_manifest(index_dir) if index_dir.is_dir() else None
        tombstoned = index_dir.is_dir() and publication_is_tombstoned(index_dir)
        legacy_allowed = index_dir.is_dir() and legacy_artifacts_allowed(index_dir)
    except (IndexConsistencyError, OSError) as exc:
        return DoctorCheck(
            "tracks-db",
            CheckStatus.FAIL,
            "integrity/schema check failed",
            f"{exc}; run `autodj index --force`",
        )
    path = index_dir / (manifest.tracks_file if manifest is not None else "tracks.db")
    if tombstoned:
        return DoctorCheck(
            "tracks-db",
            CheckStatus.WARN,
            "database absent",
            f"{path}; run `autodj index` when music is available",
        )
    if manifest is None and index_dir.is_dir() and not legacy_allowed:
        return DoctorCheck(
            "tracks-db",
            CheckStatus.FAIL,
            "integrity/schema check failed",
            "publication history has no active manifest; run `autodj index --force`",
        )
    if not path.is_file():
        return DoctorCheck(
            "tracks-db",
            CheckStatus.WARN,
            "database absent",
            f"{path}; run `autodj index`",
        )
    try:
        count = _validate_sqlite(path, "tracks", _TRACKS_COLUMNS)
    except (OSError, sqlite3.DatabaseError) as exc:
        return DoctorCheck(
            "tracks-db",
            CheckStatus.FAIL,
            "integrity/schema check failed",
            f"{exc}; run `autodj index --force`",
        )
    return DoctorCheck(
        "tracks-db", CheckStatus.PASS, "integrity and schema valid", f"{path}; rows={count}"
    )


def _dj_meta_database_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Validate the active optional DJ metadata cache without creating it."""
    path = cfg.index.active_dir / "dj_meta.db"
    if not path.is_file():
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.WARN,
            "database absent",
            f"{path}; run `autodj analyse` when DJ metadata is needed",
        )
    try:
        count = _validate_sqlite(path, "dj_meta", _DJ_META_COLUMNS)
    except (OSError, sqlite3.DatabaseError) as exc:
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.FAIL,
            "integrity/schema check failed",
            f"{exc}; rebuild it with `autodj analyse`",
        )
    return DoctorCheck(
        "dj-meta-db", CheckStatus.PASS, "integrity and schema valid", f"{path}; rows={count}"
    )


def _module_available(name: str) -> bool:
    """Return whether a Python module is loaded or has a discoverable import spec."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _dependency_check() -> DoctorCheck:
    """Inspect optional audio modules and FFmpeg without importing or executing them."""
    missing_modules = [name for name in ("soundfile", "sounddevice") if not _module_available(name)]
    ffmpeg = shutil.which("ffmpeg")
    if not missing_modules and ffmpeg:
        return DoctorCheck(
            "dependencies",
            CheckStatus.PASS,
            "soundfile, sounddevice, and FFmpeg are available.",
        )
    issues: list[str] = []
    details: list[str] = []
    if missing_modules:
        issues.append("missing " + ", ".join(missing_modules))
        details.append("Install playback dependencies with the play or all extra.")
    if not ffmpeg:
        issues.append("FFmpeg missing")
        details.append(
            "Optional ALAC browser transcoding is unavailable; raw ALAC fallback remains available."
        )
    return DoctorCheck(
        "dependencies",
        CheckStatus.WARN,
        "; ".join(issues) + ".",
        "Action: " + " ".join(details),
    )


def _model_cache_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Inspect the configured model cache without downloading or repairing it."""
    try:
        status = inspect_model_cache(cfg.model, cfg.index)
    except (ImportError, OSError, RuntimeError, ValueError):
        return DoctorCheck(
            "model-cache",
            CheckStatus.FAIL,
            "inspection failed",
            "inspect model-cache permissions and installed model dependencies",
        )
    if status.complete:
        return DoctorCheck("model-cache", CheckStatus.PASS, str(status.path), status.reason)
    return DoctorCheck(
        "model-cache",
        CheckStatus.WARN,
        str(status.path),
        f"{status.reason}; run `autodj index` to download or validate the model",
    )


def _network_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Classify configured bind exposure using the canonical loopback policy."""
    server = cfg.server
    if is_loopback_bind(server.host):
        return DoctorCheck(
            "network-safety",
            CheckStatus.PASS,
            "loopback-only",
            f"{server.host}:{server.port}",
        )
    if server.access_token:
        return DoctorCheck(
            "network-safety",
            CheckStatus.PASS,
            "authenticated non-loopback bind",
            f"{server.host}:{server.port}",
        )
    if server.insecure_lan:
        return DoctorCheck(
            "network-safety",
            CheckStatus.WARN,
            "explicit insecure LAN acknowledgement",
            f"{server.host}:{server.port}; configure an access token when possible",
        )
    return DoctorCheck(
        "network-safety",
        CheckStatus.FAIL,
        "non-loopback bind lacks authentication",
        f"{server.host}:{server.port}; configure an access token or explicit insecure LAN mode",
    )


def _bundle_check(package_dir: Path | None = None) -> DoctorCheck:
    """Validate built web bundle metadata or report source-asset fallback."""
    root = Path(__file__).parent if package_dir is None else package_dir
    bundle = root / "static_dist"
    stamp = bundle / "build-info.json"
    runtime_version: str | None = None
    if stamp.is_file():
        try:
            payload = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return DoctorCheck(
                "frontend-bundle",
                CheckStatus.FAIL,
                "invalid build-info.json",
                f"{exc}; rebuild the web bundle",
            )
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str) or not version:
            return DoctorCheck(
                "frontend-bundle",
                CheckStatus.FAIL,
                "invalid build-info.json",
                "version must be a non-empty string; rebuild the web bundle",
            )
        try:
            runtime_version = current_version()
        except (OSError, RuntimeError, TypeError, ValueError):
            return DoctorCheck(
                "frontend-bundle",
                CheckStatus.FAIL,
                "version inspection failed",
                "inspect installed/source version metadata and rebuild the web bundle",
            )
        if version != runtime_version:
            return DoctorCheck(
                "frontend-bundle",
                CheckStatus.FAIL,
                f"bundle {version}; runtime {runtime_version}",
                "rebuild the web bundle for the installed AutoDJ version",
            )
    complete = all((bundle / name).is_file() for name in REQUIRED_BUILT_ASSETS)
    if not stamp.is_file() or not complete:
        reason = "missing build-info.json" if not stamp.is_file() else "incomplete built assets"
        return DoctorCheck(
            "frontend-bundle",
            CheckStatus.WARN,
            "source assets in use",
            f"{reason}; build the production bundle when packaging AutoDJ",
        )
    return DoctorCheck(
        "frontend-bundle",
        CheckStatus.PASS,
        f"bundle {runtime_version}; runtime {runtime_version}",
    )


def run_doctor(
    cfg: AutoDJConfig,
    *,
    python_version: tuple[int, int] | None = None,
) -> DoctorReport:
    """Run all checks in stable order without repairing or creating state."""
    return DoctorReport(
        (
            _configuration_check(cfg),
            _python_check(python_version),
            _path_check("music-path", cfg.library.music_dir, writable=False),
            _path_check("index-path", cfg.index.index_dir, writable=True),
            _path_check("model-path", cfg.index.model_dir, writable=True),
            _index_check(cfg),
            _tracks_database_check(cfg),
            _dj_meta_database_check(cfg),
            _dependency_check(),
            _model_cache_check(cfg),
            _network_check(cfg),
            _bundle_check(),
        )
    )
