import numpy as np
import pytest

from autodj.playout import render_handoff
from autodj.stereo import SAMPLE_RATE


def audio(seconds, value=1.0):
    return np.full((round(seconds * SAMPLE_RATE), 2), value, dtype=np.float32)


def test_fade_starts_a_third_length_before_segue_and_keeps_one_second_tail():
    handoff = render_handoff(
        audio(4),
        {"start": 0, "end": 4, "segue": 2.5},
        {"start": 2, "intro": 7},
        fade_seconds=1.5,
    )
    assert handoff.segue_s == pytest.approx(2.5)
    assert handoff.fade_start_s == pytest.approx(2.0)
    assert handoff.incoming_s == pytest.approx(2.5)
    assert len(handoff.audio) == round(2.5 * SAMPLE_RATE)
    assert len(handoff.next_overlay) == SAMPLE_RATE
    assert handoff.next_start_offset == 2 * SAMPLE_RATE
    assert handoff.audio[-1, 0] == pytest.approx(1 - np.expm1(1.0) / np.expm1(3.0), abs=1e-5)
    assert handoff.next_overlay[0, 0] < 1.0
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.0, abs=1e-6)


def test_start_and_end_clamp_retained_region_and_short_tail_shortens_fade():
    handoff = render_handoff(
        audio(5),
        {"start": 1, "end": 2.5, "segue": 2.2},
        {"start": -4},
        start_offset=round(1.2 * SAMPLE_RATE),
        fade_seconds=1.5,
    )
    assert handoff.start_offset == round(1.2 * SAMPLE_RATE)
    assert handoff.audio.shape == (round((2.2 - 1.2) * SAMPLE_RATE), 2)
    assert len(handoff.next_overlay) == round(0.3 * SAMPLE_RATE)
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.0, abs=1e-6)
    assert handoff.next_start_offset == 0


def test_short_liner_is_backtimed_to_its_intro_without_pulling_music_early():
    handoff = render_handoff(
        audio(12),
        {"start": 0, "outro": 5, "segue": 8, "end": 12},
        {"start": 0, "intro": 4},
        fade_seconds=1.5,
        liner=audio(6, 0.25),
        overlap_start=0,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == pytest.approx(6)
    assert handoff.incoming_s == pytest.approx(8)
    assert len(handoff.audio) == 8 * SAMPLE_RATE
    assert handoff.audio[6 * SAMPLE_RATE, 0] == pytest.approx(1.25)


def test_long_liner_delays_music_but_does_not_move_the_outgoing_fade():
    handoff = render_handoff(
        audio(14),
        {"start": 0, "outro": 5, "segue": 8, "end": 14},
        {"start": 0, "intro": 4},
        fade_seconds=1.5,
        liner=audio(8, 0.25),
        overlap_start=0,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == pytest.approx(5)
    assert handoff.incoming_s == pytest.approx(9)
    assert handoff.fade_start_s == pytest.approx(7.5)
    assert len(handoff.audio) == 9 * SAMPLE_RATE
    assert len(handoff.next_overlay) == 4 * SAMPLE_RATE
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.25)


def test_unknown_intro_means_zero_and_voice_never_advances_music():
    handoff = render_handoff(
        audio(12),
        {"start": 0, "outro": 5, "segue": 8, "end": 12},
        {},
        liner=audio(2, 0.2),
        overlap_start=0,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == pytest.approx(6)
    assert handoff.incoming_s == pytest.approx(8)


def test_positive_liner_overlap_preserves_full_plague_tail_after_music_entry():
    handoff = render_handoff(
        audio(12),
        {"start": 0, "segue": 5, "end": 12},
        {"start": 0, "intro": 0},
        fade_seconds=1.5,
        liner=audio(8.809, 0.2),
        overlap_start=5.3,
    )
    assert handoff.liner_s == pytest.approx(5)
    assert handoff.incoming_s == pytest.approx(10.3)
    assert len(handoff.next_overlay) == round((13.809 - 10.3) * SAMPLE_RATE)
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.2)
    assert handoff.audio[5 * SAMPLE_RATE, 0] == pytest.approx(
        0.2 + 1 - np.expm1(1.0) / np.expm1(3.0), abs=1e-5
    )


@pytest.mark.parametrize("intro,liner_start", [(0, 4), (2, 6)])
def test_liner_segue_fits_main_content_before_vocals_and_keeps_tail(intro, liner_start):
    handoff = render_handoff(
        audio(12),
        {"outro": 4, "segue": 8, "end": 12},
        {"start": 0, "intro": intro},
        liner=audio(6, 0.2),
        overlap_start=0,
        outro_lead_limit=None,
        liner_segue=4,
    )
    assert handoff.liner_s == pytest.approx(liner_start)
    assert handoff.incoming_s == pytest.approx(8)
    assert len(handoff.next_overlay) == (2 + intro) * SAMPLE_RATE
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.2)


