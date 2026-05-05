"""Genre normaliser — collapse free-text genre strings into canonical buckets.

Music libraries (beets, MusicBrainz, Discogs, last.fm tags, hand-edited
ID3s) all spell genres differently:

- "Electronic / EDM / IDM" / "electronic" / "Elektronisch"
- "Hip-Hop" / "Hip Hop" / "Rap" / "HipHop"
- "Trip-Hop" / "Trip Hop" / "TripHop"
- "Indie Rock" / "Alt Rock" / "Alternative" / "Alternative Rock"

When AutoDJ matches a preset's `genres = [...]` filter against an
entry's free-text genre, exact-string comparison fails on every variant.
This module provides :func:`normalise` to fold a raw genre string into
a single canonical token + :func:`canonicalise_list` to do the same for
preset configuration values.

The mapping is *opinionated and non-exhaustive* — adding a new entry
just means appending an alias to ``_ALIASES``.  The mapping is
deliberately simple substring matching rather than ML/embedding-based
classification, so behaviour is predictable and debuggable.

Example:
    >>> from autodj.genres import normalise
    >>> normalise("Electronic / EDM / Trance")
    'electronic'
    >>> normalise("Hip Hop")
    'hip-hop'
    >>> normalise("Alternative Rock")
    'rock'
    >>> normalise("")
    ''
"""

from __future__ import annotations

# Canonical token → list of substrings that map to it.  Each substring
# is matched case-insensitively against the raw genre.  First match
# wins, so order from most-specific to least-specific.
_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("trip-hop", ("trip-hop", "trip hop", "triphop")),
    ("hip-hop", ("hip-hop", "hip hop", "hiphop", "rap", "trap")),
    ("drum-and-bass", ("drum and bass", "drum & bass", "drum-and-bass", "dnb", "d&b", "jungle")),
    ("house", ("house", "deep house", "tech house", "future house", "garage")),
    ("techno", ("techno", "minimal techno", "industrial techno")),
    (
        "electronic",
        (
            "electronic",
            "edm",
            "idm",
            "electronica",
            "synthwave",
            "synth-wave",
            "ambient",
            "downtempo",
            "chillwave",
            "chillout",
            "trance",
            "dubstep",
            "drum and bass",
        ),
    ),
    ("metal", ("metal", "death metal", "black metal", "doom", "metalcore", "deathcore", "thrash")),
    ("punk", ("punk", "hardcore punk", "post-punk", "ska-punk")),
    (
        "rock",
        (
            "rock",
            "indie rock",
            "alt rock",
            "alternative",
            "alternative rock",
            "post-rock",
            "prog rock",
            "progressive rock",
            "garage rock",
            "psychedelic rock",
            "classic rock",
            "soft rock",
            "art rock",
        ),
    ),
    ("pop", ("pop", "synth-pop", "synthpop", "indie pop", "electropop", "k-pop", "j-pop")),
    ("r-n-b", ("r&b", "rnb", "r-n-b", "rhythm and blues", "soul", "neo-soul", "funk")),
    ("jazz", ("jazz", "bebop", "swing", "fusion", "smooth jazz", "free jazz")),
    ("classical", ("classical", "baroque", "romantic", "orchestral", "chamber", "opera")),
    ("country", ("country", "americana", "bluegrass", "folk-country", "country rock")),
    ("folk", ("folk", "indie folk", "folk rock", "acoustic")),
    ("blues", ("blues", "delta blues", "electric blues")),
    ("reggae", ("reggae", "dub", "dancehall", "ska")),
    ("world", ("world", "afrobeat", "latin", "samba", "bossa nova", "flamenco")),
    ("soundtrack", ("soundtrack", "score", "film score", "ost", "video game")),
]


def normalise(genre: str | None) -> str:
    """Return the canonical genre token for *genre*, or ``""`` if unknown.

    Splits the input on common multi-genre separators (``/``, ``;``, ``,``)
    and returns the canonical of the first token that matches an alias.
    Empty / unknown inputs return ``""`` (caller can treat as "no genre"
    rather than a sentinel string).

    Args:
        genre: Raw genre string from beets / ID3 / file tags.  ``None``
            and ``""`` are treated as "no genre".

    Returns:
        Canonical token (``"electronic"``, ``"hip-hop"``, …) or ``""``.

    Example:
        >>> normalise("Electronic / Trance")
        'electronic'
        >>> normalise("Alternative Rock")
        'rock'
        >>> normalise("Punk Rock")
        'punk'
        >>> normalise(None)
        ''
    """
    if not genre:
        return ""
    # Look at every slash/comma/semicolon-separated token, take the first
    # one that maps to a canonical bucket.
    for chunk in _split_chunks(genre):
        canon = _match_chunk(chunk)
        if canon:
            return canon
    return ""


def canonicalise_list(genres: list[str] | None) -> list[str]:
    """Normalise every entry of *genres* and de-duplicate (preserves order).

    Use on preset configuration values like ``genres = ["Electronic",
    "House"]`` so the matcher compares canonical tokens.  Empty entries
    are dropped.

    Args:
        genres: List of raw genre strings.  ``None`` returns ``[]``.

    Returns:
        List of canonical tokens, de-duplicated.
    """
    if not genres:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for g in genres:
        canon = normalise(g)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def matches(entry_genre: str | None, allowed: list[str]) -> bool:
    """Return True if *entry_genre* canonicalises to anything in *allowed*.

    *allowed* should already be canonical (call
    :func:`canonicalise_list` on raw user input first).  Empty *allowed*
    means "no filter" → always matches.

    Args:
        entry_genre: Raw genre on the candidate track.
        allowed: List of canonical tokens to allow.  Empty = no filter.

    Returns:
        ``True`` if *entry_genre* normalises to something in *allowed*,
        or *allowed* is empty.

    Example:
        >>> matches("Electronic / Trance", ["electronic"])
        True
        >>> matches("Indie Rock", ["rock"])
        True
        >>> matches("Jazz", ["rock", "metal"])
        False
        >>> matches("anything", [])   # empty filter
        True
    """
    if not allowed:
        return True
    canon = normalise(entry_genre)
    return canon in allowed if canon else False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _split_chunks(genre: str) -> list[str]:
    """Split *genre* on common multi-genre separators."""
    out = [genre]
    for sep in ("/", ";", ",", "|"):
        out = [p for chunk in out for p in chunk.split(sep)]
    return [p.strip() for p in out if p.strip()]


def _match_chunk(chunk: str) -> str:
    """Return the canonical bucket for a single trimmed genre chunk."""
    low = chunk.lower()
    for canon, aliases in _ALIASES:
        for alias in aliases:
            if alias in low:
                return canon
    return ""
