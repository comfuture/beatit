"""Exercise chunk boundaries, retries and owner isolation against real MySQL."""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from transcription.chunk_uploads import ChunkUploads

pytestmark = pytest.mark.mysql


@pytest.fixture
def chunks(tmp_path, monkeypatch, authenticated_app, authenticated_client):
    monkeypatch.setattr("transcription.chunk_uploads.CHUNK_BYTES", 8)
    monkeypatch.setattr("transcription.app.CHUNK_BYTES", 8)
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        yield app, client, ChunkUploads(app.state.store)


def start(client, data=b"12345678abcdefghEND", **kwargs):
    response = client.post(
        "/api/uploads", json={"filename": "song.mp4", "size": len(data), **kwargs}
    )
    assert response.status_code == 201, response.text
    return "/api/uploads/" + response.json()["id"]


def send(client, path, offset, data):
    return client.patch(path, content=data, headers={"Upload-Offset": str(offset)})


def test_chunk_retry_restart_tail_and_exactly_once_publication(chunks):
    app, client, uploads = chunks
    data = b"12345678abcdefghEND"
    path = start(client, data)
    identifier = path.rsplit("/", 1)[-1]
    assert client.post(path + "/complete").status_code == 409
    assert send(client, path, 8, data[8:16]).status_code == 409
    assert send(client, path, 0, data[:8]).json()["offset"] == 8
    assert send(client, path, 0, data[:8]).json()["offset"] == 8
    assert send(client, path, 0, b"xxxxxxxx").status_code == 409
    directory = uploads.directory(app.state.test_user["id"], identifier)
    with (directory / "input").open("ab") as file:
        file.write(b"interrupted unacknowledged tail")
    # Reconstruct staging after restart; only the atomically committed offset counts.
    recovered = ChunkUploads(app.state.store)
    assert recovered.status(app.state.test_user["id"], identifier)["offset"] == 8
    assert send(client, path, 8, data[8:16]).json()["offset"] == 16
    assert send(client, path, 16, data[16:]).json()["offset"] == len(data)
    assert client.get("/api/jobs").json() == []
    assert client.post(path + "/complete").status_code == 202
    assert client.get(path).json()["state"] == "completed"
    assert client.post(path + "/complete").json()["state"] == "completed"
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["id"] == identifier
    source = app.state.store.directory(identifier) / "input"
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(data).digest()
    assert not (directory / "input").exists()
    assert client.get(f"/api/jobs/{identifier}").json()["status"] == "queued"


def test_chunk_size_validation_disconnect_and_busy_lock(chunks):
    app, client, uploads = chunks
    path = start(client)
    identifier = path.rsplit("/", 1)[-1]
    for offset, data, expected in (
        (-1, b"x", 422),
        (1, b"x", 409),
        (0, b"short", 413),
        (0, b"far too long", 413),
    ):
        assert send(client, path, offset, data).status_code == expected
    response = client.patch(path, content=iter([b"short"]), headers={"Upload-Offset": "0"})
    assert response.status_code == 400
    assert client.get(path).json()["offset"] == 0
    directory = uploads.directory(app.state.test_user["id"], identifier)
    with uploads.locked(directory):
        assert send(client, path, 0, b"12345678").status_code == 409
        assert client.delete(path).status_code == 409
    assert send(client, path, 0, b"12345678").status_code == 200
    assert (directory / "input").read_bytes() == b"12345678"


def test_chunk_routes_require_the_current_owner(chunks):
    app, client, uploads = chunks
    path = start(client)
    token, _ = app.state.auth.create_session({"sub": "other", "email": "b@example.test"})
    with TestClient(app, cookies={app.state.auth.settings.cookie_name: token}) as other:
        anonymous = TestClient(app)
        try:
            for method, suffix, kwargs in (
                ("GET", "", {}),
                ("DELETE", "", {}),
                ("POST", "/complete", {}),
                ("PATCH", "", {"content": b"12345678", "headers": {"Upload-Offset": "0"}}),
            ):
                assert other.request(method, path + suffix, **kwargs).status_code == 404
                assert anonymous.request(method, path + suffix, **kwargs).status_code == 401
            assert (
                anonymous.post("/api/uploads", json={"filename": "x.wav", "size": 1}).status_code
                == 401
            )
        finally:
            anonymous.close()
    assert client.get(path).json()["offset"] == 0
    assert (
        client.post(
            "/api/uploads",
            headers={"Origin": "https://evil.test"},
            json={"filename": "x.wav", "size": 1},
        ).status_code
        == 403
    )


