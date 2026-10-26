import ast
import importlib.util
import io
import json
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transcription import colab_artifacts
from transcription.colab_artifacts import MEDIA, import_output
from transcription.colab_controller import Controller, create_controller
from transcription.colab_transport import ColabTransport, ColabUnavailable, validate_session

JOB = "a" * 32
ATTEMPT = "b" * 32
NAME = "beatit-chunsik"
SECRET = "controller-private-" + "x" * 32


def session(**changes):
    value = {
        "name": NAME,
        "endpoint": "runtime-endpoint",
        "token": "runtime-scoped-token",
        "url": "https://colab.research.google.com/tun/m/runtime-endpoint",
        "token_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "variant": "GPU",
        "accelerator": "T4",
    }
    value.update(changes)
    return {NAME: value}


def result_entries(generation="render-123"):
    musicxml = b'<score-partwise version="4.0"><part-list/></score-partwise>'
    midi = b"MThd\x00\x00\x00\x06\x00\x00\x00\x00\x00\x60"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>'
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as output:
        output.writestr("score.musicxml", musicxml)
        output.writestr("score.mid", midi)
        output.writestr("score-1.svg", svg)
    result = {
        "generation": generation,
        "pages": ["score-1.svg"],
        "event_count": 1,
        "duration": 1,
    }
    entries = {name: b"audio payload" for name in MEDIA}
    entries.update(
        {
            "result.json": json.dumps(result).encode(),
            "analysis.json": b'{"transcription_device":"cuda","separation_device":"cuda"}',
            "events.json": b'[{"time":0.1,"pitch":35,"strength":0.8}]',
            f"{generation}/score.musicxml": musicxml,
            f"{generation}/score.mid": midi,
            f"{generation}/performance.mid": midi,
            f"{generation}/score-1.svg": svg,
            f"{generation}/score-bundle.zip": bundle.getvalue(),
        }
    )
    return entries


def write_archive(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


class FakeTransport:
    def __init__(self, archive):
        self.archive = archive
        self.marker = {}
        self.executed = []
        self.uploaded = []
        self.attached = []
        self.launched = threading.Event()
        self.finish = threading.Event()
        self.finish.set()
        self.cancel_fails = False
        self.commands = []

    def attach(self, sessions):
        self.attached.append(sessions)

    def execute(self, code, timeout=600):
        self.executed.append(code)
        expression = ast.parse(code).body[-1]
        call = expression.value if isinstance(expression, ast.Expr) else None
        name = (
            call.func.id if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) else None
        )
        self.commands.append(name)
        if name == "prepare":
            self.marker = {"fingerprint": ast.literal_eval(call.args[0])}
        elif name == "launch":
            self.launched.set()
        elif name == "cancel" and self.cancel_fails:
            raise ColabUnavailable("Runtime is disconnected")

    def read_json(self, path):
        if path.endswith("/prepared.json"):
            return self.marker
        if path.endswith("/status.json"):
            return {"state": "completed" if self.finish.is_set() else "running"}
        if path.endswith("/progress.json"):
            return {"stage": "transcribing", "progress": 0.7, "message": "GPU 분석 중"}
        raise FileNotFoundError(path)

    def upload(self, local, remote):
        self.uploaded.append((local.name, remote))

    def download(self, remote, local):
        shutil.copyfile(self.archive, local)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_SESSION_NAME", NAME)
    data = tmp_path / "data"
    directory = data / JOB
    directory.mkdir(parents=True)
    (directory / "request.json").write_text('{"filename":"song.wav","options":{}}')
    (directory / "input").write_bytes(b"local input")
    source = tmp_path / "source.tar"
    source.write_bytes(b"versioned source payload")
    archive = write_archive(tmp_path / "output.zip", result_entries())
    transport = FakeTransport(archive)
    controller = Controller(data, tmp_path / "state", source, transport=transport)
    return controller, transport, directory


