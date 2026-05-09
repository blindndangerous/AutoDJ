"""AutoDJ command-line interface.

Entry point for all AutoDJ commands.  Install the package with ``uv sync``
and then run:

.. code-block:: bash

    uv run autodj index          # build or update the music library index
    uv run autodj play           # start playing music

See each command's ``--help`` for full option documentation.
"""

from __future__ import annotations

import io
import logging
import os
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
from rich.panel import Panel

if TYPE_CHECKING:
    from autodj.beets import Track
    from autodj.config import AutoDJConfig
    from autodj.indexer import IndexEntry
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

    candidates: list[Track | IndexEntry] = []

    if cfg.library.beets_db and cfg.library.beets_db.exists():
        from autodj.beets import search_tracks as _search

        beets_results = _search(cfg.library.beets_db, seed)
        indexed_paths = {e.path for e in sim.entries}
        candidates = [t for t in beets_results if str(t.path) in indexed_paths]

    if not candidates:
        q = seed.lower()
        candidates = [e for e in sim.entries if q in e.title.lower() or q in e.artist.lower()]

    if not candidates:
        console_.print(f"[yellow]No indexed tracks match '{seed}'. Starting random.[/yellow]")
        return None

    if len(candidates) == 1 or not interactive:
        chosen = candidates[0]
        display = getattr(chosen, "display_name", str(chosen))
        console_.print(f"Seed: [bold]{display}[/bold]")
        path_str = str(getattr(chosen, "path", chosen))
        return next((e for e in sim.entries if e.path == path_str), None)

    console_.print(f"\nMultiple matches for '{seed}':")
    for i, c in enumerate(candidates[:10], 1):
        name = getattr(c, "display_name", str(c))
        console_.print(f"  {i}. {name}")
    try:
        choice = click.prompt("Choose (number)", type=click.IntRange(1, min(len(candidates), 10)))
        chosen = candidates[choice - 1]
        path_str = str(getattr(chosen, "path", chosen))
        return next((e for e in sim.entries if e.path == path_str), None)
    except (click.Abort, EOFError):
        console_.print("[yellow]Cancelled — starting random.[/yellow]")
        return None


