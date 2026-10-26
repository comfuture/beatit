"""Foreground operator connection: Google account credentials never leave this machine."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="chunsik", help="Existing SSH host alias")
    parser.add_argument("--port", type=int, default=18001)
    parser.add_argument(
        "--remote-token-file", default="/home/ubuntu/services/beatit-secrets/colab-api-token"
    )
    parser.add_argument("--work-dir", type=Path, default=Path("work/colab-operator"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", args.host):
        parser.error("Use an SSH hostname or configured alias")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid controller port")
    os.umask(0o077)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    work.chmod(0o700)
    lock = (work / "operator.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("An operator is already using this work directory")
    name = "beatit-chunsik"
    state = work / "sessions.json"
    cli = ["colab", "--config", str(state)]

    def command(*arguments, timeout=150):
        with (work / "cli.log").open("ab") as log:
            result = subprocess.run(
                [*cli, *arguments], stdout=log, stderr=subprocess.STDOUT, timeout=timeout
            )
        if result.returncode:
            raise RuntimeError(f"Colab {arguments[0]} failed; inspect the private operator log")

    def rpc(path, payload=None, method=None):
        code = f"""import json, pathlib, sys, urllib.request, urllib.error
token = pathlib.Path({args.remote_token_file!r}).read_text().strip()
payload = json.loads({json.dumps(payload)!r})
request = urllib.request.Request(
    {"http://127.0.0.1:" + str(args.port) + path!r},
    data=json.dumps(payload).encode() if payload is not None else None,
    method={method!r},
    headers={{'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}},
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print(json.dumps(json.load(response)))
except urllib.error.HTTPError as error:
    try:
        detail = json.loads(error.read()).get('detail')
    except ValueError:
        detail = None
    print(json.dumps({{'rpc_error': error.code, 'detail': detail if isinstance(detail, str) else None}}))
    sys.exit(1)
"""
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", args.host, "sudo", "-n", "python3", "-"],
            input=code,
            capture_output=True,
            text=True,
            timeout=25,
        )
        if result.returncode:
            try:
                detail = json.loads(result.stdout).get("detail")
                if isinstance(detail, str):
                    raise RuntimeError(detail)
            except (ValueError, TypeError):
                pass
            raise RuntimeError("Cannot reach the private Colab controller over SSH")
        return json.loads(result.stdout)

    connected = allocated = released = False
    try:
        rpc("/v1/status")
        command("sessions", timeout=20)
        sessions = json.loads(state.read_text()) if state.exists() else {}
        if name not in sessions:
            print("Colab T4 런타임을 요청합니다. 연결 종료 시 해제합니다.", flush=True)
            allocated = True
            command("new", "-s", name, "--gpu", "T4")
        allocated = True
        last_status = None
        while True:
            sessions = json.loads(state.read_text())
            expires = datetime.fromisoformat(
                sessions[name]["token_expires_at"].replace("Z", "+00:00")
            )
            if (expires - datetime.now(timezone.utc)).total_seconds() < 600:
                command("sessions", timeout=20)
                sessions = json.loads(state.read_text())
                if name not in sessions:
                    raise RuntimeError("Colab reclaimed the runtime; reconnect explicitly")
            status = rpc(
                "/v1/attachment", {"sessions": {name: sessions[name]}, "lease_seconds": 90}
            )
            connected = True
            summary = (status["state"], status["prepared"], status["active"])
            if summary != last_status:
                print(
                    json.dumps(
                        {
                            "state": status["state"],
                            "prepared": status["prepared"],
                            "active": status["active"],
                            "accelerator": status["accelerator"],
                        }
                    ),
                    flush=True,
                )
                last_status = summary
            if status["release_requested"]:
                rpc("/v1/attachment", method="DELETE")
                print("유휴 시간이 지나 런타임을 해제합니다.", flush=True)
                break
            time.sleep(20)
    except KeyboardInterrupt:
        print("Colab 연결을 종료하고 런타임을 해제합니다.", flush=True)
    finally:
        if connected:
            try:
                rpc("/v1/attachment?force=true", method="DELETE")
            except Exception:
                pass
        if allocated:
            try:
                current = json.loads(state.read_text()) if state.exists() else {}
                if name in current:
                    command("stop", "-s", name)
                    released = True
                else:
                    print(
                        "Colab 할당 응답을 확인하지 못했습니다. 로컬 colab sessions에서 "
                        "미확인 할당을 확인하고 해제하세요.",
                        file=sys.stderr,
                    )
            finally:
                if released:
                    try:
                        rpc("/v1/attachment?force=true&released=true", method="DELETE")
                    except Exception:
                        pass
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
