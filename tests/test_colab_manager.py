import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from test_colab import ATTEMPT, JOB, NAME, SECRET, session
from test_colab import environment as environment

from transcription.colab_controller import Controller, ManagerUpdate, create_controller
from transcription.colab_manager import Manager, ManagerFailure
from transcription.colab_transport import ColabUnavailable


@pytest.fixture
def managed(environment, monkeypatch):
    original, transport, directory = environment
    monkeypatch.setenv("COLAB_ALLOCATION_MODE", "server")
    controller = Controller(original.data, original.state, original.source, transport=transport)
    controller.manager_update(ManagerUpdate())
    yield controller, transport, directory
    controller.stop()


def controller_rpc(controller):
    def rpc(path, payload=None, *, method=None, **kwargs):
        route = urlparse(path)
        try:
            if route.path == "/v1/status":
                return controller.status()
            if route.path == "/v1/manager":
                return controller.manager_update(ManagerUpdate(**payload))
            if route.path == "/v1/attachment":
                if method == "DELETE":
                    query = parse_qs(route.query)
                    return controller.detach(
                        force=query.get("force") == ["true"],
                        released=query.get("released") == ["true"],
                    )
                return controller.attach(payload["sessions"], payload["lease_seconds"])
        except ValueError as error:
            raise RuntimeError(str(error)) from None
        raise AssertionError(f"Unexpected controller route: {path}")

    return rpc


class FakeManager(Manager):
    """Account sync and allocation have no Google or subprocess side effects."""

    def __init__(self, state, controller):
        super().__init__(state, rpc=controller_rpc(controller))
        self.account = {}
        self.commands = []
        self.on_new = None
        self.account_checks = 0

    def account_endpoints(self):
        self.account_checks += 1
        return {value["endpoint"] for value in self.account.values()}

    def sync(self):
        self.config.write_text(json.dumps(self.account))

    def command(self, *arguments, timeout=35):
        self.commands.append(arguments)
        if arguments[0] == "sessions":
            self.sync()
        elif arguments[0] == "new":
            if self.on_new:
                self.on_new(self)
            else:
                self.account.update(session())
                self.sync()
        elif arguments[0] == "stop":
            self.account.pop(arguments[arguments.index("-s") + 1], None)
            self.sync()
        else:
            raise AssertionError(f"Unexpected Colab command: {arguments}")

    def count(self, action):
        return sum(command[0] == action for command in self.commands)


def finish(controller):
    controller.job_thread.join(timeout=5)
    assert not controller.job_thread.is_alive(), "Fake controller job did not finish"
    return controller.attempt(JOB, ATTEMPT)


def test_idle_manager_renews_lease_without_allocating(managed, tmp_path):
    controller, transport, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    for _ in range(3):
        manager.step()
    status = controller.status()
    assert status["state"] == "idle"
    assert status["can_allocate"] is True
    assert status["prepared"] is False
    assert status["allocation_requested"] is False
    assert manager.commands == [("sessions",)]
    assert not manager.owned.exists()
    assert not manager.intent.exists()
    assert transport.executed == []


def test_queued_job_allocates_attaches_and_completes_with_one_runtime(managed, tmp_path):
    controller, transport, directory = managed
    manager = FakeManager(tmp_path / "manager", controller)
    transport.marker = {"fingerprint": controller.fingerprint}
    controller.submit(JOB, ATTEMPT)
    assert controller.status()["allocation_requested"] is True
    assert controller.status()["state"] == "allocating"
    assert controller.submit(JOB, ATTEMPT)["status"] == "running"
    manager.step()
    assert finish(controller)["status"] == "completed"
    assert manager.count("new") == 1
    assert transport.commands.count("launch") == 1
    assert json.loads(manager.owned.read_text())["endpoint"] == "runtime-endpoint"
    assert not manager.intent.exists()
    assert (directory / "result.json").exists()
    with controller.connect() as connection:
        record = connection.execute(
            "SELECT runtime FROM attempts WHERE attempt=?", (ATTEMPT,)
        ).fetchone()
    assert record["runtime"] == controller.runtime_id


