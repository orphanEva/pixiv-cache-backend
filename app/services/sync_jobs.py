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
        """Atomically create a task or preserve a full rescan requested mid-run."""
        dedupe = self.dedupe_key(source_id)
        job_id = uuid.uuid4().hex
        script = """
local existing = redis.call('GET', KEYS[1])
if existing then
  local key = ARGV[7] .. existing
  local status = redis.call('HGET', key, 'status')
  if status == 'queued' or status == 'running' then
    if ARGV[4] == '1' then
      if status == 'queued' then
        redis.call('HSET', key, 'full', '1')
      else
        redis.call('HSET', key, 'rerun_full', '1')
      end
    end
    return {existing, '1'}
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[6])
redis.call('HSET', KEYS[2], 'job_id', ARGV[1], 'status', 'queued',
  'source_id', ARGV[3], 'full', ARGV[4], 'rerun_full', '0', 'restart_scan', '0', 'attempts', '0',
  'created_at', ARGV[2], 'started_at', '', 'finished_at', '',
  'error_type', '', 'error', '', 'result', '')
redis.call('EXPIRE', KEYS[2], ARGV[5])
redis.call('XADD', KEYS[3], '*', 'job_id', ARGV[1])
return {ARGV[1], '0'}
"""
        result_id, deduplicated = await self.redis.eval(
            script, 3, dedupe, self.status_key(job_id), self.stream,
            job_id, utc_iso(), str(source_id), "1" if full else "0",
            self.settings.sync_job_status_ttl_seconds,
            self.settings.sync_job_dedupe_ttl_seconds, self.status_prefix,
        )
        result = await self.get(result_id)
        if result is None:
            raise RuntimeError("Sync job status disappeared immediately after enqueue")
        result["deduplicated"] = str(deduplicated) == "1"
        return result

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
        script = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'queued' and status ~= 'running' then return 0 end
redis.call('HINCRBY', KEYS[1], 'attempts', 1)
redis.call('HSET', KEYS[1], 'status', 'running', 'started_at', ARGV[1],
  'consumer', ARGV[2], 'error_type', '', 'error', '', 'finished_at', '')
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""
        claimed = await self.redis.eval(script, 1, key, utc_iso(), consumer,
                                        self.settings.sync_job_status_ttl_seconds)
        if not claimed:
            return None
        return await self.get(job_id)

    async def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> bool:
        return await self._finish(job_id, status="succeeded", result=result)

    async def clear_restart_scan(self, job_id: str) -> None:
        """Consume the restart flag after the old database checkpoint was cleared."""
        await self.redis.hset(self.status_key(job_id), "restart_scan", "0")

    async def mark_failed(self, job_id: str, exc: Exception) -> bool:
        return await self._finish(job_id, status="failed", exc=exc)

    async def _finish(self, job_id: str, *, status: str, result=None, exc=None) -> bool:
        """Finish atomically, or queue the full scan requested during this run."""
        job = await self.get(job_id)
        if not job:
            return False
        script = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('HGET', KEYS[1], 'rerun_full') == '1' then
  redis.call('HSET', KEYS[1], 'status', 'queued', 'full', '1', 'rerun_full', '0', 'restart_scan', '1',
    'started_at', '', 'finished_at', '', 'error_type', '', 'error', '', 'result', '')
  redis.call('EXPIRE', KEYS[1], ARGV[7])
  redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[8])
  redis.call('XADD', KEYS[3], '*', 'job_id', ARGV[1])
  return 1
end
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'finished_at', ARGV[3],
  'result', ARGV[4], 'error_type', ARGV[5], 'error', ARGV[6])
redis.call('EXPIRE', KEYS[1], ARGV[7])
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
return 0
"""
        rescheduled = await self.redis.eval(
            script, 3, self.status_key(job_id), self.dedupe_key(job["source_id"]), self.stream,
            job_id, status, utc_iso(),
            json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result is not None else "",
            exc.__class__.__name__ if exc else "", str(exc)[:1000] if exc else "",
            self.settings.sync_job_status_ttl_seconds, self.settings.sync_job_dedupe_ttl_seconds,
        )
        return bool(rescheduled)

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
        if "rerun_full" in value:
            value["rerun_full"] = value["rerun_full"] in (True, 1, "1", "true", "True")
        if "restart_scan" in value:
            value["restart_scan"] = value["restart_scan"] in (True, 1, "1", "true", "True")
        result = value.get("result")
        if result:
            try:
                value["result"] = json.loads(result)
            except (TypeError, ValueError):
                pass
        value.pop("consumer", None)
        value.setdefault("deduplicated", False)
        return value
