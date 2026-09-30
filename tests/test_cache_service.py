from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.errors import PixivUnavailableError
from app.models.work import PixivAsset, PixivVersion, PixivWork, WorkType
from app.schemas.pixiv import RemoteSnapshot
from app.services.cache_service import CacheService


class FakeLock:
    async def acquire(self):
        return True

    async def release(self):
        return None


class FakeRedis:
    def lock(self, *args, **kwargs):
        return FakeLock()


class FakeSession:
    def __init__(self):
        self.work: PixivWork | None = None
        self._next_version_id = 1

    def add(self, obj):
        if isinstance(obj, PixivWork):
            obj.id = 1
            obj.versions = []
            self.work = obj
        elif isinstance(obj, PixivVersion):
            obj.id = self._next_version_id
            self._next_version_id += 1
            obj.assets = []
            self.work.versions.append(obj)
        elif isinstance(obj, PixivAsset):
            version = next(v for v in self.work.versions if v.id == obj.version_id)
            version.assets.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class MemoryCacheService(CacheService):
    async def _load(self, pixiv_id: int, work_type: WorkType):
        work = self.session.work
        if work and work.pixiv_id == pixiv_id and work.work_type == work_type:
            return work
        return None


class FakePixivClient(PixivClient):
    def __init__(self, snapshots: list[RemoteSnapshot | Exception]):
        self.snapshots = list(snapshots)
        self.calls = 0

    async def _next(self):
        self.calls += 1
        value = self.snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def get_illust_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        return await self._next()

    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        return await self._next()


def novel_snapshot(token: str, text: str, title: str = "demo") -> RemoteSnapshot:
    return RemoteSnapshot(
        pixiv_id=123,
        work_type=WorkType.NOVEL,
        title=title,
        text_content=text,
        version_token=token,
        metadata={"title": title},
    )


def make_service(client, tmp_path: Path) -> MemoryCacheService:
    settings = get_settings()
    settings.storage_root = tmp_path
    settings.remote_check_ttl_seconds = 0
    settings.serve_stale_on_remote_unavailable = True
    session = FakeSession()
    service = MemoryCacheService(session, client, redis_client=FakeRedis())
    service.storage.root = tmp_path
    service.storage.settings.storage_root = tmp_path
    return service


@pytest.mark.asyncio
async def test_cache_create_validate_update_and_stale(tmp_path: Path):
    client = FakePixivClient([
        novel_snapshot("a" * 64, "v1"),
        novel_snapshot("a" * 64, "v1"),
        novel_snapshot("b" * 64, "v2", "demo2"),
        PixivUnavailableError("temporary"),
    ])
    service = make_service(client, tmp_path)

    created = await service.get_novel(123, refresh=True)
    assert created.source == "remote-created"
    assert created.version == 1
    assert Path(tmp_path / "novel/123/versions/1/novel.txt").read_text() == "v1"

    unchanged = await service.get_novel(123, refresh=True)
    assert unchanged.source == "local-validated"
    assert unchanged.version == 1

    updated = await service.get_novel(123, refresh=True)
    assert updated.source == "remote-updated"
    assert updated.version == 2
    assert updated.title == "demo2"
    assert Path(tmp_path / "novel/123/versions/1/novel.txt").read_text() == "v1"
    assert Path(tmp_path / "novel/123/versions/2/novel.txt").read_text() == "v2"

    stale = await service.get_novel(123, refresh=True)
    assert stale.source == "local-stale-remote-unavailable"
    assert stale.version == 2
    assert client.calls == 4

    history = await service.get_history(123, WorkType.NOVEL)
    assert [v.version for v in history.versions] == [2, 1]
