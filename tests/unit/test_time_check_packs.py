"""Voice packs found in the time checks folders, and clips chosen from the page (#388)."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from autodj.clip_dates import ClipDates
from autodj.config import TimeChecksConfig, load_config
from autodj.doctor import CheckStatus, _time_checks_check, _voice_pack_checks
from autodj.stations import StationPaths
from autodj.time_check_files import PackConflict, PackNotFound
from autodj.time_checks import SettingsBody, TimeChecks, found_packs
from tests.unit.test_time_checks import _settings, _source, _write_pack


def _checks(tmp_path: Path, *listed: Path) -> TimeChecks:
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path
    cfg.time_checks = TimeChecksConfig(voice_packs=[str(folder) for folder in listed])
    return TimeChecks(cfg, "UTC")


def _zip_pack(path: Path, hours: list[int], minutes: list[int]) -> Path:
    source = _write_pack(path.with_suffix(""), hours, minutes)
    with zipfile.ZipFile(path, "w") as archive:
        for file in source.iterdir():
            archive.write(file, file.name)
    return path


AT = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)


def test_folders_in_the_time_checks_folder_are_packs_with_no_config_line(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    assert checks.packs == {}
    _write_pack(checks.root / "Studio", [3], [5])
    _write_pack(checks.root / "Night", [3], [5])
    (checks.root / ".pack-upload-1").mkdir()
    # Found at the next check, without a restart, and ticked while no choice is saved.
    assert _source(checks, AT) in ("voice pack Night", "voice pack Studio")
    status = checks.status()
    assert [(pack["name"], pack["source"], pack["removable"]) for pack in status["packs"]] == [
        ("Night", "station", True),
        ("Studio", "station", True),
    ]
    assert status["enabled_packs"] == ["Night", "Studio"]
    assert status["folders"] == ["Night", "Studio"]
    # A folder taken away by hand is gone at the next status.
    for file in (checks.root / "Night").iterdir():
        file.unlink()
    (checks.root / "Night").rmdir()
    assert [pack["name"] for pack in checks.status()["packs"]] == ["Studio"]


def test_configured_and_found_packs_merge_and_the_first_source_keeps_a_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path
    shared = tmp_path / "default" / "time_checks"
    own = tmp_path / "default" / "stations" / "night" / "time_checks"
    elsewhere = _write_pack(tmp_path / "voices" / "Studio", [3], [5])
    # The same folder listed in config and found is one pack, from config.
    listed = _write_pack(own / "Listed", [3], [5])
    for folder in (own / "studio", own / "Own", shared / "Own", shared / "Shared"):
        _write_pack(folder, [3], [5])
    cfg.time_checks = TimeChecksConfig(voice_packs=[str(elsewhere), str(listed)])
    paths = StationPaths.for_station(cfg, "night")
    found = found_packs(cfg, paths)
    assert [(name, pack.source) for name, pack in found.packs.items()] == [
        ("Studio", "config"),
        ("Listed", "config"),
        ("Own", "station"),
        ("Shared", "shared"),
    ]
    assert found.packs["Own"].path == own / "Own"
    assert not found.packs["Shared"].removable
    assert len(found.clashes) == 2
    assert any(str(own / "studio") in clash for clash in found.clashes)
    assert any(str(shared / "Own") in clash for clash in found.clashes)
    checks = TimeChecks(cfg, "UTC", paths)
    checks.status()
    checks.status()
    # Each folder left out is logged once, not at every status.
    assert caplog.text.count("is not used: the voice pack name") == 2
    with pytest.raises(PackConflict, match="shared time checks folder"):
        checks.delete_pack("Shared")
    doctor = _time_checks_check(cfg, paths)
    assert doctor.status is CheckStatus.PASS  # time checks are off
    cfg.time_checks.enabled = True
    assert "is not used: the voice pack name" in _time_checks_check(cfg, paths).summary
    assert [check.name for check in _voice_pack_checks(cfg, paths)] == [
        "voice-pack-Studio",
        "voice-pack-Listed",
        "voice-pack-Own",
        "voice-pack-Shared",
    ]


def test_a_pack_found_after_the_choice_is_saved_starts_unticked(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path / "voices" / "Studio", [3], [5])
    checks = _checks(tmp_path, pack)
    checks.save(_settings(enabled_packs=["Studio"]))
    _write_pack(checks.root / "Night", [3], [5])
    status = checks.status()
    assert [(pack["name"], pack["enabled"]) for pack in status["packs"]] == [
        ("Studio", True),
        ("Night", False),
    ]
    # A restart reads the saved choice the same way.
    again = TimeChecks(checks.cfg, "UTC")
    assert again.settings.enabled_packs == ["Studio"]


def test_a_pack_added_from_a_zip_counts_at_once_and_a_removed_one_is_forgotten(
    tmp_path: Path,
) -> None:
    pack = _write_pack(tmp_path / "voices" / "Listed", [3], [5])
    checks = _checks(tmp_path, pack)
    assert checks.add_pack("Studio", _zip_pack(tmp_path / "Studio.zip", [3], [5]), replace=False)
    assert checks.packs["Studio"].removable
    assert _source(checks, AT, "Studio") == "voice pack Studio"
    checks.save(_settings(enabled_packs=["Listed", "Studio"]))
    checks.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))
    with pytest.raises(PackConflict, match="already exists"):
        checks.add_pack("studio", _zip_pack(tmp_path / "again.zip", [4], [6]), replace=False)
    # A replaced pack keeps its tick and dates, and its folder's spelling.
    assert checks.add_pack("STUDIO", tmp_path / "again.zip", replace=True) == "Studio"
    assert checks.settings.enabled_packs == ["Listed", "Studio"]
    assert "Studio" in checks.pack_dates
    assert _source(checks, AT, "Studio") is None  # the new files say 04:06 only
    with pytest.raises(PackConflict, match=r"listed in config\.toml"):
        checks.add_pack("Listed", tmp_path / "again.zip", replace=True)
    with pytest.raises(PackConflict, match=r"listed in config\.toml"):
        checks.delete_pack("Listed")
    checks.delete_pack("Studio")
    assert "Studio" not in checks.packs and not (checks.root / "Studio").exists()
    assert checks.settings.enabled_packs == ["Listed"]
    saved = json.loads(checks.state_path.read_text(encoding="utf-8"))
    assert saved["settings"]["enabled_packs"] == ["Listed"]
    assert "pack_dates" not in saved
    with pytest.raises(PackNotFound):
        checks.delete_pack("Studio")
    # Added again, it is a new pack: unticked, as the choice is saved.
    checks.add_pack("Studio", tmp_path / "Studio.zip", replace=False)
    assert [(item["name"], item["enabled"]) for item in checks.status()["packs"]] == [
        ("Listed", True),
        ("Studio", False),
    ]


def test_a_pack_cannot_take_the_recorded_clips_folders_name(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    (checks.root / "Clips").mkdir(parents=True)
    checks.save(_settings(recorded_folder="Clips"))
    with pytest.raises(PackConflict, match="recorded minute clips folder"):
        checks.add_pack("clips", _zip_pack(tmp_path / "p.zip", [3], [5]), replace=True)
    with pytest.raises(PackNotFound):
        checks.delete_pack("Clips")
    assert (checks.root / "Clips").is_dir()


def test_recorded_clips_intro_and_outro_are_chosen_by_name_and_kept(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    clips = checks.root / "Clips"
    clips.mkdir(parents=True)
    for folder, filename in (
        (clips, "03_05.wav"),
        (checks.root, "hello.wav"),
        (checks.root, "bye.wav"),
    ):
        sf.write(folder / filename, np.zeros((441, 2)), 44100)
    assert checks.status()["folders"] == ["Clips"]
    assert [pack["name"] for pack in checks.status()["packs"]] == ["Clips"]
    assert _source(checks, AT) is None
    chosen = _settings(recorded_folder="Clips", intro="hello.wav", outro="bye.wav")
    assert checks.check_choices(chosen) is None
    checks.save(chosen)
    status = checks.status()
    # The clips folder is not a voice pack.
    assert status["packs"] == [] and status["missing_minutes"] == 1439
    assert status["clip_files"] == ["bye.wav", "hello.wav"]
    rendered = checks.render(AT)
    assert rendered is not None and rendered.source == "recorded clip"
    assert rendered.audio.shape == (1323, 2)
    # Kept across a restart.
    again = TimeChecks(checks.cfg, "UTC")
    assert (again.settings.recorded_folder, again.settings.intro, again.settings.outro) == (
        "Clips",
        "hello.wav",
        "bye.wav",
    )
    assert again.folder == clips
    # No outro: "" turns off one from config.toml too.
    checks.cfg.time_checks.outro_clip = str(checks.root / "bye.wav")
    checks.save(_settings(recorded_folder="Clips", intro="hello.wav", outro=""))
    rendered = checks.render(AT)
    assert rendered is not None and rendered.audio.shape == (882, 2)
    assert checks.check_choices(_settings(recorded_folder="Gone")) == (
        "No folder named Gone in the time checks folder"
    )
    assert checks.check_choices(_settings(intro="03_05.wav")) == (
        "No intro clip named 03_05.wav in the time checks folder"
    )


def test_the_doctor_reads_the_chosen_clips(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    (checks.root / "Clips").mkdir(parents=True)
    checks.save(_settings(enabled=True, recorded_folder="Clips", intro="gone.wav"))
    check = _time_checks_check(checks.cfg)
    assert check.detail["folder"] == str(checks.root / "Clips")
    assert "intro clip gone.wav not found" in check.summary


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recorded_folder", "../elsewhere"),
        ("recorded_folder", ".hidden"),
        ("recorded_folder", " padded"),
        ("intro", "C:/intro.wav"),
        ("intro", "../intro.wav"),
        ("outro", "notes.txt"),
    ],
)
def test_clip_choices_are_names_never_paths(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        _settings(**{field: value})


def test_older_saved_settings_without_clip_choices_still_load(tmp_path: Path) -> None:
    checks = _checks(tmp_path)
    checks.state_path.parent.mkdir(parents=True, exist_ok=True)
    checks.state_path.write_text(
        '{"version":1,"settings":{"enabled":true,"hour_format":"24","rule":"off",'
        '"enabled_packs":[],"pack_order":"rotate"}}',
        encoding="utf-8",
    )
    again = TimeChecks(checks.cfg, "UTC")
    assert again.settings == SettingsBody.model_validate(
        {
            "enabled": True,
            "hour_format": "24",
            "rule": "off",
            "enabled_packs": [],
            "pack_order": "rotate",
            "recorded_folder": "",
            "intro": None,
            "outro": None,
        }
    )


def test_an_intro_never_chosen_comes_from_config_and_status_shows_its_name_only(
    tmp_path: Path,
) -> None:
    checks = _checks(tmp_path)
    secret = tmp_path / "secret-folder"
    secret.mkdir()
    sf.write(secret / "intro.wav", np.zeros((441, 2)), 44100)
    checks.cfg.time_checks.intro_clip = str(secret / "intro.wav")
    status = checks.status()
    assert status["intro"] is None and status["intro_config"] == "intro.wav"
    assert status["outro_config"] is None
    assert "secret-folder" not in json.dumps(status)


def test_a_failed_remove_or_refused_zip_keeps_the_tick_and_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import time_checks
    from autodj.time_check_files import PackError

    checks = _checks(tmp_path)
    checks.add_pack("Studio", _zip_pack(tmp_path / "Studio.zip", [3], [5]), replace=False)
    checks.save(_settings(enabled_packs=["Studio"]))
    checks.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))

    def locked(_root: Path, _name: str) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(time_checks, "remove_pack", locked)
    with pytest.raises(PermissionError):
        checks.delete_pack("Studio")
    assert checks.settings.enabled_packs == ["Studio"] and "Studio" in checks.pack_dates
    monkeypatch.undo()
    # A refused zip for a name with a leftover tick and dates leaves them too.
    checks.delete_pack("Studio")
    checks.save(_settings(enabled_packs=["Studio"]))
    checks.save_pack_dates("Studio", ClipDates(not_after=date(2026, 12, 26)))
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("notes.txt", b"x")
    with pytest.raises(PackError):
        checks.add_pack("Studio", bad, replace=False)
    assert checks.settings.enabled_packs == ["Studio"] and "Studio" in checks.pack_dates


def test_a_minute_clip_upload_goes_to_the_recorded_clips_folder(tmp_path: Path) -> None:
    import io

    checks = _checks(tmp_path)
    (checks.root / "Clips").mkdir(parents=True)
    checks.save(_settings(recorded_folder="Clips"))
    for name in ("15_05.wav", "intro.wav"):
        buffer = io.BytesIO()
        sf.write(buffer, np.zeros((441, 2)), 44100, format="WAV")
        upload = checks.root / f".clip-upload-{name}"
        upload.write_bytes(buffer.getvalue())
        checks.add_clip(name, upload, replace=False)
    assert (checks.root / "Clips" / "15_05.wav").is_file()
    assert (checks.root / "intro.wav").is_file()
    assert checks.status()["missing_minutes"] == 1439
