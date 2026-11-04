from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import time
import uuid

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError
from app.core.redis_lock import RenewingRedisLock

logger = logging.getLogger(__name__)
_writer: ContextVar[MaintenanceWriter | None] = ContextVar("maintenance_writer", default=None)


def recovery_key() -> str:
    return f"{get_settings().maintenance_freeze_key}:recovery"


def writers_key() -> str:
    return f"{get_settings().maintenance_freeze_key}:writers"


async def maintenance_active(redis_client) -> bool:
    return bool(await redis_client.exists(get_settings().maintenance_freeze_key)
                or await redis_client.exists(recovery_key()))


def ensure_not_in_maintenance_sync_message() -> ArchiveMaintenanceError:
    return ArchiveMaintenanceError(
        "Archive writes are temporarily frozen for maintenance/backup"
    )


async def require_writes_allowed(redis_client) -> None:
    if await maintenance_active(redis_client):
        raise ensure_not_in_maintenance_sync_message()


def get_current_writer() -> MaintenanceWriter | None:
    current = _writer.get()
    return current if current is not None and not current._closed else None


async def require_writer_alive() -> None:
    current = get_current_writer()
    if current is not None:
        await current.ensure_alive()


class MaintenanceWriter:
    """An atomic writer registration, renewable until its outermost scope exits.

    Freeze acquisition and registration serialize in Redis: a writer is either
    visible in the drain set, or rejected before it starts. Nested scopes reuse
    the current writer. The lease must be checked before commits/publication.
    """

    def __init__(self, redis_client) -> None:
        self.redis = redis_client
        self.settings = get_settings()
        self.ttl = max(3, int(getattr(self.settings, "maintenance_writer_ttl_seconds", 180)))
        self.token = uuid.uuid4().hex
        self._task = None
        self._context_token = None
        self._parent = None
        self._lost = False
        self._closed = True
        self._deadline = 0.0

    async def __aenter__(self):
        parent = get_current_writer()
        if parent is not None and parent.redis is self.redis:
            await parent.ensure_alive()
            self._parent = parent
            return parent
        script = """
if redis.call('EXISTS', KEYS[1], KEYS[2]) ~= 0 then return 0 end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZADD', KEYS[3], now + tonumber(ARGV[2]), ARGV[1])
return 1
"""
        started = time.monotonic()
        try:
            if not await self.redis.eval(script, 3, self.settings.maintenance_freeze_key,
                                         recovery_key(), writers_key(), self.token, self.ttl):
                raise ensure_not_in_maintenance_sync_message()
        except BaseException:
            # Registration may have succeeded before a response was lost.
            try:
                await asyncio.shield(self.redis.zrem(writers_key(), self.token))
            except BaseException:
                logger.warning("maintenance_writer_registration_cleanup_failed")
            raise
        self._closed = False
        self._deadline = started + self.ttl
        self._context_token = _writer.set(self)
        self._task = asyncio.create_task(self._renew_loop())
        return self

    def check_alive(self) -> None:
        """Synchronous local check for an immediate filesystem publication."""
        if self._closed or self._lost or time.monotonic() >= self._deadline:
            self._lost = True
            raise ArchiveMaintenanceError("Maintenance writer lease was lost; refusing to write")

    async def ensure_alive(self) -> None:
        self.check_alive()
        script = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if score and tonumber(score) > now then return 1 end
return 0
"""
        try:
            if not await self.redis.eval(script, 1, writers_key(), self.token):
                raise ArchiveMaintenanceError("Maintenance writer lease expired")
        except BaseException:
            self._lost = True
            raise

    async def _renew_loop(self) -> None:
        script = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
return 1
"""
        try:
            while True:
                await asyncio.sleep(max(1, self.ttl // 3))
                started = time.monotonic()
                if not await self.redis.eval(script, 1, writers_key(), self.token, self.ttl):
                    self._lost = True
                    return
                self._deadline = started + self.ttl
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost = True
            logger.exception("maintenance_writer_renewal_failed")

    async def __aexit__(self, exc_type, exc, tb):
        if self._parent is not None:
            return
        self._closed = True
        if self._context_token is not None:
            _writer.reset(self._context_token)
            self._context_token = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.shield(self.redis.zrem(writers_key(), self.token))
        except Exception:
            # The stale lease expires automatically; never bypass a freeze by
            # pretending an uncertain unregister succeeded.
            logger.exception("maintenance_writer_unregister_failed")


class MaintenanceFreeze:
    """Global write freeze backed by a renewable Redis lock."""

    def __init__(self, redis_client, *, allow_recovery: bool = False) -> None:
        self.redis = redis_client
        self.settings = get_settings()
        self.allow_recovery = allow_recovery
        self.token = uuid.uuid4().hex
        self.lock = RenewingRedisLock(
            redis_client,
            self.settings.maintenance_freeze_key,
            ttl_seconds=self.settings.maintenance_freeze_ttl_seconds,
            wait_seconds=self.settings.maintenance_freeze_wait_seconds,
        )

    async def __aenter__(self):
        if not self.allow_recovery and await self.redis.exists(recovery_key()):
            raise ArchiveMaintenanceError("Restore recovery is pending; inspect restore-state.json before releasing maintenance")
        try:
            acquired = await self.lock.acquire(token=self.token)
        except BaseException:
            # SET may have succeeded even when its response was cancelled/lost.
            # An explicit token permits cleanup without deleting another owner.
            script = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
            try:
                await asyncio.shield(self.redis.eval(
                    script, 1, self.settings.maintenance_freeze_key, self.token,
                ))
            except BaseException:
                logger.warning("maintenance_freeze_acquire_cleanup_failed")
            raise
        if not acquired:
            raise ArchiveMaintenanceError(
                "Could not acquire archive maintenance freeze"
            )
        try:
            if not self.allow_recovery and await self.redis.exists(recovery_key()):
                raise ArchiveMaintenanceError("Restore recovery is pending")
            await self.wait_for_active_writers()
            await self.ensure_alive()
            return self
        except BaseException:
            await self.lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        await self.lock.release()

    async def ensure_alive(self) -> None:
        self.lock.ensure_alive()
        if not await self.lock.lock.owned():
            raise ArchiveMaintenanceError("Maintenance freeze ownership was lost")

    async def wait_for_active_writers(self) -> None:
        deadline = time.monotonic() + self.settings.maintenance_wait_for_writers_seconds
        pattern = f"{self.settings.cache_lock_prefix}*"
        script = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""
        while True:
            await self.ensure_alive()
            registered = int(await self.redis.eval(script, 1, writers_key()))
            active = []
            # Compatibility with cache writers from the preceding release.
            # New writers are covered by the atomic registration above.
            async for key in self.redis.scan_iter(match=pattern, count=200):
                active.append(key)
                if len(active) >= 10:
                    break
            if not registered and not active:
                return
            if time.monotonic() >= deadline:
                raise ArchiveMaintenanceError(
                    "Timed out waiting for active archive writers to finish"
                )
            await asyncio.sleep(1)
