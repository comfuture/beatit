import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from authlib.jose import JoseError, JsonWebKey, JsonWebToken
from authlib.oauth2 import OAuth2Error
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import StreamingResponse

from transcription.auth import (
    SESSION_ABSOLUTE_SECONDS,
    SESSION_SECONDS,
    AuthService,
    AuthSettings,
    install_auth,
    login_return_path,
    require_user,
    state_return_path,
    token_hash,
)


@pytest.fixture
def auth_db(mysql_database):
    return mysql_database


@pytest.fixture
def service(auth_db):
    return AuthService(
        auth_db,
        AuthSettings("beatit-client", "test-client-secret", "https://beatit.example.test"),
    )


@pytest.fixture
def signing_key():
    return JsonWebKey.generate_key("RSA", 2048, options={"kid": "beatit-test"}, is_private=True)


def identity(**values):
    return {
        "iss": "https://accounts.google.com",
        "aud": "beatit-client",
        "sub": "test-auth-" + uuid4().hex,
        "email": "owner@example.test",
        "email_verified": True,
        "name": "Owner",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": "expected-nonce",
        **values,
    }


def auth_app(service):
    app = FastAPI()
    install_auth(app, service.db, service=service)

    @app.get("/private")
    async def private(user=Depends(require_user)):
        return {"id": user["id"]}

    return app


def cookie_request(service, token):
    return Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"{service.settings.cookie_name}={token}".encode())],
        }
    )


def test_missing_configuration_never_grants_identity():
    service = AuthService(None, AuthSettings())
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        assert client.get("/api/auth/me").json() == {"user": None}
        assert client.get("/private").status_code == 401
        response = client.get("/api/auth/google")
        assert response.status_code == 503
        assert "GOOGLE_CLIENT_ID" in response.json()["detail"]


@pytest.mark.parametrize(
    "value",
    [
        "http://beatit.example.test",
        "https://user:secret@example.test",
        "https://example.test/another/path",
        "https://example.test?redirect=elsewhere",
        "https://example.test#fragment",
        "//example.test",
    ],
)
def test_public_url_must_be_an_explicit_secure_origin(monkeypatch, value):
    monkeypatch.setenv("BEATIT_PUBLIC_URL", value)
    with pytest.raises(ValueError):
        AuthSettings.from_env()


def test_localhost_uses_separate_development_cookie(monkeypatch):
    monkeypatch.setenv("BEATIT_PUBLIC_URL", "http://localhost:8000/")
    settings = AuthSettings.from_env()
    assert settings.callback_url == "http://localhost:8000/api/auth/callback"
    assert settings.cookie_name == "beatit-session"
    assert not settings.secure


@pytest.mark.parametrize(
    "target",
    [
        "https://attacker.example/",
        "//attacker.example/",
        "/\\attacker.example",
        "/api/auth/logout",
        "/sessions/" + "a" * 31,
        "/sessions/" + "A" * 32,
        "/sessions/" + "a" * 32 + "?next=https://attacker.example",
        "/sessions/" + "a" * 32 + "/../other",
    ],
)
def test_login_return_target_rejects_external_and_noncanonical_paths(target):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        login_return_path(target)
    assert error.value.status_code == 400


def test_legacy_oauth_state_returns_to_workspace():
    assert state_return_path("legacy-random-state") == "/"
    assert state_return_path("random.https://attacker.example") == "/"


def test_session_identity_is_stable_revocable_and_not_an_email_key(service):
    claims = identity()
    token, first = service.create_session(claims)
    assert service.current_user(cookie_request(service, token))["id"] == first["id"]
    with service.db.connection() as connection:
        stored = connection.execute(
            "SELECT token_hash FROM beatit.sessions WHERE user_id=%s", (first["id"],)
        ).fetchone()
        assert stored["token_hash"] == token_hash(token)
        assert stored["token_hash"] != token
    next_token, same = service.create_session({**claims, "email": "changed@example.test"}, token)
    assert first["id"] == same["id"]
    assert same["email"] == "changed@example.test"
    assert service.current_user(cookie_request(service, token)) is None
    other_token, other = service.create_session(identity(email=claims["email"]))
    assert first["id"] != other["id"]
    service.revoke_session(cookie_request(service, next_token))
    assert service.current_user(cookie_request(service, next_token)) is None
    assert service.current_user(cookie_request(service, other_token))["id"] == other["id"]
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.sessions SET expires=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE user_id=%s",
            (other["id"],),
        )
    assert service.current_user(cookie_request(service, other_token)) is None


