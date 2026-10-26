import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .colab_worker import execution_capabilities
from .models import Options, Revision, ScoreMetadata
from .pipeline import MAX_DURATION, save_json
from .runtime import capabilities
from .score_layout import score_layout
from .store import JobStore
from .waveform import waveform_peaks

EXTENSIONS = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".aiff",
    ".aif",
    ".wma",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
}
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_BYTES", str(1024**3)))


def upload_limit():
    return (
        min(MAX_UPLOAD, 120 * 1024**2)
        if os.environ.get("DRUM_EXECUTION_BACKEND") == "colab"
        else MAX_UPLOAD
    )


JOB_AUDIO = {"audio.wav", "drums.wav"} | {
    f"stem-{name}.flac" for name in ("kick", "snare", "toms", "hh", "cymbals")
}
STATIC = Path(__file__).parent / "static" / "web"


def create_app(data_root: Path | None = None, start_worker: bool = True) -> FastAPI:
    store = JobStore(data_root or Path(os.environ.get("DRUM_DATA_DIR", "data")))

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            store.start()
        yield
        if start_worker:
            store.stop()

    app = FastAPI(title="Drum Score", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
        + os.environ.get("ALLOWED_HOSTS", "").split(","),
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        # Prevent another web site from submitting local jobs via a browser form.
        origin = request.headers.get("origin")
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            raise_error = PlainTextResponse("Cross-origin requests are disabled", status_code=403)
            return raise_error
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def get_job(identifier: str):
        try:
            return store.get(identifier)
        except KeyError:
            raise HTTPException(404, "작업을 찾을 수 없습니다.") from None

    @app.get("/api/system")
    def system():
        return {
            **execution_capabilities(capabilities()),
            "max_upload_bytes": upload_limit(),
            "max_duration_seconds": MAX_DURATION,
        }

    @app.get("/api/jobs")
    def list_jobs():
        return store.list()

    @app.post("/api/jobs", status_code=202)
    async def upload(file: UploadFile = File(...), options: str = Form("{}")):
        filename = (file.filename or "media").replace("\\", "/").rsplit("/", 1)[-1][:200]
        if Path(filename).suffix.lower() not in EXTENSIONS:
            raise HTTPException(
                415, "지원하지 않는 파일 형식입니다. 오디오 또는 비디오 파일을 선택하세요."
            )
        try:
            parsed = Options.model_validate_json(options)
        except ValidationError as error:
            raise HTTPException(
                422,
                [
                    {"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
                    for item in error.errors()
                ],
            ) from None
        if os.environ.get("DRUM_EXECUTION_BACKEND") == "colab" and parsed.device not in {
            "auto",
            "cuda",
        }:
            raise HTTPException(422, "Colab 분석은 자동 선택 또는 CUDA 장치를 사용하세요.")
        identifier = uuid4().hex
        directory = store.directory(identifier)
        directory.mkdir()
        size = 0
        try:
            with (directory / "input").open("wb") as destination:
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > upload_limit():
                        raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
                    destination.write(chunk)
            if size == 0:
                raise HTTPException(400, "빈 파일은 분석할 수 없습니다.")
            save_json(
                directory / "request.json", {"filename": filename, "options": parsed.model_dump()}
            )
            store.add(identifier, filename)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await file.close()
        return get_job(identifier)

    @app.get("/api/jobs/{identifier}")
    def job(identifier: str):
        return get_job(identifier)

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel(identifier: str):
        get_job(identifier)
        store.cancel(identifier)
        return get_job(identifier)

    @app.post("/api/jobs/{identifier}/retry", status_code=202)
    def retry(identifier: str):
        get_job(identifier)
        try:
            store.retry(identifier)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_job(identifier)

    @app.post("/api/jobs/{identifier}/score", status_code=202)
    def revise(identifier: str, revision: Revision):
        existing = get_job(identifier)
        if revision.events is not None:
            duration = existing.get("result", {}).get("duration", 0)
            if any(event.time >= duration for event in revision.events):
                raise HTTPException(422, "타격 시간은 오디오 길이보다 짧아야 합니다.")
        try:
            store.revise(
                identifier,
                revision.options.model_dump(),
                [e.model_dump() for e in revision.events] if revision.events is not None else None,
                metadata=revision.metadata.model_dump() if revision.metadata is not None else None,
                beat_review=revision.beat_review.model_dump()
                if revision.beat_review is not None
                else None,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_job(identifier)

    @app.post("/api/jobs/{identifier}/score-metadata", status_code=202)
    def revise_metadata(identifier: str, metadata: ScoreMetadata):
        get_job(identifier)
        try:
            store.revise(identifier, None, metadata=metadata.model_dump())
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_job(identifier)

    @app.delete("/api/jobs/{identifier}", status_code=204)
    def delete(identifier: str):
        get_job(identifier)
        store.delete(identifier)

    @app.get("/api/jobs/{identifier}/log", response_class=PlainTextResponse)
    def log(identifier: str):
        get_job(identifier)
        path = store.directory(identifier) / "worker.log"
        if not path.is_file():
            return "아직 로그가 없습니다."
        with path.open("rb") as file:
            file.seek(max(0, path.stat().st_size - 64000))
            return file.read().decode("utf-8", errors="replace")

    @app.get("/api/jobs/{identifier}/files/{filename}")
    def artifact(identifier: str, filename: str, download: bool = False):
        job = get_job(identifier)
        directory = store.directory(identifier)
        if filename in JOB_AUDIO:
            path = directory / filename
        else:
            result = job.get("result", {})
            allowed = {
                "score.musicxml",
                "score.mid",
                "performance.mid",
                "score.pdf",
                "score-bundle.zip",
                "events.json",
                "analysis.json",
                "engraver.log",
                "tom-refinement.json",
            } | set(result.get("pages", []))
            if filename not in allowed or not result.get("generation"):
                raise HTTPException(404, "파일이 없습니다.")
            path = directory / result["generation"] / filename
        if not path.is_file():
            raise HTTPException(404, "파일이 없습니다.")
        media_type = {
            ".svg": "image/svg+xml",
            ".musicxml": "application/vnd.recordare.musicxml+xml",
            ".wav": "audio/wav",
            ".flac": "audio/flac",
            ".mid": "audio/midi",
            ".json": "application/json",
            ".zip": "application/zip",
            ".pdf": "application/pdf",
        }.get(path.suffix)
        return FileResponse(path, media_type=media_type, filename=path.name if download else None)

    @app.get("/api/jobs/{identifier}/score-layout")
    def layout(identifier: str, generation: str):
        result = get_job(identifier).get("result")
        if not result:
            raise HTTPException(404, "악보가 준비되지 않았습니다.")
        if generation != result["generation"]:
            raise HTTPException(409, "악보가 변경되었습니다. 다시 불러오세요.")
        try:
            measures = score_layout(store.directory(identifier) / generation, result)
        except (OSError, ValueError):
            measures = []
        return {"generation": generation, "measures": measures}

    @app.get("/api/jobs/{identifier}/waveform")
    def waveform(identifier: str, source: str = "audio.wav"):
        job = get_job(identifier)
        if source not in JOB_AUDIO or not job.get("result"):
            raise HTTPException(404, "오디오가 준비되지 않았습니다.")
        path = store.directory(identifier) / source
        if not path.is_file():
            raise HTTPException(404, "오디오가 없습니다.")
        stat = path.stat()
        try:
            return waveform_peaks(str(path), stat.st_mtime_ns, stat.st_size)
        except (RuntimeError, ValueError):
            raise HTTPException(422, "파형을 읽을 수 없습니다.") from None

    if (STATIC / "index.html").is_file():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="web")
    else:

        @app.get("/", response_class=PlainTextResponse, status_code=503)
        def missing_frontend():
            return "Build the Beatit UI first: cd frontend && npm ci && npm run build"

    return app
