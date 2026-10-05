"""Safe tests for the standalone guided setup and its saved launcher."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY = Path(__file__).parents[2]
SCRIPTS = REPOSITORY / "scripts"


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
    root, music = _project(tmp_path)
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

    args = setup_modules.setup.parser().parse_args(["--dry-run", "--music-dir", str(music)])
    assert setup_modules.setup.execute(args, root) == 0

    output = capsys.readouterr().out
    assert "Processing: nvidia" in output
    assert "Install: uv venv" in output
    assert not (root / "config.toml").exists()
    assert not (root / ".uv").exists()
    assert {path.relative_to(root) for path in root.rglob("*")} == before
    mocks.install_python.assert_not_called()
    mocks.run.assert_not_called()


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


def test_explicit_existing_environment_skips_package_install(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, music = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(
        [
            "--yes",
            "--backend",
            "cpu",
            "--environment",
            str(environment),
            "--music-dir",
            str(music),
            "--port",
            "8181",
        ]
    )

    assert setup_modules.setup.execute(args, root) == 0

    mocks.install_python.assert_not_called()
    mocks.create_configuration.assert_called_once_with(root, music.resolve(), 8181)
    selection = setup_modules.support.read_json(root / setup_modules.support.STATE)
    assert selection == {
        "environment": str(environment.resolve()),
        "backend": "cpu",
        "external": True,
    }


def test_existing_configuration_is_preserved(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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


def test_doctor_failure_does_not_discard_verified_runtime(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    root, music = _project(tmp_path)
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
            "--music-dir",
            str(music),
        ]
    )

    assert setup_modules.setup.execute(args, root) == 0

    assert setup_modules.support.read_json(root / setup_modules.support.STATE)["backend"] == "cpu"
    output = capsys.readouterr().out
    assert "WARN library: music folder is empty" in output
    assert "Run `autodj doctor` for details and repair guidance." in output
    assert '"checks"' not in output
    command = mocks.doctor.call_args.args[0]
    assert command[-3:] == ["doctor", "--no-fix", "--json"]


def test_selected_gpu_backend_fails_when_runtime_has_no_usable_gpu(
    setup_modules, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, music = _project(tmp_path)
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
            "--music-dir",
            str(music),
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
    root, music = _project(tmp_path)
    environment = _environment(setup_modules.support, root)
    mocks = _patch_execution(setup_modules, monkeypatch)
    args = setup_modules.setup.parser().parse_args(
        [
            "--yes",
            "--backend",
            "cpu",
            "--environment",
            str(environment),
            "--music-dir",
            str(music),
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
    pip_command = commands[1]
    python_argument = pip_command.index("--python") + 1
    assert pip_command[python_argument] == str(setup_modules.support.python_path(environment))
    assert " " in pip_command[python_argument]
    recipe = setup_modules.support.profiles(root)["nvidia"]["platforms"]["Windows"]
    assert pip_command[pip_command.index("--index-url") + 1] == recipe["index"]
    assert pip_command[-len(recipe["packages"]) :] == recipe["packages"]
    assert commands[2][-4:] == [
        "--constraint",
        str(root / ".uv/setup-nvidia-constraints.txt"),
        "-e",
        ".[all]",
    ]
    assert all("sync" not in command for command in commands)

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


def test_install_stamp_skips_matching_inputs_and_reruns_after_lock_change(
    setup_modules, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = setup_modules.support
    root, _ = _project(tmp_path)
    environment = root / ".uv" / "cached environment"
    python = support.python_path(environment)
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    stamp = environment / ".autodj-setup.json"
    paths = ("pyproject.toml", "scripts/setup-profiles.json")
    support.write_json(
        stamp,
        {"dependencies": support.fingerprint(root, paths, "cpu:Linux:x86_64:")},
    )
    run = Mock()
    monkeypatch.setattr(support, "run", run)

    support.install_python(root, environment, "cpu", "Linux")
    run.assert_not_called()

    (root / "pyproject.toml").write_text("[project]\nname = 'updated'\n", encoding="utf-8")
    support.install_python(root, environment, "cpu", "Linux")
    expected = support.install_commands(root, environment, "cpu", "Linux")
    expected.append(["uv", "pip", "check", "--python", str(python)])
    assert [call.args[0] for call in run.call_args_list] == expected
    assert support.read_json(stamp)["dependencies"] == support.fingerprint(
        root, paths, "cpu:Linux:x86_64:"
    )
    run_environment = run.call_args.args[2]
    assert run_environment["UV_PROJECT_ENVIRONMENT"] == str(environment)
    assert run_environment["AUTODJ_RUNTIME"] == "current"
    assert "VIRTUAL_ENV" not in run_environment

    support.install_python(root, environment, "cpu", "Linux")
    assert [call.args[0] for call in run.call_args_list] == expected


def test_configuration_escapes_path_and_validates_port(setup_modules, tmp_path: Path) -> None:
    root, _ = _project(tmp_path)

    class SerializedDirectory:
        def is_dir(self) -> bool:
            return True

        def as_posix(self) -> str:
            return 'C:/Music/Artist "Night"\\Set\n# Café'

    music_value = SerializedDirectory().as_posix()
    setup_modules.support.create_configuration(root, SerializedDirectory(), 8188)
    text = (root / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["library"]["music_dir"] == music_value
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


def test_create_configuration_rejects_invalid_port_without_a_file(
    setup_modules, tmp_path: Path
) -> None:
    root, _ = _project(tmp_path)
    with pytest.raises(ValueError, match="Port must be between"):
        setup_modules.support.create_configuration(root, Path("."), 0)
    assert not (root / "config.toml").exists()
