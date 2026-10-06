import numpy as np
import soundfile as sf
import torch

from transcription import drumsep


class SplitEvenly(torch.nn.Module):
    """Stand-in separator: every stem receives a fifth of the input chunk."""

    def forward(self, x):
        return torch.stack([x / len(drumsep.STEMS)] * len(drumsep.STEMS), dim=1)


def test_overlap_add_reconstructs_long_and_short_inputs(tmp_path):
    rng = np.random.default_rng(0)
    for seconds in (3, 40):
        audio = rng.standard_normal((2, drumsep.SAMPLE_RATE * seconds)).astype(np.float32)
        paths = drumsep.separate(audio, SplitEvenly(), "cpu", tmp_path / str(seconds))
        stems = {name: sf.read(path, dtype="float32")[0].T for name, path in paths.items()}
        assert set(stems) == set(drumsep.STEMS)
        assert all(stem.shape == audio.shape for stem in stems.values())
        assert np.allclose(sum(stems.values()), audio, atol=1e-5)


def test_mono_input_is_duplicated_to_stereo(tmp_path):
    audio = np.ones(drumsep.SAMPLE_RATE, dtype=np.float32)
    stems = drumsep.separate(audio, SplitEvenly(), "cpu", tmp_path)
    assert sf.read(stems["kick"])[0].shape == (drumsep.SAMPLE_RATE, 2)


def full_track_reference(audio, model):
    """Previous overlap-add algorithm, including its full-track padding and buffers."""
    import torch.nn.functional as F

    chunk = drumsep.CONFIG.chunk_size
    step = chunk // drumsep.CONFIG.num_overlap
    border, fade = chunk - step, chunk // 10
    mix = torch.from_numpy(audio)
    padded = mix.shape[-1] > 2 * border
    if padded:
        mix = F.pad(mix[None], (border, border), mode="reflect")[0]
    window = torch.ones(chunk)
    window[:fade] = torch.linspace(0, 1, fade)
    window[-fade:] = torch.linspace(1, 0, fade)
    result = torch.zeros((len(drumsep.STEMS), *mix.shape))
    weight = torch.zeros(mix.shape[-1])
    starts = list(range(0, mix.shape[-1], step))
    for index, start in enumerate(starts):
        part = mix[:, start : start + chunk]
        size = part.shape[-1]
        mode = "reflect" if size > chunk // 2 else "constant"
        part = F.pad(part[None], (0, chunk - size), mode=mode)
        estimate = model(part)[0, ..., :size]
        current = window[:size].clone()
        if index == 0:
            current[:fade] = 1
        if index == len(starts) - 1:
            current[-min(fade, size) :] = 1
        result[..., start : start + size] += estimate * current
        weight[start : start + size] += current
    output = (result / weight.clamp_min(1e-8)).numpy()
    return output[..., border:-border] if padded else output


def test_streaming_matches_full_track_reference_and_bounds_buffers(tmp_path, monkeypatch):
    monkeypatch.setattr(drumsep.CONFIG, "chunk_size", 64)

    class ChunkDependent(torch.nn.Module):
        def forward(self, x):
            # Non-constant chunk-edge predictions exercise overlap weights and padding.
            return torch.stack([x * i + torch.linspace(0, i, 64) for i in range(1, 6)], 1)

    rng = np.random.default_rng(1)
    model = ChunkDependent()
    for length in (1, 16, 63, 64, 95, 96, 97, 103, 192, 211):
        audio = rng.standard_normal((2, length)).astype(np.float32)
        expected = full_track_reference(audio, model)
        # Any length-dependent result/weight allocation must fail, even for long inputs.
        original_zeros = torch.zeros

        def bounded_zeros(shape, *args, **kwargs):
            assert shape in (64, (5, 2, 64))
            return original_zeros(shape, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(torch, "zeros", bounded_zeros)
            paths = drumsep.separate(audio, model, "cpu", tmp_path / str(length))
        actual = np.stack([sf.read(path, dtype="float32")[0].T for path in paths.values()])
        np.testing.assert_allclose(actual, expected, atol=1e-6)


def test_network_matches_checkpoint_shapes():
    model = drumsep.build_model()
    names = set(model.state_dict())
    assert "first_conv.weight" in names and "final_conv.2.weight" in names
    assert model.state_dict()["final_conv.2.weight"].shape[0] == len(drumsep.STEMS) * 16
