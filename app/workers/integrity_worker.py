from __future__ import annotations

import asyncio
import logging
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis_client import close_redis, get_redis
from app.core.redis_lock import RenewingRedisLock
from app.core.schema_version import ensure_schema_version
from app.db.session import SessionLocal, engine
from app.services.storage_integrity import StorageIntegrityService

logger = logging.getLogger(__name__)


class IntegrityWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = get_redis()
        self.stop_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None

    async def run(self) -> None:
        if self.settings.schema_check_on_startup:
            await ensure_schema_version(engine)

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("integrity_worker_started")
        try:
            if self.settings.integrity_audit_initial_delay_seconds > 0:
                if await self._wait_or_stop(
                    self.settings.integrity_audit_initial_delay_seconds
                ):
                    return

            if self.settings.integrity_audit_interval_seconds <= 0:
                await self.stop_event.wait()
                return

            while not self.stop_event.is_set():
                await self._run_once()
                if await self._wait_or_stop(
                    self.settings.integrity_audit_interval_seconds
                ):
                    break
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None
            logger.info("integrity_worker_stopped")

    async def _run_once(self) -> None:
        lock = RenewingRedisLock(
            self.redis,
            self.settings.integrity_worker_lock_key,
            ttl_seconds=self.settings.integrity_worker_lock_ttl_seconds,
            wait_seconds=0,
        )
        if not await lock.acquire():
            logger.info("integrity_audit_skipped_lock_busy")
            return
        try:
            async with SessionLocal() as session:
                result = await StorageIntegrityService(session).audit(
                    verify_hash=self.settings.integrity_audit_verify_hash,
                    repair_safe=self.settings.integrity_audit_repair_safe,
                )
            report = result.get("after") if result.get("repair_safe") else result
            logger.info(
                "integrity_audit_completed",
                extra={
                    "ok": report.get("ok"),
                    "verify_hash": report.get("verify_hash"),
                    "missing_files": len(report.get("missing_files") or []),
                    "hash_mismatches": len(report.get("hash_mismatches") or []),
                    "orphan_version_dirs": len(
                        report.get("orphan_version_dirs") or []
                    ),
                    "part_files": len(report.get("part_files") or []),
                    "truncated": report.get("truncated"),
                },
            )
        except Exception:
            logger.error("integrity_audit_failed", exc_info=True)
        finally:
            await lock.release()

    async def _heartbeat_loop(self) -> None:
        interval = max(2, self.settings.integrity_worker_heartbeat_ttl_seconds // 3)
        while True:
            await self.redis.set(
                self.settings.integrity_worker_heartbeat_key,
                "ok",
                ex=self.settings.integrity_worker_heartbeat_ttl_seconds,
            )
            await asyncio.sleep(interval)

    async def _wait_or_stop(self, seconds: int) -> bool:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def stop(self) -> None:
        self.stop_event.set()


async def main() -> None:
    configure_logging()
    worker = IntegrityWorker()
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
