"""Library statistics display for AutoDJ.

Reads the index metadata and renders a Rich overview of the library:
BPM distribution, top genres, decade breakdown, track-length histogram,
top artists, key distribution, major/minor split, and energy histogram.

No FAISS index or MuQ model is needed — only ``metadata.json`` is read.

Example:
    >>> from autodj.stats import print_stats
    >>> from autodj.indexer import load_index
    >>> entries, _ = load_index(index_dir)
    >>> print_stats(entries, console)
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BAR_WIDTH = 18
_FILLED = "█"
_EMPTY = "░"
_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _bar(count: int, max_count: int, width: int = _BAR_WIDTH) -> str:
    """Return an ASCII bar of *width* characters proportional to count/max."""
    if max_count == 0:
        return _EMPTY * width
    filled = round(count * width / max_count)
    return _FILLED * filled + _EMPTY * (width - filled)


def _fmt_duration(total_seconds: float) -> str:
    """Format total seconds as ``Xh Ym``."""
    hours, rem = divmod(int(total_seconds), 3600)
    mins = rem // 60
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def print_stats(entries: list[IndexEntry], console: Console) -> None:
    """Print a Rich library overview to *console*.

    Sections displayed:
    - Summary: track count, total play time
    - BPM distribution histogram
    - Top 10 genres
    - Decade breakdown
    - Track-length buckets
    - Top 10 artists
    - Key distribution
    - Major/minor split
    - Energy histogram

    Args:
        entries: List of :class:`~autodj.indexer.IndexEntry` objects from
            the index.
        console: Rich :class:`~rich.console.Console` to write to.
    """
    if not entries:
        console.print("[yellow]No tracks in index.[/yellow]")
        return

    n = len(entries)
    total_secs = sum(e.length for e in entries)

    console.print(
        Panel(
            f"[bold green]{n:,}[/bold green] tracks  ·  "
            f"[bold]{_fmt_duration(total_secs)}[/bold] total play time",
            title="[bold blue]AutoDJ Library Stats[/bold blue]",
            expand=False,
        )
    )

    # ----------------------------------------------------------------
    # BPM distribution
    # ----------------------------------------------------------------
    bpm_buckets: dict[str, int] = {}
    for lo in range(60, 190, 10):
        bpm_buckets[f"{lo}–{lo + 9}"] = 0
    bpm_buckets["180+"] = 0
    bpm_buckets["Unknown"] = 0

    for e in entries:
        b = e.bpm
        if b <= 0:
            bpm_buckets["Unknown"] += 1
        elif b >= 180:
            bpm_buckets["180+"] += 1
        else:
            lo = int(b // 10) * 10
            lo = max(60, min(lo, 180))
            key = f"{lo}–{lo + 9}"
            if key in bpm_buckets:
                bpm_buckets[key] += 1
            else:
                bpm_buckets["Unknown"] += 1

    max_bpm = max(bpm_buckets.values(), default=1)
    tbl = Table(title="BPM Distribution", show_header=False, box=None, padding=(0, 1))
    tbl.add_column("Range", style="dim", min_width=8)
    tbl.add_column("Bar")
    tbl.add_column("Count", justify="right", style="cyan")
    for label, count in bpm_buckets.items():
        if count or label not in ("Unknown",):
            tbl.add_row(label, _bar(count, max_bpm), str(count))
    console.print(tbl)

    # ----------------------------------------------------------------
    # Top genres
    # ----------------------------------------------------------------
    genre_counts: Counter[str] = Counter(
        e.genre.strip() for e in entries if e.genre and e.genre.strip()
    )
    if genre_counts:
        tbl = Table(title="Top Genres", show_header=False, box=None, padding=(0, 1))
        tbl.add_column("Genre", style="dim")
        tbl.add_column("Bar")
        tbl.add_column("Count", justify="right", style="cyan")
        top_count = genre_counts.most_common(1)[0][1]
        for genre, count in genre_counts.most_common(10):
            tbl.add_row(genre, _bar(count, top_count), str(count))
        console.print(tbl)

    # ----------------------------------------------------------------
    # Decade breakdown
    # ----------------------------------------------------------------
    decade_counts: dict[str, int] = {}
    unknown_decade = 0
    for e in entries:
        y = e.year
        if not y or y < 1900:
            unknown_decade += 1
        else:
            dec = (y // 10) * 10
            label = f"{dec}s"
            decade_counts[label] = decade_counts.get(label, 0) + 1
    if unknown_decade:
        decade_counts["Unknown"] = unknown_decade

    if decade_counts:
        max_dec = max(decade_counts.values(), default=1)
        tbl = Table(title="By Decade", show_header=False, box=None, padding=(0, 1))
        tbl.add_column("Decade", style="dim", min_width=8)
        tbl.add_column("Bar")
        tbl.add_column("Count", justify="right", style="cyan")
        for label in sorted(k for k in decade_counts if k != "Unknown") + (
            ["Unknown"] if "Unknown" in decade_counts else []
        ):
            tbl.add_row(label, _bar(decade_counts[label], max_dec), str(decade_counts[label]))
        console.print(tbl)

    # ----------------------------------------------------------------
    # Track length buckets
    # ----------------------------------------------------------------
    length_buckets = {"< 2 min": 0, "2–5 min": 0, "5–10 min": 0, "> 10 min": 0}
    for e in entries:
        s = e.length
        if s < 120:
            length_buckets["< 2 min"] += 1
        elif s < 300:
            length_buckets["2–5 min"] += 1
        elif s < 600:
            length_buckets["5–10 min"] += 1
        else:
            length_buckets["> 10 min"] += 1

    max_len = max(length_buckets.values(), default=1)
    tbl = Table(title="Track Lengths", show_header=False, box=None, padding=(0, 1))
    tbl.add_column("Bucket", style="dim")
    tbl.add_column("Bar")
    tbl.add_column("Count", justify="right", style="cyan")
    for label, count in length_buckets.items():
        tbl.add_row(label, _bar(count, max_len), str(count))
    console.print(tbl)

    # ----------------------------------------------------------------
    # Top artists
    # ----------------------------------------------------------------
    artist_counts: Counter[str] = Counter(
        e.artist.strip() for e in entries if e.artist and e.artist.strip()
    )
    if artist_counts:
        tbl = Table(title="Top Artists", show_header=False, box=None, padding=(0, 1))
        tbl.add_column("Artist", style="dim")
        tbl.add_column("Bar")
        tbl.add_column("Count", justify="right", style="cyan")
        top_artist = artist_counts.most_common(1)[0][1]
        for artist, count in artist_counts.most_common(10):
            tbl.add_row(artist, _bar(count, top_artist), str(count))
        console.print(tbl)

    # ----------------------------------------------------------------
    # Key distribution
    # ----------------------------------------------------------------
    key_counts: dict[int, int] = {}
    for e in entries:
        k = e.key
        if k >= 0:
            key_counts[k] = key_counts.get(k, 0) + 1

    if key_counts:
        max_key = max(key_counts.values(), default=1)
        tbl = Table(title="Key Distribution", show_header=False, box=None, padding=(0, 1))
        tbl.add_column("Key", style="dim", min_width=3)
        tbl.add_column("Bar")
        tbl.add_column("Count", justify="right", style="cyan")
        for k in range(12):
            count = key_counts.get(k, 0)
            tbl.add_row(_KEY_NAMES[k], _bar(count, max_key), str(count))
        console.print(tbl)

    # Major / minor split
    major = sum(1 for e in entries if e.mode == 1)
    minor = sum(1 for e in entries if e.mode == 0)
    mode_total = major + minor
    if mode_total:
        major_pct = round(major * 100 / mode_total)
        minor_pct = 100 - major_pct
        tbl = Table(title="Mode Split", show_header=False, box=None, padding=(0, 1))
        tbl.add_column("Mode", style="dim")
        tbl.add_column("Bar")
        tbl.add_column("Count", justify="right", style="cyan")
        tbl.add_row(f"Major ({major_pct}%)", _bar(major, mode_total), str(major))
        tbl.add_row(f"Minor ({minor_pct}%)", _bar(minor, mode_total), str(minor))
        console.print(tbl)

    # Energy histogram
    energy_buckets = {
        "0.00–0.05 (silence)": 0,
        "0.05–0.15 (quiet)": 0,
        "0.15–0.30 (medium)": 0,
        "0.30–0.50 (loud)": 0,
        "0.50+ (very loud)": 0,
    }
    for e in entries:
        eng = e.energy
        if eng < 0.05:
            energy_buckets["0.00–0.05 (silence)"] += 1
        elif eng < 0.15:
            energy_buckets["0.05–0.15 (quiet)"] += 1
        elif eng < 0.30:
            energy_buckets["0.15–0.30 (medium)"] += 1
        elif eng < 0.50:
            energy_buckets["0.30–0.50 (loud)"] += 1
        else:
            energy_buckets["0.50+ (very loud)"] += 1

    max_eng = max(energy_buckets.values(), default=1)
    tbl = Table(title="Energy Distribution", show_header=False, box=None, padding=(0, 1))
    tbl.add_column("Range", style="dim")
    tbl.add_column("Bar")
    tbl.add_column("Count", justify="right", style="cyan")
    for label, count in energy_buckets.items():
        tbl.add_row(label, _bar(count, max_eng), str(count))
    console.print(tbl)
