"""Backup and restore with several stations (#70): each station's folder and settings."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from autodj.backup import STATION_FILES, BackupError, create_backup, restore_backup
from autodj.config import AutoDJConfig, load_config
from autodj.version import current_version


def _config(root: Path, *, stations: str = "nightowl", extra: str = "") -> AutoDJConfig:
    """A configuration with the default station and the named other stations."""
    (root / "music").mkdir(parents=True, exist_ok=True)
    text = f"""
[library]
music_dir = "{(root / "music").as_posix()}"

[index]
index_dir = "{(root / "index").as_posix()}"
model_dir = "{(root / "models").as_posix()}"

[recording]
folder = "{(root / "recordings" / "default").as_posix()}"
"""
    for slug in filter(None, stations.split(",")):
        text += f"""
[stations.{slug}.stream]
station_name = "{slug.title()} Radio"

[stations.{slug}.recording]
folder = "{(root / "recordings" / slug).as_posix()}"
"""
    path = root / "config.toml"
    path.write_text(text + extra, encoding="utf-8")
    cfg = load_config(path, environ={})
    cfg.index.active_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _station_dir(cfg: AutoDJConfig, slug: str) -> Path:
    return cfg.index.active_dir / "stations" / slug


def _fill(cfg: AutoDJConfig) -> dict[Path, bytes]:
    """Give both stations different state files and folders; return what was written."""
    written: dict[Path, bytes] = {}

    def put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written[path] = data

    for base, who in ((cfg.index.active_dir, b"default"), (_station_dir(cfg, "nightowl"), b"owl")):
        for name in STATION_FILES:
            put(base / name, b'{"station": "' + who + b'", "file": "' + name.encode() + b'"}')
        put(base / "liners" / "id.mp3", who + b" liner")
        put(base / "carts" / "jingle.wav", who + b" cart")
        put(base / "time_checks" / "03_05.wav", who + b" clip")
        put(base / "clockwheels" / "Night.json", who + b" wheel")
    put(cfg.index.index_dir / "profiles" / "Party.json", b"default profile")
    put(_station_dir(cfg, "nightowl") / "profiles" / "Party.json", b"owl profile")
    return written


def test_two_stations_round_trip(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    written = _fill(cfg)

    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    with zipfile.ZipFile(archive) as opened:
        names = set(opened.namelist())
        manifest = json.loads(opened.read("manifest.json"))
    assert manifest["stations"] == ["nightowl"]
    # The default station keeps today's names; Night Owl's are under its slug.
    assert {"index/web_state.json", "liners/id.mp3", "profiles/Party.json"} <= names
    assert {
        "stations/nightowl/index/web_state.json",
        "stations/nightowl/index/listener_bans.json",
        "stations/nightowl/index/recording.json",
        "stations/nightowl/liners/id.mp3",
        "stations/nightowl/carts/jingle.wav",
        "stations/nightowl/time_checks/03_05.wav",
        "stations/nightowl/clockwheels/Night.json",
        "stations/nightowl/profiles/Party.json",
    } <= names
    # A station never holds the shared categories, and nothing is stored twice.
    assert "stations/nightowl/index/categories.json" not in names
    assert not any(name.startswith("stations/nightowl/stations/") for name in names)

    for path in written:
        path.unlink()
    assert restore_backup(cfg, archive, force=True) == len(manifest["files"])

    for path, data in written.items():
        assert path.read_bytes() == data, path


def test_restore_to_a_new_machine_creates_the_station_folder(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "old")
    written = _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    fresh = _config(tmp_path / "new")
    restore_backup(fresh, archive, force=False)

    for path, data in written.items():
        moved = tmp_path / "new" / path.relative_to(tmp_path / "old")
        assert moved.read_bytes() == data


def test_a_station_folder_set_elsewhere_is_restored_there(tmp_path: Path) -> None:
    liners = tmp_path / "owl-liners"
    cfg = _config(
        tmp_path,
        extra=f'\n[stations.nightowl.playback]\nliners_folder = "{liners.as_posix()}"\n',
    )
    liners.mkdir()
    (liners / "owl.mp3").write_bytes(b"owl")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("stations/nightowl/liners/owl.mp3") == b"owl"
    (liners / "owl.mp3").unlink()

    restore_backup(cfg, archive, force=True)

    assert (liners / "owl.mp3").read_bytes() == b"owl"


def test_a_shared_liner_folder_is_stored_once(tmp_path: Path) -> None:
    shared = tmp_path / "liners"
    cfg = _config(
        tmp_path,
        extra=(
            f'\n[playback]\nliners_folder = "{shared.as_posix()}"\n'
            f'\n[stations.nightowl.playback]\nliners_folder = "{shared.as_posix()}"\n'
        ),
    )
    shared.mkdir()
    (shared / "id.mp3").write_bytes(b"both")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
    assert "liners/id.mp3" in names
    assert not [name for name in names if name.startswith("stations/nightowl/liners/")]
    (shared / "id.mp3").unlink()

    restore_backup(cfg, archive, force=True)

    assert (shared / "id.mp3").read_bytes() == b"both"


def test_a_station_folder_holding_another_is_refused(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        extra=(f'\n[stations.nightowl.playback]\nliners_folder = "{tmp_path.as_posix()}/index"\n'),
    )

    with pytest.raises(BackupError, match="stations/nightowl/liners folder"):
        create_backup(cfg, tmp_path / "elsewhere" / "backup.zip", force=False)


def test_backup_refuses_a_destination_inside_a_station_folder(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    inside = _station_dir(cfg, "nightowl") / "liners" / "backup.zip"
    inside.parent.mkdir(parents=True)

    with pytest.raises(BackupError, match="outside the stations/nightowl/liners folder"):
        create_backup(cfg, inside, force=False)


def test_restore_refuses_a_station_the_config_does_not_define(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "two")
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    single = _config(tmp_path / "one", stations="")

    with pytest.raises(BackupError, match=r"add \[stations.nightowl\] to config.toml first"):
        restore_backup(single, archive, force=True)

    # Nothing was unpacked, the default station's files included.
    assert not (single.index.active_dir / "web_state.json").exists()
    assert not (single.index.active_dir / "stations").exists()


def _archive(path: Path, files: dict[str, bytes], stations: list[str] | None = None) -> Path:
    manifest: dict[str, object] = {"autodj_version": current_version(), "files": list(files)}
    if stations is not None:
        manifest["stations"] = stations
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_restore_refuses_a_member_of_an_undefined_station_without_a_list(tmp_path: Path) -> None:
    cfg = _config(tmp_path, stations="")
    archive = _archive(tmp_path / "b.zip", {"stations/nightowl/index/web_state.json": b"{}"})

    with pytest.raises(BackupError, match="does not define"):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize(
    "name",
    [
        "stations/../index/web_state.json",
        "stations/default/index/web_state.json",
        "stations/Night_Owl/index/web_state.json",
        "stations/-owl/liners/id.mp3",
    ],
)
def test_restore_refuses_unsafe_station_names(tmp_path: Path, name: str) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "evil.zip", {name: b"x"})

    with pytest.raises(BackupError, match="unsafe"):
        restore_backup(cfg, archive, force=True)

    assert not (cfg.index.active_dir / "web_state.json").exists()


@pytest.mark.parametrize("slug", ["../x", "default", "UPPER", 7])
def test_restore_refuses_unsafe_names_in_the_station_list(tmp_path: Path, slug: object) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "evil.zip", {}, stations=[slug])  # type: ignore[list-item]

    with pytest.raises(BackupError, match=r"unsafe station name|no valid manifest"):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize(
    "name",
    [
        "stations/nightowl/index/categories.json",
        "stations/nightowl/index/library.db",
        "stations/nightowl/index/sub/web_state.json",
        "stations/nightowl/stereo-tool-presets/x.sts",
        "stations/nightowl/web_state.json",
        "stations/nightowl/liners",
    ],
)
def test_restore_refuses_files_a_station_does_not_keep(tmp_path: Path, name: str) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "odd.zip", {name: b"x"}, stations=["nightowl"])

    with pytest.raises(BackupError, match="unknown file"):
        restore_backup(cfg, archive, force=True)


def test_restore_refuses_two_folders_that_land_in_one_place(tmp_path: Path) -> None:
    shared = tmp_path / "liners"
    cfg = _config(
        tmp_path,
        extra=(
            f'\n[playback]\nliners_folder = "{shared.as_posix()}"\n'
            f'\n[stations.nightowl.playback]\nliners_folder = "{shared.as_posix()}"\n'
        ),
    )
    archive = _archive(
        tmp_path / "two.zip",
        {"liners/a.mp3": b"a", "stations/nightowl/liners/b.mp3": b"b"},
        stations=["nightowl"],
    )

    with pytest.raises(BackupError, match="would both restore into"):
        restore_backup(cfg, archive, force=True)

    assert not shared.exists()


def test_restore_without_force_refuses_existing_station_files(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    owl = _station_dir(cfg, "nightowl") / "web_state.json"
    owl.parent.mkdir(parents=True)
    owl.write_bytes(b"current")
    archive = _archive(
        tmp_path / "b.zip", {"stations/nightowl/index/web_state.json": b"saved"}, ["nightowl"]
    )

    with pytest.raises(BackupError, match="already exists"):
        restore_backup(cfg, archive, force=False)

    assert owl.read_bytes() == b"current"
    liners = _station_dir(cfg, "nightowl") / "liners"
    liners.mkdir()
    archive = _archive(
        tmp_path / "c.zip", {"stations/nightowl/liners/id.mp3": b"saved"}, ["nightowl"]
    )
    with pytest.raises(BackupError, match="already exists"):
        restore_backup(cfg, archive, force=False)


def test_an_archive_from_before_stations_restores_and_leaves_stations_alone(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    owl = _station_dir(cfg, "nightowl") / "schedule.json"
    owl.parent.mkdir(parents=True)
    owl.write_bytes(b"owl schedule")
    archive = _archive(tmp_path / "old.zip", {"index/schedule.json": b"default schedule"})

    restore_backup(cfg, archive, force=True)

    assert (cfg.index.active_dir / "schedule.json").read_bytes() == b"default schedule"
    assert owl.read_bytes() == b"owl schedule"


def test_restore_replaces_only_the_station_files_in_the_archive(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    folder = _station_dir(cfg, "nightowl")
    folder.mkdir(parents=True)
    (folder / "carts.json").write_bytes(b"current carts")
    archive = _archive(
        tmp_path / "part.zip",
        {"stations/nightowl/index/web_state.json": b"saved"},
        stations=["nightowl"],
    )

    restore_backup(cfg, archive, force=True)

    assert (folder / "web_state.json").read_bytes() == b"saved"
    assert (folder / "carts.json").read_bytes() == b"current carts"


def _link(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize("station", ["default", "nightowl"])
def test_backup_refuses_external_file_links(tmp_path: Path, station: str) -> None:
    cfg = _config(tmp_path)
    base = cfg.index.active_dir if station == "default" else _station_dir(cfg, station)
    liners = base / "liners"
    liners.mkdir(parents=True)
    (liners / "ordinary.wav").write_bytes(b"liner")
    secret = tmp_path / "private.txt"
    secret.write_bytes(b"private")
    link = liners / "linked.wav"
    _link(link, secret)
    archive = tmp_path / "backup.zip"
    with pytest.raises(BackupError, match="outside backup folder"):
        create_backup(cfg, archive, force=False)
    assert not archive.exists()
    link.unlink()
    create_backup(cfg, archive, force=False)
    with zipfile.ZipFile(archive) as opened:
        assert any(name.endswith("liners/ordinary.wav") for name in opened.namelist())
        assert all(opened.read(name) != b"private" for name in opened.namelist())


def test_backup_keeps_in_root_file_links_as_audio(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    liners = cfg.index.active_dir / "liners"
    liners.mkdir()
    original = liners / "original.wav"
    original.write_bytes(b"audio")
    _link(liners / "alias.wav", original)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("liners/alias.wav") == b"audio"


@pytest.mark.parametrize("station", ["default", "nightowl"])
def test_backup_refuses_linked_subfolders(tmp_path: Path, station: str) -> None:
    cfg = _config(tmp_path)
    base = cfg.index.active_dir if station == "default" else _station_dir(cfg, station)
    liners = base / "liners"
    liners.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.wav").write_bytes(b"private")
    _link(liners / "linked", outside, directory=True)
    with pytest.raises(BackupError, match="linked backup folder"):
        create_backup(cfg, tmp_path / "backup.zip", force=False)


@pytest.mark.parametrize("station", ["default", "nightowl"])
def test_state_links_cannot_copy_or_restore_external_data(tmp_path: Path, station: str) -> None:
    cfg = _config(tmp_path)
    base = cfg.index.active_dir if station == "default" else _station_dir(cfg, station)
    base.mkdir(parents=True, exist_ok=True)
    state = base / "web_state.json"
    state.write_text("{}")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    state.unlink()
    external = tmp_path / "private.json"
    external.write_text("private")
    _link(state, external)
    with pytest.raises(BackupError, match="outside backup folder"):
        create_backup(cfg, tmp_path / "unsafe.zip", force=False)
    with pytest.raises(BackupError, match="outside backup folder"):
        restore_backup(cfg, archive, force=True)
    assert external.read_text() == "private"


def test_backup_refuses_windows_junction(tmp_path: Path) -> None:
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction test")
    cfg = _config(tmp_path)
    liners = _station_dir(cfg, "nightowl") / "liners"
    liners.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.wav").write_bytes(b"private")
    junction = liners / "linked"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(BackupError, match="linked backup folder"):
            create_backup(cfg, tmp_path / "backup.zip", force=False)
        assert (outside / "private.wav").read_bytes() == b"private"
    finally:
        junction.rmdir()


def test_restore_refuses_archive_symlink_entries(tmp_path: Path) -> None:
    import stat

    cfg = _config(tmp_path)
    archive = tmp_path / "link.zip"
    name = "liners/id.wav"
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr(info, "../../private.txt")
        opened.writestr(
            "manifest.json",
            json.dumps(
                {
                    "autodj_version": current_version(),
                    "files": [name],
                }
            ),
        )
    with pytest.raises(BackupError, match="linked file in archive"):
        restore_backup(cfg, archive, force=True)
    assert not (cfg.index.active_dir / name).exists()
