"""Offline, explicit-owner data operations for managed MySQL and object storage.

Backups contain personal data and source recordings. Protect them as secrets.
Stop every application instance before backup, restore, import, or rollback.
"""

import argparse
import hashlib
import io
import json
import os
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .legacy_import import import_legacy, rollback_import
from .object_storage import file_digest
from .store import JobStore

FORMAT = "beatit-backup-v1"
MAX_MANIFEST = 128 * 1024 * 1024
TABLES = {
    "users": ("id", "google_sub", "email", "name", "picture", "created"),
    "projects": ("id", "owner_id", "title", "created", "updated"),
    "jobs": (
        "id",
        "project_id",
        "owner_id",
        "filename",
        "status",
        "created",
        "updated",
        "queued_at",
        "request",
        "progress",
        "result",
        "payloads",
        "error",
        "claim_token",
        "lease_until",
        "attempt",
    ),
    "owner_schedule": ("owner_id", "last_served"),
    "artifacts": ("job_id", "path", "object_key", "size", "sha256"),
    "revisions": ("job_id", "generation", "result", "request", "payloads", "created"),
    "legacy_imports": ("source", "legacy_id", "owner_id", "job_id"),
}
JSON_COLUMNS = {"request", "progress", "result", "payloads"}


def _offline(app_stopped):
    if not app_stopped:
        raise ValueError("Stop all Beatit application instances, then pass --app-stopped.")


def _migrations(connection):
    return [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM beatit.schema_migrations ORDER BY name"
        ).fetchall()
    ]


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported backup value: {type(value).__name__}")


def probe(store):
    """Measure real database reads/locking and an S3 put/get/delete round trip."""
    started = time.perf_counter()
    with store.connect() as connection:
        version = connection.execute("SELECT VERSION() AS version").fetchone()["version"]
        if connection.execute("SELECT 1 AS value").fetchone()["value"] != 1:
            raise RuntimeError("Database read probe returned an unexpected value.")
    select_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    with store.connect() as connection:
        connection.execute("SELECT id FROM beatit.worker_slot WHERE id=1 FOR UPDATE").fetchone()
        connection.raw.rollback()
    transaction_ms = (time.perf_counter() - started) * 1000
    key = "probes/" + uuid4().hex
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="beatit-probe-") as temporary:
        source, restored = Path(temporary) / "source", Path(temporary) / "restored"
        source.write_bytes(os.urandom(4096))
        try:
            store.objects.put(key, source)
            store.objects.get(key, restored)
            if file_digest(source) != file_digest(restored):
                raise ValueError("Object storage round-trip checksum did not match.")
        finally:
            store.objects.delete(key)
    return {
        "database": {
            "engine": "mysql",
            "version": version,
            "select_ms": round(select_ms, 3),
            "locking_transaction_ms": round(transaction_ms, 3),
        },
        "objects": {
            "bytes": 4096,
            "sha256_match": True,
            "put_get_delete_ms": round((time.perf_counter() - started) * 1000, 3),
        },
    }


