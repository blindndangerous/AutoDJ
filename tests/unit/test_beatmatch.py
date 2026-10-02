"""Beatmatched mixing: tempo match, phase lock and the glide back to native tempo."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import ClassVar
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.beatmatch import (
    HOP,
    MIN_RAMP_FRAMES,
    Glide,
    downbeats,
    local_period,
    phase_locked_start,
    tempo_ratio,
)
from autodj.dj_meta import Cue, DjMeta
from autodj.mixbus import RenderedTrack
from autodj.player import Player, _BeatmatchPlan
from tests.unit._fakes import make_cfg_mock, make_sim_index

SR = 44100


def _grid(bpm: float, start: float, seconds: float) -> list[float]:
    period = 60.0 / bpm
    return [start + k * period for k in range(int((seconds - start) / period))]


def _clicks(beats: Sequence[float], seconds: float, *, loud_from: int = 0) -> np.ndarray:
    """Stereo click track: a short 2 kHz ping on each beat, louder on downbeats."""
    out = np.zeros((int(seconds * SR), 2), np.float32)
    t = np.arange(int(0.02 * SR)) / SR
    ping = np.sin(2 * np.pi * 2000.0 * t) * np.exp(-t / 0.005)
    for i, beat in enumerate(beats):
        level = 0.8 if (i - loud_from) % 4 == 0 else 0.25
        at = round(beat * SR)
        end = min(len(out), at + len(ping))
        out[at:end] += (level * ping[: end - at])[:, None]
    return out


def _onset_ms(audio: np.ndarray, at: int, window_s: float = 0.1) -> float | None:
    """Milliseconds from *at* to the strongest click's onset within the window."""
    half = int(window_s * SR)
    seg = np.abs(audio[max(0, at - half) : at + half, 0])
    if len(seg) < 2 * half or seg.max() < 1e-3:
        return None
    return float(np.argmax(seg > 0.3 * seg.max()) - half) / SR * 1000.0


def _onsets(audio: np.ndarray, threshold: float = 0.05) -> np.ndarray:
    """Sample positions where clicks start (rising edges of a 2 ms envelope)."""
    env = np.abs(audio[:, 0])
    loud = env > threshold * env.max()
    starts = np.flatnonzero(loud[1:] & ~loud[:-1]) + 1
    keep = [s for i, s in enumerate(starts) if i == 0 or s - starts[i - 1] > 0.1 * SR]
    return np.asarray(keep)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestTempoRatio:
    def test_same_tempo_needs_no_stretch(self) -> None:
        assert tempo_ratio(0.5, 0.5, 0.08) == pytest.approx(1.0)

    def test_faster_incoming_is_slowed(self) -> None:
        assert tempo_ratio(60 / 120, 60 / 124, 0.08) == pytest.approx(124 / 120)

    def test_half_and_double_time_match(self) -> None:
        assert tempo_ratio(60 / 140, 60 / 70, 0.08) == pytest.approx(1.0)
        assert tempo_ratio(60 / 70, 60 / 140, 0.08) == pytest.approx(1.0)
        # 72 BPM counts as 144 against 140 BPM: slowed by 144 / 140.
        assert tempo_ratio(60 / 140, 60 / 72, 0.08) == pytest.approx(144 / 140)

    def test_too_far_apart_or_unknown(self) -> None:
        assert tempo_ratio(60 / 100, 60 / 140, 0.08) is None
        assert tempo_ratio(0.0, 0.5, 0.08) is None
        assert tempo_ratio(0.5, -1.0, 0.08) is None


class TestLocalPeriod:
    def test_steady_grid(self) -> None:
        assert local_period(_grid(120, 0.3, 30), 10.0) == pytest.approx(0.5)
        # Past the end of the grid the last beats count.
        assert local_period(_grid(120, 0.3, 30), 100.0) == pytest.approx(0.5)

    def test_too_short_or_unsteady_grid(self) -> None:
        assert local_period(_grid(120, 0.0, 4), 1.0) is None
        jitter = list(np.cumsum([0.3, 0.7] * 12))
        assert local_period(jitter, 3.0) is None


class TestDownbeats:
    def test_first_beat_by_default(self) -> None:
        beats = _grid(120, 0.25, 6)
        assert downbeats(beats) == beats[::4]
        assert downbeats([]) == []

    def test_anchor_picks_the_bar_phase(self) -> None:
        beats = _grid(120, 0.25, 6)
        assert downbeats(beats, anchor_s=beats[2] + 0.01) == beats[2::4]
        # An anchor between beats is not trusted.
        assert downbeats(beats, anchor_s=beats[2] + 0.25) == beats[::4]


