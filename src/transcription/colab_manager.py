"""Server-side Colab lifecycle. Account credentials never enter the job controller."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from .colab_transport import validate_session
from .colab_worker import controller_request


class ManagerFailure(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


class Manager:
    def __init__(self, state: Path, *, rpc=controller_request):
        self.state = state
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        self.name = os.environ.get("COLAB_SESSION_NAME", "beatit-chunsik")
        self.config = state / "sessions.json"
        self.owned = state / "owned.json"
        self.intent = state / "allocation-intent.json"
        self.rpc = rpc
        self.stopping = threading.Event()
        self.initialized = False
        self.error = None
        self.retry_after = 0

    def command(self, *arguments, timeout=35):
        home = self.state / "home"
        home.mkdir(exist_ok=True, mode=0o700)
        try:
            with (self.state / "cli.log").open("ab") as output:
                position = output.tell()
                result = subprocess.run(
                    ["colab", "--auth", "adc", "--config", str(self.config), *arguments],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "HOME": str(home)},
                    timeout=timeout,
                )
            if result.returncode:
                with (self.state / "cli.log").open("rb") as log:
                    log.seek(position)
                    tail = log.read()[-16000:].lower()
                authentication = any(
                    word in tail
                    for word in (
                        b"invalid_grant",
                        b"invalid_rapt",
                        b"default credentials",
                        b"reauth",
                    )
                )
                raise ManagerFailure("authentication" if authentication else "allocation")
        except (OSError, subprocess.TimeoutExpired):
            raise ManagerFailure("connection") from None

    def sessions(self):
        return json.loads(self.config.read_text()) if self.config.exists() else {}

    def account_endpoints(self):
        # CLI sync only prunes existing local aliases; it does not save assignments
        # created on other machines. Reconciliation must consult the account itself.
        try:
            result = subprocess.run(
                [sys.executable, "-m", "transcription.colab_manager", "--list-endpoints"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=35,
            )
            if result.returncode:
                raise ManagerFailure("authentication" if result.returncode == 2 else "connection")
            return set(json.loads(result.stdout))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ManagerFailure("connection") from None

    def owned_endpoint(self):
        return json.loads(self.owned.read_text())["endpoint"] if self.owned.exists() else None

    def update(self, phase="idle", error=None):
        return self.rpc("/v1/manager", {"phase": phase, "error": error, "lease_seconds": 90})

    def record_owned(self, session):
        temporary = self.owned.with_suffix(".tmp")
        temporary.write_text(json.dumps({"endpoint": session["endpoint"]}))
        temporary.replace(self.owned)
        self.intent.unlink(missing_ok=True)

    def reconcile_intent(self):
        baseline = set(json.loads(self.intent.read_text()).get("endpoints_before", []))
        self.command("sessions")
        current = self.sessions()
        if self.name in current:
            if current[self.name]["endpoint"] in baseline:
                raise ManagerFailure("cleanup")
            self.record_owned(current[self.name])
            return
        endpoints = self.account_endpoints()
        if endpoints - baseline:
            # It might be our unrecorded assignment or another device's new runtime.
            # Never guess ownership, delete it, or allocate another VM around it.
            raise ManagerFailure("cleanup")
        self.intent.unlink()

    def allocate(self):
        finished = threading.Event()

        def renew():
            while not finished.wait(20):
                try:
                    self.update("allocating")
                except RuntimeError:
                    pass

        heartbeat = threading.Thread(target=renew, daemon=True, name="allocation-lease")
        heartbeat.start()
        try:
            self.command("new", "-s", self.name, "--gpu", "T4", timeout=150)
        finally:
            finished.set()
            heartbeat.join(timeout=2)

    def release(self, *, force=False):
        if self.intent.exists() and not self.owned.exists():
            self.reconcile_intent()
        expected = self.owned_endpoint()
        current = self.sessions().get(self.name)
        if current and current["endpoint"] != expected:
            raise ManagerFailure("cleanup")
        if not current and expected and expected in self.account_endpoints():
            raise ManagerFailure("cleanup")
        # A non-forced detach refuses release if a new job won the idle-release race.
        self.rpc("/v1/attachment?force=" + str(force).lower(), method="DELETE")
        if current:
            self.command("stop", "-s", self.name, timeout=60)
        if expected and expected in self.account_endpoints():
            raise ManagerFailure("cleanup")
        self.rpc("/v1/attachment?force=true&released=true", method="DELETE")
        self.owned.unlink(missing_ok=True)
        self.intent.unlink(missing_ok=True)

    def step(self):
        status = self.rpc("/v1/status")
        if self.error:
            if (
                self.error != "authentication"
                and not status["active"]
                and time.time() >= self.retry_after
            ):
                # Never retry an ambiguous allocation; an operator must reconcile it.
                if self.intent.exists():
                    self.error = "cleanup"
                elif self.error != "cleanup":
                    self.error = None
            if self.error:
                self.update("error", self.error)
                return
        self.update()
        if not self.initialized:
            self.command("sessions")
            self.initialized = True
        sessions = self.sessions()
        current = sessions.get(self.name)
        if not current and self.owned.exists():
            # A successful account sync pruned this named endpoint: it was reclaimed.
            if self.owned_endpoint() in self.account_endpoints():
                raise ManagerFailure("cleanup")
            self.rpc("/v1/attachment?force=true&released=true", method="DELETE")
            self.owned.unlink()
            status = self.rpc("/v1/status")
        if self.intent.exists():
            self.reconcile_intent()
            current = self.sessions().get(self.name)
        if not current and status.get("allocation_requested"):
            baseline = sorted(self.account_endpoints())
            status = self.rpc("/v1/status")
            if not status.get("allocation_requested"):
                return
            self.update("allocating")
            self.intent.write_text(
                json.dumps(
                    {
                        "requested_at": time.time(),
                        "endpoints_before": baseline,
                    }
                )
            )
            try:
                self.allocate()
            except ManagerFailure:
                if self.name in self.sessions():
                    self.release(force=True)
                else:
                    self.reconcile_intent()
                raise
            current = self.sessions().get(self.name)
            if not current:
                raise ManagerFailure("cleanup")
            self.record_owned(current)
        if current:
            if current["endpoint"] != self.owned_endpoint():
                raise ManagerFailure("cleanup")
            expiry = datetime.fromisoformat(current["token_expires_at"].replace("Z", "+00:00"))
            if (expiry - datetime.now(timezone.utc)).total_seconds() < 600:
                self.command("sessions")
                current = self.sessions().get(self.name)
                if not current:
                    if self.owned_endpoint() in self.account_endpoints():
                        raise ManagerFailure("cleanup")
                    self.rpc("/v1/attachment?force=true&released=true", method="DELETE")
                    self.owned.unlink(missing_ok=True)
                    return
            validate_session({self.name: current}, self.name)
            if current["endpoint"] != self.owned_endpoint():
                raise ManagerFailure("cleanup")
            attached = self.rpc(
                "/v1/attachment", {"sessions": {self.name: current}, "lease_seconds": 90}
            )
            if attached.get("release_requested"):
                self.update("releasing")
                try:
                    self.release()
                except RuntimeError as error:
                    if isinstance(error, ManagerFailure):
                        raise
                    # A job can arrive between observing idle and detaching.
                    if not self.rpc("/v1/status")["active"]:
                        raise

    def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    self.step()
                except Exception as error:
                    self.error = error.kind if isinstance(error, ManagerFailure) else "connection"
                    self.retry_after = time.time() + 30
                    try:
                        self.update("error", self.error)
                    except RuntimeError:
                        pass
                    # Never print CLI tracebacks, responses, or credentials to container logs.
                    print("Colab manager: " + self.error, flush=True)
                (self.state / "heartbeat").touch()
                self.stopping.wait(5)
        finally:
            if self.owned.exists() or self.intent.exists() or self.name in self.sessions():
                try:
                    self.release(force=True)
                except Exception:
                    print("Colab manager: cleanup pending", flush=True)


def query_account_endpoints():
    import google.auth
    from colab_cli.auth import PUBLIC_SCOPES
    from colab_cli.client import Client, Prod
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from google.auth.transport.requests import AuthorizedSession, Request

    try:
        credentials, _ = google.auth.load_credentials_from_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=PUBLIC_SCOPES
        )
        request = partial(Request(), timeout=20)
        if not credentials.valid:
            credentials.refresh(request)
        with AuthorizedSession(credentials, auth_request=request) as session:
            session.request = partial(session.request, timeout=(10, 25))
            return sorted(
                assignment.endpoint for assignment in Client(Prod(), session).list_assignments()
            )
    except (DefaultCredentialsError, RefreshError):
        raise ManagerFailure("authentication") from None


def main():
    os.umask(0o077)
    # Convert the newly consented server credential into ephemeral ADC. No Mac token is used.
    source = Path(os.environ["COLAB_OAUTH_TOKEN_FILE"])
    credential = json.loads(source.read_text())
    with tempfile.TemporaryDirectory(prefix="colab-account-") as temporary:
        adc = Path(temporary) / "authorized-user.json"
        adc.write_text(
            json.dumps(
                {
                    "type": "authorized_user",
                    **{
                        key: credential[key]
                        for key in ("client_id", "client_secret", "refresh_token")
                    },
                }
            )
        )
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc)
        manager = Manager(Path(os.environ.get("COLAB_MANAGER_STATE", "/app/colab-state")))
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: manager.stopping.set())
        manager.run()


if __name__ == "__main__":
    if sys.argv[1:] == ["--list-endpoints"]:
        try:
            print(json.dumps(query_account_endpoints()))
        except Exception as error:
            sys.exit(
                2 if isinstance(error, ManagerFailure) and error.kind == "authentication" else 3
            )
    else:
        main()
