"""Tests for autodj.runtime_state — settings persistence across restarts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj._bridge import PlayerBridge
from autodj.player import PlayerState
from autodj.runtime_state import (
    STATE_VERSION,
    load_into_bridge,
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
        no_repeat_window=20,
        artist_repeat_window=3,
    )
    cfg = SimpleNamespace(
        transitions=SimpleNamespace(effect="none", wet_mix=1.0),
        djmix=SimpleNamespace(
            harmonic_mode="off",
            beatmatch=False,
            phrase_align=False,
            outro_intro_align=False,
            filter_sweep=False,
            phrase_bars=8,
        ),
        playback=playback,
        replaygain=SimpleNamespace(enabled=False, target_db=-14.0),
        stream=SimpleNamespace(bitrate=320),
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
        _state=PlayerState(no_repeat_window=20),
        _sim=SimpleNamespace(entries_snapshot=lambda: (), ntotal=0),
    )


def _write_state(index_dir: Path, payload: object) -> None:
    if isinstance(payload, dict):
        payload = {"schema_version": STATE_VERSION, **payload}
    (index_dir / "web_state.json").write_text(json.dumps(payload), encoding="utf-8")


def _load(player: SimpleNamespace, index_dir: Path, skip: frozenset[str] = frozenset()) -> None:
    load_into_bridge(PlayerBridge(player, player._sim), index_dir, skip)


def _warnings(caplog: pytest.LogCaptureFixture, field: str) -> int:
    return len([r for r in caplog.records if f"invalid {field} " in r.message])


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"preset": 12}, "preset"),
        ({"preset": "nosuchpreset_xyz"}, "preset"),
        ({"transition": 12}, "transition"),
        ({"transition": "no_such_effect"}, "transition"),
        ({"djmix": {"harmonic_mode": "same_key"}}, "harmonic_mode"),
        ({"djmix": {"harmonic_mode": True}}, "harmonic_mode"),
        ({"djmix": {"phrase_bars": 0}}, "phrase_bars"),
        ({"djmix": {"beatmatch": "true"}}, "beatmatch"),
        ({"playback": {"transition_mode": 12}}, "transition_mode"),
        ({"playback": {"transition_mode": "garbage-mode"}}, "transition_mode"),
        ({"playback": {"key_notation": "alien"}}, "key_notation"),
        ({"playback": {"liners_pick_mode": "invalid"}}, "liners_pick_mode"),
        ({"playback": {"liners_duck_db": 1.0}}, "liners_duck_db"),
        ({"playback": {"liners_duck_db": -45.0}}, "liners_duck_db"),
        ({"playback": {"liners_every_minutes": float("inf")}}, "liners_every_minutes"),
        ({"playback": {"crossfade_seconds": float("inf")}}, "crossfade_seconds"),
        ({"playback": {"crossfade_seconds": None}}, "crossfade_seconds"),
        ({"playback": {"crossfade_eq_duck": "false"}}, "crossfade_eq_duck"),
        ({"playback": {"enable_mood_arc": "false"}}, "enable_mood_arc"),
        ({"playback": {"stream_bitrate": 100}}, "stream_bitrate"),
        ({"playback": {"stream_bitrate": "320"}}, "stream_bitrate"),
        ({"playback": {"stream_bitrate": True}}, "stream_bitrate"),
        ({"playback": {"no_repeat_window": -1}}, "no_repeat_window"),
        ({"playback": {"artist_repeat_window": 2.5}}, "artist_repeat_window"),
        ({"playback": {"transition_wet_mix": 2.0}}, "transition_wet_mix"),
        ({"playback": {"replaygain_target_db": "loud"}}, "replaygain_target_db"),
        ({"playback": {"volume": True}}, "volume"),
        ({"playback": {"volume": "0.5"}}, "volume"),
        ({"playback": {"is_muted": "true"}}, "is_muted"),
        # Config-only options are not web settings, so they are never restored.
        ({"playback": {"prefetch_next_track": False}}, "prefetch_next_track"),
        ({"playback": "invalid"}, "playback"),
        ({"bpm_range": "invalid"}, "bpm_range"),
        ({"bpm_range": None}, "bpm_range"),
        ({"bpm_range": {"lo": 90.0, "hi": float("inf")}}, "bpm_range"),
        ({"discovery_every": "invalid"}, "discovery_every"),
        ({"quantum_crossfade": True}, "quantum_crossfade"),
    ],
)
def test_one_invalid_field_is_warned_and_the_rest_still_apply(
    tmp_path: Path, caplog, payload, field
) -> None:
    player = _make_player()
    before = PlayerBridge(player, player._sim).get_settings()
    playback = payload.get("playback", {})
    rest = {"crossfade_seconds": 7.0} if isinstance(playback, dict) else {}
    _write_state(
        tmp_path,
        {
            "discovery_every": 11,
            **payload,
            "playback": {**rest, **playback} if isinstance(playback, dict) else playback,
        },
    )

    with caplog.at_level("WARNING"):
        _load(player, tmp_path)

    after = PlayerBridge(player, player._sim).get_settings()
    assert _warnings(caplog, field) == 1
    if field != "discovery_every":
        assert after["discovery_every"] == 11
    if rest and field != "crossfade_seconds":
        assert after["playback"]["crossfade_seconds"] == 7.0
    for key in ("preset", "transition", "djmix", "bpm_range"):
        assert after[key] == before[key]
    unchanged = {k for k in before["playback"] if k != "crossfade_seconds"}
    assert {k: after["playback"][k] for k in unchanged} == {
        k: before["playback"][k] for k in unchanged
    }


@pytest.mark.parametrize("version", [None, 0, 1, 3, "2", True])
def test_state_with_another_schema_version_is_ignored(tmp_path: Path, caplog, version) -> None:
    """Only version 2 is read; version 1 stored "compatible" beside
    ``harmonic_mixing: false``, so reading it would turn key filtering on."""
    state: dict = {"transition": "echo_out", "djmix": {"harmonic_mode": "compatible"}}
    if version is not None:
        state["schema_version"] = version
    (tmp_path / "web_state.json").write_text(json.dumps(state), encoding="utf-8")
    player = _make_player()

    _load(player, tmp_path)

    assert player._cfg.transitions.effect == "none"
    assert player._cfg.djmix.harmonic_mode == "off"
    assert "ignoring web_state.json: schema_version" in caplog.text


def test_non_object_state_root_is_warned_and_ignored(tmp_path: Path, caplog) -> None:
    _write_state(tmp_path, ["not", "an", "object"])

    _load(_make_player(), tmp_path)

    assert "root is not an object" in caplog.text


def test_settings_given_on_the_command_line_are_not_restored(tmp_path: Path) -> None:
    player = _make_player()
    player._cfg.transitions.effect = "echo_out"
    player._cfg.djmix.beatmatch = True
    player._cfg.playback.show_lyrics = False
    player._bpm_range = (100.0, 120.0)
    _write_state(
        tmp_path,
        {
            "transition": "rotate",
            "djmix": {"beatmatch": False, "phrase_align": True},
            "playback": {"show_lyrics": True, "crossfade_seconds": 7.0},
            "bpm_range": {"lo": 80.0, "hi": 90.0},
            "discovery_every": 9,
        },
    )

    _load(player, tmp_path, frozenset({"transition", "beatmatch", "show_lyrics", "bpm_range"}))

    assert player._cfg.transitions.effect == "echo_out"
    assert player._cfg.djmix.beatmatch is True
    assert player._cfg.playback.show_lyrics is False
    assert player._bpm_range == (100.0, 120.0)
    # Everything the command line did not set still comes back.
    assert player._cfg.djmix.phrase_align is True
    assert player._cfg.playback.crossfade_seconds == 7.0
    assert player._discovery_every == 9


def test_mood_arc_follows_the_restored_switch(tmp_path: Path) -> None:
    player = _make_player()
    _write_state(tmp_path, {"playback": {"enable_mood_arc": True, "mood_arc_hours": 2.5}})
    _load(player, tmp_path)
    assert player._mood_arc is not None

    _write_state(tmp_path, {"playback": {"enable_mood_arc": False}})
    _load(player, tmp_path)
    assert player._mood_arc is None


def test_null_discovery_clears_existing_cadence(tmp_path: Path) -> None:
    player = _make_player()
    player._discovery_every = 4
    _write_state(tmp_path, {"discovery_every": None})

    _load(player, tmp_path)

    assert player._discovery_every is None


def test_null_clears_every_nullable_liner_cadence(tmp_path) -> None:
    """The saved settings store a switched-off liner trigger as null."""
    player = _make_player()
    player._cfg.playback.liners_every_n_songs = 2
    player._cfg.playback.liners_every_minutes = 5.0
    player._cfg.playback.liners_random_min_minutes = 7.0
    player._cfg.playback.liners_random_max_minutes = 12.0
    _write_state(
        tmp_path,
        {
            "playback": {
                "liners_every_n_songs": None,
                "liners_every_minutes": None,
                "liners_random_min_minutes": None,
                "liners_random_max_minutes": None,
            },
        },
    )

    _load(player, tmp_path)

    assert player._cfg.playback.liners_every_n_songs is None
    assert player._cfg.playback.liners_every_minutes is None
    assert player._cfg.playback.liners_random_min_minutes is None
    assert player._cfg.playback.liners_random_max_minutes is None


class TestStateFile:
    def test_returns_path_when_index_dir_set(self, tmp_path) -> None:
        path = state_file_for(tmp_path)
        assert path == tmp_path / "web_state.json"

    def test_returns_none_for_none_input(self) -> None:
        assert state_file_for(None) is None


class TestLoadInto:
    def test_no_file_is_no_op(self, tmp_path) -> None:
        p = _make_player()
        _load(p, tmp_path)  # no state file present
        assert p._cfg.transitions.effect == "none"

    def test_unreadable_file_is_no_op(self, tmp_path) -> None:
        (tmp_path / "web_state.json").write_text("not json {{{", encoding="utf-8")
        p = _make_player()
        _load(p, tmp_path)
        assert p._cfg.transitions.effect == "none"

    def test_invalid_utf8_is_warned_and_ignored(self, tmp_path, caplog) -> None:
        (tmp_path / "web_state.json").write_bytes(b"\xff\xfe\xfa")
        p = _make_player()

        with caplog.at_level("WARNING"):
            _load(p, tmp_path)

        assert p._cfg.transitions.effect == "none"
        assert len([record for record in caplog.records if "unreadable" in record.message]) == 1

    def test_loads_bpm_range(self, tmp_path) -> None:
        _write_state(tmp_path, {"bpm_range": {"lo": 90, "hi": 140}})
        p = _make_player()
        _load(p, tmp_path)
        assert p._bpm_range == (90.0, 140.0)

    def test_clears_bpm_range_on_null_bounds(self, tmp_path) -> None:
        _write_state(tmp_path, {"bpm_range": {"lo": None, "hi": None}})
        p = _make_player()
        p._bpm_range = (90.0, 140.0)
        _load(p, tmp_path)
        assert p._bpm_range is None

    def test_clears_discovery_on_zero(self, tmp_path) -> None:
        _write_state(tmp_path, {"discovery_every": 0})
        p = _make_player()
        p._discovery_every = 20
        _load(p, tmp_path)
        assert p._discovery_every is None

    def test_load_restores_a_saved_preset(self, tmp_path) -> None:
        from autodj.presets import BUILTIN_PRESETS

        _write_state(tmp_path, {"preset": "wakeup"})
        p = _make_player()
        _load(p, tmp_path)
        assert p._preset is BUILTIN_PRESETS["wakeup"]

    def test_stream_bitrate_is_restored_into_the_stream_config(self, tmp_path) -> None:
        player = _make_player()
        _write_state(tmp_path, {"playback": {"stream_bitrate": 128}})
        _load(player, tmp_path)
        assert player._cfg.stream.bitrate == 128


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

    def test_file_fsync_failure_preserves_old_state_and_cleans_temp(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ) -> None:
        path = tmp_path / "web_state.json"
        path.write_text('{"preset": "old"}', encoding="utf-8")

        def fail_fsync(_fd: int) -> None:
            raise OSError("storage flush failed")

        monkeypatch.setattr(os, "fsync", fail_fsync)

        with caplog.at_level("WARNING"):
            save_from_player({"preset": "new"}, tmp_path)

        assert json.loads(path.read_text(encoding="utf-8"))["preset"] == "old"
        assert not list(tmp_path.glob("*.tmp"))
        assert len([record for record in caplog.records if "Failed to save" in record.message]) == 1

    def test_no_index_dir_is_no_op(self) -> None:
        save_from_player({"preset": "chill"}, None)


class TestRoundTrip:
    def test_save_bridge_snapshot_then_load_preserves_every_persisted_field(
        self,
        tmp_path,
    ) -> None:
        p1 = _make_player()
        p1._cfg.djmix.harmonic_mode = "strict"
        p1._cfg.djmix.beatmatch = True
        p1._cfg.djmix.phrase_align = True
        p1._cfg.djmix.outro_intro_align = True
        p1._cfg.djmix.filter_sweep = True
        p1._cfg.djmix.phrase_bars = 16
        p1._cfg.transitions.effect = "echo_out"
        p1._cfg.transitions.wet_mix = 0.6
        p1._cfg.replaygain.target_db = -18.0
        p1._cfg.playback.no_repeat_window = 50
        p1._cfg.playback.artist_repeat_window = 5
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
        p1._cfg.stream.bitrate = 192
        p1._bpm_range = (100.0, 132.0)
        p1._discovery_every = 9
        p1._state.volume = 0.0014
        p1._state.is_muted = True

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
            "harmonic_mode",
            "beatmatch",
            "phrase_align",
            "outro_intro_align",
            "filter_sweep",
            "phrase_bars",
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
            "stream_bitrate",
            "no_repeat_window",
            "artist_repeat_window",
            "transition_wet_mix",
            "replaygain_target_db",
            "volume",
            "is_muted",
            # Bridge-visible derived session values are never persisted.
            "library_size",
        }
        # Config-only absolute path is neither browser-visible nor persisted.
        assert "liners_folder" not in saved["playback"]
        saved["playback"]["liners_folder"] = p1._cfg.playback.liners_folder
        save_from_player(saved, tmp_path)
        stored = json.loads((tmp_path / "web_state.json").read_text(encoding="utf-8"))
        assert stored["schema_version"] == STATE_VERSION
        assert "available_presets" not in stored
        assert "library_size" not in stored["playback"]
        assert "liners_folder" not in stored["playback"]
        # Config-only options stay with config.toml.
        assert "prefetch_next_track" not in stored["playback"]
        assert "silence_trigger_crossfade" not in stored["playback"]

        p2 = _make_player()
        _load(p2, tmp_path)
        restored = PlayerBridge(p2, p2._sim).get_settings()
        expected_playback = {
            key: value
            for key, value in saved["playback"].items()
            if key
            not in {
                "library_size",
                "liners_folder",
                "prefetch_next_track",
                "silence_trigger_crossfade",
            }
        }
        assert restored["transition"] == saved["transition"]
        assert restored["djmix"] == saved["djmix"]
        assert {key: restored["playback"][key] for key in expected_playback} == expected_playback
        assert restored["bpm_range"] == saved["bpm_range"]
        assert restored["discovery_every"] == saved["discovery_every"]
        # The running player's history windows follow the restored values.
        assert p2._state.recently_played.maxlen == 50
        assert p2._state.recently_played_artists.maxlen == 5
