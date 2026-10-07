from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.base import PixivClient
from app.models.work import SyncSource, SyncSourceType, WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import now_utc


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

    async def _scan(self, fetch_page, old_frontier: set[str], *, full: bool) -> dict:
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
