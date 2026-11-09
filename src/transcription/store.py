"""MySQL authoritative projects, immutable object artifacts and fenced job leases.

Pipeline JSON files are a subprocess interchange format only. Reads and queue decisions
use MySQL; each inference attempt has a private, disposable working directory.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .database import Database
from .models import ScoreMetadata
from .object_storage import ObjectStorage, file_digest
from .pipeline import save_json
from .playback_preview import (
    PLAYBACK_PREVIEWS,
    PREVIEW_FILES,
    PREVIEW_METADATA,
    prepare_playback_previews,
    preview_matches,
)
from .upload_journal import (
    abandon_uploads,
    collect_staged_objects,
    finish_publication,
    lock_publication,
    put_staged_object,
)

TERMINAL = {"completed", "failed", "cancelled"}
QUEUED = {"stage": "queued", "progress": 0, "message": "분석 대기 중"}
_JSON_STATE = {"request.json", "progress.json", "result.json", "error.json"}
_MEDIA = (
    {"input", "audio.wav", "drums.wav", "worker.log", "analysis.json", "events.json"}
    | PREVIEW_FILES
    | {f"stem-{name}.flac" for name in ("kick", "snare", "toms", "hh", "cymbals")}
)


class JobStore:
    def __init__(self, root: Path, database_url: str | None = None, objects=None, db=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db or Database(database_url)
        self.pool = self.db.pool
        self.objects = objects if objects is not None else ObjectStorage()
        self.lock = threading.RLock()
        self._cache_lock_guard = threading.Lock()
        self._cache_locks = weakref.WeakValueDictionary()
        self.stopping = threading.Event()
        self.current = self.process = self.thread = None
        self.instance = uuid4().hex
        self.lease_seconds = int(os.environ.get("BEATIT_LEASE_SECONDS", "90"))
        if self.lease_seconds < 30:
            raise ValueError("BEATIT_LEASE_SECONDS must be at least 30")
        self.max_pending = int(os.environ.get("BEATIT_MAX_PENDING_PER_USER", "3"))

    def connect(self):
        return self.db.connection()

    def start(self):
        self._recover_legacy_records()
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="job-queue")
        self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self._terminate(self.process)
        if self.thread:
            self.thread.join(timeout=15)

    def close(self):
        self.stop()
        self.db.close()

    @staticmethod
    def _identifier(identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise KeyError(identifier)
        return identifier

    def directory(self, identifier: str) -> Path:
        return self.root / self._identifier(identifier)

    @staticmethod
    def _read(path, default):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    @staticmethod
    def _owned(connection, identifier, owner_id=None, lock=False):
        query = "SELECT * FROM beatit.jobs WHERE id=%s"
        values = [identifier]
        if owner_id is not None:
            query += " AND owner_id=%s"
            values.append(owner_id)
        if lock:
            query += " FOR UPDATE"
        row = connection.execute(query, values).fetchone()
        if row is None:
            raise KeyError(identifier)
        return row

    @staticmethod
    def _job(row, include_result=True):
        job = {
            key: row[key]
            for key in (
                "id",
                "project_id",
                "owner_id",
                "filename",
                "status",
                "created",
                "updated",
                "error",
                "progress",
                "attempt",
            )
        }
        job["has_result"] = row["result"] is not None
        if include_result:
            job["request"] = row["request"]
            if job["has_result"]:
                job["result"] = row["result"]
        return job

    def get(self, identifier: str, include_result=True, owner_id=None) -> dict:
        self._identifier(identifier)
        with self.connect() as connection:
            return self._job(self._owned(connection, identifier, owner_id), include_result)

    def list(self, owner_id: str, query: str = "", project_id=None) -> list[dict]:
        sql = "SELECT * FROM beatit.jobs WHERE owner_id=%s AND filename LIKE %s"
        params = [owner_id, "%" + query + "%"]
        if project_id is not None:
            sql += " AND project_id=%s"
            params.append(project_id)
        with self.connect() as connection:
            rows = connection.execute(sql + " ORDER BY created DESC LIMIT 100", params).fetchall()
        return [self._job(row, False) for row in rows]

    def create_project(self, owner_id: str, title: str | None = None, identifier=None, name=None):
        identifier = self._identifier(identifier or uuid4().hex)
        title = (name if name is not None else title or "").strip()[:200]
        if not title:
            raise ValueError("프로젝트 이름을 입력하세요.")
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO beatit.projects(id,owner_id,title,created,updated)
                   VALUES (%s,%s,%s,%s,%s)""",
                (identifier, owner_id, title, now, now),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM beatit.projects WHERE id=%s", (identifier,)
                ).fetchone()
            )

    def get_project(self, identifier: str, owner_id: str):
        self._identifier(identifier)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT p.*, count(j.id) AS job_count FROM beatit.projects p
                   LEFT JOIN beatit.jobs j ON j.project_id=p.id AND j.owner_id=p.owner_id
                   WHERE p.id=%s AND p.owner_id=%s GROUP BY p.id""",
                (identifier, owner_id),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def list_projects(self, owner_id: str, query=""):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT p.*, count(j.id) AS job_count FROM beatit.projects p
                   LEFT JOIN beatit.jobs j ON j.project_id=p.id AND j.owner_id=p.owner_id
                   WHERE p.owner_id=%s AND p.title LIKE %s
                   GROUP BY p.id ORDER BY p.updated DESC LIMIT 100""",
                    (owner_id, "%" + query + "%"),
                ).fetchall()
            ]

    def _admit(self, connection, owner_id):
        # Serialize admission for one owner even across API processes.
        connection.execute(
            """INSERT INTO beatit.owner_schedule(owner_id) VALUES (%s)
               ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id)""",
            (owner_id,),
        )
        connection.execute(
            "SELECT owner_id FROM beatit.owner_schedule WHERE owner_id=%s FOR UPDATE", (owner_id,)
        )
        pending = connection.execute(
            "SELECT count(*) AS n FROM beatit.jobs WHERE owner_id=%s AND status IN ('queued','running')",
            (owner_id,),
        ).fetchone()["n"]
        if pending >= self.max_pending:
            raise ValueError(f"계정당 대기·진행 작업은 {self.max_pending}개까지 가능합니다.")

    def add(self, identifier: str, filename: str, owner_id: str, project_id=None):
        directory = self.directory(identifier)
        request = self._read(directory / "request.json", {})
        if not (directory / "input").is_file():
            raise ValueError("업로드한 파일이 없습니다.")
        with self.connect() as connection:
            self._admit(connection, owner_id)
            if (
                project_id is not None
                and not connection.execute(
                    "SELECT 1 FROM beatit.projects WHERE id=%s AND owner_id=%s",
                    (project_id, owner_id),
                ).fetchone()
            ):
                raise KeyError(project_id)
        manifests, payloads = self._capture(identifier, directory)
        try:
            now = time.time()
            with self.connect() as connection:
                self._admit(connection, owner_id)
                if project_id is None:
                    project_id = uuid4().hex
                    connection.execute(
                        "INSERT INTO beatit.projects VALUES (%s,%s,%s,%s,%s)",
                        (project_id, owner_id, filename, now, now),
                    )
                elif not connection.execute(
                    "SELECT 1 FROM beatit.projects WHERE id=%s AND owner_id=%s FOR UPDATE",
                    (project_id, owner_id),
                ).fetchone():
                    raise KeyError(project_id)
                connection.execute(
                    """INSERT INTO beatit.jobs
                       (id,project_id,owner_id,filename,status,created,updated,queued_at,request,progress,payloads)
                       VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s)""",
                    (
                        identifier,
                        project_id,
                        owner_id,
                        filename,
                        now,
                        now,
                        now,
                        json.dumps(request),
                        json.dumps(QUEUED),
                        json.dumps(payloads),
                    ),
                )
                connection.execute(
                    "INSERT IGNORE INTO beatit.owner_schedule(owner_id) VALUES (%s)",
                    (owner_id,),
                )
                self._write_manifests(connection, identifier, manifests)
        except BaseException:
            # Do not synchronously delete: COMMIT may have succeeded despite a
            # lost acknowledgement. Atomic journal removal/ref checks decide GC.
            abandon_uploads(self, manifests)
            raise

    @staticmethod
    def _safe_path(relative):
        path = PurePosixPath(relative)
        if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise KeyError(relative)
        return path

    @staticmethod
    def _durable(relative):
        parts = PurePosixPath(relative).parts
        if len(parts) == 1:
            return relative in _MEDIA
        return (
            len(parts) == 2
            and re.fullmatch(r"render-\d+", parts[0]) is not None
            and not parts[1].endswith(".tmp")
            and PurePosixPath(parts[1]).suffix
            in {".musicxml", ".svg", ".pdf", ".mid", ".zip", ".json", ".log"}
        )

    def _capture(self, identifier, directory):
        with self.connect() as connection:
            previous = {
                row["path"]: row
                for row in connection.execute(
                    "SELECT * FROM beatit.artifacts WHERE job_id=%s", (identifier,)
                ).fetchall()
            }
        manifests, payloads = [], {}
        try:
            for path in sorted(directory.rglob("*")):
                relative = path.relative_to(directory).as_posix()
                if path.is_symlink() or not path.is_file() or not self._durable(relative):
                    continue
                digest = file_digest(path)
                existing = previous.get(relative)
                # Never recycle a retired key; each new PUT is journaled first.
                key = f"jobs/{identifier}/{uuid4().hex}/{digest}/{relative}"
                token = None
                if not existing or existing["sha256"] != digest:
                    try:
                        token = put_staged_object(self, key, path)
                    except Exception as error:
                        if relative not in PREVIEW_FILES:
                            raise
                        logging.warning(
                            "Optional playback object upload failed (%s); original audio remains available",
                            type(error).__name__,
                        )
                        continue
                else:
                    key = existing["object_key"]
                manifest = {
                    "path": relative,
                    "object_key": key,
                    "size": path.stat().st_size,
                    "sha256": digest,
                }
                if token:
                    manifest["upload_token"] = token
                manifests.append(manifest)
                if path.suffix == ".json":
                    payloads[relative] = json.loads(path.read_text())
        except BaseException:
            abandon_uploads(self, manifests)
            raise
        return manifests, payloads

    @staticmethod
    def _write_manifests(connection, identifier, manifests):
        for manifest in manifests:
            lock_publication(connection, manifest)
            connection.execute(
                """INSERT IGNORE INTO beatit.object_garbage(object_key,created)
                   SELECT object_key,UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) FROM beatit.artifacts
                   WHERE job_id=%s AND path=%s AND object_key<>%s""",
                (identifier, manifest["path"], manifest["object_key"]),
            )
            connection.execute(
                """INSERT INTO beatit.artifacts(job_id,path,object_key,size,sha256)
                   VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                   object_key=VALUES(object_key),size=VALUES(size),sha256=VALUES(sha256)""",
                (
                    identifier,
                    manifest["path"],
                    manifest["object_key"],
                    manifest["size"],
                    manifest["sha256"],
                ),
            )
            finish_publication(connection, manifest)

    def _cached_object(self, manifest):
        # Cache keys include digest so a new revision cannot reuse stale bytes.
        path = self.root / ".objects" / manifest["sha256"] / PurePosixPath(manifest["path"]).name
        if path.is_file() and path.stat().st_size == manifest["size"]:
            return path
        # A slow cold stem must not stall playback of an already cached track,
        # another artifact, cancellation or the inference process state lock.
        # Weak values release idle locks once the concurrent readers finish.
        with self._cache_lock_guard:
            download_lock = self._cache_locks.get(path)
            if download_lock is None:
                download_lock = threading.Lock()
                self._cache_locks[path] = download_lock
        with download_lock:
            if path.is_file() and path.stat().st_size == manifest["size"]:
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            try:
                self.objects.get(manifest["object_key"], temporary)
                if (
                    temporary.stat().st_size != manifest["size"]
                    or file_digest(temporary) != manifest["sha256"]
                ):
                    raise OSError("Stored artifact integrity check failed")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def artifact_manifest(self, identifier: str, relative: str, owner_id=None):
        """Read an authorized immutable object reference without downloading bytes."""
        self._identifier(identifier)
        self._safe_path(relative)
        sql = """SELECT a.* FROM beatit.artifacts a JOIN beatit.jobs j ON j.id=a.job_id
                 WHERE a.job_id=%s AND a.path=%s"""
        arguments = [identifier, relative]
        if owner_id is not None:
            sql += " AND j.owner_id=%s"
            arguments.append(owner_id)
        with self.connect() as connection:
            manifest = connection.execute(sql, arguments).fetchone()
        if manifest is None:
            raise KeyError(relative)
        return manifest

    def playback_manifest(self, identifier: str, source: str, owner_id=None):
        original = self.artifact_manifest(identifier, source, owner_id)
        preview = PLAYBACK_PREVIEWS.get(source)
        if preview:
            query = """SELECT a.*, JSON_EXTRACT(j.payloads, '$."playback-previews.json"') AS preview_metadata
                       FROM beatit.artifacts a JOIN beatit.jobs j ON j.id=a.job_id
                       WHERE a.job_id=%s AND a.path=%s"""
            arguments = [identifier, preview]
            if owner_id is not None:
                query += " AND j.owner_id=%s"
                arguments.append(owner_id)
            with self.connect() as connection:
                candidate = connection.execute(query, arguments).fetchone()
            if candidate:
                metadata = candidate.pop("preview_metadata", None)
                if isinstance(metadata, (str, bytes)):
                    try:
                        metadata = json.loads(metadata)
                    except ValueError:
                        metadata = None
                if preview_matches(metadata, source, original["sha256"], candidate["sha256"]):
                    return candidate, "audio/mp4"
        return original, "audio/flac" if source.endswith(".flac") else "audio/wav"

    def backfill_playback_previews(self, identifier: str, owner_id=None):
        """Publish only optional playback derivatives, without changing score revisions."""
        self._identifier(identifier)
        with self.connect() as connection:
            row = self._owned(connection, identifier, owner_id)
            if row["status"] != "completed" or row["result"] is None:
                raise ValueError("Only completed jobs can receive playback previews")
            previous = {
                item["path"]: item
                for item in connection.execute(
                    "SELECT * FROM beatit.artifacts WHERE job_id=%s", (identifier,)
                ).fetchall()
            }
        originals = {
            name: previous[name]["sha256"] for name in PLAYBACK_PREVIEWS if name in previous
        }
        with tempfile.TemporaryDirectory(dir=self.root, prefix="playback-preview-") as scratch:
            directory = Path(scratch)
            for name in set(originals) | PREVIEW_FILES:
                if name in previous:
                    shutil.copyfile(self._cached_object(previous[name]), directory / name)
            if PREVIEW_METADATA in row["payloads"]:
                save_json(directory / PREVIEW_METADATA, row["payloads"][PREVIEW_METADATA])
            report = prepare_playback_previews(directory)
            if not report["created"] and not report["reused"]:
                return {**report, "published": []}
            metadata = self._read(directory / PREVIEW_METADATA, {})
            manifests = []
            for name in [*PLAYBACK_PREVIEWS.values(), PREVIEW_METADATA]:
                path = directory / name
                if not path.is_file():
                    continue
                if name != PREVIEW_METADATA:
                    source = next(key for key, value in PLAYBACK_PREVIEWS.items() if value == name)
                    if not preview_matches(
                        metadata, source, originals.get(source), file_digest(path)
                    ):
                        continue
                digest = file_digest(path)
                old = previous.get(name)
                key = (
                    old["object_key"]
                    if old and old["sha256"] == digest
                    else f"jobs/{identifier}/{uuid4().hex}/{digest}/{name}"
                )
                token = None
                if not old or old["sha256"] != digest:
                    token = put_staged_object(self, key, path)
                manifest = {
                    "path": name,
                    "object_key": key,
                    "size": path.stat().st_size,
                    "sha256": digest,
                }
                if token:
                    manifest["upload_token"] = token
                manifests.append(manifest)
            with self.connect() as connection:
                current = self._owned(connection, identifier, owner_id, lock=True)
                if current["status"] != "completed" or current["result"] != row["result"]:
                    raise ValueError("Job changed while preparing playback previews; retry later")
                current_originals = {
                    item["path"]: item["sha256"]
                    for item in connection.execute(
                        "SELECT path,sha256 FROM beatit.artifacts WHERE job_id=%s AND path IN ('audio.wav','drums.wav') FOR UPDATE",
                        (identifier,),
                    ).fetchall()
                }
                if current_originals != originals:
                    raise ValueError("Audio changed while preparing playback previews; retry later")
                self._write_manifests(connection, identifier, manifests)
                payloads = {**current["payloads"], PREVIEW_METADATA: metadata}
                connection.execute(
                    "UPDATE beatit.jobs SET payloads=%s WHERE id=%s",
                    (json.dumps(payloads), identifier),
                )
            return {**report, "published": [item["path"] for item in manifests]}

    def file_path(self, identifier: str, relative: str, owner_id=None):
        return self._cached_object(self.artifact_manifest(identifier, relative, owner_id))

    def log(self, identifier, owner_id=None):
        with self.connect() as connection:
            row = self._owned(connection, identifier, owner_id)
            return row["payloads"].get("worker.log", "아직 로그가 없습니다.")

    def restore(self, identifier, destination=None):
        with self.connect() as connection:
            row = self._owned(connection, identifier)
            manifests = connection.execute(
                "SELECT * FROM beatit.artifacts WHERE job_id=%s", (identifier,)
            ).fetchall()
        destination = destination or self.directory(identifier)
        destination.mkdir(parents=True, exist_ok=True)
        for manifest in manifests:
            relative = self._safe_path(manifest["path"])
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._cached_object(manifest), target)
        for name in _JSON_STATE:
            (destination / name).unlink(missing_ok=True)
        # SQL JSON is authoritative, including repaired retry checkpoints.
        for relative, payload in row["payloads"].items():
            if relative.endswith(".json") and self._durable(relative):
                target = destination / self._safe_path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                save_json(target, payload)
        save_json(destination / "request.json", row["request"])
        save_json(destination / "progress.json", row["progress"])
        if row["result"] is not None:
            save_json(destination / "result.json", row["result"])
        return destination

    def revise(
        self, identifier, options, events=None, metadata=None, beat_review=None, owner_id=None
    ):
        with self.connect() as connection:
            self._admit(connection, owner_id or self._owned(connection, identifier)["owner_id"])
            row = self._owned(connection, identifier, owner_id, lock=True)
            if row["status"] not in TERMINAL or row["result"] is None:
                raise ValueError("완료된 악보만 수정할 수 있습니다.")
            saved = row["result"]
            request = {
                "filename": row["filename"],
                "options": options if options is not None else saved["options"],
                "revision": True,
                "events": events,
                "beat_review": beat_review if beat_review is not None else saved.get("beat_review"),
                "metadata": metadata
                if metadata is not None
                else saved.get(
                    "metadata", ScoreMetadata.for_filename(row["filename"]).model_dump()
                ),
            }
            if options is None:
                request.update(metadata_only=True, source_generation=saved["generation"])
            self._enqueue(connection, identifier, request, "악보 재생성 대기 중")
        # Interchange files are helpful for local operators, but never read by API get().
        directory = self.directory(identifier)
        directory.mkdir(exist_ok=True)
        save_json(directory / "request.json", request)

    @staticmethod
    def _enqueue(connection, identifier, request, message):
        now = time.time()
        connection.execute(
            """UPDATE beatit.jobs SET status='queued',request=%s,progress=%s,error=NULL,
               queued_at=%s,updated=%s,claim_token=NULL,lease_until=NULL WHERE id=%s""",
            (json.dumps(request), json.dumps({**QUEUED, "message": message}), now, now, identifier),
        )

    def retry(self, identifier, owner_id=None):
        with self.connect() as connection:
            initial = self._owned(connection, identifier, owner_id)
            self._admit(connection, initial["owner_id"])
            row = self._owned(connection, identifier, owner_id, lock=True)
            if row["status"] not in {"failed", "cancelled"}:
                raise ValueError("실패하거나 취소된 작업만 다시 시도할 수 있습니다.")
            request = dict(row["request"])
            stage = row["progress"].get("stage")
            payloads = row["payloads"]
            if stage in {"refining", "engraving"} and all(
                name in payloads for name in ("analysis.json", "events.json")
            ):
                if not request.get("revision") and stage == "engraving":
                    request["reestimate_tempo"] = True
                    payloads = dict(payloads)
                    analysis = dict(payloads["analysis.json"])
                    analysis["inference_elapsed_seconds"] = row["progress"].get("elapsed_seconds")
                    payloads["analysis.json"] = analysis
                    connection.execute(
                        "UPDATE beatit.jobs SET payloads=%s WHERE id=%s",
                        (json.dumps(payloads), identifier),
                    )
                request["revision"] = True
            self._enqueue(connection, identifier, request, "다시 분석 대기 중")
        directory = self.directory(identifier)
        directory.mkdir(exist_ok=True)
        save_json(directory / "request.json", request)

    def cancel(self, identifier, owner_id=None):
        with self.connect() as connection:
            row = self._owned(connection, identifier, owner_id, lock=True)
            if row["status"] in TERMINAL:
                return
            connection.execute(
                "UPDATE beatit.jobs SET status='cancelled',updated=%s WHERE id=%s",
                (time.time(), identifier),
            )
        with self.lock:
            if self.current == identifier and self.process and self.process.poll() is None:
                self._terminate(self.process)

    def delete(self, identifier, owner_id=None):
        self.cancel(identifier, owner_id)
        self._preserve_legacy_record(self.directory(identifier))
        with self.connect() as connection:
            self._owned(connection, identifier, owner_id, lock=True)
            connection.execute(
                """INSERT IGNORE INTO beatit.object_garbage(object_key,created)
                   SELECT object_key,%s FROM beatit.artifacts WHERE job_id=%s""",
                (time.time(), identifier),
            )
            connection.execute("DELETE FROM beatit.jobs WHERE id=%s", (identifier,))
        shutil.rmtree(self.directory(identifier), ignore_errors=True)
        self.collect_garbage()

    def collect_garbage(self):
        collect_staged_objects(self)
        with self.connect() as connection:
            garbage = connection.execute(
                "SELECT object_key FROM beatit.object_garbage LIMIT 100"
            ).fetchall()
        for item in garbage:
            try:
                with self.connect() as connection:
                    # Restore/import may preserve shared immutable keys. Never
                    # remove bytes that still have a committed artifact reference.
                    if connection.execute(
                        "SELECT 1 FROM beatit.artifacts WHERE object_key=%s LIMIT 1",
                        (item["object_key"],),
                    ).fetchone():
                        continue
                    self.objects.delete(item["object_key"])
                    connection.execute(
                        "DELETE FROM beatit.object_garbage WHERE object_key=%s",
                        (item["object_key"],),
                    )
            except Exception:
                logging.exception("Deferred object deletion will be retried")
                break

    def claim(self):
        """One transaction locks the global inference slot and rotates owners fairly."""
        token = uuid4().hex
        with self.connect() as connection:
            slot = connection.execute(
                "SELECT * FROM beatit.worker_slot WHERE id=1 FOR UPDATE"
            ).fetchone()
            now = self._lease_now(connection)
            if slot["token"] and slot["lease_until"] > now:
                return None
            # A remote cancellation failure reserves capacity until recovery. No
            # new CPU/GPU attempt starts while an orphaned paid call is known.
            outstanding = connection.execute(
                "SELECT DISTINCT claim_token FROM beatit.remote_calls"
            ).fetchall()
            for record in outstanding:
                self._cancel_remote_calls(connection, record["claim_token"])
            if slot["token"]:
                self._cancel_remote_calls(connection, slot["token"])
                self._cancel_local_recovery(slot["job_id"], slot["token"])
                connection.execute(
                    """UPDATE beatit.jobs SET status='failed',error=%s,claim_token=NULL,
                       lease_until=NULL,updated=%s WHERE id=%s AND claim_token=%s AND status='running'""",
                    (
                        "작업 서버 연결이 끊겼습니다. 다시 시도하세요.",
                        now,
                        slot["job_id"],
                        slot["token"],
                    ),
                )
            row = connection.execute(
                """SELECT j.* FROM beatit.jobs j JOIN beatit.owner_schedule s ON s.owner_id=j.owner_id
                   WHERE j.status='queued' ORDER BY s.last_served,j.queued_at,j.id
                   LIMIT 1 FOR UPDATE SKIP LOCKED"""
            ).fetchone()
            if row is None:
                connection.execute(
                    "UPDATE beatit.worker_slot SET token=NULL,job_id=NULL,lease_until=0 WHERE id=1"
                )
                return None
            connection.execute(
                """UPDATE beatit.jobs SET status='running',claim_token=%s,lease_until=%s,
                   attempt=attempt+1,updated=%s WHERE id=%s""",
                (token, now + self.lease_seconds, now, row["id"]),
            )
            connection.execute(
                "UPDATE beatit.owner_schedule SET last_served=%s WHERE owner_id=%s",
                (now, row["owner_id"]),
            )
            connection.execute(
                "UPDATE beatit.worker_slot SET job_id=%s,token=%s,lease_until=%s WHERE id=1",
                (row["id"], token, now + self.lease_seconds),
            )
        return {**self._job(row), "claim_token": token}

    @staticmethod
    def _lease_now(connection):
        return float(
            connection.execute("SELECT UNIX_TIMESTAMP(UTC_TIMESTAMP(6)) AS stamp").fetchone()[
                "stamp"
            ]
        )

    def heartbeat(self, identifier, token, progress=None, log=None):
        with self.connect() as connection:
            slot = connection.execute(
                "SELECT * FROM beatit.worker_slot WHERE id=1 FOR UPDATE"
            ).fetchone()
            now = self._lease_now(connection)
            if slot["token"] != token or slot["lease_until"] <= now:
                return False
            row = connection.execute(
                "SELECT * FROM beatit.jobs WHERE id=%s AND claim_token=%s AND status='running' FOR UPDATE",
                (identifier, token),
            ).fetchone()
            if row is None:
                return False
            payloads = dict(row["payloads"])
            if log is not None:
                payloads["worker.log"] = log[-64000:]
            connection.execute(
                "UPDATE beatit.jobs SET lease_until=%s,progress=%s,payloads=%s,updated=%s WHERE id=%s",
                (
                    now + self.lease_seconds,
                    json.dumps(progress or row["progress"]),
                    json.dumps(payloads),
                    now,
                    identifier,
                ),
            )
            connection.execute(
                "UPDATE beatit.worker_slot SET lease_until=%s WHERE id=1",
                (now + self.lease_seconds,),
            )
        return True

    def finish_claim(self, identifier, token, directory, success=True, error=None):
        """Upload immutable objects first, then publish manifests/result under the lease fence."""
        if success:
            try:
                prepare_playback_previews(directory)
            except Exception as preview_error:
                logging.warning(
                    "Playback preview preparation failed (%s); using original audio",
                    type(preview_error).__name__,
                )
        manifests, payloads = self._capture(identifier, directory)
        result = self._read(directory / "result.json", None)
        progress = self._read(directory / "progress.json", QUEUED)
        log_path = directory / "worker.log"
        if log_path.is_file():
            with log_path.open("rb") as stream:
                stream.seek(max(0, log_path.stat().st_size - 64000))
                payloads["worker.log"] = stream.read().decode("utf-8", errors="replace")
        if success and result is None:
            success, error = False, "작업 결과가 없습니다. 다시 시도하세요."
        if not success and not error:
            error = "작업이 중단되었습니다. 로그를 확인하고 다시 시도하세요."
        with self.connect() as connection:
            slot = connection.execute(
                "SELECT * FROM beatit.worker_slot WHERE id=1 FOR UPDATE"
            ).fetchone()
            if slot["token"] != token or slot["lease_until"] <= self._lease_now(connection):
                return False
            row = connection.execute(
                "SELECT * FROM beatit.jobs WHERE id=%s AND claim_token=%s AND status='running' FOR UPDATE",
                (identifier, token),
            ).fetchone()
            if row is None:
                return False
            merged = {**row["payloads"], **payloads}
            self._write_manifests(connection, identifier, manifests)
            published = result if success else row["result"]
            now = time.time()
            connection.execute(
                """UPDATE beatit.jobs SET status=%s,result=%s,progress=%s,payloads=%s,error=%s,
                   updated=%s,claim_token=NULL,lease_until=NULL WHERE id=%s""",
                (
                    "completed" if success else "failed",
                    json.dumps(published) if published is not None else None,
                    json.dumps(progress),
                    json.dumps(merged),
                    error,
                    now,
                    identifier,
                ),
            )
            if success:
                connection.execute(
                    """INSERT IGNORE INTO beatit.revisions(job_id,generation,result,request,payloads,created)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        identifier,
                        result["generation"],
                        json.dumps(result),
                        json.dumps(row["request"]),
                        json.dumps(merged),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE beatit.projects SET updated=%s WHERE id=%s", (now, row["project_id"])
            )
            connection.execute(
                "UPDATE beatit.worker_slot SET token=NULL,job_id=NULL,lease_until=0 WHERE id=1"
            )
        return True

    def _preserve_legacy_record(self, directory):
        from .modal_worker import CALL_FILE, call_record

        record = call_record(directory)
        legacy = directory / CALL_FILE
        if legacy.is_file() and not record.is_file():
            record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            save_json(record, json.loads(legacy.read_text()))
        if not record.is_file():
            return None
        value = json.loads(record.read_text())
        token = hashlib.sha256(("legacy:" + directory.name).encode()).hexdigest()[:32]
        with self.connect() as connection:
            job = connection.execute(
                "SELECT status,lease_until FROM beatit.jobs WHERE id=%s", (directory.name,)
            ).fetchone()
            if job and job["status"] == "running" and (job["lease_until"] or 0) > time.time():
                return None
            connection.execute(
                "INSERT IGNORE INTO beatit.remote_calls(call_id,job_id,claim_token,created) VALUES (%s,%s,%s,%s)",
                (value["call_id"], directory.name, token, time.time()),
            )
        return token

    def _recover_legacy_records(self):
        from .modal_worker import CALL_DIRECTORY, CALL_FILE, call_record

        directories = {record.parent for record in self.root.glob("*/" + CALL_FILE)}
        directories.update(
            self.root / record.stem for record in (self.root / CALL_DIRECTORY).glob("*.json")
        )
        for directory in directories:
            try:
                self._identifier(directory.name)
                token = self._preserve_legacy_record(directory)
                if token:
                    with self.connect() as connection:
                        self._cancel_remote_calls(connection, token)
                    call_record(directory).unlink(missing_ok=True)
                    (directory / CALL_FILE).unlink(missing_ok=True)
            except Exception:
                logging.exception("Interrupted legacy Modal call remains queued for recovery")
        # DB-only records can outlive both scratch and a deleted job. Only legacy
        # tokens without an active worker slot are eligible in this pass.
        with self.connect() as connection:
            records = connection.execute("SELECT * FROM beatit.remote_calls").fetchall()
        for record in records:
            token = hashlib.sha256(("legacy:" + record["job_id"]).encode()).hexdigest()[:32]
            if record["claim_token"] == token:
                try:
                    with self.connect() as connection:
                        self._cancel_remote_calls(connection, token)
                except Exception:
                    logging.exception("Durable legacy Modal cancellation will be retried")

    def _cancel_remote_calls(self, connection, token):
        from .modal_worker import CALL_FILE, call_record, cancel_remote_call, load_credentials

        records = connection.execute(
            "SELECT * FROM beatit.remote_calls WHERE claim_token=%s FOR UPDATE", (token,)
        ).fetchall()
        if records:
            load_credentials()
            for record in records:
                cancel_remote_call(record["call_id"])
                connection.execute(
                    "DELETE FROM beatit.remote_calls WHERE call_id=%s", (record["call_id"],)
                )
                attempt = self.root / ".attempts" / record["job_id"] / token
                call_record(attempt).unlink(missing_ok=True)
                legacy_token = hashlib.sha256(("legacy:" + record["job_id"]).encode()).hexdigest()[
                    :32
                ]
                if token == legacy_token:
                    legacy = self.directory(record["job_id"])
                    call_record(legacy).unlink(missing_ok=True)
                    (legacy / CALL_FILE).unlink(missing_ok=True)

    def _cancel_local_recovery(self, identifier, token):
        from .modal_worker import CALL_DIRECTORY, cancel_record

        # Only this expired/released attempt is eligible; never cancel another
        # instance's live work while starting a second API process.
        directory = self.root / ".attempts" / identifier / token
        cancel_record(directory)
        # Single-user legacy records are not tied to a new lease. Their old IDs
        # are only considered for the matching job once its lease is being freed.
        legacy = self.root / CALL_DIRECTORY / (identifier + ".json")
        if legacy.is_file() or (self.directory(identifier) / "modal-call.json").is_file():
            cancel_record(self.directory(identifier))

    def release_claim(self, identifier, token, error=None):
        with self.connect() as connection:
            connection.execute("SELECT id FROM beatit.worker_slot WHERE id=1 FOR UPDATE")
            self._cancel_remote_calls(connection, token)
            self._cancel_local_recovery(identifier, token)
            connection.execute(
                """UPDATE beatit.jobs SET status='failed',error=%s,updated=%s,
                   claim_token=NULL,lease_until=NULL WHERE id=%s AND claim_token=%s AND status='running'""",
                (
                    error or "작업이 중단되었습니다. 다시 시도하세요.",
                    time.time(),
                    identifier,
                    token,
                ),
            )
            connection.execute(
                "UPDATE beatit.worker_slot SET token=NULL,job_id=NULL,lease_until=0 WHERE id=1 AND token=%s",
                (token,),
            )

    def update(self, identifier, status, error=None):
        """Explicit operator/test checkpoint; never called by public API or queue decisions."""
        directory = self.directory(identifier)
        manifests, payloads = self._capture(identifier, directory)
        with self.connect() as connection:
            row = self._owned(connection, identifier, lock=True)
            if row["claim_token"]:
                raise ValueError("Cannot checkpoint an active leased job")
            result = self._read(directory / "result.json", row["result"])
            progress = self._read(directory / "progress.json", row["progress"])
            request = self._read(directory / "request.json", row["request"])
            self._write_manifests(connection, identifier, manifests)
            connection.execute(
                """UPDATE beatit.jobs SET status=%s,error=%s,result=%s,progress=%s,request=%s,
                   payloads=%s,updated=%s WHERE id=%s""",
                (
                    status,
                    error,
                    json.dumps(result) if result is not None else None,
                    json.dumps(progress),
                    json.dumps(request),
                    json.dumps({**row["payloads"], **payloads}),
                    time.time(),
                    identifier,
                ),
            )

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

    def _run_claim(self, claim):
        identifier, token = claim["id"], claim["claim_token"]
        directory = self.root / ".attempts" / identifier / token
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def keepalive():
            while not heartbeat_stop.wait(3):
                try:
                    log_path = directory / "worker.log"
                    text = None
                    if log_path.is_file():
                        with log_path.open("rb") as stream:
                            stream.seek(max(0, log_path.stat().st_size - 64000))
                            text = stream.read().decode("utf-8", errors="replace")
                    if self.heartbeat(
                        identifier, token, self._read(directory / "progress.json", None), text
                    ):
                        continue
                except Exception:
                    logging.exception("Job lease heartbeat failed; stopping inference")
                lease_lost.set()
                with self.lock:
                    if self.process and self.process.poll() is None:
                        self._terminate(self.process)
                return

        heartbeat_thread = threading.Thread(target=keepalive, daemon=True)
        heartbeat_thread.start()
        try:
            self.restore(identifier, directory)
            if lease_lost.is_set() or self.stopping.is_set():
                return
            with (directory / "worker.log").open("a") as log:
                with self.lock:
                    self.current = identifier
                    if lease_lost.is_set():
                        return
                    self.process = subprocess.Popen(
                        [sys.executable, "-u", "-m", "transcription.worker", str(directory)],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env={
                            **os.environ,
                            "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                            "BEATIT_JOB_ID": identifier,
                            "BEATIT_PARENT_PID": str(os.getpid()),
                            "BEATIT_CLAIM_TOKEN": token,
                            "BEATIT_DATABASE_URL": self.db.url,
                            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "4"),
                        },
                    )
                self.process.wait()
            if not lease_lost.is_set() and not self.stopping.is_set():
                error = self._read(directory / "error.json", {}).get("message")
                self.finish_claim(identifier, token, directory, self.process.returncode == 0, error)
        except Exception as error:
            logging.exception("Job failed; previous published revision remains available")
            try:
                self.release_claim(identifier, token, str(error))
            except Exception:
                logging.exception("Unable to release job; lease expiry will recover it")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=12)
            with self.lock:
                if self.process and self.process.poll() is None:
                    self._terminate(self.process)
                self.current = self.process = None
            try:
                self.release_claim(identifier, token)
            except Exception:
                logging.exception("Unable to release job; lease expiry will recover it")
            from .modal_worker import call_record

            if not call_record(directory).exists():
                shutil.rmtree(directory, ignore_errors=True)

    def _loop(self):
        while not self.stopping.is_set():
            try:
                claim = self.claim()
                if claim:
                    self._run_claim(claim)
                else:
                    self.collect_garbage()
                    self.stopping.wait(0.8)
            except Exception:
                logging.exception("Database queue unavailable; retrying without changing ownership")
                self.stopping.wait(3)