class OneWatchTick:
    def __init__(self):
        self.calls = 0

    def wait(self, timeout):
        self.calls += 1
        return self.calls > 1


def test_controller_watch_does_not_cancel_a_job_waiting_for_its_first_vm(managed):
    controller, transport, _ = managed
    controller.active = (JOB, ATTEMPT)
    controller.awaiting_runtime = True
    stopping = controller.stopping
    controller.stopping = OneWatchTick()
    try:
        controller._watch()
    finally:
        controller.stopping = stopping
    assert not controller.cancelled.is_set()
    assert transport.executed == []
    controller.active = None


@pytest.mark.parametrize("reason", ["cancel", "manager_error", "expired", "shutdown"])
def test_failure_before_attachment_never_executes_remote_commands_or_leaves_debt(managed, reason):
    controller, transport, _ = managed
    controller.submit(JOB, ATTEMPT)
    if reason == "cancel":
        controller.cancel(JOB, ATTEMPT)
    elif reason == "manager_error":
        controller.manager_update(ManagerUpdate(phase="error", error="allocation"))
    elif reason == "expired":
        controller.manager_until = time.time() - 1
    else:
        controller.stop()
    result = finish(controller)
    assert result["status"] in {"cancelled", "failed"}
    assert not result.get("cleanup_pending")
    assert controller.orphans == []
    assert controller.status()["allocation_requested"] is False
    assert controller.active is None
    assert transport.executed == []
    assert transport.uploaded == []


@pytest.mark.parametrize("reason", ["cancel", "manager_error"])
def test_cancelled_queue_with_late_attachment_still_never_touches_the_vm(managed, reason):
    controller, transport, _ = managed
    with controller.execution_lock:
        controller.submit(JOB, ATTEMPT)
        if reason == "cancel":
            controller.cancel(JOB, ATTEMPT)
        else:
            controller.manager_update(ManagerUpdate(phase="error", error="allocation"))
        controller.attach(session(), 90)
    assert finish(controller)["status"] == ("cancelled" if reason == "cancel" else "failed")
    assert controller.orphans == []
    assert transport.executed == []
    assert transport.uploaded == []


def test_controller_restart_fails_unattached_queue_without_cleanup_debt(managed):
    controller, transport, _ = managed
    controller.submit(JOB, ATTEMPT)
    controller.stop()
    restarted = Controller(
        controller.data, controller.state, controller.source, transport=transport
    )
    assert restarted.attempt(JOB, ATTEMPT)["status"] in {"cancelled", "failed"}
    assert restarted.orphans == []
    assert restarted.status()["allocation_requested"] is False
    assert transport.executed == []


def test_manager_heartbeat_expiry_blocks_new_jobs_until_renewed(managed):
    controller, transport, _ = managed
    controller.manager_until = time.time() - 1
    assert controller.status()["can_allocate"] is False
    assert controller.status()["state"] == "unavailable"
    with pytest.raises(ColabUnavailable):
        controller.submit(JOB, ATTEMPT)
    with pytest.raises(KeyError):
        controller.attempt(JOB, ATTEMPT)
    controller.manager_update(ManagerUpdate())
    assert controller.status()["can_allocate"] is True
    assert controller.status()["state"] == "idle"
    assert transport.executed == []


def test_managed_runtime_also_requires_a_healthy_manager_for_new_jobs(managed):
    controller, transport, _ = managed
    controller.attach(session(), 90)
    controller.manager_update(ManagerUpdate(phase="error", error="authentication"))
    with pytest.raises(ColabUnavailable, match="인증"):
        controller.submit(JOB, ATTEMPT)
    assert transport.executed == []


def test_private_manager_heartbeat_rejects_authentication_and_invalid_payloads(managed):
    controller, _, _ = managed
    with TestClient(create_controller(controller, SECRET)) as client:
        assert client.post("/v1/manager", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + SECRET}
        for payload in (
            {"phase": "running"},
            {"error": "secret account traceback"},
            {"refresh_token": "account-secret"},
            {"lease_seconds": 999},
        ):
            assert client.post("/v1/manager", json=payload, headers=headers).status_code == 422
        response = client.post(
            "/v1/manager", json={"phase": "idle", "lease_seconds": 30}, headers=headers
        )
        assert response.status_code == 200
        assert response.json()["can_allocate"] is True


