"""Edge media authorization is owner scoped and never hydrates storage bytes."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from transcription.app import JOB_AUDIO, create_app


@pytest.fixture
def owned_media(tmp_path, authenticated_app, authenticated_client):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        response = client.post("/api/jobs", files={"file": ("fixture.wav", b"source")})
        assert response.status_code == 202
        identifier = response.json()["id"]
        store = app.state.store
        directory = store.directory(identifier)
        for name in JOB_AUDIO:
            (directory / name).write_bytes(("private " + name).encode())
        (directory / "result.json").write_text(
            json.dumps({"generation": "render-1", "duration": 8, "options": {}})
        )
        store.update(identifier, "completed")
        yield app, client, identifier


@pytest.mark.parametrize("source", sorted(JOB_AUDIO))
def test_owner_receives_exact_media_manifest_without_storage_access(
    owned_media, monkeypatch, source
):
    app, client, identifier = owned_media
    store = app.state.store

    def no_download(*args, **kwargs):
        raise AssertionError("Media authorization must never download object bytes")

    monkeypatch.setattr(store.objects, "get", no_download)
    monkeypatch.setattr(store, "file_path", no_download)
    response = client.get(f"/api/jobs/{identifier}/media-authorize", params={"source": source})
    assert response.status_code == 200
    manifest = response.json()
    expected = ("private " + source).encode()
    assert set(manifest) == {"object_key", "size", "sha256", "content_type"}
    assert manifest["object_key"].startswith("jobs/" + identifier + "/")
    assert manifest["object_key"].endswith("/" + source)
    assert manifest["size"] == len(expected)
    assert manifest["sha256"] == hashlib.sha256(expected).hexdigest()
    assert manifest["content_type"] == ("audio/flac" if source.endswith(".flac") else "audio/wav")
    assert response.headers["cache-control"] == "private, no-store"
    assert not (store.root / ".objects").exists()


def test_media_authorization_denies_anonymous_other_owner_and_revoked_session(owned_media):
    app, owner, identifier = owned_media
    route = f"/api/jobs/{identifier}/media-authorize"
    token, _ = app.state.auth.create_session(
        {"sub": "media-owner-b", "email": "b@example.test", "name": "Owner B"}
    )
    other = TestClient(app, cookies={app.state.auth.settings.cookie_name: token})
    anonymous = TestClient(app)
    try:
        for source in JOB_AUDIO:
            assert anonymous.get(route, params={"source": source}).status_code == 401
            assert other.get(route, params={"source": source}).status_code == 404
        assert owner.get(route).status_code == 200
        old_cookie = dict(owner.cookies)
        assert owner.post("/api/auth/logout").status_code == 204
        owner.cookies.update(old_cookie)
        assert owner.get(route).status_code == 401
    finally:
        other.close()
        anonymous.close()


@pytest.mark.parametrize(
    "source",
    [
        "../audio.wav",
        "/audio.wav",
        "input",
        "events.json",
        "score.musicxml",
        "jobs.sqlite3",
        "stem-unknown.flac",
    ],
)
def test_authorization_cannot_expose_non_audio_or_arbitrary_object_paths(owned_media, source):
    _, client, identifier = owned_media
    assert (
        client.get(f"/api/jobs/{identifier}/media-authorize", params={"source": source}).status_code
        == 404
    )


def test_authorization_validates_job_id_and_requires_a_committed_manifest(owned_media):
    app, client, _ = owned_media
    assert client.get("/api/jobs/invalid/media-authorize").status_code == 404
    assert client.get(f"/api/jobs/{'f' * 32}/media-authorize").status_code == 404
    queued = client.post("/api/jobs", files={"file": ("new.wav", b"source")}).json()
    assert client.get(f"/api/jobs/{queued['id']}/media-authorize").status_code == 404
    with pytest.raises(KeyError):
        app.state.store.artifact_manifest(
            queued["id"], "../input", owner_id=app.state.test_user["id"]
        )


@pytest.mark.parametrize("base", ["", "/media"])
def test_system_advertises_only_the_configured_same_origin_media_path(
    tmp_path, monkeypatch, authenticated_app, authenticated_client, base
):
    monkeypatch.setenv("BEATIT_MEDIA_BASE_URL", base)
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        assert client.get("/api/system").json()["media_base_url"] == base


@pytest.mark.parametrize(
    "base", ["https://objects.example.test", "//objects.example.test", "/media/", "/another-path"]
)
def test_invalid_media_path_is_rejected_before_database_or_auth_initialization(monkeypatch, base):
    monkeypatch.setenv("BEATIT_MEDIA_BASE_URL", base)
    with pytest.raises(RuntimeError, match="must be empty or /media"):
        create_app(start_worker=False, store=SimpleNamespace(db=None))


def test_direct_origin_preserves_full_range_head_and_if_range_playback(owned_media):
    _, owner, identifier = owned_media
    path = f"/media/{identifier}/audio.wav"
    original = b"private audio.wav"
    response = owner.get(path)
    assert response.status_code == 200 and response.content == original
    assert response.headers["x-beatit-media"] == "origin"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"
    assert response.headers["etag"] == '"' + hashlib.sha256(original).hexdigest() + '"'
    partial = owner.get(path, headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206 and partial.content == original[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(original)}"
    head = owner.head(path)
    assert head.status_code == 200 and not head.content
    assert int(head.headers["content-length"]) == len(original)
    changed = owner.get(path, headers={"Range": "bytes=2-5", "If-Range": '"stale"'})
    assert changed.status_code == 200 and changed.content == original
    assert owner.get(path, headers={"Range": "bytes=999-"}).status_code == 416


def test_direct_origin_rechecks_owner_and_revocation_and_restricts_paths(owned_media):
    app, owner, identifier = owned_media
    path = f"/media/{identifier}/audio.wav"
    token, _ = app.state.auth.create_session({"sub": "origin-b", "email": "b@example.test"})
    with TestClient(app, cookies={app.state.auth.settings.cookie_name: token}) as other:
        anonymous = TestClient(app)
        try:
            for client, expected in [(anonymous, 401), (other, 404)]:
                response = client.get(path)
                assert response.status_code == expected
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["vary"] == "Cookie"
                assert response.headers["cross-origin-resource-policy"] == "same-origin"
            assert other.head(path).status_code == 404
            assert owner.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
            assert owner.get(path + "?arbitrary=1").status_code == 404
            assert owner.get(f"/media/{identifier}/input").status_code == 404
            assert owner.get(f"/media/{identifier}/audio-preview.m4a").status_code == 404
            old_cookie = dict(owner.cookies)
            assert owner.post("/api/auth/logout").status_code == 204
            owner.cookies.update(old_cookie)
            assert owner.get(path, headers={"Range": "bytes=0-3"}).status_code == 401
        finally:
            anonymous.close()


def test_direct_origin_uses_one_immutable_manifest_during_artifact_replacement(
    owned_media, monkeypatch
):
    app, owner, identifier = owned_media
    store = app.state.store
    original = b"private audio.wav"
    select = store.playback_manifest

    def replace_after_selection(*args, **kwargs):
        manifest, content_type = select(*args, **kwargs)
        (store.directory(identifier) / "audio.wav").write_bytes(b"replacement audio")
        store.update(identifier, "completed")
        return manifest, content_type

    monkeypatch.setattr(store, "playback_manifest", replace_after_selection)
    response = owner.get(f"/media/{identifier}/audio.wav")
    assert response.status_code == 200 and response.content == original
    assert response.headers["etag"] == '"' + hashlib.sha256(original).hexdigest() + '"'


def test_direct_origin_keeps_verified_aac_preview_selection(
    tmp_path, authenticated_app, authenticated_client
):
    import numpy as np
    import soundfile as sf

    from transcription.playback_preview import prepare_playback_previews

    app = authenticated_app(tmp_path)
    with authenticated_client(app) as owner:
        identifier = owner.post("/api/jobs", files={"file": ("audio.wav", b"source")}).json()["id"]
        directory = app.state.store.directory(identifier)
        sf.write(directory / "audio.wav", np.zeros(8000, dtype=np.float32), 8000)
        report = prepare_playback_previews(directory)
        assert report["created"] == ["audio-preview.m4a"]
        (directory / "result.json").write_text('{"generation":"render-1","duration":1}')
        app.state.store.update(identifier, "completed")
        expected = (directory / "audio-preview.m4a").read_bytes()
        response = owner.get(f"/media/{identifier}/audio.wav")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mp4"
        assert response.content == expected
