"""Shared word matching used by Browse and History."""

from __future__ import annotations

from collections.abc import Iterable


def search_tokens(query: str) -> tuple[str, ...]:
    """Return case-folded words from *query*, as Browse interprets them."""
    return tuple(query.casefold().split())


def matches_search_text(tokens: Iterable[str], text: str) -> bool:
    """Whether every token occurs as a substring of *text*, ignoring case."""
    normalized = text.casefold()
    return all(token in normalized for token in tokens)


def matches_search_fields(query: str, *fields: object) -> bool:
    """Apply Browse-style AND substring matching across text fields."""
    tokens = search_tokens(query)
    if not tokens:
        return True
    return matches_search_text(tokens, " ".join(str(value or "") for value in fields))
