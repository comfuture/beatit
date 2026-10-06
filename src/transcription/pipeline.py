import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from .engrave import render
from .models import Options
from .rhythm import align_downbeat, estimate_tempo
from .runtime import select_device
from .score import KIT, quantize, write_musicxml

STEMS = ("kick", "snare", "toms", "hh", "cymbals")

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


def transcribe(
    drums: Path,
    directory: Path,
    device: str,
    sensitivity: float,
    progress: Progress,
    allow_fallback: bool,
):
    """DrumSep kit stems + ADTOF on kit and stems -> onset events with articulation/velocity."""
    import numpy as np
    import soundfile as sf
    import torch

    from . import detection, drumsep

    audio, rate = sf.read(drums, dtype="float32", always_2d=True)
    if rate != drumsep.SAMPLE_RATE:
        raise RuntimeError(f"드럼 오디오는 {drumsep.SAMPLE_RATE} Hz여야 합니다.")
    audio = audio.T
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    torch.manual_seed(0)
    labels = {
        "kit": "전체 드럼",
        "kick": "킥",
        "snare": "스네어",
        "toms": "탐",
        "hh": "하이햇",
        "cymbals": "심벌",
    }

    def infer(selected):
        progress("separating", 0.36, f"DrumSep으로 악기별 드럼을 분리합니다 · {selected.upper()}")
        # Float WAVs retain the original separator precision for ADTOF. Temporary
        # files are cleaned on both success and failure, including a CPU fallback.
        with tempfile.TemporaryDirectory(prefix="drumsep-", dir=directory) as scratch:
            separator = drumsep.load_model(selected)
            stems = drumsep.separate(
                audio,
                separator,
                selected,
                Path(scratch),
                lambda done: progress(
                    "separating", 0.36 + 0.16 * done, f"악기별 드럼 분리 · {done * 100:.0f}%"
                ),
            )
            del separator
            model = detection.load_adtof()
            names = ["kit", *drumsep.STEMS]
            activations, loudness = {}, {}
            for index, name in enumerate(names):
                source = audio if name == "kit" else sf.read(stems[name], dtype="float32")[0].T
                activations[name] = detection.activations(
                    model,
                    detection.spectrogram(source),
                    selected,
                    lambda done, index=index, name=name: progress(
                        "transcribing",
                        0.53 + 0.2 * (index + done) / len(names),
                        f"드럼 타격 분석 · {labels[name]} · {selected.upper()}",
                    ),
                )
                if name != "kit":
                    loudness[name] = detection.loudness_db(source)
                    with sf.SoundFile(
                        directory / f"stem-{name}.flac", "w", rate, 2, subtype="PCM_16"
                    ) as output:
                        for start in range(0, source.shape[-1], drumsep.CONFIG.chunk_size):
                            block = source[:, start : start + drumsep.CONFIG.chunk_size]
                            output.write(np.clip(block, -1, 1).T)
                    del block  # Its view would otherwise retain this stem during the next read.
                    stems[name].unlink()
                del source
        return activations, loudness

    warnings = []
    fallback = False
    try:
        activations, loudness = infer(device)
    except RuntimeError:
        if device == "cpu" or not allow_fallback:
            raise
        warnings.append(f"DrumSep/ADTOF {device.upper()} 실행 실패로 CPU에서 다시 실행했습니다.")
        device = "cpu"
        fallback = True
    if fallback:
        # Leave the exception handler first so its traceback releases failed models.
        activations, loudness = infer(device)
    events, combined = detection.detect_events(
        activations["kit"],
        {name: activations[name] for name in drumsep.STEMS},
        loudness,
        sensitivity,
    )
    np.save(directory / "activations.npy", combined)
    return events, device, warnings


