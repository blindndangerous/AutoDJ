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
from copy import deepcopy
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
from rich.panel import Panel

from autodj.config import TRANSITION_MODES
from autodj.dj_meta import HARMONIC_MODES
from autodj.stream_secret import paired_devices_path
from autodj.transitions import TRANSITION_EFFECT_NAMES

# Sorted so `--help` lists the effects in a stable order.  Derived from the
# enum so the CLI can never offer fewer effects than the web UI.
_TRANSITION_CHOICES = sorted(TRANSITION_EFFECT_NAMES)
# Declaration order, not sorted: --help must keep listing the modes in the
# order config.TRANSITION_MODES declares them.
_TRANSITION_MODE_CHOICES = list(TRANSITION_MODES)


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


def _parse_bpm_range(value: str) -> tuple[float, float]:
    """Parse a BPM range string like ``"90-130"`` into ``(90.0, 130.0)``.

    Accepts both ASCII hyphen ``-`` and en-dash ``–`` as separators.

    Args:
        value: Range string, e.g. ``"90-130"`` or ``"90–130"``.

    Returns:
        ``(lo, hi)`` floats.

    Raises:
        click.BadParameter: If the string cannot be parsed.
    """
    # Allow en-dash as well as regular hyphen
    normalized = value.replace("\u2013", "-").replace("\u2014", "-")
    parts = normalized.split("-")
    if len(parts) != 2:
        raise click.BadParameter(
            f"BPM range must be in the format 'MIN-MAX', e.g. '90-130'. Got: '{value}'"
        )
    try:
        lo, hi = float(parts[0]), float(parts[1])
    except ValueError as err:
        raise click.BadParameter(f"BPM range values must be numbers. Got: '{value}'") from err
    if lo >= hi:
        raise click.BadParameter(f"BPM range MIN must be less than MAX. Got: {lo}–{hi}")
    return lo, hi


