"""Tests for recently-added server endpoints + behaviour.

Covers:
- ``GET /api/version`` (build-stamp footer endpoint)
- _version_info edge cases (subprocess / metadata / mtime fallbacks)
- advance_now log banner (INFO line on every track change)
- advance_now warning escalations (picker / refresh failures)
- repick_next blacklist-append failure path
- Camelot vs Musical key notation + sharps/flats preference
- PlayerBridge re-export identity from autodj.server -> autodj._bridge

Fixtures (`client`, `bridge`) come from ``conftest.py``; mock-builders
(``_make_entry`` / ``_make_player_mock`` / ``_make_sim_mock``) come from
``_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from autodj.indexer import IndexEntry
from autodj.server import create_app

from ._helpers import _make_entry


class TestVersion:
    def test_version_returns_200(self, client) -> None:
        assert client.get("/api/version").status_code == 200

    def test_version_payload_shape(self, client) -> None:
        data = client.get("/api/version").json()
        assert set(data.keys()) == {"version", "commit", "built_at"}
        assert data["version"]
        assert data["commit"]
        assert data["built_at"]

    def test_version_built_at_is_iso(self, client) -> None:
        import datetime as dt

        data = client.get("/api/version").json()
        # Round-trip parse; raises ValueError on malformed input.
        dt.datetime.fromisoformat(data["built_at"])


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


class TestAdvanceLogging:
    """The Advance banner + warning escalations added during the
    advance/analysis logging pass."""

    def test_advance_now_emits_info_banner(self, bridge, caplog) -> None:
        """advance_now logs a single INFO line with bpm + Camelot key."""
        import logging

        bridge.player._dry_run = True
        replacement = _make_entry(99)
        bridge.player._pick_next.return_value = replacement
        with caplog.at_level(logging.INFO, logger="autodj._bridge"):
            bridge.advance_now()
        banner = [r for r in caplog.records if r.message.startswith("Advance:")]
        assert len(banner) == 1
        msg = banner[0].getMessage()
        assert "BPM" in msg
        # Camelot label for key=0, mode=1 is "8B".
        assert "8B" in msg

    def test_advance_now_warns_when_pick_next_fails(self, bridge, caplog) -> None:
        """Picker failure on the *current* track is now WARNING, not debug."""
        import logging

        bridge.player._dry_run = True
        bridge.player._state.next_track = None
        bridge.player._state.queued_next = None
        bridge.player._state.queue = []
        bridge.player._pick_next.side_effect = RuntimeError("faiss kaboom")
        with caplog.at_level(logging.WARNING, logger="autodj._bridge"):
            bridge.advance_now()
        warns = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "_pick_next(cur) failed" in r.message
        ]
        assert len(warns) == 1

    def test_advance_now_warns_when_next_refresh_fails(self, bridge, caplog) -> None:
        """Next-track refresh failure is now WARNING, not debug."""
        import logging

        bridge.player._dry_run = True
        # The advance pulls the prefetched next_track directly (no
        # _pick_next call), then _pick_next runs once for the
        # refresh.  Make that single call raise.
        bridge.player._pick_next.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.WARNING, logger="autodj._bridge"):
            bridge.advance_now()
        warns = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "next-track refresh failed" in r.message
        ]
        assert len(warns) == 1
        # next_track cleared so the browser displays "no upcoming track".
        assert bridge.player._state.next_track is None


class TestVersionHelperEdgeCases:
    """Cover the _version_info branches that the happy-path /api/version
    test does not exercise."""

    def test_version_handles_subprocess_failure(self, monkeypatch) -> None:
        """Missing git on PATH falls through to commit='unknown'."""
        import subprocess as _sp

        from autodj.server import _version_info

        # Bust the cache so the patched failure is observed.
        _version_info.cache_clear()

        def _raise(*_a, **_kw):
            raise FileNotFoundError("git: not found")

        monkeypatch.setattr(_sp, "check_output", _raise)
        info = _version_info()
        # cache_clear so subsequent unrelated tests get a fresh real value.
        _version_info.cache_clear()
        assert info["commit"] == "unknown"
        assert info["version"]
        assert info["built_at"]

    def test_version_handles_missing_package_metadata(self, monkeypatch) -> None:
        """importlib.metadata raising PackageNotFoundError -> version='0.0.0'."""
        import importlib.metadata as _md

        from autodj.server import _version_info

        _version_info.cache_clear()

        def _missing(_name: str) -> str:
            raise _md.PackageNotFoundError("autodj")

        monkeypatch.setattr(_md, "version", _missing)
        info = _version_info()
        _version_info.cache_clear()
        assert info["version"] == "0.0.0"

    def test_version_built_at_falls_back_to_process_start(self, monkeypatch, tmp_path) -> None:
        """No bundled / source app.js anywhere -> built_at = datetime.now(UTC)."""
        from pathlib import Path as _P

        from autodj.server import _version_info

        _version_info.cache_clear()
        # Pretend nothing exists on either candidate path.
        original_exists = _P.exists

        def _never(self: _P) -> bool:
            if self.name == "app.js":
                return False
            return original_exists(self)

        monkeypatch.setattr(_P, "exists", _never)
        info = _version_info()
        _version_info.cache_clear()
        # ISO-with-Z-style timestamp; just sanity-parse.
        import datetime as _dt

        _dt.datetime.fromisoformat(info["built_at"])


class TestAdvanceBannerEdgeCases:
    """Edge branches in the Advance banner formatter -- BPM-missing
    fallback and log-banner exception handler."""

    def test_advance_banner_handles_zero_bpm_track(self, bridge, caplog) -> None:
        """A track with bpm=0 renders as 'BPM ?' in the banner."""
        import logging

        bridge.player._dry_run = True
        zero_bpm_entry = IndexEntry(
            path="Z:/Music/no_bpm.flac",
            title="Untagged",
            artist="Unknown",
            album="",
            genre="",
            bpm=0.0,
            year=0,
            length=120.0,
            energy=0.0,
            key=-1,
            mode=-1,
            tempo_confidence=0.0,
        )
        bridge.player._pick_next.return_value = zero_bpm_entry
        bridge.player._state.next_track = zero_bpm_entry
        with caplog.at_level(logging.INFO, logger="autodj._bridge"):
            bridge.advance_now()
        banners = [r for r in caplog.records if r.message.startswith("Advance:")]
        assert banners
        assert "BPM ?" in banners[0].getMessage()


class TestAdvanceBannerNoneOutgoing:
    """Cover the (cur is None) branch of the banner formatter -- fires
    on the very first seed advance when state.queued_next is set but
    nothing was playing yet."""

    def test_advance_banner_renders_none_outgoing(self, bridge, caplog) -> None:
        import logging

        bridge.player._dry_run = True
        bridge.player._state.current_track = None
        bridge.player._state.queued_next = _make_entry(7)
        with caplog.at_level(logging.INFO, logger="autodj._bridge"):
            bridge.advance_now()
        banners = [r for r in caplog.records if r.message.startswith("Advance:")]
        assert banners
        assert "(none)" in banners[0].getMessage()


class TestRepickNextErrorPaths:
    """The repick_next blacklist-append fallback (debug log only) used to
    crash callers when state.recently_played was a non-list mock."""

    def test_repick_next_handles_blacklist_append_failure(self, bridge) -> None:
        """state.recently_played raising on append must not crash repick."""
        bad_list = MagicMock()
        bad_list.append.side_effect = RuntimeError("backed by a frozenset?")
        bridge.player._state.recently_played = bad_list
        replacement = _make_entry(123)
        bridge.player._pick_next.return_value = replacement

        bridge.repick_next(blacklist_path="/library/missing.flac")
        # next_track still got refreshed even though blacklist append blew up.
        assert bridge.player._state.next_track is replacement


class TestKeyNotation:
    """Status payload exposes a notation-aware ``key_label`` for
    display + a separate always-Camelot ``camelot_cell`` for the
    Camelot wheel SVG (which is Camelot-shaped regardless of which
    notation the user picks)."""

    def test_status_includes_key_label_camelot_default(self, client) -> None:
        data = client.get("/api/status").json()
        ct = data["current_track"]
        # Fixture entry has key=0, mode=1 -> Camelot 8B (= C major).
        assert ct["camelot_cell"] == "8B"
        assert ct["key_label"] == "8B"

    def test_post_settings_switches_to_musical(self, client) -> None:
        # Flip to musical notation, then re-fetch status; key_label
        # follows.  camelot_cell stays put -- it drives the wheel SVG.
        resp = client.post(
            "/api/playback-settings",
            json={"key_notation": "musical"},
        )
        assert resp.status_code == 200
        data = client.get("/api/status").json()
        assert data["current_track"]["key_label"] == "C"
        assert data["current_track"]["camelot_cell"] == "8B"

    def test_post_settings_musical_with_flats(self, bridge) -> None:
        """Switching to musical + flats renders accidentals as flats."""
        from fastapi.testclient import TestClient

        # Replace the fixture's track with one keyed at C# / Db.
        bridge.player._state.current_track = IndexEntry(
            path="z:/x.flac",
            title="x",
            artist="a",
            album="",
            genre="",
            bpm=120.0,
            year=0,
            length=180.0,
            energy=0.0,
            key=1,
            mode=1,
            tempo_confidence=0.0,
        )
        bridge.player._cfg.playback.key_notation = "musical"
        bridge.player._cfg.playback.key_prefer_flats = True

        tc = TestClient(create_app(bridge))
        ct = tc.get("/api/status").json()["current_track"]
        assert ct["key_label"] == "Db"
        # Wheel SVG cell address is independent of display notation.
        assert ct["camelot_cell"] == "3B"


class TestBridgeReExport:
    """The split moved PlayerBridge to autodj._bridge but it must remain
    importable via the public autodj.server namespace."""

    def test_re_export_identity(self) -> None:
        from autodj._bridge import PlayerBridge as _Inner
        from autodj.server import PlayerBridge as _Outer

        assert _Outer is _Inner
        # Live in the private module, exposed via the public one.
        assert _Outer.__module__ == "autodj._bridge"
