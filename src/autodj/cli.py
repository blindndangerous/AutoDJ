"""AutoDJ command-line interface.

Entry point for all AutoDJ commands.  Install the package with ``uv sync``
and then run:

.. code-block:: bash

    uv run autodj index          # build or update the music library index
    uv run autodj serve          # start the auto-DJ and its web page

See each command's ``--help`` for full option documentation.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Force UTF-8 output on Windows (default terminal encoding is cp1252 which
# cannot print Unicode box-drawing characters or em-dashes used in track names).
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Reduce CUDA memory fragmentation during long indexing runs.
# Must be set before torch is imported — cli.py is the earliest entry point.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from autodj.machine_tokens import KNOWN_SCOPES
from autodj.secret_files import paired_devices_path

if TYPE_CHECKING:
    from autodj.beets import Track
    from autodj.config import AutoDJConfig, ServerConfig
    from autodj.doctor import DoctorReport
    from autodj.indexer import IndexEntry
    from autodj.library import LibraryManager
    from autodj.machine_tokens import MachineTokenStore
    from autodj.pairing import DeviceRegistry
    from autodj.similarity import SimilarityIndex

console = Console()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_seed(
    sim: SimilarityIndex | LibraryManager,
    cfg: AutoDJConfig,
    seed: str | None,
    console_: Console,
) -> IndexEntry | None:
    """Resolve a seed string to an :class:`~autodj.indexer.IndexEntry`.

    When several library tracks match, the first one is used.

    Args:
        sim: Loaded :class:`~autodj.similarity.SimilarityIndex`.
        cfg: Full :class:`~autodj.config.AutoDJConfig`.
        seed: User-supplied search term, or ``None`` for no seed.
        console_: Rich console for printing messages.

    Returns:
        The chosen :class:`~autodj.indexer.IndexEntry`, or ``None`` if no
        seed was specified or no match was found.
    """
    if seed is None:
        return None
    similarity = sim
    entries = similarity.entries_snapshot()
    candidates: list[Track | IndexEntry] = []

    if cfg.library.beets_db and cfg.library.beets_db.exists():
        from autodj.beets import search_tracks as _search

        beets_results = _search(cfg.library.beets_db, seed)
        indexed_paths = {e.path for e in entries}
        candidates = [t for t in beets_results if str(t.path) in indexed_paths]

    if not candidates:
        q = seed.lower()
        candidates = [e for e in entries if q in e.title.lower() or q in e.artist.lower()]

    if not candidates:
        console_.print(f"[yellow]No library tracks match '{seed}'. Starting random.[/yellow]")
        return None

    chosen = candidates[0]
    display = getattr(chosen, "display_name", str(chosen))
    console_.print(f"Seed: [bold]{display}[/bold]")
    path_str = str(getattr(chosen, "path", chosen))
    return next((e for e in entries if e.path == path_str), None)


def _analyse_catalog_dj_meta(
    cfg: AutoDJConfig, catalog: Any, entries: list[IndexEntry], *, limit: int | None = None
) -> None:
    """Backfill CPU DJ metadata, placed from each song's saved Intro and Outro."""
    from autodj.indexer import backfill_dj_meta

    backfill_dj_meta(cfg, entries, limit=limit, prune=False, points_for=catalog.points)


def _load_cfg_or_exit(
    config_path: str | None,
    *,
    show_error: bool = True,
    log_file: bool = False,
) -> AutoDJConfig:  # pragma: no cover
    """Load *config_path* or print + exit on missing file.

    An index folder in the old layout is moved into the new one first
    (:mod:`autodj.layout`), then the configuration is read again.  With
    *log_file* the server's log file opens here, and what loading and the
    move logged is written to it too.
    """
    from logging.handlers import MemoryHandler

    from autodj.config import load_config, move_station_tables
    from autodj.layout import LayoutError, move_old_layout

    # Held until the log file is open: it lives in the index folder the move may change.
    early = MemoryHandler(capacity=10_000, flushLevel=logging.CRITICAL + 1) if log_file else None
    if early is not None:
        logging.getLogger().addHandler(early)
    try:
        cfg = load_config(config_path)
        if move_old_layout(cfg):
            cfg = load_config(config_path)
        if move_station_tables(cfg):
            cfg = load_config(config_path)
        from autodj.stereo import configure_spill_dir

        configure_spill_dir(cfg.index.index_dir)
        if early is not None and isinstance(cfg.index.index_dir, Path):
            handler = _add_log_file(cfg.index.index_dir / "logs" / "autodj.log")
            if handler is not None:
                early.setTarget(handler)
                early.flush()
        return cfg
    except LayoutError as exc:
        console.print(f"[bold red]{escape(str(exc))}[/]")
        raise click.exceptions.Exit(1) from exc
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if show_error:
            console.print(f"[bold red]Config not found or invalid:[/] {escape(str(exc))}")
        raise click.exceptions.Exit(1) from exc
    finally:
        if early is not None:
            logging.getLogger().removeHandler(early)
            early.close()


def _append_cli_source(cfg: AutoDJConfig) -> None:
    """Record the CLI as the latest configuration source."""
    if not cfg.config_sources or cfg.config_sources[-1] != "cli":
        cfg.config_sources = (*cfg.config_sources, "cli")


def _load_index_for_serve(cfg: AutoDJConfig) -> SimilarityIndex:  # pragma: no cover
    """Start with an empty similarity view; LibraryManager loads catalog vectors."""
    from autodj.config import station_config
    from autodj.library import require_current_library
    from autodj.similarity import SimilarityIndex
    from autodj.stations import StationPaths

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
    return SimilarityIndex.empty()


