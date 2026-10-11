"""Public metadata and webhook isolation."""

from __future__ import annotations

import threading
import time
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from autodj.config import NowPlayingConfig, WebhookConfig, load_config
from autodj.nowplaying import NowPlaying
from autodj.webhooks import (
    MAX_RETRY_AFTER_S,
    WebhookWorker,
    _post,
    _retry_after,
    webhook_body,
)


def test_snapshot_exposes_metadata_without_paths() -> None:
    current = SimpleNamespace(
        artist="Artist", title="Song", album="Album", length=180, path="secret"
    )
    next_track = SimpleNamespace(
        artist="Next", title="Later", album="Other", length=200, path="private"
    )
    cfg = SimpleNamespace(
        stream=SimpleNamespace(station_name="Station"),
        nowplaying=NowPlayingConfig(show_next=False, history_limit=1),
    )
    history_snapshot = Mock(
        return_value=[
            {
                "artist": "Old",
                "title": "Earlier",
                "duration": 90,
                "played_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "artist": "Artist",
                "title": "Song",
                "duration": 180,
                "played_at": "2026-01-01T01:00:00+00:00",
            },
        ]
    )
    bridge = SimpleNamespace(
        _open_play=(current, {"played_at": "2026-01-01T01:00:00+00:00"}),
        player=SimpleNamespace(
            _cfg=cfg,
            _state=SimpleNamespace(current_track=current, next_track=next_track),
            _playback_pos=[220500],
            _current_sr=44100,
        ),
        stream=SimpleNamespace(listener_count=3),
        history_snapshot=history_snapshot,
    )
    feed = NowPlaying(bridge, timezone="UTC")
    payload = feed.snapshot()
    assert payload["schema"] == 2
    assert payload["next_track"] is None
    assert payload["listener_count"] == 3
    assert payload["history"][0]["title"] == "Earlier"
    assert payload["current_track"]["album"] == "Album"
    assert feed.snapshot()["current_track"]["elapsed"] == 5
    assert "secret" not in str(payload) and "private" not in str(payload)
    feed.snapshot()
    history_snapshot.assert_called_once()


