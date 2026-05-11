"""Library statistics display for AutoDJ.

Reads the index metadata and renders a Rich overview of the library:
BPM distribution, top genres, decade breakdown, track-length histogram,
top artists, key distribution, major/minor split, and energy histogram.

No FAISS index or MuQ model is needed — only ``tracks.db`` is read.

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


def _make_table(title: str, label_col: str, min_width: int = 0) -> Table:
    """Build a 3-column histogram table (label, bar, count)."""
    tbl = Table(title=title, show_header=False, box=None, padding=(0, 1))
    if min_width:
        tbl.add_column(label_col, style="dim", min_width=min_width)
    else:
        tbl.add_column(label_col, style="dim")
    tbl.add_column("Bar")
    tbl.add_column("Count", justify="right", style="cyan")
    return tbl


def _bpm_bucket(bpm: float) -> str:
    """Return the histogram-bucket label for *bpm*."""
    if bpm <= 0:
        return "Unknown"
    if bpm >= 180:
        return "180+"
    lo = max(60, min(int(bpm // 10) * 10, 180))
    return f"{lo}–{lo + 9}"


def _length_bucket(seconds: float) -> str:
    """Return the track-length bucket label for *seconds*."""
    if seconds < 120:
        return "< 2 min"
    if seconds < 300:
        return "2–5 min"
    if seconds < 600:
        return "5–10 min"
    return "> 10 min"


def _energy_bucket(energy: float) -> str:
    """Return the energy bucket label for *energy*."""
    if energy < 0.05:
        return "0.00–0.05 (silence)"
    if energy < 0.15:
        return "0.05–0.15 (quiet)"
    if energy < 0.30:
        return "0.15–0.30 (medium)"
    if energy < 0.50:
        return "0.30–0.50 (loud)"
    return "0.50+ (very loud)"


def _print_summary(entries: list[IndexEntry], console: Console) -> None:
    """Render the top summary panel (track count + total play time)."""
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


def _print_bpm(entries: list[IndexEntry], console: Console) -> None:
    """Render the BPM-distribution histogram."""
    buckets: dict[str, int] = {f"{lo}–{lo + 9}": 0 for lo in range(60, 190, 10)}
    buckets["180+"] = 0
    buckets["Unknown"] = 0
    for e in entries:
        key = _bpm_bucket(e.bpm)
        buckets[key] = buckets.get(key, 0) + 1
    max_bpm = max(buckets.values(), default=1)
    tbl = _make_table("BPM Distribution", "Range", min_width=8)
    for label, count in buckets.items():
        if count or label != "Unknown":
            tbl.add_row(label, _bar(count, max_bpm), str(count))
    console.print(tbl)


def _print_genres(entries: list[IndexEntry], console: Console) -> None:
    """Render the top-10 genre histogram (skipped when no genre tags)."""
    counts: Counter[str] = Counter(e.genre.strip() for e in entries if e.genre and e.genre.strip())
    if not counts:
        return
    tbl = _make_table("Top Genres", "Genre")
    top = counts.most_common(1)[0][1]
    for genre, count in counts.most_common(10):
        tbl.add_row(genre, _bar(count, top), str(count))
    console.print(tbl)


def _print_decades(entries: list[IndexEntry], console: Console) -> None:
    """Render the by-decade histogram (skipped when no year tags)."""
    counts: dict[str, int] = {}
    unknown = 0
    for e in entries:
        if not e.year or e.year < 1900:
            unknown += 1
        else:
            label = f"{(e.year // 10) * 10}s"
            counts[label] = counts.get(label, 0) + 1
    if unknown:
        counts["Unknown"] = unknown
    if not counts:
        return
    max_dec = max(counts.values(), default=1)
    tbl = _make_table("By Decade", "Decade", min_width=8)
    ordered = sorted(k for k in counts if k != "Unknown")
    if "Unknown" in counts:
        ordered.append("Unknown")
    for label in ordered:
        tbl.add_row(label, _bar(counts[label], max_dec), str(counts[label]))
    console.print(tbl)


def _print_lengths(entries: list[IndexEntry], console: Console) -> None:
    """Render the track-length-bucket histogram."""
    buckets = {"< 2 min": 0, "2–5 min": 0, "5–10 min": 0, "> 10 min": 0}
    for e in entries:
        buckets[_length_bucket(e.length)] += 1
    max_len = max(buckets.values(), default=1)
    tbl = _make_table("Track Lengths", "Bucket")
    for label, count in buckets.items():
        tbl.add_row(label, _bar(count, max_len), str(count))
    console.print(tbl)


def _print_artists(entries: list[IndexEntry], console: Console) -> None:
    """Render the top-10 artists histogram (skipped when no artist tags)."""
    counts: Counter[str] = Counter(
        e.artist.strip() for e in entries if e.artist and e.artist.strip()
    )
    if not counts:
        return
    tbl = _make_table("Top Artists", "Artist")
    top = counts.most_common(1)[0][1]
    for artist, count in counts.most_common(10):
        tbl.add_row(artist, _bar(count, top), str(count))
    console.print(tbl)


def _print_keys(entries: list[IndexEntry], console: Console) -> None:
    """Render the chromatic-key histogram (skipped when no detected keys)."""
    counts: dict[int, int] = {}
    for e in entries:
        if e.key >= 0:
            counts[e.key] = counts.get(e.key, 0) + 1
    if not counts:
        return
    max_key = max(counts.values(), default=1)
    tbl = _make_table("Key Distribution", "Key", min_width=3)
    for k in range(12):
        count = counts.get(k, 0)
        tbl.add_row(_KEY_NAMES[k], _bar(count, max_key), str(count))
    console.print(tbl)


def _print_modes(entries: list[IndexEntry], console: Console) -> None:
    """Render the major/minor split (skipped when no mode tags)."""
    major = sum(1 for e in entries if e.mode == 1)
    minor = sum(1 for e in entries if e.mode == 0)
    total = major + minor
    if not total:
        return
    major_pct = round(major * 100 / total)
    minor_pct = 100 - major_pct
    tbl = _make_table("Mode Split", "Mode")
    tbl.add_row(f"Major ({major_pct}%)", _bar(major, total), str(major))
    tbl.add_row(f"Minor ({minor_pct}%)", _bar(minor, total), str(minor))
    console.print(tbl)


def _print_energy(entries: list[IndexEntry], console: Console) -> None:
    """Render the energy-bucket histogram."""
    buckets = {
        "0.00–0.05 (silence)": 0,
        "0.05–0.15 (quiet)": 0,
        "0.15–0.30 (medium)": 0,
        "0.30–0.50 (loud)": 0,
        "0.50+ (very loud)": 0,
    }
    for e in entries:
        buckets[_energy_bucket(e.energy)] += 1
    max_eng = max(buckets.values(), default=1)
    tbl = _make_table("Energy Distribution", "Range")
    for label, count in buckets.items():
        tbl.add_row(label, _bar(count, max_eng), str(count))
    console.print(tbl)


def print_stats(entries: list[IndexEntry], console: Console) -> None:
    """Print a Rich library overview to *console*."""
    if not entries:
        console.print("[yellow]No tracks in index.[/yellow]")
        return
    _print_summary(entries, console)
    _print_bpm(entries, console)
    _print_genres(entries, console)
    _print_decades(entries, console)
    _print_lengths(entries, console)
    _print_artists(entries, console)
    _print_keys(entries, console)
    _print_modes(entries, console)
    _print_energy(entries, console)
