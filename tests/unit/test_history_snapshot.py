"""The browser history response needs a recording identity for Track info."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from autodj._bridge import PlayerBridge


def test_history_snapshot_keeps_recording_path_and_play_id() -> None:
    bridge = PlayerBridge(player=MagicMock(), sim=MagicMock())
    bridge.on_track_started(
        SimpleNamespace(path="/music/song.flac", title="Song", artist="Artist", length=60)
    )

    row = bridge.history_snapshot()[0]
    assert row["path"] == "/music/song.flac"
    assert row["event_id"] == bridge._play_history[0]["event_id"]
    assert set(row) == {"event_id", "path", "title", "artist", "duration", "played_at"}
