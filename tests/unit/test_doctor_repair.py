from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from autodj.config import PlaybackConfig, ServerConfig
from autodj.doctor_repair import plan_saved_state_repair, plan_unknown_key_repairs


def test_unknown_keys_warn_without_rejecting_valid_settings(caplog) -> None:
    cfg = PlaybackConfig.from_dict({"enable_daypart": True, "crossfade_seconds": 2})
    assert cfg.crossfade_seconds == 2
    ServerConfig.from_dict({"liner_upload_max_bytes": 1024})
    assert "enable_daypart" in caplog.text
    assert "liner_upload_max_bytes" in caplog.text


def test_plans_and_applies_simple_unknown_key_removal(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"# keep me\n[server]\nold_option = true # obsolete\nport = 8080\n"
    config.write_bytes(original)
    repairs = plan_unknown_key_repairs(str(config))

    assert len(repairs) == 1
    repair = repairs[0]
    assert repair.path == config
    assert repair.keys_by_section == {"server": ("old_option",)}
    assert repair.original == original
    assert repair.replacement == b"# keep me\n[server]\nport = 8080\n"
    backup = repair.apply()
    assert backup.read_bytes() == original
    assert config.read_bytes() == repair.replacement


def test_plans_unknown_keys_across_base_and_local_overlay(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    local = tmp_path / "config.local.toml"
    config.write_text("[server]\nold_option = true\n", encoding="utf-8")
    local.write_text("[server]\nold_option = false\n", encoding="utf-8")

    repairs = plan_unknown_key_repairs(str(config))
    assert [repair.path for repair in repairs] == [config, local]
    assert all(repair.replacement.replace(b"\r\n", b"\n") == b"[server]\n" for repair in repairs)


def test_explicit_local_path_is_planned_once(tmp_path: Path) -> None:
    local = tmp_path / "config.local.toml"
    local.write_text("[server]\nold_option = true\n", encoding="utf-8")
    repairs = plan_unknown_key_repairs(str(local))
    assert [repair.path for repair in repairs] == [local]


def test_symlinked_config_is_not_replaced(tmp_path: Path) -> None:
    source = tmp_path / "real.toml"
    link = tmp_path / "config.toml"
    source.write_text("[server]\nold_option = true\n", encoding="utf-8")
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
    config.write_text(f"[server]\nold_option = {value}", encoding="utf-8")
    repairs = plan_unknown_key_repairs(str(config))
    assert repairs == []


def test_apply_detects_changed_source_without_writing(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_bytes(b"[server]\nold_option = true\n")
    repair = plan_unknown_key_repairs(str(config))[0]
    config.write_bytes(b"[server]\nold_option = false\n")
    with pytest.raises(RuntimeError, match="changed since repair was planned"):
        repair.apply()
    assert config.read_bytes() == b"[server]\nold_option = false\n"
    assert list(tmp_path.glob("*.bak")) == []


def test_backup_name_collision_is_not_overwritten(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[server]\nold_option = true\n"
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
    original = b"[server]\nold_option = true\n[library]\nunknown_option = 1\n"
    config.write_bytes(original)
    repair = plan_unknown_key_repairs(str(config))[0]
    assert repair.keys_by_section == {"server": ("old_option",), "library": ("unknown_option",)}
    backup = repair.apply()
    assert backup.read_bytes() == original
    assert config.read_bytes() == b"[server]\n[library]\n"
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_server_external_alias_is_known_but_internal_field_is_unknown(tmp_path: Path) -> None:
    from autodj.config import unknown_config_keys

    assert unknown_config_keys({"server": {"liner_upload_max_mib": 20}}) == {
        "server": ("liner_upload_max_mib",)
    }
    assert unknown_config_keys({"server": {"liner_upload_max_bytes": 20}}) == {
        "server": ("liner_upload_max_bytes",)
    }


def test_complex_unknown_assignment_prevents_all_edits_in_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[server]\nold_option = true\nlegacy_list = [\n  1,\n  2,\n]\n"
    config.write_bytes(original)
    assert plan_unknown_key_repairs(str(config)) == []
    assert config.read_bytes() == original


def test_repairs_preserve_unknown_sections_and_presets(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = (
        b"[server]\nold_option = true\n"
        b"[legacy_section]\nkeep = 'yes'\n"
        b"[presets.workout]\nbpm_min = 120\n"
    )
    config.write_bytes(original)
    repair = plan_unknown_key_repairs(str(config))[0]
    assert repair.replacement == (
        b"[server]\n[legacy_section]\nkeep = 'yes'\n[presets.workout]\nbpm_min = 120\n"
    )


def test_saved_state_repair_removes_only_obsolete_keys_and_keeps_backup(tmp_path: Path) -> None:
    import json

    path = tmp_path / "web_state.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "eq": {"low": 1.4},
            "playback": {
                "no_repeat_minutes": 40,
                "repeat_rules": {"related_artists_file": "", "track": {"minutes": 460}},
                "pick_mode": "current_all_counts_0",
            },
        },
        indent=2,
    ).encode()
    path.write_bytes(original)
    repair = plan_saved_state_repair(tmp_path)
    assert repair is not None
    assert repair.keys_by_section == {
        "playback": ("no_repeat_minutes",),
        "playback.repeat_rules": ("related_artists_file",),
    }
    backup = repair.apply()
    assert backup.read_bytes() == original
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["eq"] == {"low": 1.4}
    assert data["playback"]["repeat_rules"] == {"track": {"minutes": 460}}
    assert data["playback"]["pick_mode"] == "current_all_counts_0"


def test_saved_state_repair_leaves_custom_related_artist_path_and_malformed_files(
    tmp_path: Path,
) -> None:
    import json

    path = tmp_path / "web_state.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "playback": {
                "related_artists_file": "custom.toml",
                "no_repeat_window": 40,
            },
        }
    ).encode()
    path.write_bytes(original)
    assert plan_saved_state_repair(tmp_path) is None
    assert path.read_bytes() == original
    path.write_text("{bad", encoding="utf-8")
    assert plan_saved_state_repair(tmp_path) is None


def test_saved_state_repair_preserves_nested_custom_path_when_top_level_path_is_empty(
    tmp_path: Path,
) -> None:
    import json

    path = tmp_path / "web_state.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "playback": {
                "related_artists_file": "",
                "repeat_rules": {"related_artists_file": "custom.toml"},
            },
        }
    ).encode()
    path.write_bytes(original)

    assert plan_saved_state_repair(tmp_path) is None
    assert path.read_bytes() == original


def test_saved_state_repair_does_not_modify_unsupported_schema(tmp_path: Path) -> None:
    path = tmp_path / "web_state.json"
    original = b'{"schema_version":99,"playback":{"no_repeat_minutes":40}}'
    path.write_bytes(original)

    assert plan_saved_state_repair(tmp_path) is None
    assert path.read_bytes() == original
