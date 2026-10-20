"""Installation and runtime diagnostics for AutoDJ.

The checks repair nothing and write no data.

With several stations (``[stations.<slug>]``), the checks that belong to
one station run once more for each other station, read from its own
configuration and its own folder, and carry its slug in
:attr:`DoctorCheck.station`.  The default station's checks keep their
names and carry no slug.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import sys
import tomllib
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.config import is_loopback_bind
from autodj.index_manifest import has_legacy_index
from autodj.repeat_settings import removed_saved_repeat_keys
from autodj.sqlite_utils import readonly_uri

_connect = sqlite3.connect

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig, IndexConfig, ModelConfig, RelayConfig, StationSpec
    from autodj.model import ModelCacheStatus
    from autodj.stations import StationPaths


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
    """One stable, render-independent diagnostic result.

    ``station`` is the slug of the station other than the default one that
    the check is about, or ``None`` for the default station and the server.
    """

    name: str
    status: CheckStatus
    summary: str
    detail: str | dict[str, Any] = ""
    repair: str | None = None
    station: str | None = None


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
        checks = []
        for check in self.checks:
            item = asdict(check)
            for key in ("repair", "station"):
                if item[key] is None:
                    del item[key]
            checks.append(item)
        return {"exit_code": self.exit_code, "checks": checks}

    def to_json(self) -> str:
        """Serialize the report without exposing configuration secrets."""
        return json.dumps(self.to_dict(), indent=2, default=str)


def render_text(report: DoctorReport) -> str:
    """Render one screen-reader-friendly line per check."""
    lines: list[str] = []
    for check in report.checks:
        where = f"Station {check.station}, " if check.station else ""
        line = f"[{check.status.upper()}] {where}{check.name}: {check.summary}"
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
        "music_dir": str(cfg.library.music_dir) if cfg.library.music_dir is not None else None,
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


def _beets_db_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check that a configured beets database file exists."""
    path = cfg.library.beets_db
    if path is None:
        return DoctorCheck("beets-db", CheckStatus.PASS, "not configured; indexing scans the files")
    if path.is_file():
        return DoctorCheck("beets-db", CheckStatus.PASS, str(path))
    return DoctorCheck(
        "beets-db",
        CheckStatus.WARN,
        str(path),
        "not found; indexing scans the files instead and enrich fails. "
        "Fix [library] beets_db or set it to an empty string.",
    )


def _beets_paths_check(cfg: AutoDJConfig) -> list[DoctorCheck]:
    """Check that the beets database's songs are songs in the library.

    Beets' relative paths are joined to ``[library] music_dir``; when that
    names the wrong folder, no beets item matches a library song and every
    beets query category is empty (see :mod:`autodj.beets_paths`).  Both
    databases are opened read-only.  No check without a beets database
    file (``beets-db`` reports a missing one).
    """
    from autodj.beets import read_items
    from autodj.beets_paths import match

    path = cfg.library.beets_db
    if path is None or not path.is_file():
        return []
    try:
        songs = [song.path for song in _catalog_songs(cfg)]
        rows = [(str(item), values) for item, values in read_items(path, ())]
    except (OSError, sqlite3.Error) as exc:
        return [
            DoctorCheck(
                "beets-paths", CheckStatus.WARN, "the beets database could not be read", str(exc)
            )
        ]
    _, check = match(rows, songs, cfg.library.music_dir)
    detail = {
        "beets_items": check.items,
        "library_songs": check.library,
        "matched": check.matched,
        "relative_paths": check.relative,
        "music_dir": str(check.music_dir) if check.music_dir is not None else None,
        "found_folder": check.found.as_posix() if check.found is not None else None,
        "example": check.example,
    }
    if check.problem:
        return [DoctorCheck("beets-paths", CheckStatus.WARN, check.problem, detail, check.fix)]
    if check.note:
        # A music_dir naming a missing folder is wrong; an unset one is only new.
        status = CheckStatus.WARN if check.music_dir is not None else CheckStatus.PASS
        return [DoctorCheck("beets-paths", status, check.note, detail)]
    return [DoctorCheck("beets-paths", CheckStatus.PASS, check.summary, detail)]


