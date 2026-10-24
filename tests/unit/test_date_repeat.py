"""Date ranges that repeat every week or every year (#470)."""

from __future__ import annotations

from datetime import date, timedelta

from autodj.date_repeat import contains, next_day, on_year, problem


def days_in(first: date, last: date | None, repeat: str, start: date, count: int) -> list[str]:
    """The days from *start* on, over *count* days, that the range holds."""
    found = []
    for offset in range(count):
        day = start + timedelta(days=offset)
        if contains(day, first, last, repeat):  # type: ignore[arg-type]
            found.append(day.isoformat())
    return found


def test_a_yearly_range_runs_past_new_year_in_every_year() -> None:
    """ADR-0009-09: month and day only; a range may wrap past New Year."""
    first, last = date(2026, 12, 20), date(2026, 1, 2)
    assert contains(date(2030, 12, 19), first, last, "yearly") is False
    assert contains(date(2030, 12, 20), first, last, "yearly")
    assert contains(date(2031, 1, 1), first, last, "yearly")
    assert contains(date(2031, 1, 2), first, last, "yearly")
    assert contains(date(2031, 1, 3), first, last, "yearly") is False
    # The years are ignored, so a year before the first date counts too.
    assert contains(date(2020, 12, 25), first, last, "yearly")
    assert next_day(date(2027, 1, 3), first, last, "yearly") == date(2027, 12, 20)
    assert next_day(date(2027, 12, 31), first, last, "yearly") == date(2027, 12, 31)
    # The same day, or no last date, is that one day each year.
    one = date(2026, 10, 31)
    assert days_in(one, one, "yearly", date(2027, 10, 1), 60) == ["2027-10-31"]
    assert days_in(one, None, "yearly", date(2027, 10, 1), 60) == ["2027-10-31"]
    assert next_day(date(2027, 11, 1), one, None, "yearly") == date(2028, 10, 31)


def test_29_february_is_28_february_in_a_common_year() -> None:
    """ADR-0009-09: the leap day rule."""
    leap = date(2028, 2, 29)
    assert on_year(leap, 2027) == date(2027, 2, 28)
    assert on_year(leap, 2032) == date(2032, 2, 29)
    assert days_in(leap, None, "yearly", date(2027, 2, 20), 20) == ["2027-02-28"]
    assert days_in(leap, None, "yearly", date(2032, 2, 20), 20) == ["2032-02-29"]
    assert next_day(date(2027, 3, 1), leap, None, "yearly") == date(2028, 2, 29)
    # As the end of a range it ends on 28 February in a common year.
    season = days_in(date(2026, 2, 27), leap, "yearly", date(2027, 2, 20), 20)
    assert season == ["2027-02-27", "2027-02-28"]


def test_a_weekly_window_runs_from_its_first_date_and_may_wrap_past_sunday() -> None:
    """ADR-0009-09: the two dates' weekdays set the window, from the first date on."""
    friday, sunday = date(2026, 11, 6), date(2026, 11, 8)
    assert days_in(friday, sunday, "weekly", date(2026, 11, 1), 16) == [
        "2026-11-06",
        "2026-11-07",
        "2026-11-08",
        "2026-11-13",
        "2026-11-14",
        "2026-11-15",
    ]
    # Saturday to Monday runs past the week's end.
    saturday, monday = date(2026, 11, 7), date(2026, 11, 9)
    assert days_in(saturday, monday, "weekly", date(2026, 11, 10), 7) == [
        "2026-11-14",
        "2026-11-15",
        "2026-11-16",
    ]
    assert next_day(date(2026, 11, 1), saturday, monday, "weekly") == saturday
    assert next_day(date(2026, 11, 10), saturday, monday, "weekly") == date(2026, 11, 14)
    assert next_day(date(2026, 11, 15), saturday, monday, "weekly") == date(2026, 11, 15)


def test_a_weekly_range_with_one_date_is_that_weekday() -> None:
    """ADR-0009-09: no last date, or the same date, is one weekday every week."""
    friday = date(2026, 11, 6)
    expected = ["2026-11-06", "2026-11-13", "2026-11-20"]
    assert days_in(friday, None, "weekly", date(2026, 10, 30), 22) == expected
    assert days_in(friday, friday, "weekly", date(2026, 10, 30), 22) == expected


def test_dates_that_do_not_repeat_keep_their_meaning() -> None:
    first, last = date(2026, 12, 1), date(2026, 12, 26)
    assert contains(date(2026, 12, 1), first, last, "none")
    assert contains(date(2027, 12, 1), first, last, "none") is False
    assert contains(date(2030, 1, 1), first, None, "none")
    assert contains(date(2020, 1, 1), None, last, "none")
    assert next_day(date(2026, 11, 1), first, last, "none") == first
    assert next_day(date(2026, 12, 27), first, last, "none") is None
    assert next_day(date(2026, 12, 27), None, None, "none") == date(2026, 12, 27)


def test_what_is_wrong_with_a_range() -> None:
    """ADR-0009-09: the checks behind the field errors."""
    first, last = date(2026, 11, 6), date(2026, 11, 12)
    assert problem(None, last, "weekly") == "needs_first"
    assert problem(None, None, "yearly") == "needs_first"
    assert problem(None, None, "none") is None
    assert problem(last, first, "none") == "backwards"
    assert problem(last, first, "weekly") == "backwards"
    assert problem(last, first, "yearly") is None  # past New Year
    assert problem(first, last, "weekly") is None  # Friday to Thursday, 7 days
    assert problem(first, last + timedelta(days=1), "weekly") == "too_long"
    assert problem(first, date(2027, 1, 1), "yearly") is None
