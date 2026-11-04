from __future__ import annotations

from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from app.core.config import get_settings
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.workers import archive_worker


async def process_result(monkeypatch, result_source, *, force_refresh=True, max_attempts=5):
    """Run the actual worker disposition logic against a real in-memory Redis queue."""
    monkeypatch.setattr(get_settings(), "archive_job_max_attempts", max_attempts)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = ArchiveJobQueue(redis)
    events = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    class Cache:
        def __init__(self, *args, **kwargs):
            pass
        async def _get(self, pixiv_id, kind, **kwargs):
            assert kwargs["bypass_ttl"] is force_refresh
            assert kwargs["refresh"] is True
            events.append("cache_result")
            return SimpleNamespace(
                pixiv_id=pixiv_id, source=result_source,
                model_dump=lambda **_: {"pixiv_id": pixiv_id, "source": result_source},
            )
    async def record_sources(*args, **kwargs):
        events.append("record_sources")
    async def update_member(job, status, exc=None):
        events.append(f"member_{status}")
    async def lease(_):
        pass
    original_succeeded = queue.mark_succeeded
    async def succeeded(*args):
        events.append("mark_succeeded")
        await original_succeeded(*args)
    monkeypatch.setattr(archive_worker, "SessionLocal", Session)
    monkeypatch.setattr(archive_worker, "CacheService", Cache)
    monkeypatch.setattr(archive_worker, "record_work_sources", record_sources)
    monkeypatch.setattr(queue, "mark_succeeded", succeeded)
    worker = archive_worker.ArchiveWorker.__new__(archive_worker.ArchiveWorker)
    worker.settings = get_settings()
    worker.redis = redis
    worker.queue = queue
    worker.consumer = "test-consumer"
    worker.client = None
    worker._touch_job_lease = lease
    worker._update_series_member = update_member
    try:
        submitted = await queue.enqueue(WorkType.NOVEL, 123, force_refresh=force_refresh)
        stream_id, job_id = await queue.read_one(worker.consumer, block_ms=1)
        assert job_id == submitted["job_id"]
        await worker._process(stream_id, job_id)
        job = await queue.get(job_id)
        delayed = await redis.zscore(queue.retry_zset, job_id)
        remaining = await redis.xlen(queue.stream)
        dead_events = await redis.xlen(queue.dead_stream)
        return job, delayed, remaining, dead_events, events
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_source", [
    "local-stale-remote-unavailable", "local-stale-remote-error", "local-lock-timeout",
])
async def test_forced_verification_retries_instead_of_marking_stale_success(monkeypatch, result_source):
    job, delayed, remaining, dead_events, events = await process_result(monkeypatch, result_source)
    assert job["status"] == "retry_wait"
    assert job["error_type"] == "PixivUnavailableError"
    assert job["attempts"] == 1
    assert delayed is not None
    assert remaining == 0  # Retry scheduling durably ACKs the consumed stream entry.
    assert dead_events == 0
    assert events == ["cache_result"]


@pytest.mark.asyncio
async def test_failed_strict_verification_exhausts_attempts_into_dead_letter(monkeypatch):
    job, delayed, remaining, dead_events, events = await process_result(
        monkeypatch, "local-stale-remote-unavailable", max_attempts=1)
    assert job["status"] == "dead"
    assert job["dead_reason"] == "retry_exhausted"
    assert job["error_type"] == "PixivUnavailableError"
    assert delayed is None and remaining == 0 and dead_events == 1
    assert events == ["cache_result", "member_failed"]


@pytest.mark.asyncio
async def test_nonforced_archive_retains_existing_stale_fallback_policy(monkeypatch):
    job, delayed, remaining, dead_events, events = await process_result(
        monkeypatch, "local-stale-remote-unavailable", force_refresh=False)
    assert job["status"] == "succeeded"
    assert delayed is None and remaining == 0 and dead_events == 0
    assert events == ["cache_result", "record_sources", "mark_succeeded", "member_archived"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_source", ["local-validated", "remote-created", "remote-updated"])
async def test_successful_forced_verification_completes_normally(monkeypatch, result_source):
    job, delayed, remaining, dead_events, events = await process_result(monkeypatch, result_source)
    assert job["status"] == "succeeded"
    assert delayed is None and remaining == 0 and dead_events == 0
    assert "mark_succeeded" in events
