"""Backup and restore with several stations (#70): each station's folder and settings."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from autodj.backup import STATION_FILES, BackupError, create_backup, restore_backup
from autodj.catalog import LibraryCatalog
from autodj.config import AutoDJConfig, load_config
from autodj.live import strip_secrets
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
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    with LibraryCatalog(cfg.index.library_dir / "library.db"):
        pass
    # The fixture only needs the main database; WAL sidecars are transient and
    # would make rollback assertions depend on SQLite's connection timing.
    for suffix in ("-shm", "-wal"):
        (cfg.index.library_dir / f"library.db{suffix}").unlink(missing_ok=True)
    return cfg


def _station_dir(cfg: AutoDJConfig, slug: str) -> Path:
    return cfg.index.stations_dir / slug


def _fill(cfg: AutoDJConfig) -> dict[Path, bytes]:
    """Give both stations different state files and folders; return what was written."""
    written: dict[Path, bytes] = {}

    def put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written[path] = data

    for base, who in (
        ((cfg.index.stations_dir / "default"), b"default"),
        (_station_dir(cfg, "nightowl"), b"owl"),
    ):
        for name in STATION_FILES:
            if name == "station.json":
                continue
            put(base / name, b'{"station": "' + who + b'", "file": "' + name.encode() + b'"}')
        # Not in *written*: a backup never holds it, so a restore never brings it.
        (base / "relays.json").write_bytes(b'{"password": "' + who + b'-relay-secret"}')
        # Live DJ accounts (#48): the backup keeps them without their hashes,
        # so that is what a restore puts back.
        djs = json.dumps(
            {
                "version": 1,
                "users": {"dj": {"name": who.decode(), "salt": "ab" * 16, "hash": "cd" * 64}},
                "settings": {"mic_return_seconds": 1.0, "mic_duck_db": 9.0},
            }
        ).encode()
        put(base / "live_djs.json", djs)
        written[base / "live_djs.json"] = strip_secrets(djs)
        put(base / "liners" / "id.mp3", who + b" liner")
        put(base / "carts" / "jingle.wav", who + b" cart")
        # The default station's clips are the shared time check packs.
        clips = cfg.index.shared_dir if who == b"default" else base
        put(clips / "time_checks" / "03_05.wav", who + b" clip")
        put(base / "clockwheels" / "Night.json", who + b" wheel")
        put(base / "intros" / "intro.wav", who + b" intro")
    put(cfg.index.stations_dir / "default" / "profiles" / "Party.json", b"default profile")
    put(_station_dir(cfg, "nightowl") / "profiles" / "Party.json", b"owl profile")
    return written


def test_two_stations_round_trip(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _config(tmp_path)
    written = _fill(cfg)

    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    with zipfile.ZipFile(archive) as opened:
        names = set(opened.namelist())
        manifest = json.loads(opened.read("manifest.json"))
        contents = b"".join(opened.read(name) for name in opened.namelist())
        default_live_djs = opened.read("index/live_djs.json")
        owl_live_djs = opened.read("stations/nightowl/index/live_djs.json")
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
        "stations/nightowl/intros/intro.wav",
        "stations/nightowl/index/matched_intros.json",
        "stations/nightowl/index/intro_rules.json",
        "stations/nightowl/index/live_djs.json",
        "stations/nightowl/profiles/Party.json",
    } <= names
    # Relay settings hold source passwords: a backup never holds them.
    assert "index/relays.json" not in names
    assert "stations/nightowl/index/relays.json" not in names
    assert b"relay-secret" not in contents
    assert "index/live_djs.json" in names
    assert default_live_djs == written[cfg.index.stations_dir / "default" / "live_djs.json"]
    assert owl_live_djs == written[_station_dir(cfg, "nightowl") / "live_djs.json"]
    for djs in (default_live_djs, owl_live_djs):
        assert b"hash" not in djs and b"salt" not in djs and b'"dj"' in djs
    assert "relay-secret" not in caplog.text
    # Nothing is stored twice.
    assert not any(name.startswith("stations/nightowl/stations/") for name in names)

    for path in written:
        path.unlink()
    assert restore_backup(cfg, archive, force=True) == len(manifest["files"])

    for path, data in written.items():
        assert path.read_bytes() == data, path
    # The live relay settings were left alone.
    assert ((cfg.index.stations_dir / "default") / "relays.json").read_bytes() == (
        b'{"password": "default-relay-secret"}'
    )
    assert (_station_dir(cfg, "nightowl") / "relays.json").read_bytes() == (
        b'{"password": "owl-relay-secret"}'
    )


def test_restore_skips_relay_settings_in_an_archive_that_has_them(tmp_path: Path) -> None:
    """An archive from a build that backed relays.json up never replaces the live one."""
    cfg = _config(tmp_path)
    written = _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    old = tmp_path / "old.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(old, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        for name in source.namelist():
            if name != "manifest.json":
                target.writestr(source.getinfo(name), source.read(name))
        stale = {
            "index/relays.json": b'{"password": "stale-default"}',
            "stations/nightowl/index/relays.json": b'{"password": "stale-owl"}',
        }
        for name, data in stale.items():
            target.writestr(name, data)
        manifest["files"] = [*manifest["files"], *stale]
        target.writestr("manifest.json", json.dumps(manifest))

    warnings: list[str] = []
    assert restore_backup(cfg, old, force=True, warnings=warnings) == len(manifest["files"]) - 2
    assert any("Relay settings" in warning for warning in warnings)
    for path, data in written.items():
        assert path.read_bytes() == data, path
    assert (
        b"default-relay-secret"
        in ((cfg.index.stations_dir / "default") / "relays.json").read_bytes()
    )
    assert b"owl-relay-secret" in (_station_dir(cfg, "nightowl") / "relays.json").read_bytes()


def test_restore_to_a_new_machine_creates_the_station_folder(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "old")
    written = _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    fresh = _config(tmp_path / "new")
    # A new machine has configuration directories but has not indexed yet.
    (fresh.index.library_dir / "library.db").unlink()
    restore_backup(fresh, archive, force=False)

    for path, data in written.items():
        moved = tmp_path / "new" / path.relative_to(tmp_path / "old")
        assert moved.read_bytes() == data


def test_a_station_folder_set_elsewhere_is_restored_there(tmp_path: Path) -> None:
    liners = tmp_path / "owl-liners"
    cfg = _config(
        tmp_path,
        extra=f'\n[stations.nightowl.time_checks]\nclips_folder = "{liners.as_posix()}"\n',
    )
    liners.mkdir()
    (liners / "owl.mp3").write_bytes(b"owl")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("stations/nightowl/time_checks/owl.mp3") == b"owl"
    (liners / "owl.mp3").unlink()

    restore_backup(cfg, archive, force=True)

    assert (liners / "owl.mp3").read_bytes() == b"owl"


def test_a_shared_clip_folder_is_stored_once(tmp_path: Path) -> None:
    shared = tmp_path / "liners"
    cfg = _config(
        tmp_path,
        extra=(
            f'\n[time_checks]\nclips_folder = "{shared.as_posix()}"\n'
            f'\n[stations.nightowl.time_checks]\nclips_folder = "{shared.as_posix()}"\n'
        ),
    )
    shared.mkdir()
    (shared / "id.mp3").write_bytes(b"both")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
    assert "time_checks/id.mp3" in names
    assert not [name for name in names if name.startswith("stations/nightowl/time_checks/")]
    (shared / "id.mp3").unlink()

    restore_backup(cfg, archive, force=True)

    assert (shared / "id.mp3").read_bytes() == b"both"


def test_a_station_folder_holding_its_own_state_folder_is_refused_by_the_config(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError, match=r"\[stations.nightowl\] time_checks.clips_folder .* holds"
    ):
        _config(
            tmp_path,
            extra=f'\n[stations.nightowl.time_checks]\nclips_folder = "{tmp_path.as_posix()}/index"\n',
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
            f'\n[time_checks]\nclips_folder = "{(tmp_path / default).as_posix()}"\n'
            f'\n[stations.nightowl.time_checks]\nclips_folder = "{(tmp_path / owl).as_posix()}"\n'
        ),
    )

    with pytest.raises(BackupError, match="overlap"):
        create_backup(cfg, tmp_path / "elsewhere" / "backup.zip", force=False)


def test_a_folder_holding_a_state_folder_is_refused_at_restore(tmp_path: Path) -> None:
    # The default station's clip folder set to its own station folder: a
    # restore of clips would replace the whole station folder.
    cfg = _config(
        tmp_path,
        stations="",
        extra=f'\n[time_checks]\nclips_folder = "{(tmp_path / "index" / "stations" / "default").as_posix()}"\n',
    )
    archive = _archive(tmp_path / "b.zip", {"time_checks/id.mp3": b"x"})

    with pytest.raises(BackupError, match="overlap"):
        restore_backup(cfg, archive, force=True)
    assert (cfg.index.stations_dir / "default").is_dir()
    assert not ((cfg.index.stations_dir / "default") / "id.mp3").exists()


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
    assert not ((single.index.stations_dir / "default") / "web_state.json").exists()
    assert not ((single.index.stations_dir / "default") / "stations").exists()


def _archive(path: Path, files: dict[str, bytes], stations: list[str] | None = None) -> Path:
    files = dict(files)
    if "index/library.db" not in files:
        with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
            catalog = Path(temporary) / "library.db"
            with LibraryCatalog(catalog):
                pass
            files["index/library.db"] = catalog.read_bytes()
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


def test_restore_accepts_web_station_definition(tmp_path: Path) -> None:
    cfg = _config(tmp_path, stations="")
    definition = {
        "version": 1,
        "created": "2026-01-01T00:00:00+00:00",
        "source": None,
        "station": {
            "autostart": False,
            "stream": {"station_name": "Web Radio"},
            "recording": {"folder": str(cfg.recording.folder / "web")},
        },
    }
    archive = _archive(
        tmp_path / "web.zip",
        {"stations/web/index/station.json": json.dumps(definition).encode()},
        stations=["web"],
    )
    restore_backup(cfg, archive, force=True)
    assert load_config(cfg.config_path, environ={}).stations[0].defined_in == "web"


def test_backup_carries_web_station_definition(tmp_path: Path) -> None:
    cfg = _config(tmp_path, stations="")
    definition = {
        "version": 1,
        "created": "2026-01-01T00:00:00+00:00",
        "source": None,
        "station": {
            "autostart": False,
            "stream": {"station_name": "Web Radio"},
            "recording": {"folder": str(cfg.recording.folder / "web")},
        },
    }
    path = cfg.index.stations_dir / "web" / "station.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(definition), encoding="utf-8")
    loaded = load_config(cfg.config_path, environ={})
    archive = create_backup(loaded, tmp_path / "web.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert json.loads(opened.read("stations/web/index/station.json")) == definition


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

    assert not ((cfg.index.stations_dir / "default") / "web_state.json").exists()


@pytest.mark.parametrize("slug", ["../x", "default", "UPPER", 7])
def test_restore_refuses_unsafe_names_in_the_station_list(tmp_path: Path, slug: object) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "evil.zip", {}, stations=[slug])  # type: ignore[list-item]

    with pytest.raises(BackupError, match=r"unsafe station name|no valid manifest"):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize(
    "name",
    [
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
            f'\n[time_checks]\nclips_folder = "{shared.as_posix()}"\n'
            f'\n[stations.nightowl.time_checks]\nclips_folder = "{shared.as_posix()}"\n'
        ),
    )
    archive = _archive(
        tmp_path / "two.zip",
        {"time_checks/a.mp3": b"a", "stations/nightowl/time_checks/b.mp3": b"b"},
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

    assert (
        (cfg.index.stations_dir / "default") / "schedule.json"
    ).read_bytes() == b"default schedule"
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
    for path in [
        *cfg.index.index_dir.rglob("*"),
        *(cfg.index.stations_dir / "default" / "profiles").rglob("*"),
    ]:
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
        pytest.raises(BackupError, match=r"could not put everything back.*autodj doctor"),
    ):
        restore_backup(cfg, archive, force=True)


def test_a_successful_restore_leaves_nothing_aside(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    restore_backup(cfg, archive, force=True)
    assert _leftovers(tmp_path / "index") == []


def test_state_files_are_copied_together_under_the_index_lock(tmp_path: Path) -> None:
    """Edits made while the archive is written do not reach its state files."""
    from contextlib import contextmanager

    from autodj import backup as backup_module

    cfg = _config(tmp_path)
    written = _fill(cfg)
    owl = _station_dir(cfg, "nightowl") / "web_state.json"
    real_write = zipfile.ZipFile.write

    def write(self, filename, arcname=None, *args, **kwargs):
        owl.write_bytes(b"edited after the snapshot")
        ((cfg.index.stations_dir / "default") / "schedule.json").write_bytes(b"edited too")
        return real_write(self, filename, arcname, *args, **kwargs)

    with patch.object(zipfile.ZipFile, "write", write):
        archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("stations/nightowl/index/web_state.json") == written[owl]
        assert (
            opened.read("index/schedule.json")
            == written[cfg.index.stations_dir / "default" / "schedule.json"]
        )

    held = {"lock": False, "copied_outside": []}
    real_lock = backup_module.im.publication_lock
    real_copy = backup_module.shutil.copyfile

    @contextmanager
    def lock(path):
        with real_lock(path):
            held["lock"] = True
            try:
                yield
            finally:
                held["lock"] = False

    def copyfile(source, target, *args, **kwargs):
        if Path(source).name in backup_module.STATION_FILES and not held["lock"]:
            held["copied_outside"].append(str(source))
        return real_copy(source, target, *args, **kwargs)

    with (
        patch.object(backup_module.im, "publication_lock", lock),
        patch.object(backup_module.shutil, "copyfile", copyfile),
    ):
        create_backup(cfg, tmp_path / "again.zip", force=False)
    assert held["copied_outside"] == []


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
    assert ((cfg.index.stations_dir / "default") / name).read_bytes() == b"x"


def test_restore_refuses_an_archive_bigger_than_the_cap(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "big.zip", {"liners/a.mp3": b"x" * 4096})
    from autodj import backup as backup_module

    with (
        patch.object(backup_module, "MAX_RESTORE_BYTES", 1024),
        pytest.raises(BackupError, match=r"more than the 0 GiB AutoDJ restores"),
    ):
        restore_backup(cfg, archive, force=True)
    assert not ((cfg.index.stations_dir / "default") / "liners").exists()


def test_restore_refuses_an_archive_bigger_than_the_free_space(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "big.zip", {"liners/a.mp3": b"x" * 4096})
    usage = shutil.disk_usage(tmp_path)._replace(free=1024)

    with (
        patch("autodj.backup.shutil.disk_usage", return_value=usage),
        pytest.raises(BackupError, match="not enough free space to restore"),
    ):
        restore_backup(cfg, archive, force=True)
    assert not ((cfg.index.stations_dir / "default") / "liners").exists()


def test_a_shared_folder_restored_where_it_is_no_longer_shared_is_warned_about(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "old" / "clips"
    old = _config(
        tmp_path / "old",
        extra=(
            f'\n[time_checks]\nclips_folder = "{shared.as_posix()}"\n'
            f'\n[stations.nightowl.time_checks]\nclips_folder = "{shared.as_posix()}"\n'
        ),
    )
    shared.mkdir()
    (shared / "id.mp3").write_bytes(b"both")
    archive = create_backup(old, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert json.loads(opened.read("manifest.json"))["shared"] == {
            "stations/nightowl/time_checks": "time_checks"
        }

    apart = _config(tmp_path / "new")
    warnings: list[str] = []
    restore_backup(apart, archive, force=True, warnings=warnings)

    assert (apart.index.shared_dir / "time_checks" / "id.mp3").read_bytes() == b"both"
    assert len(warnings) == 1
    assert "stations/nightowl/time_checks shared the time_checks folder" in warnings[0]
    assert str(_station_dir(apart, "nightowl") / "time_checks") in warnings[0]

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


# ---------------------------------------------------------------------------
# Links (#193): a link that leaves its folder is refused; one inside is fine
# ---------------------------------------------------------------------------


def _link(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.parametrize("station", ["default", "nightowl"])
def test_backup_refuses_a_file_link_that_leaves_its_folder(tmp_path: Path, station: str) -> None:
    cfg = _config(tmp_path)
    base = (
        (cfg.index.stations_dir / "default") if station == "default" else _station_dir(cfg, station)
    )
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


def test_backup_keeps_a_file_link_inside_its_folder(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    liners = (cfg.index.stations_dir / "default") / "liners"
    liners.mkdir()
    original = liners / "original.wav"
    original.write_bytes(b"audio")
    _link(liners / "alias.wav", original)

    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    with zipfile.ZipFile(archive) as opened:
        assert opened.read("liners/alias.wav") == b"audio"


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("station", ["default", "nightowl"])
def test_backup_refuses_a_linked_subfolder(tmp_path: Path, station: str, kind: str) -> None:
    cfg = _config(tmp_path)
    base = (
        (cfg.index.stations_dir / "default") if station == "default" else _station_dir(cfg, station)
    )
    liners = base / "liners"
    liners.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.wav").write_bytes(b"private")
    if kind == "junction":
        _junction(liners / "linked", outside)
    else:
        _link(liners / "linked", outside, directory=True)

    with pytest.raises(BackupError, match="linked backup folder"):
        create_backup(cfg, tmp_path / "backup.zip", force=False)
    assert (outside / "private.wav").read_bytes() == b"private"


@pytest.mark.parametrize("station", ["default", "nightowl"])
def test_state_links_cannot_copy_or_restore_outside_data(tmp_path: Path, station: str) -> None:
    cfg = _config(tmp_path)
    base = (
        (cfg.index.stations_dir / "default") if station == "default" else _station_dir(cfg, station)
    )
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


def test_restore_refuses_a_link_stored_in_the_archive(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("liners/link.wav")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")
        archive.writestr(
            "manifest.json",
            json.dumps({"autodj_version": current_version(), "files": ["liners/link.wav"]}),
        )

    with pytest.raises(BackupError, match="linked file in archive"):
        restore_backup(cfg, path, force=True)


def test_a_junction_replaced_by_a_restore_is_removed_as_a_link(tmp_path: Path) -> None:
    """Deleting what a restore set aside never walks into a junction's target."""
    cfg = _config(tmp_path, stations="")
    real = tmp_path / "real-liners"
    real.mkdir()
    (real / "keep.wav").write_bytes(b"keep")
    _junction((cfg.index.stations_dir / "default") / "liners", real)
    archive = _archive(tmp_path / "b.zip", {"liners/new.wav": b"new"})

    restore_backup(cfg, archive, force=True)

    assert ((cfg.index.stations_dir / "default") / "liners" / "new.wav").read_bytes() == b"new"
    assert not ((cfg.index.stations_dir / "default") / "liners").is_junction()
    assert (real / "keep.wav").read_bytes() == b"keep"
    assert _leftovers(tmp_path / "index") == []