def _can_import(name: str) -> bool:
    """Return True when *name* imports cleanly."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _require_model_analysis() -> None:
    """Reject unavailable local model analysis before model imports or downloads."""
    from autodj.model_identity import model_analysis_available

    if not model_analysis_available():
        raise click.ClickException(
            "Local model analysis is disabled or its packages are not installed. "
            "Run `autodj setup --backend cpu` to enable it. Imported songs remain available."
        )


def _configured_allowed_hosts(
    cfg: AutoDJConfig, allowed_hosts: tuple[str, ...]
) -> list[str] | None:
    """Return the allowed hosts before LAN detection: ``--allowed-host`` values, else config.

    Args:
        cfg: Loaded configuration.
        allowed_hosts: ``--allowed-host`` values; empty keeps the config list.
    """
    return list(allowed_hosts) if allowed_hosts else cfg.server.allowed_hosts


def _stage_serve_server(
    cfg: AutoDJConfig,
    *,
    host: str | None,
    port: int | None,
    access_token: str | None,
    insecure_lan: bool | None,
    allowed_hosts: tuple[str, ...],
    allowed_origins: tuple[str, ...],
    lan: bool | None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> ServerConfig:
    """Return the server settings ``serve`` will use, after CLI overrides and ``--lan``.

    CLI values replace config values; with LAN mode on, detected hosts and
    their origins are added and the saved access token is used when no token
    is configured.  A non-loopback host with no allowlists, token or
    ``--insecure-lan`` turns LAN mode on, so ``serve --host 192.168.1.20
    --port 9000`` serves there with pairing.  The result has passed :func:`validate_server_exposure`.

    Args:
        cfg: Loaded configuration (not modified).
        host: ``--host``, or ``None``.
        port: ``--port``, or ``None``.
        access_token: ``--access-token``, or ``None``.
        insecure_lan: ``--insecure-lan``, or ``None``.
        allowed_hosts: ``--allowed-host`` values; empty keeps the config list.
        allowed_origins: ``--allowed-origin`` values; empty keeps the config list.
        lan: ``--lan``, or ``None`` to use ``[server] lan``.
        ssl_certfile: ``--ssl-certfile``, or ``None``.
        ssl_keyfile: ``--ssl-keyfile``, or ``None``.  Giving either TLS option
            replaces both ``[server] ssl_certfile`` and ``ssl_keyfile``.

    Raises:
        click.ClickException: The settings are invalid or unsafe, or the saved
            access token cannot be read or written.
    """
    from dataclasses import replace

    from autodj.config import is_loopback_bind, validate_server_exposure
    from autodj.lan import AccessTokenError, lan_server_config
    from autodj.secret_files import access_token_path

    server = cfg.server
    cli_tls = ssl_certfile is not None or ssl_keyfile is not None
    try:
        staged = replace(
            server,
            host=server.host if host is None else host,
            port=server.port if port is None else port,
            access_token=server.access_token if access_token is None else access_token,
            insecure_lan=server.insecure_lan if insecure_lan is None else insecure_lan,
            lan=server.lan if lan is None else lan,
            allowed_hosts=_configured_allowed_hosts(cfg, allowed_hosts),
            allowed_origins=(
                server.allowed_origins if not allowed_origins else list(allowed_origins)
            ),
            ssl_certfile=ssl_certfile if cli_tls else server.ssl_certfile,
            ssl_keyfile=ssl_keyfile if cli_tls else server.ssl_keyfile,
        )
        # A network host with nothing else set would be refused for lack of a
        # token; serve it in LAN mode at that address instead: detected names
        # and origins for the port, a saved token, pairing.
        if (
            not staged.lan
            and not is_loopback_bind(staged.host)
            and not staged.allowed_hosts
            and not staged.allowed_origins
            and not staged.access_token
            and not staged.insecure_lan
        ):
            staged = replace(staged, lan=True)
        if staged.lan:
            staged = lan_server_config(
                staged, tls=staged.ssl_certfile is not None, token_path=access_token_path(cfg)
            )
        validate_server_exposure(staged)
    except AccessTokenError as exc:
        raise click.ClickException(f"LAN mode cannot start: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    return staged


def _print_serve_banner(
    console_: Console, library: LibraryManager
) -> None:  # pragma: no cover -- terminal banner
    """Print the current library counts."""
    state = library.snapshot()
    console_.print(
        Panel(
            f"[bold green]AutoDJ[/] - {state['tracks_ready']} songs; "
            f"{state['tracks_indexed']} analyzed",
            expand=False,
        )
    )


def _serve_url(server: ServerConfig) -> str:
    """Return the address a browser on this machine should open.

    A wildcard bind address is not one the Host allowlist accepts, so the URL
    names ``localhost`` when it is allowed, otherwise the first allowed host.
    """
    scheme = "https" if server.ssl_certfile is not None else "http"
    name = server.host
    if name in ("0.0.0.0", "::"):  # nosec B104 -- comparison only
        allowed = server.effective_allowed_hosts()
        name = "localhost" if "localhost" in allowed else next(iter(allowed), name)
    if ":" in name:
        name = f"[{name}]"
    return f"{scheme}://{name}:{server.port}"


def _print_serve_url_banner(
    console_: Console, server: ServerConfig
) -> str:  # pragma: no cover -- terminal banner
    """Print the web-UI URL + reachability hint; return the URL."""
    from autodj.config import is_loopback_bind

    url = _serve_url(server)
    console_.print(f"  Web UI  : [link={url}]{escape(url)}[/link]")
    if server.ssl_certfile is not None:
        console_.print(
            "  [dim](TLS active — AudioWorklet effects work on remote hosts.  "
            "Trust the certificate's CA on every listening device.)[/]",
        )
    if is_loopback_bind(server.host):
        console_.print(
            "  [dim](Reachable from this machine only.  "
            "Use [bold]--lan[/] to open it to your local network.)[/]",
        )
    elif server.host in ("0.0.0.0", "::"):  # nosec B104 -- comparison only
        where = (
            "the addresses listed below"
            if server.lan
            else f"this machine's name or address from {escape('[server]')} allowed_hosts"
        )
        console_.print(
            f"  [dim](Listening on all interfaces.  Other devices open {where}.)[/]",
        )
    else:
        console_.print(
            f"  [dim](Listening on [bold]{escape(server.host)}[/].  "
            "Reachable from devices that can route to this address.)[/]",
        )
    console_.print("  Press [bold]Ctrl+C[/] to quit\n")
    return url


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Optional TOML config. An explicitly supplied missing path is an error.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable debug logging (otherwise info / warning / error are shown).",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """AutoDJ — AI-powered local music continuity player.

    Indexes your music library using MuQ audio embeddings and plays songs
    in a continuous flow based on sonic similarity.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    # Default level INFO so users see boot banners, WS connect /
    # disconnect, background analysis progress, and external-cue import
    # results without having to opt into -v.  -v drops to DEBUG.
    level = logging.DEBUG if verbose else logging.INFO
    handler: logging.Handler
    if sys.stderr.isatty():
        from rich.logging import RichHandler

        from autodj.progress import get_progress_console

        handler = RichHandler(
            console=get_progress_console(), show_time=False, show_level=False, show_path=False
        )
    else:
        handler = logging.StreamHandler(sys.stderr)
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        level=level,
        handlers=[handler],
        force=True,
    )
    # basicConfig honours `force=True` to wipe any pre-existing handlers
    # (pytest, jupyter, etc.) but the root logger may still carry an old
    # level from a prior import.  Set it explicitly so the configured
    # level takes effect regardless of import order.
    logging.getLogger().setLevel(level)
    # At INFO these read as errors or noise: faiss logs a ModuleNotFoundError
    # each time it picks its CPU build, and httpx logs every model-download
    # request.  -v shows them again.
    for name in ("faiss.loader", "httpx"):
        logging.getLogger(name).setLevel(logging.NOTSET if verbose else logging.WARNING)


def _file_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """Return *record* as the log file should hold it.

    A record that carries ``LOG_FILE_TEXT`` (the start-up pairing code) is
    written to the file with that text instead, so the code stays on the
    console only.  The console handlers still get the original record.
    """
    from autodj.lan import LOG_FILE_TEXT

    text = getattr(record, LOG_FILE_TEXT, None)
    if not isinstance(text, str):
        return record
    copy = logging.makeLogRecord(record.__dict__)
    copy.msg, copy.args = text, None
    return copy


def _add_log_file(path: Path) -> logging.Handler | None:
    """Also write the log to *path*: a new file each day, the last 7 kept."""
    from logging.handlers import TimedRotatingFileHandler

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(path, when="midnight", backupCount=7, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not open the log file %s: %s", path, exc)
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_file_log_record)
    logging.getLogger().addHandler(handler)
    return handler


# ---------------------------------------------------------------------------
# doctor subcommand
# ---------------------------------------------------------------------------


@cli.command("setup", add_help_option=False, context_settings={"ignore_unknown_options": True})
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
def cmd_setup(arguments: tuple[str, ...]) -> None:
    """Detect hardware, install its runtime, and configure AutoDJ."""
    from autodj.launcher import run_setup

    raise click.exceptions.Exit(run_setup(list(arguments)))


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--fix/--no-fix",
    default=None,
    help="Apply supported repairs without prompting, or disable repairs. "
    "By default, ask before each repair in an interactive terminal.",
)
@click.option(
    "--station",
    default=None,
    metavar="SLUG",
    help="Check only this station besides the server: default, or a [stations.<slug>] name.",
)
@click.pass_context
def cmd_doctor(
    ctx: click.Context,
    as_json: bool,
    fix: bool | None,
    station: str | None,
) -> None:
    """Check configuration, storage, dependencies, model, network, and stream health.

    With several stations, each station's own checks run once per station.
    """
    from autodj.doctor import (
        CheckStatus,
        DoctorCheck,
        DoctorReport,
        render_text,
        run_doctor,
    )

    if as_json and fix:
        raise click.UsageError("--json is read-only; run repairs separately with --fix.")
    offer_repairs = not as_json and (sys.stdin.isatty() if fix is None else fix)
    # With --station only that station's own repairs run: nothing server-wide.
    server_repairs = offer_repairs and station is None
    try:
        cfg = _doctor_load_config(ctx.obj["config_path"], server_repairs, fix is True, as_json)
    except click.exceptions.Exit:
        if not as_json:
            raise
        report = DoctorReport(
            (
                DoctorCheck(
                    "configuration",
                    CheckStatus.FAIL,
                    "configuration invalid",
                    "fix the configuration and retry",
                ),
            )
        )
        click.echo(report.to_json())
        raise click.exceptions.Exit(1) from None
    if station is not None and station not in {"default", *(s.slug for s in cfg.stations)}:
        raise click.BadParameter(f"no station is named {station!r}", param_hint="--station")
    report = run_doctor(cfg, station=station)
    click.echo(report.to_json() if as_json else render_text(report))
    if offer_repairs and _doctor_repair_saved_settings(cfg, report, fix is True):
        click.echo("Rechecking after repairs...")
        report = run_doctor(cfg, station=station)
        click.echo(render_text(report))
    if offer_repairs and _doctor_create_directories(
        cfg, report, fix is True, server=server_repairs
    ):
        click.echo("Rechecking after repairs...")
        report = run_doctor(cfg, station=station)
        click.echo(render_text(report))
    if server_repairs and _doctor_recover_restore(cfg, report):
        click.echo("Rechecking after repairs...")
        report = run_doctor(cfg, station=station)
        click.echo(render_text(report))
    recovery_failed = False
    if server_repairs:
        from autodj.doctor_commands import run_recovery_commands

        report, recovery_failed = run_recovery_commands(
            cfg, report, auto_fix=fix is True, station=station
        )
    if report.exit_code or recovery_failed:
        raise click.exceptions.Exit(1)