def test_existing_owned_runtime_is_reused_across_manager_restart(managed, tmp_path):
    controller, transport, _ = managed
    state = tmp_path / "manager"
    manager = FakeManager(state, controller)
    manager.account = session()
    manager.sync()
    manager.record_owned(manager.account[NAME])
    controller.attach(session(), 90)
    controller.prepared_runtime = controller.runtime_id
    restarted = FakeManager(state, controller)
    restarted.account = session()
    restarted.step()
    assert restarted.count("new") == 0
    assert restarted.count("stop") == 0
    assert controller.status()["prepared"] is True
    assert len(transport.attached) == 2


def test_idle_owned_runtime_is_released_and_release_is_confirmed(managed, tmp_path):
    controller, transport, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    manager.account = session()
    manager.sync()
    manager.record_owned(manager.account[NAME])
    controller.attach(session(), 90)
    old_runtime = controller.runtime_id
    controller.last_activity -= controller.idle_seconds + 1
    manager.step()
    assert manager.count("new") == 0
    assert manager.count("stop") == 1
    assert not manager.owned.exists()
    assert not manager.intent.exists()
    assert not controller.attached()
    assert old_runtime in controller.confirmed_released
    assert transport.executed == []


def test_idle_release_race_preserves_runtime_if_another_job_has_started(managed, tmp_path):
    controller, _, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    manager.account = session()
    manager.sync()
    manager.record_owned(manager.account[NAME])
    controller.attach(session(), 90)
    controller.active = (JOB, ATTEMPT)
    try:
        with pytest.raises(RuntimeError, match="active"):
            manager.release()
        assert manager.count("stop") == 0
        assert manager.owned.exists()
        assert controller.attached()
    finally:
        controller.active = None


def test_release_refuses_a_named_alias_that_now_points_to_another_account_runtime(
    managed, tmp_path
):
    controller, _, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    manager.record_owned(session()[NAME])
    manager.account = session(
        endpoint="unrelated-endpoint",
        url="https://colab.research.google.com/tun/m/unrelated-endpoint",
    )
    manager.sync()
    with pytest.raises(ManagerFailure, match="cleanup"):
        manager.release(force=True)
    assert manager.count("stop") == 0
    assert manager.owned.exists()
    assert NAME in manager.account


def test_manager_never_adopts_an_existing_unowned_named_runtime(managed, tmp_path):
    controller, transport, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    manager.account = session()
    with pytest.raises(ManagerFailure, match="cleanup"):
        manager.step()
    assert manager.count("new") == 0
    assert manager.count("stop") == 0
    assert not manager.owned.exists()
    assert transport.attached == []


def test_token_refresh_rejects_a_rebound_alias_before_attachment(managed, tmp_path):
    controller, transport, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    old = session(
        token_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=500)).isoformat()
    )
    manager.config.write_text(json.dumps(old))
    manager.record_owned(old[NAME])
    manager.initialized = True
    manager.account = session(
        endpoint="unrelated-endpoint",
        url="https://colab.research.google.com/tun/m/unrelated-endpoint",
    )
    with pytest.raises(ManagerFailure, match="cleanup"):
        manager.step()
    assert manager.owned_endpoint() == "runtime-endpoint"
    assert manager.count("stop") == 0
    assert transport.attached == []


def test_token_refresh_keeps_ownership_if_alias_disappears_but_endpoint_remains(managed, tmp_path):
    controller, _, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    old = session(
        token_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=500)).isoformat()
    )
    manager.config.write_text(json.dumps(old))
    manager.record_owned(old[NAME])
    manager.initialized = True
    manager.account = {"renamed": old[NAME]}
    controller.attach(old, 90)
    old_runtime = controller.runtime_id
    with pytest.raises(ManagerFailure, match="cleanup"):
        manager.step()
    assert manager.owned_endpoint() == "runtime-endpoint"
    assert old_runtime not in controller.confirmed_released
    assert manager.count("stop") == 0


