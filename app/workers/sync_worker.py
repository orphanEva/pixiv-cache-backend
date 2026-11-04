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
from app.core.errors import ArchiveMaintenanceError
from app.core.logging import configure_logging
from app.core.maintenance import maintenance_active, MaintenanceWriter
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import ensure_schema_version
from app.db.session import SessionLocal, engine
from app.models.work import SyncSource
from app.services.cache_service import now_utc
from app.services.account_migration import AccountMigrationService
from app.services.source_sync import SourceSyncService
from app.services.sync_jobs import SyncJobQueue
from app.services.remote_recheck import RemoteRecheckService

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
        self._last_account_migration = 0.0
        self._account_activation_done = False
        self._activated_account_id: str | None = None
        self._account_superseded = False
        self._last_recheck_schedule = 0.0

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
                await self._migrate_account_if_needed()
                if self._account_superseded:
                    await asyncio.sleep(2)
                    continue
                try:
                    await self._schedule_due_if_needed()
                except ArchiveMaintenanceError:
                    await asyncio.sleep(2)
                    continue
                await self._schedule_remote_rechecks()
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

    async def _migrate_account_if_needed(self) -> None:
        if not self.settings.account_migration_enabled or self._account_superseded:
            return
        now_mono = asyncio.get_running_loop().time()
        if now_mono - self._last_account_migration < self.settings.account_migration_tick_seconds:
            return
        self._last_account_migration = now_mono
        try:
            async with MaintenanceWriter(self.redis), SessionLocal() as session:
                service = AccountMigrationService(
                    session, self.client, self.redis,
                )
                try:
                    result = await service.tick(allow_account_switch=not self._account_activation_done)
                finally:
                    # Consume startup authorization even if a later item or an
                    # uncertain activation COMMIT fails. An older worker must
                    # never regain permission to replace a newer account.
                    if getattr(service, "activation_attempted", False):
                        self._account_activation_done = True
                        self._activated_account_id = service.activation_target
                target_id = result.get("target_user_id")
                if result.get("status") == "account_changed" or (
                    self._activated_account_id is not None and target_id is not None
                    and str(target_id) != self._activated_account_id
                ):
                    self._account_superseded = True
                    logger.warning("sync_worker_account_superseded_restart_required")
                elif result.get("activation_succeeded"):
                    self._account_activation_done = True
                    self._activated_account_id = str(target_id) if target_id else None
        except Exception as exc:
            # A failing export must not stop discovery. Persisted items resume
            # on the next tick; do not include upstream credential-bearing text.
            logger.warning("account_migration_tick_failed", extra={
                "error_type": exc.__class__.__name__,
            })

    async def _schedule_remote_rechecks(self) -> None:
        if not self.settings.remote_recheck_enabled:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_recheck_schedule < self.settings.remote_recheck_poll_seconds:
            return
        self._last_recheck_schedule = now
        try:
            async with SessionLocal() as session:
                await RemoteRecheckService(session, self.redis).schedule()
        except Exception as exc:
            logger.warning("remote_recheck_schedule_failed", extra={"error_type": type(exc).__name__})

    async def _schedule_due_if_needed(self) -> None:
        if self.settings.sync_scheduler_poll_seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        now_mono = loop.time()
        if now_mono - self._last_schedule < self.settings.sync_scheduler_poll_seconds:
            return
        self._last_schedule = now_mono
        now = now_utc()
        async with MaintenanceWriter(self.redis), SessionLocal() as session:
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
        disposition_saved = False
        try:
            async with MaintenanceWriter(self.redis), SessionLocal() as session:
                source = await session.get(SyncSource, job["source_id"])
                if source is None:
                    raise RuntimeError(f"Sync source {job['source_id']} no longer exists")
                if not source.enabled:
                    result = {
                        "source_id": source.id,
                        "skipped": True,
                        "reason": "source_disabled",
                    }
                else:
                    if job.get("restart_scan"):
                        source.scan_state_json = None
                        await session.commit()
                        # Clear only after the DB reset is durable, before scanning
                        # new pages, so reclaiming a later crash resumes progress.
                        await self.queue.clear_restart_scan(job_id)
                    result = await SourceSyncService(
                        session,
                        self.client,
                        self.redis,
                    ).run_source(source, full=bool(job.get("full")))
            followup = await self.queue.mark_succeeded(job_id, result)
            disposition_saved = True
            logger.info(
                "sync_job_full_rescan_queued" if followup else "sync_job_succeeded",
                extra={"job_id": job_id, "source_id": job["source_id"]},
            )
        except Exception as exc:
            await self.queue.mark_failed(job_id, exc)
            disposition_saved = True
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
            except Exception:
                logger.exception("sync_job_lease_refresh_failed", extra={"job_id": job_id})
            # A failed status write must leave the pending entry recoverable.
            if disposition_saved:
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
