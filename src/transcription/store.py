import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from .pipeline import save_json

TERMINAL = {"completed", "failed", "cancelled"}


class JobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "jobs.sqlite3"
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.current = None
        self.process = None
        self.thread = None
        self.instance = None
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, error TEXT
            )""")

    def connect(self):
        return sqlite3.connect(self.database, timeout=15)

    def start(self):
        self.instance = (self.root / "server.lock").open("a")
        try:
            fcntl.flock(self.instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.instance.close()
            raise RuntimeError(
                "이 데이터 디렉토리에서 이미 서버가 실행 중입니다. 서버는 한 프로세스로 실행하세요."
            ) from None
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', error='서버가 중단되었습니다. 다시 시도하세요.' WHERE status='running'"
            )
        self.thread = threading.Thread(target=self._loop, daemon=True, name="job-queue")
        self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self._terminate(self.process)
        if self.thread:
            self.thread.join(timeout=10)
        if self.instance:
            self.instance.close()

    def directory(self, identifier: str) -> Path:
        # IDs always originate from uuid4.hex. Never allow paths supplied by clients.
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise KeyError(identifier)
        return self.root / identifier

    def add(self, identifier: str, filename: str):
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, 'queued', ?, ?, NULL)",
                (identifier, filename, now, now),
            )

    def update(self, identifier: str, status: str, error=None):
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, updated=?, error=? WHERE id=?",
                (status, time.time(), error, identifier),
            )

    def list(self) -> list[dict]:
        with self.connect() as connection:
            identifiers = connection.execute(
                "SELECT id FROM jobs ORDER BY created DESC LIMIT 100"
            ).fetchall()
        return [self.get(row[0], include_result=False) for row in identifiers]

    def get(self, identifier: str, include_result=True) -> dict:
        directory = self.directory(identifier)
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        job = dict(row)
        job["progress"] = self._read(
            directory / "progress.json",
            {"stage": "queued", "progress": 0, "message": "분석 대기 중"},
        )
        job["has_result"] = (directory / "result.json").is_file()
        if include_result:
            job["request"] = self._read(directory / "request.json", {})
            if job["has_result"]:
                job["result"] = self._read(directory / "result.json", {})
        return job

    @staticmethod
    def _read(path, default):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def revise(self, identifier: str, options: dict, events=None):
        with self.lock:
            job = self.get(identifier)
            if job["status"] not in TERMINAL or not job["has_result"]:
                raise ValueError("완료된 악보만 수정할 수 있습니다.")
            directory = self.directory(identifier)
            request = {
                "filename": job["filename"],
                "options": options,
                "revision": True,
                "events": events,
            }
            save_json(directory / "request.json", request)
            save_json(
                directory / "progress.json",
                {"stage": "queued", "progress": 0, "message": "악보 재생성 대기 중"},
            )
            (directory / "error.json").unlink(missing_ok=True)
            self.update(identifier, "queued")

    def retry(self, identifier: str):
        with self.lock:
            job = self.get(identifier)
            if job["status"] not in {"failed", "cancelled"}:
                raise ValueError("실패하거나 취소된 작업만 다시 시도할 수 있습니다.")
            directory = self.directory(identifier)
            (directory / "error.json").unlink(missing_ok=True)
            save_json(
                directory / "progress.json",
                {"stage": "queued", "progress": 0, "message": "다시 분석 대기 중"},
            )
            self.update(identifier, "queued")

    def cancel(self, identifier: str):
        with self.lock:
            job = self.get(identifier)
            if job["status"] in TERMINAL:
                return
            self.update(identifier, "cancelled")
            if self.current == identifier and self.process and self.process.poll() is None:
                self._terminate(self.process)

    def delete(self, identifier: str):
        import shutil

        with self.lock:
            self.cancel(identifier)
            shutil.rmtree(self.directory(identifier), ignore_errors=True)
            with self.connect() as connection:
                connection.execute("DELETE FROM jobs WHERE id=?", (identifier,))

    @staticmethod
    def _terminate(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    def _loop(self):
        while not self.stopping.is_set():
            with self.lock:
                with self.connect() as connection:
                    row = connection.execute(
                        "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                    ).fetchone()
                if row:
                    identifier = row[0]
                    directory = self.directory(identifier)
                    self.current = identifier
                    self.update(identifier, "running")
                    try:
                        with (directory / "worker.log").open("a") as log:
                            self.process = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-u",
                                    "-m",
                                    "transcription.worker",
                                    str(directory),
                                ],
                                stdout=log,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                                env={
                                    **os.environ,
                                    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                                    "OMP_NUM_THREADS": "4",
                                },
                            )
                    except OSError as error:
                        self.update(identifier, "failed", str(error))
                        self.current, self.process = None, None
            if not row:
                self.stopping.wait(0.4)
                continue
            if self.process is None:
                continue
            process = self.process
            process.wait()
            with self.lock:
                try:
                    job = self.get(identifier)
                    if job["status"] == "running":
                        if process.returncode == 0 and job["has_result"]:
                            self.update(identifier, "completed")
                        else:
                            error = self._read(directory / "error.json", {}).get(
                                "message", "작업이 중단되었습니다. 로그를 확인하세요."
                            )
                            self.update(identifier, "failed", error)
                except KeyError:
                    pass  # A cancelled job may have been deleted while the process exited.
                self.current, self.process = None, None
