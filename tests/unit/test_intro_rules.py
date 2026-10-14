"""Intro rules reuse clockwheel conditions and the matched-intro lifecycle."""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj.catalog import normalize_path
from autodj.intro_rules import IntroRule, IntroRules
from autodj.matched_intros import Intro, MatchedIntros, file_name
from tests.unit._break_station import write_voice

NOW = 1_800_000_000.0


def library(tmp_path: Path):
    clip = write_voice(tmp_path / "liner.wav", 1)
    liner = SimpleNamespace(path=str(clip))
    record = {"id": "liner-1", "kind": "liner", "path": str(clip)}
    return SimpleNamespace(
        record_for_id=lambda key: record if key == "liner-1" else None,
        entry_for_path=lambda path: liner if path == str(clip) else None,
        entries_snapshot=lambda kind: (liner,),
        catalog=SimpleNamespace(
            sources=lambda *args: [
                {"id": "music", "path": str(tmp_path), "recursive": True, "kind": "music"}
            ]
        ),
    )


def rule(**changes):
    return IntroRule(
        conditions=[{"field": "genre", "operator": "is", "value": "Rock"}],
        clip_ids=["liner-1"],
        **changes,
    )


def test_rule_matches_genre_year_and_beets_with_cooldown(tmp_path: Path) -> None:
    store = IntroRules(
        tmp_path / "intro_rules.json", lambda at: date(2026, 10, 13), random.Random(1)
    )
    key = "a" * 32
    store.save(
        key,
        IntroRule(
            conditions=[
                {"field": "genre", "operator": "is", "value": "Rock"},
                {"field": "year", "operator": "between", "value": 1990, "upper": 1999},
                {
                    "field": "beets",
                    "name": "original_year",
                    "operator": "between",
                    "value": 1980,
                    "upper": 1989,
                },
            ],
            clip_ids=["liner-1"],
            cooldown_minutes=60,
        ),
        create=True,
    )
    lib = library(tmp_path)
    lib.clock_beets = SimpleNamespace(
        snapshot=lambda fields: (
            fields,
            {normalize_path(str(tmp_path / "song.flac")): {"original_year": 1984}},
            None,
        )
    )
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=1995)
    # original_year uses first_release_year, which accepts original_year in a Beets row.
    chosen = store.choose(entry, NOW, lib)
    assert chosen is not None and chosen.intro_id == f"rule:{key}"
    assert store.choose(entry, NOW + 10, lib) is None
    store.heard(key)
    store.upkeep()
    assert store.all()[key].last_aired is not None
    assert (
        store.choose(SimpleNamespace(path=entry.path, genre="Pop", year=1995), NOW + 4000, lib)
        is None
    )


def test_rule_dates_chance_and_priority_after_song_and_artist(tmp_path: Path) -> None:
    lib = library(tmp_path)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=1995)
    store = MatchedIntros(
        tmp_path / "matched_intros.json",
        tmp_path / "intros",
        day_at=lambda at: date(2026, 10, 13),
        rng=random.Random(1),
    )
    store.rules = IntroRules(tmp_path / "intro_rules.json", store._day_at, store._rng)
    store.rules.save("a" * 32, rule(not_before=date(2026, 10, 14)), create=True)
    assert store.rules.choose(entry, NOW, lib) is None
    store.rules.save("a" * 32, rule(), create=False)
    assert store.rules.choose(entry, NOW, lib) is not None
    store.release((f"rule:{'a' * 32}",))
    store.heard(f"rule:{'a' * 32}")
    store.upkeep()
    assert store.rules.all()["a" * 32].last_aired is not None
    clip_id = "b" * 32
    store.folder.mkdir()
    write_voice(store.folder / file_name(clip_id, ".wav"), 1)
    store.add(clip_id, Intro(file=file_name(clip_id, ".wav"), track_id="song"))
    assert store.choose("song", "", NOW).intro_id == clip_id


