"""Music clock conditions that refer to saved rotation categories."""

from types import SimpleNamespace

import pytest

from autodj.catalog import LibraryCatalog
from autodj.categories_api import clockwheel_uses
from autodj.clockwheel import ClockSlot, ConditionPaths, condition_paths, pick_slot
from autodj.clockwheel_store import ClockwheelStore, validate_clockwheel
from tests.integration.test_categories_api import _client as category_client


def _entry(name):
    return SimpleNamespace(path=f"{name}/{name}.flac")


def _slot(conditions, match="all", kind="music"):
    return ClockSlot(
        kind=kind,
        match=match,
        conditions=tuple(conditions),
        selection="random",
        enforce_rules=False,
    )


def _cat(category):
    return {"field": "category", "category": category}


def test_category_membership_intersects_other_conditions_and_categories():
    a, b, c = (_entry(name) for name in "abc")
    entries = [a, b, c]
    categories = {
        "one": ("One", frozenset({a.path, b.path})),
        "two": ("Two", frozenset({b.path, c.path})),
    }
    bpm = {"field": "bpm", "operator": "gte", "value": 120}
    for entry, value in zip(entries, (130, 125, 140), strict=True):
        entry.bpm = value
    folder = {"field": "folder", "folder": "a", "recursive": False}
    # The category sets intersect at b; an additional metadata condition narrows it to b.
    slot = _slot([_cat("one"), _cat("two"), bpm])
    assert condition_paths(slot, entries, None, categories) == {b.path}
    assert condition_paths(_slot([_cat("one"), folder]), entries, None, categories) == {a.path}


def test_match_any_is_union_without_duplicate_rng_candidates():
    a, b, c = (_entry(name) for name in "abc")
    entries = [a, b, c]
    categories = {
        "one": ("One", frozenset({a.path, b.path})),
        "two": ("Two", frozenset({b.path, c.path})),
    }

    class Capture:
        candidates = None

        def choice(self, candidates):
            self.candidates = list(candidates)
            return self.candidates[0]

    rng = Capture()
    selected = pick_slot(
        [_slot([_cat("one"), _cat("two")], match="any")],
        0,
        entries,
        {},
        lambda _entry: True,
        rng,
        categories=categories,
    )
    assert selected.entry is a
    assert rng.candidates == [a, b, c]


def test_missing_category_skips_whole_match_any_step_and_empty_category_is_empty():
    a, fallback = _entry("a"), _entry("fallback")
    missing = _slot([_cat("gone"), {"field": "year", "operator": "gte", "value": 1900}], "any")
    result = pick_slot(
        [missing, ClockSlot(kind="track", path=fallback.path)],
        0,
        [a, fallback],
        {},
        lambda _entry: True,
        categories={},
    )
    assert result.entry is fallback
    assert any(
        "category gone" in message and "no longer exists" in message for message in result.skipped
    )

    a.year = 2000
    empty = _slot([_cat("empty"), {"field": "year", "operator": "gte", "value": 1900}], "any")
    result = pick_slot(
        [empty],
        0,
        [a],
        {},
        lambda _entry: True,
        categories={"empty": ("Empty", frozenset())},
    )
    assert result.entry is a


def test_category_condition_cache_tracks_category_membership_changes():
    a, b = _entry("a"), _entry("b")
    slots, entries, liners, beets = [_slot([_cat("one")])], [a, b], [], None
    first = {"one": ("One", frozenset({a.path}))}
    changed = {"one": ("One", frozenset({b.path}))}
    cache = ConditionPaths(slots, entries, liners, beets, first)
    assert cache.current(slots, entries, liners, beets, first)
    assert not cache.current(slots, entries, liners, beets, changed)
    assert cache.paths(0) == {a.path}
    assert (
        pick_slot(
            slots,
            0,
            entries,
            {},
            lambda _entry: True,
            categories=first,
            condition_cache=cache,
        ).entry
        is a
    )
    assert condition_paths(slots[0], entries, beets, changed) == {b.path}
    assert (
        pick_slot(
            slots,
            0,
            entries,
            {},
            lambda _entry: True,
            categories=changed,
            condition_cache=None,
        ).entry
        is b
    )


