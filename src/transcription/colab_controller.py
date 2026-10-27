"""A private, single-runtime controller. Google account authentication stays with the operator."""

import hashlib
import hmac
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .colab_artifacts import import_output, pack_input, save_json, valid_identifier
from .colab_transport import ColabTransport, ColabUnavailable, validate_session

REMOTE = "/content/beatit"


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: str = Field(pattern=r"^[0-9a-f]{32}$")


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessions: dict
    lease_seconds: int = Field(default=90, ge=30, le=120)


class ManagerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["idle", "allocating", "releasing", "error"] = "idle"
    error: Literal["authentication", "allocation", "connection", "cleanup"] | None = None
    lease_seconds: int = Field(default=90, ge=30, le=120)


MANAGER_ERRORS = {
    "authentication": "서버 Colab 인증을 다시 진행해야 합니다.",
    "allocation": "Colab 런타임을 할당하지 못했습니다. 계정 한도와 가용성을 확인하세요.",
    "connection": "서버 Colab 관리자 연결을 확인하세요.",
    "cleanup": "이전 Colab 런타임 정리가 필요합니다.",
}


class Controller:
    def __init__(self, data: Path, state: Path, source: Path, *, transport=None, idle_seconds=600):
        self.data = data.resolve()
        self.state = state.resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.source = source
        self.fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
        self.session_name = os.environ.get("COLAB_SESSION_NAME", "beatit-chunsik")
        self.transport = transport or ColabTransport(self.state / "transport", self.session_name)
        self.idle_seconds = idle_seconds
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.lease_until = 0.0
        self.managed = os.environ.get("COLAB_ALLOCATION_MODE", "operator") == "server"
        self.manager_until = 0.0
        self.manager_phase = "idle"
        self.manager_error = None
        self.last_activity = time.time()
        self.runtime_id = None
        self.accelerator = "T4"
        self.prepared_runtime = None
        self.confirmed_released = set()
        self.phase = "unavailable"
        self.active = None
        self.awaiting_runtime = False
        self.job_thread = None
        self.monitor = None
        self.database = self.state / "controller.sqlite3"
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS attempts (
                attempt TEXT PRIMARY KEY, job TEXT NOT NULL, request_hash TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, runtime TEXT, updated REAL NOT NULL
            )""")
            self.orphans = connection.execute(
                "SELECT job, attempt, runtime FROM attempts "
                "WHERE status IN ('running', 'orphaned') AND runtime IS NOT NULL"
            ).fetchall()
            connection.execute(
                "UPDATE attempts SET status='failed', error=?, updated=? WHERE status='running'",
                ("Colab 컨트롤러가 재시작되었습니다. 다시 시도하세요.", time.time()),
            )
        if self.orphans:
            self.runtime_id = self.orphans[0][2]

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def start(self):
        self.monitor = threading.Thread(target=self._watch, daemon=True, name="colab-lease")
        self.monitor.start()

    def stop(self):
        self.stopping.set()
        self.cancelled.set()
        if self.job_thread:
            self.job_thread.join(timeout=20)
        if self.monitor:
            self.monitor.join(timeout=5)

    def _watch(self):
        while not self.stopping.wait(2):
            with self.lock:
                if self.active and not self.awaiting_runtime and not self.attached():
                    self.cancelled.set()
            if self.orphans and self.attached() and self.execution_lock.acquire(blocking=False):
                try:
                    self._recover()
                except ColabUnavailable:
                    self.phase = "error"
                    self.stopping.wait(10)
                finally:
                    self.execution_lock.release()

    def attached(self):
        return self.runtime_id is not None and time.time() < self.lease_until

    def manager_update(self, update: ManagerUpdate):
        if not self.managed:
            raise ValueError("Server allocation is disabled")
        with self.lock:
            self.manager_until = time.time() + update.lease_seconds
            # A job may have arrived since the manager last observed idle. Do not
            # withdraw its healthy lease while the subsequent detach is refused.
            self.manager_phase = (
                "idle" if update.phase == "releasing" and self.active else update.phase
            )
            self.manager_error = update.error
        return self.status()

    def can_allocate(self):
        return (
            self.managed
            and time.time() < self.manager_until
            and self.manager_error is None
            and self.manager_phase != "releasing"
        )

    def attach(self, sessions: dict, lease_seconds: int):
        session = validate_session(sessions, self.session_name)
        identifier = hashlib.sha256(session["endpoint"].encode()).hexdigest()[:16]
        with self.lock:
            if self.active and not self.awaiting_runtime and identifier != self.runtime_id:
                raise ValueError("Cancel the active job before replacing the runtime")
            if self.orphans and any(row[2] != identifier for row in self.orphans):
                raise ValueError("Confirm release of the previous runtime before replacing it")
            if identifier in self.confirmed_released:
                raise ValueError("This runtime has already been released")
            if identifier != self.runtime_id:
                self.prepared_runtime = None
                self.last_activity = time.time()
            self.transport.attach(sessions)
            self.runtime_id = identifier
            self.accelerator = session.get("accelerator", "GPU")
            self.lease_until = time.time() + lease_seconds
            if not self.active:
                self.phase = (
                    "error"
                    if self.orphans
                    else ("ready" if self.prepared_runtime == identifier else "idle")
                )
        return self.status()

    def detach(self, force=False, released=False):
        with self.lock:
            if self.active and not force:
                raise ValueError("A Colab job is active")
            self.lease_until = 0
            self.cancelled.set()
            self.prepared_runtime = None
            self.phase = "unavailable"
            if released and self.runtime_id:
                self.confirmed_released.add(self.runtime_id)
                self.orphans = [row for row in self.orphans if row[2] != self.runtime_id]
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE attempts SET status='failed', error=? WHERE status='orphaned' AND runtime=?",
                        ("운영자가 Colab 런타임을 해제했습니다.", self.runtime_id),
                    )
        return self.status()

    def status(self):
        with self.lock:
            attached = self.attached()
            state = self.phase if attached else "unavailable"
            if self.managed and not attached and time.time() < self.manager_until:
                state = "error" if self.manager_error else self.manager_phase
                if self.active and not self.cancelled.is_set() and not self.manager_error:
                    state = "allocating"
            return {
                "state": state,
                "accelerator": self.accelerator,
                "prepared": attached
                and not self.orphans
                and self.prepared_runtime == self.runtime_id,
                "idle_timeout_seconds": self.idle_seconds,
                "active": self.active is not None,
                "release_requested": attached
                and self.active is None
                and time.time() - self.last_activity >= self.idle_seconds,
                "runtime_id": self.runtime_id if attached else None,
                "allocation_mode": "server" if self.managed else "operator",
                "can_allocate": self.can_allocate(),
                "allocation_requested": self.managed
                and self.active is not None
                and not self.cancelled.is_set()
                and not attached,
                "manager_error": MANAGER_ERRORS.get(self.manager_error),
                "cleanup_pending": bool(self.orphans),
            }

    def attempt(self, job: str, attempt: str):
        with self.connect() as connection:
            record = connection.execute(
                "SELECT status, error FROM attempts WHERE job=? AND attempt=?", (job, attempt)
            ).fetchone()
        if record is None:
            raise KeyError(attempt)
        result = dict(record)
        if result["status"] == "orphaned":
            result.update(status="failed", cleanup_pending=True)
        return result

    def submit(self, job: str, attempt: str):
        if not valid_identifier(job) or not valid_identifier(attempt):
            raise ValueError("Invalid job identifier")
        directory = self.data / job
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Job directory does not exist")
        input_path = directory / "input"
        if input_path.is_symlink() or not input_path.is_file():
            raise ValueError("Job input does not exist")
        if input_path.stat().st_size > 120 * 1024**2:
            raise ValueError("Colab 업로드는 120 MiB 이하여야 합니다.")
        request = directory / "request.json"
        if request.is_symlink() or not request.is_file() or request.stat().st_size > 32 * 1024**2:
            raise ValueError("Invalid job request")
        fingerprint = hashlib.sha256(request.read_bytes()).hexdigest()
        with self.lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT job, request_hash, status, error FROM attempts WHERE attempt=?", (attempt,)
            ).fetchone()
            if existing:
                if existing["job"] != job or existing["request_hash"] != fingerprint:
                    raise ValueError("Attempt identifier already used for another request")
                return self.attempt(job, attempt)
            if (self.managed or not self.attached()) and not self.can_allocate():
                raise ColabUnavailable(
                    MANAGER_ERRORS.get(self.manager_error)
                    or "Colab 연결 대기: 런타임 관리자 연결을 확인하세요."
                )
            if self.active:
                raise ValueError("Another Colab job is active")
            connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, 'running', NULL, ?, ?)",
                (
                    attempt,
                    job,
                    fingerprint,
                    self.runtime_id if self.attached() else None,
                    time.time(),
                ),
            )
            connection.commit()
            self.active = (job, attempt)
            self.awaiting_runtime = not self.attached()
            self.cancelled.clear()
            self.last_activity = time.time()
            self.job_thread = threading.Thread(
                target=self._run, args=(job, attempt), daemon=True, name="colab-job"
            )
            self.job_thread.start()
        return {"status": "running", "error": None}

    def cancel(self, job: str, attempt: str):
        with self.lock:
            if self.active == (job, attempt):
                self.cancelled.set()
        return self.attempt(job, attempt)

    def _code(self, function: str, *arguments):
        return (
            "import sys\nsys.path.insert(0, '/content/beatit/source/src')\n"
            "from transcription.colab_payload import "
            + function
            + "\n"
            + function
            + "("
            + ", ".join(repr(arg) for arg in arguments)
            + ")\n"
        )

    def _recover(self):
        remaining = []
        for job, attempt, runtime in self.orphans:
            if runtime != self.runtime_id:
                remaining.append((job, attempt, runtime))
                continue
            try:
                self.transport.execute(self._code("cancel", job, attempt), timeout=30)
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE attempts SET status='failed' WHERE attempt=?", (attempt,)
                    )
            except ColabUnavailable:
                remaining.append((job, attempt, runtime))
        self.orphans = remaining
        if remaining:
            raise ColabUnavailable(
                "이전 Colab 작업 정리가 필요합니다. 운영자 CLI를 다시 연결하세요."
            )

    def _check(self):
        if self.cancelled.is_set() or self.stopping.is_set() or not self.attached():
            raise InterruptedError("Colab 작업이 취소되었거나 연결이 종료되었습니다.")

    def _progress(self, directory: Path, stage: str, fraction: float, message: str, started: float):
        if directory.is_dir():
            save_json(
                directory / "progress.json",
                {
                    "stage": stage,
                    "progress": round(fraction, 3),
                    "message": message,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                },
            )

    def _collect_log(self, remote: str, directory: Path):
        if not directory.is_dir():
            return
        try:
            with tempfile.TemporaryDirectory(dir=self.state) as scratch:
                path = Path(scratch) / "remote.log"
                self.transport.download(remote + "/worker.log", path, limit=2 * 1024**2)
                content = path.read_text(errors="replace")[-128 * 1024 :]
                content = re.sub(
                    r"(?i)(authorization:|colab-runtime-proxy-token=)[^\s]+",
                    r"\1[redacted]",
                    content,
                )
                seed = getattr(self.transport, "seed", None)
                if seed and seed.get("token"):
                    content = content.replace(seed["token"], "[redacted]")
                with (directory / "worker.log").open("a") as log:
                    log.write("\n[colab] 원격 분석 로그\n" + content + "\n")
        except Exception:
            # Diagnostics never change an otherwise successful result or expose CLI state.
            pass

    def _prepare(self, directory: Path, started: float):
        self._check()
        try:
            marker = self.transport.read_json(REMOTE + "/prepared.json")
        except FileNotFoundError:
            marker = {}
        if marker.get("fingerprint") == self.fingerprint:
            self.prepared_runtime = self.runtime_id
            return True
        self.phase = "preparing"
        self._progress(directory, "preparing", 0.02, "Colab GPU 환경과 모델을 준비합니다.", started)
        self.transport.execute(
            "from pathlib import Path\nPath('/content/beatit').mkdir(exist_ok=True)"
        )
        self.transport.upload(self.source, REMOTE + "/source.tar")
        payload = Path(__file__).with_name("colab_payload.py").read_text()
        self.transport.execute(
            payload + "\nsys.path.insert(0, '/content/beatit/source/src')\n"
            "for name in list(sys.modules):\n"
            "    if name == 'transcription' or name.startswith('transcription.'):\n"
            "        del sys.modules[name]\n"
            # Extract before importing the just-uploaded package.
            "with tarfile.open(BASE / 'source.tar') as a:\n"
            "    (BASE / 'source').mkdir(exist_ok=True)\n"
            "    a.extractall(BASE / 'source', filter='data')\n"
            + "prepare("
            + repr(self.fingerprint)
            + ")\n",
            timeout=900,
        )
        self._check()
        self.prepared_runtime = self.runtime_id
        return False

    def _run(self, job: str, attempt: str):
        directory = self.data / job
        remote = REMOTE + "/jobs/" + job + "/" + attempt
        started = time.monotonic()
        launched = False
        uncertain = False
        runtime = None
        final, error = "failed", None
        with self.execution_lock:
            try:
                if self.cancelled.is_set() or self.stopping.is_set():
                    raise InterruptedError("Colab 작업이 취소되었습니다.")
                if self.managed and not self.can_allocate():
                    raise ColabUnavailable(
                        MANAGER_ERRORS.get(self.manager_error)
                        or "서버 Colab 관리자 연결을 확인하세요."
                    )
                if not self.attached():
                    self._progress(
                        directory, "allocating", 0.01, "Colab GPU를 할당합니다.", started
                    )
                    deadline = time.monotonic() + 210
                    while not self.attached():
                        if self.cancelled.is_set() or self.stopping.is_set():
                            raise InterruptedError("Colab GPU 할당 대기가 취소되었습니다.")
                        if not self.can_allocate() or time.monotonic() >= deadline:
                            raise ColabUnavailable(
                                MANAGER_ERRORS.get(self.manager_error)
                                or "Colab GPU 할당 대기 시간이 초과되었습니다."
                            )
                        self.cancelled.wait(0.2)
                if self.cancelled.is_set() or self.stopping.is_set():
                    raise InterruptedError("Colab 작업이 취소되었습니다.")
                if self.managed and not self.can_allocate():
                    raise ColabUnavailable(
                        MANAGER_ERRORS.get(self.manager_error)
                        or "서버 Colab 관리자 연결을 확인하세요."
                    )
                runtime = self.runtime_id
                self.awaiting_runtime = False
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE attempts SET runtime=? WHERE attempt=?", (runtime, attempt)
                    )
                with tempfile.TemporaryDirectory(dir=self.state) as scratch:
                    packed = Path(scratch) / "input.zip"
                    pack_input(directory, packed)
                    if packed.stat().st_size > 128 * 1024**2:
                        raise ValueError("Colab 전송 데이터는 128 MiB 이하여야 합니다.")
                    self._recover()
                    reused = self._prepare(directory, started)
                    self._check()
                    self.phase = "running"
                    self._progress(
                        directory, "uploading", 0.06, "Colab GPU로 오디오를 전송합니다.", started
                    )
                    self.transport.execute(
                        "from pathlib import Path\nPath("
                        + repr(remote)
                        + ").mkdir(parents=True, exist_ok=True)\n"
                    )
                    self.transport.upload(packed, remote + "/input.zip")
                    self._check()
                    launched = True
                    self.transport.execute(self._code("launch", job, attempt))
                    deadline = time.monotonic() + 3600
                    while time.monotonic() < deadline:
                        self._check()
                        try:
                            status = self.transport.read_json(remote + "/status.json")
                        except FileNotFoundError:
                            status = {"state": "running"}
                        try:
                            progress = self.transport.read_json(remote + "/progress.json")
                            self._progress(
                                directory,
                                progress["stage"],
                                0.1 + 0.8 * progress["progress"],
                                progress["message"],
                                started,
                            )
                        except FileNotFoundError:
                            pass
                        if status["state"] == "completed":
                            break
                        if status["state"] in {"failed", "cancelled"}:
                            raise RuntimeError("Colab 분석이 중단되었습니다. 다시 시도하세요.")
                        if self.cancelled.wait(2):
                            self._check()
                    else:
                        raise TimeoutError("Colab 분석 제한 시간을 초과했습니다.")
                    self._check()
                    self._progress(
                        directory, "downloading", 0.94, "분석 결과를 가져옵니다.", started
                    )
                    archive = Path(scratch) / "result.zip"
                    self.transport.download(remote + "/result.zip", archive)
                    self._check()
                    self._progress(
                        directory, "publishing", 0.98, "악보와 오디오를 저장합니다.", started
                    )
                    result = import_output(
                        archive,
                        directory,
                        {
                            "accelerator": self.accelerator,
                            "runtime_id": self.runtime_id,
                            "prepared_reused": reused,
                            "source_fingerprint": self.fingerprint,
                            "total_elapsed_seconds": round(time.monotonic() - started, 1),
                        },
                    )
                    if directory.is_dir():
                        with (directory / "worker.log").open("a") as log:
                            log.write(
                                "[colab] CUDA 분석 완료 · "
                                + self.accelerator
                                + (" · 준비된 런타임 재사용" if reused else " · 환경 준비 완료")
                                + " · "
                                + str(result["event_count"])
                                + " events\n"
                            )
                    self._progress(directory, "complete", 1, "악보 생성 완료", started)
                    final = "completed"
            except InterruptedError as exception:
                final, error = "cancelled", str(exception)
            except Exception as exception:
                error = (
                    str(exception)
                    if isinstance(exception, (ColabUnavailable, ValueError, RuntimeError))
                    else "Colab 작업을 완료하지 못했습니다. 다시 시도하세요."
                )
            finally:
                if launched and final != "completed":
                    try:
                        self.transport.execute(self._code("cancel", job, attempt), timeout=30)
                    except Exception:
                        error = "Colab 연결이 끊겼습니다. 운영자 CLI에서 런타임을 해제하세요."
                        uncertain = runtime not in self.confirmed_released
                if not uncertain and runtime is not None:
                    if launched:
                        self._collect_log(remote, directory)
                    try:
                        self.transport.execute(
                            "import shutil\nshutil.rmtree("
                            + repr(remote)
                            + ", ignore_errors=True)\n",
                            timeout=30,
                        )
                    except Exception:
                        pass
                with self.lock, self.connect() as connection:
                    if uncertain:
                        final = "orphaned"
                        self.orphans.append((job, attempt, runtime))
                    connection.execute(
                        "UPDATE attempts SET status=?, error=?, updated=? WHERE attempt=?",
                        (final, error, time.time(), attempt),
                    )
                    self.active = None
                    self.awaiting_runtime = False
                    self.last_activity = time.time()
                    self.phase = (
                        "error"
                        if uncertain
                        else (
                            "ready" if self.attached() and self.prepared_runtime else "unavailable"
                        )
                    )


def create_controller(controller: Controller, token: str) -> FastAPI:
    if len(token) < 32:
        raise ValueError("A private controller API token is required")

    @asynccontextmanager
    async def lifespan(app):
        controller.start()
        yield
        controller.stop()

    app = FastAPI(title="Beatit Colab controller", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        from fastapi.responses import JSONResponse

        if request.url.path != "/health":
            if not hmac.compare_digest(
                request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
            ):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Invalid request size"}, status_code=400)
            if length > 64 * 1024:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/v1/status")
    def status():
        return controller.status()

    @app.post("/v1/manager")
    def manager(update: ManagerUpdate):
        try:
            return controller.manager_update(update)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/v1/attachment")
    def attach(attachment: Attachment):
        try:
            return controller.attach(attachment.sessions, attachment.lease_seconds)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.delete("/v1/attachment")
    def detach(force: bool = False, released: bool = False):
        try:
            return controller.detach(force, released)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/v1/jobs/{job}", status_code=202)
    def submit(job: str, submission: Submission):
        try:
            return controller.submit(job, submission.attempt)
        except ColabUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/v1/jobs/{job}/{attempt}")
    def attempt(job: str, attempt: str):
        try:
            return controller.attempt(job, attempt)
        except KeyError:
            raise HTTPException(404, "Attempt not found") from None

    @app.post("/v1/jobs/{job}/{attempt}/cancel")
    def cancel(job: str, attempt: str):
        try:
            return controller.cancel(job, attempt)
        except KeyError:
            raise HTTPException(404, "Attempt not found") from None

    return app


def main():
    import uvicorn

    os.umask(0o077)
    controller = Controller(
        Path(os.environ.get("DRUM_DATA_DIR", "/app/data")),
        Path(os.environ.get("COLAB_STATE_DIR", "/app/colab-state")),
        Path(os.environ.get("COLAB_SOURCE_ARCHIVE", "/app/beatit-source.tar")),
        idle_seconds=int(os.environ.get("COLAB_IDLE_SECONDS", "600")),
    )
    token = Path(os.environ["COLAB_CONTROLLER_TOKEN_FILE"]).read_text().strip()
    uvicorn.run(create_controller(controller, token), host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
