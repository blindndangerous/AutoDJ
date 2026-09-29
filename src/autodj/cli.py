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
import logging
import os
import secrets
import shutil
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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
from rich.panel import Panel

from autodj.stream_secret import paired_devices_path

if TYPE_CHECKING:
    from autodj.beets import Track
    from autodj.config import AutoDJConfig, ServerConfig
    from autodj.indexer import IndexEntry
    from autodj.pairing import DeviceRegistry
    from autodj.similarity import SimilarityIndex

console = Console()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_seed(
    sim: SimilarityIndex,
    cfg: AutoDJConfig,
    seed: str | None,
    console_: Console,
) -> IndexEntry | None:
    """Resolve a seed string to an :class:`~autodj.indexer.IndexEntry`.

    When several indexed tracks match, the first one is used.

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
        console_.print(f"[yellow]No indexed tracks match '{seed}'. Starting random.[/yellow]")
        return None

    chosen = candidates[0]
    display = getattr(chosen, "display_name", str(chosen))
    console_.print(f"Seed: [bold]{display}[/bold]")
    path_str = str(getattr(chosen, "path", chosen))
    return next((e for e in entries if e.path == path_str), None)


def _load_cfg_or_exit(
    config_path: str | None,
    *,
    show_error: bool = True,
) -> AutoDJConfig:  # pragma: no cover
    """Load *config_path* or print + exit on missing file."""
    from autodj.config import load_config

    try:
        return load_config(config_path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if show_error:
            console.print(f"[bold red]Config not found or invalid:[/] {exc}")
        raise click.exceptions.Exit(1) from exc


def _append_cli_source(cfg: AutoDJConfig) -> None:
    """Record the CLI as the latest configuration source."""
    if not cfg.config_sources or cfg.config_sources[-1] != "cli":
        cfg.config_sources = (*cfg.config_sources, "cli")


def _apply_index_name(cfg: AutoDJConfig, index_name: str | None) -> bool:  # pragma: no cover
    """Validate + apply ``--name``; return whether effective config changed."""
    if index_name is None:
        return False
    from autodj.config import validate_index_name

    try:
        validate_index_name(index_name)
    except ValueError as exc:
        console.print(f"[bold red]Invalid --name:[/] {exc}")
        sys.exit(1)
    if cfg.index.name == index_name:
        return False
    cfg.index.name = index_name
    _append_cli_source(cfg)
    return True


def _load_index_for_serve(
    cfg: AutoDJConfig, *, active_dir: Path | None = None
) -> SimilarityIndex:  # pragma: no cover
    """Load an index for web serving; a directory without one serves an empty index."""
    from autodj.similarity import SimilarityIndex as _SI

    try:
        return _SI.from_index_dir(
            cfg.index.active_dir if active_dir is None else active_dir,
            music_dir=cfg.library.music_dir,
        )
    except FileNotFoundError:
        console.print(
            "[yellow]Index is empty; the web UI will stay ready while you run autodj index.[/]"
        )
        return _SI.empty()


def _scan_index_rows(
    base: Path, active_name: str
) -> list[tuple[str, str, str]]:  # pragma: no cover
    """Walk *base* for index directories; return (name, track count, path) rows.

    The track count comes from each index's ``index-manifest.json``.  An
    index from before the manifest format shows as needing a rebuild.
    """
    from autodj.index_manifest import (
        IndexConsistencyError,
        UnsupportedIndexError,
        read_manifest,
    )

    rows: list[tuple[str, str, str]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        try:
            manifest = read_manifest(entry)
        except UnsupportedIndexError:
            status = "[red]old format: rebuild with autodj index --force[/red]"
        except IndexConsistencyError:
            status = "[red]corrupt[/red]"
        else:
            if manifest is None:
                if not (entry / "tracks.db").exists():
                    continue
                status = "[red]old format: rebuild with autodj index --force[/red]"
            else:
                status = f"{manifest.vector_count} tracks"
        active_marker = "  *" if entry.name == active_name else "   "
        rows.append((active_marker + entry.name, status, str(entry)))
    return rows


def _can_import(name: str) -> bool:
    """Return True when *name* imports cleanly."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _require_ffmpeg_for_stream(cfg: AutoDJConfig) -> None:
    """Refuse to start stream mode when ffmpeg is not on the PATH.

    Args:
        cfg: Staged application configuration.

    Raises:
        click.ClickException: If ``cfg.stream.enabled`` and ffmpeg is missing.
    """
    if cfg.stream.enabled and shutil.which("ffmpeg") is None:
        raise click.ClickException(
            "Stream mode needs ffmpeg on the PATH. Install ffmpeg, or start without --stream."
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
    is configured.  The result has passed :func:`validate_server_exposure`.

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

    from autodj.config import validate_server_exposure
    from autodj.lan import AccessTokenError, lan_server_config
    from autodj.stream_secret import access_token_path

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
    console_: Console, sim: SimilarityIndex
) -> None:  # pragma: no cover -- terminal banner
    """Print the index summary banner."""
    console_.print(Panel(f"[bold green]AutoDJ[/] — {sim.ntotal} tracks indexed", expand=False))


