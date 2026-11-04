"""Local Ugoira MP4 worker. This module never creates a Pixiv client."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from app.core.config import get_settings
from app.core.errors import ArchiveStorageError
from app.core.logging import configure_logging
from app.core.maintenance import maintenance_active
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import ensure_schema_version
from app.db.session import SessionLocal, engine
from app.services.media_jobs import MediaJobQueue
from app.services.ugoira_preview import UgoiraPreviewService

logger = logging.getLogger(__name__)


class MediaWorker:
    def __init__(self):
        self.settings = get_settings()
        self.redis = get_redis()
        self.queue = MediaJobQueue(self.redis)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.stop_event = asyncio.Event()

    async def run(self):
        if self.settings.schema_check_on_startup:
            await ensure_schema_version(engine)
        await self.queue.ensure_group()
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self.stop_event.is_set():
                if await maintenance_active(self.redis):
                    await asyncio.sleep(2)
                    continue
                await self.queue.promote_due_retries()
                message = await self.queue.read_one(self.consumer, block_ms=self.settings.media_worker_block_ms)
                if message:
                    await self._process(*message)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _process(self, stream_id, job_id):
        job = await self.queue.mark_running(job_id, self.consumer)
        if job is None:
            await self.queue.ack(stream_id)
            return
        lease = asyncio.create_task(self._touch_job_lease(stream_id))
        acknowledge = False
        try:
            async with SessionLocal() as session:
                service = UgoiraPreviewService(session, self.redis)

                async def progress(phase):
                    await self.queue.set_phase(job_id, phase)

                result = await service.generate(job["pixiv_id"], job["version_no"], progress=progress)
            await self.queue.mark_succeeded(job_id, result)
            acknowledge = True
        except Exception as exc:
            retryable = not isinstance(exc, (ValueError, LookupError, FileNotFoundError, PermissionError, ArchiveStorageError))
            if retryable and int(job["attempts"]) < self.settings.media_job_max_attempts:
                await self.queue.schedule_retry(
                    stream_id, job_id, exc,
                    delay_seconds=self.queue.retry_delay_seconds(job["attempts"]),
                )
            else:
                await self.queue.mark_dead(job_id, exc, reason="retry_exhausted" if retryable else "non_retryable")
                acknowledge = True
            logger.warning("media_job_failed", extra={"job_id": job_id, "error_type": type(exc).__name__})
        finally:
            lease.cancel()
            try:
                await lease
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("media_job_lease_failed", extra={"job_id": job_id})
        if acknowledge:
            await self.queue.ack(stream_id)

    async def _touch_job_lease(self, stream_id):
        interval = max(1, self.settings.media_job_claim_idle_ms // 3000)
        while True:
            await asyncio.sleep(interval)
            await self.queue.touch(stream_id, self.consumer)

    async def _heartbeat_loop(self):
        ttl = self.settings.media_worker_heartbeat_ttl_seconds
        while True:
            await self.redis.set(f"{self.settings.media_job_prefix.rstrip(':')}:worker:healthy", self.consumer, ex=ttl)
            await asyncio.sleep(max(1, ttl // 3))

    def stop(self):
        self.stop_event.set()


async def main():
    configure_logging()
    worker = MediaWorker()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
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
