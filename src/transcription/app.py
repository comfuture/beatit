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

from .models import Options, Revision
from .pipeline import MAX_DURATION, save_json
from .runtime import capabilities
from .store import JobStore

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
STATIC = Path(__file__).parent / "static"


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
            **capabilities(),
            "max_upload_bytes": MAX_UPLOAD,
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
        identifier = uuid4().hex
        directory = store.directory(identifier)
        directory.mkdir()
        size = 0
        try:
            with (directory / "input").open("wb") as destination:
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
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
            )
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
        if filename in {"audio.wav", "drums.wav"}:
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
            ".mid": "audio/midi",
            ".json": "application/json",
            ".zip": "application/zip",
            ".pdf": "application/pdf",
        }.get(path.suffix)
        return FileResponse(path, media_type=media_type, filename=path.name if download else None)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="web")
    return app