def test_logout_revokes_database_session_and_clears_cookie(service):
    token, user = service.create_session(identity())
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        client.cookies.set(service.settings.cookie_name, token)
        response = client.get("/api/auth/me")
        assert response.json()["user"]["id"] == user["id"]
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/private").status_code == 200
        response = client.post("/api/auth/logout")
        assert response.status_code == 204
        assert not response.content
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert service.current_user(cookie_request(service, token)) is None
        assert client.get("/private").status_code == 401


def session_row(service, token):
    with service.db.connection() as connection:
        return connection.execute(
            "SELECT *, UTC_TIMESTAMP(6) AS checked_at FROM beatit.sessions WHERE token_hash=%s",
            (token_hash(token),),
        ).fetchone()


def test_valid_session_is_reused_without_cookie_writes_or_public_authentication(
    service, monkeypatch
):
    token, user = service.create_session(identity())
    before = session_row(service, token)
    checked = []
    original = service._session

    def lookup(request):
        checked.append(True)
        return original(request)

    monkeypatch.setattr(service, "_session", lookup)
    app = auth_app(service)

    @app.get("/public")
    def public():
        return {"status": "ok"}

    with TestClient(app, base_url=service.settings.public_url) as client:
        client.cookies.set(service.settings.cookie_name, token)
        assert "set-cookie" not in client.get("/public").headers
        assert checked == []
        for path in ("/private", "/api/auth/me", "/private"):
            response = client.get(path)
            assert response.status_code == 200
            assert "set-cookie" not in response.headers
        assert len(checked) == 3
        assert client.get("/api/auth/me").json()["user"]["id"] == user["id"]
    after = session_row(service, token)
    assert after["expires"] == before["expires"]
    assert after["absolute_expires"] - after["created"] == timedelta(
        seconds=SESSION_ABSOLUTE_SECONDS
    )


def test_next_authenticated_request_renews_the_same_token_once(service):
    token, user = service.create_session(identity())
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.sessions SET expires=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR WHERE token_hash=%s",
            (token_hash(token),),
        )
    with TestClient(auth_app(service), base_url=service.settings.public_url) as client:
        client.cookies.set(service.settings.cookie_name, token)
        response = client.get("/private")
        assert response.json()["id"] == user["id"]
        assert response.cookies.get(service.settings.cookie_name) == token
        cookie = response.headers["set-cookie"]
        assert all(word in cookie for word in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))
        assert "Domain=" not in cookie
        assert response.headers["cache-control"] == "private, no-store"
        renewed = session_row(service, token)
        remaining = (renewed["expires"] - renewed["checked_at"]).total_seconds()
        assert SESSION_SECONDS - 10 < remaining <= SESSION_SECONDS
        assert "set-cookie" not in client.get("/api/auth/me").headers
        assert "set-cookie" not in client.get("/private").headers
        assert session_row(service, token)["expires"] == renewed["expires"]


def test_renewal_is_bounded_by_absolute_session_expiry(service):
    token, _ = service.create_session(identity())
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.sessions SET expires=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR, "
            "absolute_expires=UTC_TIMESTAMP(6)+INTERVAL 3 HOUR WHERE token_hash=%s",
            (token_hash(token),),
        )
    with TestClient(auth_app(service), base_url=service.settings.public_url) as client:
        client.cookies.set(service.settings.cookie_name, token)
        response = client.get("/private")
        assert response.status_code == 200
        row = session_row(service, token)
        assert row["expires"] == row["absolute_expires"]
        age = int(response.headers["set-cookie"].split("Max-Age=", 1)[1].split(";", 1)[0])
        assert 3 * 60 * 60 - 10 < age <= 3 * 60 * 60
        # At the absolute boundary, every later request must not rewrite the cookie.
        assert "set-cookie" not in client.get("/private").headers


