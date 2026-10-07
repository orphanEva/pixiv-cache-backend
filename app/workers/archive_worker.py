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
from app.core.errors import is_retryable_archive_error
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import ensure_schema_version
from app.db.session import SessionLocal, engine
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import CacheService
from app.services.work_sources import record_work_sources

logger = logging.getLogger(__name__)


class ArchiveWorker:
    """执行真正的 Pixiv 归档任务，并负责失败重试与死信落盘。"""

    def __init__(self) -> None:
        """初始化 Redis 队列、Pixiv 客户端和 worker 身份。"
        self.settings = get_settings()
        self.redis = get_redis()
        self.queue = ArchiveJobQueue(self.redis)
        self.client = PixivPyClient()
        self.stop_event = asyncio.Event()
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self._heartbeat_task: asyncio.Task | None = None

    async def run(self) -> None:
        """启动归档 worker 主循环，持续提升到期重试并消费新任务。"""
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
                promoted = await self.queue.promote_due_retries()
                if promoted:
                    logger.info(
                        "archive_retry_promoted",
                        extra={"count": promoted},
                    )
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
        """处理单个归档任务，并按错误类型决定重试或进入 DLQ。"""
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
                latest_job = await self.queue.mark_finalizing(job_id)
                await record_work_sources(
                    session,
                    str(result.pixiv_id),
                    (latest_job or job).get("source_ids") or [],
                )
            await self.queue.mark_succeeded(
                job_id,
                result.model_dump(mode="json"),
            )
            logger.info("archive_job_succeeded", extra={"job_id": job_id})
        except Exception as exc:
            retryable = is_retryable_archive_error(exc)
            attempts = int(job.get("attempts") or 0)
            if retryable and attempts < self.settings.archive_job_max_attempts:
                delay = self.queue.retry_delay_seconds(attempts)
                await self.queue.schedule_retry(
                    stream_id,
                    job_id,
                    exc,
                    delay_seconds=delay,
                )
                logger.warning(
                    "archive_job_retry_scheduled",
                    extra={
                        "job_id": job_id,
                        "pixiv_id": job["pixiv_id"],
                        "attempts": attempts,
                        "retry_in_seconds": delay,
                        "error_type": exc.__class__.__name__,
                    },
                )
            else:
                reason = "retry_exhausted" if retryable else "non_retryable"
                await self.queue.mark_dead(job_id, exc, reason=reason)
                logger.error(
                    "archive_job_dead",
                    extra={
                        "job_id": job_id,
                        "pixiv_id": job["pixiv_id"],
                        "attempts": attempts,
                        "dead_reason": reason,
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
        """长任务执行期间持续刷新 Stream pending lease。"""
        interval = max(5, self.settings.archive_job_claim_idle_ms // 3000)
        while True:
            await asyncio.sleep(interval)
            await self.queue.touch(stream_id, self.consumer)

    async def _heartbeat_loop(self) -> None:
        """周期写入 worker heartbeat，供健康检查与运维 API 使用。"""
        interval = max(2, self.settings.archive_worker_heartbeat_ttl_seconds // 3)
        while True:
            await self._heartbeat()
            await asyncio.sleep(interval)

    async def _heartbeat(self) -> None:
        """写入一次 archive worker heartbeat。"""
        await self.redis.set(
            self.settings.archive_worker_heartbeat_key,
            self.consumer,
            ex=self.settings.archive_worker_heartbeat_ttl_seconds,
        )

    def stop(self) -> None:
        """请求 worker 在当前任务结束后优雅退出。"""
        self.stop_event.set()


async def main() -> None:
    """archive worker 进程入口。"""
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
