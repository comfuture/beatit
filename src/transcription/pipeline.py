import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from .engrave import render
from .models import Options
from .runtime import select_device
from .score import KIT, quantize, write_musicxml

MAX_DURATION = float(os.environ.get("MAX_DURATION_SECONDS", "1800"))


def save_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Progress:
    def __init__(self, directory: Path):
        self.directory = directory
        self.started = time.monotonic()

    def __call__(self, stage: str, fraction: float, message: str):
        save_json(
            self.directory / "progress.json",
            {
                "stage": stage,
                "progress": round(fraction, 3),
                "message": message,
                "elapsed_seconds": round(time.monotonic() - self.started, 1),
            },
        )
        print(f"[{stage}] {message}", flush=True)


def convert_media(source: Path, destination: Path) -> float:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg/ffprobe가 필요합니다. 실행 안내의 설치 명령을 확인하세요.")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode:
        raise ValueError("미디어 파일을 읽을 수 없습니다: " + probe.stderr[-700:])
    metadata = json.loads(probe.stdout)
    if not any(s.get("codec_type") == "audio" for s in metadata.get("streams", [])):
        raise ValueError("이 파일에는 오디오 트랙이 없습니다.")
    duration = float(metadata.get("format", {}).get("duration", 0))
    if duration > MAX_DURATION:
        raise ValueError(f"최대 {MAX_DURATION / 60:g}분까지 분석할 수 있습니다.")
    converted = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            str(MAX_DURATION + 1),
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if converted.returncode:
        raise ValueError("오디오 변환 실패: " + converted.stderr[-700:])
    import soundfile as sf

    duration = sf.info(destination).duration
    if not 0.1 <= duration <= MAX_DURATION:
        raise ValueError(f"오디오는 0.1초 이상, {MAX_DURATION / 60:g}분 이하여야 합니다.")
    return duration


def separate(
    directory: Path, device: str, progress: Progress, allow_fallback: bool
) -> tuple[Path, str, list[str]]:
    progress(
        "separating",
        0.15,
        f"Demucs로 드럼을 분리합니다 · {device.upper()} · 첫 실행 시 모델 다운로드",
    )
    output = directory / "stems"
    command = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        "htdemucs",
        "--two-stems",
        "drums",
        "--other-method",
        "none",
        "--segment",
        "6",
        "--shifts",
        "1",
        "-d",
        device,
        "-o",
        str(output),
        str(directory / "audio.wav"),
    ]
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    result = subprocess.run(command, env=env, timeout=7200)
    warnings = []
    if result.returncode:
        if device != "cpu" and allow_fallback:
            warnings.append(
                f"Demucs {device.upper()} 실행 실패로 CPU에서 다시 실행했습니다. 로그를 확인하세요."
            )
            progress("separating", 0.16, "GPU 실행에 실패했습니다. CPU에서 다시 시도합니다.")
            command[command.index("-d") + 1] = "cpu"
            result = subprocess.run(command, env=env, timeout=7200)
            device = "cpu"
        if result.returncode:
            raise RuntimeError("Demucs 드럼 분리에 실패했습니다. 작업 로그를 확인하세요.")
    stem = output / "htdemucs" / "audio" / "drums.wav"
    if not stem.is_file():
        raise RuntimeError("Demucs 출력에 drums.wav가 없습니다.")
    destination = directory / "drums.wav"
    shutil.move(stem, destination)
    shutil.rmtree(output)
    return destination, device, warnings