@pytest.mark.parametrize("kind", ["liner", "state", "preset"])
def test_backup_refuses_hard_linked_files(tmp_path: Path, kind: str) -> None:
    cfg = _config(tmp_path)
    external = tmp_path / "private.bin"
    external.write_bytes(b"private")
    relative = {"liner": "liners/alias.wav", "state": "web_state.json"}
    path = (
        (cfg.index.stations_dir / "default") / relative[kind]
        if kind != "preset"
        else cfg.index.shared_dir / "stereo-tool-presets/alias.sts"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.hardlink_to(external)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    archive = tmp_path / "unsafe.zip"
    with pytest.raises(BackupError, match="hard-linked backup file"):
        create_backup(cfg, archive, force=False)
    assert not archive.exists()
    assert external.read_bytes() == b"private"


# ---------------------------------------------------------------------------
# Restore safety, second review: journals, crashes, locks and leftovers
# ---------------------------------------------------------------------------


def _snapshot_files(root: Path) -> dict[str, bytes]:
    """Every file under *root* and its bytes, ignoring locks and the journal."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix != ".lock"
        and path.name != ".autodj-restore.json"
        and ".db-shm" not in path.name
        and ".db-wal" not in path.name
    }


def _live_state(cfg: AutoDJConfig) -> dict[str, bytes]:
    """The data a restore may touch: the index folder and the shared profiles."""
    return {**_snapshot_files(cfg.index.index_dir)}


def _changed_since_backup(cfg: AutoDJConfig) -> None:
    for path in [
        *cfg.index.index_dir.rglob("*"),
        *(cfg.index.stations_dir / "default" / "profiles").rglob("*"),
    ]:
        if (
            path.is_file()
            and path.suffix not in (".lock", ".db")
            and ".db-shm" not in path.name
            and ".db-wal" not in path.name
        ):
            path.write_bytes(b"current " + path.name.encode())


@pytest.mark.parametrize("target_name", ["liners", "library.db", "web_state.json"])
def test_failed_replacement_restores_the_data(tmp_path: Path, target_name: str) -> None:
    """A failure after an asset, catalog or later state file is moved is undone."""
    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    _changed_since_backup(cfg)
    before = _live_state(cfg)
    from autodj import backup as backup_module

    real_put = backup_module._Swap.put
    target = (
        (
            cfg.index.library_dir
            if target_name == "library.db"
            else cfg.index.stations_dir / "default"
        )
        / target_name
        if target_name in {"library.db", "web_state.json"}
        else (cfg.index.stations_dir / "default") / "liners"
    )
    raised = False

    def fail_after_put(swap, source, destination):
        nonlocal raised
        real_put(swap, source, destination)
        if destination == target and not raised:
            raised = True
            raise OSError(f"injected failure after replacing {target_name}")

    with (
        patch.object(backup_module._Swap, "put", fail_after_put),
        pytest.raises(BackupError, match="nothing was changed"),
    ):
        restore_backup(cfg, archive, force=True)
    assert raised
    assert _live_state(cfg) == before
    assert _leftovers(tmp_path / "index") == []
    assert not (cfg.index.library_dir / ".autodj-restore.json").exists()


class _Crash(BaseException):
    """The process dies here: no undo, no clean-up."""


@pytest.mark.parametrize("target_name", ["liners", "library.db", "web_state.json"])
def test_a_crash_part_way_is_rolled_back_by_doctor(tmp_path: Path, target_name: str) -> None:
    from autodj import backup as backup_module
    from autodj.backup import pending_restore_state, recover_restore, restore_leftovers
    from autodj.doctor import CheckStatus, run_doctor

    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    _changed_since_backup(cfg)
    before = _live_state(cfg)
    target = (
        (
            cfg.index.library_dir
            if target_name == "library.db"
            else cfg.index.stations_dir / "default"
        )
        / target_name
        if target_name in {"library.db", "web_state.json"}
        else (cfg.index.stations_dir / "default") / "liners"
    )
    real_put = backup_module._Swap.put

    def crash_after_put(swap, source, destination):
        real_put(swap, source, destination)
        if destination == target:
            raise _Crash

    with (
        patch.object(backup_module._Swap, "put", crash_after_put),
        patch.object(backup_module._Swap, "undo", lambda self: []),
        patch("autodj.backup.shutil.rmtree"),
        pytest.raises(_Crash),
    ):
        restore_backup(cfg, archive, force=True)

    assert pending_restore_state(cfg.index.library_dir) == "installing"
    check = next(c for c in run_doctor(cfg, python_version=(3, 14)).checks if c.name == "restore")
    assert (check.status, check.repair) == (CheckStatus.FAIL, "recover-restore")
    with pytest.raises(BackupError, match="did not finish"):
        restore_backup(cfg, archive, force=True)

    keep = tmp_path / "kept"
    done = recover_restore(cfg, keep)

    assert any("rolled back" in line for line in done)
    assert _live_state(cfg) == before
    assert pending_restore_state(cfg.index.library_dir) is None
    assert not restore_leftovers(cfg)
    # The rollback deleted nothing: what it moved is in the kept folder.
    assert keep.is_dir()


def test_a_crash_after_every_file_is_in_place_is_finished_by_doctor(tmp_path: Path) -> None:
    from autodj import backup as backup_module
    from autodj.backup import pending_restore_state, recover_restore

    cfg = _config(tmp_path)
    written = _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    _changed_since_backup(cfg)

    def crash(self):
        raise _Crash

    with (
        patch.object(backup_module._Swap, "commit", crash),
        patch("autodj.backup.shutil.rmtree"),
        pytest.raises(_Crash),
    ):
        restore_backup(cfg, archive, force=True)

    assert pending_restore_state(cfg.index.library_dir) == "installed"
    for path, data in written.items():
        assert path.read_bytes() == data
    keep = tmp_path / "kept"
    done = recover_restore(cfg, keep)
    assert any("already complete" in line for line in done)
    assert _leftovers(tmp_path / "index") == []
    assert pending_restore_state(cfg.index.library_dir) is None
    for path, data in written.items():
        assert path.read_bytes() == data
    assert any(".old-" in path.name or "old-" in path.name for path in keep.iterdir())


def test_old_copies_that_cannot_be_deleted_do_not_fail_a_finished_restore(tmp_path: Path) -> None:
    from autodj.backup import pending_restore_state, recover_restore
    from autodj.doctor import CheckStatus, run_doctor

    cfg = _config(tmp_path)
    written = _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    _changed_since_backup(cfg)
    warnings: list[str] = []

    with patch("autodj.backup._remove", side_effect=OSError("in use")):
        restore_backup(cfg, archive, force=True, warnings=warnings)

    for path, data in written.items():
        assert path.read_bytes() == data
    assert len(warnings) == 1
    assert warnings[0].startswith("The restore is complete, but these old copies could not")
    assert pending_restore_state(cfg.index.library_dir) == "installed"
    check = next(c for c in run_doctor(cfg, python_version=(3, 14)).checks if c.name == "restore")
    assert check.status is CheckStatus.WARN
    recover_restore(cfg, tmp_path / "kept")
    assert _leftovers(tmp_path / "index") == []
    assert pending_restore_state(cfg.index.library_dir) is None


def test_a_failed_undo_names_where_each_old_file_is(tmp_path: Path) -> None:
    from autodj import backup as backup_module
    from autodj.backup import pending_restore_state, recover_restore

    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    _changed_since_backup(cfg)
    before = _live_state(cfg)
    real = os.replace
    state = {"installing": True, "failed_puts": 0}

    def replace(source, target):
        name = Path(target).name
        if state["installing"] and name == "web_state.json" and ".restore-" in str(source):
            state["installing"] = False
            raise OSError("disk went away")
        if not state["installing"] and ".old-" in Path(source).name and state["failed_puts"] < 1:
            state["failed_puts"] += 1
            raise OSError("still busy")
        return real(source, target)

    with (
        patch("autodj.backup.os.replace", replace),
        pytest.raises(BackupError, match=r"could not put everything back: .* is still at .*\.old-"),
    ):
        restore_backup(cfg, archive, force=True)

    # The journal stays, so doctor finishes the rollback.
    assert pending_restore_state(cfg.index.library_dir) == "installing"
    recover_restore(cfg, tmp_path / "kept")
    assert _live_state(cfg) == before
    assert pending_restore_state(cfg.index.library_dir) is None
    assert backup_module._read_journal(cfg.index.library_dir) is None


def test_stray_copies_without_a_journal_are_reported_and_moved_aside(tmp_path: Path) -> None:
    from autodj.backup import recover_restore
    from autodj.doctor import CheckStatus, run_doctor

    cfg = _config(tmp_path)
    stray = (cfg.index.stations_dir / "default") / f".liners.old-{'a' * 32}"
    stray.mkdir()
    (stray / "old.mp3").write_bytes(b"old")
    check = next(c for c in run_doctor(cfg, python_version=(3, 14)).checks if c.name == "restore")
    assert check.status is CheckStatus.WARN
    assert check.detail["strays"] == [str(stray)]
    assert check.summary == "1 hidden copy left by an earlier restore"

    keep = tmp_path / "kept"
    recover_restore(cfg, keep)

    assert not stray.exists()
    assert [path.read_bytes() for path in keep.rglob("old.mp3")] == [b"old"]
    assert all(c.name != "restore" for c in run_doctor(cfg, python_version=(3, 14)).checks)


def test_no_restore_check_without_leftovers(tmp_path: Path) -> None:
    from autodj.doctor import run_doctor

    cfg = _config(tmp_path)
    assert all(c.name != "restore" for c in run_doctor(cfg, python_version=(3, 14)).checks)


def test_restore_refuses_while_autodj_serves_the_index(tmp_path: Path) -> None:
    from autodj.backup import hold_serve_lock, server_running

    cfg = _config(tmp_path)
    _fill(cfg)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    lock = hold_serve_lock(cfg.index.library_dir)
    try:
        assert server_running(cfg.index.library_dir)
        with pytest.raises(BackupError, match=r"AutoDJ is serving .*; stop it before restoring"):
            restore_backup(cfg, archive, force=True)
        with pytest.raises(BackupError, match="already serving"):
            hold_serve_lock(cfg.index.library_dir)
    finally:
        lock.release()
    assert not server_running(cfg.index.library_dir)
    restore_backup(cfg, archive, force=True)


def test_sqlite_journals_are_never_left_beside_a_restored_database(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    cfg = _config(tmp_path, stations="")
    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as db:
        db.execute("CREATE TABLE t(x)")
        db.execute("INSERT INTO t VALUES (1)")
        db.commit()
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert not [
            name for name in opened.namelist() if name.endswith(("-journal", "-wal", "-shm"))
        ]
    hot = cfg.index.library_dir / "library.db-journal"
    hot.write_bytes(b"a stale hot journal")

    restore_backup(cfg, archive, force=True)

    assert not hot.exists()
    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as db:
        assert db.execute("SELECT x FROM t").fetchall() == [(1,)]


@pytest.mark.parametrize(
    "name",
    [
        "liners/CONIN$",
        "liners/conout$.mp3",
        "liners/CLOCK$",
        "liners/what?.mp3",
        "liners/star*.mp3",
        'liners/quote".mp3',
        "liners/a<b.mp3",
        "liners/a>b.mp3",
        "liners/a|b.mp3",
    ],
)
def test_restore_refuses_more_names_windows_cannot_hold(tmp_path: Path, name: str) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "evil.zip", {name: b"x"})
    with pytest.raises(BackupError, match="unsafe file name"):
        restore_backup(cfg, archive, force=True)


def test_autodjs_own_temporary_files_stay_out_of_a_backup(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    liners = (cfg.index.stations_dir / "default") / "liners"
    (liners / f".id.mp3.{'b' * 32}.tmp").write_bytes(b"half written")
    aside = liners / f".sub.old-{'c' * 32}"
    aside.mkdir()
    (aside / "x.mp3").write_bytes(b"old")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
    assert not [name for name in names if ".tmp" in name or ".old-" in name]
    assert "liners/id.mp3" in names


def test_backup_refuses_when_there_is_no_room(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    usage = shutil.disk_usage(tmp_path)._replace(free=1024)
    with (
        patch("autodj.backup.shutil.disk_usage", return_value=usage),
        pytest.raises(BackupError, match="not enough free space for the snapshot"),
    ):
        create_backup(cfg, tmp_path / "backup.zip", force=False)
    assert not (tmp_path / "backup.zip").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_private_file_comes_back_private(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _fill(cfg)
    bans = _station_dir(cfg, "nightowl") / "listener_bans.json"
    bans.chmod(0o600)
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    bans.unlink()
    restore_backup(cfg, archive, force=True)
    assert bans.stat().st_mode & 0o777 == 0o600


def test_undo_removes_the_folders_it_made_innermost_first(tmp_path: Path) -> None:
    from autodj.backup import _Swap

    swap = _Swap(tmp_path / "journal.json", [])
    swap.start()
    deep = tmp_path / "a" / "b" / "c"
    swap.make_dir(deep)
    assert deep.is_dir()
    assert swap.undo() == []
    assert not (tmp_path / "a").exists()
    assert not (tmp_path / "journal.json").exists()


def test_each_stations_shows_are_backed_up_and_restored(tmp_path: Path) -> None:
    """Shows (#106) are a station's own: each station gets its own shows.json back."""
    cfg = _config(tmp_path)
    assert "shows.json" in STATION_FILES
    shows = {
        (cfg.index.stations_dir / "default") / "shows.json": {
            "schema_version": 1,
            "shows": {"a" * 32: "day"},
        },
        _station_dir(cfg, "nightowl") / "shows.json": {
            "schema_version": 1,
            "shows": {"b" * 32: "night"},
        },
    }
    for path, document in shows.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "shows.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        names = set(opened.namelist())
    assert {"index/shows.json", "stations/nightowl/index/shows.json"} <= names
    for path in shows:
        path.unlink()
    restore_backup(cfg, archive, force=True)
    for path, document in shows.items():
        assert json.loads(path.read_text(encoding="utf-8")) == document, path


def test_a_restore_forgets_every_stations_show_on_the_air(tmp_path: Path) -> None:
    """show_on_air.json (#106) is runtime state: never backed up, cleared by a restore."""
    cfg = _config(tmp_path)
    marks = [
        (cfg.index.stations_dir / "default") / "show_on_air.json",
        _station_dir(cfg, "nightowl") / "show_on_air.json",
    ]
    for mark in marks:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text('{"version": 1}', encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "air.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert not any(name.endswith("show_on_air.json") for name in opened.namelist())
    restore_backup(cfg, archive, force=True)
    assert not any(mark.exists() for mark in marks)


def test_stereo_tool_keys_and_program_stay_out_of_backups(tmp_path: Path) -> None:
    from autodj import stereotool_setup as setup

    cfg = _config(tmp_path)
    _fill(cfg)
    key = "<STEREO-TOOL-BACKUP-SECRET-1234>"
    setup.save_license(setup.install_license_path(cfg.index.index_dir), key)
    setup.save_station_license(cfg.index.stations_dir / "default", key)
    # An install not yet upgraded still has the old file beside its secrets.json.
    setup.save_station_license(_station_dir(cfg, "nightowl"), key)
    setup.old_station_license_path(_station_dir(cfg, "nightowl")).write_text(key, "ascii")
    program = setup.managed_path(cfg.index.index_dir)
    program.parent.mkdir(parents=True)
    program.write_bytes(b"\x7fELF stereo tool program bytes")

    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
        contents = b"".join(opened.read(name) for name in names)
    assert not [
        name
        for name in names
        if ".stereo-tool-license" in name or "secrets.json" in name or "tools/" in name
    ]
    assert key.encode() not in contents
    assert b"stereo tool program bytes" not in contents
