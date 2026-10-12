"""The common part of the beets query language, understood by AutoDJ itself.

Beets is not installed in the container, so AutoDJ cannot hand a query to
it.  This module parses and evaluates the syntax people use most, against
the fields AutoDJ reads from the beets database (see
:func:`autodj.beets.read_items`) or, without beets, from its own library:

- ``field:value`` matches when the field contains *value*, ignoring case,
  as beets does.  On a number field (``year``, ``bpm`` and the other
  numeric columns beets defines) the value must be a number and match it
  exactly.
- ``field:a..b`` is a range of numbers, ends included; either end may be
  left open (``year:1990..`` or ``bpm:..100``).
- ``^`` (or ``-``) in front of a term negates it: ``^genre:christmas``.
- Quotes keep a phrase together: ``artist:"the beatles"`` or
  ``"love song"``.
- A bare word, with no field, matches artist, title, album, album artist,
  genre or comments.
- Terms separated by spaces must all match.

Anything else beets offers -- regular expressions (``field::re``), exact
matches (``field:=value``), alternatives (``a , b``), sorting
(``year+``), path queries and plugin queries such as fuzzy matching -- is
refused with a message that names the problem, rather than being read as
something it is not.

The first-release year has one extra rule (see :func:`first_release_year`):
``original_year`` falls back to ``year`` when beets has no first-release
year for a song, and a song with neither is left out of year filters.

Example:
    >>> query = parse_query('genre:rock original_year:1990..1999 ^artist:"bon jovi"')
    >>> query.matches({"genre": "Rock", "original_year": 1994, "artist": "Pearl Jam"})
    True
"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The longest query accepted, in characters.
MAX_QUERY_LENGTH = 500
#: The most terms one query may have.
MAX_TERMS = 30

#: Fields a bare word searches, as beets searches items.
DEFAULT_FIELDS = ("artist", "title", "album", "albumartist", "genre", "comments")

#: The first-release year, with its fallback to ``year``.
FIRST_RELEASE_YEAR = "original_year"

#: Beets' numeric item columns: ``field:value`` compares them as numbers.
NUMERIC_FIELDS = frozenset(
    {
        "year",
        "month",
        "day",
        "original_year",
        "original_month",
        "original_day",
        "track",
        "tracktotal",
        "disc",
        "disctotal",
        "bpm",
        "length",
        "bitrate",
        "bitrate_mode",
        "samplerate",
        "bitdepth",
        "channels",
        "comp",
        "mtime",
        "added",
        "rg_track_gain",
        "rg_track_peak",
        "rg_album_gain",
        "rg_album_peak",
        "r128_track_gain",
        "r128_album_gain",
    }
)

_FIELD = re.compile(r"[a-z_][a-z0-9_]*\Z")
_SORT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*[+-]\Z")


class QueryError(ValueError):
    """A query AutoDJ cannot run, with a message that says why in words."""


def number(value: Any) -> float | None:
    """*value* as a finite number, or ``None`` when it is not one."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        result = float(value)
    elif isinstance(value, str | bytes):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        try:
            result = float(text.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _year(value: Any) -> int | None:
    """A positive year from a beets or library value; 0 and blanks mean unknown."""
    found = number(value)
    if found is None or found <= 0:
        return None
    return int(found)


def first_release_year(item: Mapping[str, Any]) -> tuple[int | None, bool]:
    """Return the song's first-release year and whether it fell back to ``year``.

    Beets' ``original_year`` is the year the song first came out; ``year``
    is the year of this release, so a 1985 song on a 2005 compilation has
    ``year`` 2005.  When ``original_year`` is missing (beets stores 0), the
    release year stands in; when both are missing the year is ``None`` and
    the song is left out of every year filter.
    """
    original = _year(item.get(FIRST_RELEASE_YEAR))
    if original is not None:
        return original, False
    release = _year(item.get("year"))
    if release is not None:
        return release, True
    return None, False


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _value(item: Mapping[str, Any], name: str) -> Any:
    """The field's value, or ``None`` when the song does not have it."""
    if name == FIRST_RELEASE_YEAR:
        return first_release_year(item)[0]
    return item.get(name)


@dataclass(frozen=True)
class Term:
    """One part of a query.

    Attributes:
        field: The field it reads, or ``None`` for a bare word.
        text: The text to find, casefolded (not used by a range).
        low: The range's lower end, or ``None`` when open.
        high: The range's upper end, or ``None`` when open.
        is_range: Whether this is a ``a..b`` range.
        negate: Whether the term was written with ``^`` (or ``-``).
        source: The term as written, for messages.
    """

    field: str | None
    text: str = ""
    low: float | None = None
    high: float | None = None
    is_range: bool = False
    negate: bool = False
    source: str = ""

    def _positive(self, item: Mapping[str, Any]) -> bool:
        if self.field is None:
            return any(
                (value := item.get(name)) is not None and self.text in _text(value).casefold()
                for name in DEFAULT_FIELDS
            )
        value = _value(item, self.field)
        if value is None:
            return False
        if self.is_range:
            found = number(value)
            if found is None:
                return False
            return (self.low is None or found >= self.low) and (
                self.high is None or found <= self.high
            )
        if self.field in NUMERIC_FIELDS:
            wanted, found = number(self.text), number(value)
            return wanted is not None and found is not None and wanted == found
        return self.text in _text(value).casefold()

    def matches(self, item: Mapping[str, Any]) -> bool:
        """Whether *item* (a mapping of field names to values) passes this term."""
        return self._positive(item) != self.negate


@dataclass(frozen=True)
class Query:
    """A parsed query: every term must match.  An empty query matches every song."""

    terms: tuple[Term, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.terms)

    def named_fields(self) -> frozenset[str]:
        """The fields the query names, as written."""
        return frozenset(term.field for term in self.terms if term.field is not None)

    def fields(self) -> frozenset[str]:
        """Every field evaluating the query reads (bare words read several)."""
        names = set(self.named_fields())
        if any(term.field is None for term in self.terms):
            names.update(DEFAULT_FIELDS)
        if FIRST_RELEASE_YEAR in names:
            names.add("year")
        return frozenset(names)

    def uses_first_release_year(self) -> bool:
        """Whether a term reads ``original_year`` (and so may use its fallback)."""
        return FIRST_RELEASE_YEAR in self.named_fields()

    def matches(self, item: Mapping[str, Any]) -> bool:
        """Whether *item* passes every term."""
        return all(term.matches(item) for term in self.terms)


def _range(name: str, value: str, written: str, negate: bool) -> Term:
    low_text, _, high_text = value.partition("..")
    if ".." in high_text:
        raise QueryError(f"\"{written}\": a range has one '..', such as {name}:1990..1999")
    ends: list[float | None] = []
    for end in (low_text, high_text):
        if not end.strip():
            ends.append(None)
            continue
        found = number(end)
        if found is None:
            raise QueryError(
                f'"{written}": {end} is not a number. Ranges compare numbers, such as '
                f"{name}:1990..1999; dates are not supported"
            )
        ends.append(found)
    low, high = ends
    if low is None and high is None:
        raise QueryError(f'"{written}": give at least one end of the range, such as {name}:1990..')
    if low is not None and high is not None and low > high:
        raise QueryError(f'"{written}": the range starts after it ends')
    return Term(name, low=low, high=high, is_range=True, negate=negate, source=written)


def _term(token: str) -> Term:
    written = token
    negate = token[:1] in ("^", "-") and len(token) > 1
    if token in ("^", "-"):
        raise QueryError(f"'{token}' needs a term after it, such as ^genre:christmas")
    if negate:
        token = token[1:]
    if _SORT.fullmatch(token) and ":" not in token:
        raise QueryError(
            f'"{written}": sorting is not supported; a show picks songs in its own order'
        )
    name, colon, value = token.partition(":")
    if not colon:
        if token.startswith("::"):
            raise QueryError(f'"{written}": regular expressions are not supported')
        return Term(None, text=token.casefold(), negate=negate, source=written)
    if not name:
        if value.startswith(":"):
            raise QueryError(f'"{written}": regular expressions are not supported')
        raise QueryError(f'"{written}": put a field name before the colon, such as genre:rock')
    field = name.casefold()
    if not _FIELD.fullmatch(field):
        raise QueryError(
            f'"{written}": {name} is not a field name. Field names use letters, digits and '
            "underscores; put quotes around text with a colon in it"
        )
    if value.startswith(":"):
        raise QueryError(
            f'"{written}": regular expressions (field::pattern) are not supported; '
            f"{field}:text matches part of the text"
        )
    if value.startswith("="):
        raise QueryError(
            f'"{written}": exact matches (field:=value) are not supported; '
            f"{field}:text matches part of the text"
        )
    if value.startswith("~"):
        raise QueryError(
            f'"{written}": fuzzy matching comes from a beets plugin and is not supported'
        )
    if field == "path":
        raise QueryError(f'"{written}": path queries are not supported; use the song\'s fields')
    if not value:
        raise QueryError(f'"{written}": enter a value after the colon, such as {field}:rock')
    if ".." in value:
        return _range(field, value, written, negate)
    if field in NUMERIC_FIELDS and number(value) is None:
        raise QueryError(
            f'"{written}": {field} is a number, such as {field}:1999, or a range, such as '
            f"{field}:1990..1999"
        )
    return Term(field, text=value.casefold(), negate=negate, source=written)


def parse_query(text: str) -> Query:
    """Parse *text* into a :class:`Query`.

    Raises:
        QueryError: The query is too long or uses syntax AutoDJ does not
            run; the message names the term and the problem.
    """
    if not isinstance(text, str):
        raise QueryError("The query must be text")
    if len(text) > MAX_QUERY_LENGTH:
        raise QueryError(f"The query is longer than {MAX_QUERY_LENGTH} characters")
    if not text.strip():
        return Query()
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    try:
        tokens = list(lexer)
    except ValueError:
        raise QueryError("A quotation mark is not closed") from None
    if len(tokens) > MAX_TERMS:
        raise QueryError(f"The query has more than {MAX_TERMS} terms")
    terms: list[Term] = []
    for token in tokens:
        if token.endswith(","):
            raise QueryError(
                "Alternatives separated by a comma are not supported; every term must match. "
                "Use a separate show, or a genre list in the form, for alternatives"
            )
        if token:
            terms.append(_term(token))
    return Query(tuple(terms))
