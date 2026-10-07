from __future__ import annotations

import asyncio
import time

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError
from app.core.redis_lock import RenewingRedisLock


async def maintenance_active(redis_client) -> bool:
    return bool(await redis_client.exists(get_settings().maintenance_freeze_key))


def ensure_not_in_maintenance_sync_message() -> ArchiveMaintenanceError:
    return ArchiveMaintenanceError(
        "Archive writes are temporarily frozen for maintenance/backup"
    )


async def require_writes_allowed(redis_client) -> None:
    if await maintenance_active(redis_client):
        raise ensure_not_in_maintenance_sync_message()


class MaintenanceFreeze:
    """Global write freeze backed by a renewable Redis lock."""

    def __init__(self, redis_client) -> None:
        self.redis = redis_client
        self.settings = get_settings()
        self.lock = RenewingRedisLock(
            redis_client,
            self.settings.maintenance_freeze_key,
            ttl_seconds=self.settings.maintenance_freeze_ttl_seconds,
            wait_seconds=self.settings.maintenance_freeze_wait_seconds,
        )

    async def __aenter__(self):
        if not await self.lock.acquire():
            raise ArchiveMaintenanceError(
                "Could not acquire archive maintenance freeze"
            )
        await self.wait_for_active_writers()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.lock.release()

    async def wait_for_active_writers(self) -> None:
        deadline = time.monotonic() + self.settings.maintenance_wait_for_writers_seconds
        pattern = f"{self.settings.cache_lock_prefix}*"
        while True:
            active = []
            async for key in self.redis.scan_iter(match=pattern, count=200):
                active.append(key)
                if len(active) >= 10:
                    break
            if not active:
                return
            if time.monotonic() >= deadline:
                raise ArchiveMaintenanceError(
                    "Timed out waiting for active archive writers to finish"
                )
            await asyncio.sleep(1)
