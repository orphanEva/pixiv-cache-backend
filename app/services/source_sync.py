from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.base import PixivClient
from app.models.work import SyncSource, SyncSourceType, WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import now_utc
from app.services.series_archive import SeriesArchiveService


class SourceSyncService:
    def __init__(
        self,
        session: AsyncSession,
        client: PixivClient,
        redis_client,
    ) -> None:
        self.session = session
        self.client = client
        self.redis = redis_client
        self.archive_queue = ArchiveJobQueue(redis_client)
        from app.core.config import get_settings
        self.settings = get_settings()

    async def run_source(self, source: SyncSource, *, full: bool = False) -> dict:
        source.last_run_at = now_utc()
        source.last_error = None
        await self.session.commit()

        try:
            if source.source_type == SyncSourceType.WATCHLIST:
                return await self._run_novel_watchlist(source)
            remote_user_id = await self._resolve_user_id(source.remote_user_id)
            previous = source.frontier_json or {}
            next_frontier = dict(previous)
            totals = {
                "discovered": 0,
                "archive_jobs": 0,
                "pages": 0,
                "stopped_at_frontier": False,
            }

            if source.include_illust:
                result = await self._scan(
                    lambda params: self._fetch_illust_page(
                        source, remote_user_id, params
                    ),
                    set(str(v) for v in (previous.get("illust") or [])),
                    full=full,
                    source_id=source.id,
                )
                next_frontier["illust"] = result["frontier"]
                self._merge_totals(totals, result)

            if source.include_novel:
                result = await self._scan(
                    lambda params: self._fetch_novel_page(
                        source, remote_user_id, params
                    ),
                    set(str(v) for v in (previous.get("novel") or [])),
                    full=full,
                    source_id=source.id,
                )
                next_frontier["novel"] = result["frontier"]
                self._merge_totals(totals, result)

            source.frontier_json = next_frontier
            source.last_success_at = now_utc()
            source.last_error = None
            source.next_run_at = now_utc() + timedelta(
                seconds=max(60, int(source.interval_seconds))
            )
            await self.session.commit()
            return {
                "source_id": source.id,
                "source_type": source.source_type.value,
                "remote_user_id": source.remote_user_id,
                "resolved_user_id": remote_user_id,
                "full": full,
                **totals,
                "frontier": next_frontier,
            }
        except Exception as exc:
            await self.session.rollback()
            source = await self.session.get(SyncSource, source.id)
            if source is not None:
                source.last_run_at = now_utc()
                source.last_error = str(exc)[:1000]
                source.next_run_at = now_utc() + timedelta(
                    seconds=min(
                        max(300, int(source.interval_seconds)),
                        self.settings.sync_failure_retry_seconds,
                    )
                )
                await self.session.commit()
            raise


    async def _run_novel_watchlist(self, source: SyncSource) -> dict:
        """每轮完整枚举当前追更列表，再为每个合集发现新章节。

        不采用普通收藏夹的 frontier 提前停止规则：旧合集也会更新章节。
        取消追更后该合集不再被本轮扫描，已保存内容不删除。
        """
        if source.remote_user_id != "novel" or not source.include_novel:
            raise ValueError("Unsupported watchlist sync source")
        web = self.client.web
        series_ids: list[int] = []
        seen: set[int] = set()
        pages = 0
        # 与普通同步源共用页数上限，超限时不记录成功进度。
        for page in range(1, self.settings.sync_max_pages_per_run + 1):
            result = await web.get_watched_novel_series_page(page)
            pages += 1
            batch = result["series_ids"]
            if not isinstance(batch, list):
                raise ValueError("Invalid novel watchlist page")
            if batch and all(sid in seen for sid in batch):
                raise ValueError("Pixiv watchlist pagination did not advance")
            for sid in batch:
                if sid not in seen:
                    seen.add(sid)
                    series_ids.append(sid)
                if len(series_ids) > 1000:
                    raise ValueError("Novel watchlist exceeds Pixiv's 1000-series limit")
            if result.get("has_next") is False or not batch:
                break
            if self.settings.sync_page_delay_seconds > 0:
                await asyncio.sleep(self.settings.sync_page_delay_seconds)
        else:
            raise ValueError("Novel watchlist exceeded SYNC_MAX_PAGES_PER_RUN")

        totals = {"series": len(series_ids), "discovered": 0,
                  "archive_jobs": 0, "already_archived": 0, "pages": pages}
        series_service = SeriesArchiveService(self.session, self.client, self.archive_queue)
        for sid in series_ids:
            outcome = await series_service.enqueue(
                "novel", sid, force_refresh=False, only_missing=True, source_id=source.id
            )
            totals["discovered"] += outcome["discovered"]
            totals["archive_jobs"] += outcome["queued"]
            totals["already_archived"] += outcome["already_archived"]

        source.frontier_json = {"novel_series": [str(sid) for sid in series_ids]}
        source.last_success_at = now_utc()
        source.last_error = None
        source.next_run_at = now_utc() + timedelta(
            seconds=max(60, int(source.interval_seconds))
        )
        await self.session.commit()
        return {
            "source_id": source.id,
            "source_type": source.source_type.value,
            "remote_user_id": source.remote_user_id,
            "full": True,
            **totals,
        }

    async def _resolve_user_id(self, value: str) -> int:
        if value == "self":
            return await self.client.get_authenticated_user_id()
        return int(value)

    async def _fetch_illust_page(self, source, user_id: int, params: dict | None):
        if source.source_type == SyncSourceType.AUTHOR:
            return await self.client.discover_author_illusts(user_id, params)
        return await self.client.discover_bookmark_illusts(
            user_id, source.restrict_mode.value, params
        )

    async def _fetch_novel_page(self, source, user_id: int, params: dict | None):
        if source.source_type == SyncSourceType.AUTHOR:
            return await self.client.discover_author_novels(user_id, params)
        return await self.client.discover_bookmark_novels(
            user_id, source.restrict_mode.value, params
        )

    async def _scan(
        self,
        fetch_page,
        old_frontier: set[str],
        *,
        full: bool,
        source_id: int | None = None,
    ) -> dict:
        next_params = None
        new_frontier: list[str] = []
        seen: set[tuple[str, int]] = set()
        pages = 0
        discovered = 0
        jobs = 0
        stopped = False

        while pages < self.settings.sync_max_pages_per_run:
            page = await fetch_page(next_params)
            pages += 1
            if pages == 1:
                new_frontier = [
                    str(item.pixiv_id)
                    for item in page.items[: self.settings.sync_frontier_size]
                ]

            for item in page.items:
                marker = str(item.pixiv_id)
                if not full and old_frontier and marker in old_frontier:
                    stopped = True
                    break
                normalized = (
                    WorkType.NOVEL
                    if item.work_type == WorkType.NOVEL
                    else WorkType.ILLUST
                )
                key = (normalized.value, item.pixiv_id)
                if key in seen:
                    continue
                seen.add(key)
                discovered += 1
                await self.archive_queue.enqueue(
                    normalized,
                    item.pixiv_id,
                    force_refresh=True,
                    source_id=source_id,
                )
                jobs += 1

            if stopped or not page.next_params:
                break
            next_params = page.next_params
            if self.settings.sync_page_delay_seconds > 0:
                await asyncio.sleep(self.settings.sync_page_delay_seconds)

        if (
            pages >= self.settings.sync_max_pages_per_run
            and next_params
            and not stopped
        ):
            raise RuntimeError(
                "Sync page limit reached before the remote source was exhausted"
            )

        return {
            "frontier": new_frontier,
            "discovered": discovered,
            "archive_jobs": jobs,
            "pages": pages,
            "stopped_at_frontier": stopped,
        }

    @staticmethod
    def _merge_totals(total: dict, result: dict) -> None:
        total["discovered"] += result["discovered"]
        total["archive_jobs"] += result["archive_jobs"]
        total["pages"] += result["pages"]
        total["stopped_at_frontier"] = (
            total["stopped_at_frontier"] or result["stopped_at_frontier"]
        )
