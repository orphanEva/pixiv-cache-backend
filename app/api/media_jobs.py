"""Queue MP4 reconstruction from local archives without remote refresh."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.jobs import ArchiveJobStatus
from app.core.redis_client import get_redis
from app.db.session import get_session
from app.services.media_jobs import MediaJobQueue
from app.services.ugoira_preview import UgoiraPreviewService

router = APIRouter(prefix="/api/media-jobs", tags=["media-jobs"])


def queue():
    return MediaJobQueue(get_redis())


@router.post("/ugoira/{pixiv_id}", status_code=202)
async def enqueue_ugoira_preview(
    pixiv_id: int, version_no: int | None = Query(None, ge=1),
    session: AsyncSession = Depends(get_session),
):
    try:
        _, version, _ = await UgoiraPreviewService(session, get_redis()).resolve_version(pixiv_id, version_no)
        return await queue().enqueue_preview(pixiv_id, version.version_no)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def list_media_jobs(
    status_filter: ArchiveJobStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
):
    return await queue().list_jobs(status=status_filter, page=page, page_size=page_size)


@router.get("/stats")
async def media_job_stats():
    return await queue().queue_stats()


@router.get("/{job_id}")
async def get_media_job(job_id: str):
    job = await queue().get(job_id)
    if job is None:
        raise HTTPException(404, "Media job not found")
    return job


@router.post("/{job_id}/retry", status_code=202)
async def retry_media_job(job_id: str):
    job = await queue().retry(job_id)
    if job is None:
        raise HTTPException(404, "Media job not found")
    return job