def _print_serve_url_banner(
    console_: Console,
    host: str,
    port: int,
    tls: bool,
) -> str:  # pragma: no cover -- terminal banner
    """Print the web-UI URL + reachability hint; return the URL."""
    scheme = "https" if tls else "http"
    url = f"{scheme}://{host}:{port}"
    console_.print(f"  Web UI  : [link={url}]{url}[/link]")
    if scheme == "https":
        console_.print(
            "  [dim](TLS active — AudioWorklet effects work on remote hosts.  "
            "Trust the certificate's CA on every listening device.)[/]",
        )
    if host in ("127.0.0.1", "localhost", "::1"):
        console_.print(
            "  [dim](Reachable from this machine only.  "
            "Use [bold]--lan[/] to open it to your local network.)[/]",
        )
    elif host == "0.0.0.0":  # nosec B104 -- explicit user intent for LAN bind
        console_.print(
            "  [dim](Listening on all interfaces — open the URL above "
            "from any device on your LAN.  Use the machine's actual IP "
            "instead of 0.0.0.0 from a remote browser.)[/]",
        )
    else:
        console_.print(
            f"  [dim](Listening on [bold]{host}[/].  "
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
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        level=level,
        stream=sys.stderr,
        force=True,
    )
    # basicConfig honours `force=True` to wipe any pre-existing handlers
    # (pytest, jupyter, etc.) but the root logger may still carry an old
    # level from a prior import.  Set it explicitly so the configured
    # level takes effect regardless of import order.
    logging.getLogger().setLevel(level)


# ---------------------------------------------------------------------------
# doctor subcommand
# ---------------------------------------------------------------------------


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_doctor(ctx: click.Context, as_json: bool) -> None:
    """Check configuration, storage, dependencies, model, network, and stream health."""
    from autodj.doctor import (
        CheckStatus,
        DoctorCheck,
        DoctorReport,
        render_text,
        run_doctor,
    )

    try:
        cfg = _load_cfg_or_exit(ctx.obj["config_path"], show_error=not as_json)
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
    report = run_doctor(cfg)
    click.echo(report.to_json() if as_json else render_text(report))
    if report.exit_code:
        raise click.exceptions.Exit(report.exit_code)


@cli.command("setup-lan")
@click.option("--host-name", help="Hostname or IP that browsers use to reach AutoDJ.")
def cmd_setup_lan(host_name: str | None) -> None:
    """Create secure fresh-clone Compose settings for browser pairing."""
    from autodj.config import ServerConfig, validate_server_exposure

    suggested = socket.gethostname().strip().lower() or "autodj.local"
    selected_host = (host_name or click.prompt("LAN hostname or IP", default=suggested)).strip()
    token = secrets.token_hex(32)
    port = 8080
    try:
        selected_host = ServerConfig(host=selected_host).host
        rendered_host = f"[{selected_host}]" if ":" in selected_host else selected_host
        origin = f"http://{rendered_host}:{port}"
        server_config = ServerConfig(
            host="0.0.0.0",  # nosec B104 - explicit LAN setup command
            port=port,
            access_token=token,
            allowed_hosts=[selected_host, "127.0.0.1"],
            allowed_origins=[origin],
            session_ttl_seconds=90 * 24 * 60 * 60,
        )
        validate_server_exposure(server_config)
    except (TypeError, ValueError) as exc:
        raise click.ClickException(f"LAN hostname is invalid: {exc}") from exc

    destination = Path(".env")
    if destination.exists():
        raise click.ClickException(
            ".env already exists; it was not changed. Add AUTODJ_ACCESS_TOKEN, "
            "AUTODJ_LAN_HOST, and AUTODJ_LAN_ORIGIN there manually."
        )
    content = (
        f"AUTODJ_ACCESS_TOKEN={token}\n"
        f"AUTODJ_LAN_HOST={selected_host}\n"
        f"AUTODJ_LAN_ORIGIN={origin}\n"
    )
    created = False
    descriptor = -1
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
        destination.chmod(0o600)
    except OSError as exc:
        cleanup_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as close_exc:
                cleanup_error = close_exc
        if created:
            try:
                destination.unlink()
            except OSError as cleanup_exc:
                if cleanup_error is None:
                    cleanup_error = cleanup_exc
        message = f"Could not create .env: {exc}"
        if cleanup_error is not None:
            message += f"; remove the incomplete .env manually: {cleanup_error}"
        raise click.ClickException(message) from exc
    click.echo(f"LAN setup saved to .env. Open {origin} after startup.")
    click.echo("Start: docker compose --profile lan up autodj-lan")
    click.echo("AutoDJ will print a short pairing code during startup.")


