"""Real AAC encoding, timeline preservation, reuse and owner-scoped preview delivery."""

import array
import hashlib
import json
import math
import shutil
import subprocess
import wave

import pytest
from fastapi.testclient import TestClient

from transcription.object_storage import file_digest
from transcription.playback_preview import (
    PLAYBACK_PREVIEWS,
    PREVIEW_METADATA,
    prepare_playback_previews,
)


def recording(path, seconds=2, frequency=440):
    rate = 44100
    samples = array.array("h")
    for frame in range(int(rate * seconds)):
        value = int(18000 * math.sin(2 * math.pi * frequency * frame / rate))
        samples.extend((value, value))
    with wave.open(str(path), "wb") as stream:
        stream.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return path


def mp4_atoms(path):
    content = path.read_bytes()
    offset, atoms = 0, []
    while offset + 8 <= len(content):
        size = int.from_bytes(content[offset : offset + 4], "big")
        atoms.append(content[offset + 4 : offset + 8])
        assert size >= 8
        offset += size
    return atoms


def test_real_aac_preserves_duration_channels_and_lossless_original_with_faststart(tmp_path):
    original = recording(tmp_path / "audio.wav")
    original_hash = file_digest(original)
    report = prepare_playback_previews(tmp_path)
    assert report == {"created": ["audio-preview.m4a"], "reused": [], "failed": []}
    preview = tmp_path / "audio-preview.m4a"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(preview)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    data = json.loads(probe.stdout)
    stream = data["streams"][0]
    assert stream["codec_name"] == "aac" and stream["channels"] == 2
    assert int(stream["sample_rate"]) == 44100
    assert abs(float(data["format"]["duration"]) - 2) < 0.025
    assert abs(float(stream["start_time"])) < 0.001
    assert preview.stat().st_size < original.stat().st_size / 8
    atoms = mp4_atoms(preview)
    assert atoms.index(b"moov") < atoms.index(b"mdat")
    assert file_digest(original) == original_hash
    metadata = json.loads((tmp_path / PREVIEW_METADATA).read_text())
    assert metadata["sources"]["audio.wav"]["original_sha256"] == original_hash


def test_unchanged_original_reuses_preview_without_invoking_encoder(tmp_path, monkeypatch):
    recording(tmp_path / "audio.wav")
    prepare_playback_previews(tmp_path)
    preview_hash = file_digest(tmp_path / "audio-preview.m4a")
    calls = []

    def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Unchanged recording must reuse its playback preview")

    monkeypatch.setattr("transcription.playback_preview.subprocess.run", unexpected)
    report = prepare_playback_previews(tmp_path)
    assert report["reused"] == ["audio-preview.m4a"] and calls == []
    assert file_digest(tmp_path / "audio-preview.m4a") == preview_hash


def test_invalid_wav_and_encoder_failure_fall_back_without_modifying_original(
    tmp_path, monkeypatch
):
    original = recording(tmp_path / "audio.wav")
    before = original.read_bytes()
    monkeypatch.setattr(
        "transcription.playback_preview.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"encoding failed"),
    )
    assert prepare_playback_previews(tmp_path)["failed"] == ["audio.wav"]
    assert original.read_bytes() == before
    assert not (tmp_path / "audio-preview.m4a").exists()
    original.write_bytes(b"fake test WAV")
    assert prepare_playback_previews(tmp_path)["failed"] == ["audio.wav"]
    assert original.read_bytes() == b"fake test WAV"


def completed_fixture(app, client, *, leased):
    response = client.post("/api/jobs", files={"file": ("fixture.wav", b"original upload")})
    identifier = response.json()["id"]
    store = app.state.store
    claim = store.claim() if leased else None
    directory = (
        store.restore(identifier, store.root / "fixture") if leased else store.directory(identifier)
    )
    recording(directory / "audio.wav")
    recording(directory / "drums.wav", frequency=90)
    result = {
        "generation": "render-1",
        "duration": 2,
        "options": {"bpm": 120},
        "metadata": {"title": "Fixture", "author": "Owner"},
    }
    (directory / "result.json").write_text(json.dumps(result))
    if leased:
        assert store.finish_claim(identifier, claim["claim_token"], directory)
    else:
        store.update(identifier, "completed")
    return identifier


