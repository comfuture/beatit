"""Application-side RPC client; no Google credentials are available here."""

import json
import os
import signal
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def controller_request(path: str, payload=None, *, method=None, timeout=10):
    base = os.environ.get("COLAB_CONTROLLER_URL", "").rstrip("/")
    secret = os.environ.get("COLAB_CONTROLLER_TOKEN_FILE", "")
    if not base or not secret:
        raise RuntimeError("Colab 컨트롤러 연결 설정이 없습니다.")
    token = Path(secret).read_text().strip()
    if len(token) < 32:
        raise RuntimeError("Colab 컨트롤러 인증 설정이 잘못되었습니다.")
    request = Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            detail = json.loads(error.read()).get("detail")
        except (ValueError, OSError):
            detail = None
        raise RuntimeError(
            detail if isinstance(detail, str) else "Colab 컨트롤러 요청에 실패했습니다."
        ) from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Colab 컨트롤러에 연결할 수 없습니다.") from None


def execution_capabilities(local: dict):
    backend = os.environ.get("DRUM_EXECUTION_BACKEND", "local")
    result = {**local, "execution_backend": backend, "execution_label": "로컬 엔진"}
    if backend == "colab":
        try:
            remote = controller_request("/v1/status", timeout=2)
        except RuntimeError:
            remote = {
                "state": "unavailable",
                "accelerator": "T4",
                "prepared": False,
                "idle_timeout_seconds": 600,
            }
        result.update(
            execution_label="Google Colab GPU",
            devices=["cuda"],
            default_device="cuda",
            musescore=False,
            remote_runtime=remote,
        )
    return result


def run(directory: Path):
    identifier = directory.name
    attempt = uuid4().hex
    route = "/v1/jobs/" + identifier
    finished = False

    def cancel(signum=None, frame=None):
        try:
            controller_request(route + "/" + attempt + "/cancel", {}, timeout=5)
        except RuntimeError:
            pass
        if signum is not None:
            raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        controller_request(route, {"attempt": attempt})
        deadline = time.monotonic() + 4800
        while time.monotonic() < deadline:
            status = controller_request(route + "/" + attempt)
            if status["status"] == "completed":
                if not (directory / "result.json").is_file():
                    raise RuntimeError("Colab 결과를 찾을 수 없습니다.")
                finished = True
                return
            if status["status"] in {"failed", "cancelled"}:
                finished = True
                raise RuntimeError(status.get("error") or "Colab 분석이 중단되었습니다.")
            time.sleep(1)
        raise TimeoutError("Colab 작업 대기 시간이 초과되었습니다.")
    finally:
        if not finished:
            cancel()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