def _doctor_load_config(
    config_path: str | None,
    offer_repairs: bool,
    auto_fix: bool,
    as_json: bool,
) -> AutoDJConfig:
    """Offer optional cleanup of unsupported keys, then load normal configuration."""
    from autodj.doctor_repair import plan_unknown_key_repairs

    if offer_repairs:
        try:
            repairs = plan_unknown_key_repairs(config_path)
            if repairs:
                for repair in repairs:
                    click.echo(f"Remove unsupported keys from {repair.path}:")
                    for section, keys in repair.keys_by_section.items():
                        click.echo(f"  [{section}]: {list(keys)}")
                if auto_fix or click.confirm(
                    "Apply this repair (one backup per affected file)?", default=False
                ):
                    for repair in repairs:
                        backup = repair.apply()
                        click.echo(f"Repaired {repair.path}. Backup: {backup}")
        except (OSError, ValueError, RuntimeError) as repair_error:
            raise click.ClickException(
                f"Could not repair configuration: {repair_error}"
            ) from repair_error
    return _load_cfg_or_exit(config_path, show_error=not as_json)


def _station_state_dir(cfg: AutoDJConfig, slug: str | None) -> Path:
    """The folder holding station *slug*'s state files; the default station's for ``None``."""
    from autodj.config import station_config
    from autodj.stations import StationPaths

    if slug is None:
        return StationPaths.default(cfg).state_dir
    spec = next(spec for spec in cfg.stations if spec.slug == slug)
    return StationPaths.for_station(station_config(spec.cfg, cfg), slug).state_dir


def _doctor_repair_saved_settings(cfg: AutoDJConfig, report: DoctorReport, auto_fix: bool) -> bool:
    """Offer each station's saved-settings cleanup, one at a time, each with a backup."""
    from autodj.doctor_repair import plan_saved_state_repair

    changed = False
    for check in report.checks:
        if check.repair != "repair-saved-settings":
            continue
        repair = plan_saved_state_repair(_station_state_dir(cfg, check.station))
        where = f" for station {check.station}" if check.station else ""
        if repair is None:
            click.echo(
                f"No safe automatic edit is available for saved settings{where}; "
                "review the details above."
            )
            continue
        click.echo(f"Remove obsolete repeat keys from {repair.path}:")
        for section, keys in repair.keys_by_section.items():
            click.echo(f"  [{section}]: {list(keys)}")
        if not auto_fix and not click.confirm("Apply this repair (with a backup)?", default=False):
            continue
        try:
            backup = repair.apply()
        except (OSError, ValueError, RuntimeError) as exc:
            raise click.ClickException(f"Could not repair saved settings: {exc}") from exc
        click.echo(f"Repaired {repair.path}. Backup: {backup}")
        changed = True
    return changed


def _doctor_recover_restore(cfg: AutoDJConfig, report: DoctorReport) -> bool:
    """Offer to roll back or finish an interrupted restore; always asks, even with --fix.

    Nothing is deleted: what is moved out of the way goes into a
    ``restore-leftovers-<time>`` folder in the index folder.
    """
    import time

    from autodj.backup import BackupError, recover_restore

    check = next((c for c in report.checks if c.repair == "recover-restore"), None)
    if check is None:
        return False
    keep = Path(cfg.index.index_dir) / f"restore-leftovers-{time.strftime('%Y%m%d-%H%M%S')}"
    if check.status.value == "fail":
        click.echo(
            "A restore stopped part way. Doctor can put every old file back where it was; the "
            f"files the restore had already put in place go into {keep}."
        )
        question = "Roll back the interrupted restore?"
    else:
        click.echo(
            f"A restore left old copies or hidden folders. Doctor can move them into {keep}; "
            "nothing is deleted."
        )
        question = "Move them out of the way?"
    if not sys.stdin.isatty():
        click.echo("Not changing a restore without asking; run `autodj doctor` in a terminal.")
        return False
    if not click.confirm(question, default=False):
        return False
    try:
        done = recover_restore(cfg, keep)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in done:
        click.echo(line)
    return True


def _doctor_create_directories(
    cfg: AutoDJConfig, report: DoctorReport, auto_fix: bool, *, server: bool = True
) -> bool:
    """Offer only missing writable storage directories identified by the checks.

    These are the index and model folders and each other station's own
    folder.  A new, empty folder replaces nothing, so no backup is made.
    """
    from autodj.doctor import CheckStatus

    paths = {"index-path": cfg.index.index_dir, "model-path": cfg.index.model_dir}
    changed = False
    for check in report.checks:
        path: Path | None
        if check.repair == "create-station-folder" and check.station is not None:
            path = _station_state_dir(cfg, check.station)
            label = f"folder for station {check.station}"
        else:
            path = paths.get(check.name) if check.station is None and server else None
            label = f"{check.name} directory"
        if path is None or check.status is not CheckStatus.WARN or path.exists():
            continue
        click.echo(f"Create missing {label}: {path}")
        if auto_fix or click.confirm("Create this directory?", default=False):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise click.ClickException(f"Could not create {path}: {exc}") from exc
            changed = True
    return changed


@cli.group("devices")
def devices_group() -> None:
    """List, rename and revoke browsers paired with this AutoDJ instance."""


def _device_registry(ctx: click.Context) -> tuple[AutoDJConfig, DeviceRegistry]:
    """Load configured registry without exposing its signing secret."""
    from autodj.pairing import DeviceRegistry

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    if cfg.server.auth_mode == "trusted_proxy":
        raise click.ClickException(
            "SSO browsers are managed in Settings; native pairing commands are disabled."
        )
    return cfg, DeviceRegistry(paired_devices_path(cfg))


@devices_group.command("list")
@click.pass_context
def cmd_devices_list(ctx: click.Context) -> None:
    """List paired browser identities and revocation state."""
    _cfg, registry = _device_registry(ctx)
    devices = registry.list_devices()
    if not devices:
        click.echo("No browsers have been paired.")
        return
    for device in devices:
        state = "revoked" if device.revoked_at is not None else "active"
        click.echo(f"{device.device_id}  {state:7}  {device.name}")


@devices_group.command("revoke")
@click.argument("device_id")
@click.pass_context
def cmd_devices_revoke(ctx: click.Context, device_id: str) -> None:
    """Revoke one paired browser immediately."""
    _cfg, registry = _device_registry(ctx)
    if not registry.revoke(device_id):
        raise click.ClickException("Active paired device was not found.")
    click.echo(f"Revoked device {device_id}.")


@devices_group.command("rename")
@click.argument("device_id")
@click.argument("name")
@click.pass_context
def cmd_devices_rename(ctx: click.Context, device_id: str, name: str) -> None:
    """Give one paired browser a new name."""
    _cfg, registry = _device_registry(ctx)
    try:
        stored = registry.rename(device_id, name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if stored is None:
        raise click.ClickException("Active paired device was not found.")
    click.echo(f"Renamed device {device_id} to {stored}.")


@devices_group.command("reset")
@click.confirmation_option(prompt="Revoke every paired browser?")
@click.pass_context
def cmd_devices_reset(ctx: click.Context) -> None:
    """Revoke every paired browser session."""
    _cfg, registry = _device_registry(ctx)
    click.echo(f"Revoked {registry.reset()} paired browser(s).")


@cli.group("tokens")
def tokens_group() -> None:
    """List, create and revoke machine tokens, such as analysis worker tokens.

    The same tokens as Settings, Analysis workers, for a server without a
    browser at hand (for example through `docker exec`).
    """


def _token_store(ctx: click.Context) -> tuple[AutoDJConfig, MachineTokenStore]:
    from autodj.machine_tokens import MachineTokenStore
    from autodj.secret_files import machine_tokens_path

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    return cfg, MachineTokenStore(machine_tokens_path(cfg))


@tokens_group.command("list")
@click.pass_context
def cmd_tokens_list(ctx: click.Context) -> None:
    """List active machine tokens (never their secrets)."""
    from datetime import datetime

    _cfg, store = _token_store(ctx)
    tokens = store.list_tokens()
    if not tokens:
        click.echo("No machine tokens.")
        return

    def when(seconds: int | None) -> str:
        if seconds is None:
            return "never"
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")

    for token in tokens:
        station = f"  station: {token.station}" if token.station else ""
        click.echo(
            f"{token.token_id}  {token.name}  scopes: {', '.join(token.scopes)}{station}  "
            f"created {when(token.created_at)}  last used {when(token.last_used_at)}"
        )


@tokens_group.command("create")
@click.argument("name")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    required=True,
    type=click.Choice(sorted(KNOWN_SCOPES)),
    help="What the token may do; give it once per scope.",
)
@click.option(
    "--station",
    default=None,
    metavar="SLUG",
    help="The station an announcer token works for: default, or a [stations.<slug>] name.",
)
@click.pass_context
def cmd_tokens_create(
    ctx: click.Context, name: str, scopes: tuple[str, ...], station: str | None
) -> None:
    """Create a token NAME and print it once."""
    cfg, store = _token_store(ctx)
    if station is not None and station not in {"default", *(s.slug for s in cfg.stations)}:
        raise click.ClickException(f"No station is named {station!r}.")
    try:
        token, secret = store.create(name, scopes, station)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    # stdout carries only the token, for scripts; the rest goes to stderr.
    click.echo(secret)
    click.echo(
        f"Created token {token.token_id} for {token.name} ({', '.join(token.scopes)}).  "
        "This token is shown only now and cannot be shown again: copy it now.",
        err=True,
    )


