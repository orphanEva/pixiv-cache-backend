from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.core.redis_client import get_redis
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def queue() -> ArchiveJobQueue:
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


@router.get("/{job_id}")
async def get_archive_job(job_id: str):
    job = await queue().get(job_id)
    if job is None:
        raise HTTPException(404, "Archive job not found")
    return job


@router.post(
    "/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_archive_job(job_id: str):
    job = await queue().retry(job_id)
    if job is None:
        raise HTTPException(404, "Archive job not found")
    return job
