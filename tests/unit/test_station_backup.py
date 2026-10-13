"""Backup and restore with several stations (#70): each station's folder and settings."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

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


def test_a_station_folder_holding_its_own_state_folder_is_refused_by_the_config(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=r"\[stations.nightowl\] playback.liners_folder .* holds"):
        _config(
            tmp_path,
            extra=f'\n[stations.nightowl.playback]\nliners_folder = "{tmp_path.as_posix()}/index"\n',
        )


@pytest.mark.parametrize(
    ("default", "owl"),
    [("shared/default", "shared"), ("shared", "shared/owl")],
    ids=["station holds the default's", "default holds the station's"],
)
def test_folders_that_hold_each_other_are_refused_at_backup(
    tmp_path: Path, default: str, owl: str
) -> None:
    cfg = _config(
        tmp_path,
        extra=(
            f'\n[playback]\nliners_folder = "{(tmp_path / default).as_posix()}"\n'
            f'\n[stations.nightowl.playback]\nliners_folder = "{(tmp_path / owl).as_posix()}"\n'
        ),
    )

    with pytest.raises(BackupError, match="overlap"):
        create_backup(cfg, tmp_path / "elsewhere" / "backup.zip", force=False)


def test_a_folder_holding_a_state_folder_is_refused_at_restore(tmp_path: Path) -> None:
    # The default station's liner folder set to the index folder itself: a
    # restore of liners would replace the whole index folder.
    cfg = _config(
        tmp_path,
        stations="",
        extra=f'\n[playback]\nliners_folder = "{(tmp_path / "index" / "default").as_posix()}"\n',
    )
    archive = _archive(tmp_path / "b.zip", {"liners/id.mp3": b"x"})

    with pytest.raises(BackupError, match="overlap"):
        restore_backup(cfg, archive, force=True)
    assert (cfg.index.active_dir).is_dir()
    assert not (cfg.index.active_dir / "id.mp3").exists()


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

    with pytest.raises(BackupError, match="overlap, so restoring one would replace the other"):
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


# ---------------------------------------------------------------------------
# Restore is all or nothing; backups hold one moment and nothing from outside
# ---------------------------------------------------------------------------


def _leftovers(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if ".old-" in path.name or ".restore-" in path.name
    )


@pytest.mark.parametrize("failing_put", [1, 3, 6, 12])
def test_a_failed_restore_puts_everything_back(tmp_path: Path, failing_put: int) -> None:
    """Folders and single files replaced before the failure come back as they were."""
    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    # The live data now differs from the backup in every file.
    current: dict[Path, bytes] = {}
    for path in [*cfg.index.active_dir.rglob("*"), *(cfg.index.index_dir / "profiles").rglob("*")]:
        if path.is_file() and path.suffix != ".lock" and path.name != "library.db":
            path.write_bytes(b"current " + path.name.encode())
            current[path] = path.read_bytes()

    from autodj import backup as backup_module

    real_put = backup_module._Swap.put
    calls = {"n": 0}

    def put(self, source, target):
        calls["n"] += 1
        if calls["n"] == failing_put:
            raise OSError("disk went away")
        return real_put(self, source, target)

    with (
        patch.object(backup_module._Swap, "put", put),
        pytest.raises(BackupError, match="nothing was changed: disk went away"),
    ):
        restore_backup(cfg, archive, force=True)

    for path, data in current.items():
        assert path.read_bytes() == data, path
    assert _leftovers(tmp_path / "index") == []


def test_a_restore_that_cannot_undo_says_where_the_old_files_are(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    from autodj import backup as backup_module

    real_put = backup_module._Swap.put
    calls = {"n": 0}

    def put(self, source, target):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk went away")
        return real_put(self, source, target)

    with (
        patch.object(backup_module._Swap, "put", put),
        patch.object(backup_module._Swap, "undo", lambda self: ["could not move x back"]),
        pytest.raises(BackupError, match=r"could not put everything back.*\.old-<id>"),
    ):
        restore_backup(cfg, archive, force=True)


def test_a_successful_restore_leaves_nothing_aside(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    restore_backup(cfg, archive, force=True)
    assert _leftovers(tmp_path / "index") == []


def test_state_files_are_one_moment_with_the_index(tmp_path: Path) -> None:
    """Edits made while the folders are read do not reach the archive's state files."""
    cfg = _config(tmp_path)
    written = _fill(cfg)
    owl = _station_dir(cfg, "nightowl") / "web_state.json"
    from autodj import backup as backup_module

    real = backup_module._regular_files

    def late_edit(root, pattern="**/*"):
        owl.write_bytes(b"edited after the snapshot")
        (cfg.index.active_dir / "schedule.json").write_bytes(b"edited too")
        return real(root, pattern)

    with patch.object(backup_module, "_regular_files", late_edit):
        archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("stations/nightowl/index/web_state.json") == written[owl]
        assert opened.read("index/schedule.json") == written[cfg.index.active_dir / "schedule.json"]


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot make symlinks here: {exc}")


