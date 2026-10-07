from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work import SyncSource, WorkSource
from app.services.cache_service import now_utc


async def record_work_sources(
    session: AsyncSession,
    work_id: str,
    source_ids: list[int],
) -> None:
    if not source_ids:
        return
    valid = set(
        (
            await session.execute(
                select(SyncSource.id).where(SyncSource.id.in_(source_ids))
            )
        ).scalars().all()
    )
    if not valid:
        return
    now = now_utc()
    for source_id in sorted(valid):
        relation = await session.get(
            WorkSource,
            {"work_id": work_id, "source_id": source_id},
        )
        if relation is None:
            session.add(
                WorkSource(
                    work_id=work_id,
                    source_id=source_id,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            relation.last_seen_at = now
    await session.commit()
