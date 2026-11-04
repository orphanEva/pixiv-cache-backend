"""Bounded remote rechecks for archived works beyond discovery frontiers."""
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.core.config import get_settings
from app.core.maintenance import MaintenanceWriter
from app.core.redis_lock import RenewingRedisLock
from app.models.work import PixivWork, WorkStatus, WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import now_utc


class RemoteRecheckService:
    def __init__(self, session, redis_client):
        self.session = session
        self.redis = redis_client
        self.settings = get_settings()
        self.queue = ArchiveJobQueue(redis_client)

    def _due(self, now):
        cutoff = now - timedelta(seconds=self.settings.remote_recheck_interval_seconds)
        return (
            PixivWork.status == WorkStatus.ACTIVE,
            or_(PixivWork.last_checked_at.is_(None), PixivWork.last_checked_at <= cutoff),
            or_(PixivWork.next_recheck_at.is_(None), PixivWork.next_recheck_at <= now),
        )

    async def status(self) -> dict:
        due = (await self.session.execute(select(func.count()).select_from(PixivWork)
                                          .where(*self._due(now_utc())))).scalar_one()
        return {"enabled": self.settings.remote_recheck_enabled,
                "interval_seconds": self.settings.remote_recheck_interval_seconds,
                "batch_size": self.settings.remote_recheck_batch_size,
                "due_works": int(due)}

    async def schedule(self) -> dict:
        if not self.settings.remote_recheck_enabled:
            return {"queued": 0, "status": "disabled"}
        lock = RenewingRedisLock(self.redis, "pixiv:remote-recheck:scheduler",
                                 ttl_seconds=180, wait_seconds=0)
        if not await lock.acquire():
            return {"queued": 0, "status": "busy"}
        try:
            async with MaintenanceWriter(self.redis) as writer:
                # End any read snapshot before selecting due rows for reservation.
                await self.session.rollback()
                now = now_utc()
                works = (await self.session.execute(select(PixivWork).where(*self._due(now))
                    .order_by(PixivWork.last_checked_at, PixivWork.work_kind, PixivWork.id)
                    .limit(self.settings.remote_recheck_batch_size).with_for_update(skip_locked=True)
                )).scalars().all()
                jobs = []
                for work in works:
                    lock.ensure_alive()
                    await writer.ensure_alive()
                    job = await self.queue.enqueue(WorkType(work.work_kind), int(work.id),
                                                   force_refresh=True, bookmark_if_missing=False)
                    jobs.append(job["job_id"])
                    work.next_recheck_at = now + timedelta(seconds=self.settings.remote_recheck_reservation_seconds)
                lock.ensure_alive()
                await writer.ensure_alive()
                await self.session.commit()
                return {"queued": len(jobs), "job_ids": jobs, "status": "scheduled"}
        except BaseException:
            await self.session.rollback()
            raise
        finally:
            await lock.release()