def _music_paths_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check registered music folders; an empty library needs no configured root."""
    catalog_path = cfg.index.library_dir / "library.db"
    if not catalog_path.is_file():
        return DoctorCheck("music-path", CheckStatus.PASS, "no music folders registered")
    try:
        conn = _connect(readonly_uri(catalog_path), uri=True)
        with closing(conn):
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "catalog_sources" not in tables:
                return DoctorCheck("music-path", CheckStatus.PASS, "no music folders registered")
            roots = [
                Path(str(row[0]))
                for row in conn.execute(
                    "SELECT path FROM catalog_sources WHERE kind='music' ORDER BY path"
                )
            ]
    except (OSError, sqlite3.DatabaseError) as exc:
        return DoctorCheck(
            "music-path",
            CheckStatus.WARN,
            "registered folders not checked",
            f"The library catalog could not be read; see the index check ({exc}).",
        )
    if not roots:
        return DoctorCheck("music-path", CheckStatus.PASS, "no music folders registered")
    unavailable = [str(root) for root in roots if not root.is_dir() or not os.access(root, os.R_OK)]
    if unavailable:
        return DoctorCheck(
            "music-path",
            CheckStatus.FAIL,
            "registered music folders unavailable",
            {"unavailable": unavailable},
        )
    return DoctorCheck(
        "music-path",
        CheckStatus.PASS,
        f"{len(roots)} registered music folder(s) readable",
        {"registered": [str(root) for root in roots]},
    )


def _saved_settings_check(cfg: AutoDJConfig, paths: StationPaths | None = None) -> DoctorCheck:
    """Report obsolete saved settings and offer optional cleanup."""
    from autodj.stations import StationPaths

    path = (paths or StationPaths.default(cfg)).web_state
    if not path.exists():
        return DoctorCheck("saved-settings", CheckStatus.PASS, "no saved settings")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return DoctorCheck(
            "saved-settings", CheckStatus.WARN, "saved settings unreadable", str(exc)
        )
    if not isinstance(data, dict):
        return DoctorCheck(
            "saved-settings", CheckStatus.WARN, "saved settings root is not an object"
        )
    version = data.get("schema_version")
    if type(version) is int and version == 1:
        return DoctorCheck(
            "saved-settings",
            CheckStatus.FAIL,
            "saved settings need migration",
            f"{path}; run `autodj migrate-index --apply`",
            "migrate-index",
        )
    removed = removed_saved_repeat_keys(data)
    if removed:
        grouped = "; ".join(f"{group}: {', '.join(keys)}" for group, keys in removed.items())
        playback = data.get("playback", {})
        related_file = playback.get("related_artists_file") if isinstance(playback, dict) else None
        rules = playback.get("repeat_rules") if isinstance(playback, dict) else None
        if related_file in (None, "") and isinstance(rules, dict):
            related_file = rules.get("related_artists_file")
        preserve = (
            " Preserve the old related-artists data and resolve that path manually."
            if related_file not in (None, "")
            else ""
        )
        return DoctorCheck(
            "saved-settings",
            CheckStatus.WARN,
            "ignoring removed repeat keys in saved settings",
            f"{path}; {grouped}. Re-enter any old limits in Settings > Picking > Repeats.{preserve}",
            "repair-saved-settings",
        )
    if type(version) is not int or version != 2:
        return DoctorCheck(
            "saved-settings",
            CheckStatus.WARN,
            "unsupported saved settings schema",
            f"schema_version must be 2, got {version!r}; runtime ignores this file",
        )
    return DoctorCheck("saved-settings", CheckStatus.PASS, "saved settings valid", str(path))


def _index_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check the current catalog without reading old generation files."""
    index_dir = cfg.index.library_dir
    catalog_path = index_dir / "library.db"
    if catalog_path.is_file():
        from autodj.catalog import catalog_counts

        try:
            from autodj.index_migration import migration_required
            from autodj.stations import StationPaths

            needed, reason = migration_required(index_dir, (StationPaths.default(cfg).web_state,))
            if needed:
                return DoctorCheck(
                    "index", CheckStatus.FAIL, "library migration needed", reason, "migrate-index"
                )
            ready, analyzed = catalog_counts(catalog_path)
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            return DoctorCheck("index", CheckStatus.FAIL, "library cannot be read", str(exc))
        return DoctorCheck(
            "index",
            CheckStatus.PASS if ready else CheckStatus.WARN,
            f"{ready} songs; {analyzed} analyzed",
            str(catalog_path),
        )
    if index_dir.exists() and not index_dir.is_dir():
        return DoctorCheck(
            "index", CheckStatus.FAIL, "library path is not a directory", str(index_dir)
        )
    if has_legacy_index(index_dir):
        return DoctorCheck(
            "index",
            CheckStatus.FAIL,
            "library migration needed",
            f"{index_dir}; stop AutoDJ and run `autodj migrate-index --apply`.",
            "migrate-index",
        )
    return DoctorCheck(
        "index",
        CheckStatus.WARN,
        "library is empty",
        f"{index_dir}; add music folders in Library tools or run `autodj index`",
        "index",
    )


