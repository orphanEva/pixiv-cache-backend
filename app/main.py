from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.health import router as health_router
from app.core.config import get_settings
from app.db.session import engine

settings = get_settings()
settings.storage_root.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.3.0", lifespan=lifespan)
app.include_router(router)
app.include_router(health_router)
app.mount("/media", StaticFiles(directory=str(settings.storage_root)), name="media")
