"""Device selection respects explicit CPU choice and labels GPU backends."""

from unittest.mock import patch

from autodj.compute import device_description, device_string


def test_explicit_cpu_choice_overrides_available_gpu(monkeypatch) -> None:
    monkeypatch.setenv("AUTODJ_GPU", "0")
    with patch("autodj.compute.torch.cuda.is_available", return_value=True):
        assert device_string() == "cpu"
        assert device_description() == "CPU"


def test_detected_amd_gpu_is_named(monkeypatch) -> None:
    monkeypatch.delenv("AUTODJ_GPU", raising=False)
    with (
        patch("autodj.compute.torch.cuda.is_available", return_value=True),
        patch("autodj.compute.torch.cuda.get_device_name", return_value="AMD Radeon 860M"),
        patch("autodj.compute.torch.version.hip", "7.14"),
    ):
        assert device_string() == "cuda"
        assert device_description() == "AMD Radeon 860M (ROCm GPU)"
