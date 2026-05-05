"""Unit tests for autodj.explain — plain-English why-this-track sentences."""

from __future__ import annotations

from autodj.explain import explain_pick
from autodj.indexer import IndexEntry


def _entry(
    path: str = "x.flac",
    title: str = "Title",
    artist: str = "Artist",
    album: str = "Album",
    bpm: float = 0.0,
    key: int = -1,
    mode: int = -1,
    energy: float = 0.0,
    genre: str = "",
) -> IndexEntry:
    return IndexEntry(
        path=path,
        title=title,
        artist=artist,
        album=album,
        bpm=bpm,
        key=key,
        mode=mode,
        energy=energy,
        genre=genre,
        length=180.0,
        year=0,
        tempo_confidence=0.0,
    )


class TestExplainPick:
    def test_none_current_returns_empty(self) -> None:
        assert explain_pick(_entry(), None) == []

    def test_seed_mode_no_previous(self) -> None:
        cur = _entry(bpm=120.0, genre="Rock")
        out = explain_pick(None, cur, mode="seed")
        assert any("seed" in s.lower() for s in out)
        assert any("120" in s for s in out)
        assert any("Rock" in s for s in out)

    def test_queue_mode_short_circuits(self) -> None:
        out = explain_pick(_entry(), _entry(), mode="queue")
        assert out and "queued" in out[0].lower()

    def test_discovery_mode_label(self) -> None:
        out = explain_pick(_entry(bpm=100.0), _entry(bpm=140.0), mode="discovery")
        assert "discovery" in out[0].lower()

    def test_pure_shuffle_mode_label(self) -> None:
        out = explain_pick(_entry(), _entry(), mode="pure_shuffle")
        assert "random" in out[0].lower()

    def test_anchored_mode_label(self) -> None:
        out = explain_pick(_entry(), _entry(), mode="anchored")
        assert "anchor" in out[0].lower() or "seed" in out[0].lower()

    def test_smart_shuffle_label(self) -> None:
        out = explain_pick(_entry(), _entry(), mode="smart_shuffle")
        assert "entropy" in out[0].lower() or "distant" in out[0].lower()

    def test_shared_genre_single(self) -> None:
        prev = _entry(genre="Trip-Hop")
        cur = _entry(genre="trip-hop")
        out = explain_pick(prev, cur)
        assert any("Trip-Hop" in s for s in out)

    def test_shared_genres_multiple(self) -> None:
        prev = _entry(genre="electronic, ambient")
        cur = _entry(genre="ambient; electronic")
        out = explain_pick(prev, cur)
        joined = " | ".join(out)
        assert "Ambient" in joined and "Electronic" in joined

    def test_genre_shift_when_no_overlap(self) -> None:
        prev = _entry(genre="Jazz")
        cur = _entry(genre="House")
        out = explain_pick(prev, cur)
        assert any("shifts" in s and "House" in s for s in out)

    def test_bpm_holds_steady(self) -> None:
        out = explain_pick(_entry(bpm=120.0), _entry(bpm=121.0))
        assert any("steady" in s for s in out)

    def test_bpm_lifts(self) -> None:
        out = explain_pick(_entry(bpm=100.0), _entry(bpm=130.0))
        assert any("lifts" in s for s in out)

    def test_bpm_eases(self) -> None:
        out = explain_pick(_entry(bpm=130.0), _entry(bpm=100.0))
        assert any("eases" in s for s in out)

    def test_camelot_same_position(self) -> None:
        # Both 8B (C major)
        out = explain_pick(_entry(key=0, mode=1), _entry(key=0, mode=1))
        assert any("Same Camelot" in s for s in out)

    def test_camelot_relative_flip(self) -> None:
        # 8B (C major) → 8A (A minor)
        out = explain_pick(_entry(key=0, mode=1), _entry(key=9, mode=0))
        assert any("relative" in s for s in out)

    def test_camelot_one_step(self) -> None:
        # 8B (C major) → 9B (G major)
        out = explain_pick(_entry(key=0, mode=1), _entry(key=7, mode=1))
        assert any("one step" in s for s in out)

    def test_camelot_two_step(self) -> None:
        # 8B → 10B
        out = explain_pick(_entry(key=0, mode=1), _entry(key=2, mode=1))
        assert any("two-step" in s for s in out)

    def test_camelot_unknown_omits_phrase(self) -> None:
        out = explain_pick(_entry(key=-1, mode=-1), _entry(key=-1, mode=-1))
        assert not any("Camelot" in s for s in out)

    def test_energy_similar(self) -> None:
        out = explain_pick(_entry(energy=0.5), _entry(energy=0.51))
        assert any("Energy similar" in s for s in out)

    def test_energy_lifts(self) -> None:
        out = explain_pick(_entry(energy=0.3), _entry(energy=0.6))
        assert any("Energy lifts" in s for s in out)

    def test_energy_eases(self) -> None:
        out = explain_pick(_entry(energy=0.7), _entry(energy=0.4))
        assert any("Energy eases" in s for s in out)

    def test_unknown_mode_falls_through(self) -> None:
        out = explain_pick(_entry(), _entry(), mode="totally_unknown_mode")
        # Falls through to the default "sonically similar" preface
        assert "similar" in out[0].lower()