def complete(controller, job=JOB, attempt=ATTEMPT):
    controller.submit(job, attempt)
    controller.job_thread.join(timeout=5)
    assert not controller.job_thread.is_alive(), "Fake transport job did not finish"
    return controller.attempt(job, attempt)


def test_private_controller_requires_authentication(environment):
    controller, transport, _ = environment
    with TestClient(create_controller(controller, SECRET)) as client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/v1/status").status_code == 401
        assert client.post("/v1/attachment", json={"sessions": session()}).status_code == 401
        assert (
            client.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        headers = {"Authorization": "Bearer " + SECRET}
        assert client.get("/v1/status", headers=headers).json()["state"] == "unavailable"
        response = client.post("/v1/attachment", json={"sessions": session()}, headers=headers)
        assert response.status_code == 200
        assert response.json()["allocation_mode"] == "operator"
        assert "runtime-scoped-token" not in response.text
        assert len(transport.attached) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://colab.research.google.com/tun/m/runtime-endpoint"},
        {"url": "https://colab.research.google.com.attacker.test/tun/m/runtime-endpoint"},
        {"url": "https://attacker.test/colab"},
        {"url": "https://user:password@colab.research.google.com/tun/m/runtime-endpoint"},
        {"url": "https://colab.research.google.com/tun/m/runtime-endpoint?token=secret"},
        {"token_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
        {"token_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()},
        {"token_expires_at": 123},
        {"endpoint": ["not", "a", "string"]},
        {"token": {"refresh_token": "not-a-runtime-token"}},
        {"refresh_token": "google-account-credential"},
        {"client_secret": "google-account-credential"},
        {"variant": "CPU"},
    ],
)
def test_attachment_rejects_invalid_hosts_expiry_and_google_account_fields(changes):
    with pytest.raises(ValueError):
        validate_session(session(**changes), NAME)


def test_attachment_must_contain_only_its_named_runtime():
    with pytest.raises(ValueError):
        validate_session({**session(), "another": session()[NAME]}, NAME)


@pytest.mark.parametrize("domain", ["prod.r.colab.dev", "prod.colab.dev"])
@pytest.mark.parametrize("path", ["", "/"])
def test_attachment_accepts_verified_production_runtime_hosts(domain, path):
    url = f"https://8080-runtime-endpoint-c.us-central1.{domain}{path}"
    attached = validate_session(session(url=url), NAME)
    assert attached["endpoint"] == "runtime-endpoint"
    assert attached["url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "https://8080-another-endpoint-c.us-central1.prod.r.colab.dev/",
        "https://8080-runtime-endpoint-extra-c.us-central1.prod.r.colab.dev/",
        "https://runtime-endpoint-c.us-central1.prod.r.colab.dev/",
        "https://8080-runtime-endpoint-c.us-central1.prod.r.colab.dev.attacker.test/",
        "https://8080-runtime-endpoint-c.us-central1.prod.colab.dev.attacker.test/",
        "https://8080-runtime-endpoint-c.us-central1.prod.r.colab.dev/tun/m/runtime-endpoint",
        "https://8080-runtime-endpoint-c.us-central1.prod.colab.dev/other-path",
        "https://arbitrary.googleusercontent.com/",
        "https://colab.research.google.com/tun/m/another-endpoint",
    ],
)
def test_attachment_rejects_mismatched_production_endpoint_paths_and_attacker_domains(url):
    with pytest.raises(ValueError):
        validate_session(session(url=url), NAME)


def test_runtime_transfer_does_not_follow_redirects_or_expose_its_token(tmp_path, monkeypatch):
    import requests

    transport = ColabTransport(tmp_path / "transport", NAME)
    transport.attach(session())
    calls = []

    class Redirect:
        is_redirect = True

        def close(self):
            pass

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Redirect()

    monkeypatch.setattr(requests, "request", request)
    with pytest.raises(ColabUnavailable) as raised:
        transport.read_json("/content/beatit/prepared.json")
    assert calls[0][2]["allow_redirects"] is False
    assert "runtime-scoped-token" not in str(raised.value)


