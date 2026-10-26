"""Remote Colab helpers. Lifecycle functions use only the notebook's standard library."""

import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

BASE = Path("/content/beatit")


def prepare(fingerprint: str):
    BASE.mkdir(exist_ok=True)
    source = BASE / "source"
    source.mkdir(exist_ok=True)
    with tarfile.open(BASE / "source.tar") as archive:
        # Only our versioned source archive is accepted; never expand links/devices.
        for entry in archive.getmembers():
            if not (entry.isfile() or entry.isdir()) or entry.name.startswith("/"):
                raise ValueError("Invalid source archive")
            if ".." in Path(entry.name).parts:
                raise ValueError("Invalid source archive path")
        archive.extractall(source, filter="data")
    commands = [
        ["uv", "sync", "--frozen", "--extra", "cuda", "--no-dev", "--python", "3.12"],
        [str(source / ".venv/bin/drum-score"), "download-model"],
        [str(source / ".venv/bin/python"), "-c", "import torch; assert torch.cuda.is_available()"],
    ]
    for command in commands:
        process = subprocess.Popen(
            command, cwd=source, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in process.stdout:
            print(line, end="", flush=True)
        if process.wait():
            raise RuntimeError("Colab environment preparation failed")
    from transcription.colab_artifacts import save_json

    save_json(BASE / "prepared.json", {"fingerprint": fingerprint})


def launch(job: str, attempt: str):
    from transcription.colab_artifacts import valid_identifier

    if not valid_identifier(job) or not valid_identifier(attempt):
        raise ValueError("Invalid job identifier")
    directory = BASE / "jobs" / job / attempt
    directory.mkdir(parents=True, exist_ok=True)
    metadata = directory / "process.json"
    if metadata.exists():
        raise RuntimeError("Attempt already launched")
    log = (directory / "worker.log").open("ab")
    process = subprocess.Popen(
        [
            str(BASE / "source/.venv/bin/python"),
            "-u",
            "-m",
            "transcription.colab_payload",
            "run",
            str(directory),
        ],
        cwd=BASE / "source",
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "OMP_NUM_THREADS": "2", "DRUM_EXECUTION_BACKEND": "local"},
    )
    log.close()
    metadata.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "start_ticks": Path(f"/proc/{process.pid}/stat").read_text().split()[21],
            }
        )
    )
    print("BEATIT_LAUNCHED", flush=True)


def cancel(job: str, attempt: str):
    from transcription.colab_artifacts import save_json, valid_identifier

    if not valid_identifier(job) or not valid_identifier(attempt):
        raise ValueError("Invalid job identifier")
    directory = BASE / "jobs" / job / attempt
    metadata = directory / "process.json"
    if not metadata.exists():
        return
    info = json.loads(metadata.read_text())
    pid = int(info["pid"])
    try:
        same_process = Path(f"/proc/{pid}/stat").read_text().split()[21] == info["start_ticks"]
    except FileNotFoundError:
        same_process = False
    if same_process:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.killpg(pid, signal.SIGKILL)
    save_json(directory / "status.json", {"state": "cancelled"})


def run(directory: Path):
    from transcription.colab_artifacts import pack_output, save_json, unpack_input

    save_json(directory / "status.json", {"state": "running"})
    try:
        unpack_input(directory / "input.zip", directory)
        request = json.loads((directory / "request.json").read_text())
        request["options"]["device"] = "cuda"
        request["options"]["renderer"] = "verovio"
        save_json(directory / "request.json", request)
        from transcription.pipeline import run as pipeline_run

        pipeline_run(directory)
        pack_output(directory, directory / "result.zip")
        save_json(directory / "status.json", {"state": "completed"})
    except Exception:
        import traceback

        traceback.print_exc()
        save_json(directory / "status.json", {"state": "failed"})
        raise


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "run":
    run(Path(sys.argv[2]))
