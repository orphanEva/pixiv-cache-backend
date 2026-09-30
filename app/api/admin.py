"""Private operational endpoints. Global API-key middleware also protects them."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.models.work import PixivWork, PixivVersion, WorkType
from app.api.routes import get_client
from app.services.cache_service import CacheService
from app.core.errors import PixivRemoteError

router = APIRouter(prefix="/api/admin/cache", tags=["admin"])


@router.get("/status")
async def cache_status(session: AsyncSession = Depends(get_session)):
    statuses = (await session.execute(
        select(PixivWork.status, func.count(PixivWork.id)).group_by(PixivWork.status)
    )).all()
    versions = (await session.execute(select(func.count(PixivVersion.id)))).scalar_one()
    return {"works_by_status": {status.value: count for status, count in statuses},
            "total_versions": versions}


@router.get("/{kind}/{pixiv_id}")
async def cache_detail(kind: WorkType, pixiv_id: int, session: AsyncSession = Depends(get_session)):
    work = (await session.execute(
        select(PixivWork).where(PixivWork.work_type == kind, PixivWork.pixiv_id == pixiv_id)
    )).scalar_one_or_none()
    if work is None:
        raise HTTPException(404, "Cache entry not found")
    return {"pixiv_id": work.pixiv_id, "type": work.work_type.value, "status": work.status.value,
            "current_version": work.current_version_no, "last_checked_at": work.last_checked_at,
            "cached_at": work.cached_at}


@router.post("/{kind}/{pixiv_id}/refresh")
async def force_refresh(kind: WorkType, pixiv_id: int, session: AsyncSession = Depends(get_session)):
    service = CacheService(session, get_client())
    try:
        return await service._get(pixiv_id, kind, refresh=True)
    except PixivRemoteError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
