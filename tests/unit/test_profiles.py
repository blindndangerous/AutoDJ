"""Unit tests for :mod:`autodj.profiles`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autodj.profiles import ProfileSnapshot, ProfileStore, validate_name


class TestValidateName:
    def test_accepts_alpha(self) -> None:
        assert validate_name("MyMix") == "MyMix"

    def test_accepts_dash_underscore_space(self) -> None:
        assert validate_name("Late_night - mix 1") == "Late_night - mix 1"

    def test_rejects_path_traversal(self) -> None:
        with pytest.raises(ValueError):
            validate_name("../escape")

    def test_rejects_slashes(self) -> None:
        with pytest.raises(ValueError):
            validate_name("a/b")

    def test_rejects_too_long(self) -> None:
        with pytest.raises(ValueError):
            validate_name("x" * 65)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            validate_name("")


class TestProfileSnapshotRoundTrip:
    def test_to_dict_from_dict(self) -> None:
        snap = ProfileSnapshot(
            name="Wakeup",
            bpm_lo=70.0,
            bpm_hi=120.0,
            beat_sync_fx=True,
        )
        d = snap.to_dict()
        again = ProfileSnapshot.from_dict(d)
        assert again == snap

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"name": "X", "bpm_low": 80}, "unknown profile keys: ['bpm_low']"),
            ({"name": "X", "extra": {}}, "unknown profile keys: ['extra']"),
            ({"bpm_lo": 80}, "needs a name"),
            (["X"], "JSON object"),
        ],
    )
    def test_from_dict_rejects_what_it_cannot_apply(self, data, message) -> None:
        with pytest.raises(ValueError, match=re.escape(message)):
            ProfileSnapshot.from_dict(data)


class TestProfileStore:
    def test_list_empty(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        assert store.list_names() == []

    def test_save_and_load(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        snap = ProfileSnapshot(name="Workout", bpm_lo=120, bpm_hi=140)
        store.save(snap)
        again = store.load("Workout")
        assert again.bpm_lo == 120
        assert again.bpm_hi == 140

    def test_list_returns_saved(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        store.save(ProfileSnapshot(name="A"))
        store.save(ProfileSnapshot(name="B"))
        store.save(ProfileSnapshot(name="C"))
        assert store.list_names() == ["A", "B", "C"]

    def test_delete_returns_true_when_removed(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        store.save(ProfileSnapshot(name="Tmp"))
        assert store.delete("Tmp") is True
        assert "Tmp" not in store.list_names()

    def test_delete_returns_false_when_missing(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        assert store.delete("NeverExisted") is False

    def test_load_names_the_profile_with_an_unknown_key(self, tmp_path: Path) -> None:
        root = tmp_path / "profiles"
        root.mkdir()
        (root / "Old.json").write_text('{"name": "Old", "extra": {}}', encoding="utf-8")
        with pytest.raises(ValueError, match=r"Profile 'Old' .*unknown profile keys: \['extra'\]"):
            ProfileStore(root).load("Old")

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        with pytest.raises(FileNotFoundError):
            store.load("Missing")

    def test_save_writes_json(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        store.save(ProfileSnapshot(name="JSON-out", bpm_lo=80))
        target = tmp_path / "profiles" / "JSON-out.json"
        assert target.is_file()
        # Round-trips via stdlib json.
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["name"] == "JSON-out"
        assert data["bpm_lo"] == 80

    def test_path_traversal_save_blocked(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path / "profiles")
        with pytest.raises(ValueError):
            store.save(ProfileSnapshot(name="../sneaky"))

    def test_list_skips_invalid_filenames(self, tmp_path: Path) -> None:
        # An attacker placing a junk file in the folder shouldn't surface.
        root = tmp_path / "profiles"
        root.mkdir()
        (root / "ok.json").write_text("{}")
        (root / "weird name.json").write_text("{}")  # contains space, valid
        (root / "evil$$.json").write_text("{}")  # invalid -- $$ rejected
        store = ProfileStore(root)
        names = store.list_names()
        assert "ok" in names
        assert "weird name" in names
        assert all("$" not in n for n in names)
