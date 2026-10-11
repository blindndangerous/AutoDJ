"""The saved cart wall: validation, atomic saves and a damaged file set aside."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from autodj.carts import CartError, CartPage, CartSlot, CartStore, CartWall, PageName


def _slot(**changes: object) -> CartSlot:
    return CartSlot.model_validate(
        {"name": "Station ID", "source": "upload", "clip": "id.wav", **changes}
    )


def test_default_wall_is_four_pages_of_twelve_empty_carts(tmp_path: Path) -> None:
    store = CartStore(tmp_path)
    assert [page.name for page in store.wall.pages] == ["Page 1", "Page 2", "Page 3", "Page 4"]
    assert all(page.slots == [None] * 12 for page in store.wall.pages)
    assert store.wall.active_page == 1
    assert not (tmp_path / "carts.json").exists()


def test_a_saved_wall_loads_after_a_restart(tmp_path: Path) -> None:
    store = CartStore(tmp_path)
    store.wall.pages[0].slots[2] = _slot(mode="replace", duration=7.4)
    store.wall.pages[1].name = "Jingles"
    store.wall.active_page = 2
    store.save()

    text = (tmp_path / "carts.json").read_bytes().decode("utf-8")
    assert "\r" not in text
    assert json.loads(text)["schema_version"] == 1
    assert CartStore(tmp_path).wall == store.wall
    assert list(tmp_path.glob(".carts.json.*")) == []


def test_a_failed_save_keeps_the_old_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CartStore(tmp_path)
    store.save()
    before = (tmp_path / "carts.json").read_bytes()
    store.wall.active_page = 3

    def refuse(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        store.save()
    assert (tmp_path / "carts.json").read_bytes() == before
    assert list(tmp_path.glob(".carts.json.*")) == []


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b'{"schema_version": 2}',
        b'{"schema_version": 1, "pages": []}',
        b'{"schema_version": 1, "active_page": 5}',
        b'{"schema_version": 1, "surprise": true}',
        json.dumps(
            {
                "schema_version": 1,
                "pages": [{"name": "Same"}, {"name": "same"}, {"name": "C"}, {"name": "D"}],
            }
        ).encode(),
        json.dumps(
            {
                "schema_version": 1,
                "pages": [
                    {
                        "name": "A",
                        "slots": [{"name": "X", "source": "upload", "clip": "../x.wav"}]
                        + [None] * 11,
                    },
                    {"name": "B"},
                    {"name": "C"},
                    {"name": "D"},
                ],
            }
        ).encode(),
    ],
    ids=[
        "not-json",
        "newer-version",
        "no-pages",
        "active-past-end",
        "unknown-key",
        "dup-names",
        "path-in-upload",
    ],
)
def test_a_damaged_wall_is_set_aside_and_startup_continues(
    tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "carts.json").write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="autodj.carts"):
        store = CartStore(tmp_path)
    assert store.wall == CartWall()
    assert not (tmp_path / "carts.json").exists()
    [bad] = tmp_path.glob("carts.json.bad-*")
    assert bad.read_bytes() == content
    assert "moved to carts.json.bad-" in caplog.text


def test_a_wall_that_cannot_be_moved_aside_still_loads_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "carts.json").write_text("{", encoding="utf-8")

    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "replace", refuse)
    with caplog.at_level(logging.ERROR, logger="autodj.carts"):
        store = CartStore(tmp_path)
    assert store.wall == CartWall()
    assert "Could not set aside the damaged cart wall" in caplog.text


def test_names_are_trimmed_and_refuse_control_characters() -> None:
    assert _slot(name="  Station ID ").name == "Station ID"
    assert PageName(name=" Jingles ").name == "Jingles"
    for bad in ("   ", "ID\nnext", "Tab\there"):
        with pytest.raises(ValidationError):
            _slot(name=bad)
        with pytest.raises(ValidationError):
            CartPage(name=bad)
    with pytest.raises(ValidationError):
        _slot(name="x" * 81)


@pytest.mark.parametrize(
    "bad",
    [
        {"duration": float("inf")},
        {"duration": -1},
        {"mode": "loud"},
        {"clip": "../secret.wav"},
        {"clip": "C:\\secret.wav"},
        {"clip": "note.txt"},
    ],
)
def test_a_slot_refuses_bad_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _slot(**bad)


def test_a_liner_clip_is_an_id_checked_when_it_is_played() -> None:
    # Catalog IDs are not file names, so only uploads are name-checked here.
    assert _slot(source="liner", clip="3f2a").clip == "3f2a"


def test_slot_lookup_names_the_problem(tmp_path: Path) -> None:
    store = CartStore(tmp_path)
    store.wall.pages[0].slots[2] = _slot()
    assert store.slot(1, 3).name == "Station ID"
    for page, slot, message in ((5, 1, "no cart page 5"), (1, 13, "no cart 13"), (1, 4, "empty")):
        with pytest.raises(CartError, match=message) as caught:
            store.slot(page, slot)
        assert caught.value.status == 404


def test_upload_names_list_only_clips(tmp_path: Path) -> None:
    store = CartStore(tmp_path)
    assert store.upload_names() == []
    store.uploads.mkdir()
    for name in ("b.wav", "A.mp3", "notes.txt", ".b.wav.0123456789abcdef.part"):
        (store.uploads / name).write_bytes(b"x")
    (store.uploads / "folder.wav").mkdir()
    assert store.upload_names() == ["A.mp3", "b.wav"]


def test_in_use_finds_an_uploaded_clip_on_any_page(tmp_path: Path) -> None:
    store = CartStore(tmp_path)
    store.wall.pages[3].slots[11] = _slot(clip="sweep.wav")
    store.wall.pages[0].slots[0] = _slot(source="liner", clip="sweep.wav")
    assert store.in_use("sweep.wav")
    assert not store.in_use("id.wav")
