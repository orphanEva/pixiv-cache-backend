from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from sqlalchemy import BigInteger, Integer, Text, select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.errors import PixivAuthError
from app.models.work import (
    AccountMigrationState, Base, CurrentFile, PixivAsset, PixivVersion, PixivWork, Series, SeriesMember,
    SyncRestrict, SyncSource, SyncSourceType, Tag, WorkSource, WorkTagRelation, WorkType,
)
from app.schemas.pixiv import DiscoveryPage, DiscoveredWork, RemoteSnapshot
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import CacheService, now_utc
from app.services.source_sync import SourceSyncService


@pytest.mark.asyncio
@pytest.mark.parametrize("full,frontier", [(False, None), (True, {"novel": ["100"]})])
async def test_reopened_scan_checks_changed_unchanged_and_missing_archives(tmp_path, monkeypatch, full, frontier):
    """The MySQL-style checkpoint and Redis work queue recover independently."""
    settings = get_settings()
    for name, value in {"sync_max_pages_per_run": 1, "sync_page_delay_seconds": 0,
                        "sync_work_recheck_ttl_seconds": 86400, "remote_check_ttl_seconds": 86400,
                        "storage_root": tmp_path, "storage_min_free_bytes": 0}.items():
        monkeypatch.setattr(settings, name, value)
    tables = [SyncSource.__table__, Series.__table__, SeriesMember.__table__, PixivWork.__table__,
              PixivVersion.__table__, PixivAsset.__table__, CurrentFile.__table__, Tag.__table__,
              WorkTagRelation.__table__, WorkSource.__table__]
    # Test-only SQLite equivalents for MySQL's MEDIUMTEXT and auto-increment BIGINT.
    for table in tables:
        for column in table.columns:
            if column.primary_key and isinstance(column.type, BigInteger):
                monkeypatch.setattr(column, "type", column.type.with_variant(Integer(), "sqlite"))
            elif isinstance(column.type, MEDIUMTEXT):
                monkeypatch.setattr(column, "type", column.type.with_variant(Text(), "sqlite"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))

    class Client:
        def __init__(self):
            self.changed = False
            self.pages = []
            self.checks = Counter()
        async def discover_author_novels(self, user_id, params):
            self.pages.append(params)
            ids = [100] if params is None else [99, 98]
            return DiscoveryPage(items=[DiscoveredWork(pixiv_id=i, work_type=WorkType.NOVEL) for i in ids],
                                 next_params={"offset": 1} if params is None else None)
        async def get_novel_snapshot(self, pixiv_id):
            self.checks[pixiv_id] += 1
            version = 2 if pixiv_id == 100 and self.changed else 1
            body = f"work-{pixiv_id}-v{version}"
            return RemoteSnapshot(pixiv_id=pixiv_id, work_type=WorkType.NOVEL,
                                  title=f"work-{pixiv_id}", text_content=body,
                                  version_token=f"{pixiv_id * 10 + version:064x}", metadata={})

    client = Client()
    try:
        # Both existing works were just checked, so TTL would incorrectly hide
        # the remote edit if the resumed first/full scan failed to force refresh.
        for work_id in (100, 99):
            async with sessions() as session:
                await CacheService(session, client, redis)._get(work_id, WorkType.NOVEL, True, bypass_ttl=True)
        client.changed = True
        client.checks.clear()
        async with sessions() as session:
            source = SyncSource(id=1, source_type=SyncSourceType.AUTHOR, remote_user_id="42",
                                restrict_mode=SyncRestrict.PUBLIC, include_illust=False,
                                include_novel=True, expand_series=False, enabled=True,
                                interval_seconds=3600, frontier_json=frontier)
            session.add(source)
            await session.commit()
            result = await SourceSyncService(session, client, redis).run_source(source, full=full)
            assert result["complete"] is False
            assert source.scan_state_json["lanes"]["novel"]["strict_remote_check"] is True
            assert source.scan_state_json["lanes"]["novel"]["next_params"] == {"offset": 1}
        # Simulate a process restart: re-open source session and service, while
        # previously submitted archive work remains in the durable Redis stream.
        async with sessions() as session:
            source = await session.get(SyncSource, 1)
            result = await SourceSyncService(session, client, redis).run_source(source)
            assert result["complete"] is True
            assert result["strict_remote_check"] is True
            assert source.scan_state_json is None
        assert client.pages == [None, {"offset": 1}]
        queue = ArchiveJobQueue(redis)
        messages = await redis.xrange(queue.stream)
        assert len(messages) == 3
        for _, message in messages:
            job = await queue.get(message["job_id"])
            assert job["force_refresh"] is True
            async with sessions() as session:
                await CacheService(session, client, redis)._get(
                    job["pixiv_id"], WorkType(job["kind"]), True, bypass_ttl=job["force_refresh"])
        async with sessions() as session:
            works = (await session.execute(select(PixivWork))).scalars().all()
            assert {work.id: work.current_version_no for work in works} == {"100": 2, "99": 1, "98": 1}
            versions = (await session.execute(select(PixivVersion))).scalars().all()
            assert Counter(version.work_id for version in versions) == {"100": 2, "99": 1, "98": 1}
        assert client.checks == {100: 1, 99: 1, 98: 1}
        assert (tmp_path / "novel/100/versions/1/novel.txt").read_text() == "work-100-v1"
        assert (tmp_path / "novel/100/versions/2/novel.txt").read_text() == "work-100-v2"
    finally:
        await redis.aclose()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_account", [None, "10", "20"])