def _apply_serve_overrides(
    cfg: AutoDJConfig, kw: dict
) -> None:  # pragma: no cover -- exercised by smoke tests
    """Apply CLI overrides for ``serve`` onto *cfg* in place.

    *kw* is the local mapping captured at the top of ``cmd_serve``;
    every key matches a click option name.
    """
    djmix_keys = {
        "harmonic_mixing": "harmonic_mixing",
        "beatmatch": "beatmatch",
        "phrase_align": "phrase_align",
        "outro_intro_align": "outro_intro_align",
        "filter_sweep": "filter_sweep",
    }
    playback_keys = {
        "enable_daypart": "enable_daypart",
        "enable_mood_arc": "enable_mood_arc",
        "import_external_cues": "import_external_cues",
        "beat_sync_fx": "beat_sync_fx",
        "key_sync_fx": "key_sync_fx",
        "show_lyrics": "show_lyrics",
    }
    for src_key, dst_key in djmix_keys.items():
        if kw.get(src_key) is not None:
            setattr(cfg.djmix, dst_key, kw[src_key])
    for src_key, dst_key in playback_keys.items():
        if kw.get(src_key) is not None:
            setattr(cfg.playback, dst_key, kw[src_key])
    if kw.get("mood_arc_hours") is not None:
        cfg.playback.mood_arc_hours = max(0.25, float(kw["mood_arc_hours"]))
    if kw.get("transition_fx") is not None:
        cfg.transitions.effect = kw["transition_fx"]
    if kw.get("transition_mode") is not None:
        from autodj.config import _validate_transition_mode

        try:
            cfg.playback.transition_mode = _validate_transition_mode(kw["transition_mode"])
        except ValueError as exc:
            console.print(f"[bold red]Invalid --transition-mode:[/] {exc}")
            sys.exit(1)


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
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> str:  # pragma: no cover -- terminal banner
    """Print the web-UI URL + reachability hint; return the URL."""
    if (ssl_certfile and not ssl_keyfile) or (ssl_keyfile and not ssl_certfile):
        console_.print(
            "[bold red]TLS error:[/] --ssl-certfile and --ssl-keyfile must be supplied together.",
        )
        sys.exit(1)
    scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
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
            "Use [bold]--host 0.0.0.0[/] to expose on your LAN.)[/]",
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
    default="config.toml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the AutoDJ config file.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable debug logging (otherwise info / warning / error are shown).",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
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
        "Named index to write to.  Files land at "
        "<index_dir>/<NAME>/vectors.index etc.  Use this to keep multiple "
        "curated libraries side-by-side (e.g. 'workout', 'chill').  "
        "Default: 'default' (or [index] name in config.toml)."
    ),
)
@click.pass_context
def cmd_index(
    ctx: click.Context,
    limit: int | None,
    force: bool,
    index_name: str | None,
) -> None:
    """Build or update the FAISS index for the music library.

    Reads track metadata from the beets database (if configured) or scans
    the filesystem.  Tracks already in the index are skipped unless --force
    is passed.

    Run with --limit 20 first to confirm the pipeline works before indexing
    your full library:

    \b
        uv run autodj index --limit 20
        uv run autodj index           # full library (run overnight on GPU machine)
    """
    from autodj.config import load_config
    from autodj.indexer import build_index

    # Indexing requires torch + muq + librosa.  Probe before doing any
    # other work so users on minimal installs (NAS, Docker) get a clear
    # message instead of a deep stack trace from inside `model.py`.
    missing = []
    for name in ("torch", "muq", "librosa", "soundfile"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        console.print(
            f"[bold red]Cannot index — missing packages: {', '.join(missing)}[/]\n"
            "Install indexing dependencies with:\n"
            "  [bold]uv sync --extra index[/]\n"
            "or for the whole kit:\n"
            "  [bold]uv sync --extra all[/]\n\n"
            "Indexing runs on CPU automatically when no NVIDIA GPU is present —\n"
            "fine for small batches (use --limit 50 to try it on a NAS first)."
        )
        sys.exit(1)

    from autodj.model import download_model_if_needed, load_model

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    # Detect compute device + warn about CPU performance for big libraries.
    import torch as _torch

    device = "CUDA (GPU)" if _torch.cuda.is_available() else "CPU"

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
    console.print()

    try:
        model_path = download_model_if_needed(cfg.model, cfg.index, hf_token=cfg.huggingface.token)
        wrapper = load_model(model_path)
        build_index(cfg, wrapper=wrapper, limit=limit, force=force)
    except Exception as exc:
        console.print(f"[bold red]Indexing failed:[/] {exc}")
        sys.exit(1)


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
        "much of your library — otherwise fix [library] music_dir / "
        "path_remap in config first."
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
def cmd_prune(ctx: click.Context, force: bool, index_name: str | None) -> None:
    """Remove indexed entries whose audio files no longer exist on disk.

    Useful after deleting, moving, or renaming files in your music library.
    Auto-prune also runs at the start of every ``autodj index`` run, so
    you usually do not need to invoke this directly.

    \b
    Examples:
      uv run autodj prune
      uv run autodj prune --force        # bypass safety threshold
    """
    from autodj.config import load_config
    from autodj.indexer import PruneSafetyError, prune_index

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    try:
        removed, kept = prune_index(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
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

    Useful when you've used keyfinder / DJ taggers AFTER your first
    ``autodj index`` run and want the harmonic-mixing engine to use
    those higher-quality keys.

    \b
    Examples:
      uv run autodj enrich
    """
    from autodj.config import load_config
    from autodj.indexer import enrich_from_beets

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    if not cfg.library.beets_db:
        console.print("[bold red]No [library] beets_db in config — enrich requires beets.[/]")
        sys.exit(1)

    try:
        updated, total = enrich_from_beets(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            beets_db=cfg.library.beets_db,
            path_remap=cfg.library.path_remap,
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
# play subcommand
# ---------------------------------------------------------------------------


@cli.command("play")
@click.option(
    "--seed",
    default=None,
    type=str,
    help=(
        "Search term to choose the starting track (matched against title and artist). "
        "If multiple tracks match, an interactive selection prompt is shown. "
        "Omit to start from a random track."
    ),
)
@click.option(
    "--crossfade",
    "crossfade_seconds",
    default=None,
    type=float,
    help="Override the crossfade duration in seconds for this session.",
)
@click.option(
    "--no-repeat",
    "no_repeat_window",
    default=None,
    type=int,
    help="Override the recently-played exclusion window for this session.",
)
@click.option(
    "--preset",
    default=None,
    type=str,
    help=(
        "BPM-shaping preset name (e.g. wakeup, chill, party). "
        "Built-in presets: wakeup, winddown, sleep, morning, slide, party, workout, chill, focus, driving. "
        "User presets can be defined in config.toml under [presets.NAME]."
    ),
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
        "Press D while playing to toggle discovery on/off."
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
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Print track selections without playing audio. "
        "Useful for testing the similarity engine without speakers."
    ),
)
@click.option(
    "--smart-shuffle",
    is_flag=True,
    default=False,
    help=(
        "Pick the most sonically DISTANT next track instead of the closest. "
        "Genuinely surprising sequences, opposite of the default DJ behaviour."
    ),
)
@click.option(
    "--pure-shuffle",
    is_flag=True,
    default=False,
    help=(
        "Pick a uniformly random next track (Random walk), ignoring "
        "similarity entirely.  Toggle off mid-set and similarity resumes "
        "from the current song."
    ),
)
@click.option(
    "--anchor-seed/--no-anchor-seed",
    "anchor_to_seed",
    default=None,
    help=(
        "Each next pick stays similar to the SEED track, not the last "
        "track that played.  Prevents drift through chained hops."
    ),
)
@click.option(
    "--show-lyrics/--no-show-lyrics",
    "show_lyrics",
    default=None,
    help=(
        "Show LRC / plain lyrics in the CLI panel and web UI.  Overrides "
        "[playback] show_lyrics in config.toml.  Default: on."
    ),
)
@click.option(
    "--daypart/--no-daypart",
    "enable_daypart",
    default=None,
    help=(
        "Pick BPM/energy targets from the current local time of day "
        "(morning/midday/afternoon/evening/night).  Only active when no "
        "explicit --preset and no --mood-arc are set."
    ),
)
@click.option(
    "--mood-arc/--no-mood-arc",
    "enable_mood_arc",
    default=None,
    help=(
        "Set-relative warmup -> peak -> cool envelope.  Anchored to the "
        "current time; loops every --mood-arc-hours."
    ),
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
    help=(
        "Auto-import cue points from Mixxx / Rekordbox / Traktor "
        "libraries on first cache load.  Default: on."
    ),
)
@click.option(
    "--beat-sync-fx/--no-beat-sync-fx",
    "beat_sync_fx",
    default=None,
    help=(
        "Snap rhythmic transition FX (echo, stutter, beat_repeat, "
        "sidechain_pump, halftime, scratch, ...) to the beat grid + "
        "size to whole bars at a blended outgoing->incoming tempo.  "
        "Default: on."
    ),
)
@click.option(
    "--key-sync-fx/--no-key-sync-fx",
    "key_sync_fx",
    default=None,
    help=(
        "Tune oscillator-based FX (pitch_swell, pitch_fall, dub_siren, "
        "ring_modulator, air_horn) to the song's root note, lerping "
        "outgoing -> incoming across the fade.  Default: on."
    ),
)
@click.option(
    "--harmonic/--no-harmonic",
    "harmonic_mixing",
    default=None,
    help="Restrict picks to Camelot-compatible keys (overrides config).",
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
    help="Auto-detect outro of outgoing + intro of incoming and crossfade between them.",
)
@click.option(
    "--filter-sweep/--no-filter-sweep",
    default=None,
    help="Apply a low-pass sweep on the outgoing tail during crossfade.",
)
@click.option(
    "--transition",
    "transition_fx",
    default=None,
    type=click.Choice(
        [
            "none",
            "echo_out",
            "reverb_tail",
            "highpass_sweep",
            "lowpass_sweep",
            "tape_stop",
            "gate_stutter",
            "noise_riser",
            "noise_drop",
            "backspin",
            "forward_spin",
            "cross_eq_swap",
            "bitcrusher",
            "flanger",
            "pitch_swell",
            "telephone",
            "chorus",
            "submerge",
            "vinyl_wow",
            "freeze",
            "glitch",
            "scratch",
            "beat_repeat",
            "sidechain_pump",
            "reverse_reverb",
            "air_horn",
            "random",
            "rotate",
        ],
        case_sensitive=False,
    ),
    help="Transition effect layered on every crossfade (overrides config).",
)
@click.option(
    "--transition-mode",
    "transition_mode",
    default=None,
    type=click.Choice(
        ["full_intro_outro", "outro_fade", "fixed_skip_silence", "fixed"],
        case_sensitive=False,
    ),
    help=(
        "How the crossfade aligns with each track's intro/outro markers. "
        "Mirrors Mixxx's AutoDJ TransitionMode.  Overrides [playback] "
        "transition_mode."
    ),
)
@click.option(
    "--name",
    "index_name",
    default=None,
    type=str,
    help="Named index to play from (default: 'default').",
)
@click.option(
    "--device",
    "audio_device",
    default=None,
    type=str,
    help=(
        "Audio output device — index (e.g. '4') or substring of the "
        "device name (e.g. 'USB Headphones').  Run "
        "[bold]autodj list-devices[/] to see options.  Overrides "
        "[playback] audio_device."
    ),
)
@click.pass_context
def cmd_play(  # pragma: no cover -- end-to-end orchestrator, exercised by smoke tests
    ctx: click.Context,
    seed: str | None,
    crossfade_seconds: float | None,
    no_repeat_window: int | None,
    preset: str | None,
    export_m3u: str | None,
    bpm_range: str | None,
    discovery_every: int | None,
    history_file: str | None,
    dry_run: bool,
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
    harmonic_mixing: bool | None,
    beatmatch: bool | None,
    phrase_align: bool | None,
    outro_intro_align: bool | None,
    filter_sweep: bool | None,
    transition_fx: str | None,
    transition_mode: str | None,
    index_name: str | None,
    audio_device: str | None,
) -> None:
    """Start the auto-DJ playback loop.

    Loads the library index and plays songs continuously, choosing each next
    track based on sonic similarity to the current one.

    \b
    Controls while playing:
      Space  — pause / resume
      N      — skip to next track
      D      — toggle discovery mode (requires --discovery-every)
      Q      — quit

    \b
    Examples:
      uv run autodj play
      uv run autodj play --seed "Portishead"
      uv run autodj play --preset wakeup
      uv run autodj play --bpm-range 90-130 --discovery-every 10
      uv run autodj play --export-m3u session.m3u --history-file history.jsonl
      uv run autodj play --dry-run
    """
    from autodj.config import load_config
    from autodj.player import Player
    from autodj.similarity import SimilarityIndex

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    # Apply session overrides
    if crossfade_seconds is not None:
        cfg.playback.crossfade_seconds = crossfade_seconds
    if no_repeat_window is not None:
        cfg.playback.no_repeat_window = no_repeat_window
    if harmonic_mixing is not None:
        cfg.djmix.harmonic_mixing = harmonic_mixing
    if beatmatch is not None:
        cfg.djmix.beatmatch = beatmatch
    if phrase_align is not None:
        cfg.djmix.phrase_align = phrase_align
    if outro_intro_align is not None:
        cfg.djmix.outro_intro_align = outro_intro_align
    if filter_sweep is not None:
        cfg.djmix.filter_sweep = filter_sweep
    if enable_daypart is not None:
        cfg.playback.enable_daypart = enable_daypart
    if enable_mood_arc is not None:
        cfg.playback.enable_mood_arc = enable_mood_arc
    if mood_arc_hours is not None:
        cfg.playback.mood_arc_hours = max(0.25, float(mood_arc_hours))
    if import_external_cues is not None:
        cfg.playback.import_external_cues = import_external_cues
    if beat_sync_fx is not None:
        cfg.playback.beat_sync_fx = beat_sync_fx
    if key_sync_fx is not None:
        cfg.playback.key_sync_fx = key_sync_fx
    if transition_fx is not None:
        cfg.transitions.effect = transition_fx
    if transition_mode is not None:
        from autodj.config import _validate_transition_mode

        try:
            cfg.playback.transition_mode = _validate_transition_mode(transition_mode)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --transition-mode:[/] {exc}")
            sys.exit(1)
    if show_lyrics is not None:
        cfg.playback.show_lyrics = show_lyrics
    if audio_device is not None:
        # Try to coerce to int (sounddevice index), else keep as substring
        try:
            cfg.playback.audio_device = int(audio_device)
        except ValueError:
            cfg.playback.audio_device = audio_device

    # Load index
    try:
        sim = SimilarityIndex.from_index_dir(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
        )
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)

    # Resolve preset
    resolved_preset = None
    if preset:
        from autodj.presets import get_preset

        try:
            resolved_preset = get_preset(preset, cfg.presets)
        except ValueError as exc:
            console.print(f"[bold red]Unknown preset:[/] {exc}")
            sys.exit(1)

    # Parse BPM range
    parsed_bpm_range = None
    if bpm_range:
        try:
            parsed_bpm_range = _parse_bpm_range(bpm_range)
        except click.BadParameter as exc:
            console.print(f"[bold red]Invalid --bpm-range:[/] {exc}")
            sys.exit(1)

    console.print(
        Panel(
            f"[bold green]AutoDJ[/] — {sim.ntotal} tracks indexed",
            expand=False,
        )
    )
    if resolved_preset:
        console.print(f"  Preset     : {resolved_preset.name}")
    if parsed_bpm_range:
        console.print(f"  BPM range  : {parsed_bpm_range[0]:.0f}–{parsed_bpm_range[1]:.0f}")
    if discovery_every:
        console.print(f"  Discovery  : every {discovery_every} tracks (press D to toggle)")

    seed_entry = _resolve_seed(sim, cfg, seed, console)

    player = Player(
        cfg,
        sim,
        dry_run=dry_run,
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
    )
    # CLI play and the web `serve` UI are intentionally decoupled —
    # the web UI persists its own settings (`web_state.json`) which
    # the CLI ignores.  CLI behaviour is driven entirely by config +
    # CLI flags.  Two surfaces, two state stores, no surprise overrides.
    try:
        player.run(seed_entry=seed_entry)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")


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
    "--host",
    default="127.0.0.1",
    show_default=True,
    type=str,
    help="Interface to bind the web server to. Use 0.0.0.0 for LAN access.",
)
@click.option(
    "--port",
    default=8080,
    show_default=True,
    type=int,
    help="Port to bind the web server to.",
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
    help="Auto-import cues from Mixxx / Rekordbox / Traktor libraries.",
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
    "--harmonic/--no-harmonic",
    "harmonic_mixing",
    default=None,
    help="Restrict picks to Camelot-compatible keys.",
)
@click.option(
    "--transition-mode",
    "transition_mode",
    default=None,
    type=click.Choice(
        ["full_intro_outro", "outro_fade", "fixed_skip_silence", "fixed"],
        case_sensitive=False,
    ),
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
    type=click.Choice(
        [
            "none",
            "echo_out",
            "reverb_tail",
            "highpass_sweep",
            "lowpass_sweep",
            "tape_stop",
            "gate_stutter",
            "noise_riser",
            "noise_drop",
            "backspin",
            "forward_spin",
            "cross_eq_swap",
            "bitcrusher",
            "flanger",
            "pitch_swell",
            "telephone",
            "chorus",
            "submerge",
            "vinyl_wow",
            "freeze",
            "glitch",
            "scratch",
            "beat_repeat",
            "sidechain_pump",
            "reverse_reverb",
            "air_horn",
            "random",
            "rotate",
        ],
        case_sensitive=False,
    ),
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
    "--no-playback",
    "no_playback",
    is_flag=True,
    default=True,
    help=(
        "Run the web UI without server-side audio output.  Default — "
        "the browser handles all playback so the CLI player and web UI "
        "stay decoupled.  Pass --server-audio to opt back in."
    ),
)
@click.option(
    "--server-audio/--no-server-audio",
    "server_audio",
    default=False,
    help=(
        "Play audio from the server process (legacy mode).  Off by default: "
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
    "--ssl-certfile",
    "ssl_certfile",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Path to TLS certificate (PEM).  When combined with --ssl-keyfile, "
        "starts uvicorn in HTTPS mode — required for AudioWorklet on "
        "non-localhost hosts.  Generate via mkcert or any internal CA."
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
    host: str,
    port: int,
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
    harmonic_mixing: bool | None,
    beatmatch: bool | None,
    phrase_align: bool | None,
    outro_intro_align: bool | None,
    filter_sweep: bool | None,
    transition_fx: str | None,
    transition_mode: str | None,
    index_name: str | None,
    no_playback: bool,
    server_audio: bool,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    """Start the auto-DJ player with a browser-based control panel.

    Runs the AutoDJ player in the background and serves a web UI at
    http://HOST:PORT.  All playback controls (pause, skip, volume, mute,
    discovery) are available from the browser.  Keyboard controls continue
    to work in the terminal at the same time.

    \b
    Examples:
      uv run autodj serve
      uv run autodj serve --seed "Portishead" --open
      uv run autodj serve --host 0.0.0.0 --port 8080
      uv run autodj serve --preset wakeup --discovery-every 10
    """
    from autodj.config import load_config
    from autodj.server import serve
    from autodj.similarity import SimilarityIndex

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    try:
        sim = SimilarityIndex.from_index_dir(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
        )
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)

    # Resolve preset
    resolved_preset = None
    if preset:
        from autodj.presets import get_preset

        try:
            resolved_preset = get_preset(preset, cfg.presets)
        except ValueError as exc:
            console.print(f"[bold red]Unknown preset:[/] {exc}")
            sys.exit(1)

    # Parse BPM range
    parsed_bpm_range = None
    if bpm_range:
        try:
            parsed_bpm_range = _parse_bpm_range(bpm_range)
        except click.BadParameter as exc:
            console.print(f"[bold red]Invalid --bpm-range:[/] {exc}")
            sys.exit(1)

    _apply_serve_overrides(cfg, locals())
    _print_serve_banner(
        console,
        sim=sim,
        resolved_preset=resolved_preset,
        parsed_bpm_range=parsed_bpm_range,
        discovery_every=discovery_every,
    )
    seed_entry = _resolve_seed(sim, cfg, seed, console, interactive=False)
    url = _print_serve_url_banner(console, host, port, ssl_certfile, ssl_keyfile)
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
            no_playback=(no_playback and not server_audio),
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
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

    from autodj.config import load_config
    from autodj.player import write_m3u
    from autodj.similarity import SimilarityIndex

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    try:
        sim = SimilarityIndex.from_index_dir(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
        )
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)

    # Resolve preset
    resolved_preset = None
    if preset:
        from autodj.presets import get_preset

        try:
            resolved_preset = get_preset(preset, cfg.presets)
        except ValueError as exc:
            console.print(f"[bold red]Unknown preset:[/] {exc}")
            sys.exit(1)

    # Parse BPM range
    parsed_bpm_range = None
    if bpm_range:
        try:
            parsed_bpm_range = _parse_bpm_range(bpm_range)
        except click.BadParameter as exc:
            console.print(f"[bold red]Invalid --bpm-range:[/] {exc}")
            sys.exit(1)

    seed_entry = _resolve_seed(sim, cfg, seed, console, interactive=True)

    # Build playlist by simulating the selection loop
    playlist: list = []
    recently_played: deque = deque(maxlen=cfg.playback.no_repeat_window)

    # Start with seed or random
    if seed_entry is not None:
        current = seed_entry
    else:
        # Non-security playlist seeding — random.choice is fine here.
        current = random.choice(sim.entries)  # nosec B311

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

    Use the index or a substring of the name with ``--device`` on the
    ``play`` command, or set ``[playback] audio_device`` in
    ``config.toml`` for a permanent default.

    \b
    Examples:
      uv run autodj list-devices
      uv run autodj play --device "USB Headphones"
      uv run autodj play --device 4
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
            '\n[dim]* = system default.  Pass --device <index> or --device "<substring>".[/]'
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
    import json as _json

    from autodj.config import load_config

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    base = cfg.index.index_dir
    if not base.exists():
        console.print(f"[yellow]No indexes found at[/] {base}")
        return

    rows: list[tuple[str, int, str]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        meta = entry / "metadata.json"
        if not meta.exists():
            continue
        try:
            data = _json.loads(meta.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, list) else 0
        except (OSError, _json.JSONDecodeError):
            count = -1
        active_marker = "  *" if entry.name == cfg.index.name else "   "
        rows.append((active_marker + entry.name, count, str(entry)))

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
    from autodj.config import load_config
    from autodj.indexer import load_index
    from autodj.stats import print_stats

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    if index_name:
        from autodj.config import validate_index_name

        try:
            validate_index_name(index_name)
        except ValueError as exc:
            console.print(f"[bold red]Invalid --name:[/] {exc}")
            sys.exit(1)
        cfg.index.name = index_name

    try:
        entries, _ = load_index(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
        )
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)

    print_stats(entries, console)