class TestPhaseLockedStart:
    _OUT: ClassVar[list[float]] = [2.0 * k for k in range(20)]
    _IN: ClassVar[list[float]] = [1.0 + 2.0 * k for k in range(10)]

    def _lock(self, **overrides: object) -> float | None:
        args: dict = {
            "entry_s": 0.5,
            "ratio": 1.0,
            "start_s": 21.1,
            "fade_s": 6.0,
            "earliest_s": 0.0,
            "latest_s": 30.0,
        }
        args.update(overrides)
        return phase_locked_start(self._OUT, self._IN, **args)

    def test_incoming_downbeat_lands_on_the_nearest_outgoing_one(self) -> None:
        # The first incoming downbeat is 0.5 s after the entry, so the fade
        # starts 0.5 s before the outgoing downbeat that moves it least.
        assert self._lock() == pytest.approx(21.5)
        assert self._lock(ratio=1.04) == pytest.approx(22.0 - 0.52)

    def test_nothing_to_lock(self) -> None:
        assert self._lock(entry_s=30.0) is None  # no incoming downbeat after the entry
        assert self._lock(fade_s=0.4) is None  # it would land after the fade
        assert self._lock(earliest_s=29.0, latest_s=29.2) is None  # no room

    def test_phrase_boundaries_come_first(self) -> None:
        assert self._lock(phrase_starts=[0.0, 16.0, 32.0]) == pytest.approx(15.5)
        # A boundary out of bounds falls back to the plain downbeats.
        assert self._lock(phrase_starts=[48.0]) == pytest.approx(21.5)


# ---------------------------------------------------------------------------
# The stretch itself
# ---------------------------------------------------------------------------


