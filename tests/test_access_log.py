"""Render genuine Uvicorn access records to verify OAuth query redaction."""

import io
import logging
from types import SimpleNamespace

import pytest
import uvicorn
from uvicorn.logging import AccessFormatter

from transcription.access_log import AuthQueryRedactionFilter, install_auth_query_redaction


@pytest.fixture
def access_logger():
    logger = logging.getLogger("uvicorn.access")
    previous = (
        logger.handlers[:],
        logger.filters[:],
        logger.level,
        logger.propagate,
        logger.disabled,
    )
    logger.handlers = []
    logger.filters = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.disabled = False
    yield logger
    logger.handlers, logger.filters, logger.level, logger.propagate, logger.disabled = previous


def capture(logger):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        AccessFormatter(
            '%(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )
    )
    logger.handlers = [handler]
    return output


def log_request(logger, path, status=302):
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1234", "GET", path, "1.1", status)


@pytest.mark.parametrize("path", ["/api/auth", "/api/auth/login", "/api/auth/callback"])
def test_oauth_query_secrets_are_removed_but_endpoint_and_status_remain(access_logger, path):
    install_auth_query_redaction()
    output = capture(access_logger)
    log_request(access_logger, path + "?code=private-authorization-code&state=private-state")
    rendered = output.getvalue()
    assert f'GET {path} HTTP/1.1" 302 Found' in rendered
    assert "127.0.0.1:1234" in rendered
    assert "private-authorization-code" not in rendered
    assert "private-state" not in rendered
    assert "?" not in rendered


@pytest.mark.parametrize(
    "path", ["/api/jobs?query=recording", "/api/health", "/api/author?query=recording"]
)
def test_normal_route_access_logging_is_preserved(access_logger, path):
    install_auth_query_redaction()
    output = capture(access_logger)
    log_request(access_logger, path, status=200)
    assert f'GET {path} HTTP/1.1" 200 OK' in output.getvalue()


def test_redaction_survives_uvicorn_configuration_after_app_construction(access_logger):
    install_auth_query_redaction()
    # The CLI creates its FastAPI app before uvicorn.run configures logging.
    uvicorn.Config(app=None)
    install_auth_query_redaction()
    assert sum(isinstance(item, AuthQueryRedactionFilter) for item in access_logger.filters) == 1
    output = capture(access_logger)
    log_request(access_logger, "/api/auth/callback?code=private-code&state=private-state")
    assert '/api/auth/callback HTTP/1.1" 302 Found' in output.getvalue()
    assert "private-" not in output.getvalue()


def test_create_app_installs_redaction_without_touching_the_oauth_request(
    access_logger, monkeypatch
):
    from fastapi import Request
    from fastapi.testclient import TestClient

    from transcription import app as application

    def auth_routes(app, database, service=None):
        @app.get("/api/auth/callback")
        def callback(request: Request):
            # OAuth still needs the original values; logging must not change scope.
            log_request(access_logger, str(request.url.path) + "?" + request.url.query)
            return {"code": request.query_params["code"], "state": request.query_params["state"]}

    monkeypatch.setattr(application, "install_auth", auth_routes)
    app = application.create_app(start_worker=False, store=SimpleNamespace(db=None))
    output = capture(access_logger)
    client = TestClient(app)
    response = client.get("/api/auth/callback?code=original-code&state=original-state")
    assert response.json() == {"code": "original-code", "state": "original-state"}
    assert "original-code" not in output.getvalue()
    assert "original-state" not in output.getvalue()
    assert '/api/auth/callback HTTP/1.1" 302 Found' in output.getvalue()