def _resolve_seed(
    sim: SimilarityIndex,
    cfg: AutoDJConfig,
    seed: str | None,
    console_: Console,
    interactive: bool = True,
) -> IndexEntry | None:
    """Resolve a seed string to an :class:`~autodj.indexer.IndexEntry`.

    Args:
        sim: Loaded :class:`~autodj.similarity.SimilarityIndex`.
        cfg: Full :class:`~autodj.config.AutoDJConfig`.
        seed: User-supplied search term, or ``None`` for no seed.
        console_: Rich console for printing messages.
        interactive: If ``True`` and multiple matches exist, prompt the user
            to choose.  If ``False``, silently take the first match.

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

    if len(candidates) == 1 or not interactive:
        chosen = candidates[0]
        display = getattr(chosen, "display_name", str(chosen))
        console_.print(f"Seed: [bold]{display}[/bold]")
        path_str = str(getattr(chosen, "path", chosen))
        return next((e for e in entries if e.path == path_str), None)

    console_.print(f"\nMultiple matches for '{seed}':")
    for i, c in enumerate(candidates[:10], 1):
        name = getattr(c, "display_name", str(c))
        console_.print(f"  {i}. {name}")
    try:
        choice = click.prompt("Choose (number)", type=click.IntRange(1, min(len(candidates), 10)))
        chosen = candidates[choice - 1]
        path_str = str(getattr(chosen, "path", chosen))
        return next((e for e in entries if e.path == path_str), None)
    except (click.Abort, EOFError):
        console_.print("[yellow]Cancelled — starting random.[/yellow]")
        return None


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


def _load_index_or_exit(
    cfg: AutoDJConfig, *, active_dir: Path | None = None
) -> SimilarityIndex:  # pragma: no cover
    """Load the similarity index for *cfg*, exiting when it is missing or too old."""
    from autodj.index_manifest import IndexConsistencyError
    from autodj.similarity import SimilarityIndex as _SI

    try:
        return _SI.from_index_dir(
            cfg.index.active_dir if active_dir is None else active_dir,
            music_dir=cfg.library.music_dir,
        )
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)
    except IndexConsistencyError as exc:
        # Covers UnsupportedIndexError, whose message already names the rebuild.
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)


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


def _resolve_preset_or_exit(cfg: AutoDJConfig, preset: str | None) -> Any:  # pragma: no cover
    """Resolve *preset* by name, exiting on ValueError; ``None`` when not requested."""
    if preset is None:
        return None
    from autodj.presets import get_preset

    try:
        return get_preset(preset, cfg.presets)
    except ValueError as exc:
        console.print(f"[bold red]Unknown preset:[/] {exc}")
        sys.exit(1)


def _parse_bpm_range_or_exit(
    bpm_range: str | None,
) -> tuple[float, float] | None:  # pragma: no cover
    """Parse ``--bpm-range`` or exit on click.BadParameter."""
    if bpm_range is None:
        return None
    try:
        return _parse_bpm_range(bpm_range)
    except click.BadParameter as exc:
        console.print(f"[bold red]Invalid --bpm-range:[/] {exc}")
        sys.exit(1)


def _scan_index_rows(
    base: Path, active_name: str
) -> list[tuple[str, int, str]]:  # pragma: no cover
    """Walk *base* for indexed-library directories; return display rows."""
    import sqlite3

    rows: list[tuple[str, int, str]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        db_path = entry / "tracks.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(db_path)
            try:
                count = int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            count = -1
        active_marker = "  *" if entry.name == active_name else "   "
        rows.append((active_marker + entry.name, count, str(entry)))
    return rows


def _can_import(name: str) -> bool:
    """Return True when *name* imports cleanly."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _apply_serve_overrides(
    cfg: AutoDJConfig, kw: dict
) -> bool:  # pragma: no cover -- exercised by smoke tests
    """Apply CLI overrides for ``serve`` onto *cfg* in place.

    *kw* is the local mapping captured at the top of ``cmd_serve``;
    every key matches a click option name.
    """
    djmix_keys = (
        "harmonic_mode",
        "beatmatch",
        "phrase_align",
        "outro_intro_align",
        "filter_sweep",
    )
    playback_keys = (
        "enable_daypart",
        "enable_mood_arc",
        "import_external_cues",
        "beat_sync_fx",
        "key_sync_fx",
        "show_lyrics",
    )
    validated_transition_mode: str | None = None
    if kw.get("transition_mode") is not None:
        from autodj.config import _validate_transition_mode

        try:
            validated_transition_mode = _validate_transition_mode(kw["transition_mode"])
        except ValueError as exc:
            console.print(f"[bold red]Invalid --transition-mode:[/] {exc}")
            sys.exit(1)

    changed = False
    for section, keys in ((cfg.djmix, djmix_keys), (cfg.playback, playback_keys)):
        for key in keys:
            if kw.get(key) is not None and getattr(section, key) != kw[key]:
                setattr(section, key, kw[key])
                changed = True
    if kw.get("mood_arc_hours") is not None:
        value = max(0.25, float(kw["mood_arc_hours"]))
        if cfg.playback.mood_arc_hours != value:
            cfg.playback.mood_arc_hours = value
            changed = True
    if kw.get("transition_fx") is not None:
        value = kw["transition_fx"]
        if cfg.transitions.effect != value:
            cfg.transitions.effect = value
            changed = True
    if (
        validated_transition_mode is not None
        and cfg.playback.transition_mode != validated_transition_mode
    ):
        cfg.playback.transition_mode = validated_transition_mode
        changed = True
    return changed


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
    console_: Console,
    *,
    sim: SimilarityIndex,
    resolved_preset: Any,
    parsed_bpm_range: tuple[float, float] | None,
    discovery_every: int | None,
) -> None:  # pragma: no cover -- terminal banner
    """Print the index summary + active preset / BPM / discovery banner."""
    console_.print(
        Panel(
            f"[bold green]AutoDJ[/] — {sim.ntotal} tracks indexed",
            expand=False,
        )
    )
    if resolved_preset:
        console_.print(f"  Preset     : {resolved_preset.name}")
    if parsed_bpm_range:
        console_.print(f"  BPM range  : {parsed_bpm_range[0]:.0f}–{parsed_bpm_range[1]:.0f}")
    if discovery_every:
        console_.print(f"  Discovery  : every {discovery_every} tracks")


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
    """List and revoke browsers paired with this AutoDJ instance."""


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
    """Write the index, DJ metadata, web settings, liners, profiles and history to a ZIP file.

    Safe while AutoDJ is serving.  config.toml, config.local.toml and
    presets.toml are not included; copy them yourself.
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
        "Skip refreshing beets ``initial_key`` data after indexing (done by "
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
    from autodj.compute import gpu_available

    device = "CUDA (GPU)" if gpu_available() else "CPU"

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
    help="Named index to operate on (default: 'default').",
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
    help="Named index to operate on (default: 'default').",
)
@click.pass_context
def cmd_enrich(ctx: click.Context, index_name: str | None) -> None:
    """Refresh the index's key / mode from beets ``initial_key`` data.

    Walks every entry in the existing index, looks it up in your beets
    database, parses the ``initial_key`` field (set by the beets
    keyfinder plugin), and replaces the librosa-detected key with the
    beets value when one is present.  No re-embedding required —
    completes in seconds even for huge libraries.

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
        console.print(f"[green]Updated key/mode on {updated} of {total} tracks[/] from beets.")


