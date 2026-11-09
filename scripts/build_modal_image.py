"""Build and publish the processing runtime separately from app deployments."""

import argparse
import json
import time
from pathlib import Path

import modal

from transcription.modal_config import (
    APP_NAME,
    CACHE_ENV,
    ENVIRONMENT,
    GPU,
    IMAGE_NAME,
    MODEL_STORAGE,
)

ROOT = Path(__file__).resolve().parents[1]


def processing_image():
    if MODEL_STORAGE not in {"volume", "image"}:
        raise ValueError("BEATIT_MODAL_MODEL_STORAGE must be volume or image")
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("ffmpeg", "git", "build-essential", "pkg-config", "libopus-dev", "libgomp1")
        .workdir("/opt/beatit")
        .uv_sync(
            str(ROOT), extras=["cuda"], frozen=True, extra_options="--no-dev", uv_version="0.9.26"
        )
        # Modal's uv launcher runs before Volume mounts. Keep its cache out of /models,
        # otherwise XDG_CACHE_HOME makes the mount point non-empty before it can mount.
        .env({"UV_CACHE_DIR": "/tmp/uv-cache"})
        .env(
            {
                **CACHE_ENV,
                "BEATIT_MODAL_APP": APP_NAME,
                "BEATIT_MODAL_GPU": GPU,
                "BEATIT_MODAL_MODEL_STORAGE": MODEL_STORAGE,
                "PYTHONUNBUFFERED": "1",
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "NUMBA_NUM_THREADS": "2",
            }
        )
    )
    if MODEL_STORAGE == "image":
        # Only model-preparation code precedes this layer; UI/pipeline edits reuse weights.
        for name in (
            "__init__.py",
            "modal_config.py",
            "modal_models.py",
            "detection.py",
            "drumsep.py",
            "rhythm.py",
        ):
            image = image.add_local_file(
                ROOT / "src/transcription" / name,
                "/opt/model-source/transcription/" + name,
                copy=True,
            )
        image = image.env({"PYTHONPATH": "/opt/model-source"}).run_commands(
            "python -m transcription.modal_models"
        )

    return image.env({"PYTHONPATH": "/opt/beatit/src"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=IMAGE_NAME)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    app = modal.App.lookup(
        APP_NAME + "-image-builds", create_if_missing=True, environment_name=ENVIRONMENT
    )
    with modal.enable_output():
        image = processing_image().build(app)
    try:
        existing = modal.Image.from_name(args.name, environment_name=ENVIRONMENT)
        existing.build(app)  # Resolves a name; it has no build recipe to execute.
    except modal.exception.NotFoundError:
        image.publish(args.name, environment_name=ENVIRONMENT)
    else:
        if existing.object_id != image.object_id:
            raise RuntimeError(
                "This image tag already references another image. Publish a new version tag instead."
            )
    report = {
        "name": args.name,
        "image_id": image.object_id,
        "environment": ENVIRONMENT,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
