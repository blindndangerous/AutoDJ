"""Recorded time, TTS fallback and top-of-hour trigger behavior."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import soundfile as sf

from autodj.config import TimeChecksConfig, load_config
from autodj.doctor import CheckStatus, _time_checks_check
from autodj.time_checks import (
    Rendered,
    SettingsBody,
    TimeChecks,
    clip_path,
    missing_minutes,
    spoken_text,
)


def _settings(**changes: object) -> SettingsBody:
    values: dict[str, object] = {
        "enabled": False,
        "hour_format": "12",
        "rule": "off",
        "enabled_packs": [],
        "pack_order": "rotate",
    }
    return SettingsBody.model_validate({**values, **changes})


def _checks(tmp_path: Path) -> TimeChecks:
    cfg = load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)})
    return TimeChecks(cfg, "America/Denver")


def test_natural_speech_and_recording_names(tmp_path: Path) -> None:
    denver = ZoneInfo("America/Denver")
    at = datetime(2026, 10, 11, 3, 5, tzinfo=denver)
    assert spoken_text(at, "12") == "It's 3:05"
    assert spoken_text(at.replace(hour=15), "24") == "It's 15:05"
    assert spoken_text(at.replace(minute=0), "12") == "It's 3 o'clock"
    assert spoken_text(at.replace(hour=15, minute=0), "24") == "It's 15 hundred"
    assert spoken_text(at.replace(hour=0, minute=7), "12") == "It's 12:07"
    assert spoken_text(at.replace(hour=0, minute=7), "24") == "It's 0:07"
    for hour_format in ("12", "24"):
        assert spoken_text(at.replace(hour=0, minute=0), hour_format) == "It's midnight"
        assert spoken_text(at.replace(hour=12, minute=0), hour_format) == "It's noon"
    (tmp_path / "03_05.wav").write_bytes(b"audio")
    assert clip_path(tmp_path, at) == tmp_path / "03_05.wav"
    assert "03_05" not in missing_minutes(tmp_path)
    assert len(missing_minutes(tmp_path)) == 1439


def test_recording_wraps_intro_and_outro_as_one_clip(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    checks.folder.mkdir(parents=True)
    local = datetime(2026, 10, 11, 3, 5, tzinfo=checks.zone)
    for filename in ("intro.wav", "03_05.wav", "outro.wav"):
        sf.write(checks.folder / filename, np.zeros((441, 2)), 44100)
    checks.config.intro_clip = str(checks.folder / "intro.wav")
    checks.config.outro_clip = str(checks.folder / "outro.wav")
    rendered = checks.render(local)
    assert rendered is not None and rendered.audio.shape == (1323, 2)
    assert rendered.source == "recorded clip"


_TTS_SCRIPT = (
    "import sys,wave; text=sys.stdin.read(); "
    "open(sys.argv[2],'a').write(text+'\\n'); "
    "w=wave.open(sys.argv[1],'wb'); w.setnchannels(1); "
    "w.setsampwidth(2); w.setframerate(44100); w.writeframes(b'\\0\\0'*100); w.close()"
)


def test_tts_fallback_writes_wav_once_and_caches_per_command_and_text(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    log = tmp_path / "spoken.txt"
    checks.config.tts_command = [sys.executable, "-c", _TTS_SCRIPT, "{out}", str(log)]
    at = datetime(2026, 10, 11, 3, 5, tzinfo=checks.zone)
    first = checks.render(at)
    assert first is not None and first.audio.shape == (100, 2)
    assert first.source == "text to speech"
    assert checks.render(at) is not None
    assert log.read_text(encoding="utf-8") == "It's 3:05\n"
    # Another voice is another cache entry.
    checks.config.tts_command = [sys.executable, "-c", _TTS_SCRIPT, "{out}", str(log), "x"]
    assert checks.render(at) is not None
    assert log.read_text(encoding="utf-8") == "It's 3:05\nIt's 3:05\n"
    assert len(list(checks.cache.glob("*.wav"))) == 2


def test_tts_text_placeholder_is_one_argument_without_a_shell(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    log = tmp_path / "argv.txt"
    script = (
        "import sys,wave; open(sys.argv[3],'w').write(repr(sys.argv[2])); "
        "w=wave.open(sys.argv[1],'wb'); w.setnchannels(1); "
        "w.setsampwidth(2); w.setframerate(44100); w.writeframes(b'\\0\\0'); w.close()"
    )
    checks.config.tts_command = [sys.executable, "-c", script, "{out}", "{text}", str(log)]
    assert checks.render(datetime(2026, 10, 11, 15, 0, tzinfo=checks.zone)) is not None
    assert log.read_text(encoding="utf-8") == repr("It's 3 o'clock")


def test_failing_or_slow_tts_skips_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import time_checks

    checks = _checks(tmp_path)
    at = datetime(2026, 10, 11, 3, 5, tzinfo=checks.zone)
    assert checks.render(at) is None
    assert checks.render(at) is None
    assert caplog.text.count("no voice pack, recorded clip or working TTS command") == 1
    checks.config.tts_command = [sys.executable, "-c", "import sys; sys.exit(3)", "{out}"]
    assert checks.render(at) is None
    assert "TTS command failed (exit 3)" in caplog.text
    # A voice that speaks elsewhere (a screen reader's SAPI bridge) writes a header only.
    header_only = (
        "import sys,wave; w=wave.open(sys.argv[1],'wb'); w.setnchannels(1); "
        "w.setsampwidth(2); w.setframerate(44100); w.close()"
    )
    checks.config.tts_command = [sys.executable, "-c", header_only, "{out}"]
    assert checks.render(at) is None
    assert "TTS command wrote no audio" in caplog.text
    monkeypatch.setattr(time_checks, "TTS_TIMEOUT_S", 0.5)
    checks.config.tts_command = [sys.executable, "-c", "import time; time.sleep(5)", "{out}"]
    assert checks.render(at.replace(minute=6)) is None
    assert "TTS unavailable" in caplog.text
    assert list(checks.cache.glob("*.wav")) == []


def test_undecodable_clip_skips_the_check(tmp_path: Path, caplog) -> None:
    checks = _checks(tmp_path)
    checks.folder.mkdir(parents=True)
    (checks.folder / "03_05.wav").write_bytes(b"not audio")
    checks.config.intro_clip = str(tmp_path / "missing-intro.wav")
    assert checks.render(datetime(2026, 10, 11, 3, 5, tzinfo=checks.zone)) is None
    assert "could not be decoded" in caplog.text


class _Clock:
    """Stands in for ``datetime`` in :mod:`autodj.time_checks`, at a set instant."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
        from autodj import time_checks

        self.at = at
        clock = self

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return clock.at.astimezone(tz)

        monkeypatch.setattr(time_checks, "datetime", Fixed)


