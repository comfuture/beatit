"""Beat, downbeat and tempo-grid estimation.

Beat This! (CPJKU, MIT code and weights) replaces Librosa beat tracking. On MDB Drums it
raised beat F-measure from 0.72 to 0.93 and removed most of Librosa's ~30 ms late bias.
The fitted global grid is then phase-corrected against detected drum onsets and its first
bar is aligned to the predicted downbeats.
"""

import hashlib
from pathlib import Path

import numpy as np

BEAT_THIS_URL = "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"
BEAT_THIS_SHA256 = "8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331"
MAX_PHASE_CORRECTION = 0.08


def fit_beat_grid(beats) -> tuple[float, float, float]:
    """Fit precise tempo/phase rather than use quantized STFT tempo bins."""
    beats = np.asarray(beats, dtype=float)
    if len(beats) < 4:
        return 120.0, 0.0, 0.0
    indices = np.arange(len(beats))
    period, intercept = np.polyfit(indices, beats, 1)
    if period <= 0:
        return 120.0, 0.0, 0.0
    residual = float(np.sqrt(np.mean((beats - (indices * period + intercept)) ** 2)))
    # Quarter-note phase only; downbeats are applied separately.
    offset = float(intercept % period)
    return float(60 / period), offset, residual


def refine_phase(offset: float, bpm: float, onsets) -> tuple[float, float]:
    """Shift the grid by the median deviation of on-beat drum onsets.

    Only onsets within an eighth of a beat from a grid beat are used, so off-beat,
    swung or triplet figures do not pull the grid.
    """
    period = 60 / bpm
    onsets = np.asarray(onsets, dtype=float)
    if len(onsets) == 0:
        return offset, 0.0
    deviation = (onsets - offset + period / 2) % period - period / 2
    deviation = deviation[np.abs(deviation) < period / 8]
    if len(deviation) < 8:
        return offset, 0.0
    shift = float(np.median(deviation))
    if abs(shift) >= MAX_PHASE_CORRECTION:
        return offset, 0.0
    return float((offset + shift) % period), shift


def beats_per_bar(beats, downbeats) -> int | None:
    """Most common number of tracked beats between consecutive downbeats."""
    beats, downbeats = np.asarray(beats, dtype=float), np.asarray(downbeats, dtype=float)
    if len(beats) < 2 or len(downbeats) < 3:
        return None
    indices = np.array([int(np.argmin(np.abs(beats - d))) for d in downbeats])
    spans = np.diff(indices)
    spans = spans[spans > 0]
    return int(np.bincount(spans).argmax()) if len(spans) else None


def align_downbeat(
    offset: float, bpm: float, downbeats, meter: str, first_onset: float | None = None
) -> tuple[float, bool]:
    """Start bar 1 on the beat phase where most predicted downbeats fall.

    The bar start is then moved back by whole bars until it precedes the first onset, so a
    pickup becomes a partial first bar instead of being dropped. It may be negative.
    """
    numerator, denominator = map(int, meter.split("/"))
    quarters = numerator * 4 / denominator
    downbeats = np.asarray(downbeats, dtype=float)
    if len(downbeats) < 2 or quarters != int(quarters):
        return offset, False
    period = 60 / bpm
    indices = np.round((downbeats - offset) / period).astype(int)
    phase = int(np.bincount(indices % int(quarters)).argmax())
    start = offset + phase * period
    bar = quarters * period
    # Half a sixteenth of tolerance, matching the quantizer's early-hit cutoff.
    earliest = (first_onset if first_onset is not None else 0.0) + period / 8
    while start > earliest:
        start -= bar
    return float(start), True


def beat_this_checkpoint(download: bool = True) -> Path:
    import torch

    path = Path(torch.hub.get_dir()) / "checkpoints" / "beat_this-final0.ckpt"
    if not path.is_file():
        if not download:
            raise RuntimeError(
                "Beat This! 모델이 없습니다. drum-score download-model을 실행하세요."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.hub.download_url_to_file(BEAT_THIS_URL, str(path), progress=False)
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(1 << 22):
            digest.update(block)
    if digest.hexdigest() != BEAT_THIS_SHA256:
        raise ValueError(f"Beat This! 가중치의 SHA-256이 일치하지 않습니다: {path}")
    return path


def _librosa_beats(path: Path):
    import librosa

    signal, sr = librosa.load(path, sr=22050, mono=True)
    if np.max(np.abs(signal)) < 1e-5:
        return np.array([])
    _, frames = librosa.beat.beat_track(y=signal, sr=sr, hop_length=512, trim=False)
    return librosa.frames_to_time(frames, sr=sr, hop_length=512)


def estimate_tempo(
    mix: Path, drums: Path, onsets, device: str, allow_fallback: bool
) -> tuple[dict, list[str]]:
    import soundfile as sf

    warnings = []
    sources = [("drums", drums)] if mix == drums else [("mix", mix), ("drums", drums)]
    tracked = {}

    def track(selected):
        from beat_this.inference import Audio2Beats

        tracker = Audio2Beats(checkpoint_path=str(beat_this_checkpoint()), device=selected)
        for label, path in sources:
            signal, rate = sf.read(path, dtype="float32")
            if np.max(np.abs(signal), initial=0) < 1e-5:
                tracked[label] = (np.array([]), np.array([]))
                continue
            beats, downbeats = tracker(signal, rate)
            tracked[label] = (np.asarray(beats, dtype=float), np.asarray(downbeats, dtype=float))

    try:
        track(device)
    except RuntimeError:
        if device == "cpu" or not allow_fallback:
            raise
        warnings.append(f"Beat This! {device.upper()} 실행 실패로 CPU에서 다시 실행했습니다.")
        track("cpu")

    candidates = []
    for label, (beats, downbeats) in tracked.items():
        bpm, offset, residual = fit_beat_grid(beats)
        if len(beats) >= 8 and 30 <= bpm <= 300:
            candidates.append((residual, label, beats, downbeats, bpm, offset))
    if candidates:
        # The source whose beats best fit one steady tempo is usually the correct octave.
        residual, label, beats, _, bpm, offset = min(candidates, key=lambda item: item[0])
        tracker_name = f"beat_this:{label}"
    else:
        beats = _librosa_beats(drums)
        bpm, offset, residual = fit_beat_grid(beats)
        tracker_name = "librosa:drums"
        warnings.append("Beat This!가 비트를 충분히 찾지 못해 Librosa 추정으로 대체했습니다.")
    reliable = len(beats) >= 4 and 30 <= bpm <= 300
    if not reliable:
        bpm, offset = 120.0, 0.0
    offset, correction = refine_phase(offset, bpm, onsets) if reliable else (offset, 0.0)
    # Downbeats from the full mix were more accurate on MDB (F 0.86 vs 0.78 on drums).
    downbeats = tracked.get("mix", tracked.get("drums", (None, np.array([]))))[1]
    if not candidates:
        downbeats = np.array([])
    return {
        "estimated_bpm": round(bpm, 3),
        "estimated_offset": round(offset, 4),
        "beat_tracker": tracker_name,
        "beat_fit_residual_ms": round(residual * 1000, 1),
        "phase_correction_ms": round(correction * 1000, 1),
        "beat_times": [round(float(t), 4) for t in beats],
        "downbeat_times": [round(float(t), 4) for t in downbeats],
        "downbeat_beats_per_bar": beats_per_bar(beats, downbeats),
        "tempo_reliable": reliable,
    }, warnings
