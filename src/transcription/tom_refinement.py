"""Optional, reversible high/low tom refinement from cached kit stems.

This is relative timbre grouping, not exact fundamental-frequency or drum-size
estimation. Never invent onsets or force a label when evidence is ambiguous.
"""

from bisect import bisect_right
from pathlib import Path

import numpy as np

TOM = 47
LOW_TOM = 45
HIGH_TOM = 50
WINDOWS = ((0.025, 0.115), (0.050, 0.150), (0.080, 0.200))
MIN_WINDOW = 0.045
MAX_SPREAD_SEMITONES = 2.5
MIN_REFERENCES = 4
MIN_GROUP_SEPARATION_SEMITONES = 5.0
MIN_SIMILARITY = 0.90
MIN_MARGIN = 0.08
EDGES = np.geomspace(65, 650, 21)


def restore_inferred_toms(events: list[dict]) -> list[dict]:
    """Undo our labels, keeping explicit manual high/low tom edits unchanged."""
    restored = []
    for event in events:
        event = dict(event)
        if event.get("tom_source_pitch") == TOM:
            event["pitch"] = TOM
            event.pop("tom_source_pitch", None)
            event.pop("tom_match_score", None)
        restored.append(event)
    return restored


def _read(path):
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    return audio.mean(axis=1), rate


def _spectra(audio, rate, time, following):
    spectra = []
    fft_size = 1 << max(12, int(np.ceil(np.log2(rate * 0.6))))
    frequency = np.fft.rfftfreq(fft_size, 1 / rate)
    keep = (frequency >= EDGES[0]) & (frequency <= EDGES[-1])
    frequency = frequency[keep]
    for begin, end in WINDOWS:
        end = min(end, following - time - 0.01)
        if end - begin < MIN_WINDOW:
            continue
        segment = audio[max(0, round((time + begin) * rate)) : round((time + end) * rate)]
        if len(segment) < rate * MIN_WINDOW:
            continue
        power = np.abs(np.fft.rfft(segment * np.hanning(len(segment)), n=fft_size)) ** 2
        power = np.convolve(power[keep], np.ones(3) / 3, mode="same")
        total = float(power.sum())
        if total < 1e-12:
            continue
        peak = float(frequency[power.argmax()])
        distribution = np.array(
            [power[(frequency >= lo) & (frequency < hi)].sum() for lo, hi in zip(EDGES, EDGES[1:])]
        )
        distribution /= distribution.sum()
        spectra.append(
            {
                "peak": peak,
                "distribution": distribution,
                "concentration": float(
                    power[np.abs(np.log2(frequency / peak)) < 0.3].sum() / total
                ),
                "rms": float(np.sqrt(np.mean(segment**2))),
            }
        )
    return spectra


def _feature(spectra):
    distribution = np.median([s["distribution"] for s in spectra], axis=0)
    distribution /= distribution.sum()
    return np.sqrt(distribution)


