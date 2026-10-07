from contextlib import asynccontextmanager
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.media import router as media_router
from app.api.pixiv_auth import router as pixiv_auth_router
from app.api.jobs import router as jobs_router
from app.api.sync import router as sync_router
from app.api.library import router as library_router
from app.api.routes import router
from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.logging import configure_logging, request_id_ctx
from app.core.security import authorized
from app.db.session import engine
from app.core.redis_client import close_redis
from app.core.schema_version import ensure_schema_version

settings = get_settings()
settings.storage_root.mkdir(parents=True, exist_ok=True)
configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.schema_check_on_startup:
        version = await ensure_schema_version(engine)
        logger.info("schema_version_verified", extra={"schema_version": version})
    logger.info("application_started")
    yield
    await close_redis()
    await engine.dispose()
    logger.info("application_stopped")


app = FastAPI(title=settings.app_name, version="1.5.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_ctx.set(request_id)
    try:
        if request.url.path not in ("/health/live", "/health/ready"):
            if len(settings.api_key) < 32:
                return JSONResponse({"detail": "API_KEY is not configured"}, status_code=503, headers={"x-request-id": request_id})
            if not authorized(settings.api_key, request.headers.get("x-api-key")):
                return JSONResponse({"detail": "Invalid API key"}, status_code=401, headers={"x-request-id": request_id})
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response
    finally:
        request_id_ctx.reset(token)


app.include_router(router)
app.include_router(health_router)
app.include_router(admin_router)
app.include_router(media_router)
app.include_router(pixiv_auth_router)
app.include_router(jobs_router)
app.include_router(sync_router)
app.include_router(library_router)
