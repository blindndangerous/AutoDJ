"""Tests for autodj.runtime_state — settings persistence across restarts."""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autodj.runtime_state import (
    _is_finite_number,
    load_into_player,
    save_from_player,
    state_file_for,
)


def _make_player() -> SimpleNamespace:
    playback = SimpleNamespace(
        crossfade_seconds=3.0,
        fade_in_seconds=3.0,
        crossfade_eq_duck=False,
        transition_mode="full_intro_outro",
        post_queue_seed="last_queued",
        key_notation="camelot",
        key_prefer_flats=False,
        show_lyrics=True,
        enable_daypart=False,
        enable_mood_arc=False,
        mood_arc_hours=3.0,
        import_external_cues=True,
        beat_sync_fx=True,
        key_sync_fx=True,
        beatmatch_on_skip=False,
        prefetch_next_track=True,
        silence_trigger_crossfade=True,
        liners_enabled=False,
        liners_every_n_songs=None,
        liners_every_minutes=None,
        liners_random_min_minutes=None,
        liners_random_max_minutes=None,
        liners_pick_mode="random",
        liners_duck_db=-12.0,
    )
    cfg = SimpleNamespace(
        transitions=SimpleNamespace(effect="none"),
        djmix=SimpleNamespace(
            harmonic_mixing=False,
            harmonic_mode="compatible",
            beatmatch=False,
            phrase_align=False,
            outro_intro_align=False,
            filter_sweep=False,
        ),
        playback=playback,
        replaygain=SimpleNamespace(enabled=False),
        presets={},
    )
    return SimpleNamespace(
        _cfg=cfg,
        _smart_shuffle=False,
        _pure_shuffle=False,
        _anchor_to_seed=False,
        _bpm_range=None,
        _preset=None,
        _discovery_every=None,
        _mood_arc=None,
        _state=SimpleNamespace(no_repeat_window=20),
        _sim=SimpleNamespace(entries_snapshot=lambda: (), ntotal=0),
    )


def _write_state(index_dir: Path, payload: object) -> None:
    (index_dir / "web_state.json").write_text(json.dumps(payload), encoding="utf-8")


def test_huge_integer_is_not_a_finite_runtime_number() -> None:
    assert _is_finite_number(10**10000) is False


@pytest.mark.parametrize(
    "payload",
    [
        {"preset": 12},
        {"transition": 12},
        {"playback": {"transition_mode": 12}},
        {"playback": {"liners_pick_mode": "invalid"}},
        {"playback": {"liners_duck_db": 1.0}},
        {"bpm_range": "invalid"},
        {"discovery_every": "invalid"},
        {"schema_version": "invalid"},
    ],
)
def test_invalid_state_field_is_warned_and_ignored(tmp_path: Path, caplog, payload) -> None:
    player = _make_player()
    _write_state(tmp_path, payload)

    load_into_player(player, tmp_path)

    assert "ignoring invalid" in caplog.text


def test_non_object_state_root_is_warned_and_ignored(tmp_path: Path, caplog) -> None:
    _write_state(tmp_path, ["not", "an", "object"])

    load_into_player(_make_player(), tmp_path)

    assert "root is not an object" in caplog.text


def test_partial_random_window_rejects_invalid_current_other_bound(tmp_path: Path, caplog) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_max_minutes = "invalid"
    _write_state(tmp_path, {"playback": {"liners_random_min_minutes": 2.0}})

    load_into_player(player, tmp_path)

    assert "invalid current liners_random_max_minutes" in caplog.text


def test_partial_random_window_restores_only_maximum(tmp_path: Path) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 1.0
    _write_state(tmp_path, {"playback": {"liners_random_max_minutes": 5.0}})

    load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_random_min_minutes == 1.0
    assert player._cfg.playback.liners_random_max_minutes == 5.0


def test_disabling_mood_arc_clears_live_arc(tmp_path: Path) -> None:
    player = _make_player()
    player._mood_arc = object()
    _write_state(tmp_path, {"playback": {"enable_mood_arc": False}})

    load_into_player(player, tmp_path)

    assert player._mood_arc is None


def test_null_discovery_clears_existing_cadence(tmp_path: Path) -> None:
    player = _make_player()
    player._discovery_every = 4
    _write_state(tmp_path, {"discovery_every": None})

    load_into_player(player, tmp_path)

    assert player._discovery_every is None


