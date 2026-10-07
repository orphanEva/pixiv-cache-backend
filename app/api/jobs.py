from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.core.redis_client import get_redis
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


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


@router.post(
    "/archive/{kind}/{pixiv_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_archive_job(
    kind: WorkType,
    pixiv_id: int,
    force_refresh: bool = Query(True),
):
    return await queue().enqueue(
        kind,
        pixiv_id,
        force_refresh=force_refresh,
    )


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
