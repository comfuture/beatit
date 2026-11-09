"""Real MySQL integration coverage for durable state and lease fencing."""

import json
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from transcription.legacy_import import import_legacy, rollback_import
from transcription.store import JobStore


@pytest.fixture
def persistence_store(tmp_path, mysql_database, disk_objects):
    store = JobStore(tmp_path / "scratch", db=mysql_database, objects=disk_objects)
    for owner in ("owner-a", "owner-b"):
        with store.connect() as connection:
            connection.execute(
                "INSERT INTO beatit.users(id,google_sub,email,picture) VALUES (%s,%s,%s,'')",
                (owner, owner, owner + "@example.com"),
            )
    yield store
    store.stop()


def enqueue(store, owner="owner-a", filename="song.wav", project_id=None):
    identifier = uuid4().hex
    directory = store.directory(identifier)
    directory.mkdir()
    (directory / "input").write_bytes(b"source audio")
    (directory / "request.json").write_text(
        json.dumps({"filename": filename, "options": {"bpm": 90}})
    )
    store.add(identifier, filename, owner_id=owner, project_id=project_id)
    return identifier


def rendered(store, claim, generation="render-1"):
    directory = store.restore(claim["id"], store.root / "fixture-attempt" / uuid4().hex)
    (directory / generation).mkdir()
    (directory / generation / "score.musicxml").write_text("<score-partwise />")
    (directory / generation / "events.json").write_text('[{"time":1,"pitch":38}]')
    (directory / "analysis.json").write_text('{"duration":5}')
    (directory / "events.json").write_text('[{"time":1,"pitch":38}]')
    (directory / "audio.wav").write_bytes(b"processed audio")
    (directory / "result.json").write_text(
        json.dumps(
            {
                "generation": generation,
                "duration": 5,
                "options": {"bpm": 90},
                "pages": [],
                "metadata": {"title": "Owner score", "author": "Owner A"},
                "beat_review": {"grid_key": "one", "beats": []},
            }
        )
    )
    return directory


def test_database_is_authoritative_and_projects_are_owner_scoped(persistence_store):
    store = persistence_store
    project = store.create_project("owner-a", name="Private recording")
    identifier = enqueue(store, project_id=project["id"])
    (store.directory(identifier) / "request.json").write_text('{"forged":true}')
    assert "forged" not in store.get(identifier, owner_id="owner-a")["request"]
    assert store.list("owner-b") == []
    assert store.list_projects("owner-b", query="Private") == []
    assert store.get_project(project["id"], "owner-a")["job_count"] == 1
    for lookup in (
        lambda: store.get(identifier, owner_id="owner-b"),
        lambda: store.get_project(project["id"], "owner-b"),
        lambda: store.file_path(identifier, "input", owner_id="owner-b"),
        lambda: store.cancel(identifier, owner_id="owner-b"),
    ):
        with pytest.raises(KeyError):
            lookup()
    assert store.get(identifier)["status"] == "queued"


def test_empty_scratch_restores_result_edits_and_artifacts(persistence_store):
    store = persistence_store
    identifier = enqueue(store)
    claim = store.claim()
    directory = rendered(store, claim)
    assert store.finish_claim(identifier, claim["claim_token"], directory)
    saved = store.get(identifier)
    shutil.rmtree(store.root)
    recovered = JobStore(store.root, db=store.db, objects=store.objects)
    assert recovered.get(identifier) == saved
    assert (
        recovered.file_path(identifier, "render-1/score.musicxml").read_text()
        == "<score-partwise />"
    )
    restored = recovered.restore(identifier)
    assert json.loads((restored / "events.json").read_text())[0]["pitch"] == 38
    assert json.loads((restored / "result.json").read_text())["metadata"]["title"] == "Owner score"
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM beatit.revisions WHERE job_id=%s", (identifier,)
            ).fetchone()["n"]
            == 1
        )


def test_competing_claims_enforce_global_worker_limit(persistence_store):
    store = persistence_store
    enqueue(store)
    enqueue(store, "owner-b")
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: store.claim(), range(2)))
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim)
    assert store.heartbeat(winner["id"], winner["claim_token"])
    assert not store.heartbeat(winner["id"], "wrong-token")


def test_fair_owner_rotation_and_per_owner_admission_limit(persistence_store):
    store = persistence_store
    for _ in range(3):
        enqueue(store)
    enqueue(store, "owner-b")
    with pytest.raises(ValueError, match="계정당"):
        enqueue(store)
    first = store.claim()
    assert first["owner_id"] == "owner-a"
    store.release_claim(first["id"], first["claim_token"])
    second = store.claim()
    assert second["owner_id"] == "owner-b"
    store.release_claim(second["id"], second["claim_token"])
    third = store.claim()
    assert third["owner_id"] == "owner-a"


def test_expired_claim_cannot_renew_or_publish_over_new_worker(persistence_store):
    store = persistence_store
    identifier = enqueue(store)
    enqueue(store, "owner-b")
    stale = store.claim()
    stale_directory = rendered(store, stale)
    with store.connect() as connection:
        connection.execute(
            "UPDATE beatit.worker_slot SET lease_until=%s WHERE id=1", (time.time() - 1,)
        )
        connection.execute(
            "UPDATE beatit.jobs SET lease_until=%s WHERE id=%s", (time.time() - 1, identifier)
        )
    assert not store.heartbeat(identifier, stale["claim_token"])
    new = store.claim()
    assert new["owner_id"] == "owner-b"
    assert store.get(identifier)["status"] == "failed"
    assert not store.finish_claim(identifier, stale["claim_token"], stale_directory)
    assert store.get(identifier)["has_result"] is False
    assert store.heartbeat(new["id"], new["claim_token"])