def _dj_meta_database_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check the active DJ metadata cache read-only, without creating it."""
    path = cfg.index.library_dir / "dj_meta.db"
    if not path.is_file():
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.WARN,
            "database absent",
            f"{path}; run `autodj analyse` when DJ metadata is needed",
            "analyse",
        )
    try:
        conn = _connect(readonly_uri(path), uri=True)
        with closing(conn):
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise sqlite3.DatabaseError("integrity_check: " + "; ".join(integrity))
            count = int(conn.execute("SELECT COUNT(*) FROM dj_meta").fetchone()[0])
    except OSError as exc:
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.FAIL,
            "cannot access database",
            f"{exc}; check file and directory permissions",
        )
    except sqlite3.DatabaseError as exc:
        if getattr(exc, "sqlite_errorcode", None) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_PERM,
            sqlite3.SQLITE_READONLY,
        }:
            return DoctorCheck(
                "dj-meta-db",
                CheckStatus.FAIL,
                "cannot access database",
                f"{exc}; stop other AutoDJ processes and check file and directory permissions",
            )
        return DoctorCheck(
            "dj-meta-db",
            CheckStatus.FAIL,
            "integrity check failed",
            f"{exc}; rebuild it with `autodj analyse`",
            "rebuild-dj-meta",
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
            "Install FFmpeg to index, analyse and mix .mp3, .m4a, .mp4 and .aac files, "
            "which are skipped without it, and to use stream mode. Optional ALAC browser transcoding is "
            "unavailable; raw ALAC fallback remains available."
        )
    return DoctorCheck(
        "dependencies",
        CheckStatus.WARN,
        "; ".join(issues) + ".",
        "Action: " + " ".join(details),
    )


def _model_cache_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Inspect the configured model cache without downloading or repairing it."""
    from autodj.model_identity import model_analysis_available

    if not model_analysis_available():
        return DoctorCheck(
            "model-cache",
            CheckStatus.WARN,
            "local model analysis unavailable",
            "Model analysis is disabled or its packages are not installed; "
            "run `autodj setup --backend cpu` to enable it. Imported songs remain available.",
        )
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
        "index",
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
    from autodj.secret_files import access_token_path

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
    if cfg.station.output != "stream":
        return DoctorCheck("stream", CheckStatus.PASS, "stream mode off")
    if shutil.which("ffmpeg") is None:
        return DoctorCheck("stream", CheckStatus.FAIL, "FFmpeg missing; stream mode cannot start")
    if is_loopback_bind(cfg.server.host) and not cfg.server.lan:
        return DoctorCheck(
            "stream",
            CheckStatus.WARN,
            "stream mode on, but the server only listens on this machine",
            "Speakers on your network cannot reach it. Start with `autodj serve --lan`.",
            "serve-lan",
        )
    return DoctorCheck("stream", CheckStatus.PASS, f"stream mode on at {cfg.stream.bitrate} kbps")


_RELAY_PROBE_TIMEOUT = 3.0


def _relay_check(
    relay: RelayConfig,
    last: Any,
    updated: str,
    connect: Callable[..., socket.socket],
) -> DoctorCheck:
    """Check that one relay's server answers, and show its last reported state.

    The probe only opens and closes a TCP connection to the port the relay
    logs in on; it sends no password and does not disturb a running relay.

    Args:
        relay: The relay's configuration.
        last: Its entry in the relay status file, if any.
        updated: When the status file was written.
        connect: ``socket.create_connection`` replacement for tests.
    """
    from autodj.relay import TYPE_LABELS, describe_error, describe_target, source_port

    name = f"relay-{relay.name}"
    where = f"{TYPE_LABELS[relay.type]} at {describe_target(relay)}"
    if not relay.enabled:
        return DoctorCheck(name, CheckStatus.PASS, f"{where}: off (enabled = false)")
    status = CheckStatus.PASS
    try:
        with closing(connect((relay.host, source_port(relay)), timeout=_RELAY_PROBE_TIMEOUT)):
            pass
        summary = f"{where}: the server answers"
    except OSError as exc:
        status = CheckStatus.WARN
        summary = f"{where}: cannot reach the server: {describe_error(exc)}"
    detail = ""
    if isinstance(last, dict) and isinstance(last.get("state"), str):
        detail = f"AutoDJ last reported it {last['state']} at {updated}"
        if last.get("error"):
            detail += f", last error: {last['error']}"
        if last.get("title_error"):
            detail += f", title updates failing: {last['title_error']}"
        if last["state"] == "retrying":
            status = CheckStatus.WARN
    return DoctorCheck(name, status, summary, detail)


