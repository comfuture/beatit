import asyncio
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from pymysql import Error as DatabaseError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .access_log import install_auth_query_redaction
from .auth import install_auth, require_user
from .chunk_uploads import CHUNK_BYTES, ChunkUploads
from .events import WorkspaceEvents
from .execution import default_engine, execution_capabilities
from .models import Options, Revision, ScoreMetadata
from .origin_media import install_origin_media
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
    return min(MAX_UPLOAD, 120 * 1024**2) if default_engine() == "colab" else MAX_UPLOAD


JOB_AUDIO = {"audio.wav", "drums.wav"} | {
    f"stem-{name}.flac" for name in ("kick", "snare", "toms", "hh", "cymbals")
}
STATIC = Path(__file__).parent / "static" / "web"


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class UploadInput(BaseModel):
    filename: str = Field(min_length=1, max_length=1000)
    size: int = Field(gt=0, strict=True)
    options: Options = Field(default_factory=Options)
    project_id: str | None = None


def create_app(
    data_root: Path | None = None, start_worker: bool = True, *, store=None, auth_service=None
) -> FastAPI:
    install_auth_query_redaction()
    media_base_url = os.environ.get("BEATIT_MEDIA_BASE_URL", "")
    if media_base_url not in {"", "/media"}:
        raise RuntimeError("BEATIT_MEDIA_BASE_URL must be empty or /media.")
    store = store or JobStore(data_root or Path(os.environ.get("DRUM_DATA_DIR", "data")))
    uploads = ChunkUploads(store)
    upload_mode = os.environ.get("BEATIT_UPLOAD_MODE", "chunked")
    if upload_mode not in {"chunked", "direct"}:
        raise RuntimeError("BEATIT_UPLOAD_MODE must be chunked or direct.")

    def system_info():
        return {
            **execution_capabilities(capabilities()),
            "max_upload_bytes": upload_limit(),
            "upload_chunk_bytes": CHUNK_BYTES,
            "upload_mode": upload_mode,
            "max_duration_seconds": MAX_DURATION,
            "media_base_url": media_base_url,
        }

    events = WorkspaceEvents(store, system_info)

    async def clean_uploads():
        while True:
            try:
                await run_in_threadpool(uploads.cleanup)
            except Exception:
                logging.exception("Expired upload cleanup will be retried")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            store.start()
        cleaner = asyncio.create_task(clean_uploads())
        try:
            yield
        finally:
            cleaner.cancel()
            with suppress(asyncio.CancelledError):
                await cleaner
            await events.close()
            if start_worker:
                store.stop()
            store.close()

    app = FastAPI(title="Drum Score", lifespan=lifespan)
    app.state.store = store
    app.state.events = events
    install_auth(app, store.db, service=auth_service)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
        + os.environ.get("ALLOWED_HOSTS", "").split(","),
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        expected_origin = os.environ.get("BEATIT_PUBLIC_URL", str(request.base_url)).rstrip("/")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            (origin and origin != expected_origin)
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise_error = PlainTextResponse("Cross-origin requests are disabled", status_code=403)
            return raise_error
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith(("/api/", "/media/")):
            response.headers["Cache-Control"] = "private, no-store"
        if request.url.path.startswith("/media/"):
            response.headers["Vary"] = "Cookie"
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    async def unavailable(request, error):
        return JSONResponse(
            {"detail": "저장소에 연결할 수 없습니다. 잠시 후 다시 시도하세요."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    for exception in (
        DatabaseError,
        SQLAlchemyError,
        Boto3Error,
        BotoCoreError,
        ClientError,
        OSError,
    ):
        app.add_exception_handler(exception, unavailable)

    def get_job(identifier: str, user: dict):
        try:
            return store.get(identifier, owner_id=user["id"])
        except KeyError:
            raise HTTPException(404, "작업을 찾을 수 없습니다.") from None

    def artifact_path(identifier, relative, user):
        try:
            return store.file_path(identifier, relative, owner_id=user["id"])
        except KeyError:
            raise HTTPException(404, "파일이 없습니다.") from None

    @app.get("/api/health")
    def health():
        with store.connect() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/system")
    async def system(user: dict = Depends(require_user)):
        return await events.system_snapshot()

    @app.get("/api/events")
    async def workspace_events(
        request: Request, job_id: str | None = None, user: dict = Depends(require_user)
    ):
        if job_id:
            await run_in_threadpool(get_job, job_id, user)
        subscription = await events.subscribe(user["id"], job_id)
        cleanup = BackgroundTasks()
        cleanup.add_task(events.unsubscribe, subscription)
        return StreamingResponse(
            events.stream(subscription, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
            background=cleanup,
        )

    @app.get("/api/jobs")
    def list_jobs(q: str = "", project_id: str | None = None, user: dict = Depends(require_user)):
        if project_id:
            get_project(project_id, user)
        return store.list(owner_id=user["id"], query=q, project_id=project_id)

    def get_project(identifier: str, user: dict):
        try:
            return store.get_project(identifier, owner_id=user["id"])
        except KeyError:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.") from None

    @app.get("/api/projects")
    def projects(q: str = "", user: dict = Depends(require_user)):
        return store.list_projects(owner_id=user["id"], query=q)

    @app.post("/api/projects", status_code=201)
    def create_project(project: ProjectInput, user: dict = Depends(require_user)):
        return store.create_project(owner_id=user["id"], name=project.name.strip())

    @app.get("/api/projects/{identifier}")
    def project(identifier: str, user: dict = Depends(require_user)):
        return get_project(identifier, user)

    def validate_upload(filename, parsed):
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1][:200]
        if Path(filename).suffix.lower() not in EXTENSIONS:
            raise HTTPException(
                415, "지원하지 않는 파일 형식입니다. 오디오 또는 비디오 파일을 선택하세요."
            )
        engine = default_engine()
        if engine in {"colab", "modal"} and parsed.device not in {"auto", "cuda"}:
            raise HTTPException(422, "원격 분석은 자동 선택 또는 CUDA 장치를 사용하세요.")
        if engine in {"colab", "modal"} and parsed.renderer == "musescore":
            raise HTTPException(422, "원격 분석은 자동 선택 또는 Verovio 렌더러를 사용하세요.")
        return filename

    @app.post("/api/uploads", status_code=201)
    def create_upload(payload: UploadInput, user: dict = Depends(require_user)):
        filename = validate_upload(payload.filename, payload.options)
        if payload.size > upload_limit():
            raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
        if payload.project_id:
            get_project(payload.project_id, user)
        return uploads.create(
            user["id"], filename, payload.size, payload.options.model_dump(), payload.project_id
        )

    @app.get("/api/uploads/{identifier}")
    def upload_status(
        identifier: str, background: BackgroundTasks, user: dict = Depends(require_user)
    ):
        metadata = uploads.status(user["id"], identifier)
        if metadata["state"] == "finalizing":
            # A restart during object publication resumes on the next status read.
            background.add_task(uploads.finish, user["id"], identifier)
        return metadata

    @app.patch("/api/uploads/{identifier}")
    async def upload_chunk(
        identifier: str,
        request: Request,
        upload_offset: int = Header(ge=0),
        user: dict = Depends(require_user),
    ):
        if upload_offset % CHUNK_BYTES:
            raise HTTPException(409, "업로드 조각의 시작 위치가 올바르지 않습니다.")
        return await uploads.receive(user["id"], identifier, upload_offset, request)

    @app.put("/api/uploads/{identifier}")
    async def upload_direct(identifier: str, request: Request, user: dict = Depends(require_user)):
        if upload_mode != "direct":
            raise HTTPException(409, "이 서버는 분할 업로드를 사용합니다.")
        return await uploads.receive_direct(user["id"], identifier, request)

    @app.post("/api/uploads/{identifier}/complete", status_code=202)
    def complete_upload(
        identifier: str, background: BackgroundTasks, user: dict = Depends(require_user)
    ):
        metadata = uploads.prepare_finish(user["id"], identifier)
        if metadata["state"] == "finalizing":
            background.add_task(uploads.finish, user["id"], identifier)
        return metadata

    @app.delete("/api/uploads/{identifier}", status_code=204)
    def cancel_upload(identifier: str, user: dict = Depends(require_user)):
        uploads.cancel(user["id"], identifier)

    @app.post("/api/jobs", status_code=202)
    async def upload(
        file: UploadFile = File(...),
        options: str = Form("{}"),
        project_id: str | None = Form(None),
        user: dict = Depends(require_user),
    ):
        if project_id:
            await run_in_threadpool(get_project, project_id, user)
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
        filename = validate_upload(file.filename or "media", parsed)
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
                directory / "request.json",
                {"filename": filename, "options": parsed.model_dump()},
            )
            try:
                await run_in_threadpool(
                    store.add, identifier, filename, owner_id=user["id"], project_id=project_id
                )
            except ValueError as error:
                raise HTTPException(429, str(error)) from None
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await file.close()
        return await run_in_threadpool(get_job, identifier, user)

    @app.get("/api/jobs/{identifier}")
    def job(identifier: str, user: dict = Depends(require_user)):
        return get_job(identifier, user)

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel(identifier: str, user: dict = Depends(require_user)):
        get_job(identifier, user)
        store.cancel(identifier, owner_id=user["id"])
        return get_job(identifier, user)

    @app.post("/api/jobs/{identifier}/retry", status_code=202)
    def retry(identifier: str, user: dict = Depends(require_user)):
        get_job(identifier, user)
        try:
            store.retry(identifier, owner_id=user["id"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_job(identifier, user)

    @app.post("/api/jobs/{identifier}/score", status_code=202)
    def revise(identifier: str, revision: Revision, user: dict = Depends(require_user)):
        existing = get_job(identifier, user)
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
                owner_id=user["id"],
                preserve_events=revision.preserve_events,
                source_generation=revision.source_generation,
                client_edit_id=revision.client_edit_id,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_job(identifier, user)

    @app.post("/api/jobs/{identifier}/score-metadata", status_code=202)
    def revise_metadata(
        identifier: str, metadata: ScoreMetadata, user: dict = Depends(require_user)
    ):
        get_job(identifier, user)
        try:
            store.revise(identifier, None, metadata=metadata.model_dump(), owner_id=user["id"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_job(identifier, user)

    @app.delete("/api/jobs/{identifier}", status_code=204)
    def delete(identifier: str, user: dict = Depends(require_user)):
        get_job(identifier, user)
        store.delete(identifier, owner_id=user["id"])

    @app.get("/api/jobs/{identifier}/log", response_class=PlainTextResponse)
    def log(identifier: str, user: dict = Depends(require_user)):
        get_job(identifier, user)
        return store.log(identifier, owner_id=user["id"]) or "아직 로그가 없습니다."

    @app.get("/api/jobs/{identifier}/media-authorize")
    def media_authorize(
        identifier: str, source: str = "audio.wav", user: dict = Depends(require_user)
    ):
        if source not in JOB_AUDIO:
            raise HTTPException(404, "오디오가 없습니다.")
        try:
            manifest, content_type = store.playback_manifest(
                identifier, source, owner_id=user["id"]
            )
        except KeyError:
            raise HTTPException(404, "오디오가 없습니다.") from None
        return {
            "object_key": manifest["object_key"],
            "size": manifest["size"],
            "sha256": manifest["sha256"],
            "content_type": content_type,
        }

    @app.get("/api/jobs/{identifier}/files/{filename}")
    def artifact(
        identifier: str, filename: str, download: bool = False, user: dict = Depends(require_user)
    ):
        job = get_job(identifier, user)
        if filename in JOB_AUDIO:
            path = artifact_path(identifier, filename, user)
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
            path = artifact_path(identifier, f"{result['generation']}/{filename}", user)
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
    def layout(identifier: str, generation: str, user: dict = Depends(require_user)):
        result = get_job(identifier, user).get("result")
        if not result:
            raise HTTPException(404, "악보가 준비되지 않았습니다.")
        if generation != result["generation"]:
            raise HTTPException(409, "악보가 변경되었습니다. 다시 불러오세요.")
        try:
            # Cache objects are named by digest; engraving expects original page
            # names together. Materialize a request-local view from the manifest.
            with tempfile.TemporaryDirectory(dir=store.root, prefix="layout-") as scratch:
                directory = Path(scratch)
                for page in result.get("pages", []):
                    if Path(page).name != page:
                        raise ValueError("Invalid score page")
                    source = artifact_path(identifier, f"{generation}/{page}", user)
                    (directory / page).symlink_to(source)
                measures = score_layout(directory, result)
        except (OSError, ValueError):
            measures = []
        return {"generation": generation, "measures": measures}

    @app.get("/api/jobs/{identifier}/waveform")
    def waveform(identifier: str, source: str = "audio.wav", user: dict = Depends(require_user)):
        job = get_job(identifier, user)
        if source not in JOB_AUDIO or not job.get("result"):
            raise HTTPException(404, "오디오가 준비되지 않았습니다.")
        path = artifact_path(identifier, source, user)
        stat = path.stat()
        try:
            return waveform_peaks(str(path), stat.st_mtime_ns, stat.st_size)
        except (RuntimeError, ValueError):
            raise HTTPException(422, "파형을 읽을 수 없습니다.") from None

    install_origin_media(app, store)

    if (STATIC / "index.html").is_file():

        @app.get("/sessions/{identifier}", include_in_schema=False)
        def session_workspace(identifier: str):
            # Serve only the shell here. Every session payload still passes the
            # authenticated owner checks on /api/jobs; arbitrary paths stay 404.
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                raise HTTPException(404, "세션 주소가 올바르지 않습니다.")
            return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/", StaticFiles(directory=STATIC, html=True), name="web")
    else:

        @app.get("/", response_class=PlainTextResponse, status_code=503)
        def missing_frontend():
            return "Build the Beatit UI first: cd frontend && npm ci && npm run build"

    return app