def test_hour_rule_plays_once_after_the_song_that_crosses_the_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(monkeypatch, datetime(2026, 10, 11, 9, 59, tzinfo=UTC))
    checks = _checks(tmp_path)
    audio = np.zeros((10, 2), np.float32)
    monkeypatch.setattr(checks, "render", lambda pack=None: Rendered(audio, "recorded clip"))
    checks.tick(clock.at, True)
    assert checks.due_audio() is None  # the rule is off
    checks.save(_settings(enabled=True, hour_format="24", rule="after_hour_song"))
    clock.at = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    checks.tick(clock.at, True)
    checks.tick(clock.at, True)
    assert checks.due_audio() is audio
    assert checks.due_audio() is None
    # No song was playing as 11:00 passed: nothing is due.
    clock.at = datetime(2026, 10, 11, 11, 0, tzinfo=UTC)
    checks.tick(clock.at, False)
    assert checks.due_audio() is None
    # Turning the rule off drops a pending check.
    clock.at = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    checks.tick(clock.at, True)
    checks.save(_settings(enabled=True, hour_format="24", rule="off"))
    assert checks.due_audio() is None


def test_scheduled_check_in_the_hour_replaces_the_hour_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    clock = _Clock(monkeypatch, datetime(2026, 10, 11, 9, 59, tzinfo=UTC))
    checks = _checks(tmp_path)
    checks.save(_settings(enabled=True, hour_format="12", rule="after_hour_song"))
    audio = np.zeros((10, 2), np.float32)
    monkeypatch.setattr(checks, "render", lambda pack=None: Rendered(audio, "recorded clip"))
    clock.at = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    checks.tick(clock.at, True)
    bridge = SimpleNamespace(liner_scheduler=MagicMock(), station=None)
    checks.fire(bridge)
    bridge.liner_scheduler.play_audio.assert_called_once_with(audio)
    clock.at = datetime(2026, 10, 11, 10, 3, tzinfo=UTC)
    assert checks.due_audio() is None


