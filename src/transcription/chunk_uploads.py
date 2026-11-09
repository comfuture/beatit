"""Owner-scoped, bounded upload staging on the single application host.

Only complete inputs enter the existing MySQL/R2 publication path. Atomic offsets
and OS locks make chunk acknowledgements retryable across process restarts.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from uuid import uuid4

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .pipeline import save_json

CHUNK_BYTES = 8 * 1024**2
UPLOAD_TTL = 24 * 60 * 60
MAX_ACTIVE = 8
MAX_PER_OWNER = 3


class ChunkUploads:
    def __init__(self, store):
        self.store = store
        self.root = store.root / ".uploads"

    def directory(self, owner, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise HTTPException(404, "업로드를 찾을 수 없습니다.")
        return self.root / hashlib.sha256(owner.encode()).hexdigest() / identifier

    @staticmethod
    def read(directory):
        try:
            return json.loads((directory / "upload.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raise HTTPException(404, "업로드가 만료되었습니다. 파일을 다시 선택하세요.") from None

    @contextmanager
    def locked(self, directory):
        try:
            lock = (directory / "lock").open("rb")
        except FileNotFoundError:
            raise HTTPException(404, "업로드를 찾을 수 없습니다.") from None
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(
                    409, "이 업로드를 처리 중입니다. 잠시 후 다시 시도하세요."
                ) from None
            yield

    def status(self, owner, identifier):
        metadata = self.read(self.directory(owner, identifier))
        if metadata["expires"] <= time.time():
            raise HTTPException(404, "업로드가 만료되었습니다. 파일을 다시 선택하세요.")
        return metadata

    def create(self, owner, filename, size, options, project_id):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "create.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.cleanup()
            active = []
            for path in self.root.glob("*/*/upload.json"):
                try:
                    metadata = self.read(path.parent)
                except HTTPException as error:
                    if error.status_code == 404:
                        continue  # A concurrent cancellation/collector removed this session.
                    raise
                if metadata["state"] != "completed":
                    active.append(metadata)
            if (
                len(active) >= MAX_ACTIVE
                or sum(m["owner"] == owner for m in active) >= MAX_PER_OWNER
            ):
                raise HTTPException(
                    429, "진행 중인 업로드가 많습니다. 완료하거나 취소한 뒤 다시 시도하세요."
                )
            reserved = sum(m["size"] - m["offset"] for m in active)
            if size + reserved + 512 * 1024**2 > shutil.disk_usage(self.root).free:
                raise HTTPException(503, "업로드 저장 공간이 부족합니다. 잠시 후 다시 시도하세요.")
            identifier = uuid4().hex
            directory = self.directory(owner, identifier)
            directory.mkdir(parents=True)
            (directory / "lock").touch()
            (directory / "input").touch()
            metadata = {
                "id": identifier,
                "owner": owner,
                "filename": filename,
                "size": size,
                "offset": 0,
                "chunk_size": CHUNK_BYTES,
                "options": options,
                "project_id": project_id,
                "state": "uploading",
                "expires": time.time() + UPLOAD_TTL,
            }
            save_json(directory / "upload.json", metadata)
            return metadata

    async def receive(self, owner, identifier, offset, request):
        directory = self.directory(owner, identifier)
        with self.locked(directory):
            metadata = self.status(owner, identifier)
            if metadata["state"] != "uploading":
                raise HTTPException(409, "파일 전송이 이미 끝났습니다.")
            expected = min(CHUNK_BYTES, metadata["size"] - offset)
            if offset > metadata["offset"] or expected <= 0:
                raise HTTPException(409, "전송 위치가 다릅니다. 업로드 상태를 다시 확인하세요.")
            length = request.headers.get("content-length")
            if length is not None and length != str(expected):
                raise HTTPException(413, "업로드 조각의 크기가 올바르지 않습니다.")
            # Never append an unvalidated or disconnected request to the input.
            with tempfile.TemporaryFile(dir=directory) as chunk:
                received = 0
                async for data in request.stream():
                    received += len(data)
                    if received > expected:
                        raise HTTPException(413, "업로드 조각의 크기가 올바르지 않습니다.")
                    await run_in_threadpool(chunk.write, data)
                if received != expected:
                    raise HTTPException(400, "업로드 조각이 완전히 전송되지 않았습니다.")
                await run_in_threadpool(self.append, directory, metadata, offset, chunk)
            return metadata

    async def receive_direct(self, owner, identifier, request):
        """Receive one complete request without an additional whole-file spool.

        A failed receive never advances the acknowledged offset. Existing complete
        bytes are only compared on retries, making a lost response harmless.
        """
        directory = self.directory(owner, identifier)
        with self.locked(directory):
            metadata = self.status(owner, identifier)
            if metadata["state"] != "uploading":
                raise HTTPException(409, "파일 전송이 이미 끝났습니다.")
            if metadata["offset"] not in {0, metadata["size"]}:
                raise HTTPException(409, "전송한 조각이 있습니다. 기존 위치에서 이어서 전송하세요.")
            expected = metadata["size"]
            length = request.headers.get("content-length")
            if length is not None and length != str(expected):
                raise HTTPException(413, "업로드한 파일의 크기가 올바르지 않습니다.")
            duplicate = metadata["offset"] == expected
            with (directory / "input").open("r+b") as destination:
                if not duplicate:
                    destination.truncate(0)
                received = 0
                try:
                    async for data in request.stream():
                        received += len(data)
                        if received > expected:
                            raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
                        if duplicate:
                            existing = await run_in_threadpool(destination.read, len(data))
                            if existing != data:
                                raise HTTPException(409, "이미 전송한 파일과 내용이 다릅니다.")
                        else:
                            await run_in_threadpool(destination.write, data)
                    if received != expected:
                        raise HTTPException(400, "파일이 완전히 전송되지 않았습니다.")
                    if not duplicate:
                        await run_in_threadpool(self.flush, destination)
                except BaseException as error:
                    if not duplicate:
                        destination.truncate(0)
                    if isinstance(error, ClientDisconnect):
                        raise HTTPException(400, "파일 전송이 중단되었습니다.") from None
                    raise
            metadata["offset"] = expected
            metadata["expires"] = time.time() + UPLOAD_TTL
            save_json(directory / "upload.json", metadata)
            return metadata

    @staticmethod
    def flush(destination):
        destination.flush()
        os.fsync(destination.fileno())

    @staticmethod
    def append(directory, metadata, offset, chunk):
        chunk.seek(0)
        with (directory / "input").open("r+b") as destination:
            if offset < metadata["offset"]:
                # A lost acknowledgement may resend the same chunk, never new bytes.
                destination.seek(offset)
                while data := chunk.read(1024**2):
                    if destination.read(len(data)) != data:
                        raise HTTPException(409, "이미 전송한 조각과 내용이 다릅니다.")
                return
            # Discard a tail left by a crash before its offset was committed.
            destination.truncate(offset)
            destination.seek(offset)
            shutil.copyfileobj(chunk, destination, 1024**2)
            destination.flush()
            os.fsync(destination.fileno())
            metadata["offset"] = destination.tell()
        metadata["expires"] = time.time() + UPLOAD_TTL
        save_json(directory / "upload.json", metadata)

    def prepare_finish(self, owner, identifier):
        directory = self.directory(owner, identifier)
        with self.locked(directory):
            metadata = self.status(owner, identifier)
            if metadata["offset"] != metadata["size"]:
                raise HTTPException(409, "파일 전송이 아직 끝나지 않았습니다.")
            if metadata["state"] != "completed":
                metadata["state"] = "finalizing"
                metadata.pop("error", None)
                metadata["expires"] = time.time() + UPLOAD_TTL
                save_json(directory / "upload.json", metadata)
            return metadata

    def finish(self, owner, identifier):
        directory = self.directory(owner, identifier)
        try:
            with self.locked(directory):
                metadata = self.status(owner, identifier)
                if metadata["state"] != "finalizing":
                    return
                try:
                    try:
                        self.store.get(identifier, owner_id=owner)
                    except KeyError:
                        target = self.store.directory(identifier)
                        target.mkdir(exist_ok=True)
                        if not (target / "input").exists():
                            os.link(directory / "input", target / "input")
                        save_json(
                            target / "request.json",
                            {
                                "filename": metadata["filename"],
                                "options": metadata["options"],
                            },
                        )
                        self.store.add(
                            identifier,
                            metadata["filename"],
                            owner_id=owner,
                            project_id=metadata["project_id"],
                        )
                    metadata["state"] = "completed"
                    save_json(directory / "upload.json", metadata)
                    (directory / "input").unlink(missing_ok=True)
                except Exception as error:
                    logging.exception("Chunked upload publication will remain retryable")
                    metadata["state"] = "failed"
                    metadata["error"] = (
                        str(error)
                        if isinstance(error, ValueError)
                        else "파일을 저장하지 못했습니다. 분석 시작을 눌러 다시 시도하세요."
                    )
                    save_json(directory / "upload.json", metadata)
        except HTTPException as error:
            if error.status_code not in {404, 409}:
                raise

    def cancel(self, owner, identifier):
        directory = self.directory(owner, identifier)
        with self.locked(directory):
            metadata = self.status(owner, identifier)
            if metadata["state"] == "finalizing":
                raise HTTPException(409, "파일을 저장 중입니다. 완료 후 작업을 취소하세요.")
            if metadata["state"] != "completed":
                # A failed publication may have left an unpublished local hard link.
                try:
                    self.store.get(identifier, owner_id=owner)
                except KeyError:
                    shutil.rmtree(self.store.directory(identifier), ignore_errors=True)
            shutil.rmtree(directory)

    def cleanup(self):
        for path in self.root.glob("*/*/upload.json"):
            try:
                with self.locked(path.parent):
                    metadata = self.read(path.parent)
                    if metadata["expires"] > time.time():
                        continue
                    try:
                        self.store.get(metadata["id"], owner_id=metadata["owner"])
                    except KeyError:
                        shutil.rmtree(self.store.directory(metadata["id"]), ignore_errors=True)
                    shutil.rmtree(path.parent)
            except HTTPException as error:
                if error.status_code not in {404, 409}:
                    raise
