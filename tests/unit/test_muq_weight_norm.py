"""Compatibility tests for MuQ's legacy weight-normalized convolutions."""

from __future__ import annotations

import warnings
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

torch = pytest.importorskip("torch")
rvq = pytest.importorskip("muq.muq.modules.rvq")


@pytest.fixture(autouse=True)
def restore_rvq_weight_norm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep MuQ's module-local function binding isolated between tests."""
    monkeypatch.setattr(rvq, "weight_norm", rvq.weight_norm)


def _legacy_conv() -> torch.nn.Module:
    conv = torch.nn.Conv1d(2, 3, kernel_size=3, padding=1)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`torch.nn.utils.weight_norm` is deprecated",
            category=FutureWarning,
        )
        return torch.nn.utils.weight_norm(conv)


def _modern_conv() -> torch.nn.Module:
    return rvq.WNConv1d(2, 3, kernel_size=3, padding=1)


def _load_checkpoint(path: Path, model: torch.nn.Module) -> None:
    if (path / "model.safetensors").exists():
        from safetensors.torch import load_model

        load_model(model, str(path / "model.safetensors"), strict=True)
    else:
        state = torch.load(path / "pytorch_model.bin", map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=True)


def test_legacy_state_dict_loads_strictly_into_modern_rvq_conv() -> None:
    from autodj.model import _prepare_muq_weight_norm

    torch.manual_seed(2718)
    legacy = _legacy_conv().eval()
    state = legacy.state_dict()
    values = torch.randn(2, 2, 11)
    expected = legacy(values)

    _prepare_muq_weight_norm()
    modern = _modern_conv().eval()
    modern.load_state_dict(state, strict=True)

    torch.testing.assert_close(modern(values), expected)


@pytest.mark.parametrize("checkpoint_format", ["torch", "safetensors"])
def test_load_model_reads_legacy_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint_format: str
) -> None:
    safetensors = (
        pytest.importorskip("safetensors.torch") if checkpoint_format == "safetensors" else None
    )
    from autodj import model as model_module

    torch.manual_seed(31415)
    source = _legacy_conv().eval()
    state = source.state_dict()
    values = torch.randn(2, 2, 13)
    expected = source(values)
    if checkpoint_format == "torch":
        torch.save(state, tmp_path / "pytorch_model.bin")
    else:
        safetensors.save_file(state, str(tmp_path / "model.safetensors"))

    class TinyMuQ:
        @classmethod
        def from_pretrained(cls, path: str) -> torch.nn.Module:
            model = _modern_conv().eval()
            _load_checkpoint(Path(path), model)
            return model

    import muq

    monkeypatch.setattr(muq, "MuQ", TinyMuQ, raising=False)
    monkeypatch.setattr(import_module("autodj.compute"), "device_string", lambda: "cpu")
    monkeypatch.setattr(model_module, "_prepare_muq_conformer", lambda _model: None)

    loaded = model_module.load_model(tmp_path).model
    torch.testing.assert_close(loaded(values), expected)


def test_load_model_reloads_modern_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import model as model_module

    model_module._prepare_muq_weight_norm()
    torch.manual_seed(1618)
    source = _modern_conv().eval()
    state = source.state_dict()
    torch.save(state, tmp_path / "pytorch_model.bin")
    values = torch.randn(2, 2, 9)
    expected = source(values)

    class TinyMuQ:
        @classmethod
        def from_pretrained(cls, path: str) -> torch.nn.Module:
            model = _modern_conv().eval()
            _load_checkpoint(Path(path), model)
            return model

    import muq

    monkeypatch.setattr(muq, "MuQ", TinyMuQ, raising=False)
    monkeypatch.setattr(import_module("autodj.compute"), "device_string", lambda: "cpu")
    monkeypatch.setattr(model_module, "_prepare_muq_conformer", lambda _model: None)

    loaded = model_module.load_model(tmp_path).model
    torch.testing.assert_close(loaded(values), expected)


def test_helper_avoids_deprecation_warning_without_changing_torch_global() -> None:
    from autodj.model import _prepare_muq_weight_norm

    global_weight_norm = torch.nn.utils.weight_norm
    _prepare_muq_weight_norm()

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        conv: Any = _modern_conv()

    assert isinstance(conv, torch.nn.Module)
    assert rvq.weight_norm is torch.nn.utils.parametrizations.weight_norm
    assert torch.nn.utils.weight_norm is global_weight_norm