@pytest.mark.parametrize("kind", ["allocation", "connection"])
def test_allocation_rejection_without_new_endpoint_clears_intent_after_account_sync(
    managed, tmp_path, kind
):
    controller, _, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)

    def reject(_):
        raise ManagerFailure(kind)

    manager.on_new = reject
    controller.submit(JOB, ATTEMPT)
    with pytest.raises(ManagerFailure, match=kind):
        manager.step()
    assert manager.count("new") == 1
    assert manager.count("sessions") >= 2
    assert not manager.intent.exists()
    manager.error = kind
    manager.retry_after = time.time() + 30
    manager.step()
    assert manager.count("new") == 1


def test_failed_allocation_with_a_proven_named_assignment_releases_only_that_vm(managed, tmp_path):
    controller, _, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    unrelated = session(name="another-project", endpoint="another-endpoint")[NAME]
    manager.account = {"another-project": unrelated}

    def interrupted(value):
        value.account.update(session())
        value.sync()
        raise ManagerFailure("connection")

    manager.on_new = interrupted
    controller.submit(JOB, ATTEMPT)
    with pytest.raises(ManagerFailure, match="connection"):
        manager.step()
    assert manager.count("new") == 1
    assert manager.count("stop") == 1
    assert NAME not in manager.account
    assert manager.account["another-project"]["endpoint"] == "another-endpoint"
    assert not manager.intent.exists()
    assert not manager.owned.exists()


def test_ambiguous_allocation_blocks_retry_without_stopping_unrelated_runtime(managed, tmp_path):
    controller, _, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)
    unrelated = session(
        name="another-project",
        endpoint="another-endpoint",
        url="https://colab.research.google.com/tun/m/another-endpoint",
    )[NAME]
    manager.account = {"another-project": unrelated}

    def ambiguous(value):
        discovered = session(
            name="unrecorded",
            endpoint="unrecorded-endpoint",
            url="https://colab.research.google.com/tun/m/unrecorded-endpoint",
        )[NAME]
        value.account["unrecorded"] = discovered
        raise ManagerFailure("connection")

    manager.on_new = ambiguous
    controller.submit(JOB, ATTEMPT)
    with pytest.raises(ManagerFailure):
        manager.step()
    assert manager.intent.exists()
    assert manager.count("new") == 1
    assert manager.count("stop") == 0
    manager.error = "connection"
    manager.retry_after = 0
    controller.cancel(JOB, ATTEMPT)
    finish(controller)
    manager.step()
    assert manager.error == "cleanup"
    assert manager.count("new") == 1
    assert manager.count("stop") == 0
    assert manager.intent.exists()
    assert manager.account["another-project"]["endpoint"] == "another-endpoint"


def test_cancellation_during_account_lookup_never_allocates_a_vm(managed, tmp_path, monkeypatch):
    controller, transport, _ = managed
    manager = FakeManager(tmp_path / "manager", controller)

    def baseline():
        controller.cancel(JOB, ATTEMPT)
        return set()

    monkeypatch.setattr(manager, "account_endpoints", baseline)
    controller.submit(JOB, ATTEMPT)
    manager.step()
    assert finish(controller)["status"] == "cancelled"
    assert manager.count("new") == 0
    assert not manager.intent.exists()
    assert transport.executed == []


@pytest.mark.parametrize("failure", ["timeout", "authentication", "malformed"])
def test_account_lookup_is_bounded_and_sanitizes_failures(tmp_path, monkeypatch, failure):
    manager = Manager(tmp_path / "manager")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="account-secret")
        return subprocess.CompletedProcess(
            argv,
            2 if failure == "authentication" else 0,
            stdout="account-secret",
            stderr="credential traceback",
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ManagerFailure) as raised:
        manager.account_endpoints()
    assert raised.value.kind == ("authentication" if failure == "authentication" else "connection")
    assert "account-secret" not in str(raised.value)
    assert "credential traceback" not in str(raised.value)
    assert calls[0][0][-1] == "--list-endpoints"
    assert calls[0][1]["timeout"] <= 35
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
