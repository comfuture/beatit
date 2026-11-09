"""Managed MySQL 8 connection pool with ordered, restart-safe migrations."""

import json
import os
import re
import ssl
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy.pool import QueuePool

_JSON_COLUMNS = {"request", "progress", "result", "payloads"}


class Result:
    def __init__(self, cursor):
        self.rowcount = cursor.rowcount
        self.rows = list(cursor.fetchall()) if cursor.description else []
        for row in self.rows:
            for key in _JSON_COLUMNS & row.keys():
                if isinstance(row[key], (str, bytes)):
                    row[key] = json.loads(row[key])
        self.index = 0

    def fetchone(self):
        if self.index >= len(self.rows):
            return None
        row = self.rows[self.index]
        self.index += 1
        return row

    def fetchall(self):
        rows = self.rows[self.index :]
        self.index = len(self.rows)
        return rows


class Connection:
    def __init__(self, connection, database):
        self.raw = connection
        self.database = database

    def execute(self, query, params=None, **kwargs):
        # "beatit" is a logical namespace; never select unrelated databases.
        query = query.replace("beatit.", f"`{self.database}`.")
        with self.raw.cursor() as cursor:
            cursor.execute(query, params or ())
            return Result(cursor)


class _Checkout:
    def __init__(self, database):
        self.database = database
        self.raw = None

    def __enter__(self):
        self.raw = self.database.pool.connect()
        try:
            self.raw.ping(reconnect=False)
            self.raw.begin()
        except Exception:
            self.raw.invalidate()
            self.raw.close()
            self.raw = self.database.pool.connect()
            self.raw.begin()
        return Connection(self.raw, self.database.name)

    def __exit__(self, exc_type, exc, tb):
        try:
            self.raw.rollback() if exc_type else self.raw.commit()
        finally:
            self.raw.close()


class Database:
    def __init__(self, url: str | None = None):
        url = url or os.environ.get("BEATIT_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("BEATIT_DATABASE_URL is required; configure managed MySQL 8.")
        self.url = url
        parsed = urlparse(url)
        if parsed.scheme not in {"mysql", "mysql+pymysql"}:
            raise RuntimeError("BEATIT_DATABASE_URL must use mysql:// (managed MySQL 8).")
        self.name = unquote(parsed.path.lstrip("/"))
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.name):
            raise ValueError("Use a dedicated MySQL database with a simple database name.")
        query = parse_qs(parsed.query)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        context = None
        if ca := query.get("ssl_ca", [None])[0]:
            context = ssl.create_default_context(cafile=ca)
        elif not local:
            context = ssl.create_default_context()

        # A local endpoint can be a Cloudflare Access TCP tunnel. Its remote hop
        # must be protected by Access; direct OCI connections validate server TLS.
        def connect():
            connection = pymysql.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 3306,
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                database=self.name,
                charset="utf8mb4",
                cursorclass=DictCursor,
                connect_timeout=10,
                read_timeout=15,
                write_timeout=15,
                ssl=context,
                autocommit=False,
                init_command="SET time_zone='+00:00'",
            )
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            return connection

        self.pool = QueuePool(
            connect,
            pool_size=int(os.environ.get("BEATIT_DB_POOL_SIZE", "6")),
            max_overflow=0,
            timeout=10,
            recycle=300,
        )
        with self.connection() as connection:
            version = connection.execute("SELECT VERSION() AS version").fetchone()["version"]
            if "mariadb" in version.lower() or int(version.split(".")[0]) < 8:
                raise RuntimeError("Beatit requires MySQL 8+ with InnoDB SKIP LOCKED support.")
        self.migrate()

    def connection(self):
        return _Checkout(self)

    def migrate(self):
        # MySQL DDL commits implicitly. The session lock prevents concurrent DDL;
        # idempotent migration statements permit restart after a partial migration.
        with self.connection() as connection:
            acquired = connection.execute(
                "SELECT GET_LOCK(%s, 15) AS acquired", (self.name + ":beatit:migrate",)
            ).fetchone()["acquired"]
            if acquired != 1:
                raise RuntimeError("Timed out waiting for Beatit schema migration lock.")
            try:
                connection.execute("""CREATE TABLE IF NOT EXISTS beatit.schema_migrations (
                    name VARCHAR(190) PRIMARY KEY,
                    applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                ) ENGINE=InnoDB""")
                for migration in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                    if not connection.execute(
                        "SELECT 1 FROM beatit.schema_migrations WHERE name=%s", (migration.name,)
                    ).fetchone():
                        sql = "\n".join(
                            line
                            for line in migration.read_text().splitlines()
                            if not line.lstrip().startswith("--")
                        )
                        for statement in sql.split(";"):
                            if statement.strip():
                                connection.execute(statement)
                        connection.execute(
                            "INSERT INTO beatit.schema_migrations(name) VALUES (%s)",
                            (migration.name,),
                        )
                        connection.raw.commit()
            finally:
                connection.execute("SELECT RELEASE_LOCK(%s)", (self.name + ":beatit:migrate",))

    def close(self):
        self.pool.dispose()
