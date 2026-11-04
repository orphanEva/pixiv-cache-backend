from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.core.config import get_settings
from app.db.session import get_session
from app.models.work import SyncRestrict, SyncSource, SyncSourceType
from app.services.cache_service import now_utc
from app.services.sync_jobs import SyncJobQueue

router = APIRouter(prefix="/api/sync", tags=["sync"])


class SyncSourcePatch(BaseModel):
    enabled: bool | None = None
    include_illust: bool | None = None
    include_novel: bool | None = None
    expand_series: bool | None = None
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
        "account_id": source.account_id,
        "restrict": source.restrict_mode.value,
        "include_illust": source.include_illust,
        "include_novel": source.include_novel,
        "expand_series": source.expand_series,
        "enabled": source.enabled,
        "interval_seconds": source.interval_seconds,
        "frontier": source.frontier_json or {},
        "scan_state": source.scan_state_json,
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
    expand_series: bool = False,
):
    if not include_illust and not include_novel:
        raise HTTPException(400, "At least one work type must be included")
    existing = (
        await session.execute(
            select(SyncSource).where(
                SyncSource.source_type == source_type,
                SyncSource.remote_user_id == remote_user_id,
                SyncSource.restrict_mode == restrict,
            )
        )
    ).scalar_one_or_none()
    rescan_required = False
    if existing is None:
        existing = SyncSource(
            source_type=source_type,
            remote_user_id=remote_user_id,
            restrict_mode=restrict,
            include_illust=include_illust,
            include_novel=include_novel,
            expand_series=expand_series,
            enabled=True,
            interval_seconds=interval_seconds,
            next_run_at=now_utc() if run_now else now_utc() + timedelta(seconds=interval_seconds),
        )
        session.add(existing)
    else:
        # A changed content selection requires a new scan rather than resuming a
        # cursor/frontier captured under different subscription options.
        rescan_required = (not existing.enabled or existing.include_illust != include_illust
                           or existing.include_novel != include_novel
                           or existing.expand_series != expand_series)
        if rescan_required:
            existing.scan_state_json = None
            existing.frontier_json = None
        existing.include_illust = include_illust
        existing.include_novel = include_novel
        existing.expand_series = expand_series
        existing.interval_seconds = interval_seconds
        existing.enabled = True
        if run_now:
            existing.next_run_at = now_utc()
    await session.commit()
    await session.refresh(existing)

    payload = _source_dict(existing)
    if existing.enabled and (run_now or rescan_required):
        payload["job"] = await SyncJobQueue(get_redis()).enqueue(existing.id, full=rescan_required)
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
    expand_series: bool = Query(False),
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
        expand_series=expand_series,
    )


@router.post("/sources/series/{series_type}/{series_id}")
async def upsert_series_source(
    series_type: Literal["novel", "manga"],
    series_id: int,
    interval_seconds: int = Query(21600, ge=300),
    run_now: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    if series_id <= 0:
        raise HTTPException(400, "series_id must be positive")
    return await _upsert_source(
        session,
        source_type=SyncSourceType.NOVEL_SERIES if series_type == "novel" else SyncSourceType.MANGA_SERIES,
        remote_user_id=str(series_id), restrict=SyncRestrict.PUBLIC,
        include_illust=series_type == "manga", include_novel=series_type == "novel",
        interval_seconds=interval_seconds, run_now=run_now,
    )


@router.post("/sources/followed-series")
async def upsert_followed_series_source(
    include_novel: bool = Query(True),
    include_manga: bool = Query(True),
    interval_seconds: int = Query(21600, ge=300),
    run_now: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    """Import the PIXIV_COOKIE account's watched series and keep discovering new ones."""
    return await _upsert_source(
        session, source_type=SyncSourceType.FOLLOWED_SERIES, remote_user_id="self",
        restrict=SyncRestrict.PUBLIC, include_illust=include_manga, include_novel=include_novel,
        interval_seconds=interval_seconds, run_now=run_now,
    )


@router.post("/sources/watchlist/novel")
async def upsert_novel_watchlist_source(
    interval_seconds: int = Query(21600, ge=300),
    run_now: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    """订阅当前 Pixiv Web Cookie 账号的小说追更列表，自动补齐全集。"""
    if not get_settings().pixiv_cookie.strip():
        raise HTTPException(422, "Configure PIXIV_COOKIE before syncing the novel watchlist")
    return await _upsert_source(
        session,
        source_type=SyncSourceType.WATCHLIST,
        remote_user_id="novel",
        restrict=SyncRestrict.PUBLIC,
        include_illust=False,
        include_novel=True,
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
    if data.get("expand_series") and source.source_type != SyncSourceType.BOOKMARKS:
        raise HTTPException(400, "expand_series is only supported for bookmark sources")
    if source.source_type == SyncSourceType.WATCHLIST and (
        data.get("include_illust", source.include_illust) or not data.get("include_novel", source.include_novel)
    ):
        raise HTTPException(400, "Novel watchlist sources must include novels only")
    if not data.get("include_illust", source.include_illust) and not data.get("include_novel", source.include_novel):
        raise HTTPException(400, "At least one work type must be included")
    rescan_required = (not source.enabled and data.get("enabled") is True) or any(
        key in data and data[key] != getattr(source, key)
        for key in ("include_illust", "include_novel", "expand_series"))
    if rescan_required:
        source.scan_state_json = None
        source.frontier_json = None
    for key, value in data.items():
        setattr(source, key, value)
    if source.enabled and source.next_run_at is None:
        source.next_run_at = now_utc()
    await session.commit()
    await session.refresh(source)
    payload = _source_dict(source)
    if source.enabled and rescan_required:
        payload["job"] = await SyncJobQueue(get_redis()).enqueue(source.id, full=True)
    return payload


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
    if not source.enabled:
        raise HTTPException(409, "Enable this sync source before running it")
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
