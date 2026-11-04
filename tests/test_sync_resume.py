from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.work import SyncSource, SyncSourceType, SyncRestrict, WorkType, WorkStatus
from app.schemas.pixiv import DiscoveryPage, DiscoveredWork
from app.services.cache_service import now_utc
from app.services.source_sync import SourceSyncService
from app.services.sync_jobs import SyncJobQueue


@pytest.mark.asyncio
async def test_scan_checkpoints_resume_and_commit_frontier_only_at_end(monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_max_pages_per_run", 1)
    monkeypatch.setattr(get_settings(), "sync_page_delay_seconds", 0)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SyncSource.__table__.create)
    calls = []

    class Client:
        async def discover_author_novels(self, user_id, params):
            calls.append(params)
            offset = (params or {}).get("offset", 0)
            return DiscoveryPage(
                items=[DiscoveredWork(pixiv_id=100-offset, work_type=WorkType.NOVEL)],
                next_params={"offset": 1} if offset == 0 else None,
            )

    async def queue_works(items, source_id, **kwargs):
        return len(items), 0

    try:
        async with sessions() as session:
            source = SyncSource(id=1, source_type=SyncSourceType.AUTHOR,
                remote_user_id="42", restrict_mode=SyncRestrict.PUBLIC,
                include_illust=False, include_novel=True, expand_series=False,
                enabled=True, interval_seconds=3600, frontier_json={"novel": ["50"]})
            session.add(source)
            await session.commit()
            service = SourceSyncService(session, Client(), None)
            service._queue_works = queue_works
            first = await service.run_source(source, full=True)
            assert first["complete"] is False
            assert source.frontier_json == {"novel": ["50"]}
            assert source.last_success_at is None
            assert source.scan_state_json["lanes"]["novel"]["next_params"] == {"offset": 1}
        # A new service/session must resume using database state, not in-memory state.
        async with sessions() as session:
            source = await session.get(SyncSource, 1)
            service = SourceSyncService(session, Client(), None)
            service._queue_works = queue_works
            second = await service.run_source(source)
            assert second["complete"] is True
            assert second["full"] is True
            assert source.scan_state_json is None
            assert source.frontier_json == {"novel": ["100"]}
            assert source.last_success_at is not None
        assert calls == [None, {"offset": 1}]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_exact_page_budget_success_and_pending_cursor(monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_max_pages_per_run", 2)
    monkeypatch.setattr(get_settings(), "sync_page_delay_seconds", 0)
    service = SourceSyncService(None, None, None)
    seen = []

    async def queue(items, source_id, **kwargs):
        seen.extend(item.pixiv_id for item in items)
        return len(items), 0

    service._queue_works = queue
    async def fetch(params):
        page = (params or {}).get("page", 1)
        return DiscoveryPage(items=[DiscoveredWork(pixiv_id=page, work_type=WorkType.NOVEL)],
                             next_params={"page": 2} if page == 1 else None)
    result = await service._scan(fetch, set(), full=True)
    assert result["complete"] is True
    assert result["pages"] == 2
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_full_request_while_running_is_executed_after_incremental_job():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = SyncJobQueue(redis)
        job = await queue.enqueue(12)
        running = await queue.mark_running(job["job_id"], "worker")
        assert running["full"] is False
        requested = await queue.enqueue(12, full=True)
        assert requested["job_id"] == job["job_id"]
        assert requested["rerun_full"] is True
        assert await queue.mark_succeeded(job["job_id"], {"complete": True}) is True
        followup = await queue.mark_running(job["job_id"], "worker")
        assert followup["full"] is True
        assert followup["rerun_full"] is False
        assert followup["restart_scan"] is True
        assert followup["attempts"] == 2
        assert await queue.mark_succeeded(job["job_id"], {"complete": True}) is False
        assert await queue.mark_running(job["job_id"], "worker") is None
        assert await redis.get(queue.dedupe_key(12)) is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_queued_full_request_upgrades_task_without_extra_message():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = SyncJobQueue(redis)
        job = await queue.enqueue(3)
        requested = await queue.enqueue(3, full=True)
        assert requested["deduplicated"] is True
        assert await redis.xlen(queue.stream) == 1
        running = await queue.mark_running(job["job_id"], "worker")
        assert running["full"] is True
        assert await queue.mark_succeeded(job["job_id"], {}) is False
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_full_request_survives_current_incremental_failure():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = SyncJobQueue(redis)
        job = await queue.enqueue(4)
        await queue.mark_running(job["job_id"], "worker")
        await queue.enqueue(4, full=True)
        assert await queue.mark_failed(job["job_id"], RuntimeError("temporary")) is True
        assert (await queue.mark_running(job["job_id"], "worker"))["full"] is True
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_recent_archives_skip_upstream_but_expired_work_is_queued(monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_work_recheck_ttl_seconds", 3600)
    works = [SimpleNamespace(id="1", work_type=WorkType.NOVEL, work_kind="novel", status=WorkStatus.ACTIVE,
                             last_checked_at=now_utc()),
             SimpleNamespace(id="2", work_type=WorkType.NOVEL, work_kind="novel", status=WorkStatus.ACTIVE,
                             last_checked_at=now_utc()-timedelta(hours=2))]
    class Session:
        async def execute(self, _):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: works))
    service = SourceSyncService(Session(), None, None)
    jobs = []
    async def enqueue(kind, pixiv_id, **kwargs):
        jobs.append(pixiv_id)
    service.archive_queue = SimpleNamespace(enqueue=enqueue)
    result = await service._queue_works(
        [DiscoveredWork(pixiv_id=i, work_type=WorkType.NOVEL) for i in (1, 2, 3)], None)
    assert result == (2, 1)
    assert jobs == [2, 3]