@pytest.mark.parametrize("expired", ["expires", "absolute_expires"])
def test_expired_sessions_are_never_refreshed(service, expired):
    token, _ = service.create_session(identity())
    with service.db.connection() as connection:
        connection.execute(
            f"UPDATE beatit.sessions SET {expired}=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND "
            "WHERE token_hash=%s",
            (token_hash(token),),
        )
    with TestClient(auth_app(service), base_url=service.settings.public_url) as client:
        client.cookies.set(service.settings.cookie_name, token)
        response = client.get("/private")
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert client.get("/api/auth/me").json() == {"user": None}


def test_simultaneous_tabs_share_one_bounded_refresh(service, monkeypatch):
    token, user = service.create_session(identity())
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.sessions SET expires=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR, "
            "absolute_expires=UTC_TIMESTAMP(6)+INTERVAL 3 DAY WHERE token_hash=%s",
            (token_hash(token),),
        )
    before = session_row(service, token)
    barrier = threading.Barrier(4)
    calls = threading.local()
    lookup = service._session
    connect = service.db.connection
    writes = []

    def simultaneous_lookup(request):
        row = lookup(request)
        if not getattr(calls, "checked", False):
            calls.checked = True
            barrier.wait(timeout=10)
        return row

    @contextmanager
    def observed_connection():
        with connect() as connection:
            execute = connection.execute

            def observed_execute(query, params=None, **kwargs):
                result = execute(query, params, **kwargs)
                if query.startswith("UPDATE beatit.sessions SET expires="):
                    writes.append(result.rowcount)
                return result

            connection.execute = observed_execute
            yield connection

    monkeypatch.setattr(service, "_session", simultaneous_lookup)
    monkeypatch.setattr(service.db, "connection", observed_connection)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: service.authenticate_request(cookie_request(service, token)), range(4)
            )
        )
    assert [principal["id"] for principal, _ in results] == [user["id"]] * 4
    assert all(age and 0 < age <= 3 * 24 * 60 * 60 for _, age in results)
    assert sorted(writes) == [0, 0, 0, 1]
    row = session_row(service, token)
    assert row["expires"] == row["absolute_expires"] == before["absolute_expires"]
    assert row["token_hash"] == before["token_hash"]


def test_revocation_during_a_refresh_cannot_resurrect_the_session(service, monkeypatch):
    token, _ = service.create_session(identity())
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.sessions SET expires=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR WHERE token_hash=%s",
            (token_hash(token),),
        )
    original = service._session
    first = True

    def revoked_after_validation(request):
        nonlocal first
        row = original(request)
        if first:
            first = False
            service.revoke_session(request)
        return row

    monkeypatch.setattr(service, "_session", revoked_after_validation)
    with TestClient(auth_app(service), base_url=service.settings.public_url) as client:
        client.cookies.set(service.settings.cookie_name, token)
        response = client.get("/private")
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
    assert session_row(service, token) is None


def test_current_user_rechecks_a_previously_authenticated_stream_request(service):
    token, user = service.create_session(identity())
    request = cookie_request(service, token)
    request.state.beatit_user = user
    assert service.current_user(request)["id"] == user["id"]
    service.revoke_session(request)
    assert service.current_user(request) is None


