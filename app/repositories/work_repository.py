from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.work import PixivVersion, PixivWork, WorkType


class WorkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, pixiv_id: int, work_type: WorkType) -> PixivWork | None:
        stmt = (
            select(PixivWork)
            .where(PixivWork.id == str(pixiv_id), PixivWork.work_type == work_type)
            .options(selectinload(PixivWork.versions).selectinload(PixivVersion.assets))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_version(self, pixiv_id: int, work_type: WorkType, version_no: int) -> PixivVersion | None:
        stmt = (
            select(PixivVersion)
            .join(PixivWork, PixivVersion.work_id == PixivWork.id)
            .where(
                PixivWork.id == str(pixiv_id),
                PixivWork.work_type == work_type,
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
