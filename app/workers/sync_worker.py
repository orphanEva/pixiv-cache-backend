from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from datetime import timedelta

from sqlalchemy import or_, select

from app.clients.pixivpy_client import PixivPyClient
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.maintenance import maintenance_active
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import ensure_schema_version
from app.db.session import SessionLocal, engine
from app.models.work import SyncSource
from app.services.cache_service import now_utc
from app.services.source_sync import SourceSyncService
from app.services.sync_jobs import SyncJobQueue

logger = logging.getLogger(__name__)


class SyncWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = get_redis()
        self.queue = SyncJobQueue(self.redis)
        self.client = PixivPyClient()
        self.stop_event = asyncio.Event()
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self._heartbeat_task: asyncio.Task | None = None
        self._last_schedule = 0.0

    async def run(self) -> None:
        if self.settings.schema_check_on_startup:
            await ensure_schema_version(engine)
        await self.queue.ensure_group()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("sync_worker_started", extra={"consumer": self.consumer})
        try:
            while not self.stop_event.is_set():
                if await maintenance_active(self.redis):
                    await asyncio.sleep(2)
                    continue
                await self._schedule_due_if_needed()
                item = await self.queue.read_one(
                    self.consumer,
                    block_ms=self.settings.sync_worker_block_ms,
                )
                if item is None:
                    continue
                stream_id, job_id = item
                await self._process(stream_id, job_id)
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None
            logger.info("sync_worker_stopped", extra={"consumer": self.consumer})

    async def _schedule_due_if_needed(self) -> None:
        loop = asyncio.get_running_loop()
        now_mono = loop.time()
        if now_mono - self._last_schedule < self.settings.sync_scheduler_poll_seconds:
            return
        self._last_schedule = now_mono
        now = now_utc()
        async with SessionLocal() as session:
            sources = (
                await session.execute(
                    select(SyncSource)
                    .where(
                        SyncSource.enabled.is_(True),
                        or_(
                            SyncSource.next_run_at.is_(None),
                            SyncSource.next_run_at <= now,
                        ),
                    )
                    .order_by(SyncSource.next_run_at.asc(), SyncSource.id.asc())
                    .limit(self.settings.sync_due_batch_size)
                )
            ).scalars().all()
            if not sources:
                return
            for source in sources:
                await self.queue.enqueue(source.id, full=False)
                reservation = min(
                    max(60, int(source.interval_seconds)),
                    self.settings.sync_schedule_reservation_seconds,
                )
                source.next_run_at = now + timedelta(seconds=reservation)
            await session.commit()

    async def _process(self, stream_id: str, job_id: str) -> None:
        job = await self.queue.mark_running(job_id, self.consumer)
        if job is None:
            await self.queue.ack(stream_id)
            return

        lease_task = asyncio.create_task(self._touch_job_lease(stream_id))
        try:
            async with SessionLocal() as session:
                source = await session.get(SyncSource, job["source_id"])
                if source is None:
                    raise RuntimeError(f"Sync source {job['source_id']} no longer exists")
                if not source.enabled and not job.get("full"):
                    result = {
                        "source_id": source.id,
                        "skipped": True,
                        "reason": "source_disabled",
                    }
                else:
                    result = await SourceSyncService(
                        session,
                        self.client,
                        self.redis,
                    ).run_source(source, full=bool(job.get("full")))
            await self.queue.mark_succeeded(job_id, result)
            logger.info(
                "sync_job_succeeded",
                extra={"job_id": job_id, "source_id": job["source_id"]},
            )
        except Exception as exc:
            await self.queue.mark_failed(job_id, exc)
            logger.error(
                "sync_job_failed",
                extra={
                    "job_id": job_id,
                    "source_id": job["source_id"],
                    "error_type": exc.__class__.__name__,
                },
                exc_info=True,
            )
        finally:
            lease_task.cancel()
            try:
                await lease_task
            except asyncio.CancelledError:
                pass
            await self.queue.ack(stream_id)

    async def _touch_job_lease(self, stream_id: str) -> None:
        interval = max(5, self.settings.sync_job_claim_idle_ms // 3000)
        while True:
            await asyncio.sleep(interval)
            await self.queue.touch(stream_id, self.consumer)

    async def _heartbeat_loop(self) -> None:
        interval = max(2, self.settings.sync_worker_heartbeat_ttl_seconds // 3)
        while True:
            await self.redis.set(
                self.settings.sync_worker_heartbeat_key,
                self.consumer,
                ex=self.settings.sync_worker_heartbeat_ttl_seconds,
            )
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self.stop_event.set()


async def main() -> None:
    configure_logging()
    worker = SyncWorker()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(name, worker.stop)
        except NotImplementedError:
            pass
    try:
        await worker.run()
    finally:
        await close_redis()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