def test_stream_handshake_renews_cookie_before_body_and_rechecks_revocation(service):
    token, user = service.create_session(identity())
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.sessions SET expires=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR WHERE token_hash=%s",
            (token_hash(token),),
        )
    app = auth_app(service)

    @app.get("/api/stream-once")
    async def stream_once(request: Request, principal=Depends(require_user)):
        async def frames():
            assert principal["id"] == user["id"]
            yield "event: snapshot\ndata: {}\n\n"
            await run_in_threadpool(service.revoke_session, request)
            # The handshake's cached principal must not outlive server revocation.
            assert request.state.beatit_user["id"] == user["id"]
            assert await run_in_threadpool(service.current_user, request) is None
            yield "event: session-expired\ndata: {}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    with TestClient(app, base_url=service.settings.public_url) as client:
        client.cookies.set(service.settings.cookie_name, token)
        response = client.get("/api/stream-once")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.cookies.get(service.settings.cookie_name) == token
        assert "HttpOnly" in response.headers["set-cookie"]
        assert response.text == (
            "event: snapshot\ndata: {}\n\nevent: session-expired\ndata: {}\n\n"
        )
        assert client.get("/private").status_code == 401


def test_legacy_sessions_survive_the_renewal_migration_and_partial_ddl_retry(service):
    token, user = service.create_session(identity())
    expiry = session_row(service, token)["expires"]
    with service.db.connection() as connection:
        connection.execute("ALTER TABLE beatit.sessions DROP COLUMN created")
        connection.execute("ALTER TABLE beatit.sessions DROP COLUMN absolute_expires")
        connection.execute(
            "DELETE FROM beatit.schema_migrations WHERE name=%s", ("006_session_renewal.sql",)
        )
    try:
        service.db.migrate()
        row = session_row(service, token)
        assert row["expires"] == expiry
        assert row["absolute_expires"] - row["created"] == timedelta(
            seconds=SESSION_ABSOLUTE_SECONDS
        )
        assert service.current_user(cookie_request(service, token))["id"] == user["id"]
        # Replaying after an implicit DDL commit must keep the existing columns/data.
        with service.db.connection() as connection:
            connection.execute(
                "DELETE FROM beatit.schema_migrations WHERE name=%s", ("006_session_renewal.sql",)
            )
        service.db.migrate()
        assert session_row(service, token)["expires"] == expiry
        # An old application image can still insert its original three-column row.
        legacy_token = "legacy-token-after-rollback"
        with service.db.connection() as connection:
            connection.execute(
                "INSERT INTO beatit.sessions (token_hash,user_id,expires) VALUES (%s,%s,%s)",
                (token_hash(legacy_token), user["id"], expiry),
            )
        assert service.current_user(cookie_request(service, legacy_token))["id"] == user["id"]
    finally:
        service.db.migrate()


async def google_metadata():
    return {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    }


def test_login_state_is_browser_bound_single_use_and_pkce_protected(service, monkeypatch):
    monkeypatch.setattr(service, "_metadata", google_metadata)
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        response = client.get("/api/auth/google", follow_redirects=False)
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["openid email profile"]
        assert query["redirect_uri"] == [service.settings.callback_url]
        assert query["prompt"] == ["select_account"]
        assert len(query["code_challenge"][0]) == 43
        state = query["state"][0]
        browser = client.cookies.get(service.settings.state_cookie_name)
        assert all(
            word in response.headers["set-cookie"]
            for word in ["Secure", "HttpOnly", "SameSite=lax"]
        )
        assert "Domain=" not in response.headers["set-cookie"]
        assert service._consume_state(state, "other-browser") is None
        record = service._consume_state(state, browser)
        assert record["nonce"] == query["nonce"][0]
        assert len(record["verifier"]) >= 43
        assert service._consume_state(state, browser) is None
        assert (
            client.get("/api/auth/callback", params={"state": state, "code": "unused"}).status_code
            == 400
        )


