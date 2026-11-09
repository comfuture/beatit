"""Authenticated route isolation and durable API recovery against real MySQL."""

import shutil
import zipfile
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.models import Options
from transcription.pipeline import save_json
from transcription.store import JobStore

pytestmark = pytest.mark.mysql


def upload(client, filename="A-private.wav", project_id=None):
    response = client.post(
        "/api/jobs",
        files={"file": (filename, b"source recording", "audio/wav")},
        data={"project_id": project_id} if project_id else {},
    )
    assert response.status_code == 202, response.text
    return response.json()


def publish_sample(store, identifier):
    claim = store.claim()
    assert claim["id"] == identifier
    directory = store.restore(identifier, store.root / ".fixture" / uuid4().hex)
    generation = "render-1"
    render = directory / generation
    render.mkdir()
    events = [{"time": 0.5, "pitch": 38, "strength": 0.8, "velocity": 100}]
    analysis = {"duration": 6, "estimated_bpm": 120, "tempo_reliable": True}
    options = Options(bpm=120, offset=0).model_dump()
    staff = "".join(
        f'<polyline class="StaffLines" points="100,{y} 900,{y}"/>'
        for y in (200, 220, 240, 260, 280)
    )
    bars = "".join(f'<polyline class="BarLine" points="{x},198 {x},282"/>' for x in (350, 650, 900))
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1400">{bars}{staff}</svg>'
    (render / "score.svg").write_text(svg)
    (render / "score.musicxml").write_text(
        '<score-partwise version="4.0"><work><work-title>A private score</work-title></work></score-partwise>'
    )
    for name, payload in (("events.json", events), ("analysis.json", analysis)):
        save_json(directory / name, payload)
        save_json(render / name, payload)
    (render / "score.mid").write_bytes(b"MThd fixture score")
    (render / "performance.mid").write_bytes(b"MThd fixture performance")
    (render / "score.pdf").write_bytes(b"%PDF fixture")
    (render / "engraver.log").write_text("private engraving log")
    save_json(render / "tom-refinement.json", {"enabled": True})
    with zipfile.ZipFile(render / "score-bundle.zip", "w") as bundle:
        bundle.write(render / "score.musicxml", "score.musicxml")
        bundle.write(render / "events.json", "events.json")
    sf.write(directory / "audio.wav", np.zeros(48000, dtype=np.float32), 8000)
    sf.write(directory / "drums.wav", np.zeros(48000, dtype=np.float32), 8000)
    for name in ("kick", "snare", "toms", "hh", "cymbals"):
        sf.write(directory / f"stem-{name}.flac", np.zeros(8000, dtype=np.float32), 8000)
    (directory / "worker.log").write_text("private worker log for owner A")
    result = {
        "generation": generation,
        "options": options,
        "duration": 6,
        "renderer": "musescore",
        "pages": ["score.svg"],
        "pdf": True,
        "grid": {"bars": 3, "bpm": 120, "offset": 0, "meter": "4/4", "bar_ticks": 48, "step": 3},
        "event_count": 1,
        "counts": {"Snare": 1},
        "warnings": [],
        "metadata": {"title": "A private score", "author": "Owner A"},
        "beat_review": {"grid_key": "private", "beats": []},
    }
    save_json(directory / "result.json", result)
    save_json(
        directory / "progress.json", {"stage": "complete", "progress": 1, "message": "Completed"}
    )
    assert store.finish_claim(identifier, claim["claim_token"], directory)
    return result


def other_client(app):
    token, user = app.state.auth.create_session(
        {
            "sub": "test-google-owner-B",
            "email": "b@example.test",
            "name": "Owner B",
        }
    )
    return TestClient(app, cookies={app.state.auth.settings.cookie_name: token}), user


def request_cases(identifier, project_id, generation):
    prefix = f"/api/jobs/{identifier}"
    cases = [
        ("GET", prefix, {}),
        ("GET", prefix + "/log", {}),
        ("GET", prefix + "/waveform", {}),
        ("GET", prefix + "/waveform?source=stem-kick.flac", {}),
        ("GET", prefix + f"/score-layout?generation={generation}", {}),
        ("POST", prefix + "/score", {"json": {"options": {}, "events": []}}),
        ("POST", prefix + "/score-metadata", {"json": {"title": "Intrusion", "author": "B"}}),
        ("POST", prefix + "/cancel", {}),
        ("POST", prefix + "/retry", {}),
        ("DELETE", prefix, {}),
        ("GET", f"/api/projects/{project_id}", {}),
        ("GET", f"/api/jobs?project_id={project_id}", {}),
        (
            "POST",
            "/api/jobs",
            {"files": {"file": ("B.wav", b"recording")}, "data": {"project_id": project_id}},
        ),
    ]
    for name in (
        "audio.wav",
        "drums.wav",
        "stem-kick.flac",
        "stem-snare.flac",
        "stem-toms.flac",
        "stem-hh.flac",
        "stem-cymbals.flac",
        "events.json",
        "analysis.json",
        "score.musicxml",
        "score.mid",
        "performance.mid",
        "score.pdf",
        "score-bundle.zip",
        "score.svg",
        "engraver.log",
        "tom-refinement.json",
    ):
        cases.append(("GET", prefix + f"/files/{name}?download=true", {}))
    cases.append(("GET", prefix + "/files/audio.wav", {"headers": {"Range": "bytes=0-15"}}))
    return cases


