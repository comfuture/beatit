import weakref

import numpy as np
import pytest
import soundfile as sf
import torch

from transcription import detection, drumsep, pipeline


class AmplifiedStems(torch.nn.Module):
    def forward(self, x):
        return torch.stack([x * (i + 1) * 4 for i in range(len(drumsep.STEMS))], dim=1)


def test_transcription_reads_one_float_stem_at_a_time_and_exports_clipped_flac(
    tmp_path, monkeypatch
):
    audio = np.random.default_rng(0).uniform(-0.8, 0.8, (2, 4429)).astype(np.float32)
    drums = tmp_path / "drums.wav"
    sf.write(drums, audio.T, drumsep.SAMPLE_RATE, subtype="FLOAT")
    monkeypatch.setattr(drumsep.CONFIG, "chunk_size", 4096)
    monkeypatch.setattr(drumsep, "load_model", lambda _: AmplifiedStems())
    monkeypatch.setattr(detection, "load_adtof", lambda: object())
    sources = []

    def spectrogram(source):
        index = len(sources)
        if index > 1:
            assert sources[-1]() is None, "The preceding full stem must be released"
        np.testing.assert_allclose(source, audio if index == 0 else audio * index * 4, atol=1e-5)
        sources.append(weakref.ref(source.base if isinstance(source.base, np.ndarray) else source))
        return np.zeros((10, 5), dtype=np.float32)

    monkeypatch.setattr(detection, "spectrogram", spectrogram)
    monkeypatch.setattr(detection, "activations", lambda model, inputs, device, progress: inputs)
    monkeypatch.setattr(detection, "loudness_db", lambda source: np.ones(10))

    def detect_events(kit, stems, loudness, sensitivity):
        assert set(stems) == set(loudness) == set(drumsep.STEMS)
        return [{"time": 0.05, "pitch": 35, "strength": 0.8}], kit

    monkeypatch.setattr(detection, "detect_events", detect_events)
    events, device, warnings = pipeline.transcribe(
        drums, tmp_path, "cpu", 1, lambda *args: None, False
    )
    assert events[0]["pitch"] == 35 and device == "cpu" and not warnings
    assert len(sources) == 6 and sources[-1]() is None
    for index, name in enumerate(drumsep.STEMS, 1):
        decoded, rate = sf.read(tmp_path / f"stem-{name}.flac")
        assert rate == drumsep.SAMPLE_RATE
        np.testing.assert_allclose(decoded.T, np.clip(audio * index * 4, -1, 1), atol=4e-5)
    assert not list(tmp_path.glob("drumsep-*"))


@pytest.mark.parametrize("allow_fallback", [False, True])
def test_failed_separation_cleans_scratch_before_cpu_fallback(
    tmp_path, monkeypatch, allow_fallback
):
    drums = tmp_path / "drums.wav"
    sf.write(drums, np.zeros((100, 2)), drumsep.SAMPLE_RATE)
    scratch = []

    def separate(audio, model, device, output_directory, progress):
        if scratch:
            assert not scratch[-1].exists()
        scratch.append(output_directory)
        (output_directory / "partial.wav").write_bytes(b"partial")
        raise RuntimeError(f"failure on {device}")

    monkeypatch.setattr(drumsep, "load_model", lambda _: object())
    monkeypatch.setattr(drumsep, "separate", separate)
    with pytest.raises(RuntimeError, match="cpu" if allow_fallback else "mps"):
        pipeline.transcribe(drums, tmp_path, "mps", 1, lambda *args: None, allow_fallback)
    assert len(scratch) == (2 if allow_fallback else 1)
    assert not list(tmp_path.glob("drumsep-*"))