@pytest.mark.asyncio
async def test_followed_series_imports_typed_independent_subscriptions(monkeypatch):
    class Client:
        async def discover_followed_series(self, series_type):
            return [{"series_id": 123, "series_type": series_type, "title": "Series"}]
    class Session:
        async def commit(self):
            pass
    service = SourceSyncService(Session(), Client(), None)
    subscriptions = []
    async def ensure(kind, series_id, interval, **kwargs):
        subscriptions.append((kind, series_id, kwargs["run_now"]))
        return SimpleNamespace(id=len(subscriptions), enabled=True), True
    jobs = []
    async def enqueue(source_id, **kwargs):
        jobs.append((source_id, kwargs["full"]))
    monkeypatch.setattr("app.services.source_sync.SyncJobQueue",
                        lambda _: SimpleNamespace(enqueue=enqueue))
    service._ensure_series_subscription = ensure
    source = SimpleNamespace(id=1, source_type=SyncSourceType.FOLLOWED_SERIES,
                             include_novel=True, include_illust=True, interval_seconds=3600,
                             last_success_at=None, account_id="99")
    result = await service._run_followed_series_source(source)
    assert subscriptions == [("novel", "123", True), ("manga", "123", True)]
    assert result["created_subscriptions"] == 2
    assert source.frontier_json == {"novel": ["123"], "manga": ["123"]}
    assert jobs == [(1, True), (2, True)]


@pytest.mark.asyncio
async def test_running_full_scan_accepts_new_full_request_and_restarts_checkpoint():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = SyncJobQueue(redis)
        job = await queue.enqueue(5, full=True)
        assert job["restart_scan"] is False
        await queue.mark_running(job["job_id"], "worker")
        requested = await queue.enqueue(5, full=True)
        assert requested["rerun_full"] is True
        assert await queue.mark_succeeded(job["job_id"], {"complete": False}) is True
        followup = await queue.mark_running(job["job_id"], "worker")
        assert followup["full"] is True
        assert followup["restart_scan"] is True
        await queue.clear_restart_scan(job["job_id"])
        assert (await queue.get(job["job_id"]))["restart_scan"] is False
    finally:
        await redis.aclose()


