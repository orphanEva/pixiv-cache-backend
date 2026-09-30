"""Serve only DB-registered files for works still eligible for local access."""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import get_settings
from app.core.security import cache_read_allowed
from app.db.session import get_session
from app.models.work import PixivAsset, PixivVersion, PixivWork, WorkType

router = APIRouter(tags=["media"])


@router.get("/media/{kind}/{pixiv_id}/versions/{version_no}/{filename}")
async def get_media(kind: WorkType, pixiv_id: int, version_no: int, filename: str,
                    session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(PixivAsset.local_path, PixivWork.status)
        .join(PixivVersion, PixivAsset.version_id == PixivVersion.id)
        .join(PixivWork, PixivVersion.work_id == PixivWork.id)
        .where(PixivWork.work_type == kind, PixivWork.id == str(pixiv_id),
               PixivVersion.version_no == version_no))
    root = get_settings().storage_root.resolve()
    for local_path, status in result.all():
        expected = root / kind.value / str(pixiv_id) / "versions" / str(version_no) / filename
        resolved = Path(local_path).resolve()
        if resolved == expected.resolve() and resolved.is_file():
            if not cache_read_allowed(status.value):
                raise HTTPException(403, "Cached resource is not currently accessible")
            return FileResponse(resolved)
    raise HTTPException(404, "Media not found")