def test_fire_says_why_nothing_played(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    checks = _checks(tmp_path)
    with pytest.raises(ValueError, match="server audio or stream mode"):
        checks.fire(SimpleNamespace(liner_scheduler=None, station=None))
    idle = SimpleNamespace(liner_scheduler=MagicMock(), station=SimpleNamespace(state="idle"))
    with pytest.raises(ValueError, match="stream must be playing"):
        checks.fire(idle)
    monkeypatch.setattr(checks, "render", lambda pack=None: None)
    with pytest.raises(ValueError, match="No voice pack, recorded clip"):
        checks.fire(SimpleNamespace(liner_scheduler=MagicMock(), station=None))


def test_saved_switches_reload_and_a_wrong_version_stops_startup(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    checks.save(_settings(enabled=True, hour_format="24", rule="after_hour_song"))
    reloaded = _checks(tmp_path)
    assert reloaded.settings.hour_format == "24"
    assert reloaded.status()["timezone"] == "America/Denver"
    state = json.loads(reloaded.state_path.read_text(encoding="utf-8"))
    state["version"] = 0
    reloaded.state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported time checks state version"):
        _checks(tmp_path)


def test_config_rejects_bad_tts_templates() -> None:
    with pytest.raises(ValueError, match=r"contain \{out\} once"):
        TimeChecksConfig(tts_command=["espeak-ng", "{text}"])
    with pytest.raises(ValueError, match="supports only"):
        TimeChecksConfig(tts_command=["espeak-ng", "{out}", "{unsafe}"])


def test_doctor_reports_missing_minutes_and_fallback(tmp_path: Path) -> None:
    cfg = load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)})
    check = _time_checks_check(cfg)
    assert check.status is CheckStatus.PASS
    assert check.summary == "disabled; 1440 of 1440 minute clips missing"
    cfg.time_checks.enabled = True
    check = _time_checks_check(cfg)
    assert check.status is CheckStatus.WARN
    assert check.summary == "1440 of 1440 minute clips missing; no TTS fallback"
    assert check.detail["missing_examples"][:2] == ["00_00", "00_01"]
    cfg.time_checks.tts_command = [sys.executable, "{out}"]
    assert _time_checks_check(cfg).status is CheckStatus.PASS
    cfg.time_checks.tts_command = ["no-such-tts-program", "{out}"]
    cfg.time_checks.intro_clip = str(tmp_path / "intro.wav")
    cfg.time_checks.clips_folder = str(tmp_path / "nowhere")
    check = _time_checks_check(cfg)
    assert check.status is CheckStatus.WARN
    assert "clips_folder is not a folder" in check.summary
    assert "intro_clip not found" in check.summary
    assert "TTS program no-such-tts-program not found" in check.summary


def test_doctor_reads_the_switches_saved_from_settings(tmp_path: Path) -> None:
    cfg = load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)})
    state = Path((cfg.index.stations_dir / "default")) / "time_checks.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        '{"version":1,"settings":{"enabled":true,"hour_format":"12","rule":"off"}}',
        encoding="utf-8",
    )
    assert _time_checks_check(cfg).status is CheckStatus.WARN
    state.write_text("{", encoding="utf-8")
    check = _time_checks_check(cfg)
    assert check.status is CheckStatus.FAIL
    assert check.repair is not None


# -- voice packs ------------------------------------------------------------


def _word(freq: float, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(4410) / 44100
    tone = amplitude * np.sin(2 * np.pi * freq * t) * np.hanning(4410)
    silence = np.zeros(2205)
    return np.concatenate([silence, tone, silence])


def _write_pack(
    folder: Path, hours: range | list[int], minutes: range | list[int], freq: float = 220.0
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for hour in hours:
        sf.write(folder / f"hr_{hour:02d}.wav", _word(freq), 44100)
        sf.write(folder / f"hr_{hour:02d}o.wav", _word(freq), 44100)
    for minute in minutes:
        sf.write(folder / f"m_{minute:02d}.wav", _word(freq * 1.5), 44100)
    return folder


def _pack_checks(tmp_path: Path, *folders: Path) -> TimeChecks:
    cfg = load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)})
    cfg.time_checks = TimeChecksConfig(voice_packs=[str(folder) for folder in folders])
    return TimeChecks(cfg, "UTC")


