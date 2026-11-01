"""Launcher synchronization of stale managed packages."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY = Path(__file__).parents[2]


def _load_script():
    path = REPOSITORY / "scripts" / "runtime_sync.py"
    spec = importlib.util.spec_from_file_location("autodj_runtime_sync_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sync_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "checkout"
    environment = root / ".uv" / "setup-cpu"
    (root / ".uv").mkdir(parents=True)
    environment.mkdir(parents=True)
    saved = {"environment": str(environment), "backend": "cpu", "external": False, "custom": 42}
    state_path = root / ".uv" / "setup.json"
    state_path.write_text(json.dumps(saved), encoding="utf-8")

    runtime = ModuleType("autodj.runtime_sync")
    runtime.needs_sync = Mock(return_value=True)
    runtime.mark_synced = Mock()
    support = ModuleType("setup_support")
    support.install_python = Mock()
    support.read_json = Mock(return_value={"amd_arch": "gfx1100"})
    activity = ModuleType("setup_activity")
    activity.ensure_idle = Mock()
    hardware = ModuleType("setup_hardware")
    hardware.detect_hardware = Mock(
        return_value=SimpleNamespace(system="Windows", machine="AMD64", amd_arch="gfx1100")
    )
    for name, value in (
        ("autodj.runtime_sync", runtime),
        ("setup_support", support),
        ("setup_activity", activity),
        ("setup_hardware", hardware),
    ):
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.setattr(
        "runpy.run_path",
        lambda *args, **kwargs: {"setup_values": lambda checkout: {"port": 8765}},
    )
    module = _load_script()
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    return SimpleNamespace(
        module=module,
        root=root,
        environment=environment,
        saved=saved,
        state_path=state_path,
        runtime=runtime,
        support=support,
        activity=activity,
        hardware=hardware,
    )


def test_unchanged_runtime_returns_without_setup_work(sync_setup) -> None:
    """an unchanged managed runtime skips installation."""
    sync_setup.runtime.needs_sync.return_value = False

    sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, "cpu")

    sync_setup.support.install_python.assert_not_called()
    sync_setup.activity.ensure_idle.assert_not_called()
    sync_setup.runtime.mark_synced.assert_not_called()


def test_external_runtime_is_never_synchronized(sync_setup) -> None:
    """externally managed packages remain untouched."""
    state = json.loads(sync_setup.state_path.read_text(encoding="utf-8"))
    state["external"] = True
    sync_setup.state_path.write_text(json.dumps(state), encoding="utf-8")

    sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, "cpu")

    sync_setup.runtime.needs_sync.assert_not_called()
    sync_setup.support.install_python.assert_not_called()
    sync_setup.runtime.mark_synced.assert_not_called()


@pytest.mark.parametrize("backend", ["cpu", "nvidia", "no-model", "amd"])
def test_sync_uses_saved_backend_and_marks_after_install(
    sync_setup, backend: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """managed sync follows the saved backend and commits success."""
    state = json.loads(sync_setup.state_path.read_text(encoding="utf-8"))
    state["backend"] = backend
    sync_setup.state_path.write_text(json.dumps(state), encoding="utf-8")

    sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, backend)

    arch = "gfx1100" if backend == "amd" else None
    sync_setup.support.install_python.assert_called_once_with(
        sync_setup.root,
        sync_setup.environment,
        backend,
        "Windows",
        "AMD64",
        arch,
        log=sync_setup.root / ".uv" / "runtime-sync.log",
    )
    sync_setup.runtime.mark_synced.assert_called_once_with(
        sync_setup.root, sync_setup.environment, backend, arch
    )
    sync_setup.activity.ensure_idle.assert_called_once_with(
        sync_setup.root, sync_setup.environment, 8765, launcher=True
    )
    assert json.loads(sync_setup.state_path.read_text(encoding="utf-8")) == state
    assert "Updating AutoDJ's packages..." in capsys.readouterr().out
    if backend == "amd":
        sync_setup.hardware.detect_hardware.assert_not_called()
    else:
        sync_setup.support.read_json.assert_not_called()
        sync_setup.hardware.detect_hardware.assert_not_called()


def test_failed_install_does_not_mark_runtime_synced(sync_setup) -> None:
    """failed package installation leaves the runtime stale."""
    sync_setup.support.install_python.side_effect = ValueError("install failed")
    stamp = sync_setup.root / ".uv/runtime-sync.json"
    stamp.write_text('{"fingerprint":"previous"}', encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match=r"Package update failed\. Run AutoDJ again to retry\. See details in \.uv/runtime-sync\.log\.",
    ) as failure:
        sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, "cpu")

    assert isinstance(failure.value.__cause__, ValueError)
    sync_setup.runtime.mark_synced.assert_not_called()
    assert not stamp.exists()


def test_busy_runtime_does_not_install_or_mark(sync_setup, capsys) -> None:
    """setup checks that AutoDJ is idle before package changes."""
    log = sync_setup.root / ".uv" / "runtime-sync.log"
    log.write_text("prior log", encoding="utf-8")
    sync_setup.activity.ensure_idle.side_effect = RuntimeError("AutoDJ is running")

    with pytest.raises(RuntimeError, match="AutoDJ is running"):
        sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, "cpu")

    sync_setup.support.install_python.assert_not_called()
    sync_setup.runtime.mark_synced.assert_not_called()
    assert capsys.readouterr().out == ""
    assert log.read_text(encoding="utf-8") == "prior log"


def test_backend_change_before_sync_is_refused(sync_setup) -> None:
    """sync refuses a selection that changed after dispatch."""
    with pytest.raises(ValueError, match="backend changed"):
        sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, "nvidia")

    sync_setup.support.install_python.assert_not_called()


def test_environment_change_before_sync_is_refused(sync_setup) -> None:
    """sync refuses an environment that changed after dispatch."""
    other = sync_setup.root / ".uv" / "other-env"
    other.mkdir()

    with pytest.raises(ValueError, match="environment changed"):
        sync_setup.module.synchronize(sync_setup.root, other, "cpu")

    sync_setup.support.install_python.assert_not_called()


@pytest.mark.parametrize("corrupt", [False, True])
def test_amd_architecture_is_detected_only_when_stamp_has_none(sync_setup, corrupt) -> None:
    """AMD package sync retains its recorded architecture."""
    state = json.loads(sync_setup.state_path.read_text(encoding="utf-8"))
    state["backend"] = "amd"
    sync_setup.state_path.write_text(json.dumps(state), encoding="utf-8")
    sync_setup.support.read_json.return_value = {}
    if corrupt:
        sync_setup.support.read_json.side_effect = ValueError("invalid marker")
    sync_setup.hardware.detect_hardware.return_value.amd_arch = "gfx1200"

    sync_setup.module.synchronize(sync_setup.root, sync_setup.environment, "amd")

    sync_setup.hardware.detect_hardware.assert_called_once_with()
    sync_setup.support.install_python.assert_called_once_with(
        sync_setup.root,
        sync_setup.environment,
        "amd",
        "Windows",
        "AMD64",
        "gfx1200",
        log=sync_setup.root / ".uv" / "runtime-sync.log",
    )
    sync_setup.runtime.mark_synced.assert_called_once_with(
        sync_setup.root, sync_setup.environment, "amd", "gfx1200"
    )
