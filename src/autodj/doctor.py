"""Read-only installation and runtime diagnostics for AutoDJ."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.config import is_loopback_bind
from autodj.index_manifest import OldDjMetaCacheError, first_absolute_path
from autodj.sqlite_utils import readonly_uri

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig, IndexConfig, ModelConfig
    from autodj.model import ModelCacheStatus


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
        "lan": cfg.server.lan,
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


def _index_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Load the index with the loader ``serve`` uses and report the outcome."""
    index_dir = cfg.index.active_dir
    if not index_dir.exists():
        return DoctorCheck(
            "index", CheckStatus.WARN, "no index", f"{index_dir}; run `autodj index`"
        )
    try:
        from autodj.similarity import SimilarityIndex

        sim = SimilarityIndex.from_index_dir(index_dir, music_dir=cfg.library.music_dir)
    except FileNotFoundError as exc:
        return DoctorCheck("index", CheckStatus.WARN, "no index", str(exc))
    except Exception as exc:  # report whatever serve would fail with
        return DoctorCheck("index", CheckStatus.FAIL, type(exc).__name__, str(exc))
    manifest = sim.manifest
    assert manifest is not None  # from_index_dir always sets it
    summary = f"generation {manifest.generation}: {sim.ntotal} tracks"
    detail = f"published {manifest.published_at}"
    if sim.ntotal == 0:
        return DoctorCheck("index", CheckStatus.WARN, summary, f"{detail}; the index is empty")
    return DoctorCheck("index", CheckStatus.PASS, summary, detail)


def _dj_meta_database_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check the active DJ metadata cache read-only, without creating it."""
    path = cfg.index.active_dir / "dj_meta.db"
    if not path.is_file():
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.WARN,
            "database absent",
            f"{path}; run `autodj analyse` when DJ metadata is needed",
        )
    try:
        conn = sqlite3.connect(readonly_uri(path), uri=True)
        with closing(conn):
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise sqlite3.DatabaseError("integrity_check: " + "; ".join(integrity))
            count = int(conn.execute("SELECT COUNT(*) FROM dj_meta").fetchone()[0])
            absolute = first_absolute_path(conn, "dj_meta")
        if absolute is not None:
            raise OldDjMetaCacheError(path, absolute)
    except OldDjMetaCacheError as exc:
        return DoctorCheck("dj-meta-db", CheckStatus.FAIL, "old DJ metadata cache", str(exc))
    except (OSError, sqlite3.DatabaseError) as exc:
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.FAIL,
            "integrity check failed",
            f"{exc}; rebuild it with `autodj analyse`",
        )
    return DoctorCheck("dj-meta-db", CheckStatus.PASS, "integrity valid", f"{path}; rows={count}")


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


def _lan_network_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Report what ``serve`` in LAN mode will bind, allow and authenticate with.

    Read-only: a missing access token file is reported, never created.

    Args:
        cfg: Loaded configuration with ``[server] lan`` on.

    Returns:
        PASS with a configured, saved or to-be-created token; WARN without
        pairing (``insecure_lan``); FAIL when the saved token cannot be read.
    """
    from autodj.lan import AccessTokenError, detect_lan_hosts, lan_bind_host, read_access_token
    from autodj.stream_secret import access_token_path

    server = cfg.server
    detail: dict[str, Any] = {
        "bind": f"{lan_bind_host(server.host)}:{server.port}",
        "detected_hosts": detect_lan_hosts(),
    }
    if server.access_token:
        detail["access_token"] = "configured"  # nosec B105 -- status label, not the token
        return DoctorCheck("network-safety", CheckStatus.PASS, "LAN mode", detail)
    if server.insecure_lan:
        detail["access_token"] = None
        return DoctorCheck(
            "network-safety",
            CheckStatus.WARN,
            "LAN mode without pairing (insecure_lan)",
            detail,
        )
    path = access_token_path(cfg)
    try:
        saved = read_access_token(path)
    except AccessTokenError as exc:
        detail["access_token"] = str(exc)
        return DoctorCheck(
            "network-safety", CheckStatus.FAIL, "LAN mode cannot load its access token", detail
        )
    detail["access_token"] = (
        f"saved in {path}" if saved is not None else f"created on first start at {path}"
    )
    return DoctorCheck("network-safety", CheckStatus.PASS, "LAN mode", detail)


def _network_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Classify configured bind exposure using the canonical loopback policy."""
    server = cfg.server
    if server.lan:
        return _lan_network_check(cfg)
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


def _stream_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check stream-mode prerequisites.

    Args:
        cfg: Loaded application configuration.

    Returns:
        A DoctorCheck describing stream-mode readiness.
    """
    if not cfg.stream.enabled:
        return DoctorCheck("stream", CheckStatus.PASS, "stream mode off")
    if shutil.which("ffmpeg") is None:
        return DoctorCheck("stream", CheckStatus.FAIL, "FFmpeg missing; stream mode cannot start")
    if is_loopback_bind(cfg.server.host) and not cfg.server.lan:
        return DoctorCheck(
            "stream",
            CheckStatus.WARN,
            "stream mode on, but the server only listens on this machine",
            "Speakers on your network cannot reach it. Start with `autodj serve --lan`.",
        )
    return DoctorCheck("stream", CheckStatus.PASS, f"stream mode on at {cfg.stream.bitrate} kbps")


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
            _dj_meta_database_check(cfg),
            _dependency_check(),
            _model_cache_check(cfg),
            _network_check(cfg),
            _stream_check(cfg),
        )
    )
