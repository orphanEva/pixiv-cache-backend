from __future__ import annotations

from pathlib import Path
import pytest
from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.errors import PixivUnavailableError
from app.models.work import CurrentFile, PixivAsset, PixivVersion, PixivWork, WorkType
from app.schemas.pixiv import RemoteSnapshot
from app.services.cache_service import CacheService


class FakeLock:
    async def acquire(self): return True
    async def release(self): return None


class FakeRedis:
    def lock(self, *args, **kwargs): return FakeLock()


class FakeSession:
    def __init__(self):
        self.work = None
        self.next_version_id = 1

    def add(self, obj):
        if isinstance(obj, PixivWork):
            obj.versions = []
            obj.current_files = []
            self.work = obj
        elif isinstance(obj, PixivVersion):
            obj.id = self.next_version_id
            self.next_version_id += 1
            obj.assets = []
            self.work.versions.append(obj)
        elif isinstance(obj, PixivAsset):
            next(v for v in self.work.versions if v.id == obj.version_id).assets.append(obj)
        elif isinstance(obj, CurrentFile):
            self.work.current_files.append(obj)

    async def execute(self, statement):
        # _sync_tags and current_file updates are SQL operations; integration
        # tests exercise actual effects on MySQL. This fake tracks the pointer.
        self.work.current_files = []
        return None

    async def flush(self): pass
    async def commit(self): pass
    async def rollback(self): pass


class MemoryCacheService(CacheService):
    async def _load(self, pixiv_id, work_type):
        w = self.session.work
        if w and w.pixiv_id == pixiv_id and w.work_type == work_type:
            return w
        return None


class FakePixiv(PixivClient):
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    async def _next(self):
        self.calls += 1
        item = self.snapshots.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get_illust_snapshot(self, pixiv_id):
        return await self._next()

    async def get_novel_snapshot(self, pixiv_id):
        return await self._next()


def snapshot(token, body, title="demo"):
    return RemoteSnapshot(
        pixiv_id=123, work_type=WorkType.NOVEL, title=title,
        text_content=body, version_token=token, metadata={"title": title},
    )


@pytest.mark.asyncio
async def test_cache_create_validate_update_stale_and_immutable_history(tmp_path: Path):
    settings = get_settings()
    old_root, old_ttl = settings.storage_root, settings.remote_check_ttl_seconds
    settings.storage_root, settings.remote_check_ttl_seconds = tmp_path, 0
    try:
        client = FakePixiv([
            snapshot("a" * 64, "v1"), snapshot("a" * 64, "v1"),
            snapshot("b" * 64, "v2", "demo2"), PixivUnavailableError("temporary")
        ])
        session = FakeSession()
        service = MemoryCacheService(session, client, redis_client=FakeRedis())
        service.storage.root = tmp_path
        v1 = await service.get_novel(123)
        assert v1.version == 1 and v1.source == "remote-created"
        assert len(session.work.current_files) == 1
        assert session.work.current_files[0].local_path.endswith("/versions/1/novel.txt")

        same = await service.get_novel(123)
        assert same.version == 1 and same.source == "local-validated"
        v2 = await service.get_novel(123)
        assert v2.version == 2 and v2.title == "demo2"
        assert session.work.current_files[0].local_path.endswith("/versions/2/novel.txt")
        old = await service.get_version(123, WorkType.NOVEL, 1)
        assert old.title == "demo" and old.version == 1
        assert (tmp_path / "novel/123/versions/1/novel.txt").read_text() == "v1"
        assert (tmp_path / "novel/123/versions/2/novel.txt").read_text() == "v2"
        stale = await service.get_novel(123)
        assert stale.version == 2 and stale.source == "local-stale-remote-unavailable"
        assert client.calls == 4
        history = await service.get_history(123, WorkType.NOVEL)
        assert [v.version for v in history.versions] == [2, 1]
    finally:
        settings.storage_root, settings.remote_check_ttl_seconds = old_root, old_ttl
