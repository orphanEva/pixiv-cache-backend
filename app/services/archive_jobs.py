from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from redis.exceptions import ResponseError

from app.core.config import get_settings
from app.models.work import WorkType


JOB_STATUSES = (
    "queued",
    "running",
    "finalizing",
    "retry_wait",
    "succeeded",
    "failed",
    "dead",
)
SOURCE_MERGE_STATUSES = {"queued", "running", "retry_wait"}
ACTIVE_STATUSES = {"queued", "running", "finalizing", "retry_wait"}


def utc_iso() -> str:
    """返回带 UTC 时区的 ISO 时间字符串，供 Redis 任务状态记录使用。"""
    return datetime.now(timezone.utc).isoformat()


def normalize_job_kind(kind: WorkType) -> WorkType:
    """把漫画和 Ugoira 统一归入现有插画归档任务类型。"""
    return WorkType.NOVEL if kind == WorkType.NOVEL else WorkType.ILLUST


class ArchiveJobQueue:
    """Redis Stream 归档任务队列。

    负责持久任务状态、作品级去重、来源合并、指数退避重试、死信记录以及
    任务查询索引。媒体下载仍由 archive worker 执行。
    """

    def __init__(self, redis_client) -> None:
        """绑定 Redis 客户端并加载归档任务相关配置。"""
        self.redis = redis_client
        self.settings = get_settings()
        self.stream = self.settings.archive_job_stream
        self.group = self.settings.archive_job_group
        self.status_prefix = self.settings.archive_job_status_prefix
        self.dedupe_prefix = self.settings.archive_job_dedupe_prefix
        self.retry_zset = self.settings.archive_job_retry_zset
        self.index_zset = self.settings.archive_job_index_zset
        self.status_index_prefix = self.settings.archive_job_status_index_prefix
        self.dead_stream = self.settings.archive_job_dead_stream
        self._group_ready = False

    async def ensure_group(self) -> None:
        """确保 Redis Stream consumer group 已存在。"""
        if self._group_ready:
            return
        try:
            await self.redis.xgroup_create(
                self.stream,
                self.group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    def status_key(self, job_id: str) -> str:
        """返回任务状态 Hash 的 Redis key。"""
        return f"{self.status_prefix}{job_id}"

    def source_key(self, job_id: str) -> str:
        """返回任务来源 ID 集合的 Redis key。"""
        return f"{self.status_key(job_id)}:sources"

    def dedupe_key(self, kind: WorkType, pixiv_id: int) -> str:
        """返回作品级去重 key。"""
        normalized = normalize_job_kind(kind)
        return f"{self.dedupe_prefix}{normalized.value}:{pixiv_id}"

    def status_index_key(self, status: str) -> str:
        """返回指定任务状态对应的有序索引 key。"""
        return f"{self.status_index_prefix}{status}"

    @staticmethod
    def _source_values(
        source_id: int | None,
        source_ids: Iterable[int] | None,
    ) -> list[int]:
        """规范化并去重同步来源 ID。"""
        values = set()
        if source_id is not None:
            values.add(int(source_id))
        if source_ids:
            values.update(int(value) for value in source_ids)
        return sorted(value for value in values if value > 0)

    async def _attach_sources_if_active(
        self,
        job_id: str,
        source_ids: Iterable[int],
    ) -> bool:
        """仅在任务仍可合并时原子追加来源 ID。

        finalizing 之后不再允许并入旧任务，避免来源在最终落库边界丢失。
        """
        values = list(source_ids)
        script = f"""
local status = redis.call('HGET', KEYS[1], 'status')
if not status or not ({' or '.join([f"status == '{item}'" for item in sorted(SOURCE_MERGE_STATUSES)])}) then
  return 0
end
for i = 1, #ARGV - 1 do
  redis.call('SADD', KEYS[2], ARGV[i])
end
redis.call('EXPIRE', KEYS[2], ARGV[#ARGV])
return 1
"""
        result = await self.redis.eval(
            script,
            2,
            self.status_key(job_id),
            self.source_key(job_id),
            *[str(value) for value in values],
            str(self.settings.archive_job_status_ttl_seconds),
        )
        return bool(result)

    async def _delete_dedupe_if_owned(self, dedupe: str, job_id: str) -> None:
        """仅当去重 key 仍属于指定任务时删除，避免误删新任务的锁。"""
        script = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
        await self.redis.eval(script, 1, dedupe, job_id)

    async def _move_status(self, job_id: str, status: str) -> None:
        """把任务从旧状态索引移动到新状态索引。"""
        raw_score = await self.redis.hget(self.status_key(job_id), "created_ts")
        score = float(raw_score) if raw_score else time.time()
        async with self.redis.pipeline(transaction=True) as pipe:
            for candidate in JOB_STATUSES:
                pipe.zrem(self.status_index_key(candidate), job_id)
            pipe.zadd(self.status_index_key(status), {job_id: score})
            await pipe.execute()

    async def _prune_indices(self) -> None:
        """清理超过任务状态保留期的查询索引，避免索引无限增长。"""
        cutoff = time.time() - self.settings.archive_job_status_ttl_seconds
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(self.index_zset, "-inf", cutoff)
            for status in JOB_STATUSES:
                pipe.zremrangebyscore(
                    self.status_index_key(status),
                    "-inf",
                    cutoff,
                )
            await pipe.execute()

    async def enqueue(
        self,
        kind: WorkType,
        pixiv_id: int,
        *,
        force_refresh: bool = True,
        source_id: int | None = None,
        source_ids: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        """创建归档任务；同一作品已有活动任务时合并来源并返回原任务。"""
        normalized = normalize_job_kind(kind)
        dedupe = self.dedupe_key(normalized, pixiv_id)
        sources = self._source_values(source_id, source_ids)

        existing_id = await self.redis.get(dedupe)
        if existing_id:
            active = await self._attach_sources_if_active(existing_id, sources)
            if active:
                existing = await self.get(existing_id)
                if existing:
                    existing["deduplicated"] = True
                    return existing
            await self._delete_dedupe_if_owned(dedupe, existing_id)

        job_id = uuid.uuid4().hex
        claimed = await self.redis.set(
            dedupe,
            job_id,
            ex=self.settings.archive_job_dedupe_ttl_seconds,
            nx=True,
        )
        if not claimed:
            for _ in range(5):
                existing_id = await self.redis.get(dedupe)
                if existing_id:
                    active = await self._attach_sources_if_active(
                        existing_id,
                        sources,
                    )
                    if active:
                        existing = await self.get(existing_id)
                        if existing:
                            existing["deduplicated"] = True
                            return existing
                    await self._delete_dedupe_if_owned(dedupe, existing_id)
                    claimed = await self.redis.set(
                        dedupe,
                        job_id,
                        ex=self.settings.archive_job_dedupe_ttl_seconds,
                        nx=True,
                    )
                    if claimed:
                        break
                await asyncio.sleep(0.01)
            if not claimed:
                raise RuntimeError("归档任务去重竞争无法在限定次数内解决")

        now = utc_iso()
        created_ts = time.time()
        mapping = {
            "job_id": job_id,
            "status": "queued",
            "kind": normalized.value,
            "pixiv_id": str(pixiv_id),
            "force_refresh": "1" if force_refresh else "0",
            "attempts": "0",
            "created_at": now,
            "created_ts": repr(created_ts),
            "started_at": "",
            "finished_at": "",
            "retry_at": "",
            "error_type": "",
            "error": "",
            "dead_reason": "",
            "result": "",
        }
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.hset(self.status_key(job_id), mapping=mapping)
                pipe.expire(
                    self.status_key(job_id),
                    self.settings.archive_job_status_ttl_seconds,
                )
                if sources:
                    pipe.sadd(
                        self.source_key(job_id),
                        *[str(value) for value in sources],
                    )
                    pipe.expire(
                        self.source_key(job_id),
                        self.settings.archive_job_status_ttl_seconds,
                    )
                pipe.zadd(self.index_zset, {job_id: created_ts})
                pipe.zadd(self.status_index_key("queued"), {job_id: created_ts})
                pipe.xadd(self.stream, {"job_id": job_id})
                await pipe.execute()
        except Exception:
            await self.redis.delete(
                dedupe,
                self.status_key(job_id),
                self.source_key(job_id),
            )
            await self.redis.zrem(self.index_zset, job_id)
            await self.redis.zrem(self.status_index_key("queued"), job_id)
            raise

        value = self._decode(mapping)
        value["source_ids"] = sources
        return value

    async def get(self, job_id: str) -> dict[str, Any] | None:
        """读取单个归档任务及其来源 ID。"""
        raw = await self.redis.hgetall(self.status_key(job_id))
        if not raw:
            return None
        value = self._decode(raw)
        values = await self.redis.smembers(self.source_key(job_id))
        value["source_ids"] = sorted(int(item) for item in values)
        return value

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """按创建时间倒序分页查询归档任务，可按状态过滤。"""
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"不支持的归档任务状态: {status}")
        await self._prune_indices()
        index = self.status_index_key(status) if status else self.index_zset
        total = int(await self.redis.zcard(index))
        start = (page - 1) * page_size
        job_ids = await self.redis.zrevrange(index, start, start + page_size - 1)
        items = []
        stale = []
        for job_id in job_ids:
            job = await self.get(job_id)
            if job is None:
                stale.append(job_id)
                continue
            items.append(job)
        if stale:
            async with self.redis.pipeline(transaction=True) as pipe:
                for job_id in stale:
                    pipe.zrem(index, job_id)
                await pipe.execute()
            total = max(0, total - len(stale))
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": math.ceil(total / page_size) if total else 0,
            },
        }

    async def replay(self, job_id: str) -> dict[str, Any] | None:
        """把终态任务重新创建为新任务，并保留原任务来源信息。"""
        previous = await self.get(job_id)
        if not previous:
            return None
        if previous["status"] in ACTIVE_STATUSES:
            previous["deduplicated"] = True
            return previous
        return await self.enqueue(
            WorkType(previous["kind"]),
            int(previous["pixiv_id"]),
            force_refresh=bool(previous["force_refresh"]),
            source_ids=previous.get("source_ids") or [],
        )

    async def retry(self, job_id: str) -> dict[str, Any] | None:
        """兼容旧接口：手动 retry 等价于 replay。"""
        return await self.replay(job_id)

    async def mark_running(
        self,
        job_id: str,
        consumer: str,
    ) -> dict[str, Any] | None:
        """把 queued/running 任务标记为执行中并增加尝试次数。

        对 retry_wait、dead、succeeded 等状态返回 None，worker 会清理对应的
        旧 Stream 消息而不会重复执行。
        """
        key = self.status_key(job_id)
        current = await self.redis.hget(key, "status")
        if current not in {"queued", "running"}:
            return None
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "attempts", 1)
            pipe.hset(
                key,
                mapping={
                    "status": "running",
                    "started_at": utc_iso(),
                    "finished_at": "",
                    "retry_at": "",
                    "consumer": consumer,
                    "error_type": "",
                    "error": "",
                    "dead_reason": "",
                },
            )
            pipe.expire(key, self.settings.archive_job_status_ttl_seconds)
            pipe.expire(
                self.source_key(job_id),
                self.settings.archive_job_status_ttl_seconds,
            )
            await pipe.execute()
        await self._move_status(job_id, "running")
        return await self.get(job_id)

    async def mark_finalizing(self, job_id: str) -> dict[str, Any] | None:
        """进入来源关系最终落库阶段，禁止新来源继续并入当前任务。"""
        key = self.status_key(job_id)
        if await self.redis.hget(key, "status") != "running":
            return None
        await self.redis.hset(key, "status", "finalizing")
        await self._move_status(job_id, "finalizing")
        return await self.get(job_id)

    def retry_delay_seconds(self, attempts: int) -> int:
        """根据已执行次数计算指数退避时间。"""
        exponent = max(0, int(attempts) - 1)
        delay = self.settings.archive_job_retry_base_seconds * (2 ** exponent)
        return min(delay, self.settings.archive_job_retry_max_seconds)

    async def schedule_retry(
        self,
        stream_id: str,
        job_id: str,
        exc: Exception,
        *,
        delay_seconds: int,
    ) -> None:
        """把失败任务放入延迟重试集合，并原子 ACK 当前 Stream 消息。"""
        retry_epoch = time.time() + max(1, int(delay_seconds))
        retry_at = datetime.fromtimestamp(retry_epoch, timezone.utc).isoformat()
        job = await self.get(job_id)
        if job is None:
            await self.ack(stream_id)
            return
        dedupe = self.dedupe_key(
            WorkType(job["kind"]),
            int(job["pixiv_id"]),
        )
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self.status_key(job_id),
                mapping={
                    "status": "retry_wait",
                    "retry_at": retry_at,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:1000],
                    "finished_at": "",
                    "dead_reason": "",
                },
            )
            pipe.expire(
                self.status_key(job_id),
                self.settings.archive_job_status_ttl_seconds,
            )
            pipe.expire(
                self.source_key(job_id),
                self.settings.archive_job_status_ttl_seconds,
            )
            pipe.expire(dedupe, self.settings.archive_job_dedupe_ttl_seconds)
            pipe.zadd(self.retry_zset, {job_id: retry_epoch})
            pipe.xack(self.stream, self.group, stream_id)
            pipe.xdel(self.stream, stream_id)
            await pipe.execute()
        await self._move_status(job_id, "retry_wait")

    async def promote_due_retries(self, limit: int = 50) -> int:
        """把到期的 retry_wait 任务重新投递到主 Stream。

        Lua 保证同一重试任务在多 worker 场景下只会被一个实例重新投递。
        """
        now = time.time()
        job_ids = await self.redis.zrangebyscore(
            self.retry_zset,
            "-inf",
            now,
            start=0,
            num=max(1, int(limit)),
        )
        promoted = 0
        script = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) then
  return 0
