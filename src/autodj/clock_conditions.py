"""Validation and evaluation for clockwheel metadata conditions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any

from autodj.beets_query import Term, first_release_year, number

_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_TEXT = {"artist", "title", "album"}
_NUMERIC = {"year", "bpm", "duration"}
_FIELDS = _TEXT | _NUMERIC | {"genre", "beets"}
_OPS = {"eq", "lt", "lte", "gt", "gte", "between"}
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
            op != "is"
            or not isinstance(wanted, str)
            or not wanted.strip()
            or len(wanted) > _MAX_VALUE
            or "upper" in value
        ):
            raise ValueError("genre requires operator 'is' and one nonempty string")
        normalized["value"] = wanted.strip()
        return normalized
    if field in _TEXT or (field == "beets" and op in {"is", "contains", "matches"}):
        if (
            op not in {"is", "contains", "matches"}
            or not isinstance(wanted, str)
            or not wanted.strip()
            or len(wanted) > _MAX_VALUE
            or "upper" in value
        ):
            raise ValueError("text fields require is, contains, or matches and a nonempty string")
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
    if op == "between":
        high_value = value.get("upper")
        high = (
            number(high_value)
            if isinstance(high_value, int | float) and not isinstance(high_value, bool)
            else None
        )
        if high is None or (field != "beets" and high < 0) or high < low:
            raise ValueError("between requires a valid upper bound at or above value")
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


def matches_condition(
    condition: Mapping[str, Any], entry: Any, beets_item: Mapping[str, Any] | None = None
) -> bool:
    """Match a validated nonfolder condition against a library entry."""
    c = validate_metadata_condition(condition)
    field, op, wanted = c["field"], c["operator"], c["value"]
    name = c["name"] if field == "beets" else field
    if field == "genre":
        return any(g.casefold() == wanted.casefold() for g in genres(_get(entry, "genre")))
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
    elif field in {"year", "bpm"} or field in _TEXT:
        raw = _get(entry, field)
    else:
        raw = None
    if raw is None or raw == "":
        return False
    if op in {"eq", "lt", "lte", "gt", "gte", "between"}:
        found = number(raw)
        if (
            found is None
            or (field != "beets" and found < 0)
            or (
                (
                    field in {"year", "bpm", "duration"}
                    or (field == "beets" and name in {"year", "bpm", "length", "original_year"})
                )
                and found == 0
            )
        ):
            return False
        if op == "eq":
            return found == wanted
        if op == "lt":
            return found < wanted
        if op == "lte":
            return found <= wanted
        if op == "gt":
            return found > wanted
        if op == "gte":
            return found >= wanted
        return Term(field, low=wanted, high=c["upper"], is_range=True).matches({field: found})
    if field == "beets":
        raw = _beets_text(raw)
    if not isinstance(raw, str) or not raw.strip():
        return False
    if op == "is":
        return raw.casefold() == wanted.casefold()
    if op == "contains":
        # A substring of the text, also for a numeric Beets column read as text.
        return wanted.casefold() in raw.casefold()
    return fnmatchcase(raw.casefold(), wanted.casefold().replace("[", "[[]"))
