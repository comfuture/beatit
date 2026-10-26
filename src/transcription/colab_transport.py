"""Official CLI execution plus bounded Jupyter file transfers, using runtime tokens only."""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse


class ColabUnavailable(RuntimeError):
    pass


def validate_session(sessions: dict, expected_name: str) -> dict:
    if set(sessions) != {expected_name}:
        raise ValueError("Exactly one named runtime must be attached")
    session = sessions[expected_name]
    allowed = {
        "name",
        "endpoint",
        "token",
        "url",
        "token_expires_at",
        "kernel_id",
        "variant",
        "accelerator",
        "machine_shape",
        "last_execution",
        "session_id",
        "running",
    }
    if not isinstance(session, dict) or set(session) - allowed:
        raise ValueError("Unexpected runtime state fields")
    for field in ("name", "endpoint", "token", "url", "token_expires_at", "variant", "accelerator"):
        if not isinstance(session.get(field), str) or not session[field]:
            raise ValueError("Invalid runtime state field")
    url = urlparse(session.get("url", ""))
    hostname = url.hostname or ""
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.query
        or url.fragment
        or not (
            hostname == "colab.research.google.com"
            or hostname.endswith(".prod.colab.dev")
            or hostname.endswith(".prod.r.colab.dev")
        )
    ):
        raise ValueError("Runtime must use an HTTPS Google Colab endpoint")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session["endpoint"]):
        raise ValueError("Invalid runtime endpoint")
    if (
        hostname == "colab.research.google.com"
        and url.path.rstrip("/") != "/tun/m/" + session["endpoint"]
    ):
        raise ValueError("Runtime URL does not match the attached endpoint")
    if hostname.endswith(".colab.dev") and (
        not re.match(r"^8080-" + re.escape(session["endpoint"]) + r"(?:-[a-z])?\.", hostname)
        or url.path not in ("", "/")
    ):
        raise ValueError("Runtime URL does not match the attached endpoint")
    if session.get("name") != expected_name or session.get("variant") != "GPU":
        raise ValueError("A named GPU runtime is required")
    if not session.get("endpoint") or not session.get("token"):
        raise ValueError("Incomplete runtime attachment")
    try:
        expires = datetime.fromisoformat(session["token_expires_at"].replace("Z", "+00:00"))
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    except (KeyError, ValueError, TypeError):
        raise ValueError("Runtime token expiry is required") from None
    if remaining < 300:
        raise ValueError("Refresh the runtime token on the operator machine first")
    return session


class ColabTransport:
    def __init__(self, root: Path, session_name: str):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_name = session_name
        self.seed = None
        self.config = self.root / "runtime.json"

    def attach(self, sessions: dict):
        self.seed = dict(validate_session(sessions, self.session_name))

    def _sync(self):
        if self.seed is None:
            raise ColabUnavailable("Colab 런타임이 연결되지 않았습니다.")
        session = dict(self.seed)
        if self.config.exists():
            previous = json.loads(self.config.read_text()).get(self.session_name, {})
            if previous.get("endpoint") == session.get("endpoint"):
                for key in ("kernel_id", "last_execution", "session_id", "running"):
                    if key in previous:
                        session[key] = previous[key]
        temporary = self.config.with_suffix(".tmp")
        temporary.write_text(json.dumps({self.session_name: session}))
        temporary.chmod(0o600)
        temporary.replace(self.config)

    def execute(self, code: str, timeout: int = 600):
        self._sync()
        with tempfile.TemporaryDirectory(dir=self.root) as scratch:
            script = Path(scratch) / "execute.py"
            script.write_text(code)
            log = Path(scratch) / "cli.log"
            environment = {
                **os.environ,
                "HOME": str(self.root / "cli-home"),
                # The controller deliberately has no Google account credentials.
                "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent-colab-account-credentials",
            }
            try:
                with log.open("wb") as output:
                    completed = subprocess.run(
                        [
                            "colab",
                            "--auth",
                            "adc",
                            "--config",
                            str(self.config),
                            "exec",
                            "-s",
                            self.session_name,
                            "-f",
                            str(script),
                            "--timeout",
                            str(timeout),
                        ],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        timeout=timeout + 30,
                        env=environment,
                    )
            except (OSError, subprocess.TimeoutExpired):
                if log.exists():
                    shutil.copyfile(log, self.root / "last-cli-error.log")
                raise ColabUnavailable(
                    "Colab 명령을 완료하지 못했습니다. 런타임을 다시 연결하세요."
                ) from None
            if completed.returncode:
                shutil.copyfile(log, self.root / "last-cli-error.log")
                # CLI tracebacks can contain proxy tokens; never send them to user logs.
                raise ColabUnavailable("Colab 실행에 실패했습니다. 런타임 상태를 확인하세요.")

    def _request(self, method: str, path: str, *, files=False, **kwargs):
        import requests

        session = self.seed
        if session is None:
            raise ColabUnavailable("Colab 연결 대기")
        prefix = "files" if files else "api/contents"
        url = session["url"].rstrip("/") + "/" + prefix + "/" + quote(path.strip("/"), safe="/")
        params = {"authuser": "0", "colab-runtime-proxy-token": session["token"]}
        try:
            response = requests.request(
                method, url, params=params, timeout=(15, 120), allow_redirects=False, **kwargs
            )
            if response.is_redirect:
                response.close()
                raise ColabUnavailable("Colab 파일 전송 경로가 변경되었습니다. 다시 연결하세요.")
            if response.status_code == 404:
                response.close()
                raise FileNotFoundError(path)
            response.raise_for_status()
            return response
        except FileNotFoundError:
            raise
        except requests.RequestException:
            raise ColabUnavailable(
                "Colab 파일 전송이 중단되었습니다. 런타임을 다시 연결하세요."
            ) from None

    def read_json(self, path: str):
        with self._request("GET", path) as response:
            data = response.json()
        content = data.get("content", "")
        if data.get("format") == "base64":
            content = base64.b64decode(content).decode("utf-8")
        return json.loads(content)

    def upload(self, local: Path, remote: str):
        if local.stat().st_size > 128 * 1024**2:
            raise ValueError("Colab 전송 파일은 128 MiB 이하여야 합니다.")
        payload = {
            "name": remote.rsplit("/", 1)[-1],
            "path": remote,
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(local.read_bytes()).decode("ascii"),
            "chunk": 1,
        }
        with self._request("PUT", remote, json=payload):
            pass

    def download(self, remote: str, local: Path, limit=4 * 1024**3):
        # Binary /files avoids materializing large WAV/stem archives as base64 JSON.
        count = 0
        with (
            self._request("GET", remote, files=True, stream=True) as response,
            local.open("wb") as output,
        ):
            for block in response.iter_content(1024**2):
                count += len(block)
                if count > limit:
                    raise ValueError("Colab 결과 파일이 크기 제한을 초과했습니다.")
                output.write(block)
