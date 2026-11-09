"""Actual subprocess lifetime tests; inference is stubbed before model imports."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

CHILD = r"""
import json, os, runpy, signal, subprocess, sys, time, types
from pathlib import Path
root = Path(sys.argv[1])
execution = types.ModuleType("transcription.execution")
def run(directory):
    def cancelled(signum, frame):
        (root / "cancelled").write_text(str(signum))
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, cancelled)
    # A model/helper process shares the inference group and inherits stdout.
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (root / "ready.json").write_text(json.dumps({"worker":os.getpid(), "helper":helper.pid}))
    while True:
        time.sleep(1)
execution.run = run
pipeline = types.ModuleType("transcription.pipeline")
pipeline.save_json = lambda path, value: path.write_text(json.dumps(value))
sys.modules[execution.__name__] = execution
sys.modules[pipeline.__name__] = pipeline
runpy.run_module("transcription.worker", run_name="__main__")
"""

PARENT = r"""
import os, subprocess, sys, time
variables = dict(os.environ)
if sys.argv[2] == "watched":
    variables["BEATIT_PARENT_PID"] = str(os.getpid())
else:
    variables.pop("BEATIT_PARENT_PID", None)
subprocess.Popen([sys.executable,"-c",sys.argv[3],sys.argv[1]],
                 env=variables, start_new_session=True)
while True:
    time.sleep(1)
"""


@pytest.fixture
def worker_parent(tmp_path):
    launched = []

    def launch(watched=True):
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                PARENT,
                str(tmp_path),
                "watched" if watched else "standalone",
                CHILD,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        launched.append(parent)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (tmp_path / "ready.json").is_file():
            if parent.poll() is not None:
                pytest.fail("Test parent exited before launching inference")
            time.sleep(0.02)
        ready = json.loads((tmp_path / "ready.json").read_text())
        return parent, ready

    yield launch
    for parent in launched:
        parent.kill() if parent.poll() is None else None
        ready = tmp_path / "ready.json"
        if ready.is_file():
            try:
                os.killpg(json.loads(ready.read_text())["worker"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        parent.communicate(timeout=5)


def test_parent_sigkill_stops_inference_group_and_runs_cancellation_handler(
    tmp_path, worker_parent
):
    parent, ready = worker_parent()
    parent.kill()
    # The worker and its model helper inherit these pipes. EOF proves neither
    # orphan retains execution after the API parent has died.
    stdout, stderr = parent.communicate(timeout=5)
    assert parent.returncode == -signal.SIGKILL
    assert (tmp_path / "cancelled").read_text() == str(signal.SIGTERM)
    assert not stdout and not stderr
    assert ready["worker"] != ready["helper"]


def test_watchdog_preserves_graceful_sigterm_cancellation(tmp_path, worker_parent):
    parent, ready = worker_parent()
    os.killpg(ready["worker"], signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (tmp_path / "cancelled").is_file():
        time.sleep(0.02)
    assert (tmp_path / "cancelled").read_text() == str(signal.SIGTERM)
    assert parent.poll() is None
    parent.kill()
    parent.communicate(timeout=5)


def test_standalone_worker_does_not_gain_a_parent_lifetime_policy(tmp_path, worker_parent):
    parent, ready = worker_parent(watched=False)
    parent.kill()
    # A standalone invocation intentionally survives its launching shell.
    time.sleep(0.5)
    assert not (tmp_path / "cancelled").exists()
    os.kill(ready["worker"], 0)
    os.killpg(ready["worker"], signal.SIGTERM)
    parent.communicate(timeout=5)
    assert (tmp_path / "cancelled").read_text() == str(signal.SIGTERM)
