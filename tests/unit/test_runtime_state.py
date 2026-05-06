"""Tests for autodj.runtime_state — settings persistence across restarts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from autodj.runtime_state import (
    load_into_player,
    save_from_player,
    state_file_for,
)


def _make_player() -> MagicMock:
    """Mock player wired to a real-ish config dataclass tree."""
    p = MagicMock()
    cfg = MagicMock()
    cfg.transitions.effect = "none"
    cfg.djmix.harmonic_mixing = False
    cfg.djmix.beatmatch = False
    cfg.djmix.phrase_align = False
    cfg.djmix.outro_intro_align = False
    cfg.djmix.filter_sweep = False
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.crossfade_eq_duck = False
    cfg.playback.transition_mode = "full_intro_outro"
    cfg.replaygain.enabled = False
    cfg.presets = {}
    p._cfg = cfg
    p._smart_shuffle = False
    p._bpm_range = None
    p._preset = None
    p._discovery_every = None
    return p


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
    def test_save_then_load_preserves_settings(self, tmp_path) -> None:
        p1 = _make_player()
        # Pretend the user set some settings
        p1._cfg.transitions.effect = "tape_stop"
        p1._cfg.djmix.beatmatch = True
        p1._cfg.playback.crossfade_seconds = 5.0
        p1._smart_shuffle = True
        p1._bpm_range = (100.0, 130.0)
        p1._discovery_every = 12

        save_from_player(
            {
                "preset": None,
                "transition": p1._cfg.transitions.effect,
                "djmix": {
                    "harmonic_mixing": p1._cfg.djmix.harmonic_mixing,
                    "beatmatch": p1._cfg.djmix.beatmatch,
                    "phrase_align": p1._cfg.djmix.phrase_align,
                    "outro_intro_align": p1._cfg.djmix.outro_intro_align,
                    "filter_sweep": p1._cfg.djmix.filter_sweep,
                },
                "playback": {
                    "crossfade_seconds": p1._cfg.playback.crossfade_seconds,
                    "crossfade_eq_duck": p1._cfg.playback.crossfade_eq_duck,
                    "smart_shuffle": p1._smart_shuffle,
                    "replaygain_enabled": p1._cfg.replaygain.enabled,
                },
                "bpm_range": {"lo": p1._bpm_range[0], "hi": p1._bpm_range[1]},
                "discovery_every": p1._discovery_every,
            },
            tmp_path,
        )

        # Fresh player loads back the same settings
        p2 = _make_player()
        load_into_player(p2, tmp_path)
        assert p2._cfg.transitions.effect == "tape_stop"
        assert p2._cfg.djmix.beatmatch is True
        assert p2._cfg.playback.crossfade_seconds == 5.0
        assert p2._smart_shuffle is True
        assert p2._bpm_range == (100.0, 130.0)
        assert p2._discovery_every == 12
