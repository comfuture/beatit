"""Explicit real-MySQL fixtures; production authentication is never bypassed."""

import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient


def pytest_collection_modifyitems(items):
    # Fixture dependencies are the source of truth, including auth tests and
    # mixed unit/API modules. CI must execute every DB-dependent case.
    for item in items:
        if {"mysql_database", "auth_db"}.intersection(item.fixturenames):
            item.add_marker(pytest.mark.mysql)


class DiskObjects:
    """Object protocol test double whose storage survives app scratch recreation."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.fail_reads = False
        self.fail_writes = False
        self.fail_deletes = False

    def put(self, key, path):
        if self.fail_writes:
            raise EndpointConnectionError(endpoint_url="https://objects.invalid")
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)

    def get(self, key, path):
        if self.fail_reads:
            raise EndpointConnectionError(endpoint_url="https://objects.invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / key, path)

    def delete(self, key):
        if self.fail_deletes:
            raise EndpointConnectionError(endpoint_url="https://objects.invalid")
        (self.root / key).unlink(missing_ok=True)


@pytest.fixture
def mysql_database():
    from transcription.database import Database

    url = os.environ.get("BEATIT_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "MySQL integration tests require BEATIT_TEST_DATABASE_URL pointing to "
            "a disposable database named beatit_test; see docs/multi-user-deployment.md."
        )
    if urlsplit(url).path.rstrip("/") != "/beatit_test":
        pytest.fail("Refusing to reset a database whose name is not beatit_test")
    database = Database(url)
    with database.connection() as connection:
        tables = [next(iter(row.values())) for row in connection.execute("SHOW TABLES").fetchall()]
        connection.execute("SET FOREIGN_KEY_CHECKS=0")
        try:
            for table in tables:
                if table != "schema_migrations":
                    quoted = table.replace("`", "``")
                    connection.execute(f"TRUNCATE TABLE `{quoted}`")
            connection.execute("INSERT INTO worker_slot(id) VALUES (1)")
        finally:
            connection.execute("SET FOREIGN_KEY_CHECKS=1")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def disk_objects(tmp_path_factory):
    return DiskObjects(tmp_path_factory.mktemp("private-objects"))


@pytest.fixture
def authenticated_app(mysql_database, disk_objects, monkeypatch):
    from transcription.app import create_app
    from transcription.auth import AuthService, AuthSettings
    from transcription.store import JobStore

    monkeypatch.setenv("DRUM_EXECUTION_BACKEND", "local")
    monkeypatch.delenv("BEATIT_PUBLIC_URL", raising=False)

    def factory(root):
        store = JobStore(root, db=mysql_database, objects=disk_objects)
        service = AuthService(
            mysql_database,
            AuthSettings(
                client_id="test-client", client_secret="test-secret", public_url="http://localhost"
            ),
        )
        token, user = service.create_session(
            {
                "sub": "test-google-owner-A",
                "email": "a@example.test",
                "email_verified": True,
                "name": "Owner A",
            }
        )
        app = create_app(root, start_worker=False, store=store, auth_service=service)
        app.state.test_cookie = {service.settings.cookie_name: token}
        app.state.test_user = user
        return app

    return factory


@pytest.fixture
def authenticated_client():
    def factory(app, **kwargs):
        return TestClient(app, cookies=app.state.test_cookie, **kwargs)

    return factory