def backup(store, destination: Path, *, app_stopped=False):
    _offline(app_stopped)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Backup destination already exists; choose a new filename.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="beatit-backup-") as temporary:
        temporary = Path(temporary)
        with store.connect() as connection:
            # The ordinary reads below all see the same InnoDB snapshot. Locking
            # the global slot prevents a worker from starting during the export.
            connection.raw.rollback()
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            connection.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            slot = connection.execute(
                "SELECT * FROM beatit.worker_slot WHERE id=1 FOR UPDATE"
            ).fetchone()
            if connection.execute("SELECT 1 FROM beatit.remote_calls LIMIT 1").fetchone():
                raise ValueError("Cancel and reconcile outstanding remote calls before backup.")
            tables = {
                name: connection.execute(
                    f"SELECT {','.join(columns)} FROM beatit.{name}"
                ).fetchall()
                for name, columns in TABLES.items()
            }
            if slot and slot["lease_until"] > time.time():
                raise ValueError(
                    "An active worker lease remains; stop the app and wait for the lease to expire."
                )
            metadata = {
                "format": FORMAT,
                "created": datetime.now(timezone.utc).isoformat(),
                "migrations": _migrations(connection),
                "tables": tables,
                "objects": [],
            }
            seen = {}
            for row in tables["artifacts"]:
                key = row["object_key"]
                if key in seen:
                    if (row["sha256"], row["size"]) != seen[key]:
                        raise ValueError("Conflicting manifests refer to the same object key.")
                    continue
                seen[key] = (row["sha256"], row["size"])
                member = "objects/" + hashlib.sha256(key.encode()).hexdigest()
                path = temporary / member
                path.parent.mkdir(exist_ok=True)
                store.objects.get(key, path)
                if path.stat().st_size != row["size"] or file_digest(path) != row["sha256"]:
                    raise ValueError("Stored object does not match its durable artifact manifest.")
                metadata["objects"].append(
                    {
                        "key": key,
                        "member": member,
                        "size": row["size"],
                        "sha256": row["sha256"],
                    }
                )
        encoded = json.dumps(metadata, default=_json_value, ensure_ascii=False).encode()
        descriptor, pending = tempfile.mkstemp(prefix=".beatit-backup-", dir=destination.parent)
        os.close(descriptor)
        try:
            with tarfile.open(pending, "w:gz") as archive:
                member = tarfile.TarInfo("manifest.json")
                member.size, member.mode = len(encoded), 0o600
                archive.addfile(member, io.BytesIO(encoded))
                for item in metadata["objects"]:
                    archive.add(temporary / item["member"], arcname=item["member"], recursive=False)
            # Exclusive creation avoids replacing an existing backup in a race.
            os.link(pending, destination)
        finally:
            Path(pending).unlink(missing_ok=True)
    return {"backup": str(destination), "jobs": len(tables["jobs"]), "objects": len(seen)}


def _read_archive(source, temporary):
    with tarfile.open(source, "r:*") as archive:
        members = {}
        for member in archive.getmembers():
            parts = PurePosixPath(member.name)
            if (
                not member.isfile()
                or parts.is_absolute()
                or ".." in parts.parts
                or "\\" in member.name
                or member.name in members
            ):
                raise ValueError("Unsafe or duplicate backup archive member.")
            members[member.name] = member
        manifest = members.get("manifest.json")
        if manifest is None or manifest.size > MAX_MANIFEST:
            raise ValueError("Missing or oversized backup manifest.")
        with archive.extractfile(manifest) as stream:
            metadata = json.load(stream)
        if metadata.get("format") != FORMAT or set(metadata.get("tables", {})) != set(TABLES):
            raise ValueError("Unsupported or incomplete backup format.")
        expected = {"manifest.json"}
        objects = {}
        for item in metadata.get("objects", []):
            key, name = item["key"], item["member"]
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 512
                or name != "objects/" + hashlib.sha256(key.encode()).hexdigest()
                or key in objects
                or name not in members
                or not isinstance(item["size"], int)
                or item["size"] < 0
                or members[name].size != item["size"]
            ):
                raise ValueError("Invalid object entry in backup manifest.")
            expected.add(name)
            destination = temporary / name
            destination.parent.mkdir(exist_ok=True)
            with archive.extractfile(members[name]) as stream, destination.open("wb") as target:
                remaining = item["size"]
                digest = hashlib.sha256()
                while remaining:
                    block = stream.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError("Truncated backup object.")
                    digest.update(block)
                    target.write(block)
                    remaining -= len(block)
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("Backup object checksum did not match.")
            objects[key] = item
        if set(members) != expected:
            raise ValueError("Unreferenced files are not allowed in a Beatit backup.")
        for name, columns in TABLES.items():
            rows = metadata["tables"][name]
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or set(row) != set(columns) for row in rows
            ):
                raise ValueError("Backup row schema does not match the current application.")
        referenced = set()
        for row in metadata["tables"]["artifacts"]:
            item = objects.get(row["object_key"])
            if item is None or (item["size"], item["sha256"]) != (row["size"], row["sha256"]):
                raise ValueError("Backup artifact manifest does not match its object.")
            referenced.add(row["object_key"])
        if referenced != set(objects):
            raise ValueError("Backup contains unreferenced objects.")
    return metadata, objects