@tokens_group.command("revoke")
@click.argument("token_id")
@click.pass_context
def cmd_tokens_revoke(ctx: click.Context, token_id: str) -> None:
    """Revoke one token at once; a worker using it stops at its next request."""
    from autodj.analysis_queue import AnalysisQueue

    cfg, store = _token_store(ctx)
    if not store.revoke(token_id):
        raise click.ClickException("Active machine token was not found.")
    library_db = cfg.index.library_dir / "library.db"
    if library_db.is_file():
        queue = AnalysisQueue(library_db)
        try:
            queue.forget_worker(token_id)
        finally:
            queue.close()
    click.echo(f"Revoked token {token_id}.")


def _server_with_saved_token(cfg: AutoDJConfig) -> ServerConfig:
    """Return ``cfg.server``, using the token ``serve --lan`` saved when none is configured.

    Raises:
        click.ClickException: The saved token file cannot be read.
    """
    from dataclasses import replace

    from autodj.lan import AccessTokenError, read_access_token
    from autodj.secret_files import access_token_path

    if cfg.server.access_token is not None:
        return cfg.server
    try:
        saved = read_access_token(access_token_path(cfg))
    except AccessTokenError as exc:
        raise click.ClickException(str(exc)) from exc
    if saved is None:
        return cfg.server
    click.echo(
        "This code comes from the token `autodj serve --lan` saved; it only works while "
        "the server uses that token.",
        err=True,
    )
    return replace(cfg.server, access_token=saved)


@devices_group.command("pairing-code")
@click.pass_context
def cmd_devices_pairing_code(ctx: click.Context) -> None:
    """Print a short-lived code that pairs one more browser."""
    from autodj.security import SecurityPolicy

    cfg, registry = _device_registry(ctx)
    policy = SecurityPolicy(_server_with_saved_token(cfg), device_is_active=registry.is_active)
    try:
        code = policy.current_pairing_code()
    except RuntimeError as exc:
        raise click.ClickException("Configure LAN access before requesting a code.") from exc
    valid_for, next_code_in = policy.pairing_code_seconds_left()
    # The running server shares this registry, so the code works there now.
    registry.open_pairing(valid_for)
    click.echo(code)
    # stderr keeps stdout to the bare code for scripts such as container_smoke.sh.
    click.echo(
        f"Pairs one browser within about {valid_for} seconds. A new code starts in "
        f"{next_code_in} seconds; if the server says pairing is paused, run this again then.",
        err=True,
    )


# ---------------------------------------------------------------------------
# backup and restore subcommands
# ---------------------------------------------------------------------------


@cli.command("backup")
@click.argument("destination", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--force", is_flag=True, help="Replace an existing archive at DESTINATION.")
@click.pass_context
def cmd_backup(ctx: click.Context, destination: Path, force: bool) -> None:
    """Back up the index, station library, cues, history, settings, liners and profiles.

    Safe while AutoDJ is serving.  config.toml and config.local.toml are not
    included; copy them yourself.
    """
    from autodj.backup import BackupError, create_backup

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    try:
        path = create_backup(cfg, destination, force=force)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Backup written: {path}")


@cli.command("restore")
@click.argument("archive", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--force", is_flag=True, help="Replace the files and folders the backup restores.")
@click.pass_context
def cmd_restore(ctx: click.Context, archive: Path, force: bool) -> None:
    """Restore a backup made by the same major.minor version, then run doctor.

    Stop AutoDJ first; restore cannot tell whether it is running.  The liners
    and profiles folders in the backup replace the current ones whole.
    """
    from autodj.backup import BackupError, restore_backup
    from autodj.doctor import render_text, run_doctor

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    warnings: list[str] = []
    try:
        restored = restore_backup(cfg, archive, force=force, warnings=warnings)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Restored {restored} files.")
    for warning in warnings:
        click.echo(f"Warning: {warning}")
    report = run_doctor(cfg)
    click.echo(render_text(report))
    if report.exit_code:
        raise click.ClickException(
            "Restore completed, but doctor found required failures; do not serve yet."
        )


# ---------------------------------------------------------------------------
# index subcommand
# ---------------------------------------------------------------------------


@cli.command("index")
@click.option(
    "--limit",
    default=None,
    type=click.IntRange(min=1),
    show_default=True,
    help=(
        "Maximum pending songs to analyse in this run; all songs are imported first. "
        "Omit to analyse all eligible songs. "
        "Use a small number (e.g. --limit 20) to test the pipeline first."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-analyse eligible songs, keeping cues, history, settings and old results until replaced.",
)
@click.option(
    "-j",
    "--workers",
    "workers",
    default=None,
    type=click.IntRange(min=1),
    help=(
        "Audio-loader prefetch threads for the embed pass "
        "(default: 1 to bound full-track analysis memory).  Higher values "
        "require enough RAM for multiple decoded tracks."
    ),
)
@click.option(
    "--no-analyse",
    "skip_analyse",
    is_flag=True,
    help="Skip the extra beat/cue backfill for registered music folders; basic cues are still read.",
)
@click.option(
    "--no-enrich",
    "skip_enrich",
    is_flag=True,
    help=(
        "Skip refreshing tags and key data from beets after indexing (done by "
        "default when [library] beets_db is configured)."
    ),
)
@click.pass_context
def cmd_index(
    ctx: click.Context,
    limit: int | None,
    force: bool,
    workers: int | None,
    skip_analyse: bool,
    skip_enrich: bool,
) -> None:
    """Add registered folders to the library, then analyse pending songs.

    Add music folders in Library before indexing. Older indexes need migrate-index first.
    Later runs rescan the folders saved in Library tools. Songs become available
    before advanced analysis; folders with advanced analysis disabled are skipped
    by that pass. --limit caps advanced analysis, not the basic import.

    --force replaces existing analysis after each successful result. It keeps
    manual cues, history, settings and usable old vectors if a song fails.
    """
    from autodj.library import LibraryManager, analyse_library
    from autodj.model_identity import model_analysis_available

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    console.print(Panel("[bold green]AutoDJ library import[/]", expand=False))
    console.print(f"  Library : {cfg.index.library_dir}")
    if limit:
        console.print(f"  Advanced analysis limit : {limit} songs")
    if force:
        console.print("  Mode : [yellow]FORCE REBUILD[/] of analysis; saved station data is kept")
    try:
        sim = _load_index_for_serve(cfg)
        library = LibraryManager(cfg, sim)
        ctx.call_on_close(library.close)
        if not library.catalog.sources("music"):
            console.print(
                "No music folders registered. Start `autodj serve` and add folders in Library."
            )
        library.scan()
        state = library.snapshot()
        console.print(f"[green]{state['tracks_ready']} songs available.[/]")
        for error in state["import"]["errors"]:
            console.print(f"[yellow]{escape(error)}[/]")
        pending = library.analysis_pending(force=force)
        if pending and not model_analysis_available():
            console.print(
                "[yellow]Advanced analysis pending: local model analysis is unavailable. "
                "Imported songs remain available; run `autodj setup --backend cpu` "
                "to enable local analysis.[/]"
            )
        elif pending:
            from autodj.model import download_model_if_needed, load_model

            missing = [
                name for name in ("torch", "muq", "librosa", "soundfile") if not _can_import(name)
            ]
            if missing:
                raise RuntimeError(
                    f"Advanced analysis needs missing packages: {', '.join(missing)}. "
                    "Install with uv sync --extra index. Imported songs remain available."
                )
            model_path = download_model_if_needed(
                cfg.model, cfg.index, hf_token=cfg.huggingface.token
            )
            completed = analyse_library(
                cfg, load_model(model_path), limit=limit, force=force, workers=workers
            )
            console.print(f"[green]Advanced analysis completed for {completed} songs.[/]")
        else:
            console.print("Advanced analysis is up to date; nothing to do.")
    except Exception as exc:
        raise click.ClickException(f"Indexing failed: {exc}") from exc

    if not skip_enrich:
        if not cfg.library.beets_db:
            console.print("[yellow]Enrich skipped: no beets database configured.[/]")
        else:
            from autodj.indexer import enrich_from_beets

            try:
                updated, total = enrich_from_beets(
                    cfg.index.library_dir,
                    music_dir=cfg.library.music_dir,
                    beets_db=cfg.library.beets_db,
                )
                console.print(f"[green]Enrich:[/] {updated} of {total} songs updated.")
            except Exception as exc:
                console.print(f"[bold red]Enrich failed:[/] {escape(str(exc))}")

    if not skip_analyse:
        try:
            library.refresh()
            entries = [
                entry
                for entry in library.entries_snapshot()
                if any(
                    source["advanced"]
                    for source in library.catalog.sources_for_path(entry.path, "music")
                )
            ]
            _analyse_catalog_dj_meta(cfg, library.catalog, entries)
        except Exception as exc:
            console.print(f"[bold red]Analyse failed:[/] {escape(str(exc))}")
    if skip_analyse and skip_enrich:
        console.print("Post-pass: skipped")


# ---------------------------------------------------------------------------
# prune subcommand
# ---------------------------------------------------------------------------


@cli.command("prune")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Allow removal when more than 20% of catalog tracks are missing. "
        "Check registered library folders before overriding."
    ),
)
@click.pass_context
def cmd_prune(
    ctx: click.Context,
    force: bool,
) -> None:
    """Preview and confirm removal of catalog records missing from available folders.

    \b
    Examples:
      uv run autodj prune
      uv run autodj prune --force        # confirm removal above the 20% safeguard
    """
    from autodj.library import LibraryManager
    from autodj.similarity import SimilarityIndex

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])

    catalog_path = cfg.index.library_dir / "library.db"
    if not catalog_path.is_file():
        raise click.ClickException("No current library catalog. Run `autodj index` first.")
    manager = LibraryManager(cfg, SimilarityIndex.empty())
    try:
        manager.scan()
        preview = manager.missing_removal_preview()
        if not preview["ready"]:
            raise click.ClickException("The scan was incomplete; no records can be removed safely.")
        count = preview["count"]
        if not count:
            console.print("[green]No missing catalog tracks.[/]")
            return
        console.print(
            f"Missing tracks: {count} of {preview['total']} ({preview['percentage']:.1f}%)."
        )
        for track in preview["tracks"]:
            console.print(
                f"  {escape(track['artist'])} — {escape(track['title'])} ({escape(track['path'])})"
            )
        if count > len(preview["tracks"]):
            console.print(f"  …and {count - len(preview['tracks'])} more")
        if preview["requires_override"] and not force:
            raise click.ClickException(
                "More than 20% are missing; rerun with --force after checking folder availability."
            )
        if not click.confirm(
            "Remove these catalog records? Play history will be preserved", default=False
        ):
            console.print("No records removed.")
            return
        removed = manager.remove_missing(preview["token"], confirmed=True, allow_mass=force)
        console.print(
            f"[green]Removed {removed} missing catalog records; play history was preserved.[/]"
        )
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# enrich subcommand
# ---------------------------------------------------------------------------


