from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from redis.exceptions import ResponseError

from app.core.config import get_settings


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyncJobQueue:
    def __init__(self, redis_client) -> None:
        self.redis = redis_client
        self.settings = get_settings()
        self.stream = self.settings.sync_job_stream
        self.group = self.settings.sync_job_group
        self.status_prefix = self.settings.sync_job_status_prefix
        self.dedupe_prefix = self.settings.sync_job_dedupe_prefix
        self._group_ready = False

    async def ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self.redis.xgroup_create(
                self.stream, self.group, id="0-0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    def status_key(self, job_id: str) -> str:
        return f"{self.status_prefix}{job_id}"

    def dedupe_key(self, source_id: int) -> str:
        return f"{self.dedupe_prefix}{source_id}"

    async def enqueue(self, source_id: int, *, full: bool = False) -> dict[str, Any]:
        dedupe = self.dedupe_key(source_id)
        existing_id = await self.redis.get(dedupe)
        if existing_id:
            existing = await self.get(existing_id)
            if existing and existing["status"] in {"queued", "running"}:
                if full and not existing.get("full"):
                    await self.redis.hset(self.status_key(existing_id), "full", "1")
                    existing["full"] = True
                existing["deduplicated"] = True
                return existing
            await self.redis.delete(dedupe)

        job_id = uuid.uuid4().hex
        claimed = await self.redis.set(
            dedupe,
            job_id,
            ex=self.settings.sync_job_dedupe_ttl_seconds,
            nx=True,
        )
        if not claimed:
            for _ in range(5):
                existing_id = await self.redis.get(dedupe)
                if existing_id:
                    existing = await self.get(existing_id)
                    if existing:
                        existing["deduplicated"] = True
                        return existing
                await asyncio.sleep(0.01)
            raise RuntimeError("Sync job deduplication race could not be resolved")

        mapping = {
            "job_id": job_id,
            "status": "queued",
            "source_id": str(source_id),
            "full": "1" if full else "0",
            "attempts": "0",
            "created_at": utc_iso(),
            "started_at": "",
            "finished_at": "",
            "error_type": "",
            "error": "",
            "result": "",
        }
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.hset(self.status_key(job_id), mapping=mapping)
                pipe.expire(
                    self.status_key(job_id),
                    self.settings.sync_job_status_ttl_seconds,
                )
                pipe.xadd(self.stream, {"job_id": job_id})
                await pipe.execute()
        except Exception:
            await self.redis.delete(dedupe, self.status_key(job_id))
            raise
        return self._decode(mapping)

    async def get(self, job_id: str) -> dict[str, Any] | None:
        raw = await self.redis.hgetall(self.status_key(job_id))
        return self._decode(raw) if raw else None

    async def retry(self, job_id: str) -> dict[str, Any] | None:
        previous = await self.get(job_id)
        if not previous:
            return None
        if previous["status"] in {"queued", "running"}:
            previous["deduplicated"] = True
            return previous
        return await self.enqueue(
            previous["source_id"],
            full=bool(previous.get("full")),
        )

    async def mark_running(self, job_id: str, consumer: str) -> dict[str, Any] | None:
        key = self.status_key(job_id)
        if not await self.redis.exists(key):
            return None
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "attempts", 1)
            pipe.hset(
                key,
                mapping={
                    "status": "running",
                    "started_at": utc_iso(),
                    "consumer": consumer,
                    "error_type": "",
                    "error": "",
                },
            )
            pipe.expire(key, self.settings.sync_job_status_ttl_seconds)
            await pipe.execute()
        return await self.get(job_id)

    async def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        await self.redis.hset(
            self.status_key(job_id),
            mapping={
                "status": "succeeded",
                "finished_at": utc_iso(),
                "result": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                "error_type": "",
                "error": "",
            },
        )
        await self._finish(job_id)

    async def mark_failed(self, job_id: str, exc: Exception) -> None:
        await self.redis.hset(
            self.status_key(job_id),
            mapping={
                "status": "failed",
                "finished_at": utc_iso(),
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
                "result": "",
            },
        )
        await self._finish(job_id)

    async def _finish(self, job_id: str) -> None:
        job = await self.get(job_id)
        if job:
            key = self.dedupe_key(job["source_id"])
            current = await self.redis.get(key)
            if current == job_id:
                await self.redis.delete(key)
        await self.redis.expire(
            self.status_key(job_id),
            self.settings.sync_job_status_ttl_seconds,
        )

    async def read_one(self, consumer: str, *, block_ms: int = 5000):
        await self.ensure_group()
        pending = await self.redis.xpending_range(
            self.stream,
            self.group,
            min="-",
            max="+",
            count=1,
            idle=self.settings.sync_job_claim_idle_ms,
        )
        if pending:
            message_id = pending[0]["message_id"]
            claimed = await self.redis.xclaim(
                self.stream,
                self.group,
                consumer,
                min_idle_time=self.settings.sync_job_claim_idle_ms,
                message_ids=[message_id],
            )
            if claimed:
                stream_id, fields = claimed[0]
                if fields.get("job_id"):
                    return stream_id, fields["job_id"]

        rows = await self.redis.xreadgroup(
            self.group,
            consumer,
            streams={self.stream: ">"},
            count=1,
            block=block_ms,
        )
        if not rows:
            return None
        _, messages = rows[0]
        if not messages:
            return None
        stream_id, fields = messages[0]
        job_id = fields.get("job_id")
        return (stream_id, job_id) if job_id else None

    async def touch(self, stream_id: str, consumer: str) -> None:
        await self.redis.xclaim(
            self.stream,
            self.group,
            consumer,
            min_idle_time=0,
            message_ids=[stream_id],
            justid=True,
        )

    async def ack(self, stream_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(self.stream, self.group, stream_id)
            pipe.xdel(self.stream, stream_id)
            await pipe.execute()

    @staticmethod
    def _decode(raw: dict[str, Any]) -> dict[str, Any]:
        value = dict(raw)
        for key in ("source_id", "attempts"):
            if key in value and value[key] != "":
                value[key] = int(value[key])
        if "full" in value:
            value["full"] = value["full"] in (True, 1, "1", "true", "True")
        result = value.get("result")
        if result:
            try:
                value["result"] = json.loads(result)
            except (TypeError, ValueError):
                pass
        value.pop("consumer", None)
        value.setdefault("deduplicated", False)
        return value
