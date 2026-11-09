import json
import signal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from transcription import execution, modal_credentials, modal_models, modal_worker
from transcription.app import create_app
from transcription.colab_artifacts import import_output
from transcription.modal_config import MANIFEST, MODEL_FINGERPRINT


@pytest.mark.parametrize("engine", ["modal", "local", "colab"])
@pytest.mark.parametrize(
    "job_request",
    [{}, {"revision": True}, {"metadata_only": True}, {"revision": True, "reestimate_tempo": True}],
)
def test_service_engine_dispatches_only_inference(tmp_path, monkeypatch, engine, job_request):
    monkeypatch.setenv("DRUM_EXECUTION_BACKEND", engine)
    (tmp_path / "request.json").write_text(json.dumps(job_request))
    calls = []
    for name, module in [
        ("local", "pipeline"),
        ("colab", "colab_worker"),
        ("modal", "modal_worker"),
    ]:
        monkeypatch.setattr(
            "transcription." + module + ".run", lambda _, name=name: calls.append(name)
        )
    execution.run(tmp_path)
    inference = not job_request.get("revision") and not job_request.get("metadata_only")
    assert calls == [engine if inference or job_request.get("reestimate_tempo") else "local"]


def test_local_is_the_default_and_invalid_configuration_fails(monkeypatch):
    monkeypatch.delenv("DRUM_EXECUTION_BACKEND", raising=False)
    assert execution.default_engine() == "local"
    monkeypatch.setenv("DRUM_EXECUTION_BACKEND", "typo")
    with pytest.raises(ValueError, match="Unknown processing engine"):
        execution.default_engine()


def test_api_uses_remote_tools_without_requiring_local_ffmpeg_or_cuda(tmp_path, monkeypatch):
    monkeypatch.setenv("DRUM_EXECUTION_BACKEND", "modal")
    monkeypatch.setattr(
        "transcription.app.capabilities", lambda: {"ffmpeg": False, "devices": ["cpu"]}
    )
    monkeypatch.setattr(
        modal_worker, "runtime_status", lambda: {"state": "idle", "can_allocate": True}
    )
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        system = client.get("/api/system").json()
        assert system["execution_backend"] == "modal"
        assert system["ffmpeg"] and system["devices"] == ["cuda"]
        for options in [{"device": "cpu"}, {"renderer": "musescore"}, {"engine": "local"}]:
            assert (
                client.post(
                    "/api/jobs",
                    files={"file": ("a.wav", b"x")},
                    data={"options": json.dumps(options)},
                ).status_code
                == 422
            )
        assert client.post("/api/jobs", files={"file": ("a.wav", b"x")}).status_code == 202


def test_file_credentials_are_loaded_without_returning_secret_values(tmp_path, monkeypatch):
    for name, secret in [
        ("MODAL_TOKEN_ID", "ak-test-id"),
        ("MODAL_TOKEN_SECRET", "as-test-secret"),
    ]:
        monkeypatch.setenv(name, "")
        file = tmp_path / name
        file.write_text(secret + "\n")
        monkeypatch.setenv(name + "_FILE", str(file))
    assert modal_credentials.load_credentials() is None
    import os

    assert os.environ["MODAL_TOKEN_ID"] == "ak-test-id"
    assert os.environ["MODAL_TOKEN_SECRET"] == "as-test-secret"


@pytest.mark.parametrize("mode", ["partial", "missing-file", "empty-file"])
def test_bad_credentials_fail_without_disclosing_values(tmp_path, monkeypatch, mode):
    for name in (
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_TOKEN_ID_FILE",
        "MODAL_TOKEN_SECRET_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODAL_TOKEN_ID", "private-id")
    if mode != "partial":
        path = tmp_path / "secret"
        if mode == "empty-file":
            path.touch()
        monkeypatch.setenv("MODAL_TOKEN_SECRET_FILE", str(path))
    with pytest.raises(RuntimeError) as error:
        modal_credentials.load_credentials()
    assert "private-id" not in str(error.value)


def test_runtime_authentication_failure_is_safe(monkeypatch):
    async def fail():
        raise RuntimeError("private authentication detail")

    monkeypatch.setattr(modal_worker, "load_credentials", lambda: None)
    monkeypatch.setattr(modal_worker, "_runtime_status", fail)
    status = modal_worker.runtime_status()
    assert status["state"] == "unavailable" and not status["can_allocate"]
    assert "private" not in json.dumps(status)


