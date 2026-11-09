"""Real MySQL races around private object staging, publication and collection."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from transcription.store import JobStore
from transcription.upload_journal import abandon_uploads


@pytest.fixture
def journal_store(tmp_path, mysql_database, disk_objects):
    store = JobStore(tmp_path / "scratch", db=mysql_database, objects=disk_objects)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO beatit.users(id,google_sub,email,picture) VALUES ('owner','owner','owner@example.test','')"
        )
    return store


def prepare(store, content=b"private input"):
    identifier = uuid4().hex
    directory = store.directory(identifier)
    directory.mkdir()
    (directory / "input").write_bytes(content)
    (directory / "request.json").write_text(json.dumps({"options": {}}))
    return identifier, directory


def submit(store):
    identifier, _ = prepare(store)
    store.add(identifier, "source.wav", owner_id="owner")
    return identifier


def pending(store):
    with store.db.connection() as connection:
        return connection.execute("SELECT * FROM beatit.pending_object_uploads").fetchall()


def expire(store):
    with store.db.connection() as connection:
        connection.execute("UPDATE beatit.pending_object_uploads SET expires=0")


def stored_keys(store):
    return {
        path.relative_to(store.objects.root).as_posix()
        for path in store.objects.root.rglob("*")
        if path.is_file()
    }


def test_admission_race_rejected_input_is_durably_tracked_and_collected(journal_store, monkeypatch):
    store = journal_store
    submit(store)
    submit(store)
    barrier = threading.Barrier(2)
    capture = store._capture

    def competing_capture(*args):
        snapshot = capture(*args)
        barrier.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(store, "_capture", competing_capture)
    identifiers = [prepare(store)[0] for _ in range(2)]

    def contender(identifier):
        try:
            store.add(identifier, "source.wav", owner_id="owner")
            return "accepted"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(contender, identifiers))
    assert sorted(outcomes) == ["accepted", "rejected"]
    assert len(store.list("owner")) == 3
    assert len(pending(store)) == 1 and len(stored_keys(store)) == 4
    store.collect_garbage()
    assert pending(store) == []
    with store.connect() as connection:
        committed = {
            item["object_key"]
            for item in connection.execute("SELECT object_key FROM beatit.artifacts").fetchall()
        }
    assert stored_keys(store) == committed and len(committed) == 3


def test_database_outage_after_capture_leaves_durable_recoverable_journal(
    journal_store, monkeypatch
):
    store = journal_store
    identifier, _ = prepare(store)
    capture, connect = store._capture, store.connect
    offline = False

    def unavailable():
        if offline:
            raise OSError("database unavailable")
        return connect()

    def captured(*args):
        nonlocal offline
        snapshot = capture(*args)
        offline = True
        return snapshot

    monkeypatch.setattr(store, "connect", unavailable)
    monkeypatch.setattr(store, "_capture", captured)
    with pytest.raises(OSError, match="unavailable"):
        store.add(identifier, "source.wav", owner_id="owner")
    assert len(pending(store)) == 1 and len(stored_keys(store)) == 1
    offline = False
    assert store.list("owner") == []
    expire(store)  # Simulate the conservative recovery delay after a hard outage.
    store.collect_garbage()
    assert pending(store) == [] and stored_keys(store) == set()


def test_publication_rollback_keeps_staged_key_until_safe_gc(journal_store, monkeypatch):
    store = journal_store
    identifier, _ = prepare(store)
    write = store._write_manifests

    def rollback(connection, *args):
        write(connection, *args)
        raise OSError("publication failed before commit")

    monkeypatch.setattr(store, "_write_manifests", rollback)
    with pytest.raises(OSError, match="before commit"):
        store.add(identifier, "source.wav", owner_id="owner")
    assert store.list("owner") == [] and len(pending(store)) == 1
    store.collect_garbage()
    assert pending(store) == [] and stored_keys(store) == set()


def test_lost_commit_acknowledgement_never_deletes_committed_object(journal_store, monkeypatch):
    store = journal_store
    identifier, _ = prepare(store)
    write = store._write_manifests

    def committed_but_unconfirmed(connection, *args):
        write(connection, *args)
        connection.raw.commit()
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(store, "_write_manifests", committed_but_unconfirmed)
    with pytest.raises(OSError, match="acknowledgement"):
        store.add(identifier, "source.wav", owner_id="owner")
    assert store.get(identifier, owner_id="owner")["status"] == "queued"
    assert pending(store) == []
    store.collect_garbage()
    assert len(stored_keys(store)) == 1
    assert store.file_path(identifier, "input", owner_id="owner").read_bytes() == b"private input"


def test_inflight_put_is_not_collected_even_if_its_journal_expires(journal_store, monkeypatch):
    store = journal_store
    identifier, directory = prepare(store)
    entered, release = threading.Event(), threading.Event()
    put = store.objects.put

    def inflight(key, path):
        # Another DB connection must already see the journal before any PUT.
        assert any(
            item["object_key"] == key and item["state"] == "uploading" for item in pending(store)
        )
        put(key, path)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(store.objects, "put", inflight)
    with ThreadPoolExecutor(max_workers=1) as executor:
        capture = executor.submit(store._capture, identifier, directory)
        assert entered.wait(3)
        try:
            expire(store)
            store.collect_garbage()
            assert len(stored_keys(store)) == 1 and len(pending(store)) == 1
            # Slow object transfers reserve no API/auth/heartbeat pool slot.
            assert store.db.pool.checkedout() == 0
            assert store.db.upload_pool.checkedout() == 1
            with store.connect() as connection:
                assert connection.execute("SELECT 1 AS alive").fetchone()["alive"] == 1
        finally:
            release.set()
        manifests, _ = capture.result(timeout=3)
    abandon_uploads(store, manifests)
    store.collect_garbage()
    assert pending(store) == [] and stored_keys(store) == set()


def test_collector_winner_fences_late_publication_and_preserves_original(journal_store):
    store = journal_store
    identifier = submit(store)
    original = store.artifact_manifest(identifier, "input")
    directory = store.directory(identifier)
    (directory / "input").write_bytes(b"new private bytes")
    manifests, _ = store._capture(identifier, directory)
    expire(store)
    store.collect_garbage()
    with pytest.raises(ValueError, match="expired before publication"):
        with store.connect() as connection:
            store._write_manifests(connection, identifier, manifests)
    assert store.artifact_manifest(identifier, "input") == original
    assert stored_keys(store) == {original["object_key"]}


def test_reused_committed_key_is_not_registered_or_collected(journal_store):
    store = journal_store
    identifier = submit(store)
    before = stored_keys(store)
    manifests, _ = store._capture(identifier, store.directory(identifier))
    assert not any("upload_token" in manifest for manifest in manifests)
    assert pending(store) == []
    with store.connect() as connection:
        store._write_manifests(connection, identifier, manifests)
    store.collect_garbage()
    assert stored_keys(store) == before


def test_uncertain_object_put_remains_tracked_until_its_recovery_grace(journal_store, monkeypatch):
    store = journal_store
    identifier, directory = prepare(store)
    put = store.objects.put

    def unknown_outcome(key, path):
        put(key, path)
        raise OSError("storage acknowledgement lost")

    monkeypatch.setattr(store.objects, "put", unknown_outcome)
    with pytest.raises(OSError, match="acknowledgement"):
        store._capture(identifier, directory)
    assert len(pending(store)) == 1
    store.collect_garbage()
    assert len(stored_keys(store)) == 1
    expire(store)
    store.collect_garbage()
    assert pending(store) == [] and stored_keys(store) == set()


def test_stale_pending_journal_cannot_delete_a_committed_reference(journal_store):
    store = journal_store
    identifier = submit(store)
    manifest = store.artifact_manifest(identifier, "input")
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO beatit.pending_object_uploads VALUES (%s,%s,'ready',0)",
            (manifest["object_key"], uuid4().hex),
        )
    store.collect_garbage()
    assert pending(store) == []
    assert stored_keys(store) == {manifest["object_key"]}
    assert store.get(identifier)["status"] == "queued"


def test_retired_reused_snapshot_cannot_resurrect_a_collected_key(journal_store):
    store = journal_store
    identifier = submit(store)
    stale, _ = store._capture(identifier, store.directory(identifier))
    (store.directory(identifier) / "input").write_bytes(b"replacement bytes")
    replacement, _ = store._capture(identifier, store.directory(identifier))
    with store.connect() as connection:
        store._write_manifests(connection, identifier, replacement)
    store.collect_garbage()
    with pytest.raises(ValueError, match="expired before publication"):
        with store.connect() as connection:
            store._write_manifests(connection, identifier, stale)
    assert store.file_path(identifier, "input").read_bytes() == b"replacement bytes"
    assert stored_keys(store) == {replacement[0]["object_key"]}