@cli.command("enrich")
@click.pass_context
def cmd_enrich(ctx: click.Context) -> None:
    """Refresh current catalog tags and key / mode from the beets database.

    Walks every entry in the existing index and looks it up in your beets
    database.  Title, artist, album, genre, BPM, year and length are
    replaced by the beets values when beets has them, and the
    librosa-detected key and mode by the parsed ``initial_key`` field
    (set by the beets keyfinder plugin) when it is present.  No
    re-embedding required — completes in seconds even for huge libraries.

    ``autodj index`` already runs this by default.  Use this standalone
    command when you want to refresh beets metadata without embedding or
    DJ-meta analysis.

    \b
    Examples:
      uv run autodj enrich
    """
    from autodj.indexer import enrich_from_beets

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])

    if not cfg.library.beets_db:
        console.print(
            f"[bold red]No {escape('[library]')} beets_db in config — enrich requires beets.[/]"
        )
        sys.exit(1)

    if not (cfg.index.library_dir / "library.db").is_file():
        raise click.ClickException("No current library catalog. Run `autodj index` first.")

    try:
        updated, total = enrich_from_beets(
            cfg.index.library_dir,
            music_dir=cfg.library.music_dir,
            beets_db=cfg.library.beets_db,
        )
    except Exception as exc:
        console.print(f"[bold red]Enrich failed:[/] {escape(str(exc))}")
        sys.exit(1)

    if total == 0:
        console.print("[yellow]No catalog entries found.[/]")
    elif updated == 0:
        console.print(
            f"[green]Index already in sync with beets[/] ({total} entries scanned, 0 changed)."
        )
    else:
        console.print(f"[green]Updated {updated} of {total} tracks[/] from beets.")


# ---------------------------------------------------------------------------
# analyse subcommand
# ---------------------------------------------------------------------------


@cli.command("analyse-library")
@click.pass_context
def cmd_analyse_library(ctx: click.Context) -> None:
    """Analyse basic-ready songs from folders with advanced analysis enabled."""
    _require_model_analysis()
    from autodj.library import analyse_library
    from autodj.model import download_model_if_needed, load_model

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    try:
        model_path = download_model_if_needed(cfg.model, cfg.index, hf_token=cfg.huggingface.token)
        completed = analyse_library(cfg, load_model(model_path))
        click.echo(f"[AutoDJ] Advanced analysis embedded {completed} tracks.")
    except Exception as exc:
        raise click.ClickException(f"Library analysis failed: {exc}") from exc


