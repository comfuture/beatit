"""Google OpenID Connect and opaque, revocable managed-database sessions.

Google credentials never reach the browser. OAuth state is single-use, bound to
the initiating browser and protected with PKCE; Google ``sub`` is the account key.
"""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JoseError, JsonWebToken
from authlib.oauth2 import OAuth2Error
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

# Authlib 1.9 uses httpx2; older 1.x versions use httpx. Both may coexist in
# applications because third-party clients still depend on the original package.
try:
    from httpx2 import HTTPError as OAuthHTTPError
except ImportError:
    OAuthHTTPError = httpx.HTTPError

DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_SECONDS = 600
SESSION_SECONDS = 7 * 24 * 60 * 60
_jwt = JsonWebToken(["RS256"])


def login_return_path(value: str) -> str:
    """Only the workspace and canonical session permalinks can receive login."""
    if value == "/" or re.fullmatch(r"/sessions/[0-9a-f]{32}", value):
        return value
    raise HTTPException(400, "올바른 세션 주소로 다시 로그인하세요.")


def state_return_path(state: str) -> str:
    # Read only after consuming the browser-bound, single-use state record. The
    # whole value, including this optional target, is covered by the stored hash.
    identifier = state.partition(".")[2]
    return f"/sessions/{identifier}" if re.fullmatch(r"[0-9a-f]{32}", identifier) else "/"


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class AuthSettings:
    client_id: str = ""
    client_secret: str = ""
    public_url: str = ""

    @classmethod
    def from_env(cls):
        public_url = os.environ.get("BEATIT_PUBLIC_URL", "").rstrip("/")
        if public_url:
            parsed = urlsplit(public_url)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or (parsed.scheme != "https" and not local)
            ):
                raise ValueError(
                    "BEATIT_PUBLIC_URL must be an HTTPS origin (HTTP on localhost only)."
                )
        return cls(
            client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            public_url=public_url,
        )

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret and self.public_url)

    @property
    def secure(self):
        return not self.public_url.startswith("http://")

    @property
    def cookie_name(self):
        return "__Host-beatit-session" if self.secure else "beatit-session"

    @property
    def state_cookie_name(self):
        return "__Host-beatit-oauth" if self.secure else "beatit-oauth"

    @property
    def callback_url(self):
        return self.public_url + "/api/auth/callback"


