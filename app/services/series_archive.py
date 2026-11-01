"""Pixiv 系列完整发现与归档投递。文件下载使用原有 Redis ArchiveWorker。"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.pixivpy_client import PixivPyClient
from app.core.maintenance import require_writes_allowed
from app.models.work import PixivWork, Series, SeriesMember, WorkType
from app.services.archive_jobs import ArchiveJobQueue

logger = logging.getLogger(__name__)


class SeriesArchiveService:
    def __init__(self, session: AsyncSession, client: PixivPyClient, queue: ArchiveJobQueue):
        self.session = session
        self.client = client
        self.queue = queue

    async def enqueue(self, series_type: str, series_id: int, *, force_refresh: bool = True) -> dict:
        """发现全集后入库成员与批量投递任务，返回本次实际排队数量。"""
        if series_type not in ("novel", "manga") or series_id <= 0:
            raise ValueError("series_type must be novel/manga and series_id must be positive")
        await require_writes_allowed(self.queue.redis)
        discovery = (
            await self.client.discover_novel_series(series_id)
            if series_type == "novel"
            else await self.client.discover_manga_series(series_id)
        )
        works = discovery["works"]
        if not works:
            raise ValueError("Pixiv series returned no downloadable works")
        if len({int(item["id"]) for item in works}) != len(works):
            raise ValueError("Series discovery contains duplicate work IDs")
        sid = str(series_id)
        # 合集按 (type, id) 定位；漫画、小说可使用相同远端 ID。
        series = await self.session.get(Series, {"type": series_type, "id": sid})
        if series is None:
            series = Series(
                id=sid, type=series_type, title="", author_id="0", author_name=""
            )
            self.session.add(series)
        series.title = str(discovery["title"])[:255]
        series.caption = discovery.get("caption")
        series.author_id = str(discovery.get("author_id") or "0")[:32]
        series.author_name = str(discovery.get("author_name") or "")[:128]
        series.published_total = int(discovery.get("published_total") or len(works))
        series.is_completed = bool(discovery.get("is_completed"))
        await self.session.flush()

        existing = (
            await self.session.execute(
                select(SeriesMember).where(
                    SeriesMember.series_type == series_type,
                    SeriesMember.series_id == sid,
                )
            )
        ).scalars().all()
        members = {item.work_id: item for item in existing}
        ids = [str(item["id"]) for item in works]
        archived_ids = set((
            await self.session.execute(
                select(PixivWork.id).where(
                    PixivWork.id.in_(ids),
                    PixivWork.series_id == sid,
                    PixivWork.series_type == series_type,
                )
            )
        ).scalars().all())
        for item in works:
            wid = str(item["id"])
            member = members.get(wid)
            if member is None:
                member = SeriesMember(series_type=series_type, series_id=sid, work_id=wid)
                self.session.add(member)
                members[wid] = member
            member.series_order = item.get("order")
            if wid in archived_ids:
                member.status = "archived"
                member.last_error = None
        # 必须在 Redis 投递之前提交 queued；否则 worker 可能先归档成功，
        # 之后 HTTP 请求又把数据库状态从 archived 覆盖回 queued。
        for item in works:
            member = members[str(item["id"])]
            member.status = "queued"
            member.last_error = None
        await self.session.commit()

        count = 0
        dedup = 0
        # Redis 投递成功不可回滚；未投递的成员恢复为 discovered，可再次发起。
        try:
            for item in works:
                result = await self.queue.enqueue(
                    WorkType.NOVEL if series_type == "novel" else WorkType.MANGA,
                    int(item["id"]),
                    force_refresh=force_refresh,
                )
                dedup += int(bool(result.get("deduplicated")))
                count += 1
        except Exception:
            for item in works[count:]:
                members[str(item["id"])].status = "discovered"
            await self.session.commit()
            logger.exception("series_enqueue_failed", extra={
                "series_id": sid, "series_type": series_type, "queued": count,
            })
            raise
        logger.info("series_enqueued", extra={
            "series_id": sid, "series_type": series_type,
            "count": len(works), "queued": count, "deduplicated": dedup,
        })
        return {
            "series_id": sid, "series_type": series_type,
            "discovered": len(works), "queued": count,
            "deduplicated": dedup, "already_archived": len(archived_ids),
            "published_total": series.published_total,
            "discovery_complete": len(works) >= series.published_total,
        }

    async def status(self, series_type: str, series_id: int) -> dict:
        series = await self.session.get(Series, {"type": series_type, "id": str(series_id)})
        if series is None:
            raise LookupError("Series not yet discovered")
        members = (
            await self.session.execute(
                select(SeriesMember).where(
                    SeriesMember.series_type == series_type,
                    SeriesMember.series_id == str(series_id),
                )
                .order_by(SeriesMember.series_order, SeriesMember.work_id)
            )
        ).scalars().all()
        return {
            "series_id": series.id,
            "series_type": series.type,
            "title": series.title,
            "published_total": series.published_total,
            "discovered": len(members),
            "discovery_complete": len(members) >= series.published_total,
            "status_counts": {
                status: sum(m.status == status for m in members)
                for status in ("discovered", "queued", "archived", "failed")
            },
            "works": [
                {"pixiv_id": int(m.work_id), "order": m.series_order, "status": m.status}
                for m in members
            ],
        }