def _noise(seconds: float, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    tone = 0.3 * np.sin(2 * np.pi * 220 * t)[:, None]
    return (tone + 0.05 * rng.standard_normal((len(t), 2))).astype(np.float32)


class TestGlide:
    def test_overlap_and_rest_join_without_a_seam(self) -> None:
        audio = _noise(30)
        glide = Glide(entry=SR + 77, ratio=1.05, played=3 * SR, ramp_frames=400)
        whole = glide.head(audio, glide.cut)
        joined = np.concatenate([glide.head(audio, glide.played), glide.continuation(audio)])
        assert len(joined) == glide.cut
        np.testing.assert_allclose(joined, whole, atol=1e-6)

    def test_output_becomes_the_source_where_the_glide_ends(self) -> None:
        audio = _noise(30)
        glide = Glide(entry=SR, ratio=0.95, played=2 * SR, ramp_frames=300)
        rest = glide.continuation(audio)
        end = glide.source_end
        np.testing.assert_allclose(rest[-256:], audio[end - 256 : end], atol=1e-5)
        assert glide.origin == end - len(rest)

    def test_tempo_returns_to_native(self) -> None:
        period = 60 / 123
        beats = _grid(123, 0.5, 40)
        audio = _clicks(beats, 40)
        glide = Glide(
            entry=SR // 2,
            ratio=1.05,
            played=4 * SR,
            ramp_frames=round(8 * SR / HOP),
            beats=tuple(round(b * SR) for b in beats),
        )
        gaps = np.diff(_onsets(glide.head(audio, glide.cut))) / SR
        assert gaps[0] == pytest.approx(period * 1.05, abs=0.002)
        assert gaps[-1] == pytest.approx(period, abs=0.002)
        assert np.all(np.diff(gaps) < 0.002)  # monotonic glide, no jump back

    def test_short_glide_and_head_past_the_cut(self) -> None:
        audio = _noise(10)
        glide = Glide(entry=0, ratio=1.02, played=SR, ramp_frames=0)
        _, steady_from = glide._steps()
        assert steady_from == -(-SR // HOP) + MIN_RAMP_FRAMES
        head = glide.head(audio, glide.cut + 1000)
        assert len(head) == glide.cut + 1000
        np.testing.assert_array_equal(
            head[glide.cut :], audio[glide.source_end : glide.source_end + 1000]
        )

    def test_fits(self) -> None:
        glide = Glide(entry=SR, ratio=1.03, played=2 * SR, ramp_frames=200)
        assert not glide.fits(3 * SR)
        assert glide.fits(10 * SR)


# ---------------------------------------------------------------------------
# The server mix
# ---------------------------------------------------------------------------


_PA = 60 / 120


def _mix_player(
    *,
    bpms: tuple[float, float] = (120.0, 123.0),
    glide_bars: int = 4,
    beatmatch: bool = True,
    phrase_align: bool = False,
) -> tuple[Player, MagicMock, MagicMock]:
    cfg = make_cfg_mock()
    cfg.playback.transition_mode = "fixed_skip_silence"
    cfg.playback.crossfade_seconds = 6.0
    cfg.playback.beat_sync_fx = False
    cfg.playback.key_sync_fx = False
    cfg.djmix.beatmatch = beatmatch
    cfg.djmix.beatmatch_glide_bars = glide_bars
    cfg.djmix.phrase_align = phrase_align
    player = Player(cfg, make_sim_index(3))
    cur, nxt = player._sim.entries[0], player._sim.entries[1]
    cur.bpm, nxt.bpm = bpms
    return player, cur, nxt


def _render_pair(
    player: Player,
    cur: MagicMock,
    nxt: MagicMock,
    *,
    in_bpm: float = 123.0,
    out_bpm: float = 120.0,
    metas: bool = True,
    in_beats: int | None = None,
) -> tuple[RenderedTrack, RenderedTrack | None, dict, list[float], list[float]]:
    """Render a silent outgoing track (with a grid) into a click track, then the click track."""
    a_beats = _grid(out_bpm, 0.25, 29.5)
    b_beats = _grid(in_bpm, 0.5, 39)
    if in_beats is not None:
        b_beats = b_beats[:in_beats]
    audio = {str(cur.path): np.zeros((30 * SR, 2), np.float32), str(nxt.path): _clicks(b_beats, 40)}
    meta = {
        str(cur.path): DjMeta(beats=a_beats, analysed=True),
        # The DJ software's first downbeat makes beat 0 a pickup.
        str(nxt.path): DjMeta(
            beats=b_beats,
            analysed=True,
            cues=[Cue(b_beats[1], "first_downbeat", source="rekordbox")],
        ),
    }
    player._peek_incoming_meta = (  # type: ignore[method-assign]
        lambda e: meta[str(e.path)] if metas else None
    )
    with patch("autodj.player.load_stereo", side_effect=lambda p, *_a: audio[p].copy()):
        first = player._render_track(cur, nxt, 0)
        assert first is not None
        second = player._render_track(nxt, None, first.next_start_offset, glide_in=first.next_glide)
    return first, second, audio, a_beats, b_beats


class TestPhaseLock:
    def test_downbeats_line_up_in_the_overlap(self) -> None:
        player, cur, nxt = _mix_player()
        first, _second, _audio, a_beats, _b = _render_pair(player, cur, nxt)
        assert first.beatmatch_ratio == pytest.approx(_PA / (60 / 123))
        overlap_from = len(first.audio) - 6 * SR
        errors = [
            _onset_ms(first.audio, round(d * SR))
            for d in a_beats[::4]
            if overlap_from + SR < round(d * SR) < len(first.audio) - SR // 10
        ]
        heard = [e for e in errors if e is not None]
        assert len(heard) >= 2
        assert max(abs(e) for e in heard) < 2.0

    def test_phrase_align_puts_the_downbeat_on_a_phrase(self) -> None:
        player, cur, nxt = _mix_player(phrase_align=True)
        first, *_rest, a_beats, b_beats = _render_pair(player, cur, nxt)
        lead = (b_beats[1] - 0.5) * first.beatmatch_ratio
        fade_start = len(first.audio) - 6 * SR
        phrases = a_beats[::32]
        assert min(abs(fade_start / SR + lead - p) for p in phrases) < 0.001

    def test_half_time_pair_is_matched(self) -> None:
        player, cur, nxt = _mix_player(bpms=(140.0, 72.0))
        first, *_ = _render_pair(player, cur, nxt, out_bpm=140.0, in_bpm=72.0)
        assert first.beatmatch_ratio == pytest.approx(144 / 140, rel=1e-3)
        assert first.next_glide is not None

    def test_exact_half_time_locks_without_stretching(self) -> None:
        player, cur, nxt = _mix_player(bpms=(140.0, 70.0))
        locked, *_ = _render_pair(player, cur, nxt, out_bpm=140.0, in_bpm=70.0)
        player._cfg.djmix.beatmatch = False
        plain, *_ = _render_pair(player, cur, nxt, out_bpm=140.0, in_bpm=70.0)
        assert locked.next_glide is None and locked.beatmatch_ratio == 1.0
        assert len(locked.audio) != len(plain.audio)  # the fade start moved

    @pytest.mark.parametrize(
        ("confidence", "metas", "in_beats"),
        [(0.1, True, None), (0.8, False, None), (0.8, True, 6)],
        ids=["low-confidence", "no-grid", "short-grid"],
    )
    def test_untrusted_grid_stretches_by_bpm_without_moving_the_fade(
        self, confidence: float, metas: bool, in_beats: int | None
    ) -> None:
        player, cur, nxt = _mix_player()
        nxt.tempo_confidence = confidence
        bm, *_ = _render_pair(player, cur, nxt, metas=metas, in_beats=in_beats)
        player._cfg.djmix.beatmatch = False
        plain, *_ = _render_pair(player, cur, nxt, metas=metas, in_beats=in_beats)
        assert len(bm.audio) == len(plain.audio)
        assert bm.beatmatch_ratio == pytest.approx(123 / 120)
        assert bm.next_glide is not None and bm.next_glide.beats == ()


class TestGlideAcrossRenders:
    def test_next_render_continues_the_stretch(self) -> None:
        player, cur, nxt = _mix_player()
        first, second, audio, _a, b_beats = _render_pair(player, cur, nxt)
        glide = first.next_glide
        assert glide is not None and second is not None
        assert second.start_offset == glide.origin == first.next_start_offset
        source = audio[str(nxt.path)]
        # The overlap ends where the next render begins: no gap, no repeat.
        expected = glide.head(source, glide.played + 2000)[glide.played :]
        np.testing.assert_allclose(second.audio[:2000], expected, atol=1e-5)
        last_heard = glide.head(source, glide.played)[-1]
        np.testing.assert_allclose(first.audio[-1], last_heard, atol=1e-6)
        # After the glide the decoded track plays on, in its own timeline.
        resume = glide.cut - glide.played
        np.testing.assert_array_equal(
            second.audio[resume : resume + SR], source[glide.source_end : glide.source_end + SR]
        )
        # Clicks after the glide sit exactly where the file has them.
        late = [b for b in b_beats if b * SR > glide.source_end + SR]
        for beat in late[:4]:
            onset = _onset_ms(second.audio, round(beat * SR) - second.start_offset)
            assert onset is not None and abs(onset) < 0.1

    def test_cursor_carries_the_glide(self) -> None:
        player, cur, nxt = _mix_player()
        third = player._sim.entries[2]
        picks = iter([(nxt, "similarity"), (third, "similarity")])
        player._choose_next = lambda _c, _ctx: next(picks)  # type: ignore[method-assign]
        audio = {
            str(cur.path): np.zeros((30 * SR, 2), np.float32),
            str(nxt.path): _clicks(_grid(123, 0.5, 39), 40),
            str(third.path): np.zeros((30 * SR, 2), np.float32),
        }
        player._set_render_cursor(cur, 0, "seed")
        with patch("autodj.player.load_stereo", side_effect=lambda p, *_a: audio[p].copy()):
            a = player._next_rendered()
            assert a is not None and a.next_glide is not None
            assert player._pending_glide == a.next_glide
            b = player._next_rendered()
        assert b is not None
        assert b.glide_in == a.next_glide
        assert b.start_offset == a.next_glide.origin
        assert b.mixed_in_ratio == pytest.approx(a.beatmatch_ratio)
        player._rewind_render_cursor(b)
        assert player._pending_glide == a.next_glide
        player._skip_past_render(a)
        assert player._pending_glide == a.next_glide
        player._set_render_cursor(cur, 0, "queue")
        assert player._pending_glide is None

    def test_failed_glide_plays_the_track_at_its_own_tempo(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        player, cur, nxt = _mix_player()
        with patch.object(Glide, "continuation", side_effect=RuntimeError("boom")):
            first, second, audio, *_ = _render_pair(player, cur, nxt)
        assert second is not None
        source = audio[str(nxt.path)]
        np.testing.assert_array_equal(second.audio[:1000], source[first.next_start_offset :][:1000])
        assert "Tempo glide failed" in caplog.text

    def test_failed_stretch_mixes_unstretched(self, caplog: pytest.LogCaptureFixture) -> None:
        player, cur, nxt = _mix_player()
        with patch.object(Glide, "head", side_effect=RuntimeError("boom")):
            first, *_ = _render_pair(player, cur, nxt)
        assert first.next_glide is None and first.beatmatch_ratio == 1.0
        assert "Beatmatch stretch failed" in caplog.text

    def test_glide_in_for_another_offset_is_ignored(self) -> None:
        audio = _noise(20)
        before = audio.copy()
        glide = Glide(entry=SR, ratio=1.03, played=2 * SR, ramp_frames=50)
        Player._play_glide_in(audio, glide.origin + 1, glide)
        np.testing.assert_array_equal(audio, before)


class TestIncomingGlide:
    def test_shorter_track_gets_the_shortest_glide_or_none(self) -> None:
        plan = _BeatmatchPlan(1.04, 0, ramp_frames=round(20 * SR / HOP))
        full = Player._incoming_glide(np.zeros((60 * SR, 2), np.float32), SR, plan, 2 * SR)
        assert full is not None and full.ramp_frames == plan.ramp_frames
        short = Player._incoming_glide(np.zeros((6 * SR, 2), np.float32), SR, plan, 2 * SR)
        assert short is not None and short.ramp_frames == 0
        assert Player._incoming_glide(np.zeros((2 * SR, 2), np.float32), SR, plan, 2 * SR) is None

    def test_no_stretch_needed(self) -> None:
        plan = _BeatmatchPlan(1.0005, 0)
        assert Player._incoming_glide(np.zeros((60 * SR, 2), np.float32), 0, plan, SR) is None


# ---------------------------------------------------------------------------
# Renders without a stretch are bit-identical to the ones before the rework
# ---------------------------------------------------------------------------

# sha256 prefix, next_start_offset and length of each render, recorded with
# the whole-track stretch implementation this module replaced.
_GOLDEN = {
    "fixed": ("f14bfbf49bf797ed", 132300, 882000),
    "skip_silence": ("5463671f45c5739b", 176400, 882000),
    "bm_equal": ("5463671f45c5739b", 176400, 882000),
    "bm_too_far": ("f14bfbf49bf797ed", 132300, 882000),
    "bm_unknown": ("f14bfbf49bf797ed", 132300, 882000),
    "offset": ("832d17f53e4eecf2", 132300, 837900),
}
_CASES: dict[str, dict] = {
    "fixed": {"mode": "fixed"},
    "skip_silence": {"mode": "fixed_skip_silence"},
    "bm_equal": {"mode": "fixed_skip_silence", "beatmatch": True, "bpms": (120.0, 120.0)},
    "bm_too_far": {"mode": "fixed", "beatmatch": True, "bpms": (100.0, 140.0)},
    "bm_unknown": {"mode": "fixed", "beatmatch": True, "bpms": (120.0, 0.0)},
    "offset": {"mode": "fixed", "offset": SR},
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_unstretched_renders_are_unchanged(name: str) -> None:
    case = _CASES[name]
    cfg = make_cfg_mock()
    cfg.playback.transition_mode = case["mode"]
    cfg.playback.beat_sync_fx = False
    cfg.playback.key_sync_fx = False
    cfg.djmix.beatmatch = case.get("beatmatch", False)
    player = Player(cfg, make_sim_index(3))
    cur, nxt = player._sim.entries[0], player._sim.entries[1]
    cur.bpm, nxt.bpm = case.get("bpms", (120.0, 120.0))
    rng = np.random.default_rng(7)
    a = (0.2 * rng.standard_normal((20 * SR, 2))).astype(np.float32)
    b = (0.2 * rng.standard_normal((15 * SR, 2))).astype(np.float32)
    b[:SR] = 0.0
    audio = {str(cur.path): a, str(nxt.path): b}
    with patch("autodj.player.load_stereo", side_effect=lambda p, *_a: audio[p].copy()):
        rendered = player._render_track(cur, nxt, case.get("offset", 0))
    assert rendered is not None
    digest = hashlib.sha256(np.ascontiguousarray(rendered.audio).tobytes()).hexdigest()[:16]
    assert (digest, rendered.next_start_offset, len(rendered.audio)) == _GOLDEN[name]
    assert rendered.beatmatch_ratio == 1.0 and rendered.next_glide is None