def _source(checks: TimeChecks, at: datetime, pack: str | None = None) -> str | None:
    rendered = checks.render(at, pack=pack)
    return None if rendered is None else rendered.source


def test_voice_pack_speaks_first_and_falls_back_to_clips_then_tts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pack = _write_pack(tmp_path / "packs" / "Studio", range(24), range(1, 30))
    checks = _pack_checks(tmp_path, pack)
    assert checks.settings.enabled_packs == ["Studio"]
    at = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)
    rendered = checks.render(at)
    assert rendered is not None and rendered.source == "voice pack Studio"
    # Two 100 ms words, each with its 40 ms of margins, overlapped by 40 ms.
    assert 0.2 < len(rendered.audio) / 44100 < 0.25
    assert _source(checks, at.replace(minute=0)) == "voice pack Studio"
    # Minute 45 has no file: the recorded clip speaks, then text to speech.
    checks.folder.mkdir(parents=True)
    sf.write(checks.folder / "03_45.wav", np.zeros((441, 2)), 44100)
    assert _source(checks, at.replace(minute=45)) == "recorded clip"
    log = tmp_path / "spoken.txt"
    checks.config.tts_command = [sys.executable, "-c", _TTS_SCRIPT, "{out}", str(log)]
    assert _source(checks, at.replace(minute=46)) == "text to speech"
    # An undecodable piece is skipped with a warning, and the next source speaks.
    (pack / "m_05.wav").write_bytes(b"not audio")
    assert _source(checks, at) == "text to speech"
    assert "Voice pack Studio skipped at 03:05" in caplog.text


def test_voice_pack_join_is_cached_until_a_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import time_checks

    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    joins: list[int] = []
    real = time_checks.join_voice

    def counting(pieces: list[np.ndarray]) -> np.ndarray:
        joins.append(len(pieces))
        return real(pieces)

    monkeypatch.setattr(time_checks, "join_voice", counting)
    at = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)
    first = checks.render(at)
    assert first is not None and checks.render(at) is not None
    assert joins == [2]
    sf.write(pack / "m_05.wav", np.concatenate([_word(330.0), _word(330.0)]), 44100)
    second = checks.render(at)
    assert joins == [2, 2]
    assert second is not None and len(second.audio) > len(first.audio)


def test_several_packs_take_turns_or_pick_at_random(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import time_checks

    first = _write_pack(tmp_path / "A", [3], [5])
    second = _write_pack(tmp_path / "B", [3], [5, 6], freq=330.0)
    checks = _pack_checks(tmp_path, first, second)
    at = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)
    assert [_source(checks, at) for _ in range(3)] == [
        "voice pack A",
        "voice pack B",
        "voice pack A",
    ]
    # It is the turn of A, which cannot say 03:06: B speaks instead of a clip.
    assert _source(checks, at.replace(minute=6)) == "voice pack B"
    # A test of one pack does not move the rotation.
    assert _source(checks, at, "B") == "voice pack B"
    assert _source(checks, at) == "voice pack A"
    # A tested pack that cannot say the minute falls back like any other source.
    assert _source(checks, at.replace(minute=6), "A") is None
    checks.save(_settings(enabled_packs=["B"]))
    assert _source(checks, at) == "voice pack B"
    checks.save(_settings(enabled_packs=[]))
    assert _source(checks, at) is None
    checks.save(_settings(enabled_packs=["A", "B"], pack_order="random"))
    monkeypatch.setattr(time_checks.secrets, "randbelow", lambda count: count - 1)
    assert _source(checks, at) == "voice pack B"