@pytest.mark.parametrize("target", ["/", "/sessions/" + "a" * 32])
def test_oauth_state_expiry_and_cancelled_login(service, monkeypatch, target):
    service._save_state("expired", "browser", "verifier", "nonce")
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.oauth_states SET expires=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE state_hash=%s",
            (token_hash("expired"),),
        )
    assert service._consume_state("expired", "browser") is None
    monkeypatch.setattr(service, "_metadata", google_metadata)
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        response = client.get(
            "/api/auth/google", params={"return_to": target}, follow_redirects=False
        )
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        response = client.get(
            "/api/auth/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert (
            response.headers["location"]
            == service.settings.public_url + target + "?auth_error=cancelled"
        )
        assert client.get("/api/auth/me").json()["user"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.example.test"},
        {"aud": "another-client"},
        {"exp": int(time.time()) - 120},
        {"iat": int(time.time()) + 300},
        {"nonce": "another-nonce"},
        {"nonce": None},
        {"nonce": None, "nonce_supported": False},
        {"email_verified": False},
        {"email_verified": "true"},
        {"sub": ""},
        {"azp": "another-client"},
        {"aud": ["beatit-client", "another-client"]},
    ],
)
def test_google_claim_validation_rejects_invalid_identity(
    service, signing_key, monkeypatch, changes
):
    async def keys(force=False):
        return {"keys": [signing_key.as_dict(is_private=False)]}

    monkeypatch.setattr(service, "_jwks", keys)
    encoded = JsonWebToken(["RS256"]).encode(
        {"alg": "RS256", "kid": "beatit-test"},
        identity(**changes),
        signing_key,
    )
    with pytest.raises((JoseError, ValueError)):
        asyncio.run(service.validate_id_token(encoded, "expected-nonce"))


@pytest.mark.parametrize("target", ["/", "/sessions/" + "a" * 32])
def test_google_callback_validates_signed_token_and_rotates_session(
    service, signing_key, monkeypatch, target
):
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    monkeypatch.setattr(service, "_metadata", google_metadata)
    subject = "test-auth-" + uuid4().hex
    callback_nonce = ""

    async def keys(force=False):
        return {"keys": [signing_key.as_dict(is_private=False)]}

    async def fetch_token(self, endpoint, **kwargs):
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert len(kwargs["code_verifier"]) >= 43
        return {
            "id_token": JsonWebToken(["RS256"])
            .encode(
                {"alg": "RS256", "kid": "beatit-test"},
                identity(sub=subject, nonce=callback_nonce),
                signing_key,
            )
            .decode(),
        }

    monkeypatch.setattr(service, "_jwks", keys)
    monkeypatch.setattr(AsyncOAuth2Client, "fetch_token", fetch_token)
    old_token, _ = service.create_session(identity())
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        client.cookies.set(service.settings.cookie_name, old_token)
        response = client.get(
            "/api/auth/google", params={"return_to": target}, follow_redirects=False
        )
        query = parse_qs(urlsplit(response.headers["location"]).query)
        callback_nonce = query["nonce"][0]
        if target != "/":
            # Editing the target suffix invalidates the original state hash.
            tampered = query["state"][0].rsplit(".", 1)[0] + "." + "b" * 32
            assert (
                client.get(
                    "/api/auth/callback", params={"code": "test-code", "state": tampered}
                ).status_code
                == 400
            )
        response = client.get(
            "/api/auth/callback",
            params={
                "code": "test-code",
                "state": query["state"][0],
                "return_to": "https://attacker.example",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == service.settings.public_url + target
        assert "HttpOnly" in response.headers["set-cookie"]
        assert service.current_user(cookie_request(service, old_token)) is None
        assert client.get("/api/auth/me").json()["user"]["email"] == "owner@example.test"
        assert client.get("/private").status_code == 200
        assert (
            client.get(
                "/api/auth/callback",
                params={"code": "test-code", "state": query["state"][0]},
            ).status_code
            == 400
        )


def test_provider_token_error_is_a_safe_authentication_failure(service, monkeypatch):
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    async def fetch_token(self, endpoint, **kwargs):
        raise OAuth2Error(description="sensitive-code-and-secret")

    monkeypatch.setattr(service, "_metadata", google_metadata)
    monkeypatch.setattr(AsyncOAuth2Client, "fetch_token", fetch_token)
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        response = client.get("/api/auth/google", follow_redirects=False)
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        response = client.get("/api/auth/callback", params={"code": "test-code", "state": state})
        assert response.status_code == 401
        assert "sensitive" not in response.text