def test_folder_and_category_conditions_use_clockwheel_membership(tmp_path: Path) -> None:
    lib = library(tmp_path)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    lib.categories = SimpleNamespace(members=lambda: {"rock": ("Rock", frozenset({entry.path}))})
    store = IntroRules(
        tmp_path / "intro_rules.json", lambda at: date(2026, 10, 13), random.Random(1)
    )
    store.save(
        "a" * 32,
        IntroRule(
            conditions=[
                {"field": "folder", "source": "music", "folder": "", "recursive": True},
                {"field": "category", "category": "rock"},
            ],
            clip_ids=["liner-1"],
            cooldown_minutes=0,
        ),
        create=True,
    )
    assert store.choose(entry, NOW, lib) is not None
    lib.categories = SimpleNamespace(members=lambda: {"rock": ("Rock", frozenset())})
    assert store.choose(entry, NOW + 3600, lib) is None


def test_bad_entries_retained_and_unreadable_file_blocks_saves(tmp_path: Path, monkeypatch) -> None:
    store = IntroRules(
        tmp_path / "intro_rules.json", lambda at: date(2026, 10, 13), random.Random(1)
    )
    key = "a" * 32
    store.save(key, rule(), create=True)
    data = json.loads(store.path.read_text())
    data["rules"]["bad"] = {"chance": 500}
    store.path.write_text(json.dumps(data))
    assert list(store.all()) == [key]
    assert store.problem() is not None
    store.save("b" * 32, rule(), create=True)
    assert json.loads(store.path.read_text())["rules"]["bad"] == {"chance": 500}
    original = store.path.read_bytes()
    store._stamp = None
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError("busy"))
    )
    assert key in store.all()
    with pytest.raises(PermissionError):
        store.remove(key)
    monkeypatch.undo()
    assert store.path.read_bytes() == original


def test_invalid_document_is_set_aside(tmp_path: Path) -> None:
    store = IntroRules(tmp_path / "intro_rules.json", lambda at: date.today(), random.Random(1))
    store.path.write_text("not json")
    assert store.all() == {}
    assert not store.path.exists()
    assert len(list(tmp_path.glob("intro_rules.json.bad-*"))) == 1


def test_first_rule_creates_a_new_station_state_folder(tmp_path: Path) -> None:
    store = IntroRules(
        tmp_path / "nightowl" / "intro_rules.json", date.fromtimestamp, random.Random(1)
    )
    store.save("a" * 32, rule(), create=True)
    assert store.path.is_file()


def test_match_any_and_chance_roll(tmp_path: Path) -> None:
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save(
        "a" * 32,
        IntroRule(
            conditions=[
                {"field": "genre", "operator": "is", "value": "Rock"},
                {"field": "year", "operator": "eq", "value": 1995},
            ],
            match="any",
            clip_ids=["liner-1"],
            chance=1,
        ),
        create=True,
    )
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=2000)
    assert store.choose(entry, NOW, library(tmp_path)) is None


def folder_rule(tmp_path: Path, **changes) -> IntroRule:
    return IntroRule(
        conditions=[{"field": "genre", "operator": "is", "value": "Rock"}],
        liner_folder=str(tmp_path),
        cooldown_minutes=0,
        **changes,
    )


def test_liner_folder_pool_is_warmed_on_the_tick_not_the_render(
    tmp_path: Path, monkeypatch
) -> None:
    lib = library(tmp_path)
    liner = lib.entries_snapshot("liner")[0]
    snapshot = (liner,)
    lib.entries_snapshot = lambda kind: snapshot
    store = IntroRules(
        tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1), library=lambda: lib
    )
    store.save("a" * 32, folder_rule(tmp_path), create=True)
    # The save leaves the pool to the station's tick, which builds it.
    assert store.needs_upkeep()
    store.upkeep()
    assert not store.needs_upkeep()
    built = []
    real = IntroRules._pool
    monkeypatch.setattr(
        IntroRules, "_pool", staticmethod(lambda *args: built.append(args) or real(*args))
    )
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    chosen = store.choose(entry, NOW, lib)
    assert chosen is not None and chosen.path == Path(liner.path)
    assert built == []  # the render found the pool ready
    store.release("a" * 32)

    # A rescan publishes new liners: until the tick, the old pool serves,
    # but only liners the library still has.
    snapshot = ()
    lib.entry_for_path = lambda path: None
    assert store.choose(entry, NOW + 3600, lib) is None
    assert built == []
    monkeypatch.setattr(store, "_warmed_at", float("-inf"))
    store.upkeep()
    assert built and store._folder_pools == {str(tmp_path): ()}


