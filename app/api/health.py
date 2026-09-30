from __future__ import annotations

import redis.asyncio as redis
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live():
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response):
    settings = get_settings()
    checks = {"mysql": False, "redis": False}
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["mysql"] = True
    except Exception:
        pass

    client = redis.from_url(settings.redis_url)
    try:
        checks["redis"] = bool(await client.ping())
    except Exception:
        pass
    finally:
        await client.aclose()

    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}
