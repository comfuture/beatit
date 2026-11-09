"""Private Modal RPC, bounded disk transfers, progress and remote cancellation."""

import asyncio
import json
import logging
import signal
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import modal

from .colab_artifacts import (
    MAX_RESULT_BYTES,
    import_output,
    pack_input,
    save_json,
    valid_identifier,
)
from .modal_config import (
    APP_NAME,
    ENVIRONMENT,
    GPU,
    JOB_VOLUME,
    MANIFEST,
    MODEL_FINGERPRINT,
    MODEL_VOLUME,
    PROGRESS_DICT,
)
from .modal_credentials import load_credentials

CALL_FILE = "modal-call.json"


def function(name):
    return modal.Function.from_name(APP_NAME, name, environment_name=ENVIRONMENT)


def volume(name):
    return modal.Volume.from_name(name, environment_name=ENVIRONMENT)


async def _runtime_status():
    worker = function("transcribe")
    await worker.hydrate.aio()
    prepared = False
    try:
        content = b"".join([chunk async for chunk in volume(MODEL_VOLUME).read_file.aio(MANIFEST)])
        prepared = json.loads(content).get("fingerprint") == MODEL_FINGERPRINT
    except (FileNotFoundError, modal.exception.NotFoundError):
        pass  # Image storage or a first run; CPU preparation verifies the deployed mode.
    return prepared


def runtime_status():
    result = {
        "state": "idle",
        "accelerator": GPU,
        "prepared": False,
        "allocation_mode": "server",
        "idle_timeout_seconds": 2,
        "can_allocate": True,
    }
    try:
        load_credentials()
        result["prepared"] = asyncio.run(asyncio.wait_for(_runtime_status(), timeout=2))
    except Exception:
        result.update(
            state="unavailable",
            can_allocate=False,
            manager_error="Modal 연결 설정을 확인하세요. 인증 후 beatit 앱을 배포해야 합니다.",
        )
    return result


def download(volume, remote: str, destination: Path):
    size = 0
    with destination.open("wb") as output:
        for chunk in volume.read_file(remote):
            size += len(chunk)
            if size > MAX_RESULT_BYTES:
                raise ValueError("Modal result archive exceeds its size limit")
            output.write(chunk)


def cancel_record(directory: Path):
    """Also used after server restart or a forced worker termination."""
    record = directory / CALL_FILE
    if not record.is_file():
        return
    load_credentials()
    value = json.loads(record.read_text())
    modal.FunctionCall.from_id(value["call_id"]).cancel(terminate_containers=True)
    record.unlink(missing_ok=True)


def run(directory: Path):
    load_credentials()
    started = time.monotonic()
    identifier = directory.name if valid_identifier(directory.name) else uuid4().hex
    key = identifier + "/" + uuid4().hex
    job, attempt = key.split("/")
    calls = []
    transferred = False
    phase_seconds = {}
    jobs = volume(JOB_VOLUME)
    progress = modal.Dict.from_name(
        PROGRESS_DICT, environment_name=ENVIRONMENT, create_if_missing=True
    )

    def report(stage, fraction, message):
        save_json(
            directory / "progress.json",
            {
                "stage": stage,
                "progress": fraction,
                "message": message,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            },
        )

    def cancel(signum=None, frame=None):
        for call in calls:
            try:
                call.cancel(terminate_containers=True)
            except Exception:
                logging.exception("Could not cancel Modal call; retained its recovery record")
            else:
                (directory / CALL_FILE).unlink(missing_ok=True)
        if signum is not None:
            raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        # Cancel a previously interrupted attempt before scheduling any more paid work.
        cancel_record(directory)
        report("preparing", 0.01, "Modal 이미지와 모델 캐시를 확인합니다.")
        preparation = function("prepare_models").spawn()
        calls.append(preparation)
        save_json(directory / CALL_FILE, {"call_id": preparation.object_id})
        deadline = time.monotonic() + 1800
        while True:
            try:
                manifest = preparation.get(timeout=1)
                break
            except (TimeoutError, modal.exception.TimeoutError):
                if time.monotonic() > deadline:
                    raise TimeoutError("Modal 모델 준비 시간이 초과되었습니다.") from None
        calls.clear()
        (directory / CALL_FILE).unlink(missing_ok=True)
        if manifest.get("fingerprint") != MODEL_FINGERPRINT:
            raise RuntimeError("Modal 모델 버전이 다릅니다. 앱을 다시 배포하세요.")
        phase_seconds["model_preparation"] = round(time.monotonic() - started, 3)

        with tempfile.TemporaryDirectory(prefix=".modal-", dir=directory) as scratch:
            scratch = Path(scratch)
            phase_started = time.monotonic()
            pack_input(directory, scratch / "input.zip")
            phase_seconds["pack_input"] = round(time.monotonic() - phase_started, 3)
            phase_started = time.monotonic()
            report("uploading", 0.03, "Modal로 분석할 오디오를 전송합니다.")
            transferred = True
            with jobs.batch_upload() as upload:
                upload.put_file(scratch / "input.zip", key + "/input.zip")
            phase_seconds["upload"] = round(time.monotonic() - phase_started, 3)
            phase_started = time.monotonic()
            report("allocating", 0.05, "준비된 Modal 이미지에서 GPU를 시작합니다.")
            call = function("transcribe").spawn(job, attempt)
            calls.append(call)
            save_json(directory / CALL_FILE, {"call_id": call.object_id})
            deadline = time.monotonic() + 7500
            while True:
                try:
                    provenance = call.get(timeout=1)
                    calls.clear()
                    (directory / CALL_FILE).unlink(missing_ok=True)
                    break
                except (TimeoutError, modal.exception.TimeoutError):
                    if time.monotonic() > deadline:
                        raise TimeoutError("Modal 분석 대기 시간이 초과되었습니다.") from None
                remote = progress.get(key)
                if remote:
                    report(remote["stage"], 0.05 + 0.85 * remote["progress"], remote["message"])
            phase_seconds["gpu_call"] = round(time.monotonic() - phase_started, 3)
            phase_started = time.monotonic()
            report("downloading", 0.92, "Modal 분석 결과를 가져옵니다.")
            download(jobs, key + "/result.zip", scratch / "result.zip")
            phase_seconds["download"] = round(time.monotonic() - phase_started, 3)
            phase_started = time.monotonic()
            report("publishing", 0.97, "분석 결과와 악보 파일을 검증합니다.")
            provenance.update(
                app=APP_NAME,
                environment=ENVIRONMENT,
                model_fingerprint=manifest["fingerprint"],
                model_storage=manifest["storage"],
                preparation_call_id=preparation.object_id,
                model_preparation=manifest.get("preparation"),
                call_id=call.object_id,
            )
            import_output(
                scratch / "result.zip",
                directory,
                provenance,
                backend="modal",
            )
            phase_seconds["validate_import"] = round(time.monotonic() - phase_started, 3)
            provenance.update(
                phase_seconds=phase_seconds,
                total_elapsed_seconds=round(time.monotonic() - started, 3),
            )
            for name in ("analysis.json", "result.json"):
                path = directory / name
                if path.is_file():
                    result = json.loads(path.read_text())
                    result["execution_runtime"] = provenance
                    save_json(path, result)
            report("complete", 1, "악보 생성 완료")
    finally:
        cancel()
        if transferred:
            try:
                jobs.remove_file(key, recursive=True)
                progress.pop(key, None)
            except Exception:
                logging.exception("Could not remove temporary Modal job files: %s", key)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
