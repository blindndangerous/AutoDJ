"""One page of a station's History, with or without liners (#601).

History keeps two lists, oldest first: songs and live sets, and the audio
aired over or between them (liners, ads, matched intros and the rest, see
:data:`~autodj.history_kinds.INSERT_KINDS`).  Each has its own room, so a
busy hour of liners never pushes songs out of the songs-only view.  A row
kept in memory carries ``_seq``, its place in airing order, which merges
the two lists back into one.

Pages are worked out by walking the lists newest first and keeping
references; only the rows on the page asked for are copied.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from heapq import merge
from itertools import islice

from autodj.history_kinds import history_kind

#: Song and live rows kept, and the most rows either view shows.
VIEW_ROWS = 500
#: Inserted rows kept beside them.
AIRED_ROWS = 1500
_KEYS = ("event_id", "path", "title", "artist", "duration", "played_at")


def _order(row: dict) -> int:
    return row.get("_seq", 0)


def newest_rows(songs: Sequence[dict], aired: Sequence[dict], liners: bool) -> Iterator[dict]:
    """The rows a view shows, newest first: songs and live sets, plus with *liners* the rest."""
    if not liners or not aired:
        return reversed(songs)
    return merge(reversed(songs), reversed(aired), key=_order, reverse=True)


def public_row(row: dict) -> dict:
    """What the page is told about a row: never its listener count or library ID."""
    item = {key: row.get(key) for key in _KEYS}
    item["kind"] = history_kind(row)
    if row.get("kind") == "live":
        item["ended_at"] = row.get("ended_at")
    return item


def history_page(
    songs: Sequence[dict],
    aired: Sequence[dict],
    *,
    page: int,
    per_page: int,
    liners: bool,
    anchor: str | None = None,
) -> dict:
    """One page of History, newest first, as ``/api/history`` returns it.

    With *anchor*, a row's ``event_id``, the page is the one holding that
    row, or the next older row this view shows: hiding liners from a page
    that starts with one lands on the song before it.  An anchor no longer
    kept leaves *page* as asked; one older than the whole view gives its
    last page.
    """
    newest = list(islice(newest_rows(songs, aired, liners), VIEW_ROWS))
    total = len(newest)
    pages = max(1, (total + per_page - 1) // per_page)
    if anchor is not None:
        place = _anchor_place(songs, aired, newest, anchor)
        if place is not None:
            page = place // per_page + 1
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    return {
        "items": [public_row(row) for row in newest[start : start + per_page]],
        "total": total,
        "page": page,
        "pages": pages,
    }


def _anchor_place(
    songs: Sequence[dict], aired: Sequence[dict], newest: list[dict], anchor: str
) -> int | None:
    for place, row in enumerate(newest):
        if row.get("event_id") == anchor:
            return place
    target = _find(anchor, songs, aired)
    if target is None:
        return None
    order = _order(target)
    for place, row in enumerate(newest):
        if _order(row) < order:
            return place
    return max(0, len(newest) - 1)


def _find(anchor: str, *lists: Iterable[dict]) -> dict | None:
    for rows in lists:
        for row in rows:
            if row.get("event_id") == anchor:
                return row
    return None


__all__ = ["AIRED_ROWS", "VIEW_ROWS", "history_page", "newest_rows", "public_row"]