@cli.group("devices")
def devices_group() -> None:
    """List, rename and revoke browsers paired with this AutoDJ instance."""


def _device_registry(ctx: click.Context) -> tuple[AutoDJConfig, DeviceRegistry]:
    """Load configured registry without exposing its signing secret."""
    from autodj.pairing import DeviceRegistry

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
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


def _server_with_saved_token(cfg: AutoDJConfig) -> ServerConfig:
    """Return ``cfg.server``, using the token ``serve --lan`` saved when none is configured.

    Raises:
        click.ClickException: The saved token file cannot be read.
    """
    from dataclasses import replace

    from autodj.lan import AccessTokenError, read_access_token
    from autodj.stream_secret import access_token_path

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
    """Print current short-lived code for pairing another browser."""
    from autodj.security import SecurityPolicy

    cfg, registry = _device_registry(ctx)
    policy = SecurityPolicy(_server_with_saved_token(cfg), device_is_active=registry.is_active)
    try:
        code = policy.current_pairing_code()
    except RuntimeError as exc:
        raise click.ClickException("Configure LAN access before requesting a code.") from exc
    valid_for, next_code_in = policy.pairing_code_seconds_left()
    click.echo(code)
    # stderr keeps stdout to the bare code for scripts such as container_smoke.sh.
    click.echo(
        f"Valid for about {valid_for} more seconds. A new code starts in {next_code_in} "
        "seconds; if the server says pairing is paused, use that one.",
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
    """Write the index, DJ metadata, web settings, liners and profiles to a ZIP file.

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
    try:
        restored = restore_backup(cfg, archive, force=force)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Restored {restored} files.")
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
    type=int,
    show_default=True,
    help=(
        "Maximum number of NEW tracks to embed in this run. "
        "Omit to process all unindexed tracks. "
        "Use a small number (e.g. --limit 20) to test the pipeline first."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Ignore the existing index and re-embed everything from scratch.",
)
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help=(
        "Named index to write to.  Files land in <index_dir>/<NAME>/.  Use this "
        "to keep multiple curated libraries side-by-side (e.g. 'workout', 'chill').  "
        "Default: 'default' (or [index] name in config.toml)."
    ),
)
@click.option(
    "-j",
    "--workers",
    "workers",
    default=None,
    type=int,
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
    help="Skip the DJ-meta backfill that runs after the embed pass.",
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
    index_name: str | None,
    workers: int | None,
    skip_analyse: bool,
    skip_enrich: bool,
) -> None:
    """Build or update the FAISS index for the music library.

    Reads track metadata from the beets database (if configured) or scans
    the filesystem.  Tracks already in the index are skipped unless --force
    is passed.

    ``index`` runs the full maintenance pipeline by default: embed new
    tracks, enrich from beets when configured, prune stale DJ-meta rows,
    and analyse missing intro/outro/cue metadata.  For a quick embed-only
    smoke test, skip the post-passes:

    \b
        uv run autodj index --limit 20 --no-enrich --no-analyse
        uv run autodj index           # full library (run overnight on GPU machine)
    """
    # Indexing requires torch + muq + librosa.  Probe before doing any
    # other work so users on minimal installs (NAS, Docker) get a clear
    # message instead of a deep stack trace from inside `model.py`.
    missing = [name for name in ("torch", "muq", "librosa", "soundfile") if not _can_import(name)]
    if missing:
        console.print(
            f"[bold red]Cannot index — missing packages: {', '.join(missing)}[/]\n"
            "Install indexing dependencies with:\n"
            "  [bold]uv sync --extra index[/]\n"
            "or for the whole kit:\n"
            "  [bold]uv sync --extra all[/]\n\n"
            "Indexing runs on CPU automatically when no compatible GPU is available —\n"
            "fine for small batches (use --limit 50 to try it on a NAS first)."
        )
        sys.exit(1)

    # Imported after the probe: indexer imports librosa at module scope.
    from autodj.indexer import build_index
    from autodj.model import download_model_if_needed, load_model

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _apply_index_name(cfg, index_name)

    # Detect compute device + warn about CPU performance for big libraries.
    from autodj.compute import device_string

    device = "CUDA (GPU)" if device_string() == "cuda" else "CPU"

    console.print(Panel("[bold green]AutoDJ Indexer[/]", expand=False))
    console.print(f"  Music dir  : {cfg.library.music_dir}")
    console.print(f"  Beets DB   : {cfg.library.beets_db or '(not configured)'}")
    console.print(f"  Index name : {cfg.index.name}")
    console.print(f"  Index dir  : {cfg.index.active_dir}")
    console.print(f"  Model      : {cfg.model.name}")
    console.print(f"  Device     : {device}")
    if device == "CPU" and not limit:
        console.print(
            "  [yellow]Note:[/] CPU indexing of a full library can take many hours.  "
            "Consider --limit 50 to test, then run a full pass on a GPU host."
        )
    if limit:
        console.print(f"  Limit      : {limit} tracks (test mode)")
    if force:
        console.print("  Mode       : [yellow]FORCE REBUILD[/]")
    post_passes: list[str] = []
    if not skip_enrich:
        post_passes.append("[green]+ enrich[/] (beets key/mode)")
    if not skip_analyse:
        post_passes.append("[green]+ analyse[/] (intro/outro/beat/cues)")
    if post_passes:
        console.print(f"  Post-pass  : {'; '.join(post_passes)}")
    else:
        console.print("  Post-pass  : [yellow]skipped[/]")
    console.print()

    try:
        model_path = download_model_if_needed(cfg.model, cfg.index, hf_token=cfg.huggingface.token)
        wrapper = load_model(model_path)
        build_index(
            cfg,
            wrapper=wrapper,
            limit=limit,
            force=force,
            workers=workers,
        )
    except Exception as exc:
        console.print(f"[bold red]Indexing failed:[/] {exc}")
        sys.exit(1)

    if not skip_enrich:
        if not cfg.library.beets_db:
            console.print("[yellow]Enrich skipped: no [library] beets_db configured.[/]")
        else:
            from autodj.indexer import enrich_from_beets

            try:
                updated, total = enrich_from_beets(
                    cfg.index.active_dir,
                    music_dir=cfg.library.music_dir,
                    beets_db=cfg.library.beets_db,
                )
                console.print(f"[green]Enrich:[/] {updated} of {total} tracks updated.")
            except Exception as exc:
                console.print(f"[bold red]Enrich failed:[/] {exc}")

    if not skip_analyse:
        from autodj.indexer import backfill_dj_meta, load_index

        try:
            entries, _, _ = load_index(cfg.index.active_dir, music_dir=cfg.library.music_dir)
            backfill_dj_meta(cfg, entries)
        except FileNotFoundError:
            console.print("[yellow]--analyse skipped: no index found.[/]")
        except Exception as exc:
            console.print(f"[bold red]Analyse failed:[/] {exc}")


# ---------------------------------------------------------------------------
# prune subcommand
# ---------------------------------------------------------------------------


@cli.command("prune")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Bypass the safety check that refuses to prune more than 20% of the "
        "index in a single pass. Use only when you really did delete that "
        "much of your library — otherwise fix [library] music_dir in "
        "config first."
    ),
)
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to operate on (default: [index] name, which is 'default' unless set).",
)
@click.pass_context
def cmd_prune(
    ctx: click.Context,
    force: bool,
    index_name: str | None,
) -> None:
    """Remove indexed entries whose audio files no longer exist on disk.

    Useful after deleting, moving, or renaming files in your music library.
    Auto-prune also runs at the start of every ``autodj index`` run, so
    you usually do not need to invoke this directly.

    \b
    Examples:
      uv run autodj prune
      uv run autodj prune --force        # bypass safety threshold
    """
    from autodj.indexer import PruneSafetyError, prune_index

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])

    _apply_index_name(cfg, index_name)

    try:
        removed, kept = prune_index(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            allow_mass_prune=force,
        )
    except PruneSafetyError as exc:
        console.print(f"[bold red]Prune aborted (safety check):[/]\n{exc}")
        sys.exit(2)
    except Exception as exc:
        console.print(f"[bold red]Prune failed:[/] {exc}")
        sys.exit(1)

    if removed == 0 and kept == 0:
        console.print("[yellow]No index found — nothing to prune.[/]")
    elif removed == 0:
        console.print(f"[green]All {kept} indexed tracks present.[/] Nothing to prune.")
    else:
        console.print(f"[green]Pruned {removed} missing tracks.[/] {kept} tracks remain in index.")


# ---------------------------------------------------------------------------
# enrich subcommand
# ---------------------------------------------------------------------------


@cli.command("enrich")
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to operate on (default: [index] name, which is 'default' unless set).",
)
@click.pass_context
def cmd_enrich(ctx: click.Context, index_name: str | None) -> None:
    """Refresh the index's tags and key / mode from the beets database.

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

    _apply_index_name(cfg, index_name)

    if not cfg.library.beets_db:
        console.print("[bold red]No [library] beets_db in config — enrich requires beets.[/]")
        sys.exit(1)

    try:
        updated, total = enrich_from_beets(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            beets_db=cfg.library.beets_db,
        )
    except Exception as exc:
        console.print(f"[bold red]Enrich failed:[/] {exc}")
        sys.exit(1)

    if total == 0:
        console.print("[yellow]No index found.[/]")
    elif updated == 0:
        console.print(
            f"[green]Index already in sync with beets[/] ({total} entries scanned, 0 changed)."
        )
    else:
        console.print(f"[green]Updated {updated} of {total} tracks[/] from beets.")


# ---------------------------------------------------------------------------
# analyse subcommand
# ---------------------------------------------------------------------------


@cli.command("analyse")
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to operate on (default: [index] name, which is 'default' unless set).",
)
@click.option(
    "--limit",
    default=None,
    type=click.IntRange(min=1),
    help="Analyse at most this many of the tracks that need it.",
)
@click.pass_context
def cmd_analyse(
    ctx: click.Context,
    index_name: str | None,
    limit: int | None,
) -> None:
    """Backfill DJ-meta (intro/outro/beat grid/cues) for indexed tracks.

    Walks the existing FAISS index and, for every entry whose
    ``dj_meta.db`` cache is missing or has ``analysed=False``, decodes
    the audio, runs :func:`autodj.dj_meta.analyse_audio`, merges cues
    imported from DJ software (``[playback] import_external_cues``), and
    writes the result.  Skips entries already analysed so repeated runs
    are cheap.  Rows of tracks no longer in the index are deleted.

    No GPU and no MuQ model required -- pure CPU work via librosa +
    numpy.  Run this on the NAS / listening host after a GPU host has
    finished the embedding pass; transition fades will then use the
    real per-track outro_start_s / intro_end_s instead of falling back
    to crossfade_seconds.

    \b
    Examples:
      uv run autodj analyse
      uv run autodj analyse --name workout
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

    _apply_index_name(cfg, index_name)

    from autodj.index_manifest import IndexConsistencyError
    from autodj.indexer import backfill_dj_meta, load_index

    try:
        entries, _, _ = load_index(cfg.index.active_dir, music_dir=cfg.library.music_dir)
    except FileNotFoundError:
        console.print(
            f"[bold red]No index at {cfg.index.active_dir}.[/]  Run `autodj index` first."
        )
        sys.exit(1)
    except IndexConsistencyError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)

    console.print(Panel("[bold green]AutoDJ DJ-meta backfill[/]", expand=False))
    console.print(f"  Index dir : {cfg.index.active_dir}")
    console.print(f"  Tracks    : {len(entries)}")
    if limit is not None:
        console.print(f"  Limit     : {limit}")
    console.print()

    try:
        backfill_dj_meta(cfg, entries, limit=limit)
    except Exception as exc:
        console.print(f"[bold red]Analyse failed:[/] {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# serve subcommand
# ---------------------------------------------------------------------------


@cli.command("serve")
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
        "prints the addresses to open and a pairing code."
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
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to play from (default: [index] name, which is 'default' unless set).",
)
@click.option(
    "--server-audio/--no-server-audio",
    "server_audio",
    default=False,
    help=(
        "Play audio from the server process.  Off by default: "
        "the browser is the audio output so skipping / volume / device "
        "changes only touch the local browser, never the server thread."
    ),
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
    index_name: str | None,
    server_audio: bool,
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

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
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
    selected_index_name = cfg.index.name
    if index_name is not None:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        selected_index_name = index_name
    from autodj.index_manifest import IndexConsistencyError

    try:
        sim = _load_index_for_serve(cfg, active_dir=cfg.index.index_dir / selected_index_name)
    except IndexConsistencyError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)
    if (
        staged_server.insecure_lan
        and staged_server.access_token is None
        and not is_loopback_bind(staged_server.host)
    ):
        console.print("[yellow]WARNING: LAN access is unauthenticated (--insecure-lan).[/]")
    _print_serve_banner(console, sim)
    seed_entry = _resolve_seed(sim, cfg, seed, console)
    url = _print_serve_url_banner(console, host, port, staged_server.ssl_certfile is not None)
    cfg.server = staged_server
    if stream is not None:
        cfg.stream.enabled = stream
    _apply_index_name(cfg, index_name)
    if stream is not None or server_cli_override:
        _append_cli_source(cfg)
    _require_ffmpeg_for_stream(cfg)
    if open_browser:
        import threading
        import webbrowser

        # Small delay so uvicorn is ready before the browser tries to connect
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    try:
        serve(
            cfg=cfg,
            sim=sim,
            seed_entry=seed_entry,
            host=host,
            port=port,
            no_playback=not server_audio,
            stream=cfg.stream.enabled,
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
            "\n[dim]* = system default.  Set [playback] audio_device to an index or name.[/]"
        )


# ---------------------------------------------------------------------------
# list-indexes subcommand
# ---------------------------------------------------------------------------


@cli.command("list-indexes")
@click.pass_context
def cmd_list_indexes(ctx: click.Context) -> None:  # pragma: no cover -- filesystem walk
    """List every named index found under ``[index] index_dir``.

    Shows each index name and its track count.  Use this to remember
    what indexes you have built — e.g. after running
    ``autodj index --name workout`` and ``autodj index --name chill``.

    \b
    Examples:
      uv run autodj list-indexes
    """
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    base = cfg.index.index_dir
    if not base.exists():
        console.print(f"[yellow]No indexes found at[/] {base}")
        return
    rows = _scan_index_rows(base, cfg.index.name)
    if not rows:
        console.print(
            f"[yellow]No named indexes found under[/] {base}\n"
            "Run [bold]autodj index --name <name>[/] to build one.",
        )
        return
    console.print(f"[bold]Indexes under[/] {base}  [dim](* = active)[/]\n")
    for name, status, path in rows:
        console.print(f"  {name:24s}  {status:18s}  [dim]{path}[/dim]")


# ---------------------------------------------------------------------------
# stats subcommand
# ---------------------------------------------------------------------------


@cli.command("stats")
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to inspect (default: [index] name, which is 'default' unless set).",
)
@click.pass_context
def cmd_stats(ctx: click.Context, index_name: str | None) -> None:
    """Print a statistical overview of the indexed music library.

    Loads the index (not the model) and displays BPM distribution, genres,
    decades, track lengths, top artists, key distribution, major/minor
    split, and energy histogram.

    \b
    Examples:
      uv run autodj stats
    """
    from autodj.index_manifest import IndexConsistencyError
    from autodj.indexer import load_index
    from autodj.stats import print_stats

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])

    _apply_index_name(cfg, index_name)

    try:
        entries, _, _ = load_index(cfg.index.active_dir, music_dir=cfg.library.music_dir)
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)
    except IndexConsistencyError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)

    print_stats(entries, console)