def test_new_completed_publication_persists_preview_and_revision_reuses_after_restore(
    tmp_path, authenticated_app, authenticated_client, monkeypatch
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        identifier = completed_fixture(app, client, leased=True)
        store = app.state.store
        route = f"/api/jobs/{identifier}/media-authorize"
        first = client.get(route).json()
        assert first["content_type"] == "audio/mp4"
        assert first["object_key"].endswith("/audio-preview.m4a")
        original = store.artifact_manifest(identifier, "audio.wav", app.state.test_user["id"])
        response = client.get(f"/api/jobs/{identifier}/files/audio.wav")
        assert response.headers["content-type"] == "audio/wav"
        assert hashlib.sha256(response.content).hexdigest() == original["sha256"]
        shutil.rmtree(store.root)
        store.root.mkdir()
        store.revise(
            identifier,
            None,
            metadata={"title": "Changed title", "author": "Owner"},
            owner_id=app.state.test_user["id"],
        )
        revision = store.claim()
        restored = store.restore(identifier, store.root / "new-attempt")
        assert (restored / PREVIEW_METADATA).is_file()
        assert (restored / "audio-preview.m4a").is_file()
        calls = []

        def unexpected(*args, **kwargs):
            calls.append(args)
            raise AssertionError("Metadata revision must not re-encode original audio")

        monkeypatch.setattr("transcription.playback_preview.subprocess.run", unexpected)
        result = json.loads((restored / "result.json").read_text())
        result["generation"] = "render-2"
        (restored / "result.json").write_text(json.dumps(result))
        assert store.finish_claim(identifier, revision["claim_token"], restored)
        assert calls == []
        assert client.get(route).json() == first


def test_unexpected_preview_failure_still_publishes_the_completed_score(
    tmp_path, authenticated_app, authenticated_client, monkeypatch
):
    def unavailable(directory):
        raise OSError("Preview scratch unavailable")

    monkeypatch.setattr("transcription.store.prepare_playback_previews", unavailable)
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        identifier = completed_fixture(app, client, leased=True)
        job = app.state.store.get(identifier)
        assert job["status"] == "completed" and job["error"] is None
        assert job["result"]["metadata"]["title"] == "Fixture"
        assert (
            client.get(f"/api/jobs/{identifier}/media-authorize").json()["content_type"]
            == "audio/wav"
        )


def test_backfill_changes_only_derived_manifests_and_is_idempotent_and_owner_scoped(
    tmp_path, authenticated_app, authenticated_client, monkeypatch
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        identifier = completed_fixture(app, client, leased=False)
        store = app.state.store
        owner_id = app.state.test_user["id"]
        route = f"/api/jobs/{identifier}/media-authorize"
        assert client.get(route).json()["content_type"] == "audio/wav"
        before = store.get(identifier)
        with store.connect() as connection:
            original_manifests = connection.execute(
                "SELECT * FROM beatit.artifacts WHERE job_id=%s ORDER BY path", (identifier,)
            ).fetchall()
            revisions = connection.execute(
                "SELECT count(*) AS n FROM beatit.revisions WHERE job_id=%s", (identifier,)
            ).fetchone()["n"]
        report = store.backfill_playback_previews(identifier, owner_id)
        assert set(report["created"]) == set(PLAYBACK_PREVIEWS.values())
        assert client.get(route).json()["content_type"] == "audio/mp4"
        assert store.get(identifier) == before
        with store.connect() as connection:
            after = {
                row["path"]: row
                for row in connection.execute(
                    "SELECT * FROM beatit.artifacts WHERE job_id=%s", (identifier,)
                ).fetchall()
            }
            assert (
                connection.execute(
                    "SELECT count(*) AS n FROM beatit.revisions WHERE job_id=%s", (identifier,)
                ).fetchone()["n"]
                == revisions
            )
        assert all(after[row["path"]] == row for row in original_manifests)
        assert set(after) - {row["path"] for row in original_manifests} == {
            *PLAYBACK_PREVIEWS.values(),
            PREVIEW_METADATA,
        }
        calls = []

        def unexpected(*args, **kwargs):
            calls.append(args)
            raise AssertionError("Backfill rerun must not re-encode")

        monkeypatch.setattr("transcription.playback_preview.subprocess.run", unexpected)
        assert set(store.backfill_playback_previews(identifier, owner_id)["reused"]) == set(
            PLAYBACK_PREVIEWS.values()
        )
        assert calls == []
        token, _ = app.state.auth.create_session(
            {"sub": "preview-owner-B", "email": "b@example.test", "name": "B"}
        )
        other = TestClient(app, cookies={app.state.auth.settings.cookie_name: token})
        assert other.get(route).status_code == 404
        assert client.get(route, params={"source": "audio-preview.m4a"}).status_code == 404
        with pytest.raises(KeyError):
            store.backfill_playback_previews(identifier, "another-owner")
        with store.connect() as connection:
            payloads = connection.execute(
                "SELECT payloads FROM beatit.jobs WHERE id=%s", (identifier,)
            ).fetchone()["payloads"]
            payloads[PREVIEW_METADATA]["sources"]["audio.wav"]["original_sha256"] = "0" * 64
            connection.execute(
                "UPDATE beatit.jobs SET payloads=%s WHERE id=%s", (json.dumps(payloads), identifier)
            )
        assert client.get(route).json()["content_type"] == "audio/wav"
        with store.connect() as connection:
            connection.execute(
                "DELETE FROM beatit.artifacts WHERE job_id=%s AND path='audio.wav'", (identifier,)
            )
        assert client.get(route).status_code == 404
        other.close()
