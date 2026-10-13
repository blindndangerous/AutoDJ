"""Listener statistics: peaks, sessions, listening time, retention and privacy (#50)."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from autodj.listener_stats import SESSION_BANDS, ListenerStats, session_band

# 2026-10-13 10:00 in New York (UTC-4).
MORNING = datetime(2026, 10, 13, 14, 0, tzinfo=UTC).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR


class Clock:
    def __init__(self, at: float = MORNING) -> None:
        self.at = at
        self.mono = 0.0

    def __call__(self) -> float:
        return self.at

    def monotonic(self) -> float:
        return self.mono


def _stats(tmp_path: Path, clock: Clock, keep: list[int] | None = None) -> ListenerStats:
    keep = keep if keep is not None else [90]
    return ListenerStats(
        tmp_path / "listener_stats.json",
        timezone="America/New_York",
        keep_days=lambda: keep[0],
        clock=clock,
        monotonic=clock.monotonic,
    )


def test_session_bands() -> None:
    assert [session_band(s) for s in (0, 59.9, 60, 299, 300, 899, 900, 3599)] == [
        0,
        0,
        1,
        1,
        2,
        2,
        3,
        3,
    ]
    assert session_band(3600) == 4 and session_band(3 * HOUR) == 5 and session_band(1e9) == 5
    assert len(SESSION_BANDS) == 6


def test_peaks_sessions_and_listening_time_by_format(tmp_path: Path) -> None:
    clock = Clock()
    stats = _stats(tmp_path, clock)
    a = stats.connected("mp3")
    clock.at += 60
    b = stats.connected("aac")
    c = stats.connected("opus")
    clock.at += 30 * 60
    stats.disconnected("aac", b)
    stats.disconnected("mp3", a)
    stats.disconnected("opus", c)
    summary = stats.summary(7, {"mp3": 0, "aac": 0, "opus": 0})
    [today] = summary["days"]
    assert today["date"] == "2026-10-13"
    assert today["peak"] == 3 and today["peak_at"] == "10:01"
    assert today["sessions"] == 3
    assert today["seconds"] == pytest.approx(31 * 60 + 2 * 30 * 60)
    assert today["longest"] == pytest.approx(31 * 60)
    assert today["bands"] == [0, 0, 0, 3, 0, 0]
    assert today["formats"] == {
        "mp3": {"peak": 1, "sessions": 1, "seconds": 1860.0},
        "aac": {"peak": 1, "sessions": 1, "seconds": 1800.0},
        "opus": {"peak": 1, "sessions": 1, "seconds": 1800.0},
    }
    period = summary["period"]
    assert period["sessions"] == 3 and period["peak"] == 3
    assert period["peak_date"] == "2026-10-13" and period["peak_at"] == "10:01"
    assert period["bands"] == [0, 0, 0, 3, 0, 0]
    assert summary["now"] == {"total": 0, "formats": {"mp3": 0, "aac": 0, "opus": 0}}
    assert summary["keep_days"] == 90 and summary["days_shown"] == 7
    assert summary["bands"] == [label for _limit, label in SESSION_BANDS]


def test_a_session_counts_on_the_day_it_began_and_listeners_carry_into_a_new_day(
    tmp_path: Path,
) -> None:
    clock = Clock(datetime(2026, 10, 14, 3, 30, tzinfo=UTC).timestamp())  # 23:30 local
    stats = _stats(tmp_path, clock)
    started = stats.connected("mp3")
    clock.at += HOUR  # 00:30 the next day
    stats.tick()
    stats.disconnected("mp3", started)
    days = {row["date"]: row for row in stats.summary(7, {})["days"]}
    assert days["2026-10-13"]["sessions"] == 1
    assert days["2026-10-13"]["seconds"] == HOUR
    # Still listening after midnight: the new day's peak is 1 too.
    assert days["2026-10-14"]["peak"] == 1 and days["2026-10-14"]["peak_at"] == "00:30"
    assert days["2026-10-14"]["sessions"] == 0


def test_days_without_listening_are_listed_with_zeros(tmp_path: Path) -> None:
    clock = Clock()
    stats = _stats(tmp_path, clock)
    stats.disconnected("mp3", stats.connected("mp3"))
    clock.at += 3 * DAY
    rows = stats.summary(30, {"mp3": 0})["days"]
    assert [row["date"] for row in rows] == [
        "2026-10-16",
        "2026-10-15",
        "2026-10-14",
        "2026-10-13",
    ]
    assert [row["sessions"] for row in rows] == [0, 0, 0, 1]
    # Nothing recorded in the period at all: no rows.
    assert stats.summary(1, {})["days"] == []


def test_saved_about_once_a_minute_and_read_back(tmp_path: Path) -> None:
    clock = Clock()
    stats = _stats(tmp_path, clock)
    stats.disconnected("aac", stats.connected("aac"))
    stats.tick()
    assert not stats.path.exists()
    clock.mono += 61
    stats.tick()
    saved = json.loads(stats.path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and saved["days"][0]["sessions"] == 1
    again = _stats(tmp_path, clock)
    assert again.summary(7, {})["days"][0]["formats"]["aac"]["sessions"] == 1


def test_never_writes_who_listened(tmp_path: Path) -> None:
    clock = Clock()
    stats = _stats(tmp_path, clock)
    for fmt in ("mp3", "aac"):
        started = stats.connected(fmt)
        clock.at += 125
        stats.disconnected(fmt, started)
    stats.flush()
    text = stats.path.read_text(encoding="utf-8") + stats.csv_text()
    # Only dates, a peak time of day, counts and lengths.
    for forbidden in ("192.", "Mozilla", "address", "player", "agent", str(int(MORNING))):
        assert forbidden not in text


def test_old_days_are_deleted_and_zero_keeps_none(tmp_path: Path) -> None:
    clock = Clock()
    keep = [3]
    stats = _stats(tmp_path, clock, keep)
    stats.disconnected("mp3", stats.connected("mp3"))
    clock.at += 2 * DAY
    stats.tick()
    assert [row["date"] for row in stats.summary(30, {})["days"]][-1] == "2026-10-13"
    clock.at += DAY
    stats.tick()
    assert stats.summary(30, {})["days"] == []
    stats.disconnected("mp3", stats.connected("mp3"))
    keep[0] = 0
    stats.tick()
    stats.flush()
    assert json.loads(stats.path.read_text(encoding="utf-8"))["days"] == []
    # While off, nothing is counted, and a late session from a kept day is ignored.
    stats.disconnected("mp3", stats.connected("mp3"))
    assert stats.summary(30, {})["days"] == []
    keep[0] = 2
    stats.disconnected("mp3", clock.at - 5 * DAY)
    assert stats.summary(30, {})["days"] == []


def test_an_unreadable_file_is_moved_aside(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "listener_stats.json"
    for bad in (
        "{not json",
        json.dumps({"days": [{"date": "2026-10-13"}]}),
        json.dumps({"days": "none"}),
        json.dumps(
            {
                "days": [
                    {
                        "date": "2026-10-13",
                        "peak": -1,
                        "peak_at": None,
                        "sessions": 0,
                        "seconds": 0,
                        "longest": 0,
                        "bands": [0] * 6,
                    }
                ]
            }
        ),
    ):
        path.write_text(bad, encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="autodj.listener_stats"):
            stats = _stats(tmp_path, Clock())
        assert stats.summary(7, {})["days"] == []
        assert not path.exists()
    assert len(list(tmp_path.glob("listener_stats.json.*.bad"))) == 4
    assert "Cannot load listener statistics" in caplog.text


def test_a_good_file_with_unknown_formats_loads_what_it_knows(tmp_path: Path) -> None:
    row = {
        "date": "2026-10-13",
        "peak": 2,
        "peak_at": "09:15",
        "sessions": 4,
        "seconds": 600,
        "longest": 300.5,
        "bands": [1, 2, 1, 0, 0, 0],
        "formats": {"mp3": {"peak": 2, "sessions": 4, "seconds": 600}, "flac": {"peak": 1}},
    }
    (tmp_path / "listener_stats.json").write_text(json.dumps({"days": [row]}), encoding="utf-8")
    [loaded] = _stats(tmp_path, Clock()).summary(7, {})["days"]
    assert loaded["peak_at"] == "09:15" and loaded["formats"] == {
        "mp3": {"peak": 2, "sessions": 4, "seconds": 600.0}
    }


def test_a_failed_save_is_logged_and_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    stats = _stats(tmp_path, clock)
    stats.disconnected("mp3", stats.connected("mp3"))

    def refuse(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("autodj.listener_stats.atomic_write", refuse)
    with caplog.at_level(logging.WARNING, logger="autodj.listener_stats"):
        stats.flush()
    assert "Could not save listener statistics" in caplog.text
    monkeypatch.undo()
    stats.flush()
    assert json.loads(stats.path.read_text(encoding="utf-8"))["days"][0]["sessions"] == 1
    # Nothing new: nothing is written.
    stats.path.unlink()
    stats.flush()
    assert not stats.path.exists()


def test_csv_has_one_row_per_kept_day_with_every_format(tmp_path: Path) -> None:
    clock = Clock()
    stats = _stats(tmp_path, clock)
    started = stats.connected("opus")
    clock.at += 90 * 60
    stats.disconnected("opus", started)
    rows = list(csv.DictReader(io.StringIO(stats.csv_text())))
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-10-13" and row["peak_listeners"] == "1"
    assert row["peak_time"] == "10:00" and row["sessions"] == "1"
    assert row["listening_hours"] == "1.50" and row["average_session_minutes"] == "90.0"
    assert row["longest_session_minutes"] == "90.0"
    assert row["opus_sessions"] == "1" and row["mp3_sessions"] == "0"
    assert row["sessions_1_to_3_hours"] == "1"
