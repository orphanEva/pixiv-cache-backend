from __future__ import annotations

from typing import Literal
import logging

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, HTTPException, Query, status

from app.core.redis_client import get_redis
from app.db.session import get_session
from app.api.routes import get_client
from app.clients.pixivpy_client import PixivPyClient
from app.services.series_archive import SeriesArchiveService
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)


ArchiveJobStatus = Literal[
    "queued",
    "running",
    "finalizing",
    "retry_wait",
    "succeeded",
    "failed",
    "dead",
]


def queue() -> ArchiveJobQueue:
    """返回绑定全局 Redis 客户端的归档任务队列。"""
    return ArchiveJobQueue(get_redis())


@router.post("/archive/series/{series_type}/{series_id}", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_series_archive(
    series_type: Literal["novel", "manga"],
    series_id: int,
    force_refresh: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    client: PixivPyClient = Depends(get_client),
):
    """手工下载整套小说或漫画系列，且将系列与成员写入 MySQL。"""
    try:
        return await SeriesArchiveService(session, client, queue()).enqueue(
            series_type, series_id, force_refresh=force_refresh
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/archive/{kind}/{pixiv_id}", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_archive_job(
    kind: WorkType,
    pixiv_id: int,
    force_refresh: bool = Query(True),
    expand_series: bool = Query(True, description="Auto-discover and archive the full novel/manga series."),
    session: AsyncSession = Depends(get_session),
    client: PixivPyClient = Depends(get_client),
):
    """自动识别系列；若无系列或识别失败则只归档当前作品。"""
    if pixiv_id <= 0:
        raise HTTPException(422, "pixiv_id must be positive")
    if expand_series:
        try:
            detected = await client.detect_series(kind, pixiv_id)
        except Exception as exc:
            logger.warning("series_detection_failed", extra={
                "pixiv_id": pixiv_id, "work_type": kind.value,
                "error_type": exc.__class__.__name__,
            })
            detected = None
        if detected:
            series_type, series_id = detected
            try:
                return await SeriesArchiveService(session, client, queue()).enqueue(
                    series_type, series_id, force_refresh=force_refresh
                )
            except ValueError as exc:
                # 明确的结构/完整性错误不允许误报为“全集已下载”。
                raise HTTPException(422, str(exc)) from exc
    return await queue().enqueue(kind, pixiv_id, force_refresh=force_refresh)


@router.get("/series/{series_type}/{series_id}")
async def get_series_archive_status(
    series_type: Literal["novel", "manga"],
    series_id: int,
    session: AsyncSession = Depends(get_session),
    client: PixivPyClient = Depends(get_client),
):
    try:
        return await SeriesArchiveService(session, client, queue()).status(series_type, series_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("")
async def list_archive_jobs(
    status_filter: ArchiveJobStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """分页查询归档任务；可直接用 status=dead 查死信任务。"""
    return await queue().list_jobs(
        status=status_filter,
        page=page,
        page_size=page_size,
    )


@router.get("/{job_id}")
async def get_archive_job(job_id: str):
    """查询一个归档任务的当前状态。"""
    job = await queue().get(job_id)
    if job is None:
        raise HTTPException(404, "Archive job not found")
    return job


@router.post(
    "/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_archive_job(job_id: str):
    """兼容旧接口：重新投递一个终态任务。"""
    job = await queue().retry(job_id)
    if job is None:
        raise HTTPException(404, "Archive job not found")
    return job



@router.post(
    "/{job_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
)
async def replay_archive_job(job_id: str):
    """重新投递一个终态归档任务，并保留原任务来源。"""
    job = await queue().replay(job_id)
    if job is None:
        raise HTTPException(404, "Archive job not found")
    return job
