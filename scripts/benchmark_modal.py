"""Qualify the deployed Modal engine using repeated, real transcription jobs."""

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import soundfile as sf

from transcription.colab_artifacts import MEDIA
from transcription.modal_worker import run
from transcription.models import Options
from transcription.pipeline import save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    if not args.input.is_file() or args.runs < 1:
        parser.error("Provide an existing input and a positive number of runs")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with args.input.open("rb") as source:
        input_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    report = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "input_bytes": args.input.stat().st_size,
        "input_sha256": input_sha256,
        "runs": [],
        "rates_usd_per_second": {"T4": 0.000164, "cpu_core": 0.0000131, "memory_gib": 0.00000222},
        "pricing_source": "https://modal.com/pricing",
        "cost_method": "Resource-rate estimates; excludes image build, storage, egress and unmeasured container startup. Not an invoice.",
    }
    for index in range(args.runs):
        directory = args.work_dir / uuid4().hex
        directory.mkdir()
        shutil.copyfile(args.input, directory / "input")
        save_json(
            directory / "request.json",
            {
                "filename": "modal-qualification.webm",
                "options": Options(device="auto", renderer="verovio").model_dump(),
            },
        )
        started = time.perf_counter()
        print(f"START run {index + 1}: {directory.name}", flush=True)
        try:
            run(directory)
        except BaseException as error:
            report["runs"].append(
                {
                    "index": index + 1,
                    "directory": directory.name,
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "error": str(error),
                }
            )
            save_json(args.work_dir / "report.json", report)
            raise
        wall_seconds = round(time.perf_counter() - started, 3)
        result = json.loads((directory / "result.json").read_text())
        runtime = result["execution_runtime"]
        if (
            result["execution_backend"] != "modal"
            or result["transcription_device"] != "cuda"
            or result["separation_device"] != "cuda"
        ):
            raise RuntimeError("The qualification did not execute the full pipeline on Modal CUDA")
        generation = directory / result["generation"]
        ElementTree.parse(generation / "score.musicxml")
        for page in result["pages"]:
            ElementTree.parse(generation / page)
        media = {
            name: {
                "seconds": sf.info(directory / name).duration,
                "frames": sf.info(directory / name).frames,
            }
            for name in sorted(MEDIA)
        }
        if any(value["frames"] == 0 for value in media.values()):
            raise RuntimeError("Empty audio artifact")
        for name in ("score.mid", "performance.mid"):
            if (generation / name).read_bytes()[:4] != b"MThd":
                raise RuntimeError("Invalid MIDI artifact")
        cpu_rate = 2 * 0.0000131 + 8 * 0.00000222
        gpu_rate = cpu_rate + 0.000164
        cpu_seconds = (runtime.get("model_preparation") or {}).get("elapsed_seconds", 0)
        entry = {
            "index": index + 1,
            "directory": directory.name,
            "wall_seconds": wall_seconds,
            "duration_seconds": result["duration"],
            "events": result["event_count"],
            "pages": len(result["pages"]),
            "runtime": runtime,
            "media": media,
            "estimated_cpu_preparation_usd": round((cpu_seconds + 2) * cpu_rate, 6),
            "estimated_gpu_usd": round((runtime["elapsed_seconds"] + 2) * gpu_rate, 6),
        }
        entry["estimated_total_usd"] = round(
            entry["estimated_cpu_preparation_usd"] + entry["estimated_gpu_usd"], 6
        )
        report["runs"].append(entry)
        save_json(args.work_dir / "report.json", report)
        print(
            json.dumps(
                {
                    key: entry[key]
                    for key in ("index", "wall_seconds", "events", "pages", "estimated_total_usd")
                }
            ),
            flush=True,
        )
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(args.work_dir / "report.json", report)


if __name__ == "__main__":
    main()
