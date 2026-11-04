"""Persistent local-only derivative jobs with their own Redis namespace."""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from app.core.config import get_settings
from app.models.work import WorkType
from app.services.archive_jobs import ArchiveJobQueue, ACTIVE_STATUSES, utc_iso


class MediaJobQueue(ArchiveJobQueue):
    """Reuse Stream recovery and status querying, never the archive queue keys."""

    def __init__(self, redis_client):
        super().__init__(redis_client)
        settings = get_settings()
        prefix = settings.media_job_prefix.rstrip(":")
        self.stream = f"{prefix}:jobs"
        self.group = f"{prefix}:workers"
        self.status_prefix = f"{prefix}:job:"
        self.dedupe_prefix = f"{prefix}:dedupe:"
        self.retry_zset = f"{prefix}:retry"
        self.index_zset = f"{prefix}:index"
        self.status_index_prefix = f"{prefix}:status:"
        self.dead_stream = f"{prefix}:dead"
        # The inherited queue methods consume these generic settings by their
        # original names. All mutable keys above and limits here are independent.
        self.settings = SimpleNamespace(
            archive_job_status_ttl_seconds=settings.media_job_status_ttl_seconds,
            archive_job_dedupe_ttl_seconds=settings.media_job_status_ttl_seconds,
            archive_job_claim_idle_ms=settings.media_job_claim_idle_ms,
            archive_job_retry_base_seconds=settings.media_job_retry_base_seconds,
            archive_job_retry_max_seconds=settings.media_job_retry_max_seconds,
            archive_job_dead_maxlen=10000,
        )

    def dedupe_key(self, kind, pixiv_id: int, version_no: int = 0) -> str:
        return f"{self.dedupe_prefix}ugoira_mp4:{int(pixiv_id)}:{int(version_no)}"

    async def enqueue_preview(self, pixiv_id: int, version_no: int) -> dict:
        if pixiv_id <= 0 or version_no <= 0:
            raise ValueError("Work ID and version must be positive")
        job_id = uuid.uuid4().hex
        created = time.time()
        mapping = {
            "job_id": job_id, "job_type": "ugoira_mp4", "status": "queued",
            "kind": "illust", "pixiv_id": str(pixiv_id), "version_no": str(version_no),
            "attempts": "0", "created_at": utc_iso(), "created_ts": repr(created),
            "started_at": "", "finished_at": "", "retry_at": "",
            "error_type": "", "error": "", "dead_reason": "", "result": "",
            "phase": "queued",
        }
        script = """
local existing = redis.call('GET', KEYS[1])
if existing then
  local status = redis.call('HGET', ARGV[3] .. existing, 'status')
  if status == 'queued' or status == 'running' or status == 'finalizing' or status == 'retry_wait' then
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return {existing, 1}
  end
end
local fields = cjson.decode(ARGV[2])
for name, value in pairs(fields) do redis.call('HSET', KEYS[2], name, value) end
redis.call('EXPIRE', KEYS[2], ARGV[4])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
redis.call('ZADD', KEYS[3], ARGV[5], ARGV[1])
redis.call('ZADD', KEYS[4], ARGV[5], ARGV[1])
redis.call('XADD', KEYS[5], '*', 'job_id', ARGV[1])
return {ARGV[1], 0}
"""
        accepted, deduplicated = await self.redis.eval(
            script, 5, self.dedupe_key(WorkType.ILLUST, pixiv_id, version_no),
            self.status_key(job_id), self.index_zset, self.status_index_key("queued"),
            self.stream, job_id, json.dumps(mapping), self.status_prefix,
            self.settings.archive_job_status_ttl_seconds, repr(created),
        )
        job = await self.get(accepted)
        if job is None:
            raise RuntimeError("Published media job status is missing")
        job["deduplicated"] = bool(deduplicated)
        return job

    async def replay(self, job_id: str) -> dict | None:
        job = await self.get(job_id)
        if job is None:
            return None
        if job["status"] in ACTIVE_STATUSES:
            job["deduplicated"] = True
            return job
        return await self.enqueue_preview(job["pixiv_id"], job["version_no"])

    async def set_phase(self, job_id: str, phase: str) -> None:
        if phase not in {"validating", "converting", "registering", "complete"}:
            raise ValueError("Unknown media job phase")
        await self.redis.hset(self.status_key(job_id), mapping={"phase": phase})

    async def _finish(self, job_id: str, *, status: str) -> None:
        job = await self.get(job_id)
        if job:
            await self._delete_dedupe_if_owned(
                self.dedupe_key(WorkType.ILLUST, job["pixiv_id"], job["version_no"]), job_id,
            )
        # Parent also checks its default (version 0) key; that key is never
        # created here. Keep the tested TTL/status-index bookkeeping shared.
        await super()._finish(job_id, status=status)

    async def schedule_retry(self, stream_id, job_id, exc, *, delay_seconds):
        await super().schedule_retry(stream_id, job_id, exc, delay_seconds=delay_seconds)
        job = await self.get(job_id)
        if job:
            key = self.dedupe_key(WorkType.ILLUST, job["pixiv_id"], job["version_no"])
            await self.redis.eval("""
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 0
""", 1, key, job_id, self.settings.archive_job_status_ttl_seconds)

    async def get(self, job_id):
        job = await super().get(job_id)
        if job:
            for key in ("source_ids", "force_refresh", "bookmark_if_missing"):
                job.pop(key, None)
        return job

    @staticmethod
    def _decode(raw):
        value = ArchiveJobQueue._decode(raw)
        if value.get("version_no"):
            value["version_no"] = int(value["version_no"])
        return value
