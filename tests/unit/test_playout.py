import numpy as np
import pytest

from autodj.playout import liner_chain_placement, render_handoff
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


def test_positive_liner_overlap_preserves_full_clockwheel_tail_after_music_entry():
    handoff = render_handoff(
        audio(12),
        {"start": 0, "segue": 5, "end": 12},
        {"start": 0, "intro": 0},
        fade_seconds=1.5,
        liner=audio(8.809, 0.2),
        liner_segue=5.3,
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


def test_explicit_overlap_respects_liner_segue_before_cold_vocals():
    handoff = render_handoff(
        audio(12),
        {"segue": 5, "end": 12},
        {},
        liner=audio(8, 0.2),
        overlap_start=5.3,
        liner_segue=6,
    )
    assert handoff.incoming_s == pytest.approx(11)


@pytest.mark.parametrize("intro,release", [(None, 4.6), (1.0, 3.6), (1.3, 3.3), (2.0, 3.3)])
def test_positive_overlap_waits_until_liner_main_ends_before_incoming_vocals(intro, release):
    handoff = render_handoff(
        audio(10),
        {"start": 0, "segue": 8, "end": 10},
        {"start": 0, "intro": intro},
        liner=audio(4.86, 0.2),
        liner_segue=4.6,
        overlap_start=3.3,
        outro_lead_limit=0,
    )
    assert handoff.liner_s == pytest.approx(8)
    assert handoff.incoming_s == pytest.approx(8 + release)
    assert len(handoff.next_overlay) / SAMPLE_RATE == pytest.approx(4.86 - release)
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.2)


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


@pytest.mark.parametrize("incoming", [{}, {"intro": 0}])
def test_negative_overlap_with_missing_or_zero_intro_uses_liner_first(incoming):
    handoff = render_handoff(
        audio(12),
        {"outro": 4, "segue": 8, "end": 12},
        incoming,
        liner=audio(2, 0.2),
        overlap_start=-3,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == pytest.approx(6)
    assert handoff.incoming_s == pytest.approx(8)


def test_negative_overlap_keeps_tail_after_a_short_liner_segue():
    handoff = render_handoff(
        audio(12),
        {"outro": 4, "segue": 8, "end": 12},
        {"intro": 5},
        liner=audio(5, 0.2),
        overlap_start=-3,
        liner_segue=2,
        outro_lead_limit=None,
    )
    assert handoff.liner_s == pytest.approx(11)
    assert handoff.incoming_s == pytest.approx(8)
    assert len(handoff.next_overlay) == 8 * SAMPLE_RATE
    assert handoff.next_overlay[-1, 0] == pytest.approx(0.2)


@pytest.mark.parametrize("overlap", [None, 0, 2.5, -2])
def test_saved_cue_placement_helper_matches_rendered_transition(overlap):
    outgoing = {"start": 0.5, "outro": 5, "segue": 8, "end": 12}
    incoming = {"start": 1, "intro": 3.5}
    main_frames = round(3.3 * SAMPLE_RATE)
    start, segue = round(0.5 * SAMPLE_RATE), round(8 * SAMPLE_RATE)
    safe_outro = round(5 * SAMPLE_RATE)
    liner_start, incoming_frame = liner_chain_placement(
        start, segue, safe_outro, main_frames, round(2.5 * SAMPLE_RATE), overlap, 1.0
    )
    rendered = render_handoff(
        audio(12),
        outgoing,
        incoming,
        liner=audio(6),
        liner_segue=3.3,
        overlap_start=overlap,
        outro_lead_limit=1.0,
    )
    assert rendered.liner_s == pytest.approx(liner_start / SAMPLE_RATE)
    assert rendered.incoming_s == pytest.approx(incoming_frame / SAMPLE_RATE)


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


def test_duck_envelope_drops_inside_spans_with_slopes_outside():
    from autodj.playout import duck_envelope

    envelope = duck_envelope(100, ((40, 60),), 0.25, 10)
    assert envelope is not None
    assert np.all(envelope[40:60] == pytest.approx(0.25))
    assert envelope[:30].tolist() == [1.0] * 30 and envelope[70:].tolist() == [1.0] * 30
    assert 0.25 < envelope[35] < 1.0 and 0.25 < envelope[65] < 1.0
    assert np.all(np.diff(envelope[30:41]) <= 0) and np.all(np.diff(envelope[59:71]) >= 0)
    # Spans at the edges, empty spans, and nothing to duck.
    assert duck_envelope(20, ((-5, 3), (15, 99)), 0.5, 4)[[0, 2, 15, 19]].tolist() == [0.5] * 4
    assert duck_envelope(20, ((5, 5),), 0.5, 4).tolist() == [1.0] * 20
    assert duck_envelope(20, (), 0.5, 4) is None
    assert duck_envelope(20, ((1, 2),), 1.0, 4) is None


def test_a_ducked_voice_lowers_the_music_and_reports_the_overlay_gain():
    voice = np.zeros((2 * SAMPLE_RATE, 2), dtype=np.float32)
    voice[:, 1] = 0.5
    music = audio(6)
    music[:, 1] = 0.0
    handoff = render_handoff(
        music,
        {"start": 0, "end": 6, "segue": 4},
        {"start": 0, "intro": 1},
        fade_seconds=1.5,
        liner=voice,
        overlap_start=1.0,
        duck_spans=((0.0, 1.0),),
        duck_db=-20.0,
    )
    # The voice starts at the segue (4 s); the music under it drops to a tenth.
    full = handoff.full_audio
    index = round((4.5 - handoff.start_offset / SAMPLE_RATE) * SAMPLE_RATE)
    assert full[index, 1] == pytest.approx(0.5)
    assert 0 < full[index, 0] < 0.1
    assert full[round(3.5 * SAMPLE_RATE) - handoff.start_offset, 0] > 0.5
    assert handoff.overlay_gain is not None
    assert len(handoff.overlay_gain) == len(handoff.next_overlay)
    # The next song enters at 5 s, as the ducked span ends: it rises back.
    assert handoff.overlay_gain[0] == pytest.approx(0.1, rel=1e-3)
    assert handoff.overlay_gain[-1] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        render_handoff(
            audio(6),
            {"segue": 4},
            {},
            liner=voice,
            duck_spans=((0.0, 1.0),),
            duck_db=float("-inf"),
        )
    plain = render_handoff(audio(6), {"segue": 4}, {}, liner=voice, duck_db=-20.0)
    assert plain.overlay_gain is None


def test_liner_and_break_ducks_keep_separate_depths_without_stacking():
    voice = np.zeros((4 * SAMPLE_RATE, 2), dtype=np.float32)
    voice[:, 1] = 0.5
    music = audio(8)
    music[:, 1] = 0
    result = render_handoff(
        music,
        {"segue": 4, "end": 8},
        {"intro": 4},
        liner=voice,
        overlap_start=0,
        liner_duck_spans=((0, 2),),
        liner_duck_db=-3,
        duck_spans=((2, 4),),
        duck_db=-9,
    )
    assert result.overlay_gain is not None
    assert result.overlay_gain[SAMPLE_RATE] == pytest.approx(10 ** (-3 / 20))
    assert result.overlay_gain[3 * SAMPLE_RATE] == pytest.approx(10 ** (-9 / 20))
    assert result.overlay_gain.min() == pytest.approx(10 ** (-9 / 20))
    first = round(result.liner_s * SAMPLE_RATE) - result.start_offset
    assert np.all(result.full_audio[first : first + 4 * SAMPLE_RATE, 1] == 0.5)
    assert result.overlay_gain[-1] == pytest.approx(1.0, abs=1e-4)
