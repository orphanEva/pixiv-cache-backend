"""Integration tests run ONLY against disposable CI MySQL+Redis after manual DDL."""
from pathlib import Path
import pytest
from sqlalchemy import delete, func, select

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.redis_client import close_redis, get_redis
from app.db.session import SessionLocal
from app.models.work import CurrentFile, PixivVersion, PixivWork, Series, Tag, WorkTagRelation, WorkType
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
            version_token=f"{self.version:064x}",
            metadata={"title": f"title-v{self.version}"},
        )

    async def get_illust_snapshot(self, pixiv_id):
        raise AssertionError("unexpected illust call")


@pytest.mark.asyncio(loop_scope="module")
async def test_mysql_redis_cache_lifecycle(tmp_path: Path):
    settings = get_settings()
    old_root, old_ttl = settings.storage_root, settings.remote_check_ttl_seconds
    settings.storage_root, settings.remote_check_ttl_seconds = tmp_path, 0
    pixiv = FakePixiv()
    work_id = 987651234
    try:
        async with SessionLocal() as session:
            await session.execute(delete(PixivWork).where(PixivWork.id == str(work_id)))
            await session.commit()
            service = CacheService(session, pixiv, redis_client=get_redis())
            v1 = await service.get_novel(work_id)
            assert v1.version == 1 and v1.source == "remote-created"
            assert v1.assets[0].local_path.endswith("/novel.txt")
            local = await service.get_novel(work_id, refresh=False)
            assert local.version == 1 and pixiv.calls == 1
            unchanged = await service.get_novel(work_id)
            assert unchanged.version == 1 and unchanged.source == "local-validated"
            pixiv.version = 2
            v2 = await service.get_novel(work_id)
            assert v2.version == 2 and v2.title == "title-v2"
            history = await service.get_history(work_id, WorkType.NOVEL)
            assert [v.version for v in history.versions] == [2, 1]
            old = await service.get_version(work_id, WorkType.NOVEL, 1)
            assert old.title == "title-v1"
            assert (tmp_path / f"novel/{work_id}/versions/1/novel.txt").read_text() == "novel-v1"
            assert (tmp_path / f"novel/{work_id}/versions/2/novel.txt").read_text() == "novel-v2"
            files = (await session.execute(select(CurrentFile).where(CurrentFile.work_id == str(work_id)))).scalars().all()
            assert len(files) == 1 and files[0].local_path.endswith("/versions/2/novel.txt")
            versions = (await session.execute(select(func.count(PixivVersion.id)).where(PixivVersion.work_id == str(work_id)))).scalar_one()
            assert versions == 2
    finally:
        settings.storage_root, settings.remote_check_ttl_seconds = old_root, old_ttl
        await close_redis()


@pytest.mark.asyncio(loop_scope="module")
async def test_series_and_tag_relationships_follow_updates(tmp_path: Path):
    """A real MySQL FK check: series must exist before a new work references it."""
    settings = get_settings()
    old_root, old_ttl = settings.storage_root, settings.remote_check_ttl_seconds
    settings.storage_root, settings.remote_check_ttl_seconds = tmp_path, 0
    pixiv_id = 987651235

    class SeriesPixiv(FakePixiv):
        async def get_novel_snapshot(self, pixiv_id):
            self.calls += 1
            tag = "初音ミク" if self.version == 1 else "原神"
            return RemoteSnapshot(
                pixiv_id=pixiv_id, work_type=WorkType.NOVEL,
                title="series novel", author_id=88, author_name="author",
                series_id="series-test-135", series_order=1,
                tags=[{"name": tag}, {"name": tag}],
                text_content=f"chapter-{self.version}",
                version_token=f"{self.version:064x}",
                metadata={"series": {"id": "series-test-135", "title": "My series"}},
            )

    client = SeriesPixiv()
    try:
        async with SessionLocal() as session:
            await session.execute(delete(PixivWork).where(PixivWork.id == str(pixiv_id)))
            await session.execute(delete(Series).where(Series.id == "series-test-135"))
            await session.commit()
            service = CacheService(session, client, redis_client=get_redis())
            await service.get_novel(pixiv_id)
            assert await session.get(Series, "series-test-135") is not None
            assert (await session.execute(
                select(func.count()).select_from(WorkTagRelation)
                .where(WorkTagRelation.work_id == str(pixiv_id))
            )).scalar_one() == 1

            client.version = 2
            await service.get_novel(pixiv_id)
            names = (await session.execute(
                select(Tag.name).join(WorkTagRelation, WorkTagRelation.tag_id == Tag.id)
                .where(WorkTagRelation.work_id == str(pixiv_id))
            )).scalars().all()
            assert names == ["原神"]
            history = await service.get_history(pixiv_id, WorkType.NOVEL)
            assert [v.version for v in history.versions] == [2, 1]
    finally:
        settings.storage_root, settings.remote_check_ttl_seconds = old_root, old_ttl
        await close_redis()
