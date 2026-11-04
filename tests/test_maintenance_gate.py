from __future__ import annotations

import asyncio
import contextvars

import fakeredis.aioredis
import pytest

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError
from app.core.maintenance import (
    MaintenanceFreeze, MaintenanceWriter, get_current_writer, maintenance_active,
    recovery_key, require_writer_alive, writers_key,
)


@pytest.fixture
async def redis(monkeypatch):
    monkeypatch.setattr(get_settings(), "maintenance_wait_for_writers_seconds", 5)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_writer_scope_is_reentrant_and_unregisters_once(redis):
    await require_writer_alive()  # Read-only callers do not need a lease.
    async with MaintenanceWriter(redis) as outer:
        assert get_current_writer() is outer
        assert await redis.zcard(writers_key()) == 1
        async with MaintenanceWriter(redis) as inner:
            assert inner is outer
            await require_writer_alive()
            inner.check_alive()
            assert await redis.zcard(writers_key()) == 1
        await outer.ensure_alive()
    assert get_current_writer() is None
    assert await redis.zcard(writers_key()) == 0


@pytest.mark.asyncio
async def test_freeze_rejects_new_writers_and_recovery_also_blocks(redis):
    async with MaintenanceFreeze(redis):
        with pytest.raises(ArchiveMaintenanceError):
            async with MaintenanceWriter(redis):
                pytest.fail("a new writer entered during freeze")
    await redis.set(recovery_key(), "pending recovery")
    assert await maintenance_active(redis)
    with pytest.raises(ArchiveMaintenanceError):
        async with MaintenanceWriter(redis):
            pytest.fail("a writer entered during recovery")


@pytest.mark.asyncio
async def test_freeze_drains_registered_writer_before_entering(redis):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def freeze_operation():
        async with MaintenanceFreeze(redis):
            entered.set()
            await release.wait()

    async with MaintenanceWriter(redis):
        task = asyncio.create_task(freeze_operation(), context=contextvars.Context())
        for _ in range(100):
            if await redis.exists(get_settings().maintenance_freeze_key):
                break
            await asyncio.sleep(0.001)
        assert not entered.is_set()
    await asyncio.wait_for(entered.wait(), 3)
    release.set()
    await task


@pytest.mark.asyncio
async def test_lost_writer_and_freeze_ownership_are_detected(redis):
    async with MaintenanceWriter(redis) as writer:
        await redis.zrem(writers_key(), writer.token)
        with pytest.raises(ArchiveMaintenanceError):
            await writer.ensure_alive()
        with pytest.raises(ArchiveMaintenanceError):
            writer.check_alive()
    async with MaintenanceFreeze(redis) as freeze:
        await redis.delete(get_settings().maintenance_freeze_key)
        with pytest.raises(ArchiveMaintenanceError):
            await freeze.ensure_alive()


@pytest.mark.asyncio
async def test_cancel_during_writer_drain_releases_freeze(redis):
    async with MaintenanceWriter(redis) as writer:
        freeze = MaintenanceFreeze(redis)
        task = asyncio.create_task(freeze.__aenter__(), context=contextvars.Context())
        for _ in range(100):
            if await redis.exists(get_settings().maintenance_freeze_key):
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await redis.exists(get_settings().maintenance_freeze_key)
        await writer.ensure_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_owner", [False, True])
@pytest.mark.parametrize("error_type", [ConnectionError, asyncio.CancelledError])
async def test_uncertain_freeze_acquire_cleans_only_its_explicit_token(redis, monkeypatch, same_owner, error_type):
    freeze = MaintenanceFreeze(redis)
    key = get_settings().maintenance_freeze_key

    async def uncertain_acquire(*, token):
        assert token == freeze.token
        await redis.set(key, token if same_owner else "another-owner", ex=60)
        raise error_type("acquire response lost")

    monkeypatch.setattr(freeze.lock.lock, "acquire", uncertain_acquire)
    with pytest.raises(error_type):
        await freeze.__aenter__()
    assert await redis.get(key) == (None if same_owner else "another-owner")
