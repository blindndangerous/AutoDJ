"""AutoDJ command-line interface.

Entry point for all AutoDJ commands.  Install the package with ``uv sync``
and then run:

.. code-block:: bash

    uv run autodj index          # build or update the music library index
    uv run autodj play           # start playing music

See each command's ``--help`` for full option documentation.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Force UTF-8 output on Windows (default terminal encoding is cp1252 which
# cannot print Unicode box-drawing characters or em-dashes used in track names).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import click
from rich.console import Console
from rich.panel import Panel

console = Console()


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
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable debug logging.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """AutoDJ — AI-powered local music continuity player.

    Indexes your music library using MERT audio embeddings and plays songs
    in a continuous flow based on sonic similarity.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        level=level,
        stream=sys.stderr,
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
@click.pass_context
def cmd_index(ctx: click.Context, limit: Optional[int], force: bool) -> None:
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
    from autodj.model import download_model_if_needed, load_model
    from autodj.indexer import build_index

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    console.print(Panel("[bold green]AutoDJ Indexer[/]", expand=False))
    console.print(f"  Music dir  : {cfg.library.music_dir}")
    console.print(f"  Beets DB   : {cfg.library.beets_db or '(not configured)'}")
    console.print(f"  Index dir  : {cfg.index.index_dir}")
    console.print(f"  Model      : {cfg.model.name}")
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
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Print track selections without playing audio. "
        "Useful for testing the similarity engine without speakers."
    ),
)
@click.pass_context
def cmd_play(
    ctx: click.Context,
    seed: Optional[str],
    crossfade_seconds: Optional[float],
    no_repeat_window: Optional[int],
    dry_run: bool,
) -> None:
    """Start the auto-DJ playback loop.

    Loads the library index and plays songs continuously, choosing each next
    track based on sonic similarity to the current one.

    \b
    Controls while playing:
      Space  — pause / resume
      N      — skip to next track
      Q      — quit

    \b
    Examples:
      uv run autodj play
      uv run autodj play --seed "Portishead"
      uv run autodj play --crossfade 5 --no-repeat 100
      uv run autodj play --dry-run
    """
    from autodj.config import load_config
    from autodj.similarity import SimilarityIndex
    from autodj.player import Player

    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as exc:
        console.print(f"[bold red]Config not found:[/] {exc}")
        sys.exit(1)

    # Apply session overrides
    if crossfade_seconds is not None:
        cfg.playback.crossfade_seconds = crossfade_seconds
    if no_repeat_window is not None:
        cfg.playback.no_repeat_window = no_repeat_window

    # Load index
    try:
        sim = SimilarityIndex.from_index_dir(cfg.index.index_dir)
    except FileNotFoundError as exc:
        console.print(f"[bold red]Index not found:[/] {exc}")
        sys.exit(1)

    console.print(
        Panel(
            f"[bold green]AutoDJ[/] — {sim.ntotal} tracks indexed",
            expand=False,
        )
    )

    # Resolve seed entry
    seed_entry = None
    if seed:
        candidates = []
        if cfg.library.beets_db and cfg.library.beets_db.exists():
            from autodj.beets import search_tracks as _search
            beets_results = _search(cfg.library.beets_db, seed)
            # Filter to only indexed paths
            indexed_paths = {e.path for e in sim.entries}
            candidates = [t for t in beets_results if str(t.path) in indexed_paths]
        else:
            # Fall back to searching within index entries directly
            q = seed.lower()
            from autodj.indexer import IndexEntry
            matching = [
                e for e in sim.entries
                if q in e.title.lower() or q in e.artist.lower()
            ]
            # Convert IndexEntry → pseudo-Track for display
            candidates = matching  # type: ignore[assignment]

        if not candidates:
            console.print(f"[yellow]No indexed tracks match '{seed}'. Starting random.[/]")
        elif len(candidates) == 1:
            chosen = candidates[0]
            console.print(f"Seed: [bold]{getattr(chosen, 'display_name', str(chosen))}[/]")
            # Locate the IndexEntry matching this path
            path_str = str(getattr(chosen, "path", chosen))
            seed_entry = next(
                (e for e in sim.entries if e.path == path_str), None
            )
        else:
            console.print(f"\nMultiple matches for '{seed}':")
            for i, c in enumerate(candidates[:10], 1):
                name = getattr(c, "display_name", str(c))
                console.print(f"  {i}. {name}")
            try:
                choice = click.prompt(
                    "Choose (number)", type=click.IntRange(1, min(len(candidates), 10))
                )
                chosen = candidates[choice - 1]
                path_str = str(getattr(chosen, "path", chosen))
                seed_entry = next(
                    (e for e in sim.entries if e.path == path_str), None
                )
            except (click.Abort, EOFError):
                console.print("[yellow]Cancelled — starting random.[/]")

    player = Player(cfg, sim, dry_run=dry_run)
    try:
        player.run(seed_entry=seed_entry)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")
