from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

EXPECTED_SCHEMA_VERSION = 3


class SchemaVersionError(RuntimeError):
    pass


async def read_schema_version(engine: AsyncEngine) -> int:
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT version FROM schema_version WHERE id = 1")
            )
            value = result.scalar_one_or_none()
    except Exception as exc:
        raise SchemaVersionError(
            "pixiv_archive schema version cannot be read; initialize the current "
            "DDL or run the required manual SQL upgrade"
        ) from exc
    if value is None:
        raise SchemaVersionError("schema_version row id=1 is missing")
    return int(value)


async def ensure_schema_version(engine: AsyncEngine) -> int:
    actual = await read_schema_version(engine)
    if actual != EXPECTED_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"pixiv_archive schema version mismatch: expected "
            f"{EXPECTED_SCHEMA_VERSION}, got {actual}. Run the manual SQL upgrade "
            "before starting this application version."
        )
    return actual