def detect(
    audio: Path,
    directory: Path,
    device: str,
    sensitivity: float,
    progress: Progress,
    allow_fallback: bool,
):
    import numpy as np
    import torch
    from adtof_pytorch import (
        FRAME_RNN_THRESHOLDS,
        LABELS_5,
        PeakPicker,
        calculate_n_bins,
        create_frame_rnn_model,
        get_default_weights_path,
        load_audio_for_model,
    )

    # Never run with silently missing/randomly initialized model parameters.
    weights = Path(get_default_weights_path())
    if not weights.is_file():
        raise RuntimeError("ADTOF 모델 가중치가 없습니다. uv sync를 다시 실행하세요.")
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    torch.manual_seed(0)
    model = create_frame_rnn_model(calculate_n_bins()).eval()
    state = torch.load(weights, map_location="cpu", weights_only=True)
    model.load_state_dict(state.get("model_weights", state), strict=True)
    progress("transcribing", 0.45, "ADTOF 입력 스펙트로그램을 만듭니다.")
    inputs = load_audio_for_model(str(audio))

    def infer(selected):
        model.to(selected)
        frames = inputs.shape[1]
        predictions = []
        block, context = 3000, 100  # 30s windows with 1s context on both edges.
        with torch.inference_mode():
            for start in range(0, frames, block):
                end = min(frames, start + block)
                left, right = max(0, start - context), min(frames, end + context)
                prediction = model(inputs[:, left:right].to(selected)).cpu().numpy()[0]
                predictions.append(prediction[start - left : end - left])
                progress(
                    "transcribing",
                    0.48 + 0.24 * end / frames,
                    f"드럼 타격 분석 · {end / 100:.1f} / {frames / 100:.1f}초 · {selected.upper()}",
                )
        return np.concatenate(predictions, axis=0)

    warnings = []
    try:
        activations = infer(device)
    except RuntimeError:
        if device == "cpu" or not allow_fallback:
            raise
        warnings.append(f"ADTOF {device.upper()} 실행 실패로 CPU에서 다시 실행했습니다.")
        device = "cpu"
        activations = infer(device)
    np.save(directory / "activations.npy", activations)
    thresholds = [min(0.95, value / sensitivity) for value in FRAME_RNN_THRESHOLDS]
    peaks = PeakPicker(thresholds=thresholds).pick(activations, labels=LABELS_5)[0]
    events = []
    for class_index, pitch in enumerate(LABELS_5):
        for onset in peaks[pitch]:
            frame = min(round(onset * 100), len(activations) - 1)
            events.append(
                {
                    "time": round(onset, 4),
                    "pitch": pitch,
                    "strength": round(float(activations[frame, class_index]), 4),
                }
            )
    events.sort(key=lambda e: (e["time"], e["pitch"]))
    return events, device, warnings


def estimate_tempo(audio: Path) -> dict:
    import librosa
    import numpy as np

    signal, sr = librosa.load(audio, sr=22050, mono=True)
    if np.max(np.abs(signal)) < 1e-5:
        return {
            "estimated_bpm": 120.0,
            "estimated_offset": 0.0,
            "beat_times": [],
            "tempo_reliable": False,
        }
    tempo, frames = librosa.beat.beat_track(y=signal, sr=sr, hop_length=512, trim=False)
    beats = librosa.frames_to_time(frames, sr=sr, hop_length=512)
    coarse_bpm = float(np.asarray(tempo).reshape(-1)[0])
    bpm, offset, residual = fit_beat_grid(beats)
    reliable = len(beats) >= 4 and 30 <= bpm <= 300
    bpm = bpm if reliable else 120
    # Preserve the whole track. A beat tracker does not identify the downbeat/meter.
    # Use its phase near the track start rather than discard a possibly long intro.
    return {
        "estimated_bpm": round(bpm, 3),
        "estimated_offset": round(offset, 4),
        "coarse_bpm": round(coarse_bpm, 3),
        "beat_fit_residual_ms": round(residual * 1000, 1),
        "beat_times": [round(float(t), 4) for t in beats],
        "tempo_reliable": reliable,
    }


def fit_beat_grid(beats) -> tuple[float, float, float]:
    """Fit precise tempo/phase rather than use quantized STFT tempo bins."""
    import numpy as np

    beats = np.asarray(beats, dtype=float)
    if len(beats) < 4:
        return 120.0, 0.0, 0.0
    indices = np.arange(len(beats))
    period, intercept = np.polyfit(indices, beats, 1)
    if period <= 0:
        return 120.0, 0.0, 0.0
    residual = float(np.sqrt(np.mean((beats - (indices * period + intercept)) ** 2)))
    # No automatic downbeat inference; this is only a quarter-note phase.
    offset = float(intercept % period)
    return float(60 / period), offset, residual


def write_midi(events: list[dict], target: Path, bpm: float):
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    drums = pretty_midi.Instrument(program=0, is_drum=True, name="Drum set")
    for event in events:
        onset = event.get("quantized_time", event["time"])
        drums.notes.append(
            pretty_midi.Note(
                pitch=event["pitch"],
                velocity=max(1, min(127, round(event["strength"] * 110 + 17))),
                start=onset,
                end=onset + 0.08,
            )
        )
    midi.instruments.append(drums)
    midi.write(str(target))


