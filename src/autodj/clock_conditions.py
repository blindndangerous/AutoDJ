"""Validation and evaluation for clockwheel metadata conditions."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Any

from autodj.beets_query import first_release_year, number

_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_TEXT = {"artist", "title", "album"}
_NUMERIC = {"year", "bpm", "duration"}
_FIELDS = _TEXT | _NUMERIC | {"genre", "beets"}
_OPS = {"eq", "lt", "lte", "gt", "gte", "between", "not_eq", "outside"}
_MAX_VALUE = 500
#: The relative date operator: a Beets date (a Unix time, such as ``added``
#: or ``mtime``) within the last N days.
WITHIN_DAYS = "within_days"
#: The longest window it takes, in days (about ten years).
MAX_WITHIN_DAYS = 3650
#: How often its window moves, in seconds: the window ends at the start of
#: the current hour, so the songs it matches stay the same for an hour and
#: cached step and category results are worked out again once an hour.
RELATIVE_DATE_STEP = 3600
_DAY = 86400
WITHIN_DAYS_ERROR = (
    f"within the last N days needs a whole number of days from 1 to {MAX_WITHIN_DAYS}"
)


def relative_date_now(now: float | None = None) -> int:
    """The time relative date conditions are judged at: the start of *now*'s hour.

    *now* is wall-clock seconds, the current time when not given.
    """
    stamp = time.time() if now is None else now
    return int(stamp // RELATIVE_DATE_STEP) * RELATIVE_DATE_STEP


def uses_relative_dates(conditions: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any of *conditions* is judged against the current time."""
    return any(condition.get("operator") == WITHIN_DAYS for condition in conditions)


def genres(value: Any) -> list[str]:
    """Return distinct, nonempty genre labels, preserving embedded commas/slashes."""
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        for label in re.split(r"[;\x00]", item):
            label = label.strip()
            if label and label.casefold() not in {x.casefold() for x in result}:
                result.append(label)
    return result


def genre_text(value: Any) -> str:
    """A genre field as the Tags form shows it, each value apart by "; ".

    A file's several values are stored apart by null characters, which a
    screen reader skips and an edit refuses.  A semicolon reads, and
    :func:`genres` splits it back into the same values when it is saved.
    """
    return "; ".join(genres(value))


def validate_category_condition(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a reference to a saved music category."""
    category = value.get("category")
    if (
        set(value) not in ({"field", "category"}, {"field", "category", "operator"})
        or value.get("field") != "category"
        or value.get("operator", "is") not in {"is", "is_not"}
        or not isinstance(category, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", category)
    ):
        raise ValueError("A category condition must name a saved category by its ID")
    return {
        "field": "category",
        "category": category,
        **({"operator": "is_not"} if value.get("operator") == "is_not" else {}),
    }


def validate_metadata_condition(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a single metadata condition."""
    if not isinstance(value, Mapping):
        raise ValueError("condition must be an object")
    if set(value) - {"field", "name", "operator", "value", "upper"}:
        raise ValueError("condition contains unsupported keys")
    field, op, wanted = value.get("field"), value.get("operator"), value.get("value")
    if not isinstance(op, str):
        raise ValueError("operator must be text")
    if not isinstance(field, str) or field not in _FIELDS:
        raise ValueError("field must be a supported metadata field")
    normalized = {"field": field, "operator": op, "value": wanted}
    if field == "beets":
        name = value.get("name")
        if (
            not isinstance(name, str)
            or len(name) > 64
            or not _NAME.fullmatch(name)
            or name in {"id", "path"}
        ):
            raise ValueError("Beets name must be a valid metadata field name")
        normalized["name"] = name
    else:
        if "name" in value:
            raise ValueError("name is only valid for Beets fields")
    if field == "genre":
        if (
            op not in {"is", "is_not"}
            or not isinstance(wanted, str)
            or not wanted.strip()
            or len(wanted) > _MAX_VALUE
            or "upper" in value
        ):
            raise ValueError("genre requires operator 'is' and one nonempty string")
        normalized["value"] = wanted.strip()
        return normalized
    if op == WITHIN_DAYS:
        if field != "beets":
            raise ValueError(
                "within the last N days is only for Beets date fields, such as added or mtime"
            )
        if "upper" in value:
            raise ValueError("within the last N days takes one number of days, not an upper value")
        days = (
            wanted
            if isinstance(wanted, int) and not isinstance(wanted, bool)
            else int(wanted)
            if isinstance(wanted, float) and wanted.is_integer()
            else None
        )
        if days is None or not 1 <= days <= MAX_WITHIN_DAYS:
            raise ValueError(WITHIN_DAYS_ERROR)
        normalized["value"] = days
        return normalized
    if field in _TEXT or (
        field == "beets"
        and op in {"is", "is_not", "contains", "matches", "not_contains", "not_matches"}
    ):
        if (
            op not in {"is", "is_not", "contains", "matches", "not_contains", "not_matches"}
            or not isinstance(wanted, str)
            or not wanted.strip()
            or len(wanted) > _MAX_VALUE
            or "upper" in value
        ):
            raise ValueError(
                "text fields require is, is not, contains, or matches and a nonempty string"
            )
        normalized["value"] = wanted
        return normalized
    if op not in _OPS:
        raise ValueError("numeric fields require a supported numeric operator")
    low = (
        number(wanted) if isinstance(wanted, int | float) and not isinstance(wanted, bool) else None
    )
    if low is None or (field != "beets" and low < 0):
        raise ValueError("numeric value must be finite and nonnegative")
    if (
        field == "year" or (field == "beets" and name in {"year", "original_year"})
    ) and not low.is_integer():
        raise ValueError("year must be an integer")
    if op in {"between", "outside"}:
        high_value = value.get("upper")
        high = (
            number(high_value)
            if isinstance(high_value, int | float) and not isinstance(high_value, bool)
            else None
        )
        if high is None or (field != "beets" and high < 0) or high < low:
            raise ValueError("range requires a valid upper bound at or above value")
        if (
            field == "year" or (field == "beets" and name in {"year", "original_year"})
        ) and not high.is_integer():
            raise ValueError("year must be an integer")
        normalized.update(value=low, upper=high)
        return normalized
    if "upper" in value:
        raise ValueError("upper is only valid with between")
    normalized["value"] = low
    return normalized


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _beets_text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str | int | float | bool):
        return str(value)
    return None