def _prototypes(candidates):
    """Deterministic two-group fitting, with explicit contrast/support checks."""
    if len(candidates) < 2 * MIN_REFERENCES:
        return None
    ordered = sorted(candidates, key=lambda c: c["peak"])
    data = np.stack([c["feature"] for c in ordered])
    centers = data[[len(data) // 5, len(data) * 4 // 5]].copy()
    for _ in range(30):
        assignment = (data @ centers.T).argmax(axis=1)
        if any(np.sum(assignment == label) < MIN_REFERENCES for label in (0, 1)):
            return None
        updated = np.stack([data[assignment == label].mean(axis=0) for label in (0, 1)])
        updated /= np.linalg.norm(updated, axis=1, keepdims=True)
        if np.allclose(centers, updated, atol=1e-6):
            centers = updated
            break
        centers = updated
    peaks = [
        np.median([c["peak"] for c, a in zip(ordered, assignment) if a == label])
        for label in (0, 1)
    ]
    order = np.argsort(peaks)
    if 12 * abs(np.log2(peaks[0] / peaks[1])) < MIN_GROUP_SEPARATION_SEMITONES:
        return None
    if np.dot(centers[0], centers[1]) > 0.85:
        return None
    return (
        centers[order],
        [float(peaks[i]) for i in order],
        [int(np.sum(assignment == i)) for i in order],
    )


def refine_toms(events: list[dict], tom_path: Path, kick_path: Path) -> tuple[list[dict], dict]:
    refined = restore_inferred_toms(events)
    candidates = [(i, e) for i, e in enumerate(refined) if e["pitch"] == TOM]
    report = {
        "enabled": True,
        "method": "relative_tom_resonance_v1",
        "candidate_count": len(candidates),
        "low_count": 0,
        "high_count": 0,
        "unresolved_count": len(candidates),
        "status": "insufficient_evidence",
        "hits": [],
    }
    if not candidates:
        report["status"] = "no_toms"
        return refined, report
    if not tom_path.is_file() or not kick_path.is_file():
        report["status"] = "missing_stems"
        return refined, report
    toms, tom_rate = _read(tom_path)
    kick, kick_rate = _read(kick_path)
    candidates.sort(key=lambda item: item[1]["time"])
    tom_times = sorted(e["time"] for e in refined if e["pitch"] in (TOM, LOW_TOM, HIGH_TOM))
    measured = []
    for source_index, event in candidates:
        next_index = bisect_right(tom_times, event["time"])
        following = tom_times[next_index] if next_index < len(tom_times) else len(toms) / tom_rate
        detail = {"event_index": source_index, "time": event["time"], "label": "unresolved"}
        report["hits"].append(detail)
        if any(
            other is not event
            and other["pitch"] in (TOM, LOW_TOM, HIGH_TOM)
            and abs(other["time"] - event["time"]) < 0.025
            for other in refined
        ):
            detail["reason"] = "overlapping_toms"
            continue
        spectra = _spectra(toms, tom_rate, event["time"], following)
        if len(spectra) < 2:
            detail["reason"] = "short_or_silent_tail"
            continue
        peaks = np.array([s["peak"] for s in spectra])
        spread = float(12 * np.log2(peaks.max() / peaks.min()))
        detail.update(
            resonance_hz=round(float(np.median(peaks)), 1), window_spread_semitones=round(spread, 2)
        )
        if spread > MAX_SPREAD_SEMITONES or min(s["concentration"] for s in spectra) < 0.4:
            detail["reason"] = "unstable_or_broad_spectrum"
            continue
        feature = _feature(spectra)
        kick_spectra = _spectra(kick, kick_rate, event["time"], following)
        nearby = [
            e
            for e in refined
            if e["pitch"] not in (TOM, LOW_TOM, HIGH_TOM)
            and abs(e["time"] - event["time"]) <= 0.035
        ]
        if kick_spectra:
            similarity = float(np.dot(feature, _feature(kick_spectra)))
            kick_peak = float(np.median([s["peak"] for s in kick_spectra]))
            same_resonance = abs(12 * np.log2(kick_peak / np.median(peaks))) < 2.5
            kick_rms = np.median([s["rms"] for s in kick_spectra])
            tom_rms = np.median([s["rms"] for s in spectra])
            overlaps_kick = any(e["pitch"] == 35 for e in nearby)
            if same_resonance and similarity > 0.9 and (overlaps_kick or kick_rms > 2 * tom_rms):
                detail["reason"] = "kick_bleed"
                continue
        measured.append(
            {
                "index": source_index,
                "peak": float(np.median(peaks)),
                "feature": feature,
                "spectra": spectra,
                "detail": detail,
                "reference": not nearby,
            }
        )
    references = [c for c in measured if c["reference"]]
    report["reference_count"] = len(references)
    prototypes = _prototypes(references)
    if prototypes is None:
        for candidate in measured:
            candidate["detail"]["reason"] = "insufficient_group_contrast"
        return refined, report
    centers, peaks, support = prototypes
    report["status"] = "completed"
    report["groups"] = [
        {"label": label, "resonance_hz": round(peak, 1), "reference_count": count}
        for label, peak, count in zip(("low", "high"), peaks, support)
    ]
    for candidate in measured:
        scores = candidate["feature"] @ centers.T
        winner = int(scores.argmax())
        score, margin = float(scores[winner]), float(abs(scores[0] - scores[1]))
        votes = [
            int((np.sqrt(s["distribution"]) @ centers.T).argmax()) for s in candidate["spectra"]
        ]
        detail = candidate["detail"]
        detail.update(match_score=round(score, 4), margin=round(margin, 4))
        if score < MIN_SIMILARITY or margin < MIN_MARGIN or any(v != winner for v in votes):
            detail["reason"] = "ambiguous_template_match"
            continue
        label = ("low", "high")[winner]
        refined[candidate["index"]].update(
            pitch=(LOW_TOM, HIGH_TOM)[winner], tom_source_pitch=TOM, tom_match_score=round(score, 4)
        )
        detail.update(label=label, reason="consistent_resonance_match")
        report[f"{label}_count"] += 1
    report["unresolved_count"] = len(candidates) - report["low_count"] - report["high_count"]
    return refined, report