def make_score(
    directory: Path, request: dict, analysis: dict, events: list[dict], progress: Progress
) -> dict:
    options = Options.model_validate(request["options"])
    resolved = options.model_copy(
        update={
            "bpm": options.bpm or analysis["estimated_bpm"],
            "offset": options.offset
            if options.offset is not None
            else analysis["estimated_offset"],
        }
    )
    progress("engraving", 0.8, "타격을 리듬 격자에 맞추고 MusicXML을 생성합니다.")
    grid = quantize(events, analysis["duration"], resolved)
    # Publish a complete generation atomically; never expose half-written revisions.
    generation = directory / ("render-" + str(time.time_ns()))
    generation.mkdir()
    write_musicxml(grid, Path(request["filename"]).stem, generation / "score.musicxml")
    write_midi(grid["events"], generation / "score.mid", grid["bpm"])
    write_midi(events, generation / "performance.mid", grid["bpm"])
    save_json(generation / "events.json", events)
    progress("engraving", 0.87, "출력용 SVG 페이지를 조판합니다.")
    rendered = render(generation / "score.musicxml", generation, options.renderer)
    counts = {name: sum(e["pitch"] == pitch for e in events) for pitch, (name, *_) in KIT.items()}
    warnings = list(analysis.get("warnings", [])) + rendered["warnings"]
    if not events:
        warnings.append(
            "드럼 타격을 검출하지 못했습니다. 감도를 높이거나 드럼 전용 입력을 확인하세요."
        )
    if not analysis["tempo_reliable"] and options.bpm is None:
        warnings.append(
            "BPM을 안정적으로 추정하지 못해 120 BPM으로 표시합니다. BPM을 직접 지정하세요."
        )
    warnings.append(
        "박자와 첫 마디 위치는 자동 판별하지 않습니다. 템포 변화·스윙은 고정 격자에 근사하므로 원음과 대조하세요."
    )
    if analysis.get("beat_fit_residual_ms", 0) > 60:
        warnings.append(
            "비트 위치가 일정한 템포에서 벗어납니다. 템포 변화나 누락된 비트를 확인하세요."
        )
    if grid["dropped_before_offset"]:
        warnings.append(
            f"첫 박 앞의 {grid['dropped_before_offset']}개 타격이 악보에서 제외되었습니다. 박 시작 위치를 조정하세요."
        )
    if grid["mean_quantization_error_ms"] > 45:
        warnings.append("타격과 리듬 격자의 평균 차이가 큽니다. BPM·첫 박·격자를 확인하세요.")
    result = {
        **analysis,
        "options": resolved.model_dump(),
        "grid": grid,
        "counts": counts,
        "renderer": rendered["renderer"],
        "pdf": rendered["pdf"],
        "pages": rendered["pages"],
        "generation": generation.name,
        "warnings": warnings,
        "event_count": len(events),
        "elapsed_seconds": round(time.monotonic() - progress.started, 1),
    }
    save_json(generation / "analysis.json", result)
    with zipfile.ZipFile(generation / "score-bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(generation.iterdir()):
            if file.suffix in (".musicxml", ".svg", ".pdf", ".mid", ".json"):
                archive.write(file, file.name)
    save_json(directory / "result.json", result)
    progress("complete", 1, "악보 생성 완료")
    return result


def run(directory: Path):
    progress = Progress(directory)
    request = json.loads((directory / "request.json").read_text())
    if request.get("revision"):
        analysis = json.loads((directory / "analysis.json").read_text())
        if request.get("reestimate_tempo"):
            progress("rhythm", 0.75, "캐시된 드럼에서 BPM과 비트 위치를 다시 계산합니다.")
            analysis.update(estimate_tempo(directory / "drums.wav"))
            save_json(directory / "analysis.json", analysis)
        events = request.get("events")
        if events is None:
            events = json.loads((directory / "events.json").read_text())
        # Edits are persisted only once their generation has rendered successfully.
        make_score(directory, request, analysis, events, progress)
        save_json(directory / "events.json", events)
        return
    options = Options.model_validate(request["options"])
    device = select_device(options.device)
    progress("converting", 0.05, "오디오를 44.1 kHz PCM WAV로 변환합니다.")
    duration = convert_media(directory / "input", directory / "audio.wav")
    warnings = []
    separation_device = None
    if options.source == "mix":
        drums, separation_device, warnings = separate(
            directory, device, progress, options.device == "auto"
        )
    else:
        drums = directory / "drums.wav"
        shutil.copyfile(directory / "audio.wav", drums)
    events, transcription_device, model_warnings = detect(
        drums, directory, device, options.sensitivity, progress, options.device == "auto"
    )
    events = [e for e in events if e["time"] < duration]
    progress("rhythm", 0.75, "BPM과 비트 위치를 분석합니다.")
    analysis = {
        **estimate_tempo(drums),
        "duration": round(duration, 4),
        "separation_device": separation_device,
        "transcription_device": transcription_device,
        "warnings": warnings + model_warnings,
        "inference_elapsed_seconds": round(time.monotonic() - progress.started, 1),
    }
    save_json(directory / "analysis.json", analysis)
    save_json(directory / "events.json", events)
    make_score(directory, request, analysis, events, progress)
