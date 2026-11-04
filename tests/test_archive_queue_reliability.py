from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue


@pytest.fixture
async def queue():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield ArchiveJobQueue(redis)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_concurrent_enqueue_publishes_one_job_with_all_sources(queue):
    jobs = await asyncio.gather(*(
        queue.enqueue(WorkType.NOVEL, 123, source_id=index)
        for index in range(1, 21)
    ))
    assert len({job["job_id"] for job in jobs}) == 1
    assert await queue.redis.xlen(queue.stream) == 1
    stored = await queue.get(jobs[0]["job_id"])
    assert stored["source_ids"] == list(range(1, 21))
    assert stored["bookmark_if_missing"] is False


@pytest.mark.asyncio
async def test_bookmark_upgrade_only_merges_before_processing(queue):
    automatic = await queue.enqueue(WorkType.NOVEL, 1, force_refresh=False)
    manual = await queue.enqueue(
        WorkType.NOVEL, 1, bookmark_if_missing=True, force_refresh=False,
    )
    assert manual["job_id"] == automatic["job_id"]
    assert manual["bookmark_if_missing"] is True
    await queue.mark_running(manual["job_id"], "worker")
    merged = await queue.enqueue(WorkType.NOVEL, 1, force_refresh=False, source_id=9)
    assert merged["job_id"] == manual["job_id"]
    assert merged["bookmark_if_missing"] is True

    running = await queue.enqueue(WorkType.NOVEL, 2, force_refresh=False)
    await queue.mark_running(running["job_id"], "worker")
    later = await queue.enqueue(
        WorkType.NOVEL, 2, force_refresh=False, bookmark_if_missing=True,
    )
    assert later["job_id"] != running["job_id"]
    assert (await queue.get(running["job_id"]))["bookmark_if_missing"] is False


@pytest.mark.asyncio
async def test_finalizing_is_recoverable_and_does_not_accept_new_sources(queue):
    old = await queue.enqueue(WorkType.NOVEL, 3, source_id=1, bookmark_if_missing=True)
    await queue.mark_running(old["job_id"], "crashed-worker")
    await queue.mark_finalizing(old["job_id"])
    newer = await queue.enqueue(WorkType.NOVEL, 3, source_id=2)
    assert newer["job_id"] != old["job_id"]
    recovered = await queue.mark_running(old["job_id"], "replacement-worker")
    assert recovered["status"] == "running"
    assert recovered["attempts"] == 2
    assert recovered["bookmark_if_missing"] is True
    assert recovered["source_ids"] == [1]
    await queue.mark_succeeded(old["job_id"], {"saved": True})
    assert await queue.redis.get(queue.dedupe_key(WorkType.NOVEL, 3)) == newer["job_id"]


@pytest.mark.asyncio
async def test_replay_preserves_bookmark_requirement(queue):
    job = await queue.enqueue(WorkType.NOVEL, 4, bookmark_if_missing=True)
    await queue.mark_failed(job["job_id"], ValueError("fixture"))
    replayed = await queue.replay(job["job_id"])
    assert replayed["job_id"] != job["job_id"]
    assert replayed["bookmark_if_missing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("bad input"), asyncio.CancelledError()])
async def test_worker_leaves_pending_message_when_cancelled_or_status_write_fails(monkeypatch, failure, maintenance_redis):
    from app.workers import archive_worker as module

    job = {"job_id": "job", "pixiv_id": 8, "kind": "novel", "attempts": 1,
           "force_refresh": False, "bookmark_if_missing": True}
    queue = SimpleNamespace(
        mark_running=AsyncMock(return_value=job), ack=AsyncMock(),
        mark_dead=AsyncMock(side_effect=ConnectionError("Redis unavailable")),
    )
    worker = object.__new__(module.ArchiveWorker)
    worker.queue = queue
    worker.consumer = "test"
    worker.client = object()
    worker.redis = maintenance_redis
    worker.settings = SimpleNamespace(archive_job_max_attempts=5)
    worker._touch_job_lease = AsyncMock()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    service = SimpleNamespace(_get=AsyncMock(side_effect=failure))
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(module, "CacheService", lambda *args, **kwargs: service)
    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else ConnectionError
    with pytest.raises(expected):
        await worker._process("1-0", "job")
    queue.ack.assert_not_awaited()
    assert service._get.await_args.kwargs["bookmark_if_missing"] is True