def test_status_lists_packs_by_name_with_coverage_and_no_paths(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path / "secret-folder" / "Studio", range(24), range(1, 60))
    checks = _pack_checks(tmp_path, pack, tmp_path / "Gone")
    checks.save(_settings(enabled_packs=["Studio"], hour_format="24"))
    status = checks.status()
    assert status["enabled_packs"] == ["Studio"] and status["pack_order"] == "rotate"
    no_dates = {
        "not_before": None,
        "not_after": None,
        "dates_text": "",
        "playable": True,
        "unplayable_text": "",
    }
    assert status["packs"] == [
        {
            "name": "Studio",
            "enabled": True,
            "minutes_covered": 1440,
            "summary": "1440 of 1440 minutes covered. Hour files missing: none. "
            "Exact-hour files missing: none. Minute files missing: none.",
            "source": "config",
            "removable": False,
            "shared_with_stations": False,
            **no_dates,
        },
        {
            "name": "Gone",
            "enabled": False,
            "minutes_covered": 0,
            "summary": "Folder not found.",
            "source": "config",
            "removable": False,
            "shared_with_stations": False,
            **no_dates,
        },
    ]
    assert "secret-folder" not in json.dumps(status)


def test_saved_settings_fill_new_keys_from_config(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    checks.state_path.parent.mkdir(parents=True, exist_ok=True)
    checks.state_path.write_text(
        '{"version":1,"settings":{"enabled":true,"hour_format":"24","rule":"off"}}',
        encoding="utf-8",
    )
    reloaded = _pack_checks(tmp_path, pack)
    assert reloaded.settings.enabled_packs == ["Studio"]
    assert reloaded.settings.hour_format == "24"


def test_settings_and_config_refuse_bad_pack_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="each pack once"):
        _settings(enabled_packs=["A", "A"])
    with pytest.raises(ValueError, match="rotate or random"):
        _settings(pack_order="shuffle")
    with pytest.raises(ValueError, match="pack_order"):
        TimeChecksConfig(pack_order="shuffle")
    with pytest.raises(TypeError, match="voice_packs"):
        TimeChecksConfig(voice_packs=[""])
    with pytest.raises(TypeError, match="voice_packs"):
        TimeChecksConfig(voice_packs="folder")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="distinct names"):
        TimeChecksConfig(voice_packs=[str(tmp_path / "a" / "Studio"), str(tmp_path / "studio")])
    assert TimeChecksConfig(voice_packs=["~/Studio"]).voice_packs == [
        str(Path("~/Studio").expanduser())
    ]


def test_unknown_config_keys_are_warned_and_ignored(caplog: pytest.LogCaptureFixture) -> None:
    section = TimeChecksConfig.from_dict({"voice_packs": [], "pack_folders": ["x"]})
    assert section.voice_packs == []
    assert "Ignoring unknown config keys in [time_checks]: ['pack_folders']" in caplog.text


def test_doctor_reports_each_pack_and_counts_pack_minutes_as_covered(tmp_path: Path) -> None:
    from autodj.doctor import _voice_pack_checks

    full = _write_pack(tmp_path / "Full", range(24), range(1, 60))
    partial = _write_pack(tmp_path / "Partial", range(23), range(1, 59))
    (partial / "hm_07.wav").write_bytes(b"x")
    cfg = load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)})
    cfg.time_checks = TimeChecksConfig(
        enabled=True, voice_packs=[str(full), str(partial), str(tmp_path / "Gone")]
    )
    checks = _voice_pack_checks(cfg)
    assert [check.name for check in checks] == [
        "voice-pack-Full",
        "voice-pack-Partial",
        "voice-pack-Gone",
    ]
    assert checks[0].status is CheckStatus.PASS
    assert checks[0].summary.startswith("enabled; 1440 of 1440 minutes covered.")
    assert checks[0].detail == str(full)
    assert checks[1].status is CheckStatus.WARN
    # 12-hour mode (the default): hour 23 uses hr_11, so only minute 59 is missing.
    assert "1416 of 1440 minutes covered. Hour files missing: none." in checks[1].summary
    assert "Minute files missing: 59." in checks[1].summary
    assert "hm_07.wav: needs h (hour) or m (minute)" in checks[1].summary
    assert checks[2].status is CheckStatus.WARN
    assert checks[2].summary == "enabled; Folder not found."
    # The full pack says every minute, so no TTS fallback is needed.
    assert _time_checks_check(cfg).status is CheckStatus.PASS
    state = Path((cfg.index.stations_dir / "default")) / "time_checks.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        '{"version":1,"settings":{"enabled":true,"hour_format":"12","rule":"off",'
        '"enabled_packs":["Partial","Old"],"pack_order":"rotate"}}',
        encoding="utf-8",
    )
    check = _time_checks_check(cfg)
    assert check.status is CheckStatus.WARN
    assert "saved voice pack Old is not found" in check.summary
    assert "no TTS fallback" in check.summary
    assert _voice_pack_checks(cfg)[0].summary.startswith("not enabled; ")
    state.write_text("{", encoding="utf-8")
    assert _voice_pack_checks(cfg)[0].summary.startswith("enabled; ")


