"""Shortened reviews preserve actual rendered samples and programme positions."""

import dataclasses

import numpy as np
import pytest
import soundfile as sf

from autodj.review_audio import ReviewSettings, ReviewSpan, shorten_review


def _settings(**changes):
    return ReviewSettings(head_seconds=1, tail_seconds=1, clip_threshold_seconds=5, **changes)


def _audio_pieces(result):
    return [(p.programme_start, p.programme_end) for p in result.pieces if p.kind == "audio"]


def test_song_middle_is_removed_once_with_tone_and_exact_mapping(tmp_path):
    rate = 1000
    audio = np.repeat(np.arange(10, dtype=np.float32), rate)[:, None] / 10
    source = tmp_path / "source.wav"
    sf.write(source, audio, rate, subtype="FLOAT")
    decoded, actual_rate = sf.read(source, dtype="float32", always_2d=True)
    result = shorten_review(
        decoded,
        [ReviewSpan("song", "Song", "song", 0, 10_000)],
        sample_rate=actual_rate,
        settings=_settings(),
    )
    assert _audio_pieces(result) == [(0, 1000), (9000, 10_000)]
    assert [(p.preview_start, p.preview_end, p.kind) for p in result.pieces] == [
        (0, 1000, "audio"),
        (1000, 1250, "tone"),
        (1250, 2250, "audio"),
    ]
    np.testing.assert_array_equal(result.audio[:1000], decoded[:1000])
    np.testing.assert_array_equal(result.audio[-1000:], decoded[-1000:])
    assert 0.01 < np.max(np.abs(result.audio[1000:1250])) <= 0.1
    exported = tmp_path / "review.wav"
    sf.write(exported, result.audio, rate, subtype="FLOAT")
    reread, _ = sf.read(exported, dtype="float32", always_2d=True)
    np.testing.assert_array_equal(reread, result.audio)


@pytest.mark.parametrize(
    "frames,expected", [(4999, [(0, 4999)]), (5000, [(0, 5000)]), (5001, [(0, 1000), (4001, 5001)])]
)
def test_clip_threshold_is_inclusive(frames, expected):
    result = shorten_review(
        np.zeros((frames, 2), np.float32),
        [ReviewSpan("clip", "Clip", "speech", 0, frames)],
        sample_rate=1000,
        settings=_settings(),
    )
    assert _audio_pieces(result) == expected


def test_protected_joins_and_overlapping_windows_never_duplicate_samples():
    """ADR-0014-02: protected joins are retained once after window union."""
    audio = np.arange(20_000, dtype=np.float32)[:, None]
    result = shorten_review(
        audio,
        [
            ReviewSpan("a", "Repeated", "song", 0, 11_000),
            ReviewSpan("b", "Repeated", "song", 9000, 20_000),
        ],
        sample_rate=1000,
        settings=_settings(),
        protected=[(8000, 12_000), (11_000, 13_000)],
    )
    assert _audio_pieces(result) == [(0, 1000), (8000, 13_000), (19_000, 20_000)]
    middle = next(p for p in result.pieces if p.programme_start == 8000)
    np.testing.assert_array_equal(
        result.audio[middle.preview_start : middle.preview_end], audio[8000:13_000]
    )


def test_selection_expands_transitively_for_short_clips_and_protected_joins():
    result = shorten_review(
        np.zeros((20_000, 2), np.float32),
        [
            ReviewSpan("song", "Song", "song", 0, 20_000),
            ReviewSpan("voice", "Voice", "speech", 5000, 9000),
        ],
        sample_rate=1000,
        settings=_settings(),
        selection=(7500, 8000),
        protected=[(8500, 11_000), (10_000, 12_000)],
    )
    assert result.effective_range == (5000, 12_000)
    assert _audio_pieces(result) == [(5000, 12_000)]


def test_keep_complete_overrides_long_clip_threshold_and_selection():
    """ADR-0014-02: a full review target overrides ordinary clip shortening."""
    result = shorten_review(
        np.zeros((12_000, 2), np.float32),
        [ReviewSpan("pending", "Pending speech", "speech", 0, 12_000, keep_complete=True)],
        sample_rate=1000,
        settings=_settings(),
        selection=(3000, 4000),
    )
    assert result.effective_range == (0, 12_000)
    assert _audio_pieces(result) == [(0, 12_000)]


def test_short_song_windows_merge_and_half_open_boundary_does_not_expand():
    result = shorten_review(
        np.ones((3000, 1), np.float32),
        [ReviewSpan("a", "A", "song", 0, 1500), ReviewSpan("b", "B", "speech", 1500, 3000)],
        sample_rate=1000,
        settings=_settings(),
        selection=(0, 1500),
    )
    assert result.effective_range == (0, 1500)
    assert _audio_pieces(result) == [(0, 1500)]
    assert len(result.audio) == 1500