def test_prepared_model_cache_is_reused_without_loading_models(tmp_path, monkeypatch):
    monkeypatch.setattr(modal_models, "MODEL_DIR", str(tmp_path))
    monkeypatch.setitem(modal_models.CACHE_ENV, "XDG_CACHE_HOME", str(tmp_path / MODEL_FINGERPRINT))
    manifest = tmp_path / MANIFEST
    manifest.parent.mkdir()
    checkpoint = manifest.parent / "model.ckpt"
    checkpoint.write_bytes(b"123")
    manifest.write_text(
        json.dumps(
            {
                "fingerprint": MODEL_FINGERPRINT,
                "bytes": 123,
                "files": [{"path": "model.ckpt", "bytes": 3}],
            }
        )
    )
    assert modal_models.prepare_models()["bytes"] == 123
    checkpoint.unlink()
    assert modal_models.model_manifest() is None
    manifest.write_text('{"fingerprint":"old"}')
    assert modal_models.model_manifest() is None


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    (directory / "input").write_bytes(b"source")
    (directory / "request.json").write_text('{"filename":"input.wav","options":{}}')
    calls = []
    handlers = {}
    monkeypatch.setattr(modal_worker, "load_credentials", lambda: None)
    monkeypatch.setattr(
        modal_worker.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    class Call:
        object_id = "fc-test"
        cancelled = False

        def __init__(self, result):
            self.result = result
            self.polls = 0

        def get(self, timeout):
            self.polls += 1
            if isinstance(self.result, BaseException):
                raise self.result
            if self.polls == 1:
                raise TimeoutError()
            return self.result

        def cancel(self, *, terminate_containers):
            assert terminate_containers
            self.cancelled = True

    preparation = Call({"fingerprint": MODEL_FINGERPRINT, "storage": "volume"})
    inference = Call({"gpu": "T4"})

    def function(name):
        def spawn(*args):
            calls.append(name)
            return preparation if name == "prepare_models" else inference

        return SimpleNamespace(spawn=spawn)

    class Volume:
        removed = []
        uploaded = []

        @contextmanager
        def batch_upload(self):
            yield self

        def put_file(self, source, target):
            self.uploaded.append(target)
            assert source.is_file()

        def read_file(self, path):
            yield b"result payload"

        def remove_file(self, path, recursive):
            assert recursive
            self.removed.append(path)

    volume = Volume()
    progress = {}
    monkeypatch.setattr(modal_worker, "function", function)
    monkeypatch.setattr(modal_worker, "volume", lambda _: volume)

    def progress_dict(*args, **kwargs):
        # A new deployment has no Dict until the client or remote function creates it.
        if not kwargs.get("create_if_missing"):
            raise modal_worker.modal.exception.NotFoundError("Progress Dict does not exist")
        return progress

    monkeypatch.setattr(modal_worker.modal.Dict, "from_name", progress_dict)
    published = []

    def publish(archive, directory, provenance, *, backend):
        assert archive.read_bytes() == b"result payload"
        published.append((backend, provenance))
        (directory / "result.json").write_text('{"execution_backend":"modal"}')

    monkeypatch.setattr(modal_worker, "import_output", publish)
    return SimpleNamespace(
        directory=directory,
        calls=calls,
        preparation=preparation,
        inference=inference,
        volume=volume,
        published=published,
        handlers=handlers,
    )


def test_modal_prepares_on_cpu_before_gpu_and_cleans_transfer_files(rpc):
    modal_worker.run(rpc.directory)
    assert rpc.calls == ["prepare_models", "transcribe"]
    assert rpc.published[0][0] == "modal"
    assert rpc.published[0][1]["model_fingerprint"] == MODEL_FINGERPRINT
    assert rpc.volume.removed and rpc.volume.uploaded
    assert not (rpc.directory / modal_worker.CALL_FILE).exists()
    assert not list(rpc.directory.glob(".modal-*"))
    assert not rpc.inference.cancelled
    assert json.loads((rpc.directory / "progress.json").read_text())["stage"] == "complete"
    runtime = json.loads((rpc.directory / "result.json").read_text())["execution_runtime"]
    assert runtime["total_elapsed_seconds"] >= sum(runtime["phase_seconds"].values()) - 0.01
    assert runtime["phase_seconds"]["validate_import"] >= 0


def test_smoke_directory_name_does_not_become_an_invalid_remote_job_id(rpc):
    directory = rpc.directory.parent / "modal-first"
    rpc.directory.rename(directory)
    modal_worker.run(directory)
    from transcription.colab_artifacts import valid_identifier

    identifier = rpc.volume.uploaded[0].split("/")[0]
    assert valid_identifier(identifier)


def test_failed_model_preparation_never_allocates_gpu(rpc):
    rpc.preparation.result = RuntimeError("download failed")
    with pytest.raises(RuntimeError, match="download failed"):
        modal_worker.run(rpc.directory)
    assert rpc.calls == ["prepare_models"]
    assert rpc.preparation.cancelled and not rpc.volume.uploaded
    assert not rpc.published


def test_cancel_terminates_remote_container_and_does_not_publish(rpc):
    def cancel_during_get(timeout):
        rpc.handlers[signal.SIGTERM](signal.SIGTERM)

    rpc.inference.get = cancel_during_get
    with pytest.raises(SystemExit):
        modal_worker.run(rpc.directory)
    assert rpc.inference.cancelled
    assert rpc.volume.removed and not rpc.published
    assert not (rpc.directory / "result.json").exists()


def test_interrupted_call_is_cancelled_from_persisted_record(tmp_path, monkeypatch):
    calls = []
    record = tmp_path / modal_worker.CALL_FILE
    record.write_text('{"call_id":"fc-old"}')
    monkeypatch.setattr(modal_worker, "load_credentials", lambda: None)
    monkeypatch.setattr(
        modal_worker.modal.FunctionCall,
        "from_id",
        lambda call_id: SimpleNamespace(cancel=lambda **kwargs: calls.append((call_id, kwargs))),
    )
    modal_worker.cancel_record(tmp_path)
    assert calls == [("fc-old", {"terminate_containers": True})]
    assert not record.exists()


def test_failed_cancel_remains_recoverable_after_job_deletion(rpc, monkeypatch):
    from transcription.store import JobStore

    rpc.inference.result = RuntimeError("Connection lost")

    def failed_cancel(**kwargs):
        raise ConnectionError("Cancellation could not reach Modal")

    rpc.inference.cancel = failed_cancel
    with pytest.raises(RuntimeError, match="Connection lost"):
        modal_worker.run(rpc.directory)
    record = modal_worker.call_record(rpc.directory)
    assert record.is_file()
    store = JobStore(rpc.directory.parent)
    store.add(rpc.directory.name, "input.wav")
    store.update(rpc.directory.name, "failed")
    store.delete(rpc.directory.name)
    assert not rpc.directory.exists() and record.is_file()
    recovered = []
    monkeypatch.setattr(
        modal_worker.modal.FunctionCall,
        "from_id",
        lambda call_id: SimpleNamespace(cancel=lambda **kwargs: recovered.append(call_id)),
    )
    store.start()
    store.stop()
    assert recovered == ["fc-test"]
    assert not record.exists()


@pytest.mark.parametrize("failure", ["credentials", "network"])
def test_legacy_record_is_migrated_before_failed_startup_cancellation(
    tmp_path, monkeypatch, failure
):
    from transcription.store import JobStore

    directory = tmp_path / ("c" * 32)
    directory.mkdir()
    (directory / modal_worker.CALL_FILE).write_text('{"call_id":"fc-legacy"}')
    store = JobStore(tmp_path)
    store.add(directory.name, "legacy.wav")
    store.update(directory.name, "cancelled")

    def fail(*args, **kwargs):
        raise ConnectionError("Unavailable")

    monkeypatch.setattr(
        modal_worker, "load_credentials", fail if failure == "credentials" else lambda: None
    )
    monkeypatch.setattr(
        modal_worker.modal.FunctionCall,
        "from_id",
        lambda _: SimpleNamespace(cancel=fail),
    )
    store.start()
    store.stop()
    record = modal_worker.call_record(directory)
    assert record.is_file()
    store.delete(directory.name)
    assert not directory.exists() and record.is_file()
    recovered = []
    monkeypatch.setattr(modal_worker, "load_credentials", lambda: None)
    monkeypatch.setattr(
        modal_worker.modal.FunctionCall,
        "from_id",
        lambda call_id: SimpleNamespace(cancel=lambda **kwargs: recovered.append(call_id)),
    )
    restarted = JobStore(tmp_path)
    restarted.start()
    restarted.stop()
    assert recovered == ["fc-legacy"]
    assert not record.exists()


def test_result_download_has_a_compressed_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(modal_worker, "MAX_RESULT_BYTES", 3)
    with pytest.raises(ValueError, match="size limit"):
        modal_worker.download(
            SimpleNamespace(read_file=lambda _: [b"abcd"]), "result.zip", tmp_path / "out"
        )


def test_modal_import_keeps_validation_and_marks_actual_engine(tmp_path):
    from test_colab import result_entries, write_archive

    archive = write_archive(tmp_path / "result.zip", result_entries())
    result = import_output(archive, tmp_path, {"gpu": "T4"}, backend="modal")
    assert result["execution_backend"] == "modal"
    assert json.loads((tmp_path / "analysis.json").read_text())["execution_backend"] == "modal"
