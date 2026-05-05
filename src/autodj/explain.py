"""Plain-English "why this track was picked" reasoning.

Given the previous track and the freshly-picked one, build a small list of
sentences explaining the choice — shared genres, BPM relationship, key
compatibility (Camelot wheel), energy delta, and the picker mode (similarity
/ pure shuffle / anchored / discovery / queue).

Inspired by the Music Genome Project's "Why this song?" surface — but
working from the metadata we already store in the FAISS index, no separate
human-curated genome required.

Example:
    >>> from autodj.explain import explain_pick
    >>> reasons = explain_pick(prev, current, mode="similarity")
    >>> for r in reasons:
    ...     print(r)
    Same genre — Trip-Hop.
    BPM holds steady at 92.
    Camelot key 8A → 9A: one step around the wheel.
    Energy lifts a touch (0.41 → 0.48).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shared_genres(a: str | None, b: str | None) -> list[str]:
    """Return the genre tokens that appear in both *a* and *b* (case-insensitive)."""
    if not a or not b:
        return []
    a_set = {tok.strip().lower() for tok in a.replace(";", ",").split(",") if tok.strip()}
    b_set = {tok.strip().lower() for tok in b.replace(";", ",").split(",") if tok.strip()}
    shared = sorted(a_set & b_set)
    # Title-case for display
    return [tok.title() for tok in shared]


def _bpm_phrase(prev_bpm: float, cur_bpm: float) -> str | None:
    """Render BPM relationship as a sentence, or None if unknown."""
    if cur_bpm <= 0:
        return None
    if prev_bpm <= 0:
        return f"BPM is {round(cur_bpm)}."
    diff = cur_bpm - prev_bpm
    if abs(diff) < 2:
        return f"BPM holds steady at {round(cur_bpm)}."
    if diff > 0:
        return f"BPM lifts {round(prev_bpm)} → {round(cur_bpm)} (+{round(diff)})."
    return f"BPM eases {round(prev_bpm)} → {round(cur_bpm)} ({round(diff)})."


def _camelot_phrase(prev: IndexEntry, cur: IndexEntry) -> str | None:
    """Render Camelot key relationship sentence, or None when unknown."""
    from autodj.dj_meta import camelot_label, camelot_position

    prev_label = camelot_label(prev.key, prev.mode)
    cur_label = camelot_label(cur.key, cur.mode)
    if prev_label == "--" or cur_label == "--":
        return None

    prev_pos = camelot_position(prev.key, prev.mode)
    cur_pos = camelot_position(cur.key, cur.mode)
    if prev_pos is None or cur_pos is None:
        return f"Key {cur_label}."

    if prev_pos == cur_pos:
        return f"Same Camelot key ({cur_label})."

    pn, ps = prev_pos
    cn, cs = cur_pos

    if pn == cn and ps != cs:
        return f"Camelot key {prev_label} → {cur_label}: relative major/minor flip."

    if ps == cs:
        diff = abs(pn - cn)
        if diff == 1 or diff == 11:
            return f"Camelot key {prev_label} → {cur_label}: one step around the wheel."
        if diff == 2 or diff == 10:
            return f"Camelot key {prev_label} → {cur_label}: two-step energy lift."

    return f"Camelot key {prev_label} → {cur_label}."


def _energy_phrase(prev_e: float, cur_e: float) -> str | None:
    """Render energy delta sentence."""
    if cur_e <= 0:
        return None
    if prev_e <= 0:
        return f"Energy {cur_e:.2f}."
    diff = cur_e - prev_e
    if abs(diff) < 0.05:
        return f"Energy similar ({prev_e:.2f} → {cur_e:.2f})."
    if diff > 0:
        return f"Energy lifts ({prev_e:.2f} → {cur_e:.2f})."
    return f"Energy eases ({prev_e:.2f} → {cur_e:.2f})."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def explain_pick(
    prev: IndexEntry | None,
    cur: IndexEntry | None,
    *,
    mode: str = "similarity",
) -> list[str]:
    """Return a list of plain-English sentences explaining why *cur* follows *prev*.

    Args:
        prev: The previously-played track, or ``None`` (e.g. seed pick).
        cur: The freshly-picked track to explain.
        mode: Picker mode label.  One of ``"similarity"`` (default),
            ``"pure_shuffle"``, ``"smart_shuffle"``, ``"anchored"``,
            ``"discovery"``, ``"queue"``, ``"seed"``.

    Returns:
        Ordered list of sentences.  Empty if *cur* is ``None``.
    """
    if cur is None:
        return []

    out: list[str] = []

    # Mode preface — one sentence about HOW the pick was made.
    if mode == "seed":
        out.append("Session seed.")
    elif mode == "queue":
        out.append("Queued by you.")
    elif mode == "discovery":
        out.append("Discovery pick — sonically distant on purpose.")
    elif mode == "pure_shuffle":
        out.append("Random walk — uniformly random, no similarity filter.")
    elif mode == "smart_shuffle":
        out.append("Entropy walk — picked the most sonically distant candidate.")
    elif mode == "anchored":
        out.append("Anchored to the session seed — similarity from the seed track.")
    else:
        out.append("Sonically similar to the previous track.")

    if prev is None:
        # Nothing to compare against — still surface a couple of facts.
        if cur.bpm > 0:
            out.append(f"BPM {round(cur.bpm)}.")
        if cur.genre:
            out.append(f"Genre: {cur.genre}.")
        return out

    # Genre overlap
    shared = _shared_genres(prev.genre, cur.genre)
    if shared:
        if len(shared) == 1:
            out.append(f"Same genre — {shared[0]}.")
        else:
            out.append(f"Shared genres — {', '.join(shared)}.")
    elif cur.genre:
        out.append(f"Genre shifts to {cur.genre}.")

    # BPM
    bpm = _bpm_phrase(prev.bpm, cur.bpm)
    if bpm:
        out.append(bpm)

    # Camelot key
    cam = _camelot_phrase(prev, cur)
    if cam:
        out.append(cam)

    # Energy
    energy = _energy_phrase(prev.energy, cur.energy)
    if energy:
        out.append(energy)

    return out
