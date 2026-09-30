"""Runs in the GitHub Actions MySQL+Redis integration job, never against production."""
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.redis_client import close_redis, get_redis
from app.db.session import SessionLocal
from app.models.work import PixivWork, WorkType
from app.schemas.pixiv import RemoteSnapshot
from app.services.cache_service import CacheService


class FakePixiv(PixivClient):
    def __init__(self):
        self.version = 1
        self.calls = 0

    async def get_novel_snapshot(self, pixiv_id):
        self.calls += 1
        return RemoteSnapshot(
            pixiv_id=pixiv_id, work_type=WorkType.NOVEL,
            title=f"title-v{self.version}", text_content=f"novel-v{self.version}",
            version_token=f"{self.version:064x}", metadata={"title": f"title-v{self.version}"},
        )

    async def get_illust_snapshot(self, pixiv_id):
        raise AssertionError("unexpected illust call")


@pytest.mark.asyncio
async def test_mysql_redis_cache_lifecycle(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    old_ttl = settings.remote_check_ttl_seconds
    settings.storage_root = tmp_path
    settings.remote_check_ttl_seconds = 0
    pixiv = FakePixiv()
    work_id = 987651234
    try:
        async with SessionLocal() as session:
            await session.execute(delete(PixivWork).where(PixivWork.pixiv_id == work_id))
            await session.commit()
            service = CacheService(session, pixiv, redis_client=get_redis())
            v1 = await service.get_novel(work_id)
            assert v1.version == 1 and v1.source == "remote-created"
            assert v1.assets[0].local_path.endswith("/novel.txt")
            assert (tmp_path / f"novel/{work_id}/versions/1/novel.txt").read_text() == "novel-v1"
            local = await service.get_novel(work_id, refresh=False)
            assert local.version == 1 and pixiv.calls == 1
            unchanged = await service.get_novel(work_id)
            assert unchanged.version == 1 and unchanged.source == "local-validated"
            pixiv.version = 2
            v2 = await service.get_novel(work_id)
            assert v2.version == 2 and v2.title == "title-v2"
            history = await service.get_history(work_id, WorkType.NOVEL)
            assert [v.version for v in history.versions] == [2, 1]
            assert (tmp_path / f"novel/{work_id}/versions/1/novel.txt").read_text() == "novel-v1"
    finally:
        settings.storage_root = old_root
        settings.remote_check_ttl_seconds = old_ttl
        await close_redis()
