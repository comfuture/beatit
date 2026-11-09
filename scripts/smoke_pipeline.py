"""Real FFmpeg / Demucs / ADTOF integration probe (no synthetic model results)."""

import argparse
import os
import shutil
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from transcription.execution import run
from transcription.models import Options
from transcription.pipeline import save_json

parser = argparse.ArgumentParser()
parser.add_argument("--input", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--engine", choices=["local", "modal", "colab"], default="local")
parser.add_argument("--device", choices=["cpu", "mps", "cuda", "auto"], default=None)
parser.add_argument("--renderer", choices=["auto", "verovio", "musescore"], default="auto")
args = parser.parse_args()
os.environ["DRUM_EXECUTION_BACKEND"] = args.engine
directory = args.output_dir.resolve()
directory.mkdir(parents=True, exist_ok=True)
shutil.copyfile(args.input, directory / "input")
save_json(
    directory / "request.json",
    {
        "filename": args.input.name,
        "options": Options(
            device=args.device or ("cpu" if args.engine == "local" else "auto"),
            renderer=args.renderer,
        ).model_dump(),
    },
)
run(directory)
print(directory / "result.json")
