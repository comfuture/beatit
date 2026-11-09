import io
import json
import tarfile
import time
from uuid import uuid4

import pytest

from transcription.auth import AuthService, AuthSettings
from transcription.data_admin import TABLES, backup, probe, restore_backup
from transcription.store import JobStore


@pytest.fixture
def admin_store(tmp_path, mysql_database, disk_objects):
    store = JobStore(tmp_path / "scratch", db=mysql_database, objects=disk_objects)
    return store


def seed(store):
    auth = AuthService(store.db, AuthSettings("test", "secret", "http://localhost"))
    token, user = auth.create_session(
        {
            "sub": "test-admin-" + uuid4().hex,
            "email": "owner@example.test",
            "name": "Owner",
        }
    )
    identifier = uuid4().hex
    directory = store.directory(identifier)
    directory.mkdir()
    (directory / "input").write_bytes(b"owned source recording")
    (directory / "request.json").write_text(
        json.dumps({"filename": "source.wav", "options": {"bpm": 90}})
    )
    store.add(identifier, "source.wav", owner_id=user["id"])
    claim = store.claim()
    scratch = store.restore(identifier, store.root / "attempt")
    render = scratch / "render-1"
    render.mkdir()
    (render / "score.musicxml").write_text("<score-partwise />")
    (render / "events.json").write_text('[{"time":1,"pitch":38}]')
    (scratch / "events.json").write_text('[{"time":1,"pitch":38}]')
    (scratch / "result.json").write_text(
        json.dumps(
            {
                "generation": "render-1",
                "duration": 5,
                "pages": [],
                "options": {"bpm": 90},
                "beat_review": {"grid_key": "grid", "beats": []},
                "metadata": {"title": "Owned score", "author": "Owner"},
            }
        )
    )
    assert store.finish_claim(identifier, claim["claim_token"], scratch)
    return user, identifier, token


def empty_database(database):
    # Fixture-only reset. The production restore path never disables FK checks
    # and refuses every nonempty destination.
    with database.connection() as connection:
        connection.execute("SET FOREIGN_KEY_CHECKS=0")
        try:
            for table in (*TABLES, "sessions", "oauth_states", "object_garbage"):
                connection.execute(f"TRUNCATE TABLE {table}")
        finally:
            connection.execute("SET FOREIGN_KEY_CHECKS=1")


def test_probe_measures_real_mysql_and_object_roundtrip(admin_store):
    result = probe(admin_store)
    assert result["database"]["engine"] == "mysql"
    assert result["database"]["select_ms"] > 0
    assert result["database"]["locking_transaction_ms"] > 0
    assert result["objects"]["sha256_match"]
    assert result["objects"]["bytes"] == 4096
    assert not [path for path in admin_store.objects.root.rglob("*") if path.is_file()]


def test_backup_restore_retains_accounts_edits_objects_and_discards_sessions(admin_store, tmp_path):
    user, identifier, token = seed(admin_store)
    saved = admin_store.get(identifier, owner_id=user["id"])
    destination = tmp_path / "backup.tar.gz"
    outcome = backup(admin_store, destination, app_stopped=True)
    assert outcome["jobs"] == 1
    assert destination.stat().st_mode & 0o777 == 0o600
    with tarfile.open(destination) as archive:
        encoded = archive.extractfile("manifest.json").read()
        metadata = json.loads(encoded)
        assert b"token_hash" not in encoded and token.encode() not in encoded
        assert "sessions" not in metadata["tables"]
        assert "oauth_states" not in metadata["tables"]
        old_keys = {row["object_key"] for row in metadata["tables"]["artifacts"]}
    empty_database(admin_store.db)
    restored = restore_backup(admin_store, destination, app_stopped=True)
    assert restored["restored_jobs"] == 1
    assert admin_store.get(identifier, owner_id=user["id"])["result"] == saved["result"]
    assert (
        admin_store.file_path(
            identifier, "render-1/score.musicxml", owner_id=user["id"]
        ).read_text()
        == "<score-partwise />"
    )
    assert admin_store.get_project(saved["project_id"], user["id"])["job_count"] == 1
    with admin_store.connect() as connection:
        assert not connection.execute("SELECT 1 FROM beatit.sessions").fetchone()
        new_keys = {
            row["object_key"]
            for row in connection.execute("SELECT object_key FROM beatit.artifacts").fetchall()
        }
        assert old_keys.isdisjoint(new_keys)
        assert all(key.startswith("restore/") for key in new_keys)
        assert connection.execute("SELECT count(*) AS n FROM beatit.revisions").fetchone()["n"] == 1


def test_operations_require_offline_flag_and_never_replace_existing_data(admin_store, tmp_path):
    seed(admin_store)
    destination = tmp_path / "backup.tar.gz"
    with pytest.raises(ValueError, match="Stop all"):
        backup(admin_store, destination)
    backup(admin_store, destination, app_stopped=True)
    with pytest.raises(ValueError, match="already exists"):
        backup(admin_store, destination, app_stopped=True)
    with pytest.raises(ValueError, match="Stop all"):
        restore_backup(admin_store, destination)
    with pytest.raises(ValueError, match="empty"):
        restore_backup(admin_store, destination, app_stopped=True)
    with admin_store.connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM beatit.jobs").fetchone()["n"] == 1