# -- voice pack play dates ------------------------------------------------------


def test_a_voice_pack_outside_its_dates_is_passed_over_but_a_test_still_plays_it(
    tmp_path: Path,
) -> None:
    from datetime import date

    from autodj.clip_dates import ClipDates

    first = _write_pack(tmp_path / "Christmas", [3], [5])
    second = _write_pack(tmp_path / "Studio", [3], [5], freq=330.0)
    checks = _pack_checks(tmp_path, first, second)
    checks.save_pack_dates(
        "Christmas", ClipDates(not_before=date(2026, 12, 1), not_after=date(2026, 12, 26))
    )
    october = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)
    assert [_source(checks, october) for _ in range(3)] == ["voice pack Studio"] * 3
    assert _source(checks, october, "Christmas") == "voice pack Christmas"
    december = datetime(2026, 12, 24, 3, 5, tzinfo=UTC)
    assert {_source(checks, december) for _ in range(4)} == {
        "voice pack Christmas",
        "voice pack Studio",
    }
    # The dates are kept beside the switches and read again on start.
    reloaded = _pack_checks(tmp_path, first, second)
    assert reloaded.pack_dates == checks.pack_dates
    christmas = reloaded.status()["packs"][0]
    assert christmas["dates_text"] == "Plays from 1 December 2026 until 26 December 2026"
    state = json.loads(checks.state_path.read_text(encoding="utf-8"))
    assert state["pack_dates"] == {
        "Christmas": {"not_before": "2026-12-01", "not_after": "2026-12-26"}
    }
    # Switches saved later keep the dates; empty dates remove them and the key.
    checks.save(_settings(enabled_packs=["Studio"]))
    assert "Christmas" in _pack_checks(tmp_path, first, second).pack_dates
    checks.save_pack_dates("Christmas", ClipDates())
    assert "pack_dates" not in json.loads(checks.state_path.read_text(encoding="utf-8"))


def test_a_file_without_pack_dates_loads_unchanged_and_bad_ones_are_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    checks.save(_settings(enabled_packs=["Studio"]))
    before = checks.state_path.read_text(encoding="utf-8")
    assert "pack_dates" not in before  # an older AutoDJ reads it as before
    assert _pack_checks(tmp_path, pack).pack_dates == {}
    state = json.loads(before)
    state["pack_dates"] = {
        "Studio": {"not_before": "2026-12-26", "not_after": "2026-12-01"},
        "Night": {"not_after": "2026-12-26"},
    }
    state["future_key"] = 1
    checks.state_path.write_text(json.dumps(state), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="autodj.time_checks"):
        loaded = _pack_checks(tmp_path, pack)
    # A pack no longer configured keeps its dates for when it comes back.
    assert set(loaded.pack_dates) == {"Night"}
    assert loaded.settings.enabled_packs == ["Studio"]
    assert "ignoring the dates of voice pack Studio" in caplog.text
    assert "ignoring unknown key 'future_key'" in caplog.text


def test_unknown_keys_and_unread_pack_dates_are_saved_back_as_written(tmp_path: Path) -> None:
    from datetime import date

    from autodj.clip_dates import ClipDates

    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    checks.save(_settings(enabled_packs=["Studio"]))
    state = json.loads(checks.state_path.read_text(encoding="utf-8"))
    state["future_key"] = {"kept": [1, 2]}
    state["pack_dates"] = {"Night": {"not_after": "someday"}}
    checks.state_path.write_text(json.dumps(state), encoding="utf-8")
    loaded = _pack_checks(tmp_path, pack)
    assert loaded.pack_dates == {}
    loaded.save(_settings(enabled_packs=[]))
    loaded.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))
    saved = json.loads(loaded.state_path.read_text(encoding="utf-8"))
    assert saved["future_key"] == {"kept": [1, 2]}
    assert saved["pack_dates"] == {
        "Night": {"not_after": "someday"},
        "Studio": {"not_after": "2026-12-26"},
    }
    # New dates for that pack replace the ones that could not be read.
    loaded.save_pack_dates("Night", ClipDates())
    saved = json.loads(loaded.state_path.read_text(encoding="utf-8"))
    assert saved["pack_dates"] == {"Studio": {"not_after": "2026-12-26"}}
    assert saved["settings"]["enabled_packs"] == []