def _relay_checks(
    cfg: AutoDJConfig,
    connect: Callable[..., socket.socket] | None = None,
    paths: StationPaths | None = None,
) -> tuple[DoctorCheck, ...]:
    """Return one check per configured relay (``[stream.relays.<name>]``)."""
    from autodj.relay_settings import load_relays
    from autodj.stations import StationPaths

    paths = paths or StationPaths.default(cfg)
    relays = load_relays(paths.relays, cfg.stream.relays, paths.slug)
    if not relays:
        return ()
    connect = connect or socket.create_connection
    from autodj.relay import read_status_file, relay_status_path

    report = read_status_file(relay_status_path(cfg, paths)) or {"updated": "", "relays": {}}
    return tuple(
        _relay_check(relay, report["relays"].get(relay.name), report["updated"], connect)
        for relay in relays.values()
    )


def _related_tracks_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Read the related tracks file the way the player will at startup."""
    from autodj.related_artist_file import DEFAULT_RELATED_TRACKS_FILE
    from autodj.repeat_rules import load_related_tracks

    path = cfg.index.library_dir / DEFAULT_RELATED_TRACKS_FILE
    if not path.exists():
        return DoctorCheck(
            "related-tracks",
            CheckStatus.PASS,
            f"Sample will be copied to {path} when AutoDJ starts",
        )
    try:
        related = load_related_tracks(path)
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            "related-tracks",
            CheckStatus.WARN,
            "related tracks file not usable; AutoDJ starts without it",
            f"{exc}. Fix {path} and save Settings > Picking > Repeats again.",
        )
    return DoctorCheck(
        "related-tracks",
        CheckStatus.PASS,
        f"{len(related.links)} tracks with related tracks from {path}",
    )


def _related_artists_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Read the related artists file the way the player will at startup."""
    from autodj.related_artist_file import DEFAULT_RELATED_ARTISTS_FILE
    from autodj.repeat_rules import load_related_artists

    path = cfg.index.library_dir / DEFAULT_RELATED_ARTISTS_FILE
    if not path.exists():
        return DoctorCheck(
            "related-artists",
            CheckStatus.PASS,
            f"Sample will be copied to {path} when AutoDJ starts",
        )
    try:
        related = load_related_artists(path)
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            "related-artists",
            CheckStatus.WARN,
            "related artists file not usable; AutoDJ starts without it",
            f"{exc}. Fix {path} and save Settings > Picking > Repeats again.",
        )
    return DoctorCheck(
        "related-artists",
        CheckStatus.PASS,
        f"{len(related.links)} artists with related artists from {path}",
    )


def run_doctor(
    cfg: AutoDJConfig,
    *,
    python_version: tuple[int, int] | None = None,
    station: str | None = None,
) -> DoctorReport:
    """Run all checks in stable order without repairing anything.

    Args:
        cfg: The loaded configuration.
        python_version: Stands in for the running Python's, in tests.
        station: Only this station's own checks (``"default"`` for the
            default station's); every station's when omitted.  The
            server-wide checks always run.
    """
    from autodj.stations import DEFAULT_SLUG

    default = station in (None, DEFAULT_SLUG)
    checks: list[DoctorCheck] = [_configuration_check(cfg), _speech_check(cfg)]
    if default:
        checks.append(_dmca_check(cfg))
    checks += [
        _python_check(python_version),
        _music_paths_check(cfg),
        _beets_db_check(cfg),
        *_beets_paths_check(cfg),
        _path_check("index-path", cfg.index.index_dir, writable=True),
        _path_check("model-path", cfg.index.model_dir, writable=True),
        _index_check(cfg),
    ]
    if default:
        checks.append(_saved_settings_check(cfg))
    checks += [
        _dj_meta_database_check(cfg),
        _dependency_check(),
        _model_cache_check(cfg),
        _network_check(cfg),
    ]
    if default:
        checks += [_stream_check(cfg), *_relay_checks(cfg)]
    checks += [_related_artists_check(cfg), _related_tracks_check(cfg)]
    if default:
        checks += [_time_checks_check(cfg), *_voice_pack_checks(cfg)]
    checks += [_categories_check(cfg), _config_keys_check(cfg), *_restore_check(cfg)]
    if default:
        checks += [_recording_check(cfg), _liners_check(cfg)]
    for spec in cfg.stations:
        if station in (None, spec.slug):
            checks += _station_checks(cfg, spec)
    checks += _station_folders_check(cfg)
    checks += [
        DoctorCheck("station-definition", CheckStatus.WARN, f"{path}: {reason}")
        for path, reason in cfg.skipped_definitions
    ]
    return DoctorReport(tuple(checks))