def test_category_condition_syntax_and_music_only_rule():
    valid = {"field": "category", "category": "rock-1"}
    normalized = validate_clockwheel(
        {
            "version": 1,
            "name": "By category",
            "steps": [{"type": "music", "conditions": [valid]}],
            "resync_minutes": [0],
        }
    )
    assert normalized["steps"][0]["conditions"] == [valid]
    for invalid in (
        {"field": "category", "category": ""},
        {"field": "category", "category": "../x"},
        {"field": "category", "category": "rock", "name": "extra"},
    ):
        with pytest.raises(ValueError, match="category condition"):
            validate_clockwheel(
                {
                    "version": 1,
                    "name": "Bad",
                    "steps": [{"type": "music", "conditions": [invalid]}],
                    "resync_minutes": [0],
                }
            )
    with pytest.raises(ValueError, match="only available for music"):
        validate_clockwheel(
            {
                "version": 1,
                "name": "Liner",
                "steps": [
                    {"type": "liner", "conditions": [valid]},
                    {"type": "music", "conditions": [_cat("rock")]},
                ],
                "resync_minutes": [0],
            }
        )


def test_category_usage_deduplicates_condition_and_legacy_references(tmp_path):
    store = ClockwheelStore(tmp_path / "clockwheels")
    store.save(
        {
            "version": 1,
            "name": "Mixed",
            "steps": [
                {"type": "music", "conditions": [_cat("rock"), _cat("rock")]},
                {"type": "category", "category": "rock"},
            ],
            "resync_minutes": [0],
        }
    )
    assert clockwheel_uses(store.root) == {"rock": ["Mixed"]}


def test_closed_category_paths_are_excluded_from_music_conditions_even_without_rules():
    a, b = _entry("a"), _entry("b")
    a.year = b.year = 2000
    selected = pick_slot(
        [_slot([{"field": "year", "operator": "gte", "value": 1900}])],
        0,
        [a, b],
        {},
        lambda _entry: True,
        categories={"closed": ("Closed", frozenset({a.path}))},
        closed_categories={"closed": "outside its hours"},
    )
    assert selected.entry is b


def test_routes_reject_missing_category_on_save_and_activation(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        app, client, _songs = category_client(tmp_path, catalog)
        app.state.security_policy.config.allowed_origins = ["http://testserver"]
        code = app.state.request_pairing_code()[0]
        paired = client.post(
            "/api/pair",
            json={"code": code, "device_name": "Desk"},
            headers={"Origin": "http://testserver"},
        )
        assert paired.status_code == 200, paired.text
        headers = {"Origin": "http://testserver"}
        category = client.post(
            "/api/categories",
            json={"name": "Rock", "source": "query", "query": "genre:rock"},
            headers=headers,
        ).json()["category"]
        clock = {
            "version": 1,
            "name": "By category",
            "steps": [{"type": "music", "conditions": [_cat("missing")]}],
            "resync_minutes": [0],
        }
        missing = client.post("/api/library/clock", json=clock, headers=headers)
        assert missing.status_code == 400
        assert "Step 1: its category is not saved" in missing.json()["detail"]

        clock["steps"] = [{"type": "music", "conditions": [_cat(category["id"])]}]
        assert client.post("/api/library/clock", json=clock, headers=headers).status_code == 200
        assert (
            client.post(
                "/api/library/clock/active", json={"name": "By category"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.delete(f"/api/categories/{category['id']}", headers=headers).status_code == 200
        )
        activated = client.post(
            "/api/library/clock/active", json={"name": "By category"}, headers=headers
        )
        assert activated.status_code == 400
        assert "Step 1: its category is not saved" in activated.json()["detail"]