end
local status = redis.call('HGET', KEYS[2], 'status')
if status ~= 'retry_wait' then
  redis.call('ZREM', KEYS[1], ARGV[1])
  return 0
end
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then
  return 0
end
redis.call('HSET', KEYS[2], 'status', 'queued', 'retry_at', '')
redis.call('XADD', KEYS[3], '*', 'job_id', ARGV[1])
return 1
"""
        for job_id in job_ids:
            result = await self.redis.eval(
                script,
                3,
                self.retry_zset,
                self.status_key(job_id),
                self.stream,
                job_id,
                repr(now),
            )
            if result:
                promoted += 1
                await self._move_status(job_id, "queued")
        return promoted

    async def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        """记录任务成功结果并释放作品去重 key。"""
        await self.redis.hset(
            self.status_key(job_id),
            mapping={
                "status": "succeeded",
                "finished_at": utc_iso(),
                "retry_at": "",
                "result": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                "error_type": "",
                "error": "",
                "dead_reason": "",
            },
        )
        await self._finish(job_id, status="succeeded")

    async def mark_dead(
        self,
        job_id: str,
        exc: Exception,
        *,
        reason: str,
    ) -> None:
        """把不可恢复或重试耗尽的任务写入 dead 状态和 DLQ Stream。"""
        job = await self.get(job_id)
        if job is None:
            return
        finished = utc_iso()
        error_text = str(exc)[:1000]
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self.status_key(job_id),
                mapping={
                    "status": "dead",
                    "finished_at": finished,
                    "retry_at": "",
                    "error_type": exc.__class__.__name__,
                    "error": error_text,
                    "dead_reason": reason,
                    "result": "",
                },
            )
            pipe.zrem(self.retry_zset, job_id)
            pipe.xadd(
                self.dead_stream,
                {
                    "job_id": job_id,
                    "kind": str(job["kind"]),
                    "pixiv_id": str(job["pixiv_id"]),
                    "attempts": str(job["attempts"]),
                    "reason": reason,
                    "error_type": exc.__class__.__name__,
                    "error": error_text,
                    "dead_at": finished,
                },
                maxlen=self.settings.archive_job_dead_maxlen,
                approximate=True,
            )
            await pipe.execute()
        await self._finish(job_id, status="dead")

    async def mark_failed(self, job_id: str, exc: Exception) -> None:
        """兼容旧调用：失败任务直接记为 failed，不写入 DLQ。"""
        await self.redis.hset(
            self.status_key(job_id),
            mapping={
                "status": "failed",
                "finished_at": utc_iso(),
                "retry_at": "",
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
                "dead_reason": "",
                "result": "",
            },
        )
        await self._finish(job_id, status="failed")

    async def _finish(self, job_id: str, *, status: str) -> None:
        """结束任务、释放去重 key，并刷新任务状态的保留时间。"""
        job = await self.get(job_id)
        if job:
            dedupe = self.dedupe_key(
                WorkType(job["kind"]),
                int(job["pixiv_id"]),
            )
            await self._delete_dedupe_if_owned(dedupe, job_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(self.retry_zset, job_id)
            pipe.expire(
                self.status_key(job_id),
                self.settings.archive_job_status_ttl_seconds,
            )
            pipe.expire(
                self.source_key(job_id),
                self.settings.archive_job_status_ttl_seconds,
            )
            await pipe.execute()
        await self._move_status(job_id, status)

    async def queue_stats(self) -> dict[str, Any]:
        """返回归档队列、重试集合、DLQ 和各任务状态的统计信息。"""
        await self.ensure_group()
        await self._prune_indices()
        pending_info = await self.redis.xpending(self.stream, self.group)
        if isinstance(pending_info, dict):
            pending = int(pending_info.get("pending", 0))
        else:
            try:
                pending = int(pending_info[0])
            except (TypeError, IndexError, ValueError):
                pending = 0
        status_counts = {
            status: int(await self.redis.zcard(self.status_index_key(status)))
            for status in JOB_STATUSES
        }
        return {
            "stream_length": int(await self.redis.xlen(self.stream)),
            "pending": pending,
            "retry_wait": int(await self.redis.zcard(self.retry_zset)),
            "dead_letter_events": int(await self.redis.xlen(self.dead_stream)),
            "status_counts": status_counts,
        }

    async def read_one(
        self,
        consumer: str,
        *,
        block_ms: int = 5000,
    ) -> tuple[str, str] | None:
        """优先认领超时 pending 消息，否则阻塞读取一条新任务。"""
        await self.ensure_group()

        pending = await self.redis.xpending_range(
            self.stream,
            self.group,
            min="-",
            max="+",
            count=1,
            idle=self.settings.archive_job_claim_idle_ms,
        )
        if pending:
            message_id = pending[0]["message_id"]
            claimed = await self.redis.xclaim(
                self.stream,
                self.group,
                consumer,
                min_idle_time=self.settings.archive_job_claim_idle_ms,
                message_ids=[message_id],
            )
            if claimed:
                stream_id, fields = claimed[0]
                job_id = fields.get("job_id")
                if job_id:
                    return stream_id, job_id

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
        """刷新 pending 消息 idle 时间，避免长任务被其他 worker 抢走。"""
        await self.redis.xclaim(
            self.stream,
            self.group,
            consumer,
            min_idle_time=0,
            message_ids=[stream_id],
            justid=True,
        )

    async def ack(self, stream_id: str) -> None:
        """确认并删除一条已完成或已失效的 Stream 消息。"""
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(self.stream, self.group, stream_id)
            pipe.xdel(self.stream, stream_id)
            await pipe.execute()

    @staticmethod
    def _decode(raw: dict[str, Any]) -> dict[str, Any]:
        """把 Redis 字符串字段恢复为 API 友好的 Python 类型。"""
        value = dict(raw)
        for key in ("pixiv_id", "attempts"):
            if key in value and value[key] != "":
                value[key] = int(value[key])
        if value.get("created_ts"):
            value["created_ts"] = float(value["created_ts"])
        if "force_refresh" in value:
            value["force_refresh"] = value["force_refresh"] in (
                True,
                1,
                "1",
                "true",
                "True",
            )
        result = value.get("result")
        if result:
            try:
                value["result"] = json.loads(result)
            except (TypeError, ValueError):
                pass
        value.pop("consumer", None)
        value.setdefault("deduplicated", False)
        return value