def test_webhook_urls_are_local_only_and_redacted(tmp_path) -> None:
    shared = tmp_path / "config.toml"
    shared.write_text('[[webhooks]]\nurl = "https://example.com/token"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"config\.local\.toml"):
        load_config(shared)
    shared.write_text("", encoding="utf-8")
    (tmp_path / "config.local.toml").write_text(
        '[[webhooks]]\nurl = "https://example.com/token"\nformat = "discord"\n', encoding="utf-8"
    )
    config = load_config(shared).webhooks[0]
    assert "token" not in repr(config)
    assert WebhookWorker((config,)).status()[0] == {
        "index": 0,
        "host": "example.com",
        "format": "discord",
        "result": "Not sent",
    }


def test_webhook_payload_formats_and_retry() -> None:
    assert _retry_after("2") == 2
    assert _retry_after("invalid") is None
    config = WebhookConfig("https://example.com/token")
    payload = {"station_name": "Station", "current_track": {"artist": "A", "title": "T"}}
    assert webhook_body(config, payload)["event"] == "track_change"
    discord = webhook_body(WebhookConfig("https://example.com", "discord"), payload)
    assert discord["embeds"][0]["description"] == "A — T"
    worker = WebhookWorker((config,))
    with patch("autodj.webhooks._post", side_effect=[(False, 0), (True, None)]) as send:
        worker.start()
        assert worker.submit(payload) is None
        deadline = time.monotonic() + 2
        while worker.status()[0]["result"] != "Delivered" and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.close()
    assert send.call_count == 2
    assert worker.status()[0]["result"] == "Delivered"


def test_show_next_is_off_by_default() -> None:
    assert NowPlayingConfig().show_next is False


def test_a_test_send_is_tried_once_and_reports_failure() -> None:
    worker = WebhookWorker((WebhookConfig("https://example.com/token"),))
    with patch("autodj.webhooks._post", return_value=(False, None)) as send:
        worker.start()
        result = worker.submit({"station_name": "S"}, 0)
        assert result is not None and result.result(timeout=2) is False
        worker.close()
    assert send.call_count == 1
    assert worker.status()[0]["result"] == "Delivery failed"


def test_retry_after_is_capped(monkeypatch) -> None:
    worker = WebhookWorker((WebhookConfig("https://example.com/token"),))
    waits: list[float] = []

    def wait_for(_predicate, timeout):
        waits.append(timeout)
        return False

    monkeypatch.setattr(worker._condition, "wait_for", wait_for)
    with patch("autodj.webhooks._post", side_effect=[(False, 3600.0), (False, None), (True, None)]):
        assert worker._deliver(0, {"station_name": "S"}) is True
    assert MAX_RETRY_AFTER_S <= waits[0] < MAX_RETRY_AFTER_S + 1
    assert 4 <= waits[1] < 5


def test_slow_destination_does_not_hold_up_another() -> None:
    slow, fast = WebhookConfig("https://slow.example/a"), WebhookConfig("https://fast.example/b")
    release = threading.Event()

    def send(config, _payload):
        if config is slow:
            assert release.wait(2)
        return True, None

    worker = WebhookWorker((slow, fast))
    with patch("autodj.webhooks._post", side_effect=send):
        worker.start()
        worker.submit({"station_name": "S"})
        result = worker.submit({"station_name": "S"}, 1)
        assert result is not None and result.result(timeout=1) is True
        release.set()
        worker.close()


def test_track_change_answers_a_waiting_test_and_close_answers_the_rest() -> None:
    worker = WebhookWorker((WebhookConfig("https://example.com/token"),))
    waiting = worker.submit({"station_name": "test"}, 0)
    worker.submit({"station_name": "track"})
    assert waiting is not None and not waiting.done()
    cancelled = worker.submit({"station_name": "again"}, 0)
    assert waiting.result(timeout=0) is False
    assert cancelled is not None and cancelled.cancel()
    worker.submit({"station_name": "replaces a cancelled test"}, 0)
    later = worker.submit({"station_name": "last"}, 0)
    worker.close()
    assert later is not None and later.result(timeout=0) is False
    closed = worker.submit({"station_name": "after close"}, 0)
    assert closed is not None and closed.result(timeout=0) is False


def test_rate_limit_retry_after_and_latest_pending_payload() -> None:
    config = WebhookConfig("https://example.com/token")
    error = urllib.error.HTTPError(config.url, 429, "rate limited", {"Retry-After": "3"}, None)
    with patch("autodj.webhooks._OPENER.open", side_effect=error):
        assert _post(config, {"current_track": None}) == (False, 3)

    entered = threading.Event()
    release = threading.Event()
    sent: list[str] = []

    def send(_config, payload):
        sent.append(payload["station_name"])
        if len(sent) == 1:
            entered.set()
            assert release.wait(2)
            return False, None
        return True, None

    worker = WebhookWorker((config,))
    with patch("autodj.webhooks._post", side_effect=send):
        worker.start()
        first = worker.submit({"station_name": "old"}, 0)
        assert entered.wait(2)
        second = worker.submit({"station_name": "new"}, 0)
        release.set()
        assert first is not None and first.result(timeout=2) is False
        assert second is not None and second.result(timeout=2) is True
        worker.close()
    assert sent == ["old", "new"]


def _feed_bridge(listeners: list[int]) -> SimpleNamespace:
    cfg = SimpleNamespace(
        stream=SimpleNamespace(station_name="Station"),
        nowplaying=NowPlayingConfig(),
    )

    class _Stream:
        @property
        def listener_count(self) -> int:
            return listeners[0]

    stream = _Stream()
    return SimpleNamespace(
        player=SimpleNamespace(
            _cfg=cfg, _state=SimpleNamespace(current_track=None, next_track=None)
        ),
        stream=stream,
        history_snapshot=Mock(return_value=[]),
    )


def test_peak_listeners_today_resets_at_station_midnight_and_uptime_counts_up() -> None:
    listeners = [2]
    # 23:00 on 10 October in Denver, after midnight UTC.
    now = [datetime(2026, 10, 11, 5, 0, tzinfo=UTC)]
    clock = [100.0]
    feed = NowPlaying(
        _feed_bridge(listeners),
        timezone="America/Denver",
        clock=lambda: now[0],
        monotonic=lambda: clock[0],
    )
    assert feed.refresh()["peak_listeners_today"] == 2
    listeners[0] = 5
    feed.refresh()
    listeners[0] = 1
    clock[0] = 190.9
    payload = feed.refresh()
    assert payload["listener_count"] == 1
    assert payload["peak_listeners_today"] == 5
    assert payload["uptime_seconds"] == 90
    # Midnight in Denver (06:00 UTC) starts a new day from whoever is
    # listening, even before anyone connects or a song changes.
    now[0] = datetime(2026, 10, 11, 6, 0, tzinfo=UTC)
    assert feed.snapshot()["peak_listeners_today"] == 1
    listeners[0] = 3
    assert feed.refresh()["peak_listeners_today"] == 3
