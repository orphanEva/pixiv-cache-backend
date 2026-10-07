from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.db.session import get_session
from app.models.work import SyncRestrict, SyncSource, SyncSourceType
from app.services.cache_service import now_utc
from app.services.sync_jobs import SyncJobQueue

router = APIRouter(prefix="/api/sync", tags=["sync"])


class SyncSourcePatch(BaseModel):
    enabled: bool | None = None
    include_illust: bool | None = None
    include_novel: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=300)


def _validate_remote_user_id(value: str) -> str:
    value = value.strip()
    if value == "self":
        return value
    if not value.isdigit() or int(value) <= 0:
        raise HTTPException(400, "user_id must be a positive Pixiv id or 'self'")
    return value


def _source_dict(source: SyncSource) -> dict:
    return {
        "id": source.id,
        "type": source.source_type.value,
        "remote_user_id": source.remote_user_id,
        "restrict": source.restrict_mode.value,
        "include_illust": source.include_illust,
        "include_novel": source.include_novel,
        "enabled": source.enabled,
        "interval_seconds": source.interval_seconds,
        "frontier": source.frontier_json or {},
        "next_run_at": source.next_run_at,
        "last_run_at": source.last_run_at,
        "last_success_at": source.last_success_at,
        "last_error": source.last_error,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


async def _upsert_source(
    session: AsyncSession,
    *,
    source_type: SyncSourceType,
    remote_user_id: str,
    restrict: SyncRestrict,
    include_illust: bool,
    include_novel: bool,
    interval_seconds: int,
    run_now: bool,
):
    existing = (
        await session.execute(
            select(SyncSource).where(
                SyncSource.source_type == source_type,
                SyncSource.remote_user_id == remote_user_id,
                SyncSource.restrict_mode == restrict,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = SyncSource(
            source_type=source_type,
            remote_user_id=remote_user_id,
            restrict_mode=restrict,
            include_illust=include_illust,
            include_novel=include_novel,
            enabled=True,
            interval_seconds=interval_seconds,
            next_run_at=now_utc() if run_now else now_utc() + timedelta(seconds=interval_seconds),
        )
        session.add(existing)
    else:
        existing.include_illust = include_illust
        existing.include_novel = include_novel
        existing.interval_seconds = interval_seconds
        existing.enabled = True
        if run_now:
            existing.next_run_at = now_utc()
    await session.commit()
    await session.refresh(existing)

    payload = _source_dict(existing)
    if run_now:
        payload["job"] = await SyncJobQueue(get_redis()).enqueue(existing.id)
    return payload


@router.post("/sources/author/{user_id}")
async def upsert_author_source(
    user_id: str,
    include_illust: bool = Query(True),
    include_novel: bool = Query(True),
    interval_seconds: int = Query(21600, ge=300),
    run_now: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    return await _upsert_source(
        session,
        source_type=SyncSourceType.AUTHOR,
        remote_user_id=_validate_remote_user_id(user_id),
        restrict=SyncRestrict.PUBLIC,
        include_illust=include_illust,
        include_novel=include_novel,
        interval_seconds=interval_seconds,
        run_now=run_now,
    )


@router.post("/sources/bookmarks/{user_id}")
async def upsert_bookmark_source(
    user_id: str,
    restrict: SyncRestrict = Query(SyncRestrict.PUBLIC),
    include_illust: bool = Query(True),
    include_novel: bool = Query(True),
    interval_seconds: int = Query(21600, ge=300),
    run_now: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    return await _upsert_source(
        session,
        source_type=SyncSourceType.BOOKMARKS,
        remote_user_id=_validate_remote_user_id(user_id),
        restrict=restrict,
        include_illust=include_illust,
        include_novel=include_novel,
        interval_seconds=interval_seconds,
        run_now=run_now,
    )


@router.get("/sources")
async def list_sync_sources(session: AsyncSession = Depends(get_session)):
    sources = (
        await session.execute(select(SyncSource).order_by(SyncSource.id.asc()))
    ).scalars().all()
    return {"sources": [_source_dict(source) for source in sources]}


@router.get("/sources/{source_id}")
async def get_sync_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(SyncSource, source_id)
    if source is None:
        raise HTTPException(404, "Sync source not found")
    return _source_dict(source)


@router.patch("/sources/{source_id}")
async def patch_sync_source(
    source_id: int,
    patch: SyncSourcePatch,
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(SyncSource, source_id)
    if source is None:
        raise HTTPException(404, "Sync source not found")
    data = patch.model_dump(exclude_none=True)
    for key, value in data.items():
        setattr(source, key, value)
    if source.enabled and source.next_run_at is None:
        source.next_run_at = now_utc()
    await session.commit()
    await session.refresh(source)
    return _source_dict(source)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sync_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        delete(SyncSource).where(SyncSource.id == source_id)
    )
    if not result.rowcount:
        raise HTTPException(404, "Sync source not found")
    await session.commit()


@router.post(
    "/sources/{source_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_sync_source(
    source_id: int,
    full: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(SyncSource, source_id)
    if source is None:
        raise HTTPException(404, "Sync source not found")
    return await SyncJobQueue(get_redis()).enqueue(source_id, full=full)


@router.get("/jobs/{job_id}")
async def get_sync_job(job_id: str):
    job = await SyncJobQueue(get_redis()).get(job_id)
    if job is None:
        raise HTTPException(404, "Sync job not found")
    return job


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_sync_job(job_id: str):
    job = await SyncJobQueue(get_redis()).retry(job_id)
    if job is None:
        raise HTTPException(404, "Sync job not found")
    return job
