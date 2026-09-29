"""Library statistics display for AutoDJ.

Reads the index metadata and renders a Rich overview of the library:
BPM distribution, top genres, decade breakdown, track-length histogram,
top artists, key distribution, major/minor split, and energy histogram.

No MuQ model is needed.  ``autodj stats`` loads the index with
:func:`autodj.indexer.load_index`, which also reads and checks the vectors.

Example:
    >>> from autodj.stats import print_stats
    >>> from autodj.indexer import load_index
    >>> entries, _, _ = load_index(index_dir)
    >>> print_stats(entries, console)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
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
_KEY_NAMES = [
    "C",
    "C sharp",
    "D",
    "D sharp",
    "E",
    "F",
    "F sharp",
    "G",
    "G sharp",
    "A",
    "A sharp",
    "B",
]
_BPM_LABELS = [f"{lo} to {lo + 9}" for lo in range(60, 180, 10)] + ["180+", "Unknown"]
_LENGTH_LABELS = ["Under 2 min", "2 to 5 min", "5 to 10 min", "Over 10 min"]
_ENERGY_LABELS = [
    "0.00 to 0.05 (silence)",
    "0.05 to 0.15 (quiet)",
    "0.15 to 0.30 (medium)",
    "0.30 to 0.50 (loud)",
    "0.50+ (very loud)",
]


def _bar(count: int, max_count: int, width: int = _BAR_WIDTH) -> str:
    """Return a text bar of *width* characters proportional to count/max."""
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


def _plural(count: int, noun: str) -> str:
    """Return ``"1 track"`` or ``"3 tracks"`` style text."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _fmt_duration_words(total_seconds: float) -> str:
    """Format total seconds as ``"X hours Y minutes"`` for plain output."""
    hours, rem = divmod(int(total_seconds), 3600)
    mins = _plural(rem // 60, "minute")
    return f"{_plural(hours, 'hour')} {mins}" if hours else mins


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _print_histogram(
    console: Console,
    title: str,
    label_col: str,
    rows: list[tuple[str, int]],
    min_width: int = 0,
    denom: int | None = None,
    total: int | None = None,
) -> None:
    """Render one histogram; prints nothing when *rows* is empty.

    On a terminal this is a label/bar/count table.  Anywhere else, such as
    the web UI's job log, each row is a sentence with a count and a
    percentage, because screen readers read the bar glyphs as noise.

    Args:
        console: Rich console to print to.
        title: Table title.
        label_col: Name of the (header-less) label column.
        rows: ``(label, count)`` pairs in display order.
        min_width: Minimum width of the label column, 0 for automatic.
        denom: Count that maps to a full bar.  Defaults to the largest count.
        total: Count that percentages are taken of.  Defaults to the sum of
            the row counts.
    """
    if not rows:
        return
    whole = total if total is not None else sum(c for _, c in rows)

    def pct(count: int) -> int:
        return round(count * 100 / whole) if whole else 0

    if not console.is_terminal:
        console.print(f"{title}:", markup=False, highlight=False)
        for label, count in rows:
            console.print(
                f"  {label}: {_plural(count, 'track')}, {pct(count)} percent",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        return
    top = denom if denom is not None else max(c for _, c in rows)
    tbl = Table(title=title, show_header=False, box=None, padding=(0, 1))
    if min_width:
        tbl.add_column(label_col, style="dim", min_width=min_width)
    else:
        tbl.add_column(label_col, style="dim")
    tbl.add_column("Bar")
    tbl.add_column("Count", justify="right", style="cyan")
    tbl.add_column("Percent", justify="right", style="dim")
    for label, count in rows:
        tbl.add_row(label, _bar(count, top), str(count), f"{pct(count)}%")
    console.print(tbl)


def _bpm_bucket(bpm: float) -> str:
    """Return the histogram-bucket label for *bpm*."""
    if bpm <= 0:
        return "Unknown"
    if bpm >= 180:
        return "180+"
    lo = max(60, min(int(bpm // 10) * 10, 180))
    return f"{lo} to {lo + 9}"


def _length_bucket(seconds: float) -> str:
    """Return the track-length bucket label for *seconds*."""
    if seconds < 120:
        return "Under 2 min"
    if seconds < 300:
        return "2 to 5 min"
    if seconds < 600:
        return "5 to 10 min"
    return "Over 10 min"


def _energy_bucket(energy: float) -> str:
    """Return the energy bucket label for *energy*."""
    if energy < 0.05:
        return "0.00 to 0.05 (silence)"
    if energy < 0.15:
        return "0.05 to 0.15 (quiet)"
    if energy < 0.30:
        return "0.15 to 0.30 (medium)"
    if energy < 0.50:
        return "0.30 to 0.50 (loud)"
    return "0.50+ (very loud)"


def _print_summary(entries: list[IndexEntry], console: Console) -> None:
    """Render the top summary panel (track count + total play time)."""
    n = len(entries)
    total_secs = sum(e.length for e in entries)
    if not console.is_terminal:
        console.print(
            f"AutoDJ library stats: {_plural(n, 'track')}, "
            f"{_fmt_duration_words(total_secs)} total play time.",
            markup=False,
            highlight=False,
        )
        return
    console.print(
        Panel(
            f"[bold green]{n:,}[/bold green] tracks  ·  "
            f"[bold]{_fmt_duration(total_secs)}[/bold] total play time",
            title="[bold blue]AutoDJ Library Stats[/bold blue]",
            expand=False,
        )
    )


def _bucket_rows(labels: list[str], keys: Iterable[str]) -> list[tuple[str, int]]:
    """Count *keys* into *labels* order, dropping ``Unknown`` when it is empty.

    Args:
        labels: Every bucket label, in display order.
        keys: One bucket label per entry.

    Returns:
        ``(label, count)`` rows in *labels* order.
    """
    counts = Counter(keys)
    return [(lb, counts[lb]) for lb in labels if counts[lb] or lb != "Unknown"]


def _top_rows(values: Iterable[str]) -> list[tuple[str, int]]:
    """Return the ten most common non-blank *values* as ``(value, count)`` rows."""
    counts = Counter(v.strip() for v in values if v and v.strip())
    return counts.most_common(10)


def _decade_rows(entries: list[IndexEntry]) -> list[tuple[str, int]]:
    """Return ``(decade, count)`` rows, oldest first, with ``Unknown`` last."""
    counts = Counter(
        f"{(e.year // 10) * 10}s" if e.year and e.year >= 1900 else "Unknown" for e in entries
    )
    labels = sorted(k for k in counts if k != "Unknown")
    if "Unknown" in counts:
        labels.append("Unknown")
    return [(lb, counts[lb]) for lb in labels]


def _key_rows(entries: list[IndexEntry]) -> list[tuple[str, int]]:
    """Return one row per chromatic key, or nothing when no key was detected."""
    counts = Counter(e.key for e in entries if e.key >= 0)
    if not counts:
        return []
    return [(_KEY_NAMES[k], counts[k]) for k in range(12)]


def _mode_rows(entries: list[IndexEntry]) -> list[tuple[str, int]]:
    """Return the major and minor rows, or nothing when no mode was detected."""
    major = sum(1 for e in entries if e.mode == 1)
    minor = sum(1 for e in entries if e.mode == 0)
    if not major + minor:
        return []
    return [("Major", major), ("Minor", minor)]


def print_stats(entries: list[IndexEntry], console: Console) -> None:
    """Print a Rich library overview to *console*."""
    if not entries:
        console.print("[yellow]No tracks in index.[/yellow]")
        return
    _print_summary(entries, console)
    _print_histogram(
        console,
        "BPM Distribution",
        "Range",
        _bucket_rows(_BPM_LABELS, (_bpm_bucket(e.bpm) for e in entries)),
        min_width=8,
    )
    n = len(entries)
    _print_histogram(console, "Top Genres", "Genre", _top_rows(e.genre for e in entries), total=n)
    _print_histogram(console, "By Decade", "Decade", _decade_rows(entries), min_width=8)
    _print_histogram(
        console,
        "Track Lengths",
        "Bucket",
        _bucket_rows(_LENGTH_LABELS, (_length_bucket(e.length) for e in entries)),
    )
    _print_histogram(
        console, "Top Artists", "Artist", _top_rows(e.artist for e in entries), total=n
    )
    _print_histogram(console, "Key Distribution", "Key", _key_rows(entries), min_width=3)
    mode_rows = _mode_rows(entries)
    _print_histogram(console, "Mode Split", "Mode", mode_rows, denom=sum(c for _, c in mode_rows))
    _print_histogram(
        console,
        "Energy Distribution",
        "Range",
        _bucket_rows(_ENERGY_LABELS, (_energy_bucket(e.energy) for e in entries)),
    )