def _restore_check(cfg: AutoDJConfig) -> tuple[DoctorCheck, ...]:
    """Report what an interrupted or untidy restore left; nothing when there is nothing."""
    from autodj.backup import restore_leftovers

    try:
        found = restore_leftovers(cfg)
    except (OSError, ValueError) as exc:
        return (
            DoctorCheck("restore", CheckStatus.WARN, "cannot look for restore leftovers", str(exc)),
        )
    if not found:
        return ()
    strays = [str(path) for path in found.strays]
    detail: dict[str, Any] = {
        "journal": str(found.journal) if found.journal else None,
        "strays": strays,
    }
    if found.state in ("installing", "unreadable"):
        return (
            DoctorCheck(
                "restore",
                CheckStatus.FAIL,
                "a restore did not finish: some files are new and some old",
                detail,
                "recover-restore",
            ),
        )
    summary = (
        "a restore finished but left old copies"
        if found.state == "installed"
        else f"{len(strays)} hidden {'copy' if len(strays) == 1 else 'copies'} left by an earlier restore"
    )
    return (DoctorCheck("restore", CheckStatus.WARN, summary, detail, "recover-restore"),)


def _table(paths: StationPaths | None, section: str) -> str:
    """The TOML table a station's *section* is set in: ``[playback]`` or ``[stations.x.playback]``."""
    if paths is None or paths.is_default:
        return f"[{section}]"
    return f"[stations.{paths.slug}.{section}]"


def _station_checks(cfg: AutoDJConfig, spec: StationSpec) -> list[DoctorCheck]:
    """The checks of one station besides the default one, from its own configuration."""
    from autodj.config import station_config
    from autodj.stations import StationPaths

    own = station_config(spec.cfg, cfg)
    paths = StationPaths.for_station(own, spec.slug)
    checks = [
        _station_folder_check(paths),
        _saved_settings_check(own, paths),
        _dmca_check(own, paths),
        _stream_check(own),
        *_relay_checks(own, paths=paths),
        _time_checks_check(own, paths),
        *_voice_pack_checks(own, paths),
        _categories_check(own, paths),
        _recording_check(own, paths),
        _liners_check(own, paths),
    ]
    return [replace(check, station=spec.slug) for check in checks]


def _station_folder_check(paths: StationPaths) -> DoctorCheck:
    """Check that another station's own folder exists and can be written, or can be made."""
    folder = paths.state_dir
    if folder.exists():
        if folder.is_dir() and os.access(folder, os.R_OK | os.W_OK):
            return DoctorCheck("station-folder", CheckStatus.PASS, str(folder))
        return DoctorCheck(
            "station-folder",
            CheckStatus.FAIL,
            str(folder),
            "not a folder AutoDJ can read and write; the station cannot keep its settings",
        )
    parent = _nearest_existing_parent(folder)
    if parent is not None and parent.is_dir() and os.access(parent, os.W_OK):
        return DoctorCheck(
            "station-folder",
            CheckStatus.WARN,
            str(folder),
            "not made yet; the station makes it when it first starts, or doctor can make it now",
            "create-station-folder",
        )
    return DoctorCheck(
        "station-folder",
        CheckStatus.FAIL,
        str(folder),
        f"missing, and {parent or 'no parent folder'} cannot be written",
    )


def _recording_check(cfg: AutoDJConfig, paths: StationPaths | None = None) -> DoctorCheck:
    """Check that the station's recording folder can be written, or made when needed.

    The configuration already refuses two stations with one recording
    folder, so only the folder itself is checked.
    """
    folder = cfg.recording.folder
    table = _table(paths, "recording")
    if folder.exists():
        if folder.is_dir() and os.access(folder, os.W_OK):
            return DoctorCheck("recording", CheckStatus.PASS, str(folder))
        return DoctorCheck(
            "recording",
            CheckStatus.WARN,
            str(folder),
            f"not a folder AutoDJ can write; a recording cannot start. Fix {table} folder.",
        )
    parent = _nearest_existing_parent(folder.absolute())
    if parent is not None and parent.is_dir() and os.access(parent, os.W_OK):
        return DoctorCheck(
            "recording", CheckStatus.PASS, str(folder), "made when the first recording starts"
        )
    return DoctorCheck(
        "recording",
        CheckStatus.WARN,
        str(folder),
        f"missing, and it cannot be made; a recording cannot start. Fix {table} folder.",
    )


