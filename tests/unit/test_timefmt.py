from datetime import time

from autodj import timefmt


def test_clock_text_follows_the_time_format(monkeypatch) -> None:
    assert timefmt.clock_text(time(15, 4, 30), seconds=True) == "15:04:30"
    monkeypatch.setattr(timefmt, "time_format", "12")
    assert timefmt.clock_text(time(15, 4)) == "3:04 PM"
    assert timefmt.clock_text(time(0, 5, 9), seconds=True) == "12:05:09 AM"
    assert timefmt.clock_text(time(12, 0)) == "12:00 PM"
