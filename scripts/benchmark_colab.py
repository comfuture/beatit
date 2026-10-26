"""Measure a fresh Colab GPU job, optionally using a token-only Docker runner.

The local CLI owns allocation/cleanup. Docker receives only the newly allocated
runtime's state, never the user's OAuth token or gcloud configuration directory.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--gpu", choices=["T4", "L4", "A100", "H100", "G4"], default="T4")
    parser.add_argument("--docker-image", help="Use an already-built CLI image for job operations")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error("Input file does not exist")
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    source_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    archive = work / "source.tar"
    with archive.open("wb") as output:
        subprocess.run(
            [
                "git",
                "archive",
                "HEAD",
                "src",
                "pyproject.toml",
                "uv.lock",
                "README.md",
                ".python-version",
            ],
            cwd=root,
            stdout=output,
            check=True,
        )
    shutil.copyfile(args.input, work / "input.media")
    shutil.copyfile(root / "scripts/colab/bootstrap.py", work / "bootstrap.py")
    shutil.copyfile(root / "scripts/colab/run_case.py", work / "run_case.py")
    execute = """import subprocess
command = ['/content/beatit-probe/source/.venv/bin/python', '-u',
           '/content/run_case.py', '/content/input.media', '/content/beatit-probe/case']
process = subprocess.Popen(command, cwd='/content/beatit-probe/source',
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for line in process.stdout:
    print(line, end='', flush=True)
if process.wait():
    raise RuntimeError(f'Pipeline exited {process.returncode}')
"""
    (work / "execute.py").write_text(execute)
    name = "beatit-cold-" + uuid.uuid4().hex[:8]
    controller = ["colab", "--config", str(work / "controller.json")]
    timings = {}
    report = {
        "session": name,
        "gpu_requested": args.gpu,
        "source_revision": source_revision,
        "docker_image": args.docker_image,
        "google_account_credentials_in_container": False,
        "phase_seconds": timings,
    }

    def command(phase, argv):
        print("START " + phase, flush=True)
        started = time.perf_counter()
        with (work / (phase + ".log")).open("w") as log:
            completed = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
        timings[phase] = round(time.perf_counter() - started, 3)
        (work / "report.json").write_text(json.dumps(report, indent=2))
        if completed.returncode:
            raise RuntimeError(f"{phase} failed; inspect {work / (phase + '.log')}")
        print(json.dumps({"phase": phase, "seconds": timings[phase]}), flush=True)

    def job(*argv):
        if args.docker_image:
            return [
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m",
                "--mount",
                f"type=bind,source={work},target=/probe",
                args.docker_image,
                "--auth",
                "adc",
                "--config",
                "/probe/runtime.json",
                *argv,
            ]
        return ["colab", "--config", str(work / "runtime.json"), *argv]

    def local(path):
        return "/probe/" + path if args.docker_image else str(work / path)

    started = time.perf_counter()
    try:
        command("allocate", [*controller, "new", "-s", name, "--gpu", args.gpu])
        shutil.copyfile(work / "controller.json", work / "runtime.json")
        command(
            "upload_source",
            job("upload", "-s", name, local("source.tar"), "/content/beatit-source.tar"),
        )
        command(
            "upload_input", job("upload", "-s", name, local("input.media"), "/content/input.media")
        )
        command(
            "bootstrap", job("exec", "-s", name, "-f", local("bootstrap.py"), "--timeout", "1800")
        )
        command(
            "upload_runner", job("upload", "-s", name, local("run_case.py"), "/content/run_case.py")
        )
        command("pipeline", job("exec", "-s", name, "-f", local("execute.py"), "--timeout", "1800"))
        command(
            "download",
            job("download", "-s", name, "/content/beatit-result.zip", local("results.zip")),
        )
        report["end_to_end_seconds"] = round(time.perf_counter() - started, 3)
        with zipfile.ZipFile(work / "results.zip") as results:
            if results.testzip():
                raise RuntimeError("Corrupt result ZIP")
            for path in results.namelist():
                if path.endswith((".musicxml", ".svg")):
                    ElementTree.fromstring(results.read(path))
                if path.endswith(".mid") and not results.read(path).startswith(b"MThd"):
                    raise RuntimeError("Invalid MIDI header")
            report["pipeline"] = json.loads(results.read("benchmark-metrics.json"))
            report["bootstrap"] = json.loads(results.read("bootstrap-metrics.json"))
        report["artifacts_verified"] = True
    finally:
        try:
            if (work / "controller.json").exists():
                command("release", [*controller, "stop", "-s", name])
                report["session_released"] = True
                for filename in ("controller.json", "runtime.json"):
                    (work / filename).unlink(missing_ok=True)
                report["runtime_credentials_removed"] = True
        finally:
            report["total_including_release_seconds"] = round(time.perf_counter() - started, 3)
            (work / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