def test_temporary_state_cleanup_failure_is_warned_after_publish(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    def fail_unlink(self, *, missing_ok=False):
        raise OSError("cleanup failed")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    save_from_player({"preset": "chill"}, tmp_path)

    assert (
        json.loads((tmp_path / "web_state.json").read_text(encoding="utf-8"))["preset"] == "chill"
    )
    assert "Failed to clean temporary web_state.json" in caplog.text


class TestStateFile:
    def test_returns_path_when_index_dir_set(self, tmp_path) -> None:
        path = state_file_for(tmp_path)
        assert path == tmp_path / "web_state.json"

    def test_returns_none_for_none_input(self) -> None:
        assert state_file_for(None) is None


class TestLoadInto:
    def test_no_file_is_no_op(self, tmp_path) -> None:
        p = _make_player()
        load_into_player(p, tmp_path)  # no state file present
        assert p._cfg.transitions.effect == "none"

    def test_unreadable_file_is_no_op(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text("not json {{{", encoding="utf-8")
        p = _make_player()
        load_into_player(p, tmp_path)
        assert p._cfg.transitions.effect == "none"

    def test_invalid_utf8_is_warned_and_ignored(self, tmp_path, caplog) -> None:
        (tmp_path / "web_state.json").write_bytes(b"\xff\xfe\xfa")
        p = _make_player()

        with caplog.at_level("WARNING"):
            load_into_player(p, tmp_path)

        assert p._cfg.transitions.effect == "none"
        assert len([record for record in caplog.records if "unreadable" in record.message]) == 1

    def test_loads_transition(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps({"transition": "echo_out"}),
            encoding="utf-8",
        )
        p = _make_player()
        load_into_player(p, tmp_path)
        assert p._cfg.transitions.effect == "echo_out"

    def test_loads_djmix_toggles(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps({"djmix": {"harmonic_mixing": True, "beatmatch": True}}),
            encoding="utf-8",
        )
        p = _make_player()
        load_into_player(p, tmp_path)
        assert p._cfg.djmix.harmonic_mixing is True
        assert p._cfg.djmix.beatmatch is True
        assert p._cfg.djmix.phrase_align is False  # untouched

    def test_loads_playback_settings(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps(
                {
                    "playback": {
                        "crossfade_seconds": 4.5,
                        "crossfade_eq_duck": True,
                        "smart_shuffle": True,
                        "replaygain_enabled": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        p = _make_player()
        load_into_player(p, tmp_path)
        assert p._cfg.playback.crossfade_seconds == 4.5
        assert p._cfg.playback.crossfade_eq_duck is True
        assert p._smart_shuffle is True
        assert p._cfg.replaygain.enabled is True

    def test_loads_daypart_arc_import_cues(self, tmp_path) -> None:
        """Regression: 0.14.0 added enable_daypart / enable_mood_arc /
        mood_arc_hours / import_external_cues to PlaybackConfig.  Without
        their entries in load_into_player, web-UI toggles silently
        revert on serve restart even though save_from_player writes them.
        """
        (tmp_path / "web_state.json").write_text(
            json.dumps(
                {
                    "playback": {
                        "enable_daypart": True,
                        "enable_mood_arc": True,
                        "mood_arc_hours": 2.5,
                        "import_external_cues": False,
                        "pure_shuffle": True,
                        "anchor_to_seed": True,
                        "show_lyrics": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        p = _make_player()
        # Pre-existing fields that the loader now also honours.
        p._pure_shuffle = False
        p._anchor_to_seed = False
        p._cfg.playback.show_lyrics = True
        p._cfg.playback.enable_daypart = False
        p._cfg.playback.enable_mood_arc = False
        p._cfg.playback.mood_arc_hours = 3.0
        p._cfg.playback.import_external_cues = True
        load_into_player(p, tmp_path)
        assert p._cfg.playback.enable_daypart is True
        assert p._cfg.playback.enable_mood_arc is True
        assert p._cfg.playback.mood_arc_hours == 2.5
        assert p._cfg.playback.import_external_cues is False
        assert p._pure_shuffle is True
        assert p._anchor_to_seed is True
        assert p._cfg.playback.show_lyrics is False
        # Mood arc was anchored to "now" by the loader so the user
        # always begins with warmup -- not mid-arc.
        assert p._mood_arc is not None

    def test_loads_bpm_range(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps({"bpm_range": {"lo": 90, "hi": 140}}),
            encoding="utf-8",
        )
        p = _make_player()
        load_into_player(p, tmp_path)
        assert p._bpm_range == (90.0, 140.0)

    def test_clears_bpm_range_on_null(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps({"bpm_range": {"lo": None, "hi": None}}),
            encoding="utf-8",
        )
        p = _make_player()
        p._bpm_range = (90.0, 140.0)  # pre-set
        load_into_player(p, tmp_path)
        assert p._bpm_range is None

    def test_loads_discovery(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps({"discovery_every": 25}),
            encoding="utf-8",
        )
        p = _make_player()
        load_into_player(p, tmp_path)
        assert p._discovery_every == 25

    def test_clears_discovery_on_zero(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text(
            json.dumps({"discovery_every": 0}),
            encoding="utf-8",
        )
        p = _make_player()
        p._discovery_every = 20
        load_into_player(p, tmp_path)
        assert p._discovery_every is None


class TestSaveFrom:
    def test_writes_file(self, tmp_path) -> None:
        settings = {
            "preset": "wakeup",
            "available_presets": ["wakeup", "chill"],  # should be stripped
            "transition": "rotate",
            "djmix": {"beatmatch": True},
        }
        save_from_player(settings, tmp_path)
        path = tmp_path / "web_state.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["preset"] == "wakeup"
        assert data["transition"] == "rotate"
        assert data["djmix"]["beatmatch"] is True
        assert "available_presets" not in data  # stripped

    def test_atomic_write_via_tmp_rename(self, tmp_path) -> None:
        save_from_player({"preset": "chill"}, tmp_path)
        # Tmp file should not linger after successful rename
        assert not (tmp_path / "web_state.json.tmp").exists()
        assert (tmp_path / "web_state.json").exists()

    def test_fsyncs_file_before_replace_and_parent_after(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        import autodj.runtime_state as runtime_state

        events: list[str] = []
        real_replace = runtime_state.os.replace

        def record_file_fsync(_fd: int) -> None:
            events.append("file_fsync")

        def record_replace(source, destination) -> None:
            events.append("replace")
            real_replace(source, destination)

        def record_directory_fsync(path) -> None:
            assert path == tmp_path
            events.append("directory_fsync")

        monkeypatch.setattr(runtime_state.os, "fsync", record_file_fsync)
        monkeypatch.setattr(runtime_state.os, "replace", record_replace)
        monkeypatch.setattr(
            runtime_state,
            "_fsync_directory",
            record_directory_fsync,
            raising=False,
        )

        save_from_player({"preset": "chill"}, tmp_path)

        assert events == ["file_fsync", "replace", "directory_fsync"]
        assert (
            json.loads((tmp_path / "web_state.json").read_text(encoding="utf-8"))["preset"]
            == "chill"
        )

    def test_file_fsync_failure_preserves_old_state_and_cleans_temp(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ) -> None:
        import autodj.runtime_state as runtime_state

        path = tmp_path / "web_state.json"
        path.write_text('{"preset": "old"}', encoding="utf-8")

        def fail_fsync(_fd: int) -> None:
            raise OSError("storage flush failed")

        monkeypatch.setattr(runtime_state.os, "fsync", fail_fsync)

        with caplog.at_level("WARNING"):
            save_from_player({"preset": "new"}, tmp_path)

        assert json.loads(path.read_text(encoding="utf-8"))["preset"] == "old"
        assert not (tmp_path / "web_state.json.tmp").exists()
        assert len([record for record in caplog.records if "Failed to save" in record.message]) == 1

    def test_base_exception_during_file_fsync_cleans_temp_and_propagates(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        import autodj.runtime_state as runtime_state

        path = tmp_path / "web_state.json"
        path.write_text('{"preset": "old"}', encoding="utf-8")

        def interrupt_fsync(_fd: int) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(runtime_state.os, "fsync", interrupt_fsync)

        with pytest.raises(KeyboardInterrupt):
            save_from_player({"preset": "new"}, tmp_path)

        assert json.loads(path.read_text(encoding="utf-8"))["preset"] == "old"
        assert not (tmp_path / "web_state.json.tmp").exists()

    def test_directory_fsync_failure_keeps_published_state(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ) -> None:
        import autodj.runtime_state as runtime_state

        path = tmp_path / "web_state.json"
        path.write_text('{"preset": "old"}', encoding="utf-8")

        def fail_directory_fsync(_path) -> None:
            raise OSError("directory flush failed")

        monkeypatch.setattr(
            runtime_state,
            "_fsync_directory",
            fail_directory_fsync,
            raising=False,
        )

        with caplog.at_level("WARNING"):
            save_from_player({"preset": "new"}, tmp_path)

        assert json.loads(path.read_text(encoding="utf-8"))["preset"] == "new"
        assert not (tmp_path / "web_state.json.tmp").exists()
        assert len([record for record in caplog.records if "durability" in record.message]) == 1

    def test_directory_fsync_is_no_op_when_directory_open_is_unavailable(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        import autodj.runtime_state as runtime_state

        def unexpected_open(_path, _flags) -> int:
            pytest.fail("os.open must not run without O_DIRECTORY support")

        monkeypatch.delattr(runtime_state.os, "O_DIRECTORY", raising=False)
        monkeypatch.setattr(runtime_state.os, "open", unexpected_open)

        runtime_state._fsync_directory(tmp_path)

    @pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
    def test_directory_fsync_propagates_supported_open_failure(
        self,
        tmp_path,
        monkeypatch,
        error_number,
    ) -> None:
        import autodj.runtime_state as runtime_state

        def fail_open(_path, _flags) -> int:
            raise OSError(error_number, "directory open failed")

        monkeypatch.setattr(runtime_state.os, "O_DIRECTORY", 0x10000, raising=False)
        monkeypatch.setattr(runtime_state.os, "open", fail_open)

        with pytest.raises(OSError) as exc_info:
            runtime_state._fsync_directory(tmp_path)

        assert exc_info.value.errno == error_number

    def test_directory_fsync_closes_descriptor_when_fsync_fails(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        import autodj.runtime_state as runtime_state

        events: list[tuple[str, int]] = []

        def record_open(_path, _flags) -> int:
            events.append(("open", 41))
            return 41

        def fail_fsync(descriptor: int) -> None:
            events.append(("fsync", descriptor))
            raise OSError(errno.EIO, "directory flush failed")

        def record_close(descriptor: int) -> None:
            events.append(("close", descriptor))

        monkeypatch.setattr(runtime_state.os, "O_DIRECTORY", 0x10000, raising=False)
        monkeypatch.setattr(runtime_state.os, "open", record_open)
        monkeypatch.setattr(runtime_state.os, "fsync", fail_fsync)
        monkeypatch.setattr(runtime_state.os, "close", record_close)

        with pytest.raises(OSError, match="directory flush failed"):
            runtime_state._fsync_directory(tmp_path)

        assert events == [("open", 41), ("fsync", 41), ("close", 41)]

    def test_no_index_dir_is_no_op(self) -> None:
        # Should not raise
        save_from_player({"preset": "chill"}, None)

    def test_save_oserror_logged_not_raised(self, tmp_path, monkeypatch) -> None:
        """When os.replace raises OSError, save logs and returns silently."""
        import os as _os

        def _bad(_a, _b):
            raise OSError("disk full")

        monkeypatch.setattr(_os, "replace", _bad)
        # No exception bubbles
        save_from_player({"preset": "chill"}, tmp_path)

    def test_load_unknown_preset_swallowed(self, tmp_path) -> None:
        """Unknown preset name in saved state → silently skipped."""
        from autodj.runtime_state import load_into_player

        (tmp_path / "web_state.json").write_text(
            '{"preset": "nosuchpreset_xyz"}',
            encoding="utf-8",
        )
        p = _make_player()
        load_into_player(p, tmp_path)  # no exception
        assert p._preset is None


class TestRoundTrip:
    def test_save_bridge_snapshot_then_load_preserves_every_persisted_field(
        self,
        tmp_path,
    ) -> None:
        from autodj._bridge import PlayerBridge

        p1 = _make_player()
        p1._cfg.djmix.harmonic_mixing = True
        p1._cfg.djmix.harmonic_mode = "strict"
        p1._cfg.djmix.beatmatch = True
        p1._cfg.djmix.phrase_align = True
        p1._cfg.djmix.outro_intro_align = True
        p1._cfg.djmix.filter_sweep = True
        p1._cfg.transitions.effect = "echo_out"
        p1._cfg.playback.crossfade_seconds = 6.0
        p1._cfg.playback.fade_in_seconds = 1.5
        p1._cfg.playback.crossfade_eq_duck = True
        p1._smart_shuffle = True
        p1._pure_shuffle = True
        p1._anchor_to_seed = True
        p1._cfg.replaygain.enabled = True
        p1._cfg.playback.transition_mode = "fixed"
        p1._cfg.playback.post_queue_seed = "pre_queue"
        p1._cfg.playback.key_notation = "musical"
        p1._cfg.playback.key_prefer_flats = True
        p1._cfg.playback.show_lyrics = False
        p1._cfg.playback.enable_daypart = True
        p1._cfg.playback.enable_mood_arc = True
        p1._cfg.playback.mood_arc_hours = 2.5
        p1._cfg.playback.import_external_cues = False
        p1._cfg.playback.beat_sync_fx = False
        p1._cfg.playback.key_sync_fx = False
        p1._cfg.playback.beatmatch_on_skip = True
        p1._cfg.playback.prefetch_next_track = False
        p1._cfg.playback.silence_trigger_crossfade = False
        p1._cfg.playback.liners_enabled = True
        p1._cfg.playback.liners_folder = "Z:/Station/Private/Liners"
        p1._cfg.playback.liners_every_n_songs = 3
        p1._cfg.playback.liners_every_minutes = None
        p1._cfg.playback.liners_random_min_minutes = 8.0
        p1._cfg.playback.liners_random_max_minutes = 14.0
        p1._cfg.playback.liners_pick_mode = "sequential"
        p1._cfg.playback.liners_duck_db = -9.0
        p1._bpm_range = (100.0, 132.0)
        p1._discovery_every = 9

        bridge1 = PlayerBridge(p1, p1._sim)
        saved = bridge1.get_settings()
        assert set(saved) == {
            "preset",
            "available_presets",
            "transition",
            "djmix",
            "playback",
            "bpm_range",
            "discovery_every",
        }
        assert set(saved["djmix"]) == {
            "harmonic_mixing",
            "harmonic_mode",
            "beatmatch",
            "phrase_align",
            "outro_intro_align",
            "filter_sweep",
        }
        assert set(saved["playback"]) == {
            "crossfade_seconds",
            "fade_in_seconds",
            "crossfade_eq_duck",
            "smart_shuffle",
            "pure_shuffle",
            "anchor_to_seed",
            "replaygain_enabled",
            "transition_mode",
            "post_queue_seed",
            "key_notation",
            "key_prefer_flats",
            "show_lyrics",
            "enable_daypart",
            "enable_mood_arc",
            "mood_arc_hours",
            "import_external_cues",
            "beat_sync_fx",
            "key_sync_fx",
            "beatmatch_on_skip",
            "prefetch_next_track",
            "silence_trigger_crossfade",
            "liners_enabled",
            "liners_every_n_songs",
            "liners_every_minutes",
            "liners_random_min_minutes",
            "liners_random_max_minutes",
            "liners_pick_mode",
            "liners_duck_db",
            # Bridge-visible derived session values are never persisted.
            "no_repeat_window",
            "library_size",
        }
        # Config-only absolute path is neither browser-visible nor persisted.
        assert "liners_folder" not in saved["playback"]
        saved["playback"]["liners_folder"] = p1._cfg.playback.liners_folder
        save_from_player(saved, tmp_path)
        stored = json.loads((tmp_path / "web_state.json").read_text(encoding="utf-8"))
        assert stored["schema_version"] == 1
        assert "available_presets" not in stored
        assert "no_repeat_window" not in stored["playback"]
        assert "library_size" not in stored["playback"]
        assert "liners_folder" not in stored["playback"]

        p2 = _make_player()
        load_into_player(p2, tmp_path)
        restored = PlayerBridge(p2, p2._sim).get_settings()
        expected_playback = {
            key: value
            for key, value in saved["playback"].items()
            if key not in {"no_repeat_window", "library_size", "liners_folder"}
        }
        assert restored["transition"] == saved["transition"]
        assert restored["djmix"] == saved["djmix"]
        assert {key: restored["playback"][key] for key in expected_playback} == expected_playback
        assert restored["bpm_range"] == saved["bpm_range"]
        assert restored["discovery_every"] == saved["discovery_every"]


def _write_state(tmp_path, data: dict) -> None:
    (tmp_path / "web_state.json").write_text(json.dumps(data), encoding="utf-8")


def test_string_false_is_rejected_instead_of_coerced(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "playback": {"prefetch_next_track": "false"}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.prefetch_next_track is True
    assert [record for record in caplog.records if "prefetch_next_track" in record.message]


def test_invalid_harmonic_mode_warns_once_and_keeps_default(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "djmix": {"harmonic_mode": "same_key"}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.djmix.harmonic_mode == "compatible"
    assert len([record for record in caplog.records if "harmonic_mode" in record.message]) == 1


def test_invalid_enable_mood_arc_warns_once_and_keeps_default(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "playback": {"enable_mood_arc": "false"}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.enable_mood_arc is False
    assert len([record for record in caplog.records if "enable_mood_arc" in record.message]) == 1


def test_future_version_warns_but_restores_known_fields(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 99, "playback": {"prefetch_next_track": False}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.prefetch_next_track is False
    assert len([record for record in caplog.records if "schema_version 99" in record.message]) == 1


def test_unknown_future_field_is_ignored(tmp_path) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "playback": {"quantum_crossfade": True}},
    )
    player = _make_player()

    load_into_player(player, tmp_path)

    assert not hasattr(player._cfg.playback, "quantum_crossfade")


def test_null_clears_every_nullable_liner_cadence(tmp_path) -> None:
    player = _make_player()
    player._cfg.playback.liners_every_n_songs = 2
    player._cfg.playback.liners_every_minutes = 5.0
    player._cfg.playback.liners_random_min_minutes = 7.0
    player._cfg.playback.liners_random_max_minutes = 12.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {
                "liners_every_n_songs": None,
                "liners_every_minutes": None,
                "liners_random_min_minutes": None,
                "liners_random_max_minutes": None,
            },
        },
    )

    load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_every_n_songs is None
    assert player._cfg.playback.liners_every_minutes is None
    assert player._cfg.playback.liners_random_min_minutes is None
    assert player._cfg.playback.liners_random_max_minutes is None


def test_reversed_random_liner_window_warns_and_leaves_pair_unchanged(
    tmp_path,
    caplog,
) -> None:
    from random import Random

    from autodj.liners import LinerTrigger

    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 8.0
    player._cfg.playback.liners_random_max_minutes = 14.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {
                "liners_random_min_minutes": 16.0,
                "liners_random_max_minutes": 10.0,
            },
        },
    )

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    playback = player._cfg.playback
    assert playback.liners_random_min_minutes == 8.0
    assert playback.liners_random_max_minutes == 14.0
    assert (
        len([record for record in caplog.records if "random liner window" in record.message]) == 1
    )
    trigger = LinerTrigger(
        enabled=True,
        random_min_minutes=playback.liners_random_min_minutes,
        random_max_minutes=playback.liners_random_max_minutes,
    )
    target = trigger.roll_random_target(rng=Random(0))
    assert target is not None
    assert 8.0 <= target <= 14.0


def test_partial_random_liner_min_validates_against_current_max(
    tmp_path,
    caplog,
) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 8.0
    player._cfg.playback.liners_random_max_minutes = 14.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {"liners_random_min_minutes": 15.0},
        },
    )

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_random_min_minutes == 8.0
    assert player._cfg.playback.liners_random_max_minutes == 14.0
    assert (
        len([record for record in caplog.records if "random liner window" in record.message]) == 1
    )


def test_partial_random_liner_max_validates_against_current_min(
    tmp_path,
    caplog,
) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 8.0
    player._cfg.playback.liners_random_max_minutes = 14.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {"liners_random_max_minutes": 7.0},
        },
    )

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_random_min_minutes == 8.0
    assert player._cfg.playback.liners_random_max_minutes == 14.0
    assert (
        len([record for record in caplog.records if "random liner window" in record.message]) == 1
    )


def test_partial_valid_random_liner_bound_is_restored(tmp_path) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 8.0
    player._cfg.playback.liners_random_max_minutes = 14.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {"liners_random_min_minutes": 10.0},
        },
    )

    load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_random_min_minutes == 10.0
    assert player._cfg.playback.liners_random_max_minutes == 14.0


def test_null_random_liner_bound_clears_only_present_field(tmp_path) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 8.0
    player._cfg.playback.liners_random_max_minutes = 14.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {"liners_random_min_minutes": None},
        },
    )

    load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_random_min_minutes is None
    assert player._cfg.playback.liners_random_max_minutes == 14.0


@pytest.mark.parametrize("invalid", ["bad", float("inf"), 0, True])
def test_invalid_random_liner_bound_leaves_both_values_unchanged(
    tmp_path,
    caplog,
    invalid,
) -> None:
    player = _make_player()
    player._cfg.playback.liners_random_min_minutes = 8.0
    player._cfg.playback.liners_random_max_minutes = 14.0
    _write_state(
        tmp_path,
        {
            "schema_version": 1,
            "playback": {
                "liners_random_min_minutes": invalid,
                "liners_random_max_minutes": 12.0,
            },
        },
    )

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_random_min_minutes == 8.0
    assert player._cfg.playback.liners_random_max_minutes == 14.0
    assert (
        len([record for record in caplog.records if "liners_random_min_minutes" in record.message])
        == 1
    )


def test_null_bpm_range_clears_an_existing_range(tmp_path) -> None:
    player = _make_player()
    player._bpm_range = (90.0, 130.0)
    _write_state(tmp_path, {"schema_version": 1, "bpm_range": None})

    load_into_player(player, tmp_path)

    assert player._bpm_range is None


def test_infinite_playback_number_warns_and_keeps_default(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "playback": {"crossfade_seconds": float("inf")}},
    )
    player = _make_player()

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.crossfade_seconds == 3.0
    assert len([record for record in caplog.records if "crossfade_seconds" in record.message]) == 1


def test_infinite_liner_cadence_warns_and_keeps_default(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "playback": {"liners_every_minutes": float("inf")}},
    )
    player = _make_player()
    player._cfg.playback.liners_every_minutes = 5.0

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._cfg.playback.liners_every_minutes == 5.0
    assert (
        len([record for record in caplog.records if "liners_every_minutes" in record.message]) == 1
    )


def test_infinite_bpm_bound_warns_and_keeps_existing_range(tmp_path, caplog) -> None:
    _write_state(
        tmp_path,
        {"schema_version": 1, "bpm_range": {"lo": 90.0, "hi": float("inf")}},
    )
    player = _make_player()
    player._bpm_range = (100.0, 120.0)

    with caplog.at_level("WARNING"):
        load_into_player(player, tmp_path)

    assert player._bpm_range == (100.0, 120.0)
    assert len([record for record in caplog.records if "bpm_range" in record.message]) == 1


# ---------------------------------------------------------------------------
# autodj.runtime_state — _restore_validated_strings invalid branches
# ---------------------------------------------------------------------------


class TestRuntimeStateValidation:
    def test_invalid_transition_mode_logged_not_raised(self, caplog) -> None:
        from autodj.runtime_state import _restore_validated_strings

        cfg = MagicMock()
        cfg.playback.transition_mode = "full_intro_outro"
        with caplog.at_level("WARNING"):
            _restore_validated_strings(cfg, {"transition_mode": "garbage-mode"})
        assert any("transition_mode" in r.message for r in caplog.records)
        # Unchanged
        assert cfg.playback.transition_mode == "full_intro_outro"

    def test_invalid_key_notation_logged_not_raised(self, caplog) -> None:
        from autodj.runtime_state import _restore_validated_strings

        cfg = MagicMock()
        cfg.playback.key_notation = "camelot"
        with caplog.at_level("WARNING"):
            _restore_validated_strings(cfg, {"key_notation": "alien"})
        assert any("key_notation" in r.message for r in caplog.records)

    def test_valid_transition_mode_applied(self) -> None:
        from autodj.runtime_state import _restore_validated_strings

        cfg = MagicMock()
        _restore_validated_strings(cfg, {"transition_mode": "fixed"})
        assert cfg.playback.transition_mode == "fixed"