async def test_self_bookmarks_bind_account_before_reusing_checkpoint(monkeypatch, previous_account):
    monkeypatch.setattr(get_settings(), "sync_page_delay_seconds", 0)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SyncSource.__table__.create)
        await connection.run_sync(AccountMigrationState.__table__.create)
    pages = []
    strict_modes = []
    class Client:
        async def get_authenticated_user_id(self):
            return 20
        async def discover_bookmark_novels(self, user_id, restrict, params):
            assert user_id == 20
            pages.append(params)
            return DiscoveryPage(items=[])
    async def queue(items, source_id, *, strict_remote_check=False):
        strict_modes.append(strict_remote_check)
        return 0, 0
    try:
        async with sessions() as session:
            source = SyncSource(id=1, source_type=SyncSourceType.BOOKMARKS, remote_user_id="self",
                account_id=previous_account, restrict_mode=SyncRestrict.PUBLIC, include_illust=False,
                include_novel=True, expand_series=False, enabled=True, interval_seconds=3600,
                frontier_json={"novel": ["100"]},
                scan_state_json={"full": False, "lanes": {"novel": {
                    "started": True, "frontier": ["200"], "next_params": {"offset": 50},
                    "complete": False, "strict_remote_check": False}}})
            session.add(source)
            await session.commit()
        async with sessions() as session:
            source = await session.get(SyncSource, 1)
            service = SourceSyncService(session, Client(), None)
            service._queue_works = queue
            await service.run_source(source)
        async with sessions() as session:
            assert (await session.get(SyncSource, 1)).account_id == "20"
        assert pages == ([{"offset": 50}] if previous_account == "20" else [None])
        assert strict_modes == [previous_account != "20"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_empty_frontier_is_incremental_not_another_first_scan(monkeypatch):
    source = SimpleNamespace(id=1, source_type=SyncSourceType.AUTHOR, remote_user_id="42",
        account_id=None, include_illust=False, include_novel=True, expand_series=False,
        interval_seconds=3600, frontier_json={"novel": []}, scan_state_json=None)
    class Session:
        async def commit(self):
            pass
    class Client:
        async def discover_author_novels(self, user_id, params):
            return DiscoveryPage(items=[])
    service = SourceSyncService(Session(), Client(), None)
    modes = []
    async def queue(items, source_id, *, strict_remote_check=False):
        modes.append(strict_remote_check)
        return 0, 0
    service._queue_works = queue
    result = await service.run_source(source)
    assert result["complete"] is True
    assert modes == [False]


@pytest.mark.asyncio
async def test_followed_source_binds_web_identity_and_resets_old_account_progress(monkeypatch):
    monkeypatch.setattr(get_settings(), "account_migration_enabled", False)
    source = SimpleNamespace(id=1, source_type=SyncSourceType.FOLLOWED_SERIES,
        remote_user_id="self", account_id="10", frontier_json={"novel": ["123"]},
        scan_state_json={"old": "cursor"}, last_success_at=now_utc())
    class Session:
        async def commit(self):
            pass
    class Client:
        async def get_authenticated_web_user_id(self, *, force=False):
            assert force is True
            return 20
        async def get_authenticated_user_id(self):
            raise AssertionError("Web follow list must not use App account identity")
    service = SourceSyncService(Session(), Client(), None)
    assert await service._bind_self_account(source) == "20"
    assert source.account_id == "20"
    assert source.frontier_json is None
    assert source.scan_state_json is None
    assert source.last_success_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type,active_id,app_id,enabled,expected_id", [
    (SyncSourceType.BOOKMARKS, "20", 10, True, None),
    (SyncSourceType.FOLLOWED_SERIES, "20", 10, True, None),
    (SyncSourceType.BOOKMARKS, "20", 10, False, "10"),
    (SyncSourceType.BOOKMARKS, None, 10, True, "10"),
    (SyncSourceType.BOOKMARKS, "20", 20, True, "20"),
    (SyncSourceType.FOLLOWED_SERIES, "20", 20, True, "30"),
])
async def test_self_source_guard_rejects_old_worker_without_overwriting_progress(
    monkeypatch, source_type, active_id, app_id, enabled, expected_id,
):
    monkeypatch.setattr(get_settings(), "account_migration_enabled", enabled)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SyncSource.__table__.create)
        await connection.run_sync(AccountMigrationState.__table__.create)
    calls = []
    class Client:
        async def get_authenticated_user_id(self):
            calls.append("app")
            return app_id
        async def get_authenticated_web_user_id(self, *, force=False):
            calls.append("web")
            assert force is True
            return 30
    previous_frontier = {"novel": ["100"]}
    previous_scan = {"full": True, "lanes": {"novel": {"next_params": {"offset": 90}}}}
    try:
        async with sessions() as session:
            if active_id is not None:
                session.add(AccountMigrationState(id=1, active_user_id=active_id))
            source = SyncSource(id=1, source_type=source_type, remote_user_id="self", account_id="55",
                restrict_mode=SyncRestrict.PUBLIC, include_illust=False, include_novel=True,
                expand_series=False, enabled=True, interval_seconds=3600,
                frontier_json=previous_frontier, scan_state_json=previous_scan)
            session.add(source)
            await session.commit()
            service = SourceSyncService(session, Client(), None)
            if expected_id is None:
                with pytest.raises(PixivAuthError, match="Another Pixiv account is active"):
                    await service._bind_self_account(source)
                assert source.account_id == "55"
                assert source.frontier_json == previous_frontier
                assert source.scan_state_json == previous_scan
                assert calls == ["app"]
            else:
                assert await service._bind_self_account(source) == expected_id
        async with sessions() as session:
            saved = await session.get(SyncSource, 1)
            assert saved.account_id == (expected_id if expected_id is not None else "55")
            assert saved.frontier_json == (None if expected_id is not None else previous_frontier)
            assert saved.scan_state_json == (None if expected_id is not None else previous_scan)
    finally:
        await engine.dispose()
