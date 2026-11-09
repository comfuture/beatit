"""Deploy once: uv run modal deploy scripts/modal_app.py.

The dependency image is referenced by a published name, independently of source changes. Weights are prepared
on CPU into a persistent Volume by default; BEATIT_MODAL_MODEL_STORAGE=image bakes
them into a cached build layer instead. Neither path downloads models on a GPU.
"""

import os
from pathlib import Path

import modal

from transcription.modal_config import (
    APP_NAME,
    GPU,
    IMAGE_NAME,
    JOB_DIR,
    JOB_VOLUME,
    MODEL_DIR,
    MODEL_STORAGE,
    MODEL_VOLUME,
    PROGRESS_DICT,
)

ROOT = Path(__file__).resolve().parents[1]
if MODEL_STORAGE not in {"volume", "image"}:
    raise ValueError("BEATIT_MODAL_MODEL_STORAGE must be volume or image")

app = modal.App(APP_NAME)
models = modal.Volume.from_name(MODEL_VOLUME, create_if_missing=True, version=2)
jobs = modal.Volume.from_name(JOB_VOLUME, create_if_missing=True, version=2)
progress = modal.Dict.from_name(PROGRESS_DICT, create_if_missing=True)

# The dependency/runtime image is built and published explicitly by build_modal_image.py.
# Deploying this app only looks up the pinned name and mounts current source.
image = modal.Image.from_name(IMAGE_NAME).add_local_dir(
    ROOT / "src/transcription",
    "/opt/beatit/src/transcription",
    ignore=["static", "__pycache__"],
)
model_mounts = {MODEL_DIR: models} if MODEL_STORAGE == "volume" else {}


@app.function(
    image=image,
    volumes=model_mounts,
    timeout=1800,
    max_containers=1,
    cpu=2,
    memory=8192,
    scaledown_window=2,
)
def prepare_models():
    import time

    from transcription.modal_models import model_manifest
    from transcription.modal_models import prepare_models as prepare

    started = time.monotonic()
    if MODEL_STORAGE == "volume":
        models.reload()
    reused = model_manifest() is not None
    manifest = prepare()
    if MODEL_STORAGE == "volume":
        models.commit()
    return {
        **manifest,
        "storage": MODEL_STORAGE,
        "preparation": {
            "cache_reused": reused,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "container_id": os.environ.get("MODAL_TASK_ID"),
        },
    }


@app.function(
    image=image,
    gpu=GPU,
    volumes={JOB_DIR: jobs, **model_mounts},
    timeout=7200,
    max_containers=1,
    min_containers=0,
    cpu=2,
    memory=8192,
    scaledown_window=2,
    retries=0,
)
def transcribe(job: str, attempt: str):
    import json
    import shutil
    import subprocess
    import sys
    import tempfile
    import time

    from transcription.colab_artifacts import pack_output, save_json, unpack_input, valid_identifier
    from transcription.modal_models import model_manifest

    if not valid_identifier(job) or not valid_identifier(attempt):
        raise ValueError("Invalid job or attempt identifier")
    if MODEL_STORAGE == "volume":
        models.reload()
    if not model_manifest():
        raise RuntimeError("Prepare the Modal model cache before allocating a GPU")
    key = job + "/" + attempt
    jobs.reload()
    shared = Path(JOB_DIR) / key
    started = time.monotonic()
    gpu_info = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="beatit-") as scratch:
        directory = Path(scratch)
        unpack_input(shared / "input.zip", directory)
        request = json.loads((directory / "request.json").read_text())
        request["options"].update(device="cuda", renderer="verovio")
        save_json(directory / "request.json", request)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "transcription.worker", str(directory)],
            env={**os.environ, "DRUM_EXECUTION_BACKEND": "local", "HF_HUB_OFFLINE": "1"},
        )
        try:
            while True:
                if (directory / "progress.json").is_file():
                    progress[key] = json.loads((directory / "progress.json").read_text())
                if process.poll() is not None:
                    break
                time.sleep(1)
            if process.returncode:
                error = directory / "error.json"
                message = (
                    json.loads(error.read_text())["message"]
                    if error.is_file()
                    else "Inference failed"
                )
                raise RuntimeError(message)
            pack_output(directory, directory / "result.zip")
            shutil.copyfile(directory / "result.zip", shared / "result.zip")
            jobs.commit()
            return {
                "gpu": GPU,
                "model_storage": MODEL_STORAGE,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "gpu_name": gpu_info,
                "container_id": os.environ.get("MODAL_TASK_ID"),
            }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


@app.local_entrypoint()
def main():
    """Populate/check the CPU model cache without starting a GPU."""
    print(prepare_models.remote())
