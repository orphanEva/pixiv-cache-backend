from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.work import PixivVersion, PixivWork, WorkType, work_identity


class WorkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, pixiv_id: int, work_type: WorkType) -> PixivWork | None:
        work_kind, work_id = work_identity(work_type, pixiv_id)
        stmt = (
            select(PixivWork)
            .where(PixivWork.work_kind == work_kind, PixivWork.id == work_id)
            .options(selectinload(PixivWork.versions).selectinload(PixivVersion.assets))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_version(self, pixiv_id: int, work_type: WorkType, version_no: int) -> PixivVersion | None:
        work_kind, work_id = work_identity(work_type, pixiv_id)
        stmt = (
            select(PixivVersion)
            .join(PixivWork, and_(
                PixivVersion.work_kind == PixivWork.work_kind,
                PixivVersion.work_id == PixivWork.id,
            ))
            .where(
                PixivWork.work_kind == work_kind,
                PixivWork.id == work_id,
                PixivVersion.version_no == version_no,
            )
            .options(selectinload(PixivVersion.assets), selectinload(PixivVersion.work))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def save(self, work: PixivWork) -> PixivWork:
        self.session.add(work)
        await self.session.flush()
        return work