def test_cli_failure_hides_tracebacks_and_never_uses_account_credentials(tmp_path, monkeypatch):
    transport = ColabTransport(tmp_path / "transport", NAME)
    transport.attach(session())
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/private/google-account.json")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        kwargs["stdout"].write(b"Traceback containing runtime-scoped-token")
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ColabUnavailable) as raised:
        transport.execute("print('remote command')")
    assert "runtime-scoped-token" not in str(raised.value)
    assert "Traceback" not in str(raised.value)
    assert calls[0][1]["GOOGLE_APPLICATION_CREDENTIALS"] == "/nonexistent-colab-account-credentials"
    assert calls[0][0][1:3] == ["--auth", "adc"]
    assert transport.config.stat().st_mode & 0o777 == 0o600


def test_unavailable_runtime_never_launches_or_creates_attempt(environment):
    controller, transport, _ = environment
    with pytest.raises(ColabUnavailable, match="Colab 연결 대기"):
        controller.submit(JOB, ATTEMPT)
    assert transport.executed == []
    with pytest.raises(KeyError):
        controller.attempt(JOB, ATTEMPT)


def test_submission_rejects_traversal_and_symlinked_job(environment, tmp_path):
    controller, transport, directory = environment
    controller.attach(session(), 90)
    for invalid in ("../secret", "a" * 31, "g" * 32, "/" + JOB):
        with pytest.raises(ValueError):
            controller.submit(invalid, ATTEMPT)
    elsewhere = tmp_path / "elsewhere"
    directory.rename(elsewhere)
    directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError):
        controller.submit(JOB, ATTEMPT)
    assert transport.executed == []


def test_same_attempt_is_idempotent_and_another_request_cannot_reuse_it(environment):
    controller, transport, directory = environment
    controller.attach(session(), 90)
    transport.marker = {"fingerprint": controller.fingerprint}
    transport.finish.clear()
    controller.submit(JOB, ATTEMPT)
    assert transport.launched.wait(3)
    assert controller.submit(JOB, ATTEMPT)["status"] == "running"
    assert transport.commands.count("launch") == 1
    (directory / "request.json").write_text('{"filename":"changed.wav","options":{}}')
    with pytest.raises(ValueError, match="another request"):
        controller.submit(JOB, ATTEMPT)
    controller.cancel(JOB, ATTEMPT)
    controller.job_thread.join(timeout=5)
    assert controller.attempt(JOB, ATTEMPT)["status"] == "cancelled"
    assert transport.commands.count("launch") == 1
    assert "cancel" in transport.commands


def test_matching_prepared_marker_skips_setup_and_imports_complete_result(environment):
    controller, transport, directory = environment
    controller.attach(session(), 90)
    transport.marker = {"fingerprint": controller.fingerprint}
    assert complete(controller)["status"] == "completed"
    assert "prepare" not in transport.commands
    assert not any(remote.endswith("/source.tar") for _, remote in transport.uploaded)
    result = json.loads((directory / "result.json").read_text())
    assert result["execution_backend"] == "colab"
    assert result["execution_runtime"]["prepared_reused"] is True
    assert (directory / result["generation"] / "score-1.svg").is_file()
    assert all((directory / name).is_file() for name in MEDIA)
    assert (directory / "input").read_bytes() == b"local input"


@pytest.mark.parametrize("marker", [{}, {"fingerprint": "old-source-version"}])
def test_missing_or_changed_preparation_fingerprint_prepares_before_launch(environment, marker):
    controller, transport, _ = environment
    controller.attach(session(), 90)
    transport.marker = marker
    assert complete(controller)["status"] == "completed"
    assert transport.commands.count("prepare") == 1
    assert transport.commands.index("prepare") < transport.commands.index("launch")
    assert any(remote.endswith("/source.tar") for _, remote in transport.uploaded)


