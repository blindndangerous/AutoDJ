"""Unit tests for autodj.stats.

All tests are pure — no filesystem I/O, no audio hardware, no model loading.
The Rich Console is directed to a StringIO buffer so output can be inspected
or simply verified not to raise.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from autodj.indexer import IndexEntry
from autodj.stats import _bar, _fmt_duration, print_stats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _console() -> Console:
    """Return a Console that writes to a StringIO buffer (no terminal needed)."""
    return Console(file=StringIO(), highlight=False, markup=False, width=120)


def _make_entry(**kwargs) -> IndexEntry:
    defaults = {
        "path": "Z:/Music/song.flac",
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "genre": "Rock",
        "bpm": 120.0,
        "year": 2000,
        "length": 180.0,
        "energy": 0.2,
        "key": 0,
        "mode": 1,
        "tempo_confidence": 0.8,
    }
    defaults.update(kwargs)
    return IndexEntry(**defaults)


# ---------------------------------------------------------------------------
# _bar
# ---------------------------------------------------------------------------


class TestBar:
    def test_fully_filled(self) -> None:
        bar = _bar(10, 10)
        assert all(c == "█" for c in bar)

    def test_fully_empty(self) -> None:
        bar = _bar(0, 10)
        assert all(c == "░" for c in bar)

    def test_zero_max_returns_empty_bar(self) -> None:
        """When max_count is 0, every character should be the empty block."""
        bar = _bar(5, 0)
        assert all(c == "░" for c in bar)

    def test_width_is_respected(self) -> None:
        assert len(_bar(5, 10, width=8)) == 8
        assert len(_bar(5, 10, width=20)) == 20

    def test_partial_fill_has_both_chars(self) -> None:
        bar = _bar(5, 10)
        assert "█" in bar
        assert "░" in bar

    def test_default_width(self) -> None:
        bar = _bar(5, 10)
        assert len(bar) == 18  # _BAR_WIDTH


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_zero_seconds(self) -> None:
        assert _fmt_duration(0) == "0m"

    def test_whole_minutes(self) -> None:
        assert _fmt_duration(300) == "5m"

    def test_less_than_one_minute(self) -> None:
        assert _fmt_duration(45) == "0m"

    def test_one_hour(self) -> None:
        assert _fmt_duration(3600) == "1h 0m"

    def test_hours_and_minutes(self) -> None:
        assert _fmt_duration(3661) == "1h 1m"

    def test_large_library(self) -> None:
        # 10 h 30 m
        assert _fmt_duration(10 * 3600 + 30 * 60) == "10h 30m"


# ---------------------------------------------------------------------------
# print_stats — smoke tests (verifies no crash; covers all branches)
# ---------------------------------------------------------------------------


class TestPrintStatsEmpty:
    def test_empty_list_does_not_raise(self) -> None:
        print_stats([], _console())

    def test_empty_list_mentions_no_tracks(self) -> None:
        buf = StringIO()
        print_stats([], Console(file=buf, highlight=False, markup=False, width=120))
        assert "No tracks" in buf.getvalue()


class TestPrintStatsBpmDistribution:
    def test_bpm_in_range_120(self) -> None:
        print_stats([_make_entry(bpm=120.0)], _console())

    def test_bpm_below_60_goes_to_unknown(self) -> None:
        print_stats([_make_entry(bpm=30.0)], _console())

    def test_bpm_zero_goes_to_unknown(self) -> None:
        print_stats([_make_entry(bpm=0.0)], _console())

    def test_bpm_above_180(self) -> None:
        print_stats([_make_entry(bpm=200.0)], _console())

    def test_bpm_exactly_180_bucket(self) -> None:
        print_stats([_make_entry(bpm=185.0)], _console())

    def test_mixed_bpm_entries(self) -> None:
        entries = [_make_entry(bpm=b) for b in [70.0, 90.0, 120.0, 0.0, 185.0]]
        print_stats(entries, _console())


class TestPrintStatsGenres:
    def test_genres_shown_when_present(self) -> None:
        entries = [
            _make_entry(genre="Jazz"),
            _make_entry(genre="Jazz"),
            _make_entry(genre="Rock"),
        ]
        print_stats(entries, _console())

    def test_no_genre_section_when_all_empty(self) -> None:
        """Genre section is skipped when all entries have an empty genre string."""
        entries = [_make_entry(genre=""), _make_entry(genre="   ")]
        print_stats(entries, _console())


class TestPrintStatsDecades:
    def test_known_years(self) -> None:
        entries = [_make_entry(year=1990), _make_entry(year=2000), _make_entry(year=2010)]
        print_stats(entries, _console())

    def test_unknown_year_zero(self) -> None:
        print_stats([_make_entry(year=0)], _console())

    def test_unknown_year_pre_1900(self) -> None:
        print_stats([_make_entry(year=1800)], _console())

    def test_mixed_known_and_unknown(self) -> None:
        entries = [_make_entry(year=2000), _make_entry(year=0)]
        print_stats(entries, _console())


class TestPrintStatsTrackLengths:
    def test_all_length_buckets(self) -> None:
        entries = [
            _make_entry(length=60.0),  # < 2 min
            _make_entry(length=200.0),  # 2–5 min
            _make_entry(length=400.0),  # 5–10 min
            _make_entry(length=700.0),  # > 10 min
        ]
        print_stats(entries, _console())


class TestPrintStatsArtists:
    def test_artists_shown_when_present(self) -> None:
        entries = [
            _make_entry(artist="Portishead"),
            _make_entry(artist="Portishead"),
            _make_entry(artist="Massive Attack"),
        ]
        print_stats(entries, _console())

    def test_no_artist_section_when_all_empty(self) -> None:
        entries = [_make_entry(artist=""), _make_entry(artist="   ")]
        print_stats(entries, _console())


class TestPrintStatsKeys:
    def test_all_12_keys(self) -> None:
        entries = [_make_entry(key=i) for i in range(12)]
        print_stats(entries, _console())

    def test_no_key_section_when_all_unknown(self) -> None:
        entries = [_make_entry(key=-1), _make_entry(key=-1)]
        print_stats(entries, _console())


class TestPrintStatsMode:
    def test_major_and_minor(self) -> None:
        entries = [_make_entry(mode=1), _make_entry(mode=1), _make_entry(mode=0)]
        print_stats(entries, _console())

    def test_no_mode_section_when_all_unknown(self) -> None:
        """mode=-1 entries are neither major nor minor; section should be skipped."""
        entries = [_make_entry(mode=-1)]
        print_stats(entries, _console())

    def test_all_major(self) -> None:
        entries = [_make_entry(mode=1) for _ in range(5)]
        print_stats(entries, _console())


class TestPrintStatsEnergy:
    def test_all_energy_buckets(self) -> None:
        entries = [
            _make_entry(energy=0.01),  # silence
            _make_entry(energy=0.10),  # quiet
            _make_entry(energy=0.20),  # medium
            _make_entry(energy=0.40),  # loud
            _make_entry(energy=0.60),  # very loud
        ]
        print_stats(entries, _console())


class TestPrintStatsSummary:
    def test_track_count_in_output(self) -> None:
        buf = StringIO()
        entries = [_make_entry() for _ in range(3)]
        print_stats(entries, Console(file=buf, highlight=False, markup=False, width=120))
        assert "3" in buf.getvalue()

    def test_single_track_no_crash(self) -> None:
        print_stats([_make_entry()], _console())
