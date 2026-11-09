"""Durable staging and fenced collection of objects uploaded before SQL publication."""

import hashlib
import logging
from uuid import uuid4

# Long captures can stage several large media objects. Definite rejections expire
# ready objects immediately; crashes/outages recover conservatively after a day.
STAGED_UPLOAD_SECONDS = 24 * 60 * 60
FAILED_UPLOAD_SECONDS = 5 * 60


def upload_lock_name(database, key):
    return "beatit:put:" + hashlib.sha256((database.name + ":" + key).encode()).hexdigest()[:48]


def put_staged_object(store, key, path):
    token = uuid4().hex
    lock_name = upload_lock_name(store.db, key)
    # This bounded pool is separate from API authentication and job heartbeats.
    with store.db.object_upload_connection() as connection:
        locked = connection.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,)).fetchone()[
            "acquired"
        ]
        if locked != 1:
            raise RuntimeError("An object upload is already active for this key")
        try:
            connection.execute(
                """INSERT INTO beatit.pending_object_uploads(object_key,token,state,expires)
                   VALUES (%s,%s,'uploading',UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))+%s)""",
                (key, token, STAGED_UPLOAD_SECONDS),
            )
            # The journal must be durable before the first byte reaches storage.
            connection.raw.commit()
            try:
                store.objects.put(key, path)
            except Exception:
                connection.execute(
                    """UPDATE beatit.pending_object_uploads SET state='ready',
                       expires=UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))+%s WHERE object_key=%s AND token=%s""",
                    (FAILED_UPLOAD_SECONDS, key, token),
                )
                connection.raw.commit()
                raise
            connection.execute(
                """UPDATE beatit.pending_object_uploads SET state='ready',
                   expires=UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))+%s WHERE object_key=%s AND token=%s""",
                (STAGED_UPLOAD_SECONDS, key, token),
            )
            connection.raw.commit()
        finally:
            try:
                connection.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                # A lost connection releases its server lock. The committed
                # journal remains and an unconfirmed upload is never published.
                logging.warning(
                    "Object upload lock connection was lost; durable cleanup remains pending"
                )
    return token


def abandon_uploads(store, manifests):
    pending = [item for item in manifests if item.get("upload_token")]
    if not pending:
        return
    try:
        with store.connect() as connection:
            for item in pending:
                connection.execute(
                    """UPDATE beatit.pending_object_uploads
                       SET expires=UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))
                       WHERE object_key=%s AND token=%s AND state='ready'""",
                    (item["object_key"], item["upload_token"]),
                )
    except Exception:
        logging.warning("Object staging cleanup deferred until its durable journal expires")


def lock_publication(connection, manifest):
    key, token = manifest["object_key"], manifest.get("upload_token")
    if token:
        pending = connection.execute(
            "SELECT token,state FROM beatit.pending_object_uploads WHERE object_key=%s FOR UPDATE",
            (key,),
        ).fetchone()
        if pending and pending["token"] == token and pending["state"] == "ready":
            return
    # Reused objects must retain a committed reference; stale snapshots cannot
    # resurrect a retired key after the collector has already removed its bytes.
    if not connection.execute(
        "SELECT 1 FROM beatit.artifacts WHERE object_key=%s LIMIT 1 FOR UPDATE", (key,)
    ).fetchone():
        raise ValueError("Staged object expired before publication; retry the operation")


def finish_publication(connection, manifest):
    if manifest.get("upload_token"):
        connection.execute(
            "DELETE FROM beatit.pending_object_uploads WHERE object_key=%s AND token=%s",
            (manifest["object_key"], manifest["upload_token"]),
        )


def collect_staged_objects(store):
    with store.connect() as connection:
        candidates = connection.execute(
            """SELECT object_key FROM beatit.pending_object_uploads
               WHERE expires<=UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) ORDER BY expires LIMIT 100"""
        ).fetchall()
    for candidate in candidates:
        key = candidate["object_key"]
        try:
            with store.connect() as connection:
                pending = connection.execute(
                    """SELECT * FROM beatit.pending_object_uploads WHERE object_key=%s
                       AND expires<=UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) FOR UPDATE""",
                    (key,),
                ).fetchone()
                if not pending:
                    continue
                lock_name = upload_lock_name(store.db, key)
                locked = connection.execute(
                    "SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,)
                ).fetchone()["acquired"]
                if locked != 1:
                    continue  # An in-flight PUT stays protected even past expiry.
                try:
                    referenced = connection.execute(
                        "SELECT 1 FROM beatit.artifacts WHERE object_key=%s LIMIT 1", (key,)
                    ).fetchone()
                    if not referenced:
                        store.objects.delete(key)
                    # Publication holds this same row lock and removes the journal
                    # with its new reference. A collector winner fences publication.
                    connection.execute(
                        "DELETE FROM beatit.pending_object_uploads WHERE object_key=%s", (key,)
                    )
                finally:
                    connection.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            logging.warning("Staged object cleanup will be retried from its durable journal")
            break