def test_active_lease_prevents_backup_and_stale_runs_restore_as_failed(admin_store, tmp_path):
    user, identifier, _ = seed(admin_store)
    with admin_store.connect() as connection:
        connection.execute(
            "UPDATE beatit.jobs SET status='running',claim_token=%s,lease_until=%s WHERE id=%s",
            (uuid4().hex, time.time() + 60, identifier),
        )
        connection.execute(
            "UPDATE beatit.worker_slot SET lease_until=%s WHERE id=1", (time.time() + 60,)
        )
    destination = tmp_path / "backup.tar.gz"
    with pytest.raises(ValueError, match="active worker lease"):
        backup(admin_store, destination, app_stopped=True)
    with admin_store.connect() as connection:
        connection.execute("UPDATE beatit.worker_slot SET lease_until=0 WHERE id=1")
    backup(admin_store, destination, app_stopped=True)
    empty_database(admin_store.db)
    restore_backup(admin_store, destination, app_stopped=True)
    job = admin_store.get(identifier, owner_id=user["id"])
    assert job["status"] == "failed" and "retry explicitly" in job["error"]
    with admin_store.connect() as connection:
        state = connection.execute(
            "SELECT claim_token,lease_until FROM beatit.jobs WHERE id=%s", (identifier,)
        ).fetchone()
        assert state == {"claim_token": None, "lease_until": None}
        slot = connection.execute("SELECT * FROM beatit.worker_slot WHERE id=1").fetchone()
        assert slot["job_id"] is None and slot["token"] is None and slot["lease_until"] == 0


def rewrite_archive(source, target, transform):
    with tarfile.open(source) as original, tarfile.open(target, "w:gz") as changed:
        for member in original.getmembers():
            content = original.extractfile(member).read()
            name, content = transform(member.name, content)
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            changed.addfile(entry, io.BytesIO(content))


@pytest.mark.parametrize("mode", ["traversal", "corrupt", "migration", "foreign-key"])
def test_untrusted_or_inconsistent_backup_is_rejected_without_partial_restore(
    admin_store, tmp_path, mode
):
    seed(admin_store)
    source, damaged = tmp_path / "good.tar.gz", tmp_path / "bad.tar.gz"
    backup(admin_store, source, app_stopped=True)
    empty_database(admin_store.db)

    def transform(name, content):
        if mode == "traversal" and name.startswith("objects/"):
            return "../escape", content
        if mode == "corrupt" and name.startswith("objects/"):
            return name, b"X" * len(content)
        if name == "manifest.json" and mode in {"migration", "foreign-key"}:
            metadata = json.loads(content)
            if mode == "migration":
                metadata["migrations"] = ["wrong-version.sql"]
            else:
                metadata["tables"]["projects"][0]["owner_id"] = "missing-owner"
            return name, json.dumps(metadata).encode()
        return name, content

    rewrite_archive(source, damaged, transform)
    with pytest.raises(Exception):
        restore_backup(admin_store, damaged, app_stopped=True)
    assert not (tmp_path / "escape").exists()
    with admin_store.connect() as connection:
        assert not connection.execute("SELECT 1 FROM beatit.users").fetchone()
        assert not connection.execute("SELECT 1 FROM beatit.projects").fetchone()
    assert not [
        path for path in (admin_store.objects.root / "restore").rglob("*") if path.is_file()
    ]


def test_storage_failure_leaves_backup_unpublished(admin_store, tmp_path):
    seed(admin_store)
    admin_store.objects.fail_reads = True
    destination = tmp_path / "backup.tar.gz"
    with pytest.raises(Exception):
        backup(admin_store, destination, app_stopped=True)
    assert not destination.exists()


def test_outstanding_remote_calls_and_worker_claims_are_never_overwritten(admin_store, tmp_path):
    _, identifier, _ = seed(admin_store)
    destination = tmp_path / "backup.tar.gz"
    with admin_store.connect() as connection:
        connection.execute(
            "INSERT INTO beatit.remote_calls VALUES (%s,%s,%s,%s)",
            ("unreconciled-source-call", identifier, uuid4().hex, time.time()),
        )
    with pytest.raises(ValueError, match="remote calls"):
        backup(admin_store, destination, app_stopped=True)
    with admin_store.connect() as connection:
        connection.execute("DELETE FROM beatit.remote_calls")
    backup(admin_store, destination, app_stopped=True)
    empty_database(admin_store.db)
    with admin_store.connect() as connection:
        connection.execute(
            "INSERT INTO beatit.remote_calls VALUES (%s,%s,%s,%s)",
            ("unreconciled-destination-call", identifier, uuid4().hex, time.time()),
        )
    with pytest.raises(ValueError, match="empty"):
        restore_backup(admin_store, destination, app_stopped=True)
    with admin_store.connect() as connection:
        connection.execute("DELETE FROM beatit.remote_calls")
        connection.execute("UPDATE beatit.worker_slot SET token=%s WHERE id=1", (uuid4().hex,))
    with pytest.raises(ValueError, match="idle worker"):
        restore_backup(admin_store, destination, app_stopped=True)
    with admin_store.connect() as connection:
        assert not connection.execute("SELECT 1 FROM beatit.users").fetchone()
