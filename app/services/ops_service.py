from __future__ import annotations

import shutil
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.core.maintenance import maintenance_active
from app.core.redis_client import get_redis
from app.core.schema_version import EXPECTED_SCHEMA_VERSION, read_schema_version
from app.db.session import SessionLocal, engine
from app.services.archive_jobs import ArchiveJobQueue
from app.services.sync_jobs import SyncJobQueue


class OpsService:
    """汇总运行状态、worker heartbeat 和 Redis 队列指标。"""

    def __init__(self) -> None:
        """初始化运维服务使用的 Redis 和配置对象。"""
        self.settings = get_settings()
        self.redis = get_redis()

    async def status(self) -> dict[str, Any]:
        """返回 API/数据库/Redis/磁盘/worker/队列的一站式运行状态。"""
        redis_ok = False
        mysql_ok = False
        schema_actual = None
        try:
            redis_ok = bool(await self.redis.ping())
        except Exception:
            redis_ok = False

        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            mysql_ok = True
            schema_actual = await read_schema_version(engine)
        except Exception:
            mysql_ok = False

        storage_root = self.settings.storage_root
        storage_root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(storage_root)

        archive_stats = {}
        sync_stats = {}
        if redis_ok:
            archive_stats = await ArchiveJobQueue(self.redis).queue_stats()
            sync_stats = await self._sync_queue_stats()

        workers = {}
        if redis_ok:
            workers = {
                "archive": await self._heartbeat(
                    self.settings.archive_worker_heartbeat_key
                ),
                "sync": await self._heartbeat(
                    self.settings.sync_worker_heartbeat_key
                ),
                "maintenance": await self._heartbeat(
                    self.settings.integrity_worker_heartbeat_key
                ),
            }

        return {
            "status": (
                "ok"
                if redis_ok
                and mysql_ok
                and schema_actual == EXPECTED_SCHEMA_VERSION
                else "degraded"
            ),
            "checks": {
                "redis": redis_ok,
                "mysql": mysql_ok,
                "schema": schema_actual == EXPECTED_SCHEMA_VERSION,
            },
            "schema": {
                "expected": EXPECTED_SCHEMA_VERSION,
                "actual": schema_actual,
            },
            "maintenance": {
                "write_freeze": (
                    await maintenance_active(self.redis) if redis_ok else None
                )
            },
            "workers": workers,
            "queues": {
                "archive": archive_stats,
                "sync": sync_stats,
            },
            "storage": {
                "root": str(storage_root.resolve()),
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "minimum_free_bytes": int(self.settings.storage_min_free_bytes),
                "below_minimum_free": (
                    usage.free < self.settings.storage_min_free_bytes
                ),
            },
        }

    async def _heartbeat(self, key: str) -> dict[str, Any]:
        """读取一个 worker heartbeat 的值和剩余 TTL。"""
        value = await self.redis.get(key)
        ttl = await self.redis.ttl(key)
        return {
            "healthy": bool(value and ttl > 0),
            "identity": value,
            "ttl_seconds": max(-1, int(ttl)),
        }

    async def _sync_queue_stats(self) -> dict[str, Any]:
        """返回同步发现队列的 Stream 长度和 pending 数量。"""
        queue = SyncJobQueue(self.redis)
        await queue.ensure_group()
        pending_info = await self.redis.xpending(queue.stream, queue.group)
        if isinstance(pending_info, dict):
            pending = int(pending_info.get("pending", 0))
        else:
            try:
                pending = int(pending_info[0])
            except (TypeError, IndexError, ValueError):
                pending = 0
        return {
            "stream_length": int(await self.redis.xlen(queue.stream)),
            "pending": pending,
        }
