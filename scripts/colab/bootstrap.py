import json
import shutil
import subprocess
import time
from pathlib import Path

base = Path("/content/beatit-probe")
base.mkdir(exist_ok=True)
(base / "source").mkdir(exist_ok=True)
metrics = {}
started = time.perf_counter()


def timed(name, command, cwd=None):
    t = time.perf_counter()
    print(f"START {name}", flush=True)
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    for line in process.stdout:
        print(line, end="", flush=True)
    if process.wait():
        raise RuntimeError(f"{name} exited {process.returncode}")
    metrics[name] = round(time.perf_counter() - t, 3)
    print(json.dumps({"step": name, "seconds": metrics[name]}), flush=True)


timed("source_unpack", ["tar", "-xf", "/content/beatit-source.tar", "-C", str(base / "source")])
uv = shutil.which("uv")
if not uv:
    timed("uv_install", ["python", "-m", "pip", "install", "uv==0.9.26"])
    uv = shutil.which("uv")
timed(
    "dependency_install",
    [uv, "sync", "--frozen", "--extra", "cuda", "--no-dev", "--python", "3.12"],
    cwd=base / "source",
)
timed(
    "model_download",
    [str(base / "source/.venv/bin/drum-score"), "download-model"],
    cwd=base / "source",
)
timed("doctor", [str(base / "source/.venv/bin/drum-score"), "doctor"], cwd=base / "source")
metrics["bootstrap_total"] = round(time.perf_counter() - started, 3)
(base / "bootstrap-metrics.json").write_text(json.dumps(metrics, indent=2))
print("BOOTSTRAP_METRICS " + json.dumps(metrics), flush=True)