def _liners_check(cfg: AutoDJConfig, paths: StationPaths | None = None) -> DoctorCheck:
    """Check the station's own liner folder, where uploads go."""
    from autodj.stations import StationPaths

    folder = (paths or StationPaths.default(cfg)).liners
    if folder.is_dir():
        return DoctorCheck("liners", CheckStatus.PASS, str(folder))
    if folder.exists():
        return DoctorCheck(
            "liners", CheckStatus.WARN, str(folder), "not a folder; move the file away"
        )
    return DoctorCheck(
        "liners", CheckStatus.PASS, "no liners yet", f"put the station's liner clips in {folder}"
    )


def _config_keys_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Name the keys and tables the configuration files have but AutoDJ ignores.

    Loading only logs them; this lists them by file, including those under
    ``[stations.<slug>]``.  The files are read, never changed.
    """
    from autodj.config import (
        STATION_SECTION_TYPES,
        unknown_config_keys,
        unknown_config_sections,
    )

    found: dict[str, list[str]] = {}
    for source in cfg.config_sources:
        path = Path(source)
        if path.suffix != ".toml" or not path.is_file():
            continue
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue  # loading has already read it; a race is not worth a report
        names = [
            f"{table}.{key}" for table, keys in unknown_config_keys(raw).items() for key in keys
        ]
        legacy_station_sections = {section.SECTION for section in STATION_SECTION_TYPES}
        names += [
            f"[{table}]"
            for table in unknown_config_sections(raw)
            if table not in legacy_station_sections
        ]
        if names:
            found[str(path)] = names
    if not found:
        return DoctorCheck("config-keys", CheckStatus.PASS, "no unknown keys")
    count = sum(len(names) for names in found.values())
    return DoctorCheck(
        "config-keys",
        CheckStatus.WARN,
        f"{count} unknown {'key or table is' if count == 1 else 'keys and tables are'} ignored",
        {
            **found,
            "action": "Fix a misspelled name, or remove it. In a terminal, doctor offers to "
            "remove unknown keys, with a backup of each file; remove unknown tables by hand.",
        },
    )


def _station_folders_check(cfg: AutoDJConfig) -> tuple[DoctorCheck, ...]:
    """Name station folders that no ``[stations.<slug>]`` table uses any more.

    They are never deleted: their settings, schedule and carts may still be
    wanted.  No check is made when there is no ``stations`` folder.
    """
    from autodj.stations import DEFAULT_SLUG, StationPaths

    folder = StationPaths.default(cfg).stations_dir
    if not folder.is_dir():
        return ()
    configured = {DEFAULT_SLUG, *(spec.slug for spec in cfg.stations)}
    try:
        unused = sorted(
            child.name
            for child in folder.iterdir()
            if child.is_dir() and not child.name.startswith(".") and child.name not in configured
        )
    except OSError as exc:
        return (DoctorCheck("station-folders", CheckStatus.WARN, "cannot list", str(exc)),)
    if not unused:
        return (DoctorCheck("station-folders", CheckStatus.PASS, "every folder is configured"),)
    return (
        DoctorCheck(
            "station-folders",
            CheckStatus.WARN,
            f"not in the configuration: {', '.join(unused)}",
            f"{folder}. These stations' settings, schedules and carts are not used and not "
            "backed up. Add their [stations.<slug>] tables back to use them, or move the "
            "folders somewhere safe; AutoDJ never deletes them.",
        ),
    )


@dataclass(frozen=True)
class _CatalogSong:
    """One music entry of the catalog, read without opening it for writing."""

    path: str
    title: str
    artist: str
    album: str
    genre: str
    year: int
    bpm: float
    length: float


def _catalog_songs(cfg: AutoDJConfig) -> list[_CatalogSong]:
    """The catalog's music entries, read-only (an empty list without a catalog)."""
    path = cfg.index.library_dir / "library.db"
    if not path.is_file():
        return []
    with closing(_connect(readonly_uri(path), uri=True)) as db:
        rows = db.execute(
            "SELECT path, title, artist, album, genre, year, bpm, length "
            "FROM catalog_entries WHERE kind='music' ORDER BY path"
        ).fetchall()
    return [_CatalogSong(*row) for row in rows]


