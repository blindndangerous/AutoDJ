"""Exercise MuQ's Transformers configuration boundary without model downloads."""

import json

import torch
from easydict import EasyDict
from transformers.models.wav2vec2_conformer.modeling_wav2vec2_conformer import (
    Wav2Vec2ConformerConfig,
    Wav2Vec2ConformerEncoder,
)


def test_muq_easydict_config_can_run_conformer_inference():
    """MuQ supplies a plain EasyDict, not a Transformers PretrainedConfig."""
    config = Wav2Vec2ConformerConfig(
        hidden_size=8,
        num_attention_heads=2,
        intermediate_size=16,
        num_hidden_layers=1,
        num_conv_pos_embeddings=4,
        num_conv_pos_embedding_groups=2,
        conv_depthwise_kernel_size=3,
    )
    encoder = Wav2Vec2ConformerEncoder(
        EasyDict(json.loads(config.to_json_string(use_diff=False)))
    ).eval()
    with torch.inference_mode():
        result = encoder(torch.zeros(1, 16, 8), output_hidden_states=True)
    assert result.last_hidden_state.shape == (1, 16, 8)
    assert torch.isfinite(result.last_hidden_state).all()