def test_a_render_before_the_first_tick_builds_the_pool_itself(tmp_path: Path) -> None:
    lib = library(tmp_path)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, folder_rule(tmp_path), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is not None


def test_rule_clips_pass_the_station_liner_filter(tmp_path: Path) -> None:
    """Another station's liners, and liners outside their play dates, never air."""
    lib = library(tmp_path)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, rule(cooldown_minutes=0), create=True)
    store.save("b" * 32, folder_rule(tmp_path), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    asked = []

    def refuse(entries, at):
        asked.append(at)
        return ()

    assert store.choose(entry, NOW, lib, allowed=refuse) is None
    assert asked and set(asked) == {NOW}
    assert store.choose(entry, NOW, lib, allowed=lambda entries, at: entries) is not None


def test_an_edit_between_reads_never_breaks_the_render(tmp_path: Path) -> None:
    lib = library(tmp_path)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, rule(cooldown_minutes=0), create=True)
    store.all()
    # Conditions compiled for an older version of the rule are passed over.
    store._compiled["a" * 32] = []
    store._stamp = store._stat()
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is None


def test_cooldown_survives_a_restart(tmp_path: Path) -> None:
    lib = library(tmp_path)
    path = tmp_path / "intro_rules.json"
    store = IntroRules(path, date.fromtimestamp, random.Random(1))
    store.save("a" * 32, rule(cooldown_minutes=60), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is not None
    store.heard("a" * 32)
    store.upkeep()
    later = IntroRules(path, date.fromtimestamp, random.Random(1))
    aired = later.all()["a" * 32].last_aired
    assert aired is not None
    assert later.choose(entry, aired + 60, lib) is None
    assert later.choose(entry, aired + 3601, lib) is not None


def test_an_unheard_rule_is_released_for_the_next_change(tmp_path: Path) -> None:
    """#209: a render that ended before its intro aired gives the rule back."""
    lib = library(tmp_path)
    store = MatchedIntros(
        tmp_path / "matched_intros.json",
        tmp_path / "intros",
        day_at=date.fromtimestamp,
        rng=random.Random(1),
    )
    store.rules = IntroRules(tmp_path / "intro_rules.json", store._day_at, store._rng)
    store.rules.save("a" * 32, rule(cooldown_minutes=60), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    chosen = store.rules.choose(entry, NOW, lib)
    assert chosen is not None
    assert store.rules.choose(entry, NOW + 30, lib) is None  # held for its change
    store.release_later((chosen.intro_id,))
    assert store.needs_upkeep()
    store.upkeep()
    assert store.rules.choose(entry, NOW + 30, lib) is not None
    assert store.rules.all()["a" * 32].last_aired is None


def test_a_large_folder_of_missing_files_cannot_hold_up_the_render(
    tmp_path: Path, monkeypatch
) -> None:
    from autodj import intro_rules

    lib = library(tmp_path)
    gone = tuple(SimpleNamespace(path=str(tmp_path / f"gone-{i}.wav")) for i in range(1000))
    lib.entries_snapshot = lambda kind: gone
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, folder_rule(tmp_path, clip_ids=[]), create=True)
    looked = []
    real = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: looked.append(self) or real(self))
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is None
    assert len(set(looked)) == len(looked) == intro_rules.MAX_TRIES