def sync_api_fixture(monkeypatch, *, enabled=True):
    from app.api import sync as api
    source = SyncSource(id=7, source_type=SyncSourceType.BOOKMARKS,
                        remote_user_id="self", restrict_mode=SyncRestrict.PUBLIC,
                        include_illust=True, include_novel=True, expand_series=False,
                        enabled=enabled, interval_seconds=3600,
                        frontier_json={"novel": ["100"]},
                        scan_state_json={"full": True, "lanes": {"novel": {"next_params": {"p": 2}}}})
    class Session:
        async def execute(self, _):
            return SimpleNamespace(scalar_one_or_none=lambda: source)
        async def get(self, *args):
            return source
        async def commit(self):
            pass
        async def refresh(self, _):
            pass
    requests = []
    async def enqueue(source_id, *, full=False):
        requests.append((source_id, full))
        return {"job_id": "rescan"}
    monkeypatch.setattr(api, "SyncJobQueue", lambda _: SimpleNamespace(enqueue=enqueue))
    monkeypatch.setattr(api, "get_redis", lambda: None)
    return api, source, Session(), requests


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_upsert_config_change_or_reenable_enqueues_full_even_without_run_now(monkeypatch, enabled):
    api, source, session, requests = sync_api_fixture(monkeypatch, enabled=enabled)
    result = await api._upsert_source(
        session, source_type=source.source_type, remote_user_id=source.remote_user_id,
        restrict=source.restrict_mode, include_illust=True, include_novel=True,
        expand_series=enabled, interval_seconds=3600, run_now=False)
    assert requests == [(7, True)]
    assert result["job"]["job_id"] == "rescan"
    assert source.scan_state_json is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_patch_config_change_or_reenable_enqueues_full(monkeypatch, enabled):
    api, source, session, requests = sync_api_fixture(monkeypatch, enabled=enabled)
    patch = api.SyncSourcePatch(expand_series=True) if enabled else api.SyncSourcePatch(enabled=True)
    result = await api.patch_sync_source(7, patch, session)
    assert requests == [(7, True)]
    assert result["job"]["job_id"] == "rescan"
    assert source.scan_state_json is None


@pytest.mark.asyncio
async def test_manual_full_run_rejects_disabled_source(monkeypatch):
    from fastapi import HTTPException
    api, _, session, requests = sync_api_fixture(monkeypatch, enabled=False)
    with pytest.raises(HTTPException) as exc:
        await api.run_sync_source(7, full=True, session=session)
    assert exc.value.status_code == 409
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_worker_clears_restart_checkpoint_and_honors_disabled(monkeypatch, enabled, maintenance_redis):
    from app.workers import sync_worker as worker_module
    source = SimpleNamespace(id=9, enabled=enabled, scan_state_json={"old": "checkpoint"})
    events = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, *args):
            return source
        async def commit(self):
            events.append("reset_committed")
    class Queue:
        async def mark_running(self, *args):
            return {"source_id": 9, "full": True, "restart_scan": True}
        async def clear_restart_scan(self, _):
            assert source.scan_state_json is None
            events.append("flag_consumed")
        async def mark_succeeded(self, _, result):
            assert bool(result.get("skipped")) is (not enabled)
            events.append("finished")
            return False
        async def mark_failed(self, _, exc):
            raise exc
        async def ack(self, _):
            events.append("ack")
    class Service:
        def __init__(self, *args):
            pass
        async def run_source(self, item, *, full):
            assert item.scan_state_json is None and full
            assert events == ["reset_committed", "flag_consumed"]
            events.append("scan")
            return {"complete": True}
    async def lease(_):
        pass
    monkeypatch.setattr(worker_module, "SessionLocal", Session)
    monkeypatch.setattr(worker_module, "SourceSyncService", Service)
    worker = worker_module.SyncWorker.__new__(worker_module.SyncWorker)
    worker.queue = Queue()
    worker.consumer = "worker"
    worker.client = None
    worker.redis = maintenance_redis
    worker._touch_job_lease = lease
    await worker._process("1-0", "job")
    assert events == (["reset_committed", "flag_consumed", "scan", "finished", "ack"]
                      if enabled else ["finished", "ack"])
