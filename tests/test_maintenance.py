from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError
from app.core.maintenance import MaintenanceFreeze, require_writes_allowed


class FakeLock:
    def __init__(self):
        self.released = False

    async def acquire(self):
        return True

    async def extend(self, ttl, replace_ttl=False):
        return True

    async def release(self):
        self.released = True


class FakeRedis:
    def __init__(self, *, maintenance=False, active_writer=False):
        self.maintenance = maintenance
        self.active_writer = active_writer
        self.underlying = FakeLock()

    def lock(self, *args, **kwargs):
        return self.underlying

    async def exists(self, key):
        return 1 if self.maintenance else 0

    async def scan_iter(self, match=None, count=None):
        if self.active_writer:
            yield "pixiv-cache:illust:1"


@pytest.mark.asyncio
async def test_write_gate_rejects_during_maintenance():
    with pytest.raises(ArchiveMaintenanceError):
        await require_writes_allowed(FakeRedis(maintenance=True))


@pytest.mark.asyncio
async def test_freeze_releases_lock_if_writer_drain_times_out():
    settings = get_settings()
    old_timeout = settings.maintenance_wait_for_writers_seconds
    settings.maintenance_wait_for_writers_seconds = 0
    redis = FakeRedis(active_writer=True)
    try:
        freeze = MaintenanceFreeze(redis)
        with pytest.raises(ArchiveMaintenanceError, match="Timed out"):
            await freeze.__aenter__()
        assert redis.underlying.released is True
    finally:
        settings.maintenance_wait_for_writers_seconds = old_timeout