# ---------------------------------------------------------------------------
# analyse subcommand
# ---------------------------------------------------------------------------


@cli.command("analyse")
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to operate on (default: 'default').",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Stop after this many tracks (test mode).",
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
    are cheap.

    No GPU and no MuQ model required -- pure CPU work via librosa +
    numpy.  Run this on the NAS / listening host after a GPU host has
    finished the embedding pass; transition fades will then use the
    real per-track outro_start_s / intro_end_s instead of falling back
    to the bar-rounded defaults.

    \b
    Examples:
      uv run autodj analyse
      uv run autodj analyse --name workout
      uv run autodj analyse --limit 100   # smoke-test on a small batch
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
    if limit is not None:
        entries = entries[:limit]

    console.print(Panel("[bold green]AutoDJ DJ-meta backfill[/]", expand=False))
    console.print(f"  Index dir : {cfg.index.active_dir}")
    console.print(f"  Tracks    : {len(entries)}")
    console.print()

    try:
        backfill_dj_meta(cfg, entries)
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
    "--preset",
    default=None,
    type=str,
    help="BPM-shaping preset name (e.g. wakeup, chill, party).",
)
@click.option(
    "--export-m3u",
    "export_m3u",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Write a live M3U playlist to this file as tracks play.",
)
@click.option(
    "--bpm-range",
    "bpm_range",
    default=None,
    type=str,
    help="Hard BPM filter, e.g. '90-130'. Tracks outside this range are excluded.",
)
@click.option(
    "--discovery-every",
    "discovery_every",
    default=None,
    type=int,
    help=(
        "Inject a sonically distant track every N tracks. "
        "Toggle via the discovery button in the web UI."
    ),
)
@click.option(
    "--history-file",
    "history_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Append a JSON Lines play history entry for every track played.",
)
@click.option(
    "--smart-shuffle",
    is_flag=True,
    default=False,
    help="Pick the most sonically DISTANT next track instead of the closest.",
)
@click.option(
    "--daypart/--no-daypart",
    "enable_daypart",
    default=None,
    help="Pick BPM/energy targets from local time of day.",
)
@click.option(
    "--mood-arc/--no-mood-arc",
    "enable_mood_arc",
    default=None,
    help="Set-relative warmup -> peak -> cool envelope.",
)
@click.option(
    "--mood-arc-hours",
    type=float,
    default=None,
    help="Length of the mood-arc envelope in hours.  Default 3.",
)
@click.option(
    "--import-external-cues/--no-import-external-cues",
    "import_external_cues",
    default=None,
    help="Import cues from Mixxx / Rekordbox / Traktor libraries and Serato file tags.",
)
@click.option(
    "--beat-sync-fx/--no-beat-sync-fx",
    "beat_sync_fx",
    default=None,
    help="Snap rhythmic transition FX to the beat grid + size to whole bars.",
)
@click.option(
    "--key-sync-fx/--no-key-sync-fx",
    "key_sync_fx",
    default=None,
    help="Tune oscillator FX (pitch_swell, dub_siren, ...) to song root note.",
)
@click.option(
    "--harmonic-mode",
    "harmonic_mode",
    default=None,
    type=click.Choice(HARMONIC_MODES, case_sensitive=False),
    help="Harmonic-mixing rule for next-track picks; 'off' disables it.",
)
@click.option(
    "--transition-mode",
    "transition_mode",
    default=None,
    type=click.Choice(_TRANSITION_MODE_CHOICES, case_sensitive=False),
    help="Crossfade alignment mode for the web-UI auto-DJ.",
)
@click.option(
    "--beatmatch/--no-beatmatch",
    default=None,
    help="Pitch-stretch incoming track to match outgoing BPM during crossfade.",
)
@click.option(
    "--phrase-align/--no-phrase-align",
    default=None,
    help="Snap crossfade start to nearest 8-bar phrase boundary.",
)
@click.option(
    "--align-outro/--no-align-outro",
    "outro_intro_align",
    default=None,
    help="Crossfade between detected outro of A and intro of B.",
)
@click.option(
    "--filter-sweep/--no-filter-sweep",
    default=None,
    help="Low-pass sweep on outgoing tail during crossfade.",
)
@click.option(
    "--transition",
    "transition_fx",
    default=None,
    type=click.Choice(_TRANSITION_CHOICES, case_sensitive=False),
    help="Transition effect layered on every crossfade.",
)
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to play from (default: 'default').",
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
    "--pure-shuffle",
    is_flag=True,
    default=False,
    help=(
        "Random walk — uniformly random next pick, ignores similarity.  "
        "Toggle off mid-set to seed similarity from the current song."
    ),
)
@click.option(
    "--anchor-seed/--no-anchor-seed",
    "anchor_to_seed",
    default=None,
    help=(
        "Each next pick stays similar to the SEED, not the last track.  "
        "Prevents drift through chained similarity hops."
    ),
)
@click.option(
    "--show-lyrics/--no-show-lyrics",
    "show_lyrics",
    default=None,
    help=(
        "Show LRC / plain lyrics in the web UI.  Overrides "
        "[playback] show_lyrics in config.toml.  Default: on."
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
    preset: str | None,
    export_m3u: str | None,
    bpm_range: str | None,
    discovery_every: int | None,
    history_file: str | None,
    smart_shuffle: bool,
    pure_shuffle: bool,
    anchor_to_seed: bool | None,
    show_lyrics: bool | None,
    enable_daypart: bool | None,
    enable_mood_arc: bool | None,
    mood_arc_hours: float | None,
    import_external_cues: bool | None,
    beat_sync_fx: bool | None,
    key_sync_fx: bool | None,
    harmonic_mode: str | None,
    beatmatch: bool | None,
    phrase_align: bool | None,
    outro_intro_align: bool | None,
    filter_sweep: bool | None,
    transition_fx: str | None,
    transition_mode: str | None,
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
      uv run autodj serve --preset wakeup --discovery-every 10
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
    staged_override_cfg = deepcopy(cfg)
    general_cli_override = _apply_serve_overrides(staged_override_cfg, locals())
    resolved_preset = _resolve_preset_or_exit(cfg, preset)
    parsed_bpm_range = _parse_bpm_range_or_exit(bpm_range)
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
    _print_serve_banner(
        console,
        sim=sim,
        resolved_preset=resolved_preset,
        parsed_bpm_range=parsed_bpm_range,
        discovery_every=discovery_every,
    )
    seed_entry = _resolve_seed(sim, cfg, seed, console, interactive=False)
    url = _print_serve_url_banner(console, host, port, staged_server.ssl_certfile is not None)
    cfg.djmix = staged_override_cfg.djmix
    cfg.playback = staged_override_cfg.playback
    cfg.transitions = staged_override_cfg.transitions
    cfg.server = staged_server
    if stream is not None:
        cfg.stream.enabled = stream
        general_cli_override = True
    _apply_index_name(cfg, index_name)
    if general_cli_override or server_cli_override:
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
            preset=resolved_preset,
            export_m3u=Path(export_m3u) if export_m3u else None,
            history_file=Path(history_file) if history_file else cfg.playback.history_file,
            discovery_every=discovery_every
            if discovery_every is not None
            else cfg.playback.discovery_every,
            bpm_range=parsed_bpm_range,
            smart_shuffle=smart_shuffle,
            pure_shuffle=pure_shuffle,
            anchor_to_seed=bool(anchor_to_seed),
            no_playback=not server_audio,
            stream=cfg.stream.enabled,
            lan_configured_hosts=lan_configured_hosts,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")


# ---------------------------------------------------------------------------
# playlist subcommand
# ---------------------------------------------------------------------------


@cli.command("playlist")
@click.option(
    "--seed",
    default=None,
    type=str,
    help="Search term to choose the starting track. Omit for a random start.",
)
@click.option(
    "--tracks",
    "n_tracks",
    default=20,
    show_default=True,
    type=int,
    help="Number of tracks to include in the playlist.",
)
@click.option(
    "--preset",
    default=None,
    type=str,
    help="BPM-shaping preset name (e.g. wakeup, chill, party).",
)
@click.option(
    "--bpm-range",
    "bpm_range",
    default=None,
    type=str,
    help="Hard BPM filter, e.g. '90-130'. Tracks outside this range are excluded.",
)
@click.option(
    "--output",
    "output_file",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Write M3U playlist to this file. Prints to stdout if omitted.",
)
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to draw tracks from (default: 'default').",
)
@click.pass_context
def cmd_playlist(
    ctx: click.Context,
    seed: str | None,
    n_tracks: int,
    preset: str | None,
    bpm_range: str | None,
    output_file: str | None,
    index_name: str | None,
) -> None:
    """Generate an offline M3U playlist using the similarity engine.

    Simulates the auto-DJ selection logic for N tracks without playing audio.
    Useful for previewing what a session would look like or generating playlists
    for use in other players.

    \b
    Examples:
      uv run autodj playlist --tracks 30 --output morning.m3u
      uv run autodj playlist --seed "Portishead" --tracks 20
      uv run autodj playlist --preset wakeup --bpm-range 80-150 --output wakeup.m3u
    """
    import random
    from collections import deque

    from autodj.player import write_m3u

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _apply_index_name(cfg, index_name)
    sim = _load_index_or_exit(cfg)
    resolved_preset = _resolve_preset_or_exit(cfg, preset)
    parsed_bpm_range = _parse_bpm_range_or_exit(bpm_range)

    seed_entry = _resolve_seed(sim, cfg, seed, console, interactive=True)

    # Build playlist by simulating the selection loop
    playlist: list = []
    recently_played: deque = deque(maxlen=cfg.playback.no_repeat_window)

    similarity = sim
    # Start with seed or random
    if seed_entry is not None:
        current = seed_entry
    else:
        # Non-security playlist seeding — random.choice is fine here.
        current = random.choice(similarity.entries_snapshot())  # nosec B311

    playlist.append(current)
    recently_played.append(current.path)

    for track_number in range(1, n_tracks):
        try:
            target_bpm = None
            bpm_weight = 0.2
            if resolved_preset:
                target_bpm = resolved_preset.target_bpm(track_number)
                bpm_weight = resolved_preset.bpm_weight

            current = sim.find_next_for_path(
                current.path,
                recently_played,
                target_bpm=target_bpm,
                bpm_weight=bpm_weight,
                bpm_range=parsed_bpm_range,
                pick_top_k=cfg.playback.pick_top_k,
                pick_temperature=cfg.playback.pick_temperature,
            )
            playlist.append(current)
            recently_played.append(current.path)
        except Exception as exc:
            console.print(f"[yellow]Stopping early: {exc}[/]")
            break

    if output_file:
        out_path = Path(output_file)
        write_m3u(playlist, out_path)
        console.print(f"[green]Wrote {len(playlist)} tracks to {out_path}[/]")
    else:
        # Print M3U to stdout
        print("#EXTM3U")
        for entry in playlist:
            dur = int(entry.length) if entry.length else -1
            display = entry.display_name
            print(f"#EXTINF:{dur},{display}")
            print(entry.path)


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
    for name, count, path in rows:
        count_str = f"{count} tracks" if count >= 0 else "[red]corrupt[/red]"
        console.print(f"  {name:24s}  {count_str:18s}  [dim]{path}[/dim]")


# ---------------------------------------------------------------------------
# stats subcommand
# ---------------------------------------------------------------------------


@cli.command("stats")
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to inspect (default: 'default').",
)
@click.pass_context
def cmd_stats(ctx: click.Context, index_name: str | None) -> None:
    """Print a statistical overview of the indexed music library.

    Loads only the metadata index (no FAISS vectors, no model) and displays
    BPM distribution, genres, decades, track lengths, and top artists.
    If the library has been enriched (via 'autodj enrich'), also shows
    key distribution, major/minor split, and energy histogram.

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