def test_defaults_custom_tone_and_song_middle_range():
    assert dataclasses.asdict(ReviewSettings()) == {
        "head_seconds": 10,
        "tail_seconds": 10,
        "clip_threshold_seconds": 300,
        "tone_seconds": 0.25,
    }
    result = shorten_review(
        np.ones((10_000, 2), np.float32),
        [ReviewSpan("song", "Song", "song", 0, 10_000)],
        sample_rate=1000,
        settings=_settings(tone_seconds=0.1),
        selection=(2000, 8000),
    )
    assert _audio_pieces(result) == [(2000, 3000), (7000, 8000)]
    assert len(result.audio) == 2100
    np.testing.assert_array_equal(result.audio[:, 0], result.audio[:, 1])


def test_disk_backed_input_and_item_boundaries_preserve_repeated_occurrences(tmp_path):
    audio = np.memmap(tmp_path / "programme.pcm", mode="w+", shape=(20_000, 2), dtype=np.float32)
    audio[:] = 0.2
    result = shorten_review(
        audio,
        [
            ReviewSpan("first", "Repeated song", "song", 0, 10_000),
            ReviewSpan("second", "Repeated song", "song", 10_000, 20_000),
        ],
        sample_rate=1000,
        settings=_settings(),
    )
    assert result.item_positions == {"first": (0, 2250), "second": (2250, 4500)}
    assert _audio_pieces(result) == [(0, 1000), (9000, 11_000), (19_000, 20_000)]
    assert not np.shares_memory(audio, result.audio)
    assert result.audio.nbytes < audio.nbytes
    np.testing.assert_array_equal(audio, np.full(audio.shape, 0.2, np.float32))


def test_small_positive_settings_retain_at_least_one_frame():
    result = shorten_review(
        np.ones((10, 1), np.float32),
        [ReviewSpan("song", "Song", "song", 0, 10)],
        sample_rate=1000,
        settings=ReviewSettings(head_seconds=0.0001, tail_seconds=0.0001, tone_seconds=0.0001),
    )
    assert _audio_pieces(result) == [(0, 1), (9, 10)]
    assert len(result.audio) == 3


def test_retained_output_can_be_disk_backed_without_truncating_complete_audio(tmp_path):
    source = np.memmap(tmp_path / "source.pcm", mode="w+", shape=(12_000, 2), dtype=np.float32)
    source[:] = 0.125
    result = shorten_review(
        source,
        [ReviewSpan("speech", "Full speech", "speech", 0, 12_000, keep_complete=True)],
        sample_rate=1000,
        settings=_settings(),
        output_path=tmp_path / "review.pcm",
    )
    assert isinstance(result.audio, np.memmap)
    assert result.audio.shape == source.shape
    assert not np.shares_memory(source, result.audio)
    np.testing.assert_array_equal(result.audio, source)


@pytest.mark.parametrize(
    "spans",
    [
        [ReviewSpan("", "Empty ID", "song", 0, 100)],
        [ReviewSpan("repeat", "A", "song", 0, 50), ReviewSpan("repeat", "B", "song", 50, 100)],
        [ReviewSpan("bad", "Outside", "song", 0, 101)],
        [ReviewSpan("bad", "Backwards", "song", 50, 49)],
    ],
)
def test_invalid_occurrences_are_rejected(spans):
    with pytest.raises(ValueError):
        shorten_review(np.zeros((100, 2), np.float32), spans, sample_rate=1000)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "2", 10**1000])
@pytest.mark.parametrize(
    "field", ["head_seconds", "tail_seconds", "clip_threshold_seconds", "tone_seconds"]
)
def test_settings_reject_invalid_values(field, value):
    with pytest.raises(ValueError):
        ReviewSettings(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_seconds", 3601),
        ("tail_seconds", 3601),
        ("clip_threshold_seconds", 86401),
        ("tone_seconds", 11),
    ],
)
def test_settings_reject_unbounded_work(field, value):
    with pytest.raises(ValueError):
        ReviewSettings(**{field: value})


@pytest.mark.parametrize(
    "options",
    [
        {"sample_rate": 0},
        {"sample_rate": True},
        {"selection": (0, 0)},
        {"selection": (-1, 10)},
        {"selection": (0, 101)},
        {"protected": [(0, 101)]},
        {"protected": [(10, 9)]},
        {"selection": (0, 1.5)},
    ],
)
def test_invalid_frame_ranges_are_rejected(options):
    with pytest.raises(ValueError):
        shorten_review(np.zeros((100, 2), np.float32), [], **{"sample_rate": 1000, **options})
