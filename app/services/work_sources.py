from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.work import SyncSource, WorkSource, normalize_work_kind
from app.services.cache_service import now_utc


async def record_work_sources(
    session: AsyncSession,
    work_id: str,
    source_ids: list[int],
    *,
    work_kind: str,
) -> None:
    await upsert_work_sources(session, [work_id], source_ids, work_kind=work_kind)
    if source_ids:
        await session.commit()


async def upsert_work_sources(
    session: AsyncSession, work_ids, source_ids, *, work_kind: str,
) -> None:
    """Attach attribution atomically, leaving commit ownership to the caller."""
    work_kind = normalize_work_kind(work_kind)
    work_ids = sorted(set(str(value) for value in work_ids))
    if not source_ids:
        return
    if not work_ids:
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
    values = [{"work_kind": work_kind, "work_id": work_id, "source_id": source_id,
               "first_seen_at": now, "last_seen_at": now}
              for work_id in work_ids for source_id in sorted(valid)]
    binding = session.get_bind() if hasattr(session, "get_bind") else None
    dialect = binding.dialect.name if binding is not None else None
    # Bound batch size avoids huge SQL statements for large collections.
    if dialect in {"mysql", "mariadb", "sqlite"}:
        for start in range(0, len(values), 200):
            batch = values[start:start + 200]
            if dialect == "sqlite":
                statement = sqlite_insert(WorkSource).values(batch)
                statement = statement.on_conflict_do_update(
                    index_elements=["work_kind", "work_id", "source_id"],
                    set_={"last_seen_at": statement.excluded.last_seen_at},
                )
            else:
                statement = mysql_insert(WorkSource).values(batch)
                statement = statement.on_duplicate_key_update(last_seen_at=statement.inserted.last_seen_at)
            await session.execute(statement)
        return
    # Fallback for alternate test adapters; production uses MySQL's atomic upsert.
    rows = (await session.execute(select(WorkSource).where(
        WorkSource.work_kind == work_kind,
        WorkSource.work_id.in_(work_ids), WorkSource.source_id.in_(valid),
    ))).scalars().all()
    relations = {(row.work_kind, row.work_id, row.source_id): row for row in rows}
    for value in values:
        relation = relations.get((work_kind, value["work_id"], value["source_id"]))
        if relation is None:
            session.add(WorkSource(**value))
        else:
            relation.last_seen_at = now