def condition_matcher(
    condition: Mapping[str, Any],
) -> Callable[..., bool]:
    """Validate a nonfolder condition once and return a test for each entry.

    The test takes a library entry, its Beets values (or ``None``) and,
    optionally, the wall-clock time a relative date condition is judged at
    (see :func:`relative_date_now`; the current time when not given).
    """
    value_of, judge = condition_value_test(condition)

    def matches(
        entry: Any, beets_item: Mapping[str, Any] | None = None, now: float | None = None
    ) -> bool:
        return judge(value_of(entry, beets_item), now)

    return matches


def condition_value_test(
    condition: Mapping[str, Any],
) -> tuple[Callable[[Any, Mapping[str, Any] | None], Any], Callable[[Any, float | None], bool]]:
    """A nonfolder condition's test in two parts, for matching a whole library.

    The first reads the one value the condition looks at from an entry and
    its Beets values; the second judges that value at a time, as
    :func:`condition_matcher` judges the entry.  Songs sharing a value are
    judged alike, so a caller may judge each value once.  A genre condition
    looks at every genre of a song: its value is the genre field and its
    judge splits it.
    """
    c = validate_metadata_condition(condition)
    if c["field"] == "genre":
        folded = c["value"].casefold()
        op = c["operator"]

        def genre_test(raw: Any, now: float | None = None) -> bool:
            found = genres(raw)
            return bool(found) and (
                any(g.casefold() == folded for g in found)
                if op == "is"
                else all(g.casefold() != folded for g in found)
            )

        return (lambda entry, _beets_item: _get(entry, "genre")), genre_test
    return _value_test(c)


def _value_test(
    c: Mapping[str, Any],
) -> tuple[Callable[[Any, Mapping[str, Any] | None], Any], Callable[[Any, float | None], bool]]:
    """The value reader and judge of a validated condition other than genre."""
    field, op, wanted = c["field"], c["operator"], c["value"]
    name = c["name"] if field == "beets" else field
    numeric = op in {"eq", "lt", "lte", "gt", "gte", "between", "not_eq", "outside"}
    no_zero = field in {"year", "bpm", "duration"} or (
        field == "beets" and name in {"year", "bpm", "length", "original_year"}
    )
    upper: Any = c.get("upper")
    folded = wanted.casefold() if isinstance(wanted, str) else ""
    pattern = folded.replace("[", "[[]")

    def value_of(entry: Any, beets_item: Mapping[str, Any] | None = None) -> Any:
        if field == "beets":
            return (
                None
                if beets_item is None
                else (
                    first_release_year(beets_item)[0]
                    if name == "original_year"
                    else beets_item.get(name)
                )
            )
        if field == "duration":
            return _get(entry, "length")
        return _get(entry, field)

    def judge(raw: Any, now: float | None = None) -> bool:
        if raw is None or raw == "":
            return False
        if op == WITHIN_DAYS:
            # Beets stores dates as Unix times; 0 means it has none.  A date
            # after the window's end (the start of this hour) is recent too.
            found = number(raw)
            return (
                found is not None and found > 0 and found >= relative_date_now(now) - wanted * _DAY
            )
        if numeric:
            found = number(raw)
            if found is None or (field != "beets" and found < 0) or (no_zero and found == 0):
                return False
            if op in {"eq", "not_eq"}:
                return (found == wanted) if op == "eq" else (found != wanted)
            if op == "lt":
                return found < wanted
            if op == "lte":
                return found <= wanted
            if op == "gt":
                return found > wanted
            if op == "gte":
                return found >= wanted
            return (
                (wanted <= found <= upper) if op == "between" else (found < wanted or found > upper)
            )
        if field == "beets":
            raw = _beets_text(raw)
        if not isinstance(raw, str) or not raw.strip():
            return False
        if op in {"is", "is_not"}:
            equal = raw.casefold() == folded
            return not equal if op == "is_not" else equal
        if op in {"contains", "not_contains"}:
            # A substring of the text, also for a numeric Beets column read as text.
            contains = folded in raw.casefold()
            return not contains if op == "not_contains" else contains
        matched = fnmatchcase(raw.casefold(), pattern)
        return not matched if op == "not_matches" else matched

    return value_of, judge