def test_upload_admission_expiry_cancellation_and_validation(chunks, monkeypatch):
    app, client, uploads = chunks
    for payload, status in (
        ({"filename": "x.exe", "size": 1}, 415),
        ({"filename": "x.wav", "size": 0}, 422),
        ({"filename": "x.wav", "size": 2**31}, 413),
        ({"filename": "x.wav", "size": 1, "project_id": "missing"}, 404),
    ):
        assert client.post("/api/uploads", json=payload).status_code == status
    paths = [start(client) for _ in range(3)]
    assert client.post("/api/uploads", json={"filename": "x.wav", "size": 1}).status_code == 429
    assert client.delete(paths[0]).status_code == 204
    assert client.get(paths[0]).status_code == 404
    identifier = paths[1].rsplit("/", 1)[-1]
    directory = uploads.directory(app.state.test_user["id"], identifier)
    metadata = json.loads((directory / "upload.json").read_text())
    metadata["expires"] = time.time() - 1
    (directory / "upload.json").write_text(json.dumps(metadata))
    assert client.get(paths[1]).status_code == 404
    with uploads.locked(directory):
        uploads.cleanup()
        assert directory.exists()
    uploads.cleanup()
    assert not directory.exists()
    monkeypatch.setenv("DRUM_EXECUTION_BACKEND", "modal")
    assert (
        client.post(
            "/api/uploads", json={"filename": "x.wav", "size": 1, "options": {"device": "cpu"}}
        ).status_code
        == 422
    )


def test_failed_or_interrupted_publication_can_resume(chunks, disk_objects, monkeypatch):
    app, client, uploads = chunks
    path = start(client, b"12345678")
    identifier = path.rsplit("/", 1)[-1]
    assert send(client, path, 0, b"12345678").status_code == 200
    disk_objects.fail_writes = True
    assert client.post(path + "/complete").status_code == 202
    assert client.get(path).json()["state"] == "failed"
    assert client.get("/api/jobs").json() == []
    disk_objects.fail_writes = False
    # Simulate a process stopping after recording finalization, before background work.
    uploads.prepare_finish(app.state.test_user["id"], identifier)
    assert client.get(path).json()["state"] == "finalizing"
    assert client.get(path).json()["state"] == "completed"
    assert len(client.get("/api/jobs").json()) == 1


def test_unknown_commit_outcome_never_publishes_twice(chunks, monkeypatch):
    app, client, uploads = chunks
    path = start(client, b"12345678")
    assert send(client, path, 0, b"12345678").status_code == 200
    original = app.state.store.add

    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("acknowledgement lost after SQL commit")

    monkeypatch.setattr(app.state.store, "add", lost_ack)
    assert client.post(path + "/complete").status_code == 202
    assert client.get(path).json()["state"] == "failed"
    assert client.post(path + "/complete").status_code == 202
    assert client.get(path).json()["state"] == "completed"
    assert len(client.get("/api/jobs").json()) == 1


def test_305_mib_video_is_published_using_only_8_mib_requests(
    tmp_path, authenticated_app, authenticated_client
):
    from transcription.chunk_uploads import CHUNK_BYTES
    from transcription.object_storage import file_digest

    total = 320415531  # Exact byte count of the video that stalled through the proxy.
    pattern = b"beatit upload regression\0"
    block = pattern * (CHUNK_BYTES // len(pattern) + 1)
    digest = hashlib.sha256()
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        response = client.post("/api/uploads", json={"filename": "large.mp4", "size": total})
        assert response.status_code == 201
        identifier = response.json()["id"]
        path = "/api/uploads/" + identifier
        for offset in range(0, total, CHUNK_BYTES):
            chunk = block[: min(CHUNK_BYTES, total - offset)]
            digest.update(chunk)
            response = send(client, path, offset, chunk)
            assert response.status_code == 200, response.text
            assert response.json()["offset"] == offset + len(chunk)
        assert client.post(path + "/complete").status_code == 202
        assert client.get(path).json()["state"] == "completed"
        source = app.state.store.directory(identifier) / "input"
        assert source.stat().st_size == total
        assert file_digest(source) == digest.hexdigest()