def _categories_check(cfg: AutoDJConfig, paths: StationPaths | None = None) -> DoctorCheck:
    """Name the rotation categories with no songs, missing playlist tracks or errors.

    The categories file is read, never set aside, and the catalog and the
    beets database are opened read-only.  Songs placed by ``year`` because
    beets has no first-release year are counted in the detail.
    """
    from autodj.categories import (
        CategoriesFile,
        beets_source,
        count_text,
        missing_text,
        playlists_folder,
        resolve_checked,
        store_path,
        uses_beets,
    )

    path = store_path(cfg, paths)
    if not path.exists():
        return DoctorCheck("categories", CheckStatus.PASS, "no categories saved")
    try:
        categories = CategoriesFile.model_validate_json(path.read_bytes()).categories
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            "categories",
            CheckStatus.WARN,
            "the categories file cannot be read; AutoDJ sets it aside when it starts",
            str(exc).splitlines()[0],
            f"Fix or remove {path}, then add the categories again in Library tools, Categories.",
        )
    if not categories:
        return DoctorCheck("categories", CheckStatus.PASS, "no categories saved")
    try:
        songs = _catalog_songs(cfg)
    except sqlite3.Error as exc:
        return DoctorCheck(
            "categories", CheckStatus.WARN, "the library could not be read", str(exc)
        )
    results, check = resolve_checked(
        categories,
        songs,
        beets=beets_source(cfg),
        music_dir=cfg.library.music_dir,
        folder=playlists_folder(cfg),
        remember_check=False,
    )
    cause = check.cause if check is not None else None
    problems: list[str] = []
    detail: dict[str, Any] = {}
    for category, result in zip(categories, results, strict=True):
        detail[category.name] = {
            "songs": result.count,
            "missing": result.missing_count,
            "missing_examples": list(result.missing),
            "release_year_fallbacks": result.fallback,
            "error": result.error,
        }
        if result.error:
            problems.append(f"{category.name}: {result.error}")
        elif not result.count and cause and uses_beets(category):
            problems.append(f"{category.name}: no songs match ({cause})")
        elif not result.count:
            problems.append(f"{category.name}: no songs match")
        if not result.error and result.missing_count:
            listed = "; ".join(result.missing[:5])
            more = result.missing_count - min(5, len(result.missing))
            listed += f"; and {more} more" if more > 0 else ""
            problems.append(f"{category.name}: {missing_text(result.missing_count)}: {listed}")
    total = sum(result.count for result in results if not result.error)
    saved = f"{len(categories)} saved, {count_text(total)} in all"
    if not problems:
        return DoctorCheck("categories", CheckStatus.PASS, saved, detail)
    fix = (
        "Fix the query or the playlist in Library tools, Categories, or the script that writes "
        "the playlist. A clockwheel step with an empty category is skipped."
    )
    if check is not None and check.fix:
        fix = f"{check.fix} {fix}"
    return DoctorCheck(
        "categories", CheckStatus.WARN, f"{saved}; " + "; ".join(problems), detail, fix
    )


def _time_checks_check(cfg: AutoDJConfig, paths: StationPaths | None = None) -> DoctorCheck:
    """Report recorded minute coverage, the clips and TTS program found, and the switches."""
    from autodj.time_checks import (
        clip_sources,
        clips_folder,
        found_packs,
        load_settings,
        missing_minutes,
        state_path,
    )
    from autodj.voice_packs import pieces_for, scan_pack

    tc = cfg.time_checks
    folder = clips_folder(cfg, paths)
    detail = {"folder": str(folder), "missing_examples": missing_minutes(folder)[:10]}
    try:
        settings = load_settings(cfg, paths)
        enabled = settings.enabled
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        return DoctorCheck(
            "time-checks",
            CheckStatus.FAIL,
            f"saved time check settings cannot be read: {exc}",
            detail,
            f"Fix or remove {state_path(cfg, paths)}, then set the switches again.",
        )
    recorded, intro, outro = clip_sources(cfg, settings, paths)
    missing = missing_minutes(recorded)
    detail = {"folder": str(recorded), "missing_examples": missing[:10]}
    coverage = f"{len(missing)} of 1440 minute clips missing"
    if not enabled:
        return DoctorCheck("time-checks", CheckStatus.PASS, f"disabled; {coverage}", detail)
    from autodj.speech import load_state
    from autodj.stations import StationPaths

    try:
        speech = load_state((paths or StationPaths.default(cfg)).speech)
    except (OSError, ValueError, KeyError, TypeError):
        return DoctorCheck("time-checks", CheckStatus.FAIL, "Saved speech state could not be read")
    if speech.get("active_set"):
        return _speech_check(cfg, paths, name="time-checks")
    problems = []
    if tc.clips_folder and not folder.is_dir():
        problems.append("clips_folder is not a folder")
    if settings.recorded_folder and not recorded.is_dir():
        problems.append(f"recorded clips folder {settings.recorded_folder} not found")
    for label, clip, chosen in (("intro", intro, settings.intro), ("outro", outro, settings.outro)):
        if clip is not None and not clip.is_file():
            problems.append(
                f"{label} clip {chosen} not found" if chosen else f"{label}_clip not found"
            )
    if tc.tts_command and shutil.which(tc.tts_command[0]) is None:
        problems.append(f"TTS program {tc.tts_command[0]} not found")
    found = found_packs(cfg, paths, skip=settings.recorded_folder)
    folders = {name: pack.path for name, pack in found.packs.items()}
    problems.extend(
        f"saved voice pack {name} is not found"
        for name in settings.enabled_packs
        if name not in folders
    )
    problems.extend(found.clashes)
    packs = [scan_pack(folders[name]) for name in settings.enabled_packs if name in folders]
    unvoiced = [
        name
        for name in missing
        if not any(
            pieces_for(pack, int(name[:2]), int(name[3:]), settings.hour_format) for pack in packs
        )
    ]
    if unvoiced and not tc.tts_command:
        problems.append("no TTS fallback")
    fallback = "TTS configured" if tc.tts_command else ""
    summary = "; ".join(part for part in (coverage, fallback, *problems) if part)
    return DoctorCheck(
        "time-checks", CheckStatus.WARN if problems else CheckStatus.PASS, summary, detail
    )


