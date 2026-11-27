"""Create the local HTTP service after the caller supplies its settings."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from video_agent.adapters.storage.jobs import ResourceMissing
from video_agent.api.assets import upload_router
from video_agent.api.jobs import job_router
from video_agent.api.middleware import LocalRequests
from video_agent.bootstrap import build_asset_importer, build_jobs, load_app_config
from video_agent.domain.errors import ErrorCode, VideoAgentError


def create_app(
    data_root: Path, *, token: str, config_path: Path = Path("config/defaults.yaml")
) -> FastAPI:
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("a local request token is required")
    if not (data_root / "agent.sqlite3").is_file():
        raise ValueError("initialize the local database before starting the service")
    config = load_app_config(config_path)
    limit = min(config.media.max_file_bytes, 524_288_000)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalRequests, token=token, file_limit=limit)
    app.include_router(upload_router(build_asset_importer(data_root, config), limit))
    app.include_router(job_router(build_jobs(data_root, config)))

    @app.exception_handler(ResourceMissing)
    async def missing(request: Request, exc: ResourceMissing) -> JSONResponse:
        return JSONResponse({"detail": "NOT_FOUND"}, 404)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "INVALID_INPUT"}, 422)

    @app.exception_handler(VideoAgentError)
    async def service_error(request: Request, exc: VideoAgentError) -> JSONResponse:
        status = {
            ErrorCode.MEDIA_LIMIT_EXCEEDED: 413,
            ErrorCode.CORRUPT_MEDIA: 422,
            ErrorCode.RESOURCE_BUSY: 409,
            ErrorCode.WORKER_ALREADY_RUNNING: 409,
            ErrorCode.IDEMPOTENCY_CONFLICT: 409,
            ErrorCode.INDEX_NOT_READY: 409,
        }.get(exc.code, 409)
        return JSONResponse({"detail": exc.code.value}, status)

    return app