def test_every_project_route_denies_other_owners_and_unauthenticated_requests(
    tmp_path,
    authenticated_app,
    authenticated_client,
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        original = upload(owner)
        result = publish_sample(app.state.store, original["id"])
        intruder, intruder_user = other_client(app)
        anonymous = TestClient(app)
        try:
            b_job = upload(intruder, "B-private.wav")
            for method, path, kwargs in request_cases(
                original["id"], original["project_id"], result["generation"]
            ):
                response = intruder.request(method, path, **kwargs)
                assert response.status_code == 404, (
                    method,
                    path,
                    response.status_code,
                    response.text,
                )
                assert original["filename"] not in response.text
                assert "content-range" not in response.headers
                response = anonymous.request(method, path, **kwargs)
                assert response.status_code == 401, (method, path, response.status_code)
            for path in ("/api/jobs", "/api/projects", "/api/system"):
                assert anonymous.get(path).status_code == 401
            assert anonymous.post("/api/projects", json={"name": "unauthorized"}).status_code == 401
            assert [job["id"] for job in intruder.get("/api/jobs").json()] == [b_job["id"]]
            assert intruder.get("/api/jobs", params={"q": "A-private"}).json() == []
            assert intruder.get("/api/projects", params={"q": "A-private"}).json() == []
            projects = intruder.get("/api/projects").json()
            assert len(projects) == 1 and projects[0]["owner_id"] == intruder_user["id"]
            assert projects[0]["job_count"] == 1
            assert owner.get(f"/api/jobs/{original['id']}").json()["status"] == "completed"
            assert (
                owner.get(f"/api/jobs/{original['id']}/log").text
                == "private worker log for owner A"
            )
            assert owner.get(f"/api/projects/{original['project_id']}").json()["job_count"] == 1
            forged = intruder.post(
                "/api/jobs",
                files={"file": ("forged.wav", b"recording")},
                data={"owner_id": app.state.test_user["id"]},
            )
            assert forged.status_code == 202
            assert forged.json()["owner_id"] == intruder_user["id"]
            assert [job["id"] for job in owner.get("/api/jobs").json()] == [original["id"]]
        finally:
            intruder.close()
            anonymous.close()


def test_project_creation_association_search_and_counts_are_owner_scoped(
    tmp_path,
    authenticated_app,
    authenticated_client,
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        project = owner.post("/api/projects", json={"name": "Owner practice"})
        assert project.status_code == 201
        project = project.json()
        first = upload(owner, project_id=project["id"])
        second = upload(owner, filename="second.wav", project_id=project["id"])
        assert first["project_id"] == second["project_id"] == project["id"]
        assert owner.get(f"/api/projects/{project['id']}").json()["job_count"] == 2
        assert len(owner.get("/api/jobs", params={"project_id": project["id"]}).json()) == 2
        assert len(owner.get("/api/projects", params={"q": "practice"}).json()) == 1
        assert owner.get("/api/projects", params={"q": "missing"}).json() == []
        assert owner.post("/api/projects", json={"name": "   "}).status_code in {409, 422}


def test_empty_scratch_restores_result_edits_range_downloads_waveform_and_layout(
    tmp_path,
    authenticated_app,
    authenticated_client,
    mysql_database,
    disk_objects,
):
    original_root = tmp_path / "first-container"
    app = authenticated_app(original_root)
    with authenticated_client(app) as owner:
        original = upload(owner)
        expected = publish_sample(app.state.store, original["id"])
        url = f"/api/jobs/{original['id']}"
        original_audio = owner.get(url + "/files/audio.wav").content
        original_svg = owner.get(url + "/files/score.svg").content
        layout = owner.get(
            url + "/score-layout", params={"generation": expected["generation"]}
        ).json()
        assert len(layout["measures"]) == 3
        cookie = dict(app.state.test_cookie)
        auth = app.state.auth
    shutil.rmtree(original_root)
    restored_store = JobStore(tmp_path / "empty-container", db=mysql_database, objects=disk_objects)
    restored = create_app(start_worker=False, store=restored_store, auth_service=auth)
    with TestClient(restored, cookies=cookie) as owner:
        saved = owner.get(url)
        assert saved.status_code == 200
        assert saved.json()["result"] == expected
        assert owner.get(url + "/files/events.json").json() == [
            {"time": 0.5, "pitch": 38, "strength": 0.8, "velocity": 100}
        ]
        assert owner.get(url + "/files/audio.wav").content == original_audio
        partial = owner.get(url + "/files/audio.wav", headers={"Range": "bytes=0-15"})
        assert partial.status_code == 206 and partial.content == original_audio[:16]
        assert partial.headers["content-range"].startswith("bytes 0-15/")
        assert partial.headers["cache-control"] == "private, no-store"
        assert owner.get(url + "/files/score.svg").content == original_svg
        assert owner.get(url + "/waveform").json()["duration"] == 6
        recovered_layout = owner.get(
            url + "/score-layout", params={"generation": expected["generation"]}
        ).json()
        assert recovered_layout == layout
        assert owner.get(url + "/log").text == "private worker log for owner A"
        assert owner.get(url + "/files/score-bundle.zip?download=true").status_code == 200
        assert (
            owner.get(url + "/score-layout", params={"generation": "render-old"}).status_code == 409
        )


def test_object_outage_is_recoverable_and_failed_upload_creates_no_project(
    tmp_path,
    authenticated_app,
    authenticated_client,
    disk_objects,
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        disk_objects.fail_writes = True
        response = owner.post("/api/jobs", files={"file": ("song.wav", b"audio")})
        assert response.status_code == 503
        assert owner.get("/api/jobs").json() == []
        assert owner.get("/api/projects").json() == []
        disk_objects.fail_writes = False
        original = upload(owner)
        publish_sample(app.state.store, original["id"])
        disk_objects.fail_reads = True
        url = f"/api/jobs/{original['id']}/files/audio.wav"
        failed = owner.get(url)
        assert failed.status_code == 503 and failed.headers["retry-after"] == "5"
        assert owner.get(f"/api/jobs/{original['id']}").json()["status"] == "completed"
        disk_objects.fail_reads = False
        assert owner.get(url).status_code == 200


def test_boto3_transfer_failures_return_recoverable_upload_error(
    tmp_path, authenticated_app, authenticated_client, disk_objects, monkeypatch
):
    from boto3.exceptions import S3UploadFailedError

    app = authenticated_app(tmp_path)

    def failed_transfer(*args):
        # boto3 upload_file wraps a service ClientError in this separate class.
        raise S3UploadFailedError("private storage endpoint upload failed")

    monkeypatch.setattr(disk_objects, "put", failed_transfer)
    with authenticated_client(app) as owner:
        response = owner.post("/api/jobs", files={"file": ("song.wav", b"audio")})
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert "private storage" not in response.text
        assert owner.get("/api/projects").json() == []
        assert owner.get("/api/jobs").json() == []


def test_logout_revokes_only_current_session_and_clears_private_access(
    tmp_path,
    authenticated_app,
    authenticated_client,
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        original = upload(owner)
        intruder, _ = other_client(app)
        try:
            before = owner.get("/api/auth/me").json()["user"]
            assert before["id"] == app.state.test_user["id"]
            token = dict(app.state.test_cookie)
            assert owner.post("/api/auth/logout").status_code == 204
            assert owner.get("/api/auth/me").json() == {"user": None}
            assert owner.get(f"/api/jobs/{original['id']}").status_code == 401
            owner.cookies.update(token)
            assert owner.get("/api/jobs").status_code == 401
            assert intruder.get("/api/auth/me").json()["user"] is not None
        finally:
            intruder.close()


def test_database_errors_return_recoverable_response_without_connection_details(
    tmp_path,
    authenticated_app,
    authenticated_client,
    monkeypatch,
):
    from pymysql import OperationalError

    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        original_list = app.state.store.list

        def unavailable(*args, **kwargs):
            raise OperationalError(2006, "private-db-host secret-password")

        monkeypatch.setattr(app.state.store, "list", unavailable)
        response = owner.get("/api/jobs")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert "private-db-host" not in response.text and "secret-password" not in response.text
        monkeypatch.setattr(app.state.store, "list", original_list)
        assert owner.get("/api/jobs").status_code == 200
