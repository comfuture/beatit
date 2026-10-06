import numpy as np

from transcription.rhythm import align_downbeat, beats_per_bar, fit_beat_grid, refine_phase


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


def test_phase_follows_on_beat_onsets_and_ignores_offbeat_figures():
    # Grid is 30 ms late; on-beat hits are at 0.5 s intervals, plus many off-beat 16ths.
    on_beat = np.arange(40) * 0.5
    off_beat = np.arange(40) * 0.5 + 0.125
    offset, shift = refine_phase(0.03, 120, np.concatenate([on_beat, off_beat]))
    assert abs(shift + 0.03) < 1e-6
    assert abs(offset) < 1e-6 or abs(offset - 0.5) < 1e-6


def test_phase_correction_is_bounded():
    offset, shift = refine_phase(0.0, 60, np.arange(20) + 0.11)
    assert (offset, shift) == (0.0, 0.0)


def test_downbeat_alignment_keeps_pickup_in_a_partial_first_bar():
    # 120 BPM, beats every 0.5 s from 0.25 s; bars start at 1.25 s, so the hit at 0.25 s
    # is a pickup that must stay in the score rather than being dropped.
    downbeats = 1.25 + np.arange(10) * 2.0
    start, aligned = align_downbeat(0.25, 120, downbeats, "4/4", first_onset=0.25)
    assert aligned
    assert abs(start - (1.25 - 2.0)) < 1e-9


def test_downbeat_alignment_without_downbeats_keeps_quarter_phase():
    assert align_downbeat(0.25, 120, [], "4/4") == (0.25, False)


def test_beats_per_bar_from_downbeat_spacing():
    beats = np.arange(48) * 0.5
    assert beats_per_bar(beats, beats[::3]) == 3
    assert beats_per_bar(beats, []) is None