def test_idle_release_is_requested_without_allocating_or_pinging(environment):
    controller, transport, _ = environment
    controller.attach(session(), 90)
    controller.last_activity -= controller.idle_seconds + 1
    assert controller.status()["release_requested"] is True
    assert transport.executed == []


@pytest.mark.parametrize(
    "entry", ["../escape", "/tmp/escape", "render-123/../../escape", "request.json"]
)
def test_unexpected_result_entries_never_change_published_data(environment, tmp_path, entry):
    _, _, directory = environment
    (directory / "result.json").write_bytes(b"previous result")
    (directory / "audio.wav").write_bytes(b"previous audio")
    entries = result_entries()
    entries[entry] = b"unexpected"
    archive = write_archive(tmp_path / "unexpected.zip", entries)
    with pytest.raises(ValueError, match="Unexpected result"):
        import_output(archive, directory, {})
    assert (directory / "result.json").read_bytes() == b"previous result"
    assert (directory / "audio.wav").read_bytes() == b"previous audio"
    assert not (directory / "render-123").exists()
    assert not (tmp_path / "escape").exists()


def test_symlink_result_entry_is_rejected_before_publication(environment, tmp_path):
    _, _, directory = environment
    entries = result_entries()
    entries.pop("audio.wav")
    archive = write_archive(tmp_path / "link.zip", entries)
    info = zipfile.ZipInfo("audio.wav")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr(info, "../../secret")
    with pytest.raises(ValueError, match="Unexpected result"):
        import_output(archive, directory, {})
    assert not (directory / "result.json").exists()


def test_crc_failure_preserves_existing_generation_and_media(environment, tmp_path):
    _, _, directory = environment
    (directory / "result.json").write_bytes(b"previous result")
    (directory / "audio.wav").write_bytes(b"previous audio")
    archive = write_archive(tmp_path / "corrupted.zip", result_entries())
    with zipfile.ZipFile(archive) as source:
        info = source.getinfo("audio.wav")
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    content = bytearray(archive.read_bytes())
    content[offset] ^= 0xFF
    archive.write_bytes(content)
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        import_output(archive, directory, {})
    assert (directory / "result.json").read_bytes() == b"previous result"
    assert (directory / "audio.wav").read_bytes() == b"previous audio"
    assert not (directory / "render-123").exists()


def test_result_pointer_is_the_last_published_file(environment, monkeypatch):
    _, transport, directory = environment
    previous = b'{"generation":"render-1"}'
    (directory / "result.json").write_bytes(previous)
    replace = colab_artifacts.os.replace
    destinations = []

    def observe(source, destination):
        destination = Path(destination)
        if destination.parent == directory and destination.name == "result.json":
            assert (directory / "result.json").read_bytes() == previous
            assert all((directory / name).is_file() for name in MEDIA)
            assert (directory / "render-123" / "score-1.svg").is_file()
        destinations.append(destination)
        return replace(source, destination)

    monkeypatch.setattr(colab_artifacts.os, "replace", observe)
    import_output(transport.archive, directory, {"runtime_id": "public-runtime-fingerprint"})
    assert destinations[-1] == directory / "result.json"
    assert json.loads((directory / "result.json").read_text())["generation"] == "render-123"


def test_restart_cancels_running_orphan_before_starting_another_job(environment):
    controller, transport, _ = environment
    controller.attach(session(), 90)
    with controller.connect() as connection:
        connection.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, 'running', NULL, ?, ?)",
            (ATTEMPT, JOB, "old request", controller.runtime_id, time.time()),
        )
    restarted = Controller(
        controller.data, controller.state, controller.source, transport=transport
    )
    assert restarted.attempt(JOB, ATTEMPT)["status"] == "failed"
    restarted.attach(session(), 90)
    transport.marker = {"fingerprint": restarted.fingerprint}
    assert complete(restarted, attempt="c" * 32)["status"] == "completed"
    assert transport.commands.index("cancel") < transport.commands.index("launch")