def test_a_switch_saved_while_dates_are_saved_keeps_both(tmp_path: Path) -> None:
    import threading
    from datetime import date

    from autodj.clip_dates import ClipDates

    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    real_write = checks._write
    started = threading.Event()
    other: list[threading.Thread] = []

    def slow_write(*args: object) -> None:
        if not other:
            # A switch save arrives in the middle of this dates save.
            thread = threading.Thread(target=checks.save, args=(_settings(hour_format="24"),))
            other.append(thread)
            thread.start()
            started.wait(0.2)
        real_write(*args)

    checks._write = slow_write  # type: ignore[method-assign]
    checks.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))
    other[0].join(5)
    saved = json.loads(checks.state_path.read_text(encoding="utf-8"))
    assert saved["settings"]["hour_format"] == "24"
    assert saved["pack_dates"] == {"Studio": {"not_after": "2026-12-26"}}


def test_a_time_checks_file_unreadable_at_start_is_never_saved_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    from autodj import time_checks
    from autodj.clip_dates import ClipDates

    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    checks.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))
    before = checks.state_path.read_text(encoding="utf-8")
    real_read = Path.read_text

    def busy(path: Path, *args: object, **kwargs: object) -> str:
        if path == checks.state_path:
            raise PermissionError(13, "Permission denied")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", busy)
    monkeypatch.setattr(time_checks, "load_settings", lambda cfg, paths=None: _settings())
    started = _pack_checks(tmp_path, pack)
    assert started.pack_dates == {}
    with pytest.raises(time_checks.TimeChecksUnreadable, match="nothing was saved"):
        started.save(_settings(hour_format="24"))
    with pytest.raises(time_checks.TimeChecksUnreadable):
        started.save_pack_dates("Studio", ClipDates())
    monkeypatch.setattr(Path, "read_text", real_read)
    assert checks.state_path.read_text(encoding="utf-8") == before
    # Readable again: the save starts from the file, so the dates stay.
    started.save(_settings(hour_format="24"))
    saved = json.loads(checks.state_path.read_text(encoding="utf-8"))
    assert saved["pack_dates"] == {"Studio": {"not_after": "2026-12-26"}}
    assert saved["settings"]["hour_format"] == "24"


def test_a_save_keeps_what_another_process_wrote_and_sets_a_damaged_file_aside(
    tmp_path: Path,
) -> None:
    from datetime import date

    from autodj.clip_dates import ClipDates

    pack = _write_pack(tmp_path / "Studio", [3], [5])
    checks = _pack_checks(tmp_path, pack)
    checks.save(_settings(enabled_packs=["Studio"]))
    # Another AutoDJ (or an editor) adds dates and a key of its own.
    state = json.loads(checks.state_path.read_text(encoding="utf-8"))
    state["pack_dates"] = {"Night": {"not_before": "2026-12-01"}}
    state["newer"] = True
    checks.state_path.write_text(json.dumps(state), encoding="utf-8")
    checks.save(_settings(hour_format="24"))
    saved = json.loads(checks.state_path.read_text(encoding="utf-8"))
    assert saved["pack_dates"] == {"Night": {"not_before": "2026-12-01"}}
    assert saved["newer"] is True
    checks.state_path.write_text("{not json", encoding="utf-8")
    checks.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))
    [bad] = list(checks.state_path.parent.glob("time_checks.json.bad-*"))
    assert bad.read_text(encoding="utf-8") == "{not json"
    saved = json.loads(checks.state_path.read_text(encoding="utf-8"))
    assert saved["pack_dates"]["Studio"] == {"not_after": "2026-12-26"}
