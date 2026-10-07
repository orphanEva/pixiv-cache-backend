from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.core.redis_client import get_redis
from app.services.archive_jobs import ArchiveJobQueue
from app.services.ops_service import OpsService

router = APIRouter(prefix="/api/ops", tags=["ops"])

ArchiveJobStatus = Literal[
    "queued",
    "running",
    "finalizing",
    "retry_wait",
    "succeeded",
    "failed",
    "dead",
]


def archive_queue() -> ArchiveJobQueue:
    """返回绑定全局 Redis 客户端的归档任务队列。"""
    return ArchiveJobQueue(get_redis())


@router.get("/status")
async def ops_status():
    """查询服务、worker、队列、schema 和磁盘的综合运行状态。"""
    return await OpsService().status()


@router.get("/jobs")
async def list_ops_jobs(
    status_filter: ArchiveJobStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """分页查询归档任务，常用于筛选 retry_wait 或 dead 任务。"""
    return await archive_queue().list_jobs(
        status=status_filter,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/jobs/{job_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
)
async def replay_ops_job(job_id: str):
    """重新投递一个终态归档任务，并保留原来的同步来源。"""
    job = await archive_queue().replay(job_id)
    if job is None:
        raise HTTPException(404, "Archive job not found")
    return job
