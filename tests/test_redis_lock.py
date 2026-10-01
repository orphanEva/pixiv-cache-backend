from __future__ import annotations

import asyncio

import pytest

from app.core.errors import PixivUnavailableError
from app.core.redis_lock import RenewingRedisLock


class FakeLock:
    def __init__(self, fail_extend=False):
        self.fail_extend = fail_extend
        self.extends = 0
        self.released = False

    async def acquire(self):
        return True

    async def extend(self, ttl, replace_ttl=False):
        self.extends += 1
        if self.fail_extend:
            raise RuntimeError("lost")
        return True

    async def release(self):
        self.released = True


class FakeRedis:
    def __init__(self, lock):
        self.fake_lock = lock

    def lock(self, *args, **kwargs):
        return self.fake_lock


@pytest.mark.asyncio
async def test_renewing_lock_extends_and_releases():
    underlying = FakeLock()
    guard = RenewingRedisLock(
        FakeRedis(underlying),
        "key",
        ttl_seconds=3,
        wait_seconds=1,
        renew_interval_seconds=1,
    )
    assert await guard.acquire() is True
    await asyncio.sleep(1.1)
    guard.ensure_alive()
    assert underlying.extends >= 1
    await guard.release()
    assert underlying.released is True


@pytest.mark.asyncio
async def test_renewal_failure_marks_lock_lost_and_skips_release():
    underlying = FakeLock(fail_extend=True)
    guard = RenewingRedisLock(
        FakeRedis(underlying),
        "key",
        ttl_seconds=3,
        wait_seconds=1,
        renew_interval_seconds=1,
    )
    assert await guard.acquire() is True
    await asyncio.sleep(1.1)
    with pytest.raises(PixivUnavailableError, match="ownership was lost"):
        guard.ensure_alive()
    await guard.release()
    assert underlying.released is False
