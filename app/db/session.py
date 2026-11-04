from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi import Request
from app.core.config import get_settings
from app.core.maintenance import MaintenanceWriter, get_current_writer
from app.core.redis_client import get_redis


class GuardedSession(AsyncSession):
    async def commit(self):
        writer = get_current_writer()
        if writer is not None:
            await writer.ensure_alive()
            return await super().commit()
        # Also guard callers outside the HTTP/worker entry points.
        async with MaintenanceWriter(get_redis()) as writer:
            await writer.ensure_alive()
            return await super().commit()

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=GuardedSession)


async def get_session(request: Request):
    writes = request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path.startswith((
        "/api/illust/", "/api/novel/",
    ))
    if writes:
        async with MaintenanceWriter(get_redis()), SessionLocal() as session:
            yield session
    else:
        async with SessionLocal() as session:
            yield session
