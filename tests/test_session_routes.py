"""Deep links must reload the shell without bypassing API authorization."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from transcription import app as application


@pytest.fixture
def shell_client(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Beatit shell</title>")
    monkeypatch.setattr(application, "STATIC", web)
    store = SimpleNamespace(db=None, close=lambda: None)
    with TestClient(application.create_app(start_worker=False, store=store)) as client:
        yield client


def test_direct_session_reload_returns_shell_but_private_data_still_requires_login(shell_client):
    identifier = "a" * 32
    response = shell_client.get(f"/sessions/{identifier}")
    assert response.status_code == 200
    assert "Beatit shell" in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert shell_client.get(f"/api/jobs/{identifier}").status_code == 401
    assert shell_client.get("/api/not-a-route").status_code == 404


@pytest.mark.parametrize("path", ["/sessions/no-such-id", "/sessions/" + "A" * 32, "/missing"])
def test_unknown_paths_are_not_rewritten_to_the_shell(shell_client, path):
    response = shell_client.get(path)
    assert response.status_code == 404
    assert "Beatit shell" not in response.text
