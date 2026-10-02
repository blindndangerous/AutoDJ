"""The ``auto`` transition effect: one effect per crossfade, suited to the pair."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from autodj.transitions import (
    _REAL_EFFECTS,
    AUTO_CATEGORIES,
    TRANSITION_EFFECT_NAMES,
    TransitionFx,
    auto_category,
    choose_auto_effect,
    pick_effect,
)


def _track(
    bpm: float = 124.0,
    confidence: float = 0.9,
    key: int = 9,
    mode: int = 0,
    energy: float = 0.1,
) -> SimpleNamespace:
    """A track as auto reads it; key 9 minor is A minor (8A)."""
    return SimpleNamespace(bpm=bpm, tempo_confidence=confidence, key=key, mode=mode, energy=energy)


class TestCategories:
    def test_discovery_pick_is_a_change_of_scene(self) -> None:
        # Even when the keys clash: the pick decides first.
        assert auto_category(_track(), _track(key=3), "discovery") == "scene_change"

    def test_clashing_keys_are_disguised(self) -> None:
        # A minor (8A) into D# minor (2A): far apart on the Camelot wheel.
        assert auto_category(_track(), _track(key=3)) == "disguise"

    def test_large_tempo_gap_is_disguised(self) -> None:
        assert auto_category(_track(bpm=120.0), _track(bpm=132.0)) == "disguise"

    def test_double_time_is_not_a_tempo_gap(self) -> None:
        assert auto_category(_track(bpm=87.0), _track(bpm=174.0)) == "clean"

    def test_doubtful_tempos_are_not_compared(self) -> None:
        out, inn = _track(bpm=120.0, confidence=0.2), _track(bpm=150.0)
        assert auto_category(out, inn) == "smooth"

    def test_energy_lift_builds_up(self) -> None:
        assert auto_category(_track(energy=0.05), _track(energy=0.08)) == "lift"

    def test_clash_outranks_a_lift(self) -> None:
        assert auto_category(_track(energy=0.05), _track(key=3, energy=0.08)) == "disguise"

    def test_same_key_and_tempo_blend_cleanly(self) -> None:
        assert auto_category(_track(bpm=124.0), _track(bpm=126.0)) == "clean"

    def test_neighbouring_key_is_smooth_not_clean(self) -> None:
        # A minor (8A) into E minor (9A): compatible, not the same key.
        assert auto_category(_track(), _track(key=4)) == "smooth"

    def test_relative_major_counts_as_compatible(self) -> None:
        # A minor (8A) into C major (8B).
        assert auto_category(_track(), _track(key=0, mode=1)) == "smooth"

    def test_unknown_keys_and_tempos_are_smooth(self) -> None:
        unknown = _track(bpm=0.0, key=-1, mode=-1, energy=0.0)
        assert auto_category(unknown, unknown) == "smooth"

    def test_a_key_that_is_not_a_number_is_unknown(self) -> None:
        odd = SimpleNamespace(bpm=124.0, tempo_confidence=0.9, key="A", mode=0, energy=0.1)
        assert auto_category(_track(), odd) == "smooth"

    def test_tempo_gap_between_close_and_clash_is_smooth(self) -> None:
        assert auto_category(_track(bpm=120.0), _track(bpm=126.0)) == "smooth"


class TestChoice:
    @pytest.mark.parametrize("seed", range(10))
    def test_choice_comes_from_the_category(self, seed: int) -> None:
        rng = np.random.default_rng(seed)
        chosen = choose_auto_effect(_track(), _track(key=3), rng=rng)
        assert chosen in AUTO_CATEGORIES["disguise"]

    def test_a_category_still_varies(self) -> None:
        rng = np.random.default_rng(0)
        seen = {choose_auto_effect(_track(), _track(key=3), rng=rng) for _ in range(60)}
        assert len(seen) > 1

    def test_clean_category_can_be_a_plain_fade(self) -> None:
        assert TransitionFx.NONE in AUTO_CATEGORIES["clean"]

    def test_every_category_holds_concrete_effects(self) -> None:
        for effects in AUTO_CATEGORIES.values():
            for fx in effects:
                assert fx is TransitionFx.NONE or fx in _REAL_EFFECTS

    def test_works_without_an_rng(self) -> None:
        assert choose_auto_effect(_track(), _track()) in AUTO_CATEGORIES["clean"]

    def test_pick_effect_without_tracks_is_smooth(self) -> None:
        chosen = pick_effect(TransitionFx.AUTO, rng=np.random.default_rng(1))
        assert chosen in AUTO_CATEGORIES["smooth"]


def test_auto_is_selectable_but_never_drawn_by_random_or_rotate() -> None:
    assert "auto" in TRANSITION_EFFECT_NAMES
    assert TransitionFx.AUTO not in _REAL_EFFECTS
