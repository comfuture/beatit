import numpy as np
import torch

from transcription import drumsep


class SplitEvenly(torch.nn.Module):
    """Stand-in separator: every stem receives a fifth of the input chunk."""

    def forward(self, x):
        return torch.stack([x / len(drumsep.STEMS)] * len(drumsep.STEMS), dim=1)


def test_overlap_add_reconstructs_long_and_short_inputs():
    rng = np.random.default_rng(0)
    for seconds in (3, 40):
        audio = rng.standard_normal((2, drumsep.SAMPLE_RATE * seconds)).astype(np.float32)
        stems = drumsep.separate(audio, SplitEvenly(), "cpu")
        assert set(stems) == set(drumsep.STEMS)
        assert all(stem.shape == audio.shape for stem in stems.values())
        assert np.allclose(sum(stems.values()), audio, atol=1e-5)


def test_mono_input_is_duplicated_to_stereo():
    audio = np.ones(drumsep.SAMPLE_RATE, dtype=np.float32)
    stems = drumsep.separate(audio, SplitEvenly(), "cpu")
    assert stems["kick"].shape == (2, drumsep.SAMPLE_RATE)


def test_network_matches_checkpoint_shapes():
    model = drumsep.build_model()
    names = set(model.state_dict())
    assert "first_conv.weight" in names and "final_conv.2.weight" in names
    assert model.state_dict()["final_conv.2.weight"].shape[0] == len(drumsep.STEMS) * 16