def test_links_never_bring_outside_files_into_a_backup(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_bytes(b"not for the backup")
    owl = _station_dir(cfg, "nightowl")
    _symlink(owl / "liners" / "secret.mp3", secret)
    _symlink(owl / "carts" / "elsewhere", secret.parent)
    (owl / "listener_bans.json").unlink()
    _symlink(owl / "listener_bans.json", secret)

    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
        assert not any("secret" in name or "elsewhere" in name for name in names)
        assert "stations/nightowl/index/listener_bans.json" not in names
        assert all(opened.read(name) != b"not for the backup" for name in names)


@pytest.mark.parametrize(
    "name",
    [
        "liners/CON",
        "liners/con.mp3",
        "liners/Nul.txt",
        "liners/aux",
        "liners/sub/PRN.wav",
        "liners/COM1",
        "liners/lpt9.mp3",
        "stations/nightowl/liners/com3.mp3",
        "liners/trailing.",
        "liners/trailing ",
        "liners/dir./x.mp3",
        "liners/tab\tname.mp3",
    ],
)
def test_restore_refuses_names_windows_cannot_hold(tmp_path: Path, name: str) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "evil.zip", {name: b"x"}, stations=["nightowl"])

    with pytest.raises(BackupError, match="unsafe file name"):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize("name", ["liners/console.mp3", "liners/com.mp3", "liners/lpt10.mp3"])
def test_names_that_only_look_like_devices_are_fine(tmp_path: Path, name: str) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "ok.zip", {name: b"x"})
    restore_backup(cfg, archive, force=True)
    assert (cfg.index.active_dir / name).read_bytes() == b"x"


def test_restore_refuses_an_archive_bigger_than_the_cap(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "big.zip", {"liners/a.mp3": b"x" * 4096})
    from autodj import backup as backup_module

    with (
        patch.object(backup_module, "MAX_RESTORE_BYTES", 1024),
        pytest.raises(BackupError, match=r"more than the 0 GiB AutoDJ restores"),
    ):
        restore_backup(cfg, archive, force=True)
    assert not (cfg.index.active_dir / "liners").exists()


def test_restore_refuses_an_archive_bigger_than_the_free_space(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "big.zip", {"liners/a.mp3": b"x" * 4096})
    usage = shutil.disk_usage(tmp_path)._replace(free=1024)

    with (
        patch("autodj.backup.shutil.disk_usage", return_value=usage),
        pytest.raises(BackupError, match="not enough free space to restore"),
    ):
        restore_backup(cfg, archive, force=True)
    assert not (cfg.index.active_dir / "liners").exists()


def test_a_shared_folder_restored_where_it_is_no_longer_shared_is_warned_about(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "old" / "liners"
    old = _config(
        tmp_path / "old",
        extra=(
            f'\n[playback]\nliners_folder = "{shared.as_posix()}"\n'
            f'\n[stations.nightowl.playback]\nliners_folder = "{shared.as_posix()}"\n'
        ),
    )
    shared.mkdir()
    (shared / "id.mp3").write_bytes(b"both")
    archive = create_backup(old, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert json.loads(opened.read("manifest.json"))["shared"] == {
            "stations/nightowl/liners": "liners"
        }

    apart = _config(tmp_path / "new")
    warnings: list[str] = []
    restore_backup(apart, archive, force=True, warnings=warnings)

    assert (apart.index.active_dir / "liners" / "id.mp3").read_bytes() == b"both"
    assert len(warnings) == 1
    assert "stations/nightowl/liners shared the liners folder" in warnings[0]
    assert str(_station_dir(apart, "nightowl") / "liners") in warnings[0]

    # Restored where they are still shared, nothing is said.
    warnings.clear()
    restore_backup(old, archive, force=True, warnings=warnings)
    assert warnings == []


def test_the_restore_command_prints_the_warnings(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from autodj.cli import cli

    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "b.zip", {"liners/a.mp3": b"x"})

    def restore(cfg, archive, *, force, warnings):
        warnings.append("In this backup a folder was shared.")
        return 1

    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.backup.restore_backup", restore),
        patch("autodj.doctor.run_doctor") as doctor,
    ):
        doctor.return_value.exit_code = 0
        doctor.return_value.checks = ()
        result = CliRunner().invoke(cli, ["restore", str(archive)])
    assert "Warning: In this backup a folder was shared." in result.output