def _speech_check(
    cfg: AutoDJConfig, paths: StationPaths | None = None, *, name: str = "speech"
) -> DoctorCheck:
    """Inspect local speech state without provider requests, repairs or heavy imports."""
    from autodj.speech import backup_files, load_state
    from autodj.speech_providers import ProviderStore
    from autodj.stations import StationPaths

    try:
        providers = ProviderStore(Path(cfg.index.index_dir) / "speech_providers.json").public()
        root = (paths or StationPaths.default(cfg)).speech
        data = load_state(root)
        missing = sum(not path.is_file() for path in backup_files(root))
        if missing:
            return DoctorCheck(name, CheckStatus.WARN, f"{missing} saved speech files missing")
        if data.get("active_set"):
            item = data["sets"].get(data["active_set"], {})
            if item.get("state") != "complete" or len(item.get("minutes", {})) != 1440:
                return DoctorCheck(
                    name, CheckStatus.FAIL, "Active generated time-check set is incomplete"
                )
            return DoctorCheck(
                name, CheckStatus.PASS, "Generated time checks use local audio; no live TTS"
            )
        return DoctorCheck(
            name,
            CheckStatus.PASS,
            f"{len(providers)} speech connections; no generated time-check set active",
            "Configured connections are not proof of live service readiness.",
        )
    except (ValueError, OSError, KeyError, TypeError):
        return DoctorCheck(
            name, CheckStatus.FAIL, "Speech configuration or saved state could not be read"
        )


def _voice_pack_checks(
    cfg: AutoDJConfig, paths: StationPaths | None = None
) -> tuple[DoctorCheck, ...]:
    """One check per voice pack, listed or found: its coverage and the files it does not use."""
    from autodj.time_checks import found_packs, load_settings
    from autodj.voice_packs import coverage, scan_pack

    try:
        settings = load_settings(cfg, paths)
        hour_format, enabled = settings.hour_format, set(settings.enabled_packs)
        skip = settings.recorded_folder
    except (OSError, ValueError, KeyError, AttributeError):
        hour_format, enabled, skip = cfg.time_checks.hour_format, None, ""
    folders = {name: pack.path for name, pack in found_packs(cfg, paths, skip=skip).packs.items()}
    if enabled is None:
        enabled = set(folders)
    checks = []
    for name, folder in folders.items():
        pack = scan_pack(folder)
        covered = coverage(pack, hour_format)
        complete = pack.found and not pack.problems and covered.minutes_covered == 1440
        state = "enabled" if name in enabled else "not enabled"
        checks.append(
            DoctorCheck(
                f"voice-pack-{name}",
                CheckStatus.PASS if complete else CheckStatus.WARN,
                f"{state}; {covered.describe(pack)}",
                str(folder),
            )
        )
    return tuple(checks)


def _dmca_check(cfg: AutoDJConfig, paths: StationPaths | None = None) -> DoctorCheck:
    """Name any setting that conflicts with DMCA mode, and how to fix it."""
    if not cfg.dmca.enabled:
        return DoctorCheck("dmca", CheckStatus.PASS, "off")
    if cfg.nowplaying.show_next:
        return DoctorCheck(
            "dmca",
            CheckStatus.WARN,
            "on, with a conflicting setting",
            f"{_table(paths, 'nowplaying')} show_next = true asks to announce the next song, "
            "which DMCA mode "
            "never does. Set show_next = false so the setting matches what listeners see.",
        )
    return DoctorCheck("dmca", CheckStatus.PASS, "on", "no conflicting settings")
