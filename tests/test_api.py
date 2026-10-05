import json

from fastapi.testclient import TestClient

from transcription.app import create_app


def test_upload_history_cancel_and_delete(tmp_path):
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("../../song.wav", b"audio", "audio/wav")},
            data={"options": '{"device":"cpu"}'},
        )
        assert response.status_code == 202
        job = response.json()
        assert job["filename"] == "song.wav"
        identifier = job["id"]
        assert (tmp_path / identifier / "input").read_bytes() == b"audio"
        assert len(client.get("/api/jobs").json()) == 1
        assert client.get(f"/api/jobs/{identifier}/files/jobs.sqlite3").status_code == 404
        assert client.post(f"/api/jobs/{identifier}/cancel").json()["status"] == "cancelled"
        assert client.delete(f"/api/jobs/{identifier}").status_code == 204
        assert not (tmp_path / identifier).exists()
        assert client.get(f"/api/jobs/{identifier}").status_code == 404


def test_invalid_inputs_leave_no_orphan_uploads(tmp_path):
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        assert client.post("/api/jobs", files={"file": ("song.exe", b"x")}).status_code == 415
        assert client.post("/api/jobs", files={"file": ("song.wav", b"")}).status_code == 400
        assert (
            client.post(
                "/api/jobs", files={"file": ("song.wav", b"x")}, data={"options": '{"bpm":0}'}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs", files={"file": ("song.wav", b"x")}, data={"options": '{"bpm":NaN}'}
            ).status_code
            == 422
        )
        assert client.get("/api/jobs").json() == []
        assert not any(p.is_dir() for p in tmp_path.iterdir())


def test_browser_cross_origin_cannot_submit_local_jobs(tmp_path):
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        response = client.post(
            "/api/jobs",
            headers={"origin": "https://example.com"},
            files={"file": ("song.wav", b"x")},
        )
        assert response.status_code == 403
        assert client.get("/api/jobs").json() == []


def test_revision_is_queued_and_validates_audio_bounds(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        job = client.post("/api/jobs", files={"file": ("song.wav", b"x")}).json()
        identifier = job["id"]
        directory = tmp_path / identifier
        (directory / "result.json").write_text(
            json.dumps({"duration": 5, "generation": "render-1", "pages": []})
        )
        app.state.store.update(identifier, "completed")
        invalid = {"options": {"bpm": 90}, "events": [{"time": 6, "pitch": 35}]}
        assert client.post(f"/api/jobs/{identifier}/score", json=invalid).status_code == 422
        valid = {"options": {"bpm": 90}, "events": [{"time": 1, "pitch": 38}]}
        assert client.post(f"/api/jobs/{identifier}/score", json=valid).status_code == 202
        request = json.loads((directory / "request.json").read_text())
        assert request["revision"]
        assert request["events"][0]["pitch"] == 38


def test_retry_after_engraving_failure_reuses_inference(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        identifier = client.post("/api/jobs", files={"file": ("song.wav", b"x")}).json()["id"]
        directory = tmp_path / identifier
        (directory / "analysis.json").write_text("{}")
        (directory / "events.json").write_text("[]")
        (directory / "progress.json").write_text('{"stage":"engraving"}')
        app.state.store.update(identifier, "failed")
        assert client.post(f"/api/jobs/{identifier}/retry").status_code == 202
        assert json.loads((directory / "request.json").read_text())["revision"]
