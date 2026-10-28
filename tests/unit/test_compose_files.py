"""The Compose files agree with what AutoDJ runs inside the container."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import autodj.doctor as doctor
from autodj.config import load_config

ROOT = Path(__file__).resolve().parents[2]


def _service(name: str) -> dict:
    compose = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    return compose["services"]["autodj"]


def test_doctor_in_the_compose_container_sees_the_lan_mode_serve_runs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`compose exec autodj autodj doctor` is a separate process without serve's --lan."""
    monkeypatch.setattr("autodj.lan.detect_lan_hosts", lambda: ["autodj.local"])
    service = _service("compose.yaml")
    assert service["command"] == ["serve", "--lan"]
    environ = {key: str(value) for key, value in service["environment"].items()}
    # The image sets these (Containerfile ENV); the index folder is the mounted volume.
    environ |= {"AUTODJ_HOST": "0.0.0.0", "AUTODJ_INDEX_DIR": str(tmp_path / "index")}
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")

    check = doctor._network_check(load_config(config, environ=environ))

    assert (check.status, check.summary) == (doctor.CheckStatus.PASS, "LAN mode")


def test_each_engine_has_a_gpu_file_for_the_same_cuda_image() -> None:
    """Podman ignores the deploy device request, so its file names the CDI device."""
    docker = _service("compose.gpu.yaml")
    podman = _service("compose.gpu.podman.yaml")

    assert podman["image"] == docker["image"]
    assert podman["environment"] == docker["environment"]
    assert docker["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
    ]
    assert "devices" not in docker
    assert podman["devices"] == ["nvidia.com/gpu=all"]
    assert "deploy" not in podman
