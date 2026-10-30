"""The setup activity guard stops before it can alter a runtime in use."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import setup_activity


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "AutoDJ checkout"
    environment = root / ".uv" / "setup-cpu"
    index_dir = tmp_path / "library data"
    root.mkdir()
    environment.mkdir(parents=True)
    index_dir.mkdir()
    return root, environment, index_dir


def _idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_activity, "_busy_port", lambda _port: False)
    monkeypatch.setattr(setup_activity, "_processes", lambda: [])


def test_launcher_server_lock_stops_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, environment, _index_dir = _paths(tmp_path)
    monkeypatch.setattr(setup_activity, "_busy_port", lambda _port: False)
    monkeypatch.setattr(setup_activity, "_processes", lambda: [])
    seen: list[Path | None] = []

    def other_servers(folder: Path | None, _own: set[int]) -> list[int]:
        seen.append(folder)
        return [4321]

    monkeypatch.setattr("autodj.update_contract.other_servers", other_servers)
    with pytest.raises(RuntimeError, match="launcher process 4321"):
        setup_activity.ensure_idle(root, environment, 8080)
    assert seen == [root / ".uv" / "update"]


def test_busy_port_stops_setup_without_identifying_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, environment, _index_dir = _paths(tmp_path)
    monkeypatch.setattr(setup_activity, "_busy_port", lambda _port: True)
    monkeypatch.setattr(setup_activity, "_processes", lambda: pytest.fail("should stop first"))
    monkeypatch.setattr("autodj.update_contract.other_servers", lambda *_args: [])

    with pytest.raises(RuntimeError, match="Port 8080 is in use"):
        setup_activity.ensure_idle(root, environment, 8080)


def test_update_ignores_its_own_supervisor_but_not_other_launchers(tmp_path, monkeypatch):
    """ADR-0012-12: only the updating supervisor is excluded from activity checks."""
    root, environment, _ = _paths(tmp_path)
    _idle(monkeypatch)
    monkeypatch.setenv("AUTODJ_SETUP_UPDATE", "4321")
    holders = {4321}
    monkeypatch.setattr(
        "autodj.update_contract.other_servers", lambda folder, own: sorted(holders - own)
    )
    monkeypatch.setattr(
        setup_activity,
        "_processes",
        lambda: [setup_activity.Process(4321, "python", "python -m autodj serve", str(root))],
    )
    setup_activity.ensure_idle(root, environment, 8080)
    holders.add(5678)
    with pytest.raises(RuntimeError, match="5678"):
        setup_activity.ensure_idle(root, environment, 8080)


def test_older_updater_is_found_through_a_wrapper_parent_chain(tmp_path, monkeypatch):
    root, _environment, _ = _paths(tmp_path)
    folder = root / ".uv" / "update"
    folder.mkdir(parents=True)
    (folder / "lock").touch()
    monkeypatch.delenv("AUTODJ_SETUP_UPDATE", raising=False)
    monkeypatch.setattr(setup_activity.os, "getppid", lambda: 100)
    monkeypatch.setattr(
        setup_activity,
        "_processes",
        lambda: [
            setup_activity.Process(100, "python", "setup.py --yes", parent_pid=50),
            setup_activity.Process(50, "uv", "uv update", parent_pid=25),
            setup_activity.Process(25, "python", "autodj serve", parent_pid=1),
        ],
    )
    monkeypatch.setattr("autodj.update_contract.try_lock", lambda _path: None)
    monkeypatch.setattr("autodj.update_contract.other_servers", lambda *_args: [25])

    assert setup_activity.updater_owner(root) == 25


def test_locked_update_without_a_parent_launcher_refuses_setup(tmp_path, monkeypatch):
    root, _environment, _ = _paths(tmp_path)
    folder = root / ".uv" / "update"
    folder.mkdir(parents=True)
    (folder / "lock").touch()
    monkeypatch.delenv("AUTODJ_SETUP_UPDATE", raising=False)
    monkeypatch.setattr(setup_activity.os, "getppid", lambda: 100)
    monkeypatch.setattr(setup_activity, "_processes", lambda: [])
    monkeypatch.setattr("autodj.update_contract.try_lock", lambda _path: None)
    monkeypatch.setattr("autodj.update_contract.other_servers", lambda *_args: [99])

    with pytest.raises(RuntimeError, match="Another AutoDJ update is running"):
        setup_activity.updater_owner(root)


def test_missing_update_lock_is_not_created_for_normal_setup(tmp_path, monkeypatch):
    root, _environment, _ = _paths(tmp_path)
    monkeypatch.delenv("AUTODJ_SETUP_UPDATE", raising=False)

    assert setup_activity.updater_owner(root) is None
    assert not (root / ".uv" / "update").exists()


def test_unlocked_existing_update_lock_is_not_an_active_update(tmp_path, monkeypatch):
    root, _environment, _ = _paths(tmp_path)
    lock = root / ".uv" / "update" / "lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    monkeypatch.delenv("AUTODJ_SETUP_UPDATE", raising=False)
    handle = object()
    monkeypatch.setattr("autodj.update_contract.try_lock", lambda _path: handle)
    unlocked = []
    monkeypatch.setattr("autodj.update_contract.unlock", unlocked.append)

    assert setup_activity.updater_owner(root) is None
    assert unlocked == [handle]


def test_numeric_update_marker_identifies_new_updater(tmp_path, monkeypatch):
    root, _environment, _ = _paths(tmp_path)
    monkeypatch.setenv("AUTODJ_SETUP_UPDATE", "7351")

    assert setup_activity.updater_owner(root) == 7351


@pytest.mark.parametrize(
    ("command", "cwd", "active"),
    [
        ("python -m autodj serve", "", True),
        ("python -m autodj index --limit 1", "", True),
        ("python -m autodj analyse", "", True),
        ("python -m autodj worker", "", True),
        ("python -m unrelated serve", "", False),
        ("python -m autodj serve", "C:/other/checkout", False),
        ("python -m autodj index", "C:/other/checkout", False),
    ],
)
def test_only_checkout_or_selected_environment_jobs_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    cwd: str,
    active: bool,
) -> None:
    root, environment, _index_dir = _paths(tmp_path)
    _idle(monkeypatch)
    monkeypatch.setattr("autodj.update_contract.other_servers", lambda *_args: [])
    process = setup_activity.Process(
        4321,
        "python.exe",
        command + (f" {environment}/Scripts/python.exe" if active and not cwd else ""),
        cwd,
    )
    # A checkout working directory is another explicit ownership signal.
    if active and not cwd:
        process = setup_activity.Process(process.pid, process.name, command, str(root))
    monkeypatch.setattr(setup_activity, "_processes", lambda: [process])

    check = setup_activity.ensure_idle
    if active:
        with pytest.raises(RuntimeError, match="process 4321"):
            check(root, environment, 8080)
    else:
        check(root, environment, 8080)


def test_process_inspection_failure_stops_setup_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, environment, _index_dir = _paths(tmp_path)
    monkeypatch.setattr(setup_activity, "_busy_port", lambda _port: False)
    monkeypatch.setattr("autodj.update_contract.other_servers", lambda *_args: [])
    monkeypatch.setattr(
        setup_activity,
        "_processes",
        lambda: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "ps")),
    )

    with pytest.raises(RuntimeError, match="cannot safely verify"):
        setup_activity.ensure_idle(root, environment, 8080)


def test_process_classification_does_not_claim_arbitrary_python_scripts() -> None:
    assert setup_activity._activity("python", "python custom_audio_import.py") is None
    assert setup_activity._activity("python", "python custom.py -m autodj serve") is None
    assert (
        setup_activity._activity("python", "python -u -m autodj.setup_model")
        == "model verification"
    )
    assert setup_activity._activity("python", "python -m autodj index") == "index"
    assert setup_activity._activity("python", 'python -c "import autodj; autodj.serve()"') is None
    assert (
        setup_activity._activity("powershell.exe", 'powershell -Command "python -m autodj serve"')
        is None
    )


def test_checkout_scope_uses_path_boundaries(tmp_path: Path) -> None:
    root, environment, _index_dir = _paths(tmp_path)
    sibling = setup_activity.Process(
        4321,
        "python.exe",
        f'python.exe -m autodj index --config "{root}-other/config.toml"',
    )
    assert not setup_activity._scoped(sibling, root, environment)


def test_global_config_option_does_not_hide_server_verb() -> None:
    assert (
        setup_activity._activity("python.exe", 'python.exe -m autodj --config "config.toml" serve')
        == "serve"
    )