@cli.command("worker")
@click.option(
    "--server",
    required=True,
    help="Address of the AutoDJ server, such as https://nas.local:8765.",
)
@click.option(
    "--token",
    required=True,
    envvar="AUTODJ_WORKER_TOKEN",
    help=(
        "Machine token from the server's Settings, Analysis workers.  Or set "
        "AUTODJ_WORKER_TOKEN, which keeps it out of the shell history."
    ),
)
@click.option(
    "--once",
    is_flag=True,
    help="Stop as soon as the server has nothing left to analyse (the same as --idle-exit 0).",
)
@click.option(
    "--idle-exit",
    "idle_exit",
    type=click.IntRange(min=0),
    default=None,
    metavar="MINUTES",
    help=(
        "Keep asking every half minute and stop once nothing has been left to analyse for "
        "MINUTES, for a server still scanning new music.  Wins over --once."
    ),
)
@click.option(
    "--idle-release",
    type=click.IntRange(min=1),
    default=None,
    metavar="MINUTES",
    help=(
        "Load the model only when songs arrive, then free it after MINUTES with an empty "
        "queue. Keep polling and repeat the hooks for each load. --idle-exit wins if both expire."
    ),
)
@click.option(
    "--batch",
    default=4,
    show_default=True,
    type=click.IntRange(1, 16),
    help="Songs to claim at a time.",
)
@click.option(
    "--device",
    type=click.Choice(["auto", "cuda", "cpu"]),
    default="auto",
    show_default=True,
    help="Where the model runs: the GPU when PyTorch sees one (auto), or force one.",
)
@click.option(
    "--ca-cert",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="PEM certificate to trust for the server, for a self-signed certificate.",
)
@click.option(
    "--before-command",
    default=None,
    help=(
        "Program to run before each model load (once without --idle-release), such as "
        "'podman stop tts-service' to free the GPU.  Run without a shell."
    ),
)
@click.option(
    "--after-command",
    default=None,
    help=(
        "Program to run after freeing the model, on exit or --idle-release, such as "
        "'podman start tts-service'.  Run without a shell."
    ),
)
@click.pass_context
def cmd_worker(
    ctx: click.Context,
    server: str,
    token: str,
    once: bool,
    idle_exit: int | None,
    idle_release: int | None,
    batch: int,
    device: str,
    ca_cert: Path | None,
    before_command: str | None,
    after_command: str | None,
) -> None:
    """Analyse new songs for an AutoDJ server, on this machine's GPU.

    Claims songs from the server, downloads their audio, analyses them exactly
    as `autodj index` would and sends the results back.  The server's music
    folder is never read directly.  Ctrl+C stops after the current song.
    Without --once or --idle-exit it keeps running and waits for new songs.
    With --idle-release, it loads the model on demand and frees it between batches.

    
    Examples:
      autodj worker --server https://nas.local:8765 --token adj1.…
      autodj worker --server https://nas.local:8765 --idle-exit 10 \
        --before-command "podman stop tts-service" --after-command "podman start tts-service"
      autodj worker --server https://nas.local:8765 --idle-release 5 \
        --before-command "podman stop tts-service" --after-command "podman start tts-service"
    """
    _require_model_analysis()
    import signal
    import threading

    from autodj.indexer import TrackAnalysis, analyse_file, analysis_identity
    from autodj.model import ModelLoadError, MuqWrapper
    from autodj.worker import (
        HttpTransport,
        ServerApi,
        Worker,
        WorkerError,
        choose_device,
        release_gpu,
        run_hook,
    )

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    stop = threading.Event()

    def interrupted(_signum: int, _frame: object) -> None:
        stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)
        console.print(
            "[yellow]Stopping after the current song.  Press Ctrl+C again to quit now.[/]"
        )

    try:
        api = ServerApi(HttpTransport(server, token, ca_cert=ca_cert))
        chosen = choose_device(device)
    except WorkerError as exc:
        raise click.ClickException(str(exc)) from exc
    from autodj.compute import device_description

    label = device_description(chosen)
    identity = analysis_identity(cfg.model.name, None)
    wrapper: MuqWrapper | None = None
    prepared = False

    def analyse(path: Path) -> TrackAnalysis:
        assert wrapper is not None  # loaded before analysis starts
        return analyse_file(path, wrapper)

    def prepare() -> None:
        nonlocal wrapper, prepared
        prepared = True
        run_hook(before_command, "Before-command")
        console.print(f"Analysing on {escape(label)}.")
        wrapper = load_model(model_path, device=chosen)

    def release() -> None:
        nonlocal wrapper, prepared
        if not prepared:
            return
        prepared = False
        try:
            if wrapper is not None:
                release_gpu(wrapper)
        finally:
            wrapper = None
            run_hook(after_command, "After-command")

    idle_seconds = idle_exit * 60.0 if idle_exit is not None else (0.0 if once else None)
    worker = Worker(
        api,
        analyse,
        identity,
        batch=batch,
        device=label,
        stop=stop,
        idle_exit=idle_seconds,
        idle_release=idle_release * 60.0 if idle_release is not None else None,
        prepare=prepare if idle_release is not None else None,
        release=release if idle_release is not None else None,
    )
    previous = signal.signal(signal.SIGINT, interrupted)
    try:
        # The token, the model and its weights are checked before anything is
        # stopped or loaded.
        server_identity = worker.connect()
        from autodj.model import download_model_if_needed, load_model
        from autodj.model_identity import folder_commit

        model_path = download_model_if_needed(cfg.model, cfg.index, hf_token=cfg.huggingface.token)
        worker.identity = analysis_identity(cfg.model.name, folder_commit(model_path))
        worker.check_identity(server_identity)
        try:
            if idle_release is None:
                prepare()
            worker.run()
        finally:
            release()
    except (WorkerError, ModelLoadError) as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        console.print("[yellow]Worker stopped.[/]")
    finally:
        signal.signal(signal.SIGINT, previous)