def write_midi(events: list[dict], target: Path, bpm: float, meter: str = "4/4"):
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    numerator, denominator = map(int, meter.split("/"))
    midi.time_signature_changes.append(pretty_midi.TimeSignature(numerator, denominator, 0))
    drums = pretty_midi.Instrument(program=0, is_drum=True, name="Drum set")
    for event in events:
        onset = max(0.0, event.get("quantized_time", event["time"]))
        velocity = event.get("velocity") or round(event["strength"] * 110 + 17)
        drums.notes.append(
            pretty_midi.Note(
                pitch=event["pitch"],
                velocity=max(1, min(127, int(velocity))),
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
    bpm = options.bpm or analysis["estimated_bpm"]
    aligned = False
    if options.offset is not None:
        offset = options.offset
    else:
        # Bar 1 starts on the tracked downbeat; earlier hits become a pickup bar.
        offset, aligned = align_downbeat(
            analysis["estimated_offset"],
            bpm,
            analysis.get("downbeat_times", []),
            options.meter,
            min((event["time"] for event in events), default=None),
        )
    resolved = options.model_copy(update={"bpm": bpm, "offset": round(offset, 4)})
    progress("engraving", 0.8, "타격을 리듬 격자에 맞추고 MusicXML을 생성합니다.")
    grid = quantize(events, analysis["duration"], resolved)
    # Publish a complete generation atomically; never expose half-written revisions.
    generation = directory / ("render-" + str(time.time_ns()))
    generation.mkdir()
    write_musicxml(grid, Path(request["filename"]).stem, generation / "score.musicxml")
    write_midi(grid["events"], generation / "score.mid", grid["bpm"], grid["meter"])
    write_midi(events, generation / "performance.mid", grid["bpm"], grid["meter"])
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
    if aligned:
        warnings.append(
            "첫 마디 위치는 Beat This! 다운비트에 맞췄습니다. 박자는 선택한 값을 사용하며 템포 변화·스윙은 고정 격자에 근사합니다."
        )
    elif options.offset is None:
        warnings.append(
            "첫 마디 위치를 자동으로 맞추지 못했습니다. 템포 변화·스윙은 고정 격자에 근사하므로 원음과 대조하세요."
        )
    numerator, denominator = map(int, options.meter.split("/"))
    estimated_beats = analysis.get("downbeat_beats_per_bar")
    if denominator == 4 and estimated_beats in (3, 4) and estimated_beats != numerator:
        warnings.append(
            f"다운비트 간격이 {estimated_beats}박으로 추정됩니다. 박자 설정을 확인하세요."
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
        "engraver_exit_code": rendered.get("exit_code", 0),
        "pages": rendered["pages"],
        "downbeat_aligned": aligned,
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
            progress("rhythm", 0.75, "캐시된 오디오에서 BPM과 비트 위치를 다시 계산합니다.")
            options = Options.model_validate(request["options"])
            drums = directory / "drums.wav"
            cached = json.loads((directory / "events.json").read_text())
            tempo, tempo_warnings = estimate_tempo(
                directory / "audio.wav" if options.source == "mix" else drums,
                drums,
                [event["time"] for event in cached],
                select_device(options.device),
                options.device == "auto",
            )
            analysis.update(tempo)
            analysis["warnings"] = list(analysis.get("warnings", [])) + tempo_warnings
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
    events, transcription_device, model_warnings = transcribe(
        drums, directory, device, options.sensitivity, progress, options.device == "auto"
    )
    events = [e for e in events if e["time"] < duration]
    progress("rhythm", 0.75, "Beat This!로 비트와 다운비트를 분석합니다.")
    tempo, tempo_warnings = estimate_tempo(
        directory / "audio.wav" if options.source == "mix" else drums,
        drums,
        [event["time"] for event in events],
        device,
        options.device == "auto",
    )
    analysis = {
        **tempo,
        "duration": round(duration, 4),
        "separation_device": separation_device,
        "transcription_device": transcription_device,
        "stems": [f"stem-{name}.flac" for name in STEMS],
        "warnings": warnings + model_warnings + tempo_warnings,
        "inference_elapsed_seconds": round(time.monotonic() - progress.started, 1),
    }
    save_json(directory / "analysis.json", analysis)
    save_json(directory / "events.json", events)
    make_score(directory, request, analysis, events, progress)
