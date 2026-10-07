from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from app.clients.pixivpy_client import PixivPyClient
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.maintenance import maintenance_active
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import ensure_schema_version
from app.db.session import SessionLocal, engine
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import CacheService

logger = logging.getLogger(__name__)


class ArchiveWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = get_redis()
        self.queue = ArchiveJobQueue(self.redis)
        self.client = PixivPyClient()
        self.stop_event = asyncio.Event()
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self._heartbeat_task: asyncio.Task | None = None

    async def run(self) -> None:
        if self.settings.schema_check_on_startup:
            await ensure_schema_version(engine)
        await self.queue.ensure_group()
        logger.info("archive_worker_started", extra={"consumer": self.consumer})
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self.stop_event.is_set():
                if await maintenance_active(self.redis):
                    await asyncio.sleep(2)
                    continue
                item = await self.queue.read_one(
                    self.consumer,
                    block_ms=self.settings.archive_worker_block_ms,
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

        logger.info("archive_worker_stopped", extra={"consumer": self.consumer})

    async def _process(self, stream_id: str, job_id: str) -> None:
        job = await self.queue.mark_running(job_id, self.consumer)
        if job is None:
            await self.queue.ack(stream_id)
            return

        logger.info(
            "archive_job_started",
            extra={
                "job_id": job_id,
                "pixiv_id": job["pixiv_id"],
                "kind": job["kind"],
                "attempts": job["attempts"],
            },
        )
        lease_task = asyncio.create_task(self._touch_job_lease(stream_id))
        try:
            async with SessionLocal() as session:
                service = CacheService(session, self.client, redis_client=self.redis)
                result = await service._get(
                    int(job["pixiv_id"]),
                    WorkType(job["kind"]),
                    refresh=True,
                    bypass_ttl=bool(job["force_refresh"]),
                )
            await self.queue.mark_succeeded(
                job_id,
                result.model_dump(mode="json"),
            )
            logger.info("archive_job_succeeded", extra={"job_id": job_id})
        except Exception as exc:
            await self.queue.mark_failed(job_id, exc)
            logger.error(
                "archive_job_failed",
                extra={"job_id": job_id, "error_type": exc.__class__.__name__},
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
        interval = max(5, self.settings.archive_job_claim_idle_ms // 3000)
        while True:
            await asyncio.sleep(interval)
            await self.queue.touch(stream_id, self.consumer)

    async def _heartbeat_loop(self) -> None:
        interval = max(2, self.settings.archive_worker_heartbeat_ttl_seconds // 3)
        while True:
            await self._heartbeat()
            await asyncio.sleep(interval)

    async def _heartbeat(self) -> None:
        await self.redis.set(
            self.settings.archive_worker_heartbeat_key,
            self.consumer,
            ex=self.settings.archive_worker_heartbeat_ttl_seconds,
        )

    def stop(self) -> None:
        self.stop_event.set()


async def main() -> None:
    configure_logging()
    worker = ArchiveWorker()
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
