"""Moving an old index folder into libraries/, shared/ and stations/."""

from __future__ import annotations

from pathlib import Path

from autodj.config import load_config
from autodj.layout import move_old_layout


def _old_index(root: Path, name: str = "default") -> Path:
    old = root / "index" / name
    for path, text in {
        "library.db": "db",
        "library.db-wal": "wal",
        "dj_meta.db": "cues",
        "related-artists.toml": "links",
        "categories.json": "{}",
        "playlists/A.m3u": "a.flac",
        "time_checks/Studio/03.wav": "pack",
        "web_state.json": "{}",
        "clockwheels/Night.json": "{}",
        "liners/id.wav": "liner",
        "logs/autodj.log": "log",
        "stations/owl/web_state.json": "owl",
        ".autodj-serve.lock": "",
    }.items():
        (old / path).parent.mkdir(parents=True, exist_ok=True)
        (old / path).write_text(text, encoding="utf-8")
    (root / "index" / "relay-status.json").write_text("relays", encoding="utf-8")
    (root / "index" / "profiles").mkdir()
    (root / "index" / "profiles" / "Party.json").write_text("{}", encoding="utf-8")
    (root / "index" / "stereo-tool-presets").mkdir()
    (root / "index" / "stereo-tool-presets" / "Night.sts").write_text("preset", encoding="utf-8")
    return old


def _config(root: Path, extra: str = ""):
    path = root / "config.toml"
    path.write_text(f'[index]\nindex_dir = "{(root / "index").as_posix()}"\n{extra}')
    return load_config(path, environ={})


def test_the_old_layout_moves_once_into_the_new_one(tmp_path: Path) -> None:
    old = _old_index(tmp_path)
    cfg = _config(tmp_path)
    index = tmp_path / "index"

    assert move_old_layout(cfg) is True

    expected = {
        "libraries/main/library.db": "db",
        "libraries/main/library.db-wal": "wal",
        "libraries/main/dj_meta.db": "cues",
        "libraries/main/related-artists.toml": "links",
        "shared/categories.json": "{}",
        "shared/playlists/A.m3u": "a.flac",
        "shared/time_checks/Studio/03.wav": "pack",
        "shared/stereo-tool-presets/Night.sts": "preset",
        "stations/default/web_state.json": "{}",
        "stations/default/clockwheels/Night.json": "{}",
        "stations/default/liners/id.wav": "liner",
        "stations/default/relay-status.json": "relays",
        "stations/default/profiles/Party.json": "{}",
        "stations/owl/web_state.json": "owl",
        "logs/autodj.log": "log",
    }
    for name, text in expected.items():
        assert (index / name).read_text(encoding="utf-8") == text, name
    assert not old.exists()
    assert move_old_layout(cfg) is False


def test_a_named_index_moves_and_another_is_left_alone(tmp_path: Path, caplog) -> None:
    _old_index(tmp_path, "gold")
    other = tmp_path / "index" / "chill"
    other.mkdir()
    (other / "library.db").write_text("other", encoding="utf-8")
    cfg = _config(tmp_path, 'name = "gold"\n')

    assert move_old_layout(cfg) is True

    assert (tmp_path / "index" / "libraries" / "main" / "library.db").read_text("utf-8") == "db"
    assert (other / "library.db").read_text("utf-8") == "other"
    assert "chill is another named index" in caplog.text


def test_files_the_new_layout_already_has_are_left_in_place(tmp_path: Path, caplog) -> None:
    old = _old_index(tmp_path)
    new = tmp_path / "index" / "stations" / "default" / "web_state.json"
    new.parent.mkdir(parents=True)
    new.write_text("newer", encoding="utf-8")

    assert move_old_layout(_config(tmp_path)) is True

    assert new.read_text("utf-8") == "newer"
    assert (old / "web_state.json").read_text("utf-8") == "{}"
    assert "web_state.json" in caplog.text
