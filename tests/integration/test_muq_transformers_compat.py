"""Exercise real MuQ/Transformers inference without downloading model weights."""

import json

import numpy as np
import pytest
import torch
from easydict import EasyDict
from transformers.models.wav2vec2_conformer.modeling_wav2vec2_conformer import (
    Wav2Vec2ConformerConfig,
    Wav2Vec2ConformerEncoder,
)

from autodj.model import _MuqConformerAdapter, _prepare_muq_conformer, load_model


def _tiny_config():
    return Wav2Vec2ConformerConfig(
        hidden_size=8,
        num_attention_heads=2,
        intermediate_size=16,
        num_hidden_layers=1,
        num_conv_pos_embeddings=4,
        num_conv_pos_embedding_groups=2,
        conv_depthwise_kernel_size=3,
        attn_implementation="eager",
    )


@pytest.mark.parametrize("dictionary_config", [False, True])
@pytest.mark.parametrize("masked", [False, True])
def test_adapter_preserves_weights_and_native_final_layer(dictionary_config, masked):
    """The compatibility layer preserves native eager inference, including masks."""
    config = _tiny_config()
    legacy = EasyDict(json.loads(config.to_json_string(use_diff=False)))
    encoder = Wav2Vec2ConformerEncoder(legacy if dictionary_config else config).eval()
    reference = Wav2Vec2ConformerEncoder(config).eval()
    reference.load_state_dict(encoder.state_dict())
    parameters = list(encoder.parameters())
    adapter = _MuqConformerAdapter(encoder).eval()
    assert all(a is b for a, b in zip(parameters, adapter.parameters(), strict=True))
    assert encoder.config._attn_implementation == "eager"
    assert encoder.layers[0].self_attn.config is encoder.config

    inputs = torch.randn(2, 16, 8)
    mask = None
    if masked:
        mask = torch.ones(2, 16, dtype=torch.bool)
        mask[0, -4:] = False
    with torch.inference_mode():
        expected = reference(inputs.clone(), attention_mask=mask).last_hidden_state
        result = adapter(inputs.clone(), attention_mask=mask)
        without_history = adapter(inputs.clone(), attention_mask=mask, output_hidden_states=False)
    torch.testing.assert_close(result.last_hidden_state, expected)
    assert len(result.hidden_states) == 1
    assert result.hidden_states[0] is result.last_hidden_state
    assert without_history.hidden_states is None
    assert torch.isfinite(result.last_hidden_state).all()


def test_prepare_leaves_alternate_encoder_unchanged():
    model = torch.nn.Module()
    model.model = torch.nn.Module()
    encoder = torch.nn.Identity()
    model.model.conformer = encoder
    _prepare_muq_conformer(model)
    assert model.model.conformer is encoder


def test_load_model_runs_real_muq_inference(tmp_path, monkeypatch):
    """Loading must adapt MuQ before its first audio forward, not just import it."""
    from muq import MuQ
    from muq.muq.muq import MuQConfig

    config = MuQConfig(
        encoder_dim=8,
        encoder_depth=1,
        conv_dim=8,
        codebook_size=8,
        w2v2_config=json.loads(_tiny_config().to_json_string(use_diff=False)),
        stat={"melspec_2048_mean": 0.0, "melspec_2048_std": 1.0},
    )
    model = MuQ(config)
    monkeypatch.setattr(MuQ, "from_pretrained", lambda _path: model)
    monkeypatch.setattr("autodj.compute.device_string", lambda: "cpu")
    wrapper = load_model(tmp_path)
    assert isinstance(model.model.conformer, _MuqConformerAdapter)
    _prepare_muq_conformer(model)  # Preparing a loaded model twice must be harmless.
    vector = wrapper.embed_array(np.zeros(24000, dtype=np.float32), 24000)
    assert vector.shape == (8,)
    assert np.isfinite(vector).all()
    assert np.linalg.norm(vector) == pytest.approx(1.0)