def test_revision_storage_failure_retains_published_result(persistence_store, monkeypatch):
    store = persistence_store
    identifier = enqueue(store)
    initial = store.claim()
    assert store.finish_claim(identifier, initial["claim_token"], rendered(store, initial))
    store.revise(identifier, {"bpm": 100}, events=[{"time": 2, "pitch": 35}], owner_id="owner-a")
    revision = store.claim()
    revision_directory = rendered(store, revision, "render-2")
    assert store.get(identifier)["result"]["generation"] == "render-1"

    def offline(*args):
        raise OSError("object storage offline")

    monkeypatch.setattr(store.objects, "put", offline)
    with pytest.raises(OSError, match="offline"):
        store.finish_claim(identifier, revision["claim_token"], revision_directory)
    store.release_claim(identifier, revision["claim_token"], "storage unavailable; retry")
    assert store.get(identifier)["status"] == "failed"
    assert store.get(identifier)["result"]["generation"] == "render-1"
    assert store.file_path(identifier, "render-1/score.musicxml").is_file()
    with pytest.raises(KeyError):
        store.file_path(identifier, "render-2/score.musicxml")
    store.retry(identifier, owner_id="owner-a")
    assert store.get(identifier)["request"]["events"] == [{"time": 2, "pitch": 35}]


def test_cancel_fences_completion_and_retry_preserves_owner(persistence_store):
    store = persistence_store
    identifier = enqueue(store)
    claim = store.claim()
    directory = rendered(store, claim)
    store.cancel(identifier, owner_id="owner-a")
    assert not store.heartbeat(identifier, claim["claim_token"])
    assert not store.finish_claim(identifier, claim["claim_token"], directory)
    store.release_claim(identifier, claim["claim_token"])
    store.retry(identifier, owner_id="owner-a")
    assert store.get(identifier, owner_id="owner-a")["status"] == "queued"
    with pytest.raises(KeyError):
        store.retry(identifier, owner_id="owner-b")


def test_superseded_mutable_objects_are_collected_after_revision(persistence_store):
    store = persistence_store
    identifier = enqueue(store)
    first = store.claim()
    directory = rendered(store, first)
    (directory / "worker.log").write_text("first attempt")
    assert store.finish_claim(identifier, first["claim_token"], directory)
    with store.connect() as connection:
        before = {
            row["path"]: row["object_key"]
            for row in connection.execute(
                "SELECT path,object_key FROM beatit.artifacts WHERE job_id=%s", (identifier,)
            ).fetchall()
        }
    store.revise(identifier, {"bpm": 100}, owner_id="owner-a")
    second = store.claim()
    revision = rendered(store, second, "render-2")
    (revision / "worker.log").write_text("second attempt")
    (revision / "events.json").write_text('[{"time":2,"pitch":35}]')
    assert store.finish_claim(identifier, second["claim_token"], revision)
    store.collect_garbage()
    for path in ("worker.log", "events.json"):
        assert not (store.objects.root / before[path]).exists()
    assert (store.objects.root / before["render-1/score.musicxml"]).is_file()
    assert store.file_path(identifier, "events.json").read_text() == '[{"time":2,"pitch":35}]'
    store.delete(identifier, owner_id="owner-a")
    assert not any(path.is_file() for path in store.objects.root.rglob("*"))


def make_legacy(source: Path):
    identifier = uuid4().hex
    source.mkdir()
    with sqlite3.connect(source / "jobs.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE jobs(id TEXT PRIMARY KEY,filename TEXT,status TEXT,created REAL,updated REAL,error TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?,?,?,1,2,NULL)", (identifier, "legacy.wav", "completed")
        )
    directory = source / identifier
    (directory / "render-42").mkdir(parents=True)
    (directory / "input").write_bytes(b"legacy original media")
    (directory / "events.json").write_text('[{"time":1,"pitch":45}]')
    (directory / "render-42" / "score.musicxml").write_text("legacy score")
    (directory / "request.json").write_text(
        '{"options":{"bpm":120},"events":[{"time":1,"pitch":45}]}'
    )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "generation": "render-42",
                "options": {"bpm": 120},
                "metadata": {"title": "Legacy edit", "author": "Owner"},
                "beat_review": {"grid_key": "legacy", "beats": []},
            }
        )
    )
    return identifier


def test_legacy_import_is_owner_explicit_idempotent_and_rollback_safe(persistence_store, tmp_path):
    store = persistence_store
    source = tmp_path / "legacy-backup"
    legacy_id = make_legacy(source)
    originals = {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    with pytest.raises(ValueError, match="sign in"):
        import_legacy(store, source, "unknown", "server-backup-2026-11-09")
    first = import_legacy(store, source, "owner-a", "server-backup-2026-11-09")
    second = import_legacy(store, source, "owner-a", "server-backup-2026-11-09")
    assert len(first["imported"]) == 1
    assert second["imported"] == [] and second["skipped"] == first["imported"]
    identifier = first["imported"][0]
    assert store.get(identifier, owner_id="owner-a")["result"]["metadata"]["title"] == "Legacy edit"
    assert store.list("owner-b") == []
    with pytest.raises(ValueError, match="another owner"):
        import_legacy(store, source, "owner-b", "server-backup-2026-11-09")
    shutil.rmtree(store.root)
    assert store.file_path(identifier, "render-42/score.musicxml").read_text() == "legacy score"
    assert rollback_import(store, "server-backup-2026-11-09", "owner-a") == {"removed": 1}
    assert store.list("owner-a") == []
    assert (source / legacy_id / "result.json").is_file()
    assert originals == {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
