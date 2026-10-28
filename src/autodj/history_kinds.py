"""What each row of a station's play history aired as (#601).

Kept free of other imports, so the player bridge can use it on the
minimal install path.
"""

from __future__ import annotations

#: ``play_history.kind`` of audio that airs over or between the songs (#601):
#: a liner mixed into a handoff or by the liner schedule, an ad spot, a
#: matched intro, a time check, a cart, a voice track or an announcer break
#: (``dj``, ``news``, ``weather``, ``music_news``, or ``speech`` for any
#: other).  A song is ``song`` and a live DJ's set ``live``.  These rows are
#: never song plays: the repeat rules, last-played times and royalty reports
#: leave them out, and History shows them only when asked.
INSERT_KINDS = frozenset(
    {
        "liner",
        "ad",
        "matched_intro",
        "time_check",
        "cart",
        "voice_track",
        "dj",
        "news",
        "weather",
        "music_news",
        "speech",
    }
)

#: Inserted kinds that air on their own between songs rather than mixed
#: over one: a show's standalone speech segment.  Like the next song, one
#: ends the song before it in a report.
STANDALONE_KINDS = frozenset({"speech"})


def history_kind(row: dict) -> str:
    """What a history row aired as: ``song``, ``live``, or one of :data:`INSERT_KINDS`.

    A song row whose file is in a liner folder (``track_kind`` ``liner``)
    is a ``liner``: a clip the station played as a track.
    """
    kind = row.get("kind") or "song"
    if kind == "song" and row.get("track_kind") == "liner":
        return "liner"
    return kind


__all__ = ["INSERT_KINDS", "STANDALONE_KINDS", "history_kind"]
