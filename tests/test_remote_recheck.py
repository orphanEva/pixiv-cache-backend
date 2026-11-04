from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.maintenance import MaintenanceFreeze
from app.core.errors import ArchiveMaintenanceError
from app.models.work import PixivWork, WorkStatus, WorkType, work_identity
from app.services.cache_service import CacheService, now_utc
from app.services.remote_recheck import RemoteRecheckService
from test_work_identity import archive


@pytest.mark.asyncio
async def test_rechecks_both_namespaces_beyond_frontier_with_bounded_reservation(archive, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "remote_recheck_enabled", True)
    monkeypatch.setattr(settings, "remote_recheck_batch_size", 1)
    async with archive.sessions() as session:
        cache = CacheService(session, archive.client, redis_client=archive.redis)
        await cache.get_novel(700)
        await cache.get_illust(700)
        await cache.get_novel(701)
        await cache.get_novel(702)
        old = now_utc() - timedelta(days=8)
        for kind in ("illust", "novel"):
            (await session.get(PixivWork, work_identity(kind, 700))).last_checked_at = old
        revoked = await session.get(PixivWork, work_identity("novel", 702))
        revoked.last_checked_at, revoked.status = old, WorkStatus.RESTRICTED
        await session.commit()
        service = RemoteRecheckService(session, archive.redis)
        assert (await service.status())["due_works"] == 2
        first = await service.schedule()
        second = await service.schedule()
        assert first["queued"] == second["queued"] == 1
        assert (await service.schedule())["queued"] == 0
        jobs = [await service.queue.get(run["job_ids"][0]) for run in (first, second)]
        assert {job["kind"] for job in jobs} == {"novel", "illust"}
        assert all(job["force_refresh"] and not job["bookmark_if_missing"] for job in jobs)
        assert (await service.status())["due_works"] == 0


@pytest.mark.asyncio
async def test_recheck_failed_commit_can_republish_without_losing_work(archive, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "remote_recheck_enabled", True)
    async with archive.sessions() as session:
        cache = CacheService(session, archive.client, redis_client=archive.redis)
        await cache.get_novel(800)
        work = await session.get(PixivWork, work_identity("novel", 800))
        work.last_checked_at = now_utc() - timedelta(days=8)
        await session.commit()
        service = RemoteRecheckService(session, archive.redis)
        original = session.commit

        async def failed_commit():
            raise ConnectionError("commit failed")

        monkeypatch.setattr(session, "commit", failed_commit)
        with pytest.raises(ConnectionError):
            await service.schedule()
        monkeypatch.setattr(session, "commit", original)
        assert (await session.get(PixivWork, work_identity("novel", 800))).next_recheck_at is None
        assert (await service.schedule())["queued"] == 1
        assert await archive.redis.xlen(service.queue.stream) == 1


@pytest.mark.asyncio
async def test_rechecks_respect_disable_and_maintenance(archive, monkeypatch):
    async with archive.sessions() as session:
        service = RemoteRecheckService(session, archive.redis)
        monkeypatch.setattr(get_settings(), "remote_recheck_enabled", False)
        assert (await service.schedule())["status"] == "disabled"
        monkeypatch.setattr(get_settings(), "remote_recheck_enabled", True)
        async with MaintenanceFreeze(archive.redis):
            with pytest.raises(ArchiveMaintenanceError):
                await service.schedule()