def test_explicit_overlap_overrides_liner_segue():
    handoff = render_handoff(
        audio(12),
        {"segue": 5, "end": 12},
        {},
        liner=audio(8, 0.2),
        overlap_start=5.3,
        liner_segue=6,
    )
    assert handoff.incoming_s == pytest.approx(10.3)


@pytest.mark.parametrize("intro, requested_delay, expected_delay", [(5, 3, 3), (3, 3, 1)])
def test_negative_overlap_starts_after_music_within_intro_and_keeps_tail(
    intro, requested_delay, expected_delay
):
    handoff = render_handoff(
        audio(12),
        {"outro": 4, "segue": 8, "end": 12},
        {"start": 0, "intro": intro},
        liner=audio(2, 0.2),
        overlap_start=-requested_delay,
        outro_lead_limit=None,
    )
    assert handoff.incoming_s == pytest.approx(8)
    assert handoff.liner_s == pytest.approx(8 + expected_delay)
    assert len(handoff.next_overlay) == expected_delay * SAMPLE_RATE + 2 * SAMPLE_RATE
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.2)


def test_negative_overlap_falls_back_to_liner_first_when_main_exceeds_intro():
    handoff = render_handoff(
        audio(12),
        {"outro": 4, "segue": 8, "end": 12},
        {"intro": 1},
        liner=audio(3, 0.2),
        overlap_start=-3,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == pytest.approx(6)
    assert handoff.incoming_s == pytest.approx(8)


@pytest.mark.parametrize("overlap", [-15.01, float("nan"), float("inf")])
def test_invalid_negative_overlap_is_rejected(overlap):
    with pytest.raises(ValueError, match="overlap_start"):
        render_handoff(audio(4), {}, {}, liner=audio(1), overlap_start=overlap)


def test_outro_after_segue_does_not_move_music_entry_and_seek_past_it_is_empty():
    points = {"start": 0, "segue": 4, "outro": 8, "end": 9}
    handoff = render_handoff(audio(9), points, {}, start_offset=6 * SAMPLE_RATE)
    assert handoff.incoming_s == pytest.approx(4)
    assert handoff.audio.shape == (0, 2)
    assert handoff.start_offset == 4 * SAMPLE_RATE
    assert handoff.next_overlay.shape[1] == 2


def test_zero_fade_is_a_cut_and_invalid_audio_or_times_are_rejected():
    cut = render_handoff(audio(3), {"segue": 2, "end": 3}, {}, fade_seconds=0)
    assert len(cut.audio) == 2 * SAMPLE_RATE
    assert cut.next_overlay.shape == (0, 2)
    with pytest.raises(ValueError, match="stereo"):
        render_handoff(np.zeros((100,)), {}, {})
    with pytest.raises(ValueError, match="fade_seconds"):
        render_handoff(audio(3), {}, {}, fade_seconds=float("nan"))


@pytest.mark.parametrize("overlap, incoming", [(None, 12), (0, 10), (7, 12)])
def test_ids_wait_for_song_segue_and_blank_overlap_uses_liner_segue(overlap, incoming):
    handoff = render_handoff(
        audio(14),
        {"outro": 3, "segue": 8, "end": 14},
        {"intro": 2},
        liner=audio(6, 0.2),
        liner_segue=4,
        overlap_start=overlap,
    )
    assert handoff.liner_s == 8
    assert handoff.incoming_s == incoming
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.2)


def test_blank_overlap_without_liner_segue_falls_back_to_end():
    handoff = render_handoff(
        audio(14),
        {"outro": 3, "segue": 8, "end": 14},
        {"intro": 20},
        liner=audio(6, 0.2),
    )
    assert handoff.liner_s == 8
    assert handoff.incoming_s == 14


def test_enabled_outro_can_start_voice_early_with_blank_incoming_overlap():
    handoff = render_handoff(
        audio(14),
        {"outro": 3, "segue": 8, "end": 14},
        {"intro": 20},
        liner=audio(6, 0.2),
        liner_segue=4,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == 4
    assert handoff.incoming_s == 8


def test_chunk_and_overlay_are_views_of_one_buffer() -> None:
    handoff = render_handoff(
        audio(10, 0.5),
        {"start": 0, "outro": 7, "segue": 8, "end": 10},
        {"start": 0, "intro": 1},
        fade_seconds=1.5,
    )
    assert len(handoff.audio) and len(handoff.next_overlay)
    assert np.shares_memory(handoff.audio, handoff.full_audio)
    assert np.shares_memory(handoff.next_overlay, handoff.full_audio)
    np.testing.assert_array_equal(
        handoff.full_audio, np.concatenate((handoff.audio, handoff.next_overlay))
    )
