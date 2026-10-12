"""Voice pack names, coverage, the join cache and the natural join on synthetic audio."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from autodj import voice_packs as vp
from autodj.voice_packs import (
    JoinCache,
    classify,
    coverage,
    crossfade_join,
    fade_edges,
    join_voice,
    match_loudness,
    pieces_for,
    scan_pack,
    signature,
    speech_level_db,
    trim_silence,
)

SR = 44100


def _ms(value: float) -> int:
    return round(SR * value / 1000)


def _tone(seconds: float, amplitude: float, freq: float = 220.0) -> np.ndarray:
    """A stereo tone with 10 ms raised-cosine ramps, standing in for a word."""
    t = np.arange(round(SR * seconds)) / SR
    wave = amplitude * np.sin(2 * np.pi * freq * t)
    ramp = _ms(10)
    window = np.ones_like(wave)
    window[:ramp] = 0.5 - 0.5 * np.cos(np.pi * np.arange(ramp) / ramp)
    window[-ramp:] = window[:ramp][::-1]
    return np.stack([wave * window] * 2, axis=1).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros((round(SR * seconds), 2), np.float32)


def _word(amplitude: float, freq: float = 220.0, pad: float = 0.3) -> np.ndarray:
    return np.concatenate([_silence(pad), _tone(0.4, amplitude, freq), _silence(pad)])


def _loud_spans(audio: np.ndarray, gate: float) -> list[tuple[int, int]]:
    """Start and end samples of the runs above *gate*, from 1 ms RMS blocks."""
    block = _ms(1)
    count = len(audio) // block
    levels = np.sqrt(np.mean(audio[: count * block, 0].reshape(count, block) ** 2, axis=1))
    loud = levels >= gate
    spans: list[tuple[int, int]] = []
    for index, flag in enumerate(loud):
        if flag and (not spans or spans[-1][1] != index):
            spans.append((index, index + 1))
        elif flag:
            spans[-1] = (spans[-1][0], index + 1)
    return [(start * block, end * block) for start, end in spans]


# -- trimming ---------------------------------------------------------------


def test_trim_keeps_a_short_lead_and_tail_around_the_word() -> None:
    trimmed = trim_silence(_word(0.5))
    # The word's ramps dip under the gate for well under a millisecond.
    expected = _ms(400 + vp.LEAD_MS + vp.TAIL_MS)
    assert expected - _ms(2) <= len(trimmed) <= expected


def test_trim_never_cuts_a_pause_inside_the_speech() -> None:
    inner = np.concatenate([_tone(0.2, 0.5), _silence(0.2), _tone(0.2, 0.5)])
    padded = np.concatenate([_silence(0.5), inner, _silence(0.5)])
    trimmed = trim_silence(padded)
    assert len(trimmed) >= len(inner)
    spans = _loud_spans(trimmed, 0.01)
    assert len(spans) == 2
    assert spans[1][0] - spans[0][1] >= _ms(190)


def test_trim_keeps_quiet_noise_and_drops_a_silent_piece() -> None:
    rng = np.random.default_rng(1)
    hiss = (rng.standard_normal((SR, 2)) * 0.05).astype(np.float32)
    # Noise only 20 dB under the peak is above the gentle threshold: kept whole.
    assert len(trim_silence(hiss)) == SR
    assert len(trim_silence(_silence(0.2))) == 0
    assert len(trim_silence(_silence(0.0))) == 0


# -- loudness ---------------------------------------------------------------


def test_loudness_match_brings_pieces_to_one_level() -> None:
    loud, quiet = _tone(0.4, 0.5), _tone(0.4, 0.1, 330.0)
    assert speech_level_db(loud) - speech_level_db(quiet) == pytest.approx(14.0, abs=0.2)
    matched = match_loudness([loud, quiet])
    assert speech_level_db(matched[0]) == pytest.approx(speech_level_db(matched[1]), abs=0.1)
    # The pieces meet in the middle, so the pack's own level is kept.
    middle = (speech_level_db(loud) + speech_level_db(quiet)) / 2
    assert speech_level_db(matched[0]) == pytest.approx(middle, abs=0.1)


def test_loudness_match_limits_the_gain_and_the_peak() -> None:
    loud, faint = _tone(0.4, 0.9), _tone(0.4, 0.9 * 10 ** (-40 / 20))
    matched = match_loudness([loud, faint])
    # Each piece moves at most 12 dB, so 40 dB apart stays 16 dB apart.
    assert speech_level_db(matched[0]) - speech_level_db(matched[1]) == pytest.approx(
        40 - 2 * vp.MAX_GAIN_DB, abs=0.2
    )
    hot = match_loudness([_tone(0.4, 0.95), _tone(0.4, 0.6)])
    assert max(float(np.abs(piece).max()) for piece in hot) <= vp.PEAK_CEILING + 1e-6


def test_loudness_match_leaves_silence_alone() -> None:
    silent = [_silence(0.1), _silence(0.1)]
    assert match_loudness(silent)[0] is silent[0]
    assert speech_level_db(_silence(0.1)) == -np.inf


# -- joining ----------------------------------------------------------------


def test_join_leaves_no_gap_between_the_words() -> None:
    joined = join_voice([_word(0.5), _word(0.5, 330.0)])
    spans = _loud_spans(joined, 0.5 * 10 ** (vp.SILENCE_DB / 20))
    assert len(spans) == 1  # one continuous phrase above the silence threshold
    words = _loud_spans(joined, 0.05)
    assert len(words) == 2
    gap = words[1][0] - words[0][1]
    # The words' 10 ms ramps sit under -20 dB for a few ms each; no silence is added.
    assert 0 <= gap <= _ms(25)
    # Two words, one lead and one tail: the overlap ate the inner margins.
    total = _ms(800 + vp.LEAD_MS + vp.TAIL_MS)
    assert total - _ms(4) <= len(joined) <= total


def test_join_has_no_discontinuity_at_the_seam() -> None:
    # Words cut hard at full level, the worst case for a click.
    t = np.arange(_ms(300)) / SR
    a = np.stack([0.5 * np.sin(2 * np.pi * 220 * t)] * 2, axis=1).astype(np.float32)
    b = np.stack([0.5 * np.sin(2 * np.pi * 330 * t + 1.0)] * 2, axis=1).astype(np.float32)
    natural = max(float(np.abs(np.diff(piece[:, 0])).max()) for piece in (a, b))
    hard = float(np.abs(np.diff(np.concatenate([a, b])[:, 0])).max())
    assert hard > 5 * natural  # a plain splice clicks
    joined = join_voice([a, b])
    steps = np.abs(np.diff(joined[:, 0]))
    assert float(steps.max()) <= 2 * natural
    # The ends are faded, so the clip starts and stops at silence.
    assert abs(float(joined[0, 0])) < 0.01 and abs(float(joined[-1, 0])) < 0.01


def test_join_is_equal_power_through_the_overlap() -> None:
    rng = np.random.default_rng(7)
    a = (rng.standard_normal((SR, 2)) * 0.1).astype(np.float32)
    b = (rng.standard_normal((SR, 2)) * 0.1).astype(np.float32)
    overlap = _ms(vp.OVERLAP_MS)
    joined = crossfade_join([a, b], overlap)
    assert len(joined) == 2 * SR - overlap
    seam = joined[SR - overlap : SR]
    rms = float(np.sqrt(np.mean(seam**2)))
    assert rms == pytest.approx(0.1, rel=0.15)


def test_join_matches_loudness_and_shrinks_the_overlap_for_short_pieces() -> None:
    joined = join_voice([_word(0.5), _word(0.05, 330.0)])
    words = _loud_spans(joined, 0.01)
    first = joined[words[0][0] : words[0][1]]
    second = joined[words[-1][0] : words[-1][1]]
    assert speech_level_db(first) == pytest.approx(speech_level_db(second), abs=0.5)
    tiny = [np.ones((10, 2), np.float32), np.ones((10, 2), np.float32)]
    assert len(crossfade_join(tiny, 1000)) == 15
    assert len(crossfade_join([np.ones((1, 2)), np.ones((1, 2))], 1000)) == 2
    assert len(fade_edges(np.ones((1, 2)))) == 1


def test_join_refuses_nothing_and_silence() -> None:
    with pytest.raises(ValueError, match="no pieces"):
        join_voice([])
    with pytest.raises(ValueError, match="silent"):
        join_voice([_word(0.5), _silence(0.5)])


# -- names and coverage -----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("hr_05.mp3", ("hour", 5)),
        ("HR_05O.MP3", ("exact", 5)),
        ("hrs05_o.mp3", ("exact", 5)),
        ("HRS14-o.mp3", ("exact", 14)),
        ("hrs05_0.mp3", "not an hour, minute, intro, outro, am or pm name"),
        ("hour-17.ogg", ("hour", 17)),
        ("h23.wav", ("hour", 23)),
        ("m_56.mp3", ("minute", 56)),
        ("min 07.flac", ("minute", 7)),
        ("Intro.wav", ("intro", 0)),
        ("pm.mp3", ("pm", 0)),
        ("readme.txt", None),
        (".hr_05.mp3", None),
        ("hm_05.mp3", "needs h (hour) or m (minute) before the number, not both or neither"),
        ("x_05.mp3", "needs h (hour) or m (minute) before the number, not both or neither"),
        ("hr_24.mp3", "hour over 23"),
        ("m_60.mp3", "minute over 59"),
        ("m_05o.mp3", "o after a minute number"),
        ("min05_o.mp3", "o after a minute number"),
        ("hr_5.mp3", "not an hour, minute, intro, outro, am or pm name"),
        ("voice2 hr_05.mp3", "not an hour, minute, intro, outro, am or pm name"),
    ],
)
def test_names_are_read_strictly(name: str, expected: object) -> None:
    assert classify(name) == expected


def _pack(folder: Path, names: list[str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")
    return folder


def _radioboss(folder: Path) -> Path:
    names = [f"hr_{hour:02d}.mp3" for hour in range(24)]
    names += [f"hr_{hour:02d}o.mp3" for hour in range(24)]
    names += [f"m_{minute:02d}.mp3" for minute in range(60)]
    return _pack(folder, names)


def test_a_full_radioboss_pack_covers_every_minute(tmp_path: Path) -> None:
    pack = scan_pack(_radioboss(tmp_path / "Studio"))
    assert pack.name == "Studio" and pack.found and pack.problems == ()
    for hour_format in ("12", "24"):
        cov = coverage(pack, hour_format)
        assert cov.minutes_covered == 1440
        assert cov.describe(pack) == (
            "1440 of 1440 minutes covered. Hour files missing: none. "
            "Exact-hour files missing: none. Minute files missing: none."
        )
    folder = tmp_path / "Studio"
    assert pieces_for(pack, 5, 56, "24") == [folder / "hr_05.mp3", folder / "m_56.mp3"]
    assert pieces_for(pack, 5, 0, "24") == [folder / "hr_05o.mp3"]


def test_missing_and_ambiguous_files_are_reported_not_guessed(tmp_path: Path) -> None:
    folder = _pack(
        tmp_path / "Night",
        [
            *(f"hr_{hour:02d}.mp3" for hour in range(24) if hour not in (3, 4, 5)),
            "hr_05.mp3",
            "H05.wav",
            "hr_01o.mp3",
            *(f"m_{minute:02d}.mp3" for minute in range(1, 60) if minute not in (7, 8, 9, 15)),
            "hm_10.mp3",
            "am.mp3",
            "notes.txt",
        ],
    )
    (folder / "sub").mkdir()
    pack = scan_pack(folder)
    assert 5 not in pack.hours  # two files for hour 05: neither is used
    cov = coverage(pack, "24")
    assert cov.missing_hours == [3, 4, 5]
    assert cov.missing_minutes == [7, 8, 9, 15]
    text = cov.describe(pack)
    assert "Hour files missing: 03 to 05." in text
    assert "Exact-hour files missing: 00, 02 to 23." in text
    assert "Minute files missing: 07 to 09, 15." in text
    assert "H05.wav and hr_05.mp3: more than one file for hour 05, none used" in text
    assert "hm_10.mp3: needs h (hour) or m (minute)" in text
    assert "am.mp3: am without the other, not used" in text
    assert "Also uses" not in text
    # No exact-hour file and no m_00: minute 00 is missing too.
    assert pieces_for(pack, 2, 0, "24") is None
    assert pieces_for(pack, 1, 0, "24") == [folder / "hr_01o.mp3"]
    assert cov.minutes_covered == 21 * 55 + 1


def test_a_missing_folder_is_reported(tmp_path: Path) -> None:
    pack = scan_pack(tmp_path / "Gone")
    assert not pack.found
    assert coverage(pack, "12").describe(pack) == "Folder not found."


def test_twelve_hour_mode_falls_back_to_the_twelve_hour_file_and_adds_am_pm(
    tmp_path: Path,
) -> None:
    folder = _pack(
        tmp_path / "Twelve",
        [
            *(f"hr_{hour:02d}.mp3" for hour in range(1, 13)),
            *(f"hr_{hour:02d}o.mp3" for hour in range(1, 13)),
            "m_00.mp3",
            *(f"m_{minute:02d}.mp3" for minute in range(1, 60)),
            "intro.wav",
            "outro.wav",
            "am.wav",
            "pm.wav",
        ],
    )
    pack = scan_pack(folder)
    assert pieces_for(pack, 13, 5, "12") == [
        folder / "intro.wav",
        folder / "hr_01.mp3",
        folder / "m_05.mp3",
        folder / "pm.wav",
        folder / "outro.wav",
    ]
    assert pieces_for(pack, 0, 0, "12")[1:3] == [folder / "hr_12o.mp3", folder / "am.wav"]
    assert coverage(pack, "12").minutes_covered == 1440
    # In 24-hour mode a 12-hour file would say the wrong hour; am and pm are not added.
    assert pieces_for(pack, 13, 5, "24") is None
    assert pieces_for(pack, 1, 5, "24") == [
        folder / "intro.wav",
        folder / "hr_01.mp3",
        folder / "m_05.mp3",
        folder / "outro.wav",
    ]
    cov = coverage(pack, "24")
    assert cov.missing_hours == [0, *range(13, 24)]
    assert "Also uses: intro, outro, am, pm." in cov.describe(pack)


def test_minute_zero_without_an_exact_file_uses_the_hour_and_m_00(tmp_path: Path) -> None:
    folder = _pack(tmp_path / "Plain", ["hr_07.mp3", "m_00.mp3"])
    assert pieces_for(scan_pack(folder), 7, 0, "24") == [folder / "hr_07.mp3", folder / "m_00.mp3"]


# -- cache ------------------------------------------------------------------


def test_cache_is_dropped_when_a_file_changes_and_capped_in_size(tmp_path: Path) -> None:
    folder = _pack(tmp_path / "P", ["hr_01.mp3", "m_01.mp3"])
    paths = [folder / "hr_01.mp3", folder / "m_01.mp3"]
    cache = JoinCache(max_bytes=3 * 800)
    audio = np.zeros((100, 2), np.float32)  # 800 bytes
    key = ("P", 1, 1, "24")
    before = signature(paths)
    cache.put(key, before, audio)
    hit = cache.get(key, before)
    assert hit is not None and not hit.flags.writeable
    audio[0, 0] = 1.0
    assert hit[0, 0] == 0.0  # the cache holds its own copy
    (folder / "m_01.mp3").write_bytes(b"a longer replacement")
    after = signature(paths)
    assert after != before
    assert cache.get(key, after) is None
    assert cache.get(key, before) is None  # the stale entry is gone
    for minute in range(5):
        cache.put(("P", 1, minute, "24"), after, audio)
    cache.put(("P", 1, 4, "24"), after, audio)
    assert cache.get(("P", 1, 0, "24"), after) is None
    assert cache.get(("P", 1, 1, "24"), after) is None
    assert cache.get(("P", 1, 4, "24"), after) is not None
    cache.put(("P", 2, 0, "24"), after, np.zeros((1000, 2), np.float32))
    assert cache.get(("P", 2, 0, "24"), after) is None  # larger than the whole cache
    (folder / "m_01.mp3").unlink()
    with pytest.raises(OSError):
        signature(paths)
