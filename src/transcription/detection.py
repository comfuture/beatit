"""Stem-assisted ADTOF onset detection, open hi-hat classification and velocity.

ADTOF is run on the Demucs drum stem and on each DrumSep stem. For every class the kit
activation and the activation from that class's own stem are averaged before peak picking.
On MDB Drums (23 tracks, ghost notes ignored) this raised 5-class onset F-measure from
0.864 to 0.882 under two-fold cross-validation; ADTOF on the stems alone was worse.
"""

from pathlib import Path

import numpy as np

FPS = 100
CLASSES = (35, 38, 47, 42, 49)  # ADTOF LABELS_5 order.
STEM_FOR = {35: "kick", 38: "snare", 47: "toms", 42: "hh", 49: "cymbals"}
# Multipliers of ADTOF's default thresholds for the averaged activation, chosen on MDB Drums.
THRESHOLD_SCALE = {35: 0.7, 38: 0.7, 47: 0.85, 42: 1.2, 49: 0.5}
# A hi-hat is open when its stem loudness falls by less than 8 dB before the next hit
# (max 150 ms) and by less than 30% of the hit's headroom above the local noise floor.
# The relative test keeps quiet hits near the floor from looking sustained. On detected
# MDB onsets: precision 0.74, recall 0.51; favouring precision keeps scores readable.
OPEN_HAT_MAX_DROP_DB = 8.0
OPEN_HAT_MAX_DROP_RATIO = 0.3
OPEN_HAT_WINDOW = 0.15
NOISE_FLOOR_WINDOW = 2.0


def load_adtof():
    import torch
    from adtof_pytorch import calculate_n_bins, create_frame_rnn_model, get_default_weights_path

    # Never run with silently missing/randomly initialized model parameters.
    weights = Path(get_default_weights_path())
    if not weights.is_file():
        raise RuntimeError("ADTOF 모델 가중치가 없습니다. uv sync를 다시 실행하세요.")
    model = create_frame_rnn_model(calculate_n_bins()).eval()
    state = torch.load(weights, map_location="cpu", weights_only=True)
    model.load_state_dict(state.get("model_weights", state), strict=True)
    return model


def mono(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    return audio.mean(axis=0) if audio.ndim == 2 else audio


def spectrogram(audio: np.ndarray):
    """ADTOF input for 44.1 kHz audio; identical to adtof_pytorch.load_audio_for_model."""
    import torch
    from adtof_pytorch.audio import create_adtof_processor

    processor = create_adtof_processor()
    filtered = processor.apply_filterbank(processor.compute_stft(mono(audio)))
    return torch.from_numpy(np.ascontiguousarray(filtered.T[None, :, :, None]))


def activations(model, inputs, device: str, progress=None) -> np.ndarray:
    import torch

    model.to(device)
    frames = inputs.shape[1]
    predictions = []
    block, context = 3000, 100  # 30 s windows with 1 s context on both edges.
    with torch.inference_mode():
        for start in range(0, frames, block):
            end = min(frames, start + block)
            left, right = max(0, start - context), min(frames, end + context)
            prediction = model(inputs[:, left:right].to(device)).cpu().numpy()[0]
            predictions.append(prediction[start - left : end - left])
            if progress:
                progress(end / frames)
    return np.concatenate(predictions, axis=0)


def loudness_db(audio: np.ndarray) -> np.ndarray:
    """Frame RMS in dB at 100 frames per second, frames centred on their timestamps."""
    import librosa

    rms = librosa.feature.rms(y=mono(audio), frame_length=1024, hop_length=441, center=True)[0]
    return 20 * np.log10(np.maximum(rms, 1e-6))


def pick_onsets(activation: np.ndarray, threshold: float) -> list[float]:
    from adtof_pytorch.post_processing import NotePeakPickingProcessor

    # Same peak-picking parameters as adtof_pytorch.PeakPicker.
    picker = NotePeakPickingProcessor(
        threshold=threshold,
        pre_avg=0.1,
        post_avg=0.01,
        pre_max=0.02,
        post_max=0.01,
        combine=0.02,
        fps=FPS,
    )
    return [time for time, _ in picker.process(activation)]


def _peak(loudness: np.ndarray, time: float) -> float:
    index = int(round(time * FPS))
    window = loudness[max(0, index - 2) : index + 3]
    return float(window.max()) if len(window) else -120.0


def open_hihats(onsets: list[float], loudness: np.ndarray) -> list[bool]:
    flags = []
    radius = int(NOISE_FLOOR_WINDOW * FPS)
    for index, time in enumerate(onsets):
        following = onsets[index + 1] if index + 1 < len(onsets) else time + OPEN_HAT_WINDOW
        end = min(following, time + OPEN_HAT_WINDOW)
        start = int(round(time * FPS))
        segment = loudness[start : int(round(end * FPS))]
        if len(segment) < 2:
            flags.append(False)
            continue
        peak = _peak(loudness, time)
        floor = float(np.percentile(loudness[max(0, start - radius) : start + radius], 5))
        drop = peak - float(segment.min())
        flags.append(
            drop < OPEN_HAT_MAX_DROP_DB and drop < OPEN_HAT_MAX_DROP_RATIO * (peak - floor)
        )
    return flags


def velocities(onsets: list[float], loudness: np.ndarray) -> list[int]:
    """MIDI velocity from stem loudness relative to the instrument's loud hits."""
    if not onsets:
        return []
    peaks = np.array([_peak(loudness, time) for time in onsets])
    relative = peaks - np.percentile(peaks, 95)
    return [int(v) for v in np.clip(np.round(112 + 2.5 * relative), 16, 127)]


def detect_events(kit: np.ndarray, stems: dict, loudness: dict, sensitivity: float):
    """Combine kit/stem activations into events. Returns (events, combined activations)."""
    from adtof_pytorch import FRAME_RNN_THRESHOLDS

    frames = min([len(kit)] + [len(stems[name]) for name in STEM_FOR.values()])
    combined = np.zeros((frames, len(CLASSES)), dtype=np.float32)
    events = []
    for index, pitch in enumerate(CLASSES):
        name = STEM_FOR[pitch]
        combined[:, index] = (kit[:frames, index] + stems[name][:frames, index]) / 2
        threshold = min(0.95, FRAME_RNN_THRESHOLDS[index] * THRESHOLD_SCALE[pitch] / sensitivity)
        onsets = pick_onsets(combined[:, index], threshold)
        opened = open_hihats(onsets, loudness[name]) if pitch == 42 else [False] * len(onsets)
        for time, is_open, velocity in zip(onsets, opened, velocities(onsets, loudness[name])):
            frame = min(int(round(time * FPS)), frames - 1)
            events.append(
                {
                    "time": round(time, 4),
                    "pitch": 46 if is_open else pitch,
                    "strength": round(float(combined[frame, index]), 4),
                    "velocity": velocity,
                }
            )
    events.sort(key=lambda event: (event["time"], event["pitch"]))
    return events, combined
