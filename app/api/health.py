from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.core.redis_client import get_redis
from app.core.schema_version import EXPECTED_SCHEMA_VERSION, read_schema_version

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live():
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response):
    checks = {"mysql": False, "schema": False, "redis": False}
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["mysql"] = True
        actual = await read_schema_version(engine)
        checks["schema"] = actual == EXPECTED_SCHEMA_VERSION
    except Exception:
        pass

    client = get_redis()
    try:
        checks["redis"] = bool(await client.ping())
    except Exception:
        pass
    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}