def test_failed_remote_cancellation_retains_cleanup_debt_across_restart(environment):
    controller, transport, _ = environment
    controller.attach(session(), 90)
    transport.marker = {"fingerprint": controller.fingerprint}
    transport.finish.clear()
    transport.cancel_fails = True
    controller.submit(JOB, ATTEMPT)
    assert transport.launched.wait(3)
    controller.cancel(JOB, ATTEMPT)
    controller.job_thread.join(timeout=5)
    assert not controller.job_thread.is_alive()
    cancelled = controller.attempt(JOB, ATTEMPT)
    assert cancelled["status"] == "failed"
    assert cancelled["cleanup_pending"] is True
    assert transport.commands.count("launch") == 1

    restarted = Controller(
        controller.data, controller.state, controller.source, transport=transport
    )
    restarted.attach(session(), 90)
    try:
        restarted.submit(JOB, "c" * 32)
    except (ColabUnavailable, ValueError):
        pass
    if restarted.job_thread:
        restarted.job_thread.join(timeout=5)
    assert transport.commands.count("launch") == 1
    assert restarted.attempt(JOB, ATTEMPT)["cleanup_pending"] is True

    transport.cancel_fails = False
    transport.finish.set()
    assert complete(restarted, attempt="d" * 32)["status"] == "completed"
    assert transport.commands.count("launch") == 2


def test_pending_cleanup_refuses_a_replacement_runtime_until_release_is_confirmed(environment):
    controller, transport, _ = environment
    controller.attach(session(), 90)
    transport.marker = {"fingerprint": controller.fingerprint}
    transport.finish.clear()
    transport.cancel_fails = True
    controller.submit(JOB, ATTEMPT)
    assert transport.launched.wait(3)
    controller.cancel(JOB, ATTEMPT)
    controller.job_thread.join(timeout=5)
    replacement = session(
        endpoint="replacement-runtime",
        url="https://colab.research.google.com/tun/m/replacement-runtime",
    )
    with pytest.raises(ValueError, match="release"):
        controller.attach(replacement, 90)
    controller.detach(force=True, released=True)
    assert controller.attempt(JOB, ATTEMPT).get("cleanup_pending", False) is False
    assert controller.attach(replacement, 90)["state"] == "idle"


def load_operator():
    path = Path(__file__).parents[1] / "scripts/connect_colab.py"
    spec = importlib.util.spec_from_file_location("beatit_test_colab_operator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_reuses_owned_session_and_releases_it_on_exit(tmp_path, monkeypatch):
    operator = load_operator()
    work = tmp_path / "operator"
    work.mkdir()
    (work / "sessions.json").write_text(json.dumps(session()))
    monkeypatch.setattr(sys, "argv", ["connect_colab.py", "--work-dir", str(work)])
    calls = []
    rpc_inputs = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ssh":
            rpc_inputs.append(kwargs["input"])
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "state": "idle",
                        "prepared": False,
                        "active": False,
                        "accelerator": "T4",
                        "release_requested": True,
                    }
                ),
            )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    operator.main()
    assert not any("new" in call for call in calls if call[0] == "colab")
    assert sum("stop" in call for call in calls if call[0] == "colab") == 1
    assert not any("refresh_token" in code or "client_secret" in code for code in rpc_inputs)
    assert any("released=true" in code for code in rpc_inputs)


def test_operator_cleans_up_a_named_allocation_when_new_times_out(tmp_path, monkeypatch):
    operator = load_operator()
    work = tmp_path / "operator"
    monkeypatch.setattr(sys, "argv", ["connect_colab.py", "--work-dir", str(work)])
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, 0, stdout='{"state":"unavailable"}')
        if "new" in argv:
            (work / "sessions.json").write_text(json.dumps(session()))
            raise subprocess.TimeoutExpired(argv, 150)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises((subprocess.TimeoutExpired, RuntimeError)):
        operator.main()
    assert sum("stop" in call for call in calls if call[0] == "colab") == 1
