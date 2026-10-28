"""The Python readiness check decides from uv and index results, with no network here."""

import subprocess
from pathlib import Path

import pytest

from scripts import python_readiness as readiness

PY315 = (3, 15)
# Renovate titles a group's approval line by the group, without a version.
DASHBOARD = (
    "## Pending Approval\n\n"
    "The following branches are pending approval. To create them, click on a checkbox below.\n\n"
    " - [ ] <!-- approve-branch=renovate/python-minor-release -->"
    "chore(deps): update python minor release\n"
    " - [ ] <!-- approve-branch=renovate/other -->chore(deps): update other to v3.15.0\n"
)


def failed(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", stderr)


def reports(*findings: readiness.Finding):
    """Stand in for a check that returns these findings."""
    return lambda *_: list(findings)


def uv_error(requirement: str, reason: str) -> subprocess.CompletedProcess[str]:
    return failed(
        "error: No solution found when resolving dependencies\n"
        f"  cause: Because {requirement} {reason} and you require {requirement}, "
        "we can conclude that your requirements are unsatisfiable.\n"
    )


# --- Which Python -----------------------------------------------------------


def test_runtime_minor_reads_python_version():
    assert readiness.runtime_minor("3.14\n") == (3, 14)
    assert readiness.runtime_minor("3.14.10") == (3, 14)
    with pytest.raises(ValueError, match=r"is not X\.Y"):
        readiness.runtime_minor("cpython@3.14")


def test_newest_minor_picks_the_newest_final_release_above_the_current_one():
    names = [
        "Python 3.14.10",
        "Python 3.15.0",
        "Python 3.15.2",
        "Python 3.16.0rc1",
        "Python 2.7.18",
    ]
    assert readiness.newest_minor(names, (3, 14)) == ((3, 15), "3.15.2")
    assert readiness.newest_minor([*names, "Python 3.16.0"], (3, 14)) == ((3, 16), "3.16.0")


def test_newest_minor_is_none_without_a_newer_release():
    assert readiness.newest_minor(["Python 3.14.10", "Python 3.15.0b1"], (3, 14)) is None


# --- Wheel tags -------------------------------------------------------------


@pytest.mark.parametrize(
    ("python", "abi", "expected"),
    [
        ("cp315", "cp315", True),
        ("cp315", "cp315t", False),
        ("cp314", "cp314", False),
        ("cp310", "abi3", True),
        ("cp316", "abi3", False),
        ("py3", "none", True),
    ],
)
def test_runs_on_regular_cpython(python, abi, expected):
    assert readiness.runs_on([python], [abi], PY315) is expected


def test_wheel_versions_match_name_python_and_platform():
    files = [
        "torch-2.14.1%2Bcpu-cp315-cp315-win_amd64.whl",
        "torch-2.14.1+cpu-cp315-cp315-manylinux_2_28_x86_64.whl",
        "torch-2.13.0+cpu-cp315-cp315t-win_amd64.whl",
        "torch-2.13.0+cpu-cp314-cp314-win_amd64.whl",
        "faiss_cpu-1.15.1-cp310-abi3-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
        "torch-2.14.1.tar.gz",
    ]
    assert readiness.wheel_versions(files, "torch", PY315, "Windows x86-64") == {"2.14.1+cpu"}
    assert readiness.wheel_versions(files, "torch", PY315, "Linux x86-64") == {"2.14.1+cpu"}
    assert readiness.wheel_versions(files, "faiss-cpu", PY315, "Linux aarch64") == {"1.15.1"}
    assert readiness.wheel_versions(files, "faiss-cpu", PY315, "Windows x86-64") == set()


# --- Setup profiles ---------------------------------------------------------


def test_profile_checks_cover_every_recipe_and_architecture():
    profiles = {
        "no-model": {"notice": "none"},
        "cpu": {
            "platforms": {
                "Windows": {"index": "https://cpu/", "packages": ["torch==2.14.1+cpu"]},
                "Linux": {"index": "https://cpu/", "packages": ["torch==2.14.1+cpu"]},
            }
        },
        "amd": {
            "platforms": {
                "Linux": {
                    "index": "https://amd/",
                    "architectures": {"gfx1201": ["torch[device-gfx1201]==2.13.0+rocm7.14.0"]},
                }
            }
        },
    }
    assert readiness.profile_checks(profiles) == [
        ("cpu Windows", "https://cpu/", "Windows x86-64", ["torch==2.14.1+cpu"]),
        ("cpu Linux", "https://cpu/", "Linux x86-64", ["torch==2.14.1+cpu"]),
        (
            "amd Linux gfx1201",
            "https://amd/",
            "Linux x86-64",
            ["torch[device-gfx1201]==2.13.0+rocm7.14.0"],
        ),
    ]


def test_container_specs_read_the_torch_family_pins():
    text = "# comment\ntorch==2.14.1+cpu \\\n    --hash=sha256:aa\ntorchaudio==2.11.0+cpu \\\n"
    assert readiness.container_specs(text) == ["torch==2.14.1+cpu", "torchaudio==2.11.0+cpu"]


def test_check_wheels_needs_the_pinned_version_and_names_a_newer_one():
    pages = {
        "torch": ["torch-2.14.1+cpu-cp315-cp315-win_amd64.whl"],
        "torchaudio": [
            "torchaudio-2.11.0+cpu-cp314-cp314-win_amd64.whl",
            "torchaudio-2.11.1+cpu-cp315-cp315-win_amd64.whl",
        ],
        "torchvision": [],
    }
    checks = readiness.check_wheels(
        "https://cpu/",
        "Windows x86-64",
        ["torch==2.14.1+cpu", "torchaudio==2.11.0+cpu", "torchvision==0.29.1+cpu"],
        PY315,
        files=lambda _index, name: pages[name],
    )
    assert [(c.package, c.ready, c.newest) for c in checks] == [
        ("torch", True, ""),
        ("torchaudio", False, "2.11.1+cpu"),
        ("torchvision", False, ""),
    ]


def test_check_wheels_also_checks_amd_device_packages():
    asked = []

    def files(_index, name):
        asked.append(name)
        return [f"{name.replace('-', '_')}-2.13.0+rocm7.14.0-cp315-cp315-linux_x86_64.whl"]

    checks = readiness.check_wheels(
        "https://amd/",
        "Linux x86-64",
        ["torch[device-gfx1201]==2.13.0+rocm7.14.0"],
        PY315,
        files=files,
    )
    assert asked == ["torch", "amd-torch-device-gfx1201"]
    assert all(check.ready for check in checks)


def test_summarize_merges_recipes_that_share_a_check():
    missing = readiness.WheelCheck(
        "torchaudio", "2.11.0+cpu", "Linux x86-64", "https://cpu/", False, "2.11.1+cpu"
    )
    ready = readiness.WheelCheck("torch", "2.14.1+cpu", "Linux x86-64", "https://cpu/", True)
    findings = readiness.summarize(
        [("cpu Linux", missing), ("container", missing), ("cpu Linux", ready)], PY315
    )
    assert findings == [
        readiness.Finding(
            False,
            "missing: torchaudio 2.11.0+cpu on Linux x86-64 (cpu Linux, container): "
            "no cp315 wheel on https://cpu/; 2.11.1+cpu has one, move the pin first",
        ),
        readiness.Finding(True, "ready: torch 2.14.1+cpu on Linux x86-64 (cpu Linux)"),
    ]


# --- Locked dependencies ----------------------------------------------------


def test_source_only_lists_registry_packages_without_wheels():
    lock = {
        "package": [
            {"name": "muq", "source": {"registry": "https://pypi.org/simple"}, "sdist": {}},
            {"name": "numpy", "source": {"registry": "https://pypi.org/simple"}, "wheels": [{}]},
            {"name": "autodj", "source": {"editable": "."}},
        ]
    }
    assert readiness.source_only(lock) == {"muq"}


def test_check_lock_platform_lists_every_pin_without_a_wheel():
    requirements = [
        "faiss-cpu==1.15.1",
        "numpy==2.3.0",
        "triton==3.8.0 ; sys_platform == 'linux'",
    ]
    results = iter(
        [
            uv_error(
                "faiss-cpu==1.15.1",
                "has no wheels with a matching platform tag (e.g., `win_amd64`)",
            ),
            uv_error("triton{sys_platform == 'linux'}==3.8.0", "has no usable wheels"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    calls = []

    def compile_(pins, only_binary, target, platform):
        calls.append(list(pins))
        assert (only_binary, target, platform) == (["numpy"], PY315, "x86_64-manylinux_2_28")
        return next(results)

    findings = readiness.check_lock_platform(
        requirements, ["numpy"], PY315, "Linux x86-64", "x86_64-manylinux_2_28", compile_
    )
    assert [f.text for f in findings] == [
        "missing: faiss-cpu 1.15.1 on Linux x86-64: has no wheels with a matching platform tag (e.g., win_amd64)",
        "missing: triton 3.8.0 on Linux x86-64: has no usable wheels",
    ]
    assert calls[-1] == ["numpy==2.3.0"]


def test_check_lock_platform_is_ready_when_everything_resolves():
    findings = readiness.check_lock_platform(
        ["numpy==2.3.0"],
        ["numpy"],
        PY315,
        "Windows x86-64",
        "x86_64-pc-windows-msvc",
        lambda *_: subprocess.CompletedProcess([], 0, "", ""),
    )
    assert findings == [readiness.Finding(True, "ready: locked dependencies on Windows x86-64")]


def test_check_lock_platform_reports_an_unrecognised_uv_failure_once():
    findings = readiness.check_lock_platform(
        ["numpy==2.3.0"],
        ["numpy"],
        PY315,
        "Linux aarch64",
        "aarch64-manylinux_2_28",
        lambda *_: failed("error: Failed to fetch https://pypi.org/simple/numpy/\n"),
    )
    assert findings == [
        readiness.Finding(
            False,
            "missing: locked dependencies on Linux aarch64: error: Failed to fetch https://pypi.org/simple/numpy/",
        )
    ]


# --- Report -----------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".python-version").write_text("3.14\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    return tmp_path


def test_run_exits_clean_without_a_newer_minor(project, capsys):
    def unused(*_):
        raise AssertionError("nothing to check")

    status = readiness.run(project, lambda: ["Python 3.14.10"], unused, unused)
    assert status == 0
    assert capsys.readouterr().out == "No stable Python minor above 3.14 yet. Nothing to check.\n"
    assert (project / "output").read_text() == "target=\nready=false\n"


def test_run_reports_ready_when_nothing_is_missing(project, capsys):
    lock = reports(readiness.Finding(True, "ready: locked dependencies on Linux x86-64"))
    profiles = reports(
        readiness.Finding(True, "ready: torch 2.14.1+cpu on Linux x86-64 (cpu Linux)")
    )
    assert readiness.run(project, lambda: ["Python 3.15.2"], lock, profiles) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Python readiness: 3.14 to 3.15 (newest release 3.15.2)",
        "ready: locked dependencies on Linux x86-64",
        "ready: torch 2.14.1+cpu on Linux x86-64 (cpu Linux)",
        "Result: ready, everything has wheels for Python 3.15",
    ]
    assert (project / "output").read_text() == "target=3.15\nready=true\n"


def test_run_reports_not_ready_when_anything_is_missing(project, capsys):
    lock = reports(readiness.Finding(False, "missing: faiss-cpu 1.15.1 on Windows x86-64: x"))
    profiles = reports(readiness.Finding(True, "ready: x"))
    assert readiness.run(project, lambda: ["Python 3.15.2"], lock, profiles) == 1
    assert (
        capsys.readouterr().out.splitlines()[-1] == "Result: not ready, 1 missing for Python 3.15"
    )
    assert (project / "output").read_text() == "target=3.15\nready=false\n"


def test_main_reports_a_failed_check_with_status_two(monkeypatch, capsys):
    def broken():
        raise OSError("no network")

    monkeypatch.setattr(readiness, "run", broken)
    assert readiness.main([]) == 2
    assert "error: the readiness check failed: no network" in capsys.readouterr().err


# --- Renovate approval ------------------------------------------------------


def test_approve_ticks_only_the_python_box():
    body, status = readiness.approve(DASHBOARD)
    assert status == "approved"
    assert body == DASHBOARD.replace(
        " - [ ] <!-- approve-branch=renovate/python", " - [x] <!-- approve-branch=renovate/python"
    )


def test_approve_also_ticks_a_line_that_names_a_version():
    versioned = DASHBOARD.replace("update python minor release", "update python to v3.15.2")
    assert readiness.approve(versioned)[1] == "approved"


def test_approve_leaves_an_approved_or_missing_box_alone():
    ticked = DASHBOARD.replace(
        " - [ ] <!-- approve-branch=renovate/python", " - [x] <!-- approve-branch=renovate/python"
    )
    assert readiness.approve(ticked) == (ticked, "already-approved")
    assert readiness.approve("## Open\n") == ("## Open\n", "not-offered")


def test_approve_option_rewrites_the_saved_body(tmp_path, capsys):
    body = tmp_path / "dashboard.md"
    body.write_bytes(DASHBOARD.encode("utf-8"))
    assert readiness.main(["--approve", str(body)]) == 0
    assert capsys.readouterr().out == "approved\n"
    assert "- [x] <!-- approve-branch=renovate/python-minor-release -->" in body.read_text()
