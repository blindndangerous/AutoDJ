"""Bounded, asynchronous outbound notifications for track changes."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import random
import threading
import urllib.error
import urllib.request
from concurrent.futures import Future
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from autodj.config import WebhookConfig
from autodj.station_log import station_thread

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep a configured destination from redirecting to another scheme or host."""

    def redirect_request(
        self, _request: Any, _fp: Any, _code: int, _msg: str, _headers: Any, _newurl: str
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def webhook_body(config: WebhookConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a generic or Discord message without exposing configuration."""
    if config.format == "generic":
        return {**payload, "event": "track_change"}
    track = payload.get("current_track") or {}
    artist = str(track.get("artist") or "Unknown artist")[:256]
    title = str(track.get("title") or "Unknown title")[:256]
    album = str(track.get("album") or "")[:256]
    station = str(payload.get("station_name") or "AutoDJ")[:256]
    description = f"{artist} — {title}"
    if album:
        description += f"\nAlbum: {album}"
    return {"embeds": [{"title": station, "description": description[:4096]}]}


def _retry_after(value: str) -> float | None:
    """Read Retry-After seconds or an HTTP date."""
    with contextlib.suppress(ValueError):
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0.0, seconds)
    with contextlib.suppress(ValueError, TypeError):
        date = parsedate_to_datetime(value)
        return max(0.0, (date - datetime.now(UTC)).total_seconds())
    return None


def _post(config: WebhookConfig, payload: dict[str, Any]) -> tuple[bool, float | None]:
    body = json.dumps(webhook_body(config, payload)).encode("utf-8")
    request = urllib.request.Request(
        config.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with _OPENER.open(request, timeout=5) as response:
            return 200 <= response.status < 300, None
    except urllib.error.HTTPError as exc:
        retry_after = None
        if exc.code == 429:
            retry_after = _retry_after(exc.headers.get("Retry-After", ""))
        return False, retry_after
    except (OSError, TimeoutError):
        return False, None


#: The longest a destination's ``Retry-After`` may hold back its next try.
MAX_RETRY_AFTER_S = 30.0
#: Track changes get this many tries; a test send gets one, so its result
#: comes back while the person who pressed Send test is still waiting.
ATTEMPTS = 3


class WebhookWorker:
    """Keep only the latest pending delivery for each configured destination.

    Each destination has its own thread, so a slow or rate-limited one never
    delays another, and nothing here runs on the playout threads.
    """

    def __init__(self, configs: tuple[WebhookConfig, ...]) -> None:
        self.configs = configs
        self._condition = threading.Condition()
        self._pending: dict[int, tuple[dict[str, Any], Future[bool] | None]] = {}
        self._status = ["Not sent" for _ in configs]
        self._failed = [False for _ in configs]
        self._closed = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._threads = [
            station_thread(
                target=self._run, args=(index,), name=f"autodj-webhook-{index + 1}", daemon=True
            )
            for index in range(len(self.configs))
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for _payload, result in self._pending.values():
                if result is not None and not result.done():
                    result.set_result(False)
            self._pending.clear()
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(timeout=0.1)

    def submit(self, payload: dict[str, Any], index: int | None = None) -> Future[bool] | None:
        """Enqueue without waiting for a network request or a sleeping retry.

        With *index*, this is a test send to that one destination and the
        returned future says whether it was delivered.  A newer payload
        replaces one still waiting; a waiting test send's future is then
        answered by the newer delivery instead of failing.
        """
        result: Future[bool] | None = Future() if index is not None else None
        with self._condition:
            if self._closed:
                if result is not None:
                    result.set_result(False)
                return result
            for target in range(len(self.configs)) if index is None else (index,):
                previous = self._pending.get(target)
                waiting = previous[1] if previous else None
                if waiting is not None and result is not None:
                    # A cancelled future (its route timed out) is done.
                    if not waiting.done():
                        waiting.set_result(False)
                    waiting = None
                self._pending[target] = (payload, result if result is not None else waiting)
            self._condition.notify_all()
        return result

    def status(self) -> list[dict[str, str | int]]:
        """Return only host names, formats and results for the paired UI."""
        with self._condition:
            return [
                {
                    "index": index,
                    "host": urlsplit(config.url).hostname or "",
                    "format": config.format,
                    "result": self._status[index],
                }
                for index, config in enumerate(self.configs)
            ]

    def _run(self, index: int) -> None:
        while True:
            with self._condition:
                while index not in self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                payload, result = self._pending.pop(index)
            success = self._deliver(index, payload, 1 if result is not None else ATTEMPTS)
            if result is not None and not result.done():
                result.set_result(success)

    def _deliver(self, index: int, payload: dict[str, Any], attempts: int = ATTEMPTS) -> bool:
        for attempt in range(attempts):
            success, retry_after = _post(self.configs[index], payload)
            if success:
                with self._condition:
                    self._status[index] = "Delivered"
                    self._failed[index] = False
                return True
            if attempt < attempts - 1:
                delay = (
                    min(retry_after, MAX_RETRY_AFTER_S)
                    if retry_after is not None
                    else (1, 4)[min(attempt, 1)]
                )
                with self._condition:
                    # A newer payload replaces this one rather than waiting.
                    self._condition.wait_for(
                        lambda: index in self._pending or self._closed,
                        timeout=delay + random.uniform(0, 0.25),
                    )
                    if index in self._pending or self._closed:
                        return False
        with self._condition:
            self._status[index] = "Delivery failed"
            if not self._failed[index]:
                logger.warning("Webhook delivery failed for destination %d", index + 1)
            self._failed[index] = True
        return False