class AuthService:
    def __init__(self, db, settings: AuthSettings | None = None):
        self.db = db
        self.settings = settings or AuthSettings.from_env()
        self._discovery = None
        self._discovery_until = 0
        self._keys = None
        self._keys_until = 0
        self._metadata_lock = asyncio.Lock()

    def _require_configuration(self):
        if not self.settings.configured:
            raise HTTPException(
                503,
                "Google 로그인 설정이 필요합니다. GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
                "BEATIT_PUBLIC_URL을 설정하세요.",
            )

    async def _metadata(self):
        async with self._metadata_lock:
            if self._discovery and time.monotonic() < self._discovery_until:
                return self._discovery
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(DISCOVERY)
                response.raise_for_status()
                metadata = response.json()
            # Only Google endpoints may receive our client secret or authorization code.
            expected = {
                "issuer": "https://accounts.google.com",
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise ValueError("Unexpected Google discovery endpoints")
            self._discovery = metadata
            self._discovery_until = time.monotonic() + 3600
            return metadata

    async def _jwks(self, force=False):
        metadata = await self._metadata()
        async with self._metadata_lock:
            if not force and self._keys and time.monotonic() < self._keys_until:
                return self._keys
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(metadata["jwks_uri"])
                response.raise_for_status()
                keys = response.json()
            if not isinstance(keys.get("keys"), list) or not keys["keys"]:
                raise ValueError("Invalid Google signing keys")
            self._keys = keys
            self._keys_until = time.monotonic() + 3600
            return keys

    def _oauth_client(self, state=None):
        return AsyncOAuth2Client(
            self.settings.client_id,
            self.settings.client_secret,
            redirect_uri=self.settings.callback_url,
            scope="openid email profile",
            state=state,
            code_challenge_method="S256",
            token_endpoint_auth_method="client_secret_post",
            timeout=15,
        )

    def _save_state(self, state, browser_token, verifier, nonce):
        with self.db.connection() as connection:
            connection.execute("DELETE FROM beatit.oauth_states WHERE expires <= UTC_TIMESTAMP(6)")
            connection.execute(
                "INSERT INTO beatit.oauth_states "
                "(state_hash, browser_hash, verifier, nonce, expires) VALUES (%s,%s,%s,%s,%s)",
                (
                    token_hash(state),
                    token_hash(browser_token),
                    verifier,
                    nonce,
                    (datetime.now(timezone.utc) + timedelta(seconds=STATE_SECONDS)).replace(
                        tzinfo=None
                    ),
                ),
            )

    def _consume_state(self, state, browser_token):
        if not state or not browser_token or len(state) > 256 or len(browser_token) > 256:
            return None
        with self.db.connection() as connection:
            record = connection.execute(
                "SELECT verifier,nonce FROM beatit.oauth_states WHERE state_hash=%s AND browser_hash=%s "
                "AND expires>UTC_TIMESTAMP(6) FOR UPDATE",
                (token_hash(state), token_hash(browser_token)),
            ).fetchone()
            if record:
                connection.execute(
                    "DELETE FROM beatit.oauth_states WHERE state_hash=%s", (token_hash(state),)
                )
            return record

    async def begin_login(self, return_to="/"):
        self._require_configuration()
        return_to = login_return_path(return_to)
        try:
            metadata = await self._metadata()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(
                503, "Google 로그인 서버에 연결할 수 없습니다. 다시 시도하세요."
            ) from None
        state, browser_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if return_to != "/":
            state += "." + return_to.rsplit("/", 1)[1]
        verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        async with self._oauth_client() as client:
            location, _ = client.create_authorization_url(
                metadata["authorization_endpoint"],
                state=state,
                code_verifier=verifier,
                nonce=nonce,
                prompt="select_account",
            )
        await run_in_threadpool(self._save_state, state, browser_token, verifier, nonce)
        response = RedirectResponse(location, status_code=302)
        response.set_cookie(
            self.settings.state_cookie_name,
            browser_token,
            max_age=STATE_SECONDS,
            secure=self.settings.secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def validate_id_token(self, encoded, nonce):
        keys = await self._jwks()
        options = {
            "iss": {"essential": True, "values": list(ISSUERS)},
            "aud": {"essential": True, "value": self.settings.client_id},
            "sub": {"essential": True},
            "exp": {"essential": True},
            "iat": {"essential": True},
            "nonce": {"essential": True, "value": nonce},
            "email": {"essential": True},
            "email_verified": {"essential": True},
        }
        try:
            claims = _jwt.decode(encoded, keys, claims_options=options)
        except (JoseError, ValueError):
            # Google rotates signing keys. Refresh once before rejecting the token.
            claims = _jwt.decode(encoded, await self._jwks(force=True), claims_options=options)
        claims.validate(leeway=30)
        if (
            not isinstance(claims.get("sub"), str)
            or not 1 <= len(claims["sub"]) <= 255
            or not isinstance(claims.get("email"), str)
            or not claims["email"]
            or claims.get("email_verified") is not True
            or not isinstance(claims.get("nonce"), str)
            or not hmac.compare_digest(claims["nonce"], nonce)
            or (claims.get("azp") is not None and claims["azp"] != self.settings.client_id)
            or (isinstance(claims["aud"], list) and claims.get("azp") != self.settings.client_id)
        ):
            raise ValueError("Invalid Google identity claims")
        return dict(claims)

    def create_session(self, claims, previous_token=None):
        """Upsert by stable Google subject, rotate the current browser's session."""
        token = secrets.token_urlsafe(48)
        with self.db.connection() as connection:
            connection.execute(
                "INSERT INTO beatit.users (id,google_sub,email,name,picture) "
                "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "email=VALUES(email),name=VALUES(name),picture=VALUES(picture)",
                (
                    uuid4().hex,
                    claims["sub"],
                    claims["email"][:320],
                    str(claims.get("name", ""))[:200],
                    self._picture(claims.get("picture")),
                ),
            )
            user = connection.execute(
                "SELECT id,email,name,picture FROM beatit.users WHERE google_sub=%s",
                (claims["sub"],),
            ).fetchone()
            connection.execute("DELETE FROM beatit.sessions WHERE expires <= UTC_TIMESTAMP(6)")
            if previous_token:
                connection.execute(
                    "DELETE FROM beatit.sessions WHERE token_hash=%s", (token_hash(previous_token),)
                )
            connection.execute(
                "INSERT INTO beatit.sessions (token_hash,user_id,expires) VALUES (%s,%s,%s)",
                (
                    token_hash(token),
                    user["id"],
                    (datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)).replace(
                        tzinfo=None
                    ),
                ),
            )
        return token, dict(user)

    @staticmethod
    def _picture(value):
        if isinstance(value, str) and len(value) <= 2048:
            url = urlsplit(value)
            if url.scheme == "https" and url.hostname and not url.username and not url.password:
                return value
        return ""

    async def finish_login(self, request: Request):
        self._require_configuration()
        state = request.query_params.get("state", "")
        record = await run_in_threadpool(
            self._consume_state,
            state,
            request.cookies.get(self.settings.state_cookie_name, ""),
        )
        if not record:
            raise HTTPException(
                400, "로그인 요청이 만료되었거나 올바르지 않습니다. 다시 로그인하세요."
            )
        return_path = state_return_path(state)
        if request.query_params.get("error"):
            response = RedirectResponse(
                self.settings.public_url + return_path + "?auth_error=cancelled", 303
            )
            response.delete_cookie(
                self.settings.state_cookie_name,
                path="/",
                secure=self.settings.secure,
                httponly=True,
                samesite="lax",
            )
            return response
        code = request.query_params.get("code", "")
        if not code or len(code) > 8192:
            raise HTTPException(400, "Google 인증 코드가 없습니다. 다시 로그인하세요.")
        try:
            metadata = await self._metadata()
            async with self._oauth_client(state=state) as client:
                tokens = await client.fetch_token(
                    metadata["token_endpoint"],
                    grant_type="authorization_code",
                    code=code,
                    code_verifier=record["verifier"],
                )
            claims = await self.validate_id_token(tokens["id_token"], record["nonce"])
        except (
            httpx.HTTPError,
            OAuthHTTPError,
            JoseError,
            OAuth2Error,
            OAuthError,
            KeyError,
            ValueError,
        ):
            raise HTTPException(
                401, "Google 인증을 확인할 수 없습니다. 다시 로그인하세요."
            ) from None
        token, _ = await run_in_threadpool(
            self.create_session, claims, request.cookies.get(self.settings.cookie_name)
        )
        response = RedirectResponse(self.settings.public_url + return_path, status_code=303)
        response.set_cookie(
            self.settings.cookie_name,
            token,
            max_age=SESSION_SECONDS,
            secure=self.settings.secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(
            self.settings.state_cookie_name,
            path="/",
            secure=self.settings.secure,
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def current_user(self, request: Request):
        token = request.cookies.get(self.settings.cookie_name, "")
        if not token or len(token) > 256:
            return None
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT u.id,u.email,u.name,u.picture FROM beatit.sessions s "
                "JOIN beatit.users u ON u.id=s.user_id "
                "WHERE s.token_hash=%s AND s.expires>UTC_TIMESTAMP(6)",
                (token_hash(token),),
            ).fetchone()
        return dict(row) if row else None

    def revoke_session(self, request: Request):
        token = request.cookies.get(self.settings.cookie_name, "")
        if token and len(token) <= 256:
            with self.db.connection() as connection:
                connection.execute(
                    "DELETE FROM beatit.sessions WHERE token_hash=%s", (token_hash(token),)
                )


async def require_user(request: Request) -> dict:
    user = await run_in_threadpool(request.app.state.auth.current_user, request)
    if user is None:
        raise HTTPException(401, "Google 계정으로 로그인하세요.")
    return user


def install_auth(app: FastAPI, db, service: AuthService | None = None):
    service = service or AuthService(db)
    app.state.auth = service

    @app.get("/api/auth/me")
    async def me(request: Request):
        user = await run_in_threadpool(service.current_user, request)
        return JSONResponse({"user": user}, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/google")
    async def login(return_to: str = "/"):
        return await service.begin_login(return_to)

    @app.get("/api/auth/callback")
    async def callback(request: Request):
        return await service.finish_login(request)

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request):
        await run_in_threadpool(service.revoke_session, request)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            service.settings.cookie_name,
            path="/",
            secure=service.settings.secure,
            httponly=True,
            samesite="lax",
        )
        return response

    return service
