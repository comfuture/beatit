"""Run inside the pinned Colab virtual environment, never in the notebook's Python."""

import argparse
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")

import torch

from transcription import pipeline
from transcription.models import Options


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; refusing to benchmark a CPU fallback")
    args.output.mkdir()
    shutil.copyfile(args.input, args.output / "input")
    pipeline.save_json(
        args.output / "request.json",
        {
            "filename": args.input.name,
            "options": Options(device="cuda", renderer="verovio").model_dump(),
        },
    )
    started = time.perf_counter()
    timings = {}
    active = None
    phase_start = started
    original = pipeline.Progress.__call__

    def track(progress, stage, fraction, message):
        nonlocal active, phase_start
        phase = stage
        if stage == "separating":
            phase = "demucs" if message.startswith("Demucs") else "drumsep"
        if phase != active:
            now = time.perf_counter()
            if active is not None:
                timings[active] = round(timings.get(active, 0) + now - phase_start, 3)
            active, phase_start = phase, now
        original(progress, stage, fraction, message)

    pipeline.Progress.__call__ = track
    try:
        pipeline.run(args.output)
    finally:
        pipeline.Progress.__call__ = original
    torch.cuda.synchronize()
    result = json.loads((args.output / "result.json").read_text())
    metrics = {
        "duration_seconds": result["duration"],
        "wall_seconds": round(time.perf_counter() - started, 3),
        "stages": timings,
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
        "peak_vram_bytes": torch.cuda.max_memory_allocated(),
        "events": result["event_count"],
        "pages": len(result["pages"]),
        "separation_device": result["separation_device"],
        "transcription_device": result["transcription_device"],
    }
    if result["separation_device"] != "cuda" or result["transcription_device"] != "cuda":
        raise RuntimeError("Pipeline did not use CUDA for both separation and transcription")
    (args.output / "benchmark-metrics.json").write_text(json.dumps(metrics, indent=2))
    with zipfile.ZipFile("/content/beatit-result.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("benchmark-metrics.json", "result.json", "request.json"):
            archive.write(args.output / name, name)
        archive.write("/content/beatit-probe/bootstrap-metrics.json", "bootstrap-metrics.json")
        for path in (args.output / result["generation"]).iterdir():
            if path.is_file():
                archive.write(path, "artifacts/" + path.name)
    print("CASE_METRICS " + json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
