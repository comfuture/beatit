import numpy as np

from transcription.detection import CLASSES, detect_events, open_hihats, velocities


def envelope(frames, onsets, decay_db_per_frame, floor=-80.0):
    loudness = np.full(frames, floor)
    for onset in onsets:
        index = int(onset * 100)
        tail = -6 - decay_db_per_frame * np.arange(frames - index)
        loudness[index:] = np.maximum(loudness[index:], tail)
    return loudness


def test_slow_decay_hi_hat_is_open_fast_decay_is_closed():
    onsets = [0.5, 1.0]
    assert open_hihats(onsets, envelope(200, onsets, 0.2)) == [True, True]
    assert open_hihats(onsets, envelope(200, onsets, 3.0)) == [False, False]


def test_slow_decay_close_to_the_noise_floor_is_not_open():
    # 0.4 dB/frame keeps a loud hit above 70% of its headroom but not a hit 12 dB above floor.
    onsets = [0.5, 1.0]
    assert open_hihats(onsets, envelope(200, onsets, 0.4, floor=-18.0)) == [False, False]
    assert open_hihats(onsets, envelope(200, onsets, 0.4, floor=-80.0)) == [True, True]


def test_velocity_follows_relative_stem_loudness():
    loudness = np.full(300, -80.0)
    loudness[100], loudness[200] = -6.0, -26.0
    loud, soft = velocities([1.0, 2.0], loudness)
    assert loud > soft >= 16


def test_kit_and_stem_activations_are_averaged_per_class():
    frames = 400
    kit = np.zeros((frames, len(CLASSES)), dtype=np.float32)
    stems = {name: np.zeros_like(kit) for name in ("kick", "snare", "toms", "hh", "cymbals")}
    # Kick is confirmed by both inputs; the snare peak only exists in the kit activation and
    # is too weak once averaged with a silent snare stem.
    kit[100, 0] = stems["kick"][100, 0] = 0.9
    kit[200, 1] = 0.3
    loudness = {name: np.full(frames, -60.0) for name in stems}
    events, combined = detect_events(kit, stems, loudness, sensitivity=1.0)
    assert [(event["time"], event["pitch"]) for event in events] == [(1.0, 35)]
    assert combined.shape == (frames, len(CLASSES))
    assert 1 <= events[0]["velocity"] <= 127
