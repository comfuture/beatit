"""Explicit-owner, restart-safe import of the old SQLite/JSON installation.

The original directory is opened read-only and never modified. Keep a separate
backup of the entire source directory before import. Imported jobs never enqueue
inference automatically; interrupted legacy work is imported as failed.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .store import QUEUED, TERMINAL, JobStore


def import_legacy(store: JobStore, source: Path, owner_id: str, source_name: str):
    if not owner_id or not source_name.strip():
        raise ValueError("An explicit owner ID and stable source name are required.")
    source = source.resolve()
    database = source / "jobs.sqlite3"
    if not database.is_file():
        raise ValueError("Legacy jobs.sqlite3 not found.")
    source_key = hashlib.sha256(source_name.encode()).hexdigest()
    with store.connect() as connection:
        if not connection.execute("SELECT 1 FROM beatit.users WHERE id=%s", (owner_id,)).fetchone():
            raise ValueError("The explicit owner must sign in with Google before legacy import.")
    imported, skipped = [], []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        rows = legacy.execute("SELECT * FROM jobs ORDER BY created").fetchall()
    for row in rows:
        legacy_id = store._identifier(row["id"])
        with store.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM beatit.legacy_imports WHERE source=%s AND legacy_id=%s",
                (source_key, legacy_id),
            ).fetchone()
        if existing:
            if existing["owner_id"] != owner_id:
                raise ValueError(
                    "This source was already imported for another owner; ownership is immutable."
                )
            skipped.append(existing["job_id"])
            continue
        directory = source / legacy_id
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Missing or unsafe legacy directory: {legacy_id}")
        identifier = hashlib.sha256((source_key + ":" + legacy_id).encode()).hexdigest()[:32]
        request = store._read(directory / "request.json", {})
        result = store._read(directory / "result.json", None)
        progress = store._read(directory / "progress.json", QUEUED)
        manifests, payloads = store._capture(identifier, directory)
        log = directory / "worker.log"
        if log.is_file():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 64000))
                payloads["worker.log"] = stream.read().decode("utf-8", errors="replace")
        status = row["status"] if row["status"] in TERMINAL else "failed"
        if status == "completed" and result is None:
            raise ValueError(f"Completed legacy job is missing its result: {legacy_id}")
        now = time.time()
        with store.connect() as connection:
            # The source key lock makes concurrent reruns safe before PK inserts.
            lock_name = "beatit:import:" + source_key[:45]
            acquired = connection.execute(
                "SELECT GET_LOCK(%s, 15) AS acquired", (lock_name,)
            ).fetchone()["acquired"]
            if acquired != 1:
                raise RuntimeError("Timed out acquiring import lock.")
            try:
                existing = connection.execute(
                    "SELECT * FROM beatit.legacy_imports WHERE source=%s AND legacy_id=%s FOR UPDATE",
                    (source_key, legacy_id),
                ).fetchone()
                if existing:
                    if existing["owner_id"] != owner_id:
                        raise ValueError("This source was already imported for another owner.")
                    skipped.append(existing["job_id"])
                    continue
                connection.execute(
                    "INSERT INTO beatit.projects VALUES (%s,%s,%s,%s,%s)",
                    (identifier, owner_id, row["filename"], row["created"], now),
                )
                error = (
                    row["error"]
                    if row["status"] in TERMINAL
                    else "Imported interrupted legacy work; retry explicitly."
                )
                connection.execute(
                    """INSERT INTO beatit.jobs
                       (id,project_id,owner_id,filename,status,created,updated,queued_at,
                        request,progress,result,payloads,error)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        identifier,
                        identifier,
                        owner_id,
                        row["filename"],
                        status,
                        row["created"],
                        now,
                        now,
                        json.dumps(request),
                        json.dumps(progress),
                        json.dumps(result) if result is not None else None,
                        json.dumps(payloads),
                        error,
                    ),
                )
                connection.execute(
                    "INSERT IGNORE INTO beatit.owner_schedule(owner_id) VALUES (%s)", (owner_id,)
                )
                store._write_manifests(connection, identifier, manifests)
                if result and result.get("generation"):
                    connection.execute(
                        "INSERT INTO beatit.revisions VALUES (%s,%s,%s,%s,%s,%s)",
                        (
                            identifier,
                            result["generation"],
                            json.dumps(result),
                            json.dumps(request),
                            json.dumps(payloads),
                            now,
                        ),
                    )
                connection.execute(
                    "INSERT INTO beatit.legacy_imports VALUES (%s,%s,%s,%s)",
                    (source_key, legacy_id, owner_id, identifier),
                )
                # Commit before releasing this connection-scoped advisory lock.
                connection.raw.commit()
                imported.append(identifier)
            finally:
                connection.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
    return {"source": source_name, "owner_id": owner_id, "imported": imported, "skipped": skipped}


def rollback_import(store: JobStore, source_name: str, owner_id: str):
    """Remove this import only; never touch the original backup or other owners."""
    source_key = hashlib.sha256(source_name.encode()).hexdigest()
    with store.connect() as connection:
        imports = connection.execute(
            "SELECT * FROM beatit.legacy_imports WHERE source=%s", (source_key,)
        ).fetchall()
    if any(row["owner_id"] != owner_id for row in imports):
        raise ValueError("Import owner does not match; refusing rollback.")
    for row in imports:
        store.delete(row["job_id"], owner_id=owner_id)
        with store.connect() as connection:
            # Upload admission takes this same parent lock before attaching a
            # job. Check emptiness after acquiring it so a concurrent upload
            # cannot be erased by the project's ON DELETE CASCADE.
            project = connection.execute(
                "SELECT id FROM beatit.projects WHERE id=%s AND owner_id=%s FOR UPDATE",
                (row["job_id"], owner_id),
            ).fetchone()
            if (
                project
                and not connection.execute(
                    "SELECT 1 FROM beatit.jobs WHERE project_id=%s LIMIT 1 FOR UPDATE",
                    (project["id"],),
                ).fetchone()
            ):
                connection.execute(
                    "DELETE FROM beatit.projects WHERE id=%s AND owner_id=%s",
                    (project["id"], owner_id),
                )
    return {"removed": len(imports)}