@cli.command("analyse")
@click.option(
    "--limit",
    default=None,
    type=click.IntRange(min=1),
    help="Analyse at most this many of the tracks that need it.",
)
@click.pass_context
def cmd_analyse(
    ctx: click.Context,
    limit: int | None,
) -> None:
    """Backfill CPU DJ metadata for music in the current catalog.

    Uses current catalog entries, decodes tracks whose ``dj_meta.db`` cache
    is missing or has ``analysed=False``, merges cues imported from DJ
    software (``[playback] import_external_cues``), and publishes automatic
    intro/outro suggestions. Skips completed analyses and preserves manual
    cue points. Tracks from every registered library folder are included.

    No GPU and no MuQ model required -- pure CPU work via librosa +
    numpy.  Run this on the NAS / listening host after a GPU host has
    finished the embedding pass; transition fades will then use the
    real per-track outro_start_s / intro_end_s instead of falling back
    to crossfade_seconds.

    \b
    Examples:
      uv run autodj analyse
      uv run autodj analyse --limit 100   # analyse only the next 100
    """
    missing = [name for name in ("librosa", "soundfile") if not _can_import(name)]
    if missing:
        console.print(
            f"[bold red]Cannot analyse — missing packages: {', '.join(missing)}[/]\n"
            "Install with:  [bold]uv sync --extra index[/]  (or --extra all)."
        )
        sys.exit(1)

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])

    from autodj.catalog import LibraryCatalog

    catalog_path = cfg.index.library_dir / "library.db"
    if not catalog_path.is_file():
        console.print(
            f"[bold red]No current library at {catalog_path}.[/]  Run `autodj index` first."
        )
        sys.exit(1)
    try:
        with LibraryCatalog(catalog_path) as catalog:
            entries = list(catalog.entries_snapshot("music"))
            console.print(Panel("[bold green]AutoDJ DJ-meta backfill[/]", expand=False))
            console.print(f"  Library   : {catalog_path}")
            console.print(f"  Tracks    : {len(entries)}")
            if limit is not None:
                console.print(f"  Limit     : {limit}")
            console.print()
            _analyse_catalog_dj_meta(cfg, catalog, entries, limit=limit)
    except Exception as exc:
        console.print(f"[bold red]Analyse failed:[/] {escape(str(exc))}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# serve subcommand
# ---------------------------------------------------------------------------


@cli.command("migrate-index")
@click.option(
    "--apply", is_flag=True, help="Save a recovery copy and convert; otherwise preview only."
)
@click.option(
    "--map",
    "move_map",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON mapping of old to new paths relative to music_dir.",
)
@click.option(
    "--music-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Base folder for paths in a legacy index; not needed for current-library state upgrades.",
)
@click.pass_context
def cmd_migrate_index(
    ctx: click.Context,
    apply: bool,
    move_map: Path | None,
    music_dir: Path | None,
) -> None:
    """Import published index data and legacy state into the current catalog.

    Stop serving and indexing before --apply. The default is a read-only preview.
    Vectors are imported unchanged; audio is not reanalysed.
    """
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    try:
        moves = json.loads(move_map.read_text(encoding="utf-8")) if move_map else {}
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Cannot read the move map: {exc}") from exc
    from autodj.config import station_config
    from autodj.index_migration import migrate_catalog
    from autodj.stations import StationPaths

    liner_dir = str(StationPaths.default(cfg).liners)
    station_web_states = [
        StationPaths.default(cfg).web_state,
        *(
            StationPaths.for_station(station_config(spec.cfg, cfg), spec.slug).web_state
            for spec in cfg.stations
        ),
    ]
    try:
        migrate_catalog(
            cfg.index.library_dir,
            music_dir or cfg.library.music_dir,
            Path(liner_dir),
            moves,
            apply=apply,
            station_web_states=station_web_states,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise click.ClickException(f"Catalog migration failed: {exc}") from exc


class _ServeCommand(click.Command):
    """Point failed serve invocations to diagnostics without changing the error."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.Abort:
            raise
        except (Exception, SystemExit) as exc:
            clean_exit = (isinstance(exc, click.exceptions.Exit) and exc.exit_code == 0) or (
                isinstance(exc, SystemExit) and exc.code in (None, 0)
            )
            if not clean_exit:
                hint = "Run `autodj doctor` to troubleshoot."
                if ctx.obj and ctx.obj.get("config_path"):
                    hint += " Use the same --config option."
                click.echo(hint, err=True)
                server = sys.modules.get("autodj.server")
                started = server is not None and server.STARTUP_COMPLETE.is_set()
                if (
                    sys.stdin.isatty()
                    and not started
                    and not os.environ.get("AUTODJ_DOCTOR_ACTIVE")
                ):
                    import subprocess  # nosec B404 -- fixed command, argv only, no shell

                    try:
                        if click.confirm("Run AutoDJ Doctor now?", default=False, err=True):
                            args = [sys.executable, "-m", "autodj"]
                            if ctx.obj and ctx.obj.get("config_path"):
                                args.extend(["--config", str(ctx.obj["config_path"])])
                            args.append("doctor")
                            environment = os.environ.copy()
                            environment["AUTODJ_RUNTIME"] = "current"
                            environment["AUTODJ_DOCTOR_ACTIVE"] = "1"
                            subprocess.run(args, env=environment, check=False)  # nosec B603
                            click.echo("After resolving the checks, run your serve command again.")
                    except (OSError, click.Abort, KeyboardInterrupt) as doctor_error:
                        click.echo(f"Doctor did not finish: {doctor_error}", err=True)
            raise


def _claim_index_for_serve(ctx: click.Context, library_dir: Path) -> None:
    """Refuse to serve a half-restored index; hold the serve lock while serving.

    A restore that stopped part way leaves its journal: some files are new
    and some old, so the server does not start until ``autodj doctor`` has
    rolled it back.  The serve lock makes ``autodj restore`` refuse while
    this server runs; it is released when the command ends.
    """
    from autodj.backup import BackupError, hold_serve_lock, pending_restore_state

    restore_state = pending_restore_state(library_dir)
    if restore_state not in (None, "installed"):
        console.print(
            "[bold red]A restore of this index folder did not finish, so some files are new and "
            "some old.[/] Run `autodj doctor` to put the old files back, then start AutoDJ again."
        )
        sys.exit(1)
    if restore_state == "installed":
        console.print(
            "[yellow]A restore left old copies beside the index folder; `autodj doctor` can move "
            "them out of the way.[/]"
        )
    try:
        serve_lock = hold_serve_lock(library_dir)
    except BackupError as exc:
        console.print(f"[bold red]{escape(str(exc))}[/]")
        sys.exit(1)
    ctx.call_on_close(serve_lock.release)


@cli.command("serve", cls=_ServeCommand)
@click.option(
    "--seed",
    default=None,
    type=str,
    help=(
        "Search term to choose the starting track (matched against title and artist). "
        "Omit to start from a random track."
    ),
)
@click.option(
    "--lan",
    is_flag=True,
    default=None,
    help=(
        "Use AutoDJ from other devices on your network: listen on all interfaces, "
        "allow this machine's names and addresses, and require pairing.  Startup "
        "prints the addresses to open, and a pairing code while no browser is paired."
    ),
)
@click.option(
    "--host",
    default=None,
    type=str,
    help="Interface to bind; defaults to [server].host (all interfaces with --lan).",
)
@click.option(
    "--port",
    default=None,
    type=click.IntRange(1, 65535),
    help="Port; defaults to [server].port.",
)
@click.option(
    "--access-token",
    default=None,
    type=str,
    hidden=True,
    help="Advanced: server secret for LAN pairing (prefer AUTODJ_ACCESS_TOKEN).",
)
@click.option(
    "--insecure-lan",
    is_flag=True,
    default=None,
    help="Skip pairing on the network (trusted networks only); use with --lan.",
)
@click.option(
    "--allowed-host",
    "allowed_hosts",
    multiple=True,
    hidden=True,
    help="Advanced: extra allowed HTTP Host name (custom DNS name, reverse proxy).",
)
@click.option(
    "--allowed-origin",
    "allowed_origins",
    multiple=True,
    hidden=True,
    help="Advanced: extra allowed browser origin including scheme and port.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    default=False,
    help="Open the web UI in the default browser after starting.",
)
@click.option(
    "--server-audio/--no-server-audio",
    "server_audio",
    default=None,
    help=("Override the default station's saved server sound-card output for this run."),
)
@click.option(
    "--stream/--no-stream",
    "stream",
    default=None,
    help="Serve the live mix as an MP3 radio stream (server mixes; the page is a remote).",
)
@click.option(
    "--ssl-certfile",
    "ssl_certfile",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Path to TLS certificate (PEM).  When combined with --ssl-keyfile, "
        "starts uvicorn in HTTPS mode — required for AudioWorklet on "
        "non-localhost hosts.  Overrides [server] ssl_certfile; renewed files "
        "are picked up without a restart."
    ),
)
@click.option(
    "--ssl-keyfile",
    "ssl_keyfile",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to TLS private key (PEM).  Pair with --ssl-certfile.",
)
@click.pass_context
def cmd_serve(  # pragma: no cover -- end-to-end orchestrator, exercised by smoke tests
    ctx: click.Context,
    seed: str | None,
    lan: bool | None,
    host: str | None,
    port: int | None,
    access_token: str | None,
    insecure_lan: bool | None,
    allowed_hosts: tuple[str, ...],
    allowed_origins: tuple[str, ...],
    open_browser: bool,
    server_audio: bool | None,
    stream: bool | None,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    """Start the auto-DJ player with a browser-based control panel.

    Runs the AutoDJ player in the background and serves a web UI at
    http://HOST:PORT.  All playback controls (pause, skip, volume, mute,
    discovery) are available from the browser.  With --server-audio the
    mix also plays on this machine's speakers, still controlled from the page.

    \b
    Examples:
      uv run autodj serve
      uv run autodj serve --seed "Portishead" --open
      uv run autodj serve --lan
      uv run autodj serve --server-audio
    """
    from autodj.server import serve

    cfg = _load_cfg_or_exit(ctx.obj["config_path"], log_file=True)
    from autodj.config import is_loopback_bind

    original_server = cfg.server
    security_cli_requested = any(
        (
            lan is not None,
            host is not None,
            port is not None,
            access_token is not None,
            insecure_lan is not None,
            bool(allowed_hosts),
            bool(allowed_origins),
            ssl_certfile is not None,
            ssl_keyfile is not None,
        )
    )
    staged_server = _stage_serve_server(
        cfg,
        host=host,
        port=port,
        access_token=access_token,
        insecure_lan=insecure_lan,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        lan=lan,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    # Captured before cfg.server is replaced by the merged, detected lists.
    lan_configured_hosts = _configured_allowed_hosts(cfg, allowed_hosts)
    server_cli_override = security_cli_requested and staged_server != original_server
    host = staged_server.host
    port = staged_server.port
    from autodj.index_manifest import IndexConsistencyError

    _claim_index_for_serve(ctx, cfg.index.library_dir)

    try:
        sim = _load_index_for_serve(cfg)
    except IndexConsistencyError as exc:
        console.print(f"[bold red]{escape(str(exc))}[/]")
        sys.exit(1)
    if (
        staged_server.insecure_lan
        and staged_server.access_token is None
        and not is_loopback_bind(staged_server.host)
    ):
        console.print("[yellow]WARNING: LAN access is unauthenticated (--insecure-lan).[/]")
    from autodj.library import LibraryManager

    library = LibraryManager(cfg, sim)
    ctx.call_on_close(library.close)
    _print_serve_banner(console, library)
    seed_entry = _resolve_seed(library, cfg, seed, console)
    url = _print_serve_url_banner(console, staged_server)
    cfg.server = staged_server
    if stream is not None:
        cfg.station.output = "stream" if stream else "browser"
        cfg.station.server_audio_too = bool(stream and server_audio)
        if cfg.default_station is not None:
            cfg.default_station.cfg.station = cfg.station
    if stream is not None or server_cli_override:
        _append_cli_source(cfg)
    if open_browser:
        import threading
        import webbrowser

        # Small delay so uvicorn is ready before the browser tries to connect
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    try:
        serve(
            cfg=cfg,
            sim=sim,
            library=library,
            seed_entry=seed_entry,
            host=host,
            port=port,
            no_playback=None if server_audio is None else not server_audio,
            stream=None,
            lan_configured_hosts=lan_configured_hosts,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")


# ---------------------------------------------------------------------------
# list-devices subcommand
# ---------------------------------------------------------------------------


@cli.command("list-devices")
def cmd_list_devices() -> None:
    """List every audio output device sounddevice can see.

    Set ``[playback] audio_device`` in ``config.toml`` to the index or a
    substring of the name to choose where ``serve --server-audio`` plays.

    \b
    Examples:
      uv run autodj list-devices
    """
    try:
        import sounddevice as sd
    except ImportError:
        console.print(
            "[bold red]sounddevice is not installed.[/]  "
            "Install playback dependencies with [bold]uv sync --extra play[/].",
        )
        sys.exit(1)

    try:
        default_out = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else None
    except (AttributeError, IndexError):
        default_out = None

    console.print("[bold]Audio output devices:[/]\n")
    devices = sd.query_devices()
    found_any = False
    for i, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) <= 0:
            continue
        found_any = True
        marker = " *" if i == default_out else "  "
        rate = int(dev.get("default_samplerate", 0))
        chans = dev.get("max_output_channels", 0)
        console.print(
            f" {marker} [bold]{i:3d}[/]  {dev['name']:40s}  [dim]{chans}ch @ {rate} Hz[/]",
        )
    if not found_any:
        console.print("[yellow]No output devices found.[/]")
    else:
        console.print(
            f"\n[dim]* = system default.  Set {escape('[playback]')} audio_device "
            "to an index or name.[/]"
        )


# ---------------------------------------------------------------------------
# export-plays subcommand
# ---------------------------------------------------------------------------


@cli.command("export-plays")
@click.option(
    "--from",
    "from_text",
    required=True,
    metavar="DATE",
    help="First day to export, YYYY-MM-DD (or an ISO 8601 date and time).",
)
@click.option(
    "--to",
    "to_text",
    required=True,
    metavar="DATE",
    help="Last day to export, YYYY-MM-DD, included (or an ISO 8601 date and time).",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write the CSV to this file instead of standard output.",
)
@click.option(
    "--format",
    "report_format",
    type=click.Choice(["csv", "soundexchange"]),
    default="csv",
    show_default=True,
    help="csv: one row per play.  soundexchange: a Report of Use for US webcasting.",
)
@click.option("--service", default="", help="Name of service, for --format soundexchange.")
@click.option(
    "--category",
    default="A",
    show_default=True,
    help="SoundExchange transmission category code (A, B, C, H, I, J, K or L).",
)
@click.option(
    "--channel", default="", help="Channel or program name, for --format soundexchange (optional)."
)
@click.option(
    "--missing",
    is_flag=True,
    help=(
        "List the songs lacking ISRC, label or other reporting fields instead of the plays,"
        " marking those SoundExchange cannot match."
    ),
)
@click.option(
    "--station",
    default="default",
    show_default=True,
    metavar="NAME",
    help="Station whose plays to export; the default station is the one the main config describes.",
)
@click.pass_context
def cmd_export_plays(
    ctx: click.Context,
    from_text: str,
    to_text: str,
    out_path: Path | None,
    report_format: str,
    service: str,
    category: str,
    channel: str,
    missing: bool,
    station: str,
) -> None:
    """Export the play log between two dates as CSV, for royalty reports.

    Writes one row per play: start, end, duration played, artist, title,
    album, ISRC, label, kind (song, liner or ad: the ad spots that aired),
    the path inside the music folder and the performances (listeners who
    heard it).  Times are local, with their UTC offset.  With
    --format soundexchange it writes a SoundExchange Report of Use instead:
    songs only, one line per recording.  A summary goes to standard error.

    \b
    Examples:
      uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --out september.csv
      uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --missing
      uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --format soundexchange --service "My Station"
      uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --station nightowl
    """
    from autodj.catalog import LibraryCatalog
    from autodj.playlog import (
        missing_csv,
        parse_bound,
        plays_between,
        plays_csv,
        soundexchange_report,
        summary,
    )
    from autodj.station_store import StationStore, migrate_legacy
    from autodj.stations import SLUG_PATTERN

    if SLUG_PATTERN.match(station) is None:
        raise click.BadParameter(
            "a station name is lower-case letters, digits and hyphens", param_hint="--station"
        )
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    try:
        start = parse_bound(from_text)
        end = parse_bound(to_text, end=True)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    db = cfg.index.library_dir / "library.db"
    if not db.is_file():
        click.echo(f"No play log found: {db} does not exist.", err=True)
        raise click.exceptions.Exit(1)
    try:
        with LibraryCatalog(db) as catalog:
            from autodj.stations import DEFAULT_SLUG, StationPaths

            paths = (
                StationPaths.default(cfg)
                if station == DEFAULT_SLUG
                else StationPaths.for_station(cfg, station)
            )
            store = StationStore(paths.state_dir / "station.db", station)
            try:
                migrate_legacy(catalog, store)
                plays = plays_between(catalog, start, end, station=station, station_store=store)
            finally:
                store.close()
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    except sqlite3.Error as exc:
        click.echo(f"Could not read the play log in {db}: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    if missing:
        text = missing_csv(plays)
    elif report_format == "soundexchange":
        try:
            text = soundexchange_report(plays, service, category, channel)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    else:
        from autodj.ads import aired_plays

        slug = None if station == "default" else station
        if slug is None or any(spec.slug == slug for spec in cfg.stations):
            ads = aired_plays(_station_state_dir(cfg, slug) / "ad_plays", start, end)
            plays = sorted([*plays, *ads], key=lambda play: play.start)
        text = plays_csv(plays)
    data = text.encode("utf-8")
    if out_path is None:
        # Bytes, so Windows text mode does not double the CSV's CRLF endings.
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        try:
            out_path.write_bytes(data)
        except OSError as exc:
            click.echo(f"Could not write {out_path}: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc
    click.echo(summary(plays), err=True)


# ---------------------------------------------------------------------------
# simulate subcommand
# ---------------------------------------------------------------------------


@cli.command("simulate")
@click.option(
    "--station",
    default="default",
    show_default=True,
    metavar="NAME",
    help="Station whose song picking to test; the default station is the one the main config "
    "describes.",
)
@click.option("--hours", type=float, default=None, help="Span to simulate, in hours.")
@click.option("--days", type=float, default=None, help="Span to simulate, in days.")
@click.option("--picks", type=int, default=None, help="Number of songs to pick instead of a span.")
@click.option(
    "--start",
    "start_text",
    default=None,
    metavar="TIME",
    help="When the run starts, such as 2026-10-12T06:00 in the station time zone (default: now).",
)
@click.option(
    "--seed", type=int, default=None, help="Seed for the random choices, to repeat a run."
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.pass_context
def cmd_simulate(
    ctx: click.Context,
    station: str,
    hours: float | None,
    days: float | None,
    picks: int | None,
    start_text: str | None,
    seed: int | None,
    as_json: bool,
) -> None:
    """Test the rotation: simulate song picking without changing anything on air.

    Plays a span (24 hours unless you say otherwise, at most 30 days) or a
    number of songs (at most 20,000) through the station's own picker:
    its clockwheel, categories and their hours, schedule, repeat rules and
    DMCA limits.  Then reports plays per song, category and artist, songs
    never played, the rules that had to be relaxed, skipped clockwheel
    steps, DMCA conflicts and any moment with nothing to play.

    \b
    Examples:
      uv run autodj simulate --days 7
      uv run autodj simulate --picks 500 --seed 42
      uv run autodj simulate --hours 24 --start 2026-10-16T18:00 --json
    """
    import json as _json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from autodj.rotation_sim import (
        SimulationError,
        SimulationRequest,
        open_station,
        report_text,
        simulate,
    )
    from autodj.scheduler import system_timezone
    from autodj.stations import SLUG_PATTERN

    if SLUG_PATTERN.match(station) is None:
        raise click.BadParameter(
            "a station name is lower-case letters, digits and hyphens", param_hint="--station"
        )
    if sum(value is not None for value in (hours, days, picks)) > 1:
        raise click.UsageError("Give --hours, --days or --picks, only one of them.")
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    start = None
    if start_text is not None:
        try:
            moment = datetime.fromisoformat(start_text)
        except ValueError as exc:
            raise click.BadParameter(
                "write the time as 2026-10-12T06:00", param_hint="--start"
            ) from exc
        if moment.tzinfo is None:
            zone = cfg.station.timezone or system_timezone()
            moment = moment.replace(tzinfo=ZoneInfo(zone))
        start = moment.timestamp()
    try:
        request = SimulationRequest(
            hours=days * 24 if days is not None else hours, picks=picks, start=start, seed=seed
        )
        with open_station(cfg, station) as source:
            if not as_json:
                click.echo("Simulating; nothing on air changes.", err=True)
            report = simulate(source, request)
    except SimulationError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1) from exc
    if as_json:
        click.echo(_json.dumps(report, indent=2, ensure_ascii=False))
    else:
        click.echo(report_text(report), nl=False)


# ---------------------------------------------------------------------------
# stats subcommand
# ---------------------------------------------------------------------------


@cli.command("stats")
@click.pass_context
def cmd_stats(ctx: click.Context) -> None:
    """Print a statistical overview of current catalog music.

    Reads catalog records (not vectors or the model) and displays BPM distribution, genres,
    decades, track lengths, top artists, key distribution, major/minor
    split, and energy histogram.

    \b
    Examples:
      uv run autodj stats
    """
    from autodj.catalog import LibraryCatalog
    from autodj.stats import print_stats

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])

    catalog_path = cfg.index.library_dir / "library.db"
    if not catalog_path.is_file():
        console.print(f"[bold red]Current library not found:[/] {escape(str(catalog_path))}")
        sys.exit(1)
    with LibraryCatalog(catalog_path) as catalog:
        print_stats(list(catalog.entries_snapshot("music")), console)
