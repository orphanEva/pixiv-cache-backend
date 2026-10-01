from __future__ import annotations

import asyncio
import logging

from app.core.errors import PixivUnavailableError

logger = logging.getLogger(__name__)


class RenewingRedisLock:
    """Redis lock guard with periodic TTL renewal.

    Renewal failure marks the guard as lost. Call ensure_alive() before any
    irreversible/transactional commit so an expired lock can never silently
    allow two writers to commit the same work version.
    """

    def __init__(
        self,
        redis_client,
        name: str,
        *,
        ttl_seconds: int,
        wait_seconds: int,
        renew_interval_seconds: int | None = None,
    ) -> None:
        self.ttl_seconds = max(3, int(ttl_seconds))
        self.wait_seconds = max(0, int(wait_seconds))
        self.renew_interval_seconds = (
            max(1, int(renew_interval_seconds))
            if renew_interval_seconds is not None
            else max(1, self.ttl_seconds // 3)
        )
        self.lock = redis_client.lock(
            name,
            timeout=self.ttl_seconds,
            blocking_timeout=self.wait_seconds,
        )
        self._renew_task: asyncio.Task | None = None
        self._lost = False
        self._stopping = False

    async def acquire(self) -> bool:
        acquired = await self.lock.acquire()
        if acquired:
            self._renew_task = asyncio.create_task(self._renew_loop())
        return bool(acquired)

    async def _renew_loop(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(self.renew_interval_seconds)
                if self._stopping:
                    break
                try:
                    await self.lock.extend(
                        self.ttl_seconds,
                        replace_ttl=True,
                    )
                except Exception:
                    self._lost = True
                    logger.error("cache_lock_renewal_failed", exc_info=True)
                    break
        except asyncio.CancelledError:
            raise

    def ensure_alive(self) -> None:
        if self._lost:
            raise PixivUnavailableError(
                "Cache lock ownership was lost during processing; refusing to commit"
            )

    async def release(self) -> None:
        self._stopping = True
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
            self._renew_task = None
        if self._lost:
            return
        try:
            await self.lock.release()
        except Exception:
            logger.warning("cache_lock_release_failed", exc_info=True)
