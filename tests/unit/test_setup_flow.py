"""Safe tests for the standalone guided setup and its saved launcher."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

REPOSITORY = Path(__file__).parents[2]
SCRIPTS = REPOSITORY / "scripts"
TORCH_FAMILY = ("torch", "torchvision", "torchaudio")


def _locked_torch_versions() -> dict[str, str]:
    """Return the torch, torchvision and torchaudio versions uv.lock records."""
    lock = tomllib.loads((REPOSITORY / "uv.lock").read_text(encoding="utf-8"))
    return {
        package["name"]: package["version"]
        for package in lock["package"]
        if package["name"] in TORCH_FAMILY
    }


def _profile_versions(packages: list[str]) -> dict[str, str]:
    """Map each ``name[extra]==version+local`` install spec to its public version."""
    versions = {}
    for spec in packages:
        name, _, version = spec.partition("==")
        versions[name.split("[")[0]] = version.split("+")[0]
    return versions


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_modules(monkeypatch: pytest.MonkeyPatch):
    """Load setup helpers without importing AutoDJ or retaining module state."""
    names = ("setup_support", "setup_hardware")
    previous = {name: sys.modules.pop(name, None) for name in names}
    monkeypatch.syspath_prepend(str(SCRIPTS))
    try:
        support = importlib.import_module("setup_support")
        hardware = importlib.import_module("setup_hardware")
        setup = _load_script("autodj_setup_under_test", SCRIPTS / "setup.py")
        yield SimpleNamespace(
            support=support,
            hardware=hardware,
            setup=setup,
        )
    finally:
        for name in ("autodj_setup_under_test", *names):
            sys.modules.pop(name, None)
        for name, module in previous.items():
            if module is not None:
                sys.modules[name] = module


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """Make an isolated checkout-shaped tree with a space in its name."""
    root = tmp_path / "AutoDJ project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "setup-profiles.json").write_bytes((SCRIPTS / "setup-profiles.json").read_bytes())
    (root / "pyproject.toml").write_text("[project]\nname = 'autodj-test'\n", encoding="utf-8")
    (root / "uv.lock").write_text("test lock\n", encoding="utf-8")
    (root / ".python-version").write_text("3.14\n", encoding="utf-8")
    music = root / "Music # Café"
    music.mkdir()
    return root, music


def _environment(support, root: Path, relative: str = ".uv/existing environment") -> Path:
    environment = root / relative
    python = support.python_path(environment)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("test interpreter placeholder\n", encoding="utf-8")
    return environment


def _hardware(
    modules,
    *,
    system: str = "Linux",
    recommendation: str = "cpu",
    gpus: tuple[str, ...] = (),
    amd_arch: str | None = None,
):
    return modules.hardware.Hardware(
        system=system,
        machine="x86_64",
        gpus=gpus,
        recommendation=recommendation,
        reason="Test hardware probe.",
        amd_arch=amd_arch,
    )


def _successful_runtime(backend: str = "cpu") -> dict[str, object]:
    return {
        "backend": backend,
        "gpu_available": False,
        "device_name": None,
        "torch_version": "2.14.0",
        "reason": "test runtime",
    }


def _patch_execution(
    modules,
    monkeypatch: pytest.MonkeyPatch,
    *,
    hardware=None,
    runtime: dict[str, object] | None = None,
):
    """Replace host probes and expensive actions while retaining setup state writes."""
    setup = modules.setup
    monkeypatch.setattr(setup, "ensure_ffmpeg", Mock(return_value=True))
    monkeypatch.delenv("AUTODJ_GPU", raising=False)
    install_python = Mock()
    prepare_caches = Mock()
    create_configuration = Mock(wraps=modules.support.create_configuration)
    initialize_paths = Mock()
    run = Mock()
    probe_runtime = Mock(return_value=runtime or _successful_runtime())
    doctor = Mock(return_value=setup.subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(
        setup,
        "detect_hardware",
        lambda: hardware or _hardware(modules, system="Windows"),
    )
    monkeypatch.setattr(setup, "prerequisites", lambda: [])
    monkeypatch.setattr(setup, "port_available", lambda port: True)
    monkeypatch.setattr(setup, "install_python", install_python)
    monkeypatch.setattr(setup, "prepare_caches", prepare_caches)
    monkeypatch.setattr(
        setup,
        "runtime_environment",
        lambda environment, backend: {
            "AUTODJ_RUNTIME": "current",
            **({"AUTODJ_GPU": "0"} if backend == "cpu" else {}),
        },
    )
    monkeypatch.setattr(setup, "probe_runtime", probe_runtime)
    monkeypatch.setattr(setup, "create_configuration", create_configuration)
    monkeypatch.setattr(setup, "initialize_paths", initialize_paths)
    monkeypatch.setattr(setup, "run", run)
    monkeypatch.setattr(setup.subprocess, "run", doctor)
    return SimpleNamespace(
        install_python=install_python,
        prepare_caches=prepare_caches,
        create_configuration=create_configuration,
        initialize_paths=initialize_paths,
        run=run,
        doctor=doctor,
        probe_runtime=probe_runtime,
    )


def test_invalid_saved_environment_stops_before_installing(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    mocks = _patch_execution(setup_modules, monkeypatch)
    state = root / setup_modules.support.STATE
    setup_modules.support.write_json(state, {"backend": "cpu", "environment": None})
    before = state.read_bytes()
    args = setup_modules.setup.parser().parse_args(["--dry-run"])
    with pytest.raises(ValueError, match="Invalid saved environment path"):
        setup_modules.setup.execute(args, root)
    assert state.read_bytes() == before
    mocks.install_python.assert_not_called()


def test_only_uv_is_a_required_setup_prerequisite(
    setup_modules, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        setup_modules.setup.shutil,
        "which",
        lambda command: "uv.exe" if command == "uv" else None,
    )
    assert setup_modules.setup.prerequisites() == []


@pytest.mark.parametrize(
    ("backend", "gpu", "amd_arch"),
    [("cpu", False, None), ("nvidia", True, None), ("amd", True, "gfx1152")],
)
def test_fresh_auto_setup_verifies_and_saves_supported_recommendation(
    setup_modules,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
    gpu: bool,
    amd_arch: str | None,
) -> None:
    root, _ = _project(tmp_path)
    (root / "config.toml").write_text("[library]\nmusic_dir = '.'\n", encoding="utf-8")
    hardware = _hardware(
        setup_modules,
        system="Windows",
        recommendation=backend,
        gpus=("Detected GPU",) if gpu else (),
        amd_arch=amd_arch,
    )
    runtime = {
        "backend": {"nvidia": "cuda", "amd": "rocm"}.get(backend, "cpu"),
        "gpu_available": gpu,
        "device_name": "Detected GPU" if gpu else None,
        "torch_version": "2.14.0",
        "reason": "verified",
    }
    mocks = _patch_execution(setup_modules, monkeypatch, hardware=hardware, runtime=runtime)
    args = setup_modules.setup.parser().parse_args(["--yes"])

    assert setup_modules.setup.execute(args, root) == 0

    state = setup_modules.support.read_json(root / setup_modules.support.STATE)
    assert state["backend"] == backend
    assert state["environment"].endswith(f"setup-{backend}")
    mocks.install_python.assert_called_once()
    assert mocks.doctor.call_args.args[0][-3:] == ["doctor", "--no-fix", "--json"]


def test_dry_run_prints_plan_without_writing_or_installing(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    root, _ = _project(tmp_path)
    mocks = _patch_execution(
        setup_modules,
        monkeypatch,
        hardware=_hardware(
            setup_modules,
            system="Linux",
            recommendation="nvidia",
            gpus=("NVIDIA Example",),
        ),
    )
    before = {path.relative_to(root) for path in root.rglob("*")}

    args = setup_modules.setup.parser().parse_args(["--dry-run"])
    assert setup_modules.setup.execute(args, root) == 0

    output = capsys.readouterr().out
    assert "Processing: nvidia" in output
    assert "Install: uv venv" in output
    assert not (root / "config.toml").exists()
    assert not (root / ".uv").exists()
    assert {path.relative_to(root) for path in root.rglob("*")} == before
    setup_modules.setup.ensure_ffmpeg.assert_not_called()
    mocks.install_python.assert_not_called()
    mocks.run.assert_not_called()


def test_managed_existing_environment_dry_run_plans_sync_without_changes(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    state = root / setup_modules.support.STATE
    original = {"backend": "cpu", "environment": str(environment)}
    setup_modules.support.write_json(state, original)
    before = state.read_bytes()
    mocks = _patch_execution(setup_modules, monkeypatch)

    assert (
        setup_modules.setup.execute(setup_modules.setup.parser().parse_args(["--dry-run"]), root)
        == 0
    )

    output = capsys.readouterr().out
    assert f"Environment: {environment.resolve()}" in output
    assert "Install: uv export" in output
    assert state.read_bytes() == before
    mocks.install_python.assert_not_called()
    assert not (root / ".uv/setup-cpu-requirements.txt").exists()


@pytest.mark.parametrize(
    ("backend_arg", "saved_backend", "expected"),
    [
        ("auto", None, "nvidia"),
        ("auto", "cpu", "cpu"),
        ("cpu", "nvidia", "cpu"),
    ],
)
def test_backend_selection_uses_auto_recommendation_and_explicit_choice(
    setup_modules,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    backend_arg: str,
    saved_backend: str | None,
    expected: str,
) -> None:
    root, _ = _project(tmp_path)
    mocks = _patch_execution(
        setup_modules,
        monkeypatch,
        hardware=_hardware(
            setup_modules,
            system="Linux",
            recommendation="nvidia",
            gpus=("NVIDIA Example",),
        ),
    )
    if saved_backend is not None:
        _environment(setup_modules.support, root, ".uv/previous")
        setup_modules.support.write_json(
            root / setup_modules.support.STATE,
            {"backend": saved_backend, "environment": ".uv/previous"},
        )

    argv = ["--dry-run"]
    if backend_arg != "auto":
        argv.extend(["--backend", backend_arg])
    args = setup_modules.setup.parser().parse_args(argv)
    assert setup_modules.setup.execute(args, root) == 0

    assert f"Processing: {expected}" in capsys.readouterr().out
    mocks.install_python.assert_not_called()


def test_explicit_existing_environment_is_adopted_without_package_install(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """ADR-0012-09: an adopted environment keeps external package ownership."""
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(
        [
            "--yes",
            "--backend",
            "cpu",
            "--environment",
            str(environment),
            "--port",
            "8181",
        ]
    )

    assert setup_modules.setup.execute(args, root) == 0

    mocks.install_python.assert_not_called()
    mocks.create_configuration.assert_called_once_with(root, 8181)
    generated = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    assert "music_dir" not in generated["library"]
    output = capsys.readouterr().out
    assert "Manage music folders" in output
    assert "without changing its packages" in output
    setup_modules.setup.ensure_ffmpeg.assert_called_once_with(auto_install=True)
    selection = setup_modules.support.read_json(root / setup_modules.support.STATE)
    assert selection == {
        "environment": str(environment.resolve()),
        "backend": "cpu",
        "external": True,
    }


def test_manage_environment_updates_adopted_runtime_and_saves_managed_ownership(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """ADR-0012-09: explicit opt-in synchronizes and manages an adopted environment."""
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(
        ["--yes", "--backend", "cpu", "--environment", str(environment), "--manage-environment"]
    )

    assert setup_modules.setup.execute(args, root) == 0

    mocks.install_python.assert_called_once_with(
        root, environment.resolve(), "cpu", "Windows", "x86_64", None
    )
    assert setup_modules.support.read_json(root / setup_modules.support.STATE) == {
        "environment": str(environment.resolve()),
        "backend": "cpu",
        "external": False,
    }
    assert "Install: uv export" in capsys.readouterr().out


@pytest.mark.parametrize("external", [False, True])
def test_saved_runtime_keeps_backend_and_respects_package_ownership(
    setup_modules, monkeypatch, tmp_path, external
):
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    setup_modules.support.write_json(
        root / setup_modules.support.STATE,
        {"backend": "cpu", "environment": str(environment), "external": external},
    )
    mocks = _patch_execution(
        setup_modules,
        monkeypatch,
        hardware=_hardware(setup_modules, recommendation="nvidia"),
    )
    assert (
        setup_modules.setup.execute(setup_modules.setup.parser().parse_args(["--yes"]), root) == 0
    )
    assert mocks.install_python.call_count == (0 if external else 1)
    saved = setup_modules.support.read_json(root / setup_modules.support.STATE)
    assert saved == {
        "backend": "cpu",
        "environment": str(environment.resolve()),
        "external": external,
    }


def test_cpu_without_platform_recipe_keeps_locked_model_dependencies(setup_modules, tmp_path):
    root, _ = _project(tmp_path)
    commands = setup_modules.support.install_commands(
        root, root / ".uv/cpu", "cpu", "Darwin", "arm64"
    )
    export = next(command for command in commands if command[1] == "export")
    assert "--extra" in export
    assert "--prune" not in export


@pytest.mark.parametrize(
    ("backend", "system"), [("cpu", "Linux"), ("nvidia", "Windows"), ("no-model", "Linux")]
)
def test_requirements_export_is_quiet(setup_modules, tmp_path, backend, system):
    """uv export writes the file without also printing every requirement during setup."""
    root, _ = _project(tmp_path)
    commands = setup_modules.support.install_commands(
        root, root / ".uv" / f"setup-{backend}", backend, system
    )
    export = next(command for command in commands if command[1] == "export")
    assert "--quiet" in export
    assert export[-2] == "--output-file"


def test_failed_managed_update_does_not_save_selection(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    old = {"environment": str(environment), "backend": "cpu", "external": True}
    setup_modules.support.write_json(root / setup_modules.support.STATE, old)
    mocks = _patch_execution(setup_modules, monkeypatch)
    mocks.install_python.side_effect = RuntimeError("dependency update failed")
    args = setup_modules.setup.parser().parse_args(["--yes", "--manage-environment"])

    with pytest.raises(RuntimeError, match="dependency update failed"):
        setup_modules.setup.execute(args, root)
    assert setup_modules.support.read_json(root / setup_modules.support.STATE) == old


def test_existing_configuration_is_preserved(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    config = root / "config.toml"
    local_config = root / "config.local.toml"
    config_bytes = b"# user's base configuration\n[library]\nmusic_dir = 'kept'\n"
    local_bytes = b"# user's local overrides\n"
    config.write_bytes(config_bytes)
    local_config.write_bytes(local_bytes)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(
        ["--yes", "--backend", "cpu", "--environment", str(environment)]
    )

    assert setup_modules.setup.execute(args, root) == 0

    assert config.read_bytes() == config_bytes
    assert local_config.read_bytes() == local_bytes
    mocks.create_configuration.assert_not_called()
    assert "Your library is empty" not in capsys.readouterr().out


def test_failed_ffmpeg_install_keeps_setup_available(setup_modules, monkeypatch, tmp_path, capsys):
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    _patch_execution(setup_modules, monkeypatch)
    setup_modules.setup.ensure_ffmpeg.side_effect = RuntimeError("WinGet failed")
    args = setup_modules.setup.parser().parse_args(
        ["--yes", "--backend", "cpu", "--environment", str(environment)]
    )
    assert setup_modules.setup.execute(args, root) == 0
    assert "FFmpeg installation failed" in capsys.readouterr().err
    assert setup_modules.support.read_json(root / setup_modules.support.STATE)["backend"] == "cpu"


def test_doctor_failure_does_not_discard_verified_runtime(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    mocks.doctor.return_value = setup_modules.setup.subprocess.CompletedProcess(
        [],
        1,
        '{"exit_code":1,"checks":[{"name":"library","status":"warn","summary":"music folder is empty","detail":"no tracks"}]}',
        "",
    )
    args = setup_modules.setup.parser().parse_args(
        [
            "--yes",
            "--backend",
            "cpu",
            "--environment",
            str(environment),
        ]
    )

    assert setup_modules.setup.execute(args, root) == 0

    assert setup_modules.support.read_json(root / setup_modules.support.STATE)["backend"] == "cpu"
    output = capsys.readouterr().out
    assert "WARN library: music folder is empty" in output
    assert "no tracks" in output
    assert "Run `autodj doctor` for details and repair guidance." in output
    assert '"checks"' not in output
    command = mocks.doctor.call_args.args[0]
    assert command[-3:] == ["doctor", "--no-fix", "--json"]


def test_selected_gpu_backend_fails_when_runtime_has_no_usable_gpu(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    _patch_execution(
        setup_modules,
        monkeypatch,
        hardware=_hardware(
            setup_modules,
            system="Windows",
            recommendation="nvidia",
            gpus=("NVIDIA Example",),
        ),
        runtime={
            "backend": "cuda",
            "gpu_available": False,
            "device_name": None,
            "torch_version": "2.14.0",
            "reason": "No usable driver",
            "error": "No usable driver",
        },
    )
    args = setup_modules.setup.parser().parse_args(
        [
            "--yes",
            "--backend",
            "nvidia",
            "--environment",
            str(environment),
        ]
    )

    with pytest.raises(ValueError, match="rerun setup with --backend cpu"):
        setup_modules.setup.execute(args, root)

    assert not (root / setup_modules.support.STATE).exists()


@pytest.mark.parametrize(
    ("actions", "expected_suffixes"),
    [
        ([], []),
        (["--test-import"], ["index"]),
        (["--serve"], ["serve"]),
        (["--test-import", "--serve"], ["index", "serve"]),
    ],
)
def test_yes_keeps_import_and_server_actions_opt_in(
    setup_modules,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    actions: list[str],
    expected_suffixes: list[str],
) -> None:
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(
        [
            "--yes",
            "--backend",
            "cpu",
            "--environment",
            str(environment),
            *actions,
        ]
    )

    assert setup_modules.setup.execute(args, root) == 0

    commands = [call.args[0] for call in mocks.run.call_args_list]
    verbs = [command[command.index("autodj") + 1] for command in commands]
    assert verbs == expected_suffixes
    assert mocks.doctor.call_count == 1
    if "index" in expected_suffixes:
        assert commands[expected_suffixes.index("index")][-4:] == [
            "--limit",
            "3",
            "--no-enrich",
            "--no-analyse",
        ]


def test_install_commands_keep_paths_with_spaces_as_separate_arguments(
    setup_modules, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _project(tmp_path)
    environment = root / ".uv" / "NVIDIA environment with spaces"
    commands = setup_modules.support.install_commands(root, environment, "nvidia", "Windows")

    assert commands[0][:4] == ["uv", "venv", "--python", "3.14"]
    export_command = commands[1]
    assert export_command[:3] == ["uv", "export", "--frozen"]
    assert (
        "--extra" in export_command and export_command[export_command.index("--extra") + 1] == "all"
    )
    assert export_command[-1] == str(root / ".uv/setup-nvidia-requirements.txt")
    torch_command = commands[2]
    pip_command = commands[3]
    python_argument = pip_command.index("--python") + 1
    assert pip_command[python_argument] == str(setup_modules.support.python_path(environment))
    assert " " in pip_command[python_argument]
    recipe = setup_modules.support.profiles(root)["nvidia"]["platforms"]["Windows"]
    assert torch_command[torch_command.index("--index-url") + 1] == recipe["index"]
    assert torch_command[-len(recipe["packages"]) :] == recipe["packages"]
    assert pip_command[-2:] == ["-e", ".[all]"]
    assert pip_command[pip_command.index("-r") + 1] == str(
        root / ".uv/setup-nvidia-requirements.txt"
    )
    assert pip_command[pip_command.index("--constraint") + 1] == str(
        root / ".uv/setup-nvidia-constraints.txt"
    )
    assert all("sync" not in command for command in commands)


@pytest.mark.parametrize("backend", ["cpu", "no-model"])
def test_cpu_and_no_model_package_exports(
    setup_modules, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    root, _ = _project(tmp_path)
    commands = setup_modules.support.install_commands(
        root, root / ".uv" / f"setup-{backend}", backend, "Linux"
    )
    export = next(command for command in commands if command[1] == "export")
    assert export[:3] == ["uv", "export", "--frozen"]
    if backend == "cpu":
        assert "--extra" in export and "all" in export
        assert "--prune" in export
        assert "-e" in commands[-1] and ".[all]" in commands[-1]
    else:
        assert "--extra" not in export and "--prune" not in export
        assert commands[-1][-2:] == ["-e", "."]
        monkeypatch.setattr(setup_modules.support, "run", Mock())
        setup_modules.support.install_python(root, root / ".uv/setup-no-model", backend, "Linux")
        assert (root / ".uv").is_dir()
        assert export[-1] == str(root / ".uv/setup-no-model-requirements.txt")


@pytest.mark.parametrize(
    ("backend", "system", "machine", "amd_arch"),
    [
        ("cpu", "Linux", "x86_64", None),
        ("cpu", "Darwin", "arm64", None),
        ("nvidia", "Windows", "x86_64", None),
        ("amd", "Linux", "x86_64", "gfx1201"),
        ("no-model", "Linux", "x86_64", None),
    ],
)
def test_managed_sync_removes_packages_the_lock_no_longer_lists(
    setup_modules, tmp_path: Path, backend: str, system: str, machine: str, amd_arch: str | None
) -> None:
    """ADR-0012-10: only the final, complete install is exact, so backend wheels stay."""
    root, _ = _project(tmp_path)
    commands = setup_modules.support.install_commands(
        root, root / ".uv" / f"setup-{backend}", backend, system, machine, amd_arch
    )
    installs = [command for command in commands if command[1:3] == ["pip", "install"]]

    assert "--exact" in installs[-1]
    assert installs[-1][installs[-1].index("-r") + 1].endswith(f"setup-{backend}-requirements.txt")
    assert all("--exact" not in command for command in installs[:-1])


def test_exact_sync_requests_every_backend_recipe(setup_modules, tmp_path: Path) -> None:
    """All declared device extras remain roots of the final dependency resolution."""
    root, _ = _project(tmp_path)
    for backend, profile in setup_modules.support.profiles(root).items():
        for system, platform in profile.get("platforms", {}).items():
            variants = platform.get("architectures", {None: platform.get("packages", [])})
            for architecture, packages in variants.items():
                commands = setup_modules.support.install_commands(
                    root, root / ".uv/runtime", backend, system, "x86_64", architecture
                )
                assert set(packages) <= set(commands[-1]), (backend, system, architecture)


def test_exact_sync_retains_device_extra_and_removes_obsolete_package(
    setup_modules, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve local wheels with uv: keep a device extra's transitive dependencies."""
    uv = shutil.which("uv")
    assert uv is not None, "Setup verification requires the project's uv tool"
    root = tmp_path
    wheels = tmp_path / "wheels"
    wheels.mkdir()

    def wheel(name: str, metadata: str = "") -> Path:
        path = wheels / f"{name}-1.0-py3-none-any.whl"
        info = f"{name}-1.0.dist-info"
        with ZipFile(path, "w") as archive:
            archive.writestr(
                f"{info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n{metadata}\n",
            )
            archive.writestr(
                f"{info}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{info}/RECORD", "")
        return path

    wheel("device_runtime")
    wheel("device_bridge", "Requires-Dist: device_runtime==1.0\n")
    wheel(
        "torch",
        'Provides-Extra: device-test\nRequires-Dist: device_bridge==1.0; extra == "device-test"\n',
    )
    wheel("obsolete")
    app = wheel("test_app", "Requires-Dist: torch==1.0\n")
    environment = root / ".uv/runtime"

    def run(command: list[str]) -> None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stdout + result.stderr

    run([uv, "venv", "--python", sys.executable, "--no-python-downloads", str(environment)])
    python = str(setup_modules.support.python_path(environment))
    offline = ["--no-index", "--find-links", str(wheels), "--no-cache"]
    run([uv, "pip", "install", "--python", python, *offline, "torch[device-test]==1.0", "obsolete"])
    monkeypatch.setattr(
        setup_modules.support,
        "_platform_recipe",
        lambda *args: {"index": "https://unused.invalid", "packages": ["torch[device-test]==1.0"]},
    )
    command = setup_modules.support.install_commands(root, environment, "amd", "Windows")[-1]
    Path(command[command.index("--constraint") + 1]).write_text("torch==1.0\n", encoding="utf-8")
    Path(command[command.index("-r") + 1]).write_text("", encoding="utf-8")
    # Substitute a prebuilt app wheel for the editable app; keep the generated
    # exact install, constraints and backend requirements unchanged.
    run([uv, *command[1:-2], *offline, str(app)])
    result = subprocess.run(
        [uv, "pip", "list", "--python", python, "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    installed = {package["name"] for package in json.loads(result.stdout)}
    assert {"torch", "device-bridge", "device-runtime", "test-app"} <= installed
    assert "obsolete" not in installed


def test_no_model_sync_keeps_installed_sound_card_output(setup_modules, tmp_path: Path) -> None:
    """ADR-0012-10: an exact no-model sync keeps sound-card packages already installed."""
    root, _ = _project(tmp_path)
    environment = root / ".uv/setup-no-model"
    site = environment / (
        "Lib/site-packages" if sys.platform == "win32" else "lib/python3.14/site-packages"
    )
    (site / "sounddevice-0.5.3.dist-info").mkdir(parents=True)

    commands = setup_modules.support.install_commands(root, environment, "no-model", "Linux")
    export = next(command for command in commands if command[1] == "export")

    assert export[export.index("--extra") + 1] == "play"
    assert commands[-1][-2:] == ["-e", ".[play]"]
    assert "--exact" in commands[-1]


@pytest.mark.parametrize(
    ("backend", "system", "amd_arch", "expected"),
    [
        (
            "cpu",
            "Linux",
            None,
            "".join(f"{name}=={_locked_torch_versions()[name]}+cpu\n" for name in TORCH_FAMILY),
        ),
        (
            "amd",
            "Linux",
            "gfx1201",
            "torch==2.13.0+rocm7.14.0\ntorchvision==0.28.0+rocm7.14.0\ntorchaudio==2.11.0.2+rocm7.14.0\n",
        ),
        (
            "amd",
            "Windows",
            "gfx1201",
            "torch==2.12.0+rocm7.14.1\ntorchvision==0.27.0+rocm7.14.1\ntorchaudio==2.11.0+rocm7.14.1\n",
        ),
    ],
)
def test_backend_constraint_files_keep_profile_versions(
    setup_modules,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    system: str,
    amd_arch: str | None,
    expected: str,
) -> None:
    root, _ = _project(tmp_path)
    monkeypatch.setattr(setup_modules.support, "run", Mock())

    setup_modules.support.install_python(
        root, root / ".uv" / f"setup-{backend}", backend, system, amd_arch=amd_arch
    )

    assert (root / ".uv" / f"setup-{backend}-constraints.txt").read_text() == expected


@pytest.mark.parametrize("backend", ["cpu", "nvidia"])
def test_pytorch_index_profiles_match_the_locked_torch(backend: str) -> None:
    """The CPU and NVIDIA recipes install the torch family that uv.lock tests.

    A lock upgrade that moves torch must move these install specs too, so a
    setup-managed runtime never falls behind the tested snapshot.
    """
    profiles = json.loads((SCRIPTS / "setup-profiles.json").read_text(encoding="utf-8"))
    locked = _locked_torch_versions()

    for system, recipe in profiles[backend]["platforms"].items():
        assert _profile_versions(recipe["packages"]) == locked, (backend, system)


def test_container_cpu_torch_matches_the_lock() -> None:
    """container/torch-cpu.txt names the locked torch family, as the image build asserts."""
    text = (REPOSITORY / "container" / "torch-cpu.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^(torch|torchaudio|torchvision)==(\S+)\+cpu ", text, re.M))

    assert pins == _locked_torch_versions()


def test_setup_run_preserves_arguments_and_environment(setup_modules, tmp_path, monkeypatch):
    root, _ = _project(tmp_path)
    environment = root / ".uv" / "environment with spaces"
    called = Mock()
    executable = "C:/Program Files/uv/uv.exe"
    monkeypatch.setattr(setup_modules.support.shutil, "which", lambda name: executable)
    monkeypatch.setattr(setup_modules.support.subprocess, "run", called)
    env = {"UV_PROJECT_ENVIRONMENT": str(environment)}
    setup_modules.support.run(
        ["uv", "pip", "install", "--python", str(environment / "python.exe")], root, env
    )
    called.assert_called_once_with(
        [executable, "pip", "install", "--python", str(environment / "python.exe")],
        cwd=root,
        env=env,
        check=True,
    )


def test_install_python_always_synchronizes_and_checks_runtime(
    setup_modules, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0012-09: a managed setup run always synchronizes and checks dependencies."""
    support = setup_modules.support
    root, _ = _project(tmp_path)
    environment = root / ".uv" / "cached environment"
    python = support.python_path(environment)
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(support, "run", run)

    support.install_python(root, environment, "cpu", "Linux")
    expected = support.install_commands(root, environment, "cpu", "Linux")
    expected.append(["uv", "pip", "check", "--python", str(python)])
    assert [call.args[0] for call in run.call_args_list] == expected
    run_environment = run.call_args.args[2]
    assert run_environment["UV_PROJECT_ENVIRONMENT"] == str(environment)
    assert run_environment["AUTODJ_RUNTIME"] == "current"
    assert "VIRTUAL_ENV" not in run_environment

    support.install_python(root, environment, "cpu", "Linux")
    assert len(run.call_args_list) == 2 * len(expected)


def test_configuration_validates_port(setup_modules, tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    setup_modules.support.create_configuration(root, 8188)
    text = (root / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert "music_dir" not in parsed["library"]
    assert parsed["server"]["port"] == 8188
    assert parsed["server"]["allowed_origins"] == [
        "http://127.0.0.1:8188",
        "http://localhost:8188",
    ]

    assert setup_modules.setup.port_number("1") == 1
    assert setup_modules.setup.port_number("65535") == 65535
    for invalid in ("not-a-number", "0", "-1", "65536"):
        with pytest.raises(argparse.ArgumentTypeError):
            setup_modules.setup.port_number(invalid)


def test_default_configuration_does_not_require_a_music_folder(
    setup_modules, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    setup_modules.support.create_configuration(root, 8080)

    parsed = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    assert parsed["library"] == {"beets_db": ""}
    assert parsed["server"]["port"] == 8080


def test_create_configuration_rejects_invalid_port_without_a_file(
    setup_modules, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    with pytest.raises(ValueError, match="Port must be between"):
        setup_modules.support.create_configuration(root, 0)
    assert not (root / "config.toml").exists()


def test_no_model_core_metadata_keeps_analysis_optional() -> None:
    """ADR-0012-06: the serving install cannot pull the model stack."""
    data = tomllib.loads((REPOSITORY / "pyproject.toml").read_text())
    names = {
        requirement.split(">", 1)[0].split("[", 1)[0]
        for requirement in data["project"]["dependencies"]
    }
    assert not names & {"torch", "muq", "transformers", "torchaudio", "torchvision"}
    optional = data["project"]["optional-dependencies"]
    assert any(requirement.startswith("muq") for requirement in optional["index"])
    assert any(requirement.startswith("transformers") for requirement in optional["index"])


def test_no_model_setup_saves_runtime_without_torch_probe(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0012-06: no-model setup verifies serving imports without Torch."""
    root, _ = _project(tmp_path)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(["--no-model", "--yes"])
    assert setup_modules.setup.execute(args, root) == 0
    state = setup_modules.support.read_json(root / setup_modules.support.STATE)
    assert state["backend"] == "no-model"
    assert state["environment"].endswith("setup-no-model")
    mocks.probe_runtime.assert_not_called()
    commands = setup_modules.support.install_commands(
        root, Path(state["environment"]), "no-model", "Linux"
    )
    assert commands[-1][-2:] == ["-e", "."]
    assert all("torch" not in str(command) for command in commands)
    assert setup_modules.support.runtime_environment(root, "no-model")["AUTODJ_NO_MODEL"] == "1"


def test_no_model_setup_rejects_gpu_and_test_import_before_writes(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    mocks = _patch_execution(setup_modules, monkeypatch)
    for flags in (["--no-model", "--backend", "nvidia"], ["--no-model", "--test-import"]):
        args = setup_modules.setup.parser().parse_args([*flags, "--yes"])
        with pytest.raises(ValueError, match="no-model"):
            setup_modules.setup.execute(args, root)
    assert not (root / ".uv").exists()
    mocks.install_python.assert_not_called()


def test_saved_no_model_rejects_test_import_before_writes(setup_modules, monkeypatch, tmp_path):
    root, _ = _project(tmp_path)
    mocks = _patch_execution(setup_modules, monkeypatch)
    environment = _environment(setup_modules.support, root)
    setup_modules.support.write_json(
        root / setup_modules.support.STATE,
        {"backend": "no-model", "environment": str(environment)},
    )
    before = (root / setup_modules.support.STATE).read_bytes()
    args = setup_modules.setup.parser().parse_args(["--test-import", "--yes"])
    with pytest.raises(ValueError, match="no-model"):
        setup_modules.setup.execute(args, root)
    assert (root / setup_modules.support.STATE).read_bytes() == before
    mocks.initialize_paths.assert_not_called()
    mocks.run.assert_not_called()


def test_interactive_no_model_never_offers_model_test_import(setup_modules, monkeypatch, tmp_path):
    root, _ = _project(tmp_path)
    mocks = _patch_execution(setup_modules, monkeypatch)
    monkeypatch.setattr(setup_modules.setup.sys.stdin, "isatty", lambda: True)
    confirm = Mock(return_value=True)
    monkeypatch.setattr(setup_modules.setup, "confirm", confirm)
    args = setup_modules.setup.parser().parse_args(["--no-model", "--port", "8080"])
    assert setup_modules.setup.execute(args, root) == 0
    confirm.assert_called_once_with("Apply this setup plan?")
    mocks.run.assert_not_called()


@pytest.mark.parametrize("backend", ["cpu", "nvidia", "amd", "no-model"])
def test_runtime_environment_uses_selected_model_mode(
    setup_modules, monkeypatch, tmp_path, backend
):
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1")
    env = setup_modules.support.runtime_environment(tmp_path, backend)
    assert env.get("AUTODJ_NO_MODEL") == ("1" if backend == "no-model" else None)


def test_setup_runs_doctor_offline_with_a_time_limit(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """ADR-0016-02: setup's Doctor asks no network, and a hung Doctor cannot hold setup."""
    root, _ = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    mocks.doctor.side_effect = setup_modules.setup.subprocess.TimeoutExpired("doctor", 600)
    args = setup_modules.setup.parser().parse_args(
        ["--yes", "--backend", "cpu", "--environment", str(environment)]
    )
    assert setup_modules.setup.execute(args, root) == 0
    kwargs = mocks.doctor.call_args.kwargs
    assert kwargs["env"]["AUTODJ_UPDATE_CHECK"] == "0"
    assert kwargs["timeout"] == setup_modules.setup.DOCTOR_TIMEOUT_SECONDS
    assert "Doctor did not finish" in capsys.readouterr().out
    assert setup_modules.support.read_json(root / setup_modules.support.STATE)["backend"] == "cpu"


def test_empty_python_version_stops_setup_with_a_clear_message(
    setup_modules, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    (root / ".python-version").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.python-version is empty"):
        setup_modules.support.python_version(root)
