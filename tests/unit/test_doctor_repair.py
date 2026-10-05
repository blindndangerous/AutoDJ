from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from autodj.config import PlaybackConfig, ServerConfig, UnknownConfigKeysError
from autodj.doctor_repair import plan_unknown_key_repairs


def test_unknown_keys_raise_structured_error() -> None:
    with pytest.raises(UnknownConfigKeysError) as caught:
        PlaybackConfig.from_dict({"enable_daypart": True})
    assert caught.value.section == "playback"
    assert caught.value.keys == ("enable_daypart",)
    assert "unknown [playback] keys" in str(caught.value)
    with pytest.raises(UnknownConfigKeysError) as server_error:
        ServerConfig.from_dict({"liner_upload_max_bytes": 1024})
    assert server_error.value.keys == ("liner_upload_max_bytes",)


def test_plans_and_applies_simple_unknown_key_removal(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"# keep me\n[playback]\nenable_daypart = true # obsolete\ncrossfade_seconds = 4\n"
    config.write_bytes(original)
    repairs = plan_unknown_key_repairs(str(config))

    assert len(repairs) == 1
    repair = repairs[0]
    assert repair.path == config
    assert repair.keys_by_section == {"playback": ("enable_daypart",)}
    assert repair.original == original
    assert repair.replacement == b"# keep me\n[playback]\ncrossfade_seconds = 4\n"
    backup = repair.apply()
    assert backup.read_bytes() == original
    assert config.read_bytes() == repair.replacement


def test_plans_unknown_keys_across_base_and_local_overlay(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    local = tmp_path / "config.local.toml"
    config.write_text("[playback]\nenable_daypart = true\n", encoding="utf-8")
    local.write_text("[playback]\nenable_daypart = false\n", encoding="utf-8")

    repairs = plan_unknown_key_repairs(str(config))
    assert [repair.path for repair in repairs] == [config, local]
    assert all(repair.replacement.replace(b"\r\n", b"\n") == b"[playback]\n" for repair in repairs)


def test_explicit_local_path_is_planned_once(tmp_path: Path) -> None:
    local = tmp_path / "config.local.toml"
    local.write_text("[playback]\nenable_daypart = true\n", encoding="utf-8")
    repairs = plan_unknown_key_repairs(str(local))
    assert [repair.path for repair in repairs] == [local]


def test_symlinked_config_is_not_replaced(tmp_path: Path) -> None:
    source = tmp_path / "real.toml"
    link = tmp_path / "config.toml"
    source.write_text("[playback]\nenable_daypart = true\n", encoding="utf-8")
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    repairs = plan_unknown_key_repairs(str(link))
    assert repairs == []
    assert source.exists()


@pytest.mark.parametrize("value", ["[\n  true,\n]\n", "'''\nvalue\n'''\n"])
def test_complex_multiline_assignment_is_left_for_manual_repair(tmp_path: Path, value: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"[playback]\nenable_daypart = {value}", encoding="utf-8")
    repairs = plan_unknown_key_repairs(str(config))
    assert repairs == []


def test_apply_detects_changed_source_without_writing(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_bytes(b"[playback]\nenable_daypart = true\n")
    repair = plan_unknown_key_repairs(str(config))[0]
    config.write_bytes(b"[playback]\nenable_daypart = false\n")
    with pytest.raises(RuntimeError, match="changed since repair was planned"):
        repair.apply()
    assert config.read_bytes() == b"[playback]\nenable_daypart = false\n"
    assert list(tmp_path.glob("*.bak")) == []


def test_backup_name_collision_is_not_overwritten(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\n"
    config.write_bytes(original)
    repair = plan_unknown_key_repairs(str(config))[0]
    colliding = config.with_name(f"{config.name}.doctor-{'a' * 32}.bak")
    colliding.write_bytes(b"existing")
    actual_uuid4 = uuid.uuid4
    collision = type("UUID", (), {"hex": "a" * 32})()
    with patch(
        "autodj.doctor_repair.uuid.uuid4",
        side_effect=[collision, actual_uuid4(), actual_uuid4(), actual_uuid4()],
    ):
        backup = repair.apply()
    assert colliding.read_bytes() == b"existing"
    assert backup != colliding
    assert backup.read_bytes() == original


def test_plans_multiple_sections_in_one_repair_and_one_backup(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\n[server]\nold_option = 1\n"
    config.write_bytes(original)
    repair = plan_unknown_key_repairs(str(config))[0]
    assert repair.keys_by_section == {"playback": ("enable_daypart",), "server": ("old_option",)}
    backup = repair.apply()
    assert backup.read_bytes() == original
    assert config.read_bytes() == b"[playback]\n[server]\n"
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_server_external_alias_is_known_but_internal_field_is_unknown(tmp_path: Path) -> None:
    from autodj.config import unknown_config_keys

    assert unknown_config_keys({"server": {"liner_upload_max_mib": 20}}) == {}
    assert unknown_config_keys({"server": {"liner_upload_max_bytes": 20}}) == {
        "server": ("liner_upload_max_bytes",)
    }


def test_complex_unknown_assignment_prevents_all_edits_in_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\nlegacy_list = [\n  1,\n  2,\n]\n"
    config.write_bytes(original)
    assert plan_unknown_key_repairs(str(config)) == []
    assert config.read_bytes() == original


def test_repairs_preserve_unknown_sections_and_presets(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = (
        b"[playback]\nenable_daypart = true\n"
        b"[legacy_section]\nkeep = 'yes'\n"
        b"[presets.workout]\nbpm_min = 120\n"
    )
    config.write_bytes(original)
    repair = plan_unknown_key_repairs(str(config))[0]
    assert repair.replacement == (
        b"[playback]\n[legacy_section]\nkeep = 'yes'\n[presets.workout]\nbpm_min = 120\n"
    )
