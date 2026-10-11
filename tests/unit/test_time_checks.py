"""Recorded time, TTS fallback and top-of-hour trigger behavior."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import soundfile as sf

from autodj.config import TimeChecksConfig, load_config
from autodj.doctor import CheckStatus, _time_checks_check
from autodj.time_checks import SettingsBody, TimeChecks, clip_path, missing_minutes, spoken_text


def _checks(tmp_path: Path) -> TimeChecks:
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path
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
    audio = checks.render(local)
    assert audio is not None and audio.shape == (1323, 2)


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
    assert first is not None and first.shape == (100, 2)
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
    assert caplog.text.count("no recorded clip or working TTS command") == 1
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
    monkeypatch.setattr(checks, "render", lambda: audio)
    checks.tick(clock.at, True)
    assert checks.due_audio() is None  # the rule is off
    checks.save(SettingsBody(enabled=True, hour_format="24", rule="after_hour_song"))
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
    checks.save(SettingsBody(enabled=True, hour_format="24", rule="off"))
    assert checks.due_audio() is None


def test_scheduled_check_in_the_hour_replaces_the_hour_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    clock = _Clock(monkeypatch, datetime(2026, 10, 11, 9, 59, tzinfo=UTC))
    checks = _checks(tmp_path)
    checks.save(SettingsBody(enabled=True, hour_format="12", rule="after_hour_song"))
    audio = np.zeros((10, 2), np.float32)
    monkeypatch.setattr(checks, "render", lambda: audio)
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
    monkeypatch.setattr(checks, "render", lambda: None)
    with pytest.raises(ValueError, match="No recorded clip"):
        checks.fire(SimpleNamespace(liner_scheduler=MagicMock(), station=None))


def test_saved_switches_reload_and_a_wrong_version_stops_startup(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    checks.save(SettingsBody(enabled=True, hour_format="24", rule="after_hour_song"))
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
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path
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
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path
    state = Path(cfg.index.active_dir) / "time_checks.json"
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
