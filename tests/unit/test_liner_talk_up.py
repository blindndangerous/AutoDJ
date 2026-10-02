"""Liner talk-ups: a liner timed to end just before the incoming vocal."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from autodj.liner_scheduler import TALK_UP_MARGIN_S, LinerScheduler
from autodj.mixbus import LinerCue, RenderedTrack

_SR = 44100
_MARGIN = int(TALK_UP_MARGIN_S * _SR)


def _cfg(**over):
    base = {
        "liners_enabled": True,
        "liners_every_n_songs": 1,
        "liners_every_minutes": None,
        "liners_random_min_minutes": None,
        "liners_random_max_minutes": None,
        "liners_pick_mode": "sequential",
        "liners_duck_db": -10.0,
        "liners_talk_up": True,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _entry(path: str) -> SimpleNamespace:
    return SimpleNamespace(path=path)


def _track(
    path: str,
    seconds: float,
    *,
    start_offset: float = 0.0,
    next_path: str | None = None,
    overlap: float = 0.0,
    next_entry_offset: float = 0.0,
    ratio: float = 1.0,
    glide_ratio: float | None = None,
) -> RenderedTrack:
    return RenderedTrack(
        _entry(path),
        np.zeros((int(seconds * _SR), 2), np.float32),
        _entry(next_path) if next_path else None,
        0,
        "none" if next_path else "",
        ratio,
        start_offset=int(start_offset * _SR),
        overlap_frames=int(overlap * _SR),
        next_entry_offset=int(next_entry_offset * _SR),
        glide_in=SimpleNamespace(ratio=glide_ratio) if glide_ratio else None,
    )


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        liner_s: float,
        vocals: dict[str, float],
        clock: list[float] | None = None,
        **cfg,
    ) -> None:
        sf.write(tmp_path / "a.wav", np.zeros(441, np.float32), _SR)
        self.bus = MagicMock()
        self.decodes = 0
        self.liner = np.zeros((int(liner_s * _SR), 2), np.float32)
        self.clock = clock or [0.0]

        def decoder(_folder: Path, _name: str) -> np.ndarray:
            self.decodes += 1
            return self.liner

        self.sched = LinerScheduler(
            _cfg(**cfg),
            tmp_path,
            self.bus,
            clock=lambda: self.clock[0],
            rng=random.Random(0),
            decoder=decoder,
            vocal_start=lambda entry: vocals.get(entry.path),
        )

    def cues(self) -> list[LinerCue | None]:
        return [call.kwargs.get("at") for call in self.bus.play_liner.call_args_list]


class TestIntro:
    def test_a_liner_that_fits_the_intro_ends_just_before_the_vocal(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=2.0, vocals={"n": 10.0})
        track = _track("n", 30.0)
        h.sched.on_track_start(track)
        (cue,) = h.cues()
        assert cue is not None and cue.track is track
        assert cue.frame == 10 * _SR - _MARGIN - 2 * _SR

    def test_positions_count_from_where_the_render_starts(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=2.0, vocals={"n": 10.0})
        h.sched.on_track_start(_track("n", 30.0, start_offset=4.0, glide_ratio=0.95))
        (cue,) = h.cues()
        # A faster glide brings the vocal in sooner; a slower one is ignored.
        assert cue is not None
        assert cue.frame == int(6 * _SR * 0.95 - _MARGIN - 2 * _SR)
        h.bus.reset_mock()
        h.sched.on_track_start(_track("n", 30.0, start_offset=4.0, glide_ratio=1.05))
        (cue,) = h.cues()
        assert cue is not None
        assert cue.frame == 6 * _SR - _MARGIN - 2 * _SR

    @pytest.mark.parametrize(
        ("vocals", "talk_up"),
        [({"n": 3.0}, True), ({}, True), ({"n": 10.0}, False)],
        ids=["too-long", "no-vocal-known", "talk-up-off"],
    )
    def test_otherwise_it_plays_as_the_track_starts(self, tmp_path, vocals, talk_up) -> None:
        h = _Harness(tmp_path, liner_s=4.0, vocals=vocals, liners_talk_up=talk_up)
        h.sched.on_track_start(_track("n", 30.0))
        assert h.cues() == [None]

    def test_without_a_render_it_plays_at_once(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=1.0, vocals={"n": 10.0})
        h.sched.on_track_start()
        assert h.cues() == [None]

    def test_without_a_vocal_source_talk_up_is_off(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=1.0, vocals={"n": 10.0})
        h.sched._vocal_start = None
        h.sched.on_track_start(_track("n", 30.0))
        assert h.cues() == [None]


class TestOverTheCrossfade:
    """The outgoing render R is 60 s long and its last 8 s mix in "n"."""

    @staticmethod
    def _outgoing(**kw) -> RenderedTrack:
        return _track("r", 60.0, next_path="n", overlap=8.0, **kw)

    def test_a_liner_too_long_for_the_intro_starts_in_the_overlap(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=4.0, vocals={"n": 5.0}, liners_every_n_songs=2)
        r = self._outgoing()
        h.sched.on_track_start(r)  # one track since: due at the next start
        (cue,) = h.cues()
        assert cue is not None and cue.track is r
        assert cue.frame == 57 * _SR - _MARGIN - 4 * _SR
        # The bus played it over the crossfade: the next start counts it.
        cue.started = True
        h.sched.on_track_start(_track("n", 30.0, start_offset=8.0))
        assert len(h.cues()) == 1
        assert h.sched._tracks_since == 0
        assert h.decodes == 1

    def test_the_overlap_runs_at_the_matched_tempo(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=4.0, vocals={"n": 5.0}, liners_every_n_songs=2)
        h.sched.on_track_start(self._outgoing(ratio=1.05))
        (cue,) = h.cues()
        assert cue is not None
        assert cue.frame == int(52 * _SR + 5 * _SR * 1.05) - _MARGIN - 4 * _SR

    def test_a_dropped_cue_plays_at_the_next_start_without_decoding_again(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=4.0, vocals={"n": 5.0}, liners_every_n_songs=2)
        h.sched.on_track_start(self._outgoing())
        assert len(h.cues()) == 1  # cued, then the track was skipped before it
        h.sched.on_track_start(_track("n", 30.0, start_offset=8.0))
        assert h.cues()[1] is None  # the vocal is already due: straight away
        assert h.decodes == 1

    def test_a_liner_that_fits_the_next_intro_waits_for_it(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=4.0, vocals={"n": 20.0}, liners_every_n_songs=2)
        h.sched.on_track_start(self._outgoing())
        assert h.cues() == []
        incoming = _track("n", 30.0, start_offset=8.0)
        h.sched.on_track_start(incoming)
        (cue,) = h.cues()
        assert cue is not None and cue.track is incoming
        assert cue.frame == 12 * _SR - _MARGIN - 4 * _SR
        assert h.decodes == 1

    def test_a_liner_too_long_even_for_the_overlap_plays_as_usual(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=7.0, vocals={"n": 5.0}, liners_every_n_songs=2)
        h.sched.on_track_start(self._outgoing())
        assert h.cues() == []
        h.sched.on_track_start(_track("n", 30.0, start_offset=8.0))
        assert h.cues() == [None]

    def test_vocal_past_the_overlap_is_counted_at_native_tempo(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=10.0, vocals={"n": 10.0}, liners_every_n_songs=2)
        h.sched.on_track_start(self._outgoing(ratio=1.05))
        (cue,) = h.cues()
        heard = 8 * _SR / 1.05
        vocal = 60 * _SR + (10 * _SR - heard)
        assert cue is not None
        assert cue.frame == int(vocal - _MARGIN - 10 * _SR)

    def test_timed_triggers_wait_while_a_cue_is_waiting(self, tmp_path) -> None:
        clock = [0.0]
        h = _Harness(
            tmp_path,
            liner_s=4.0,
            vocals={"n": 5.0},
            clock=clock,
            liners_every_n_songs=2,
            liners_every_minutes=1.0,
        )
        h.sched.on_track_start(self._outgoing())
        clock[0] = 600.0
        h.sched.tick()
        assert len(h.cues()) == 1

    @pytest.mark.parametrize(
        "case", ["not-due", "no-next", "no-overlap", "no-vocal", "vocal-before-entry", "idle"]
    )
    def test_nothing_is_cued_ahead(self, tmp_path, case: str) -> None:
        vocals = {} if case == "no-vocal" else {"n": 5.0}
        every = 3 if case == "not-due" else 2
        h = _Harness(tmp_path, liner_s=4.0, vocals=vocals, liners_every_n_songs=every)
        track = self._outgoing(next_entry_offset=6.0 if case == "vocal-before-entry" else 0.0)
        if case == "no-next":
            track = _track("r", 60.0)
        if case == "no-overlap":
            track = _track("r", 60.0, next_path="n")
        if case == "idle":
            h.sched._can_fire = lambda: False
        h.sched.on_track_start(track)
        assert h.cues() == []
        assert h.decodes == 0

    def test_an_empty_library_cues_nothing(self, tmp_path) -> None:
        h = _Harness(tmp_path, liner_s=4.0, vocals={"n": 5.0}, liners_every_n_songs=2)
        (tmp_path / "a.wav").unlink()
        h.sched.on_track_start(self._outgoing())
        assert h.cues() == []
