"""Vocal timing from synced lyrics, and the crossfade vocal-clash guard."""

from __future__ import annotations

import pytest

from autodj.audio_meta import LyricLine
from autodj.vocals import (
    LINE_CAP_S,
    VocalCache,
    first_vocal_s,
    guard_fade,
    sung_spans,
)


def _lines(*stamps: tuple[float, str]) -> list[LyricLine]:
    return [LyricLine(time_s=t, text=text) for t, text in stamps]


def _spans(*stamps: tuple[float, str]) -> tuple[tuple[float, float], ...]:
    return sung_spans(_lines(*stamps))


class TestSungSpans:
    def test_a_line_ends_at_the_next_line(self) -> None:
        assert _spans((10.0, "one"), (12.0, "two"), (13.5, "")) == ((10.0, 12.0), (12.0, 13.5))

    def test_a_line_before_a_long_break_is_capped(self) -> None:
        assert _spans((10.0, "one"), (40.0, "two")) == (
            (10.0, 10.0 + LINE_CAP_S),
            (40.0, 40.0 + LINE_CAP_S),
        )

    def test_instrumental_markers_are_not_sung(self) -> None:
        assert _spans((0.0, ""), (8.0, "  "), (16.0, "words")) == ((16.0, 16.0 + LINE_CAP_S),)

    def test_a_line_repeated_at_the_same_time_runs_to_the_next_time(self) -> None:
        spans = _spans((10.0, "chorus"), (10.0, "chorus"), (11.0, "next"))
        assert spans[0] == (10.0, 11.0)

    def test_first_vocal(self) -> None:
        assert first_vocal_s(_spans((0.0, ""), (7.5, "hello"))) == 7.5
        assert first_vocal_s(()) is None

    def test_lines_in_skipped_silence_are_not_heard(self) -> None:
        spans = _spans((0.0, "Artist - Title"), (6.0, "first words"))
        assert first_vocal_s(spans, after=1.5) == 6.0
        assert first_vocal_s(spans, after=7.0) is None


class TestGuardFade:
    # The outgoing track sings 108-112 and 112-117; its outro is
    # instrumental after that.
    LATE_SINGER = _spans((108.0, "x"), (112.0, "y"), (117.0, ""))

    def test_no_clash_keeps_the_fade(self) -> None:
        spans = _spans((100.0, "a"), (104.0, "b"), (108.0, "c"), (112.0, ""))
        assert guard_fade(spans, 2.0, start_s=110.0, fade_s=8.0, entry_s=0.0) is None

    def test_an_incoming_vocal_after_the_fade_keeps_it(self) -> None:
        assert guard_fade(self.LATE_SINGER, 9.0, start_s=110.0, fade_s=8.0, entry_s=0.0) is None

    def test_a_line_sung_across_the_incoming_vocal_ends_the_fade_there(self) -> None:
        spans = _spans((100.0, "a"), (104.0, "b"), (108.0, "c"), (113.0, ""))
        assert guard_fade(spans, 2.0, start_s=110.0, fade_s=8.0, entry_s=0.0) == (110.0, 2.0)

    def test_the_fade_may_run_on_until_the_outgoing_track_sings_again(self) -> None:
        spans = _spans((100.0, "a"), (106.0, ""), (115.0, "b"))
        assert guard_fade(spans, 2.0, start_s=110.0, fade_s=8.0, entry_s=0.0) == (110.0, 5.0)

    def test_the_incoming_stretch_moves_its_vocal(self) -> None:
        spans = _spans((104.0, "b"), (108.0, "c"), (111.0, "d"), (115.0, ""))
        # Two seconds after the entry, played 1.5 times slower: 3 s into the fade.
        result = guard_fade(spans, 3.0, start_s=110.0, fade_s=8.0, entry_s=1.0, ratio=1.5)
        assert result == (110.0, 3.0)

    def test_a_fade_that_would_drop_below_a_second_moves_to_the_nearest_gap(self) -> None:
        result = guard_fade(self.LATE_SINGER, 0.5, start_s=110.0, fade_s=6.0, entry_s=0.0)
        assert result is not None
        start, fade = result
        # Earlier: the fade ends as the outgoing track's first line starts.
        assert start == pytest.approx(107.0)
        assert fade == pytest.approx(1.0)

    def test_moves_go_in_whole_beats_and_respect_the_earliest_start(self) -> None:
        result = guard_fade(
            self.LATE_SINGER,
            0.5,
            start_s=110.0,
            fade_s=6.0,
            entry_s=0.0,
            earliest_s=108.0,
            end_s=130.0,
            beat_s=0.5,
        )
        # The first whole-beat start after the outgoing singer stops at 117.
        assert result == (116.5, 6.0)

    def test_a_moved_fade_ends_with_the_track(self) -> None:
        result = guard_fade(
            self.LATE_SINGER,
            0.5,
            start_s=110.0,
            fade_s=6.0,
            entry_s=0.0,
            earliest_s=108.0,
            end_s=120.0,
            beat_s=0.5,
        )
        assert result == (116.5, 3.5)

    def test_no_arrangement_keeps_the_fade(self) -> None:
        spans = sung_spans(_lines(*((90.0 + 4 * i, "la") for i in range(15))))
        assert guard_fade(spans, 0.0, start_s=110.0, fade_s=6.0, entry_s=0.0) is None

    def test_never_below_a_second(self) -> None:
        spans = _spans((108.0, "x"), (113.0, ""))
        assert guard_fade(spans, 0.5, start_s=110.0, fade_s=0.8, entry_s=0.0) is None

    @pytest.mark.parametrize(
        ("spans", "first"),
        [((), 2.0), (((108.0, 113.0),), None)],
    )
    def test_without_lyrics_on_both_sides_nothing_changes(self, spans, first) -> None:
        assert guard_fade(spans, first, start_s=110.0, fade_s=8.0, entry_s=0.0) is None


class TestVocalCache:
    def test_each_track_is_read_once(self) -> None:
        reads: list[str] = []

        def loader(path: str) -> list[LyricLine]:
            reads.append(path)
            return _lines((1.0, "hi"))

        cache = VocalCache(loader)
        assert cache.spans("a") == cache.spans("a") == ((1.0, 1.0 + LINE_CAP_S),)
        assert reads == ["a"]

    def test_old_tracks_are_forgotten(self) -> None:
        reads: list[str] = []
        cache = VocalCache(lambda path: reads.append(path) or [], size=1)
        cache.spans("a")
        cache.spans("b")
        cache.spans("a")
        assert reads == ["a", "b", "a"]
