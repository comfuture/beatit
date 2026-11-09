import asyncio
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from authlib.jose import JoseError, JsonWebKey, JsonWebToken
from authlib.oauth2 import OAuth2Error
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from transcription.auth import (
    AuthService,
    AuthSettings,
    install_auth,
    require_user,
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


def test_oauth_state_expiry_and_cancelled_login(service, monkeypatch):
    service._save_state("expired", "browser", "verifier", "nonce")
    with service.db.connection() as connection:
        connection.execute(
            "UPDATE beatit.oauth_states SET expires=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE state_hash=%s",
            (token_hash("expired"),),
        )
    assert service._consume_state("expired", "browser") is None
    monkeypatch.setattr(service, "_metadata", google_metadata)
    with TestClient(auth_app(service), base_url="https://beatit.example.test") as client:
        response = client.get("/api/auth/google", follow_redirects=False)
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        response = client.get(
            "/api/auth/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/?auth_error=cancelled")
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


def test_google_callback_validates_signed_token_and_rotates_session(
    service, signing_key, monkeypatch
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
        response = client.get("/api/auth/google", follow_redirects=False)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        callback_nonce = query["nonce"][0]
        response = client.get(
            "/api/auth/callback",
            params={"code": "test-code", "state": query["state"][0]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == service.settings.public_url + "/"
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