def restore_backup(store, source: Path, *, app_stopped=False):
    _offline(app_stopped)
    uploaded = []
    committed = False
    with tempfile.TemporaryDirectory(prefix="beatit-restore-") as temporary:
        temporary = Path(temporary)
        metadata, objects = _read_archive(source, temporary)
        namespace = "restore/" + uuid4().hex + "/"
        remapped = {key: namespace + hashlib.sha256(key.encode()).hexdigest() for key in objects}
        try:
            with store.connect() as connection:
                slot = connection.execute(
                    "SELECT * FROM beatit.worker_slot WHERE id=1 FOR UPDATE"
                ).fetchone()
                if slot and (slot["job_id"] or slot["token"] or slot["lease_until"] > time.time()):
                    raise ValueError(
                        "Restore requires an idle worker slot with no outstanding claim."
                    )
                for table in (
                    *TABLES,
                    "sessions",
                    "oauth_states",
                    "object_garbage",
                    "remote_calls",
                ):
                    if connection.execute(f"SELECT 1 FROM beatit.{table} LIMIT 1").fetchone():
                        raise ValueError(
                            "Restore requires an empty application database; no existing data was changed."
                        )
                if metadata["migrations"] != _migrations(connection):
                    raise ValueError(
                        "Backup migrations differ; restore with the matching application version first."
                    )
                for key, item in objects.items():
                    uploaded.append(remapped[key])
                    store.objects.put(remapped[key], temporary / item["member"])
                for table, columns in TABLES.items():
                    for original in metadata["tables"][table]:
                        row = dict(original)
                        if table == "users":
                            row["created"] = datetime.fromisoformat(row["created"])
                        if table == "artifacts":
                            row["object_key"] = remapped[row["object_key"]]
                        if table == "jobs":
                            row["claim_token"], row["lease_until"] = None, None
                            if row["status"] == "running":
                                row["status"] = "failed"
                                row["error"] = "Restored interrupted work; retry explicitly."
                        values = [
                            json.dumps(row[column])
                            if column in JSON_COLUMNS and row[column] is not None
                            else row[column]
                            for column in columns
                        ]
                        connection.execute(
                            f"INSERT INTO beatit.{table} ({','.join(columns)}) VALUES "
                            f"({','.join('%s' for _ in columns)})",
                            values,
                        )
                connection.execute(
                    "UPDATE beatit.worker_slot SET job_id=NULL,token=NULL,lease_until=0 WHERE id=1"
                )
            committed = True
        finally:
            if not committed:
                for key in uploaded:
                    try:
                        store.objects.delete(key)
                    except Exception:
                        pass  # Unreferenced restore/ objects may be cleaned up after an outage.
    return {"restored_jobs": len(metadata["tables"]["jobs"]), "restored_objects": len(objects)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Beatit managed data operations")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("probe", help="Measure DB locking and private object round trip")
    for name in ("backup", "restore"):
        command = subcommands.add_parser(name)
        command.add_argument("archive", type=Path)
        command.add_argument("--app-stopped", action="store_true", required=True)
    command = subcommands.add_parser("import-legacy")
    command.add_argument("source", type=Path)
    command.add_argument("--owner-id", required=True)
    command.add_argument("--source-name", required=True)
    command.add_argument("--app-stopped", action="store_true", required=True)
    command = subcommands.add_parser("rollback-import")
    command.add_argument("--owner-id", required=True)
    command.add_argument("--source-name", required=True)
    command.add_argument("--app-stopped", action="store_true", required=True)
    arguments = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="beatit-admin-") as temporary:
            store = JobStore(Path(temporary))
            try:
                if arguments.command == "probe":
                    result = probe(store)
                elif arguments.command == "backup":
                    result = backup(store, arguments.archive, app_stopped=arguments.app_stopped)
                elif arguments.command == "restore":
                    result = restore_backup(
                        store, arguments.archive, app_stopped=arguments.app_stopped
                    )
                elif arguments.command == "import-legacy":
                    result = import_legacy(
                        store, arguments.source, arguments.owner_id, arguments.source_name
                    )
                else:
                    result = rollback_import(store, arguments.source_name, arguments.owner_id)
            finally:
                store.db.close()
        print(json.dumps(result, ensure_ascii=False))
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    except Exception as error:
        # Driver exception messages can contain connection parameters. Only emit
        # the exception class; server logs/secret-aware operators can investigate.
        parser.exit(
            1, f"Data operation failed ({type(error).__name__}); no credentials were printed.\n"
        )


if __name__ == "__main__":
    main()
