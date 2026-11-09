"""Real HTTP streaming checks against disposable MySQL and private object storage."""

import asyncio
import hashlib
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from transcription import events as event_module
from transcription.chunk_uploads import ChunkUploads
from transcription.object_storage import file_digest

pytestmark = pytest.mark.mysql


@contextmanager
def running_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="on", timeout_graceful_shutdown=0.2)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Test HTTP server did not start")
            time.sleep(0.01)
        yield address
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def read_event(lines):
    name = None
    data = None
    for line in lines:
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif not line and name:
            return name, data
    raise AssertionError("Event stream ended without an event")


def test_real_stream_snapshot_owner_checks_job_changes_and_session_revocation(
    tmp_path,
    monkeypatch,
    authenticated_app,
):
    monkeypatch.setattr(event_module, "SAMPLE_SECONDS", 0.03)
    monkeypatch.setattr(event_module, "AUTH_SECONDS", 0.05)
    app = authenticated_app(tmp_path)
    app.state.events.system_factory = lambda: {"ffmpeg": True, "upload_mode": "direct"}
    other_token, _ = app.state.auth.create_session({"sub": "owner-b", "email": "b@example.test"})
    with running_server(app) as address:
        with httpx.Client(base_url=address, cookies=app.state.test_cookie, timeout=3) as owner:
            job = owner.post("/api/jobs", files={"file": ("private.wav", b"audio")}).json()
            assert httpx.get(address + "/api/events").status_code == 401
            intruder = httpx.get(
                address + "/api/events",
                params={"job_id": job["id"]},
                cookies={app.state.auth.settings.cookie_name: other_token},
            )
            assert intruder.status_code == 404
            with owner.stream("GET", "/api/events", params={"job_id": job["id"]}) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["x-accel-buffering"] == "no"
                lines = response.iter_lines()
                name, snapshot = read_event(lines)
                assert name == "snapshot"
                assert [item["id"] for item in snapshot["jobs"]] == [job["id"]]
                assert snapshot["system"]["upload_mode"] == "direct"
                assert owner.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
                name, update = read_event(lines)
                assert name == "job" and update["job"]["status"] == "cancelled"
                assert update["job"]["owner_id"] == app.state.test_user["id"]
                assert owner.post("/api/auth/logout").status_code == 204
                assert read_event(lines)[0] == "session-expired"
            assert owner.get("/api/events").status_code == 401


def test_selected_job_outside_history_remains_owner_scoped_and_pinned(
    tmp_path,
    authenticated_app,
):
    app = authenticated_app(tmp_path)
    store = app.state.store
    owner = app.state.test_user["id"]
    project = store.create_project(owner, "History")
    with store.connect() as connection:
        for index in range(105):
            connection.execute(
                """INSERT INTO beatit.jobs
                (id,project_id,owner_id,filename,status,created,updated,queued_at,
                 request,progress,payloads,result)
                VALUES (%s,%s,%s,%s,'completed',%s,%s,%s,'{}','{}','{}',%s)""",
                (
                    f"{index:032x}",
                    project["id"],
                    owner,
                    "recording.wav",
                    index,
                    index,
                    index,
                    json.dumps({"generation": "render-1"}),
                ),
            )
    summaries = app.state.events._read(owner, {"0" * 32})
    assert len(summaries) == 101
    assert "0" * 32 in summaries
    assert app.state.events._read("another-owner", {"0" * 32}) == {}


def test_live_event_connection_does_not_prevent_bounded_server_shutdown(
    tmp_path,
    authenticated_app,
):
    app = authenticated_app(tmp_path)
    app.state.events.system_factory = lambda: {"ffmpeg": True}
    client = None
    response = None
    try:
        with running_server(app) as address:
            client = httpx.Client(base_url=address, cookies=app.state.test_cookie, timeout=3)
            response = client.send(client.build_request("GET", "/api/events"), stream=True)
            assert read_event(response.iter_lines())[0] == "snapshot"
            assert len(app.state.events.owners) == 1
            started = time.monotonic()
        assert time.monotonic() - started < 3
        assert not app.state.events.owners
        assert app.state.events.task is None
    finally:
        if response:
            response.close()
        if client:
            client.close()


def test_direct_upload_size_retry_ownership_and_disconnect_rollback(
    tmp_path,
    monkeypatch,
    authenticated_app,
    authenticated_client,
):
    monkeypatch.setenv("BEATIT_UPLOAD_MODE", "direct")
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        session = owner.post("/api/uploads", json={"filename": "direct.mp4", "size": 8}).json()
        path = "/api/uploads/" + session["id"]
        assert owner.get("/api/system").json()["upload_mode"] == "direct"
        assert owner.put(path, content=b"short").status_code == 413
        uploads = ChunkUploads(app.state.store)
        messages = [
            {"type": "http.request", "body": b"half", "more_body": True},
            {"type": "http.disconnect"},
        ]

        async def receive():
            return messages.pop(0)

        request = Request({"type": "http", "headers": []}, receive=receive)
        with pytest.raises(HTTPException) as interrupted:
            asyncio.run(uploads.receive_direct(app.state.test_user["id"], session["id"], request))
        assert interrupted.value.status_code == 400
        assert owner.get(path).json()["offset"] == 0
        directory = uploads.directory(app.state.test_user["id"], session["id"])
        assert (directory / "input").stat().st_size == 0
        assert owner.put(path, content=b"12345678").json()["offset"] == 8
        assert owner.put(path, content=b"12345678").json()["offset"] == 8
        assert owner.put(path, content=b"XXXXXXXX").status_code == 409
        token, _ = app.state.auth.create_session({"sub": "other", "email": "b@example.test"})
        with TestClient(app, cookies={app.state.auth.settings.cookie_name: token}) as other:
            assert other.put(path, content=b"XXXXXXXX").status_code == 404
        assert TestClient(app).put(path, content=b"XXXXXXXX").status_code == 401
        assert owner.post(path + "/complete").status_code == 202
        assert owner.get(path).json()["state"] == "completed"
        assert len(owner.get("/api/jobs").json()) == 1


def test_exact_305_mib_video_arrives_in_one_streamed_http_put(
    tmp_path,
    monkeypatch,
    authenticated_app,
):
    monkeypatch.setenv("BEATIT_UPLOAD_MODE", "direct")
    total = 320415531
    block = b"direct origin upload\0" * (8 * 1024**2 // 21 + 1)
    digest = hashlib.sha256()

    def source():
        for offset in range(0, total, 8 * 1024**2):
            chunk = block[: min(8 * 1024**2, total - offset)]
            digest.update(chunk)
            yield chunk

    app = authenticated_app(tmp_path)
    with running_server(app) as address:
        with httpx.Client(base_url=address, cookies=app.state.test_cookie, timeout=30) as owner:
            session = owner.post(
                "/api/uploads", json={"filename": "large.mp4", "size": total}
            ).json()
            path = "/api/uploads/" + session["id"]
            response = owner.put(path, content=source())
            assert response.status_code == 200, response.text
            assert response.json()["offset"] == total
            assert owner.post(path + "/complete").status_code == 202
            deadline = time.monotonic() + 15
            while owner.get(path).json()["state"] != "completed":
                assert time.monotonic() < deadline
                time.sleep(0.02)
            source_path = app.state.store.directory(session["id"]) / "input"
            assert source_path.stat().st_size == total
            assert file_digest(source_path) == digest.hexdigest()
