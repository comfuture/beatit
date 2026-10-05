import numpy as np

from transcription.pipeline import fit_beat_grid


def test_global_beat_fit_recovers_tempo_beyond_frame_resolution():
    actual_bpm = 131
    period = 60 / actual_bpm
    beats = np.round((np.arange(400) * period + 0.04) / (512 / 22050)) * (512 / 22050)
    bpm, offset, residual = fit_beat_grid(beats)
    assert abs(bpm - actual_bpm) < 0.02
    assert abs(offset - 0.04) < 0.002
    assert residual < 0.01


def test_too_few_beats_have_explicit_default():
    assert fit_beat_grid([0, 0.5]) == (120.0, 0.0, 0.0)
