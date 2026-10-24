"""Validation and evaluation for clockwheel metadata conditions."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from fnmatch import fnmatchcase
from typing import Any

from autodj.beets_query import first_release_year, number

_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_TEXT = {"artist", "title", "album"}
_NUMERIC = {"year", "bpm", "duration"}
_FIELDS = _TEXT | _NUMERIC | {"genre", "beets"}
_OPS = {"eq", "lt", "lte", "gt", "gte", "between", "not_eq", "outside"}
_MAX_VALUE = 500


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
) -> Callable[[Any, Mapping[str, Any] | None], bool]:
    """Validate a nonfolder condition once and return a test for each entry.

    The test takes a library entry and its Beets values (or ``None``).
    """
    c = validate_metadata_condition(condition)
    field, op, wanted = c["field"], c["operator"], c["value"]
    name = c["name"] if field == "beets" else field
    if field == "genre":
        folded = wanted.casefold()

        def genre_matches(entry: Any, beets_item: Mapping[str, Any] | None = None) -> bool:
            found = genres(_get(entry, "genre"))
            return bool(found) and (
                any(g.casefold() == folded for g in found)
                if op == "is"
                else all(g.casefold() != folded for g in found)
            )

        return genre_matches
    numeric = op in {"eq", "lt", "lte", "gt", "gte", "between", "not_eq", "outside"}
    no_zero = field in {"year", "bpm", "duration"} or (
        field == "beets" and name in {"year", "bpm", "length", "original_year"}
    )
    upper: Any = c.get("upper")
    folded = wanted.casefold() if isinstance(wanted, str) else ""
    pattern = folded.replace("[", "[[]")

    def matches(entry: Any, beets_item: Mapping[str, Any] | None = None) -> bool:
        if field == "beets":
            raw = (
                None
                if beets_item is None
                else (
                    first_release_year(beets_item)[0]
                    if name == "original_year"
                    else beets_item.get(name)
                )
            )
        elif field == "duration":
            raw = _get(entry, "length")
        else:
            raw = _get(entry, field)
        if raw is None or raw == "":
            return False
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

    return matches
