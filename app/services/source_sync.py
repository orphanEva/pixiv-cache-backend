from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.base import PixivClient
from app.core.errors import PixivAuthError, PixivRemoteError, PixivUnavailableError
from app.models.work import (
    AccountMigrationState, PixivWork, SyncRestrict, SyncSource, SyncSourceType,
    WorkSource, WorkStatus, WorkType, work_identity,
)
from app.services.archive_jobs import ArchiveJobQueue
from app.services.cache_service import now_utc
from app.services.sync_jobs import SyncJobQueue
from app.services.work_sources import upsert_work_sources


class SourceSyncService:
    def __init__(self, session: AsyncSession, client: PixivClient, redis_client) -> None:
        self.session = session
        self.client = client
        self.redis = redis_client
        self.archive_queue = ArchiveJobQueue(redis_client)
        from app.core.config import get_settings
        self.settings = get_settings()

    async def run_source(self, source: SyncSource, *, full: bool = False) -> dict:
        # Save identity before rollback can expire ORM attributes.
        source_id = source.id
        source.last_run_at = now_utc()
        source.last_error = None
        await self.session.commit()
        try:
            self_account_id = await self._bind_self_account(source)
            if source.source_type in {SyncSourceType.NOVEL_SERIES, SyncSourceType.MANGA_SERIES}:
                return await self._run_series_source(source, full=full)
            if source.source_type == SyncSourceType.FOLLOWED_SERIES:
                return await self._run_followed_series_source(source, full=full)
            if source.source_type == SyncSourceType.WATCHLIST:
                return await self._run_novel_watchlist(source, full=full)
            remote_user_id = (int(self_account_id) if self_account_id is not None
                              else await self._resolve_user_id(source.remote_user_id))
            previous = source.frontier_json or {}
            state = dict(source.scan_state_json or {})
            # A full request supersedes an unfinished incremental scan. Scheduler
            # continuations resume a saved full scan without requiring full=True.
            if not state or (full and not state.get("full")):
                state = {"full": bool(full), "lanes": {}, "expanded_series": []}
            state["lanes"] = dict(state.get("lanes") or {})
            effective_full = bool(state.get("full"))
            expanded = set(state.get("expanded_series") or [])
            totals = {"discovered": 0, "archive_jobs": 0, "skipped_fresh": 0,
                      "pages": 0, "stopped_at_frontier": False}
            lanes = []
            if source.include_illust:
                lanes.append(("illust", self._fetch_illust_page))
            if source.include_novel:
                lanes.append(("novel", self._fetch_novel_page))
            for name, fetch in lanes:
                lane = dict(state["lanes"].get(name) or {})
                if lane.get("complete"):
                    continue
                budget = self.settings.sync_max_pages_per_run - totals["pages"]
                if budget <= 0:
                    break

                async def checkpoint(progress, lane_name=name):
                    state["lanes"][lane_name] = dict(progress)
                    state["expanded_series"] = sorted(expanded)
                    # Assign a fresh outer object for SQLAlchemy JSON tracking.
                    source.scan_state_json = {**state, "lanes": dict(state["lanes"])}
                    source.next_run_at = now_utc() + timedelta(
                        seconds=self.settings.sync_resume_delay_seconds)
                    await self.session.commit()

                result = await self._scan(
                    lambda params, method=fetch: method(source, remote_user_id, params),
                    set(str(v) for v in (previous.get(name) or [])),
                    full=effective_full, source_id=source_id, state=lane,
                    strict_remote_check=(effective_full or name not in previous),
                    checkpoint=checkpoint, page_budget=budget,
                    expand_series=(source.source_type == SyncSourceType.BOOKMARKS
                                   and bool(source.expand_series)),
                    expanded_series=expanded, source_interval=source.interval_seconds,
                )
                self._merge_totals(totals, result)
            complete = all(state["lanes"].get(name, {}).get("complete") for name, _ in lanes)
            if complete:
                next_frontier = dict(previous)
                for name, _ in lanes:
                    next_frontier[name] = state["lanes"][name]["frontier"]
                source.frontier_json = next_frontier
                source.scan_state_json = None
                source.last_success_at = now_utc()
                delay = max(60, int(source.interval_seconds))
            else:
                source.scan_state_json = {**state, "lanes": dict(state["lanes"])}
                delay = self.settings.sync_resume_delay_seconds
            source.last_error = None
            source.next_run_at = now_utc() + timedelta(seconds=delay)
            await self.session.commit()
            return {
                "source_id": source_id, "source_type": source.source_type.value,
                "remote_user_id": source.remote_user_id, "resolved_user_id": remote_user_id,
                "account_id": source.account_id,
                "full": effective_full, "complete": complete, **totals,
                "strict_remote_check": any(
                    state["lanes"].get(name, {}).get("strict_remote_check") for name, _ in lanes
                ),
                "frontier": source.frontier_json or {}, "scan_state": source.scan_state_json,
            }
        except Exception as exc:
            await self.session.rollback()
            source = await self.session.get(SyncSource, source_id)
            if source is not None:
                source.last_run_at = now_utc()
                source.last_error = str(exc)[:1000]
                source.next_run_at = now_utc() + timedelta(seconds=min(
                    max(300, int(source.interval_seconds)), self.settings.sync_failure_retry_seconds))
                await self.session.commit()
            raise

    async def _bind_self_account(self, source: SyncSource) -> str | None:
        """A self cursor belongs to the authenticated account, never its token."""
        legacy_watchlist = source.source_type == SyncSourceType.WATCHLIST
        if source.remote_user_id != "self" and not legacy_watchlist:
            return None
        if source.source_type not in {
            SyncSourceType.FOLLOWED_SERIES, SyncSourceType.WATCHLIST,
            SyncSourceType.BOOKMARKS, SyncSourceType.AUTHOR,
        }:
            return None
        app_account_id = None
        guard_enabled = self.settings.account_migration_enabled
        web_source = source.source_type in {SyncSourceType.FOLLOWED_SERIES, SyncSourceType.WATCHLIST}
        if guard_enabled or not web_source:
            app_account_id = str(int(await self.client.get_authenticated_user_id()))
            if int(app_account_id) <= 0:
                raise PixivAuthError("Pixiv did not return a verified App account ID")
        if guard_enabled:
            # A locking scalar read observes the current coordinator value even
            # under MySQL REPEATABLE READ, without reusing an ORM identity-map row.
            active_account_id = (await self.session.execute(
                select(AccountMigrationState.active_user_id)
                .where(AccountMigrationState.id == 1).with_for_update(read=True)
            )).scalar_one_or_none()
            if active_account_id is not None and str(active_account_id) != app_account_id:
                raise PixivAuthError("Another Pixiv account is active; this worker cannot bind self sources")
        if web_source:
            identity = await self.client.get_authenticated_web_user_id(force=True)
        else:
            identity = app_account_id
        account_id = str(int(identity))
        if int(account_id) <= 0:
            raise ValueError("Authenticated Pixiv account identity must be positive")
        if source.account_id != account_id:
            source.account_id = account_id
            source.frontier_json = None
            source.scan_state_json = None
            source.last_success_at = None
            # Persist the new binding and reset together before any page is read.
            # Previously archived works and versions are deliberately retained.
            await self.session.commit()
        elif guard_enabled:
            # Do not retain the coordinator row lock throughout the remote scan.
            await self.session.commit()
        return account_id

    async def _run_series_source(self, source: SyncSource, *, full: bool = False) -> dict:
        from app.services.series_archive import SeriesArchiveService
        series_type = "novel" if source.source_type == SyncSourceType.NOVEL_SERIES else "manga"
        strict = bool(full or source.last_success_at is None
                      or (source.scan_state_json or {}).get("strict_remote_check"))
        result = await SeriesArchiveService(self.session, self.client, self.archive_queue).enqueue(
            series_type, int(source.remote_user_id), force_refresh=strict,
            source_id=source.id, bookmark_if_missing=False)
        source.last_success_at = now_utc()
        source.last_error = None
        source.scan_state_json = None
        source.next_run_at = now_utc() + timedelta(seconds=max(60, source.interval_seconds))
        await self.session.commit()
        return {"source_id": source.id, "source_type": source.source_type.value,
                "complete": True, "strict_remote_check": strict, **result}

    async def _run_novel_watchlist(self, source: SyncSource, *, full: bool = False) -> dict:
        """Legacy novel-only source: scan every watched series, with durable resume.

        Unlike followed_series, this source does not create independent child
        subscriptions: removing a remote watch stops this source scanning it.
        """
        from app.services.series_archive import SeriesArchiveService
        if source.remote_user_id != "novel" or not source.include_novel or source.include_illust:
            raise ValueError("Unsupported watchlist sync source")
        source_id, interval = source.id, int(source.interval_seconds)
        state = dict(source.scan_state_json or {})
        if state.get("mode") != "novel_watchlist" or (full and not state.get("strict_remote_check")):
            state = {"mode": "novel_watchlist", "page": 1, "series_ids": [], "processed": [],
                     "discovery_complete": False,
                     "strict_remote_check": bool(full or source.last_success_at is None)}
        strict = bool(state["strict_remote_check"])
        ids = list(state.get("series_ids") or [])
        processed = set(state.get("processed") or [])
        budget = max(1, int(self.settings.sync_max_pages_per_run))
        pages = 0

        async def checkpoint():
            state["series_ids"] = list(ids)
            state["processed"] = sorted(processed)
            source.scan_state_json = dict(state)
            source.next_run_at = now_utc() + timedelta(seconds=self.settings.sync_resume_delay_seconds)
            await self.session.commit()

        while not state["discovery_complete"] and pages < budget:
            page = int(state["page"])
            if page > self.settings.series_max_pages:
                raise ValueError("Novel watchlist exceeds SERIES_MAX_PAGES")
            result = await self.client.web.get_watched_novel_series_page(page)
            batch = result.get("series_ids")
            if not isinstance(batch, list) or any(isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0 for sid in batch):
                raise ValueError("Invalid novel watchlist page")
            if len(set(batch)) != len(batch) or set(batch).intersection(ids):
                raise ValueError("Pixiv watchlist pagination did not advance")
            if not batch and result.get("has_next") is True:
                raise ValueError("Pixiv watchlist returned an empty page but claims more pages")
            if "total" in result:
                metadata = {"total": result["total"], "max_page": result["max_page"]}
                if state.get("metadata") is not None and state["metadata"] != metadata:
                    source.scan_state_json = None
                    await self.session.commit()
                    raise PixivUnavailableError("Pixiv watchlist changed during pagination; restart scan")
                state["metadata"] = metadata
                if metadata["total"] > self.settings.series_max_works or metadata["max_page"] > self.settings.series_max_pages:
                    raise ValueError("Novel watchlist exceeds configured discovery limits")
            ids.extend(batch)
            if len(ids) > self.settings.series_max_works:
                raise ValueError("Novel watchlist exceeds SERIES_MAX_WORKS")
            pages += 1
            state["page"] = page + 1
            state["discovery_complete"] = result.get("has_next") is False or not batch
            if state["discovery_complete"] and "metadata" in state and len(ids) != state["metadata"]["total"]:
                raise PixivRemoteError("Pixiv watchlist returned an incomplete collection")
            await checkpoint()
            if not state["discovery_complete"] and pages < budget and self.settings.sync_page_delay_seconds > 0:
                await asyncio.sleep(self.settings.sync_page_delay_seconds)

        totals = {"series": len(ids), "discovered": 0, "archive_jobs": 0, "already_archived": 0, "pages": pages}
        failed = []
        if state["discovery_complete"]:
            pending = [sid for sid in ids if sid not in processed]
            for sid in pending[:budget]:
                try:
                    outcome = await SeriesArchiveService(self.session, self.client, self.archive_queue).enqueue(
                        "novel", sid, force_refresh=strict, only_missing=not strict,
                        source_id=source_id, bookmark_if_missing=False)
                    totals["discovered"] += outcome["discovered"]
                    totals["archive_jobs"] += outcome["queued"]
                    totals["already_archived"] += outcome["already_archived"]
                    processed.add(sid)
                except (PixivRemoteError, ValueError):
                    # Continue healthy series, without keeping an aborted DB
                    # transaction or marking incomplete work as a successful scan.
                    failed.append(sid)
                    await self.session.rollback()
                    source = await self.session.get(SyncSource, source_id)
                    if source is None:
                        raise LookupError("Watchlist source was removed during synchronization")
                await checkpoint()
        if failed:
            raise ValueError(f"Unable to archive {len(failed)} watched novel series; series IDs: "
                             + ",".join(str(sid) for sid in failed[:20]))
        complete = bool(state["discovery_complete"] and len(processed) == len(ids))
        if complete:
            source.frontier_json = {"novel_series": [str(sid) for sid in ids]}
            source.scan_state_json = None
            source.last_success_at = now_utc()
            source.next_run_at = now_utc() + timedelta(seconds=max(60, interval))
        source.last_error = None
        await self.session.commit()
        return {"source_id": source_id, "source_type": source.source_type.value,
                "remote_user_id": source.remote_user_id, "account_id": source.account_id,
                "full": True, "complete": complete, "strict_remote_check": strict, **totals}

    async def _resolve_user_id(self, value: str) -> int:
        return await self.client.get_authenticated_user_id() if value == "self" else int(value)

    async def _run_followed_series_source(self, source: SyncSource, *, full: bool = False) -> dict:
        """Import the Web account's watched series as independent subscriptions."""
        discovered = created = 0
        frontier = {}
        strict = bool(full or source.last_success_at is None)
        rescan_ids = set()
        for series_type, enabled in (("novel", source.include_novel), ("manga", source.include_illust)):
            if not enabled:
                continue
            items = await self.client.discover_followed_series(series_type)
            frontier[series_type] = [str(item["series_id"]) for item in items]
            for item in items:
                subscription, is_new = await self._ensure_series_subscription(
                    series_type, str(item["series_id"]), source.interval_seconds, run_now=True)
                discovered += 1
                created += int(is_new)
                if strict and subscription.enabled:
                    subscription.scan_state_json = {"strict_remote_check": True}
                    subscription.next_run_at = now_utc()
                    rescan_ids.add(subscription.id)
        # Commit children before dispatch; the scheduler can recover the strict
        # request from MySQL even if Redis is unavailable during submission.
        await self.session.commit()
        queue = SyncJobQueue(self.redis)
        for source_id in sorted(rescan_ids):
            await queue.enqueue(source_id, full=True)
        source.frontier_json = frontier
        source.scan_state_json = None
        source.last_success_at = now_utc()
        source.last_error = None
        source.next_run_at = now_utc() + timedelta(seconds=max(60, source.interval_seconds))
        await self.session.commit()
        return {"source_id": source.id, "source_type": source.source_type.value,
                "complete": True, "discovered_series": discovered,
                "created_subscriptions": created, "rescan_subscriptions": len(rescan_ids),
                "strict_remote_check": strict, "account_id": source.account_id, "frontier": frontier}

    async def _fetch_illust_page(self, source, user_id: int, params: dict | None):
        if source.source_type == SyncSourceType.AUTHOR:
            return await self.client.discover_author_illusts(user_id, params)
        return await self.client.discover_bookmark_illusts(user_id, source.restrict_mode.value, params)

    async def _fetch_novel_page(self, source, user_id: int, params: dict | None):
        if source.source_type == SyncSourceType.AUTHOR:
            return await self.client.discover_author_novels(user_id, params)
        return await self.client.discover_bookmark_novels(user_id, source.restrict_mode.value, params)

    async def _scan(
        self, fetch_page, old_frontier: set[str], *, full: bool,
        source_id: int | None = None, state: dict | None = None,
        checkpoint=None, page_budget: int | None = None, expand_series: bool = False,
        expanded_series: set[str] | None = None, source_interval: int = 21600,
        strict_remote_check: bool | None = None,
    ) -> dict:
        progress = dict(state or {})
        # First scans and explicit full scans must inspect upstream even when a
        # local archive was checked recently. Persist that requirement per lane
        # so a later scheduler job resumes with the same verification semantics.
        strict = bool(full or progress.get("strict_remote_check") or (
            not old_frontier if strict_remote_check is None else strict_remote_check
        ))
        next_params = progress.get("next_params")
        new_frontier = list(progress.get("frontier") or [])
        seen: set[tuple[str, int]] = set()
        expanded = expanded_series if expanded_series is not None else set()
        pages = discovered = jobs = skipped = 0
        stopped = False
        complete = bool(progress.get("complete"))
        budget = max(1, int(page_budget or self.settings.sync_max_pages_per_run))
        while not complete and pages < budget:
            page = await fetch_page(next_params)
            pages += 1
            if not progress.get("started"):
                new_frontier = [str(item.pixiv_id)
                                for item in page.items[:self.settings.sync_frontier_size]]
            selected = []
            for item in page.items:
                if not full and old_frontier and str(item.pixiv_id) in old_frontier:
                    stopped = True
                    break
                normalized = WorkType.NOVEL if item.work_type == WorkType.NOVEL else WorkType.ILLUST
                key = (normalized.value, item.pixiv_id)
                if key not in seen:
                    seen.add(key)
                    selected.append(item)
            discovered += len(selected)
            page_jobs, page_skipped = await self._queue_works(
                selected, source_id, strict_remote_check=strict)
            jobs += page_jobs
            skipped += page_skipped
            if expand_series:
                for item in selected:
                    await self._expand_bookmark_series(
                        item, source_id, expanded, source_interval, strict_remote_check=strict)
            # Always save this page's outgoing cursor, including an exhausted page
            # at the exact budget boundary. Budget exhaustion is normal progress.
            next_params = page.next_params
            complete = stopped or not next_params
            progress = {"started": True, "next_params": None if complete else next_params,
                        "frontier": new_frontier, "complete": complete,
                        "strict_remote_check": strict}
            if checkpoint is not None:
                await checkpoint(progress)
            if not complete and pages < budget and self.settings.sync_page_delay_seconds > 0:
                await asyncio.sleep(self.settings.sync_page_delay_seconds)
        return {"frontier": new_frontier, "discovered": discovered, "archive_jobs": jobs,
                "skipped_fresh": skipped, "pages": pages, "stopped_at_frontier": stopped,
                "complete": complete, "strict_remote_check": strict, "scan_state": progress}

    async def _queue_works(self, items, source_id: int | None, *,
                           strict_remote_check: bool = False) -> tuple[int, int]:
        cached = {}
        if not strict_remote_check and self.session is not None and items:
            rows = (await self.session.execute(select(PixivWork).where(
                tuple_(PixivWork.work_kind, PixivWork.id).in_([work_identity(item.work_type, item.pixiv_id) for item in items])))).scalars().all()
            cached = {(work.work_kind, work.id): work for work in rows}
        now = now_utc()
        ttl = max(0, self.settings.sync_work_recheck_ttl_seconds)
        queued = skipped = 0
        fresh_ids = {"illust": [], "novel": []}
        for item in items:
            kind = WorkType.NOVEL if item.work_type == WorkType.NOVEL else WorkType.ILLUST
            work = cached.get(work_identity(kind, item.pixiv_id))
            same_kind = work is not None and ((work.work_type == WorkType.NOVEL) == (kind == WorkType.NOVEL))
            fresh = (same_kind and work.status == WorkStatus.ACTIVE and work.last_checked_at
                     and ttl > 0 and (now - work.last_checked_at).total_seconds() < ttl)
            if fresh:
                skipped += 1
                if source_id is not None:
                    fresh_ids[work.work_kind].append(work.id)
                continue
            await self.archive_queue.enqueue(kind, item.pixiv_id, force_refresh=True, source_id=source_id)
            queued += 1
        for namespace, ids in fresh_ids.items():
            if ids:
                await upsert_work_sources(self.session, ids, [source_id], work_kind=namespace)
        return queued, skipped

    async def _expand_bookmark_series(self, item, source_id, expanded: set[str], interval: int,
                                      *, strict_remote_check: bool = False):
        if item.work_type not in {WorkType.NOVEL, WorkType.MANGA}:
            return
        series_type = "novel" if item.work_type == WorkType.NOVEL else "manga"
        series_id = getattr(item, "series_id", None)
        if not series_id:
            detected = await self.client.detect_series(item.work_type, item.pixiv_id)
            if detected:
                series_type, series_id = detected
        if not series_id:
            return
        identity = f"{series_type}:{series_id}"
        if identity in expanded:
            return
        subscription, _ = await self._ensure_series_subscription(series_type, str(series_id), interval)
        from app.services.series_archive import SeriesArchiveService
        await SeriesArchiveService(self.session, self.client, self.archive_queue).enqueue(
            series_type, int(series_id), force_refresh=strict_remote_check, bookmark_if_missing=False,
            source_ids=[value for value in (source_id, subscription.id) if value is not None])
        expanded.add(identity)

    async def _ensure_series_subscription(self, series_type: str, series_id: str,
                                          interval: int, *, run_now: bool = False):
        source_type = (SyncSourceType.NOVEL_SERIES if series_type == "novel"
                       else SyncSourceType.MANGA_SERIES)
        subscription = (await self.session.execute(select(SyncSource).where(
            SyncSource.source_type == source_type, SyncSource.remote_user_id == str(series_id),
            SyncSource.restrict_mode == SyncRestrict.PUBLIC))).scalar_one_or_none()
        is_new = subscription is None
        if is_new:
            subscription = SyncSource(
                source_type=source_type, remote_user_id=str(series_id), restrict_mode=SyncRestrict.PUBLIC,
                include_illust=series_type == "manga", include_novel=series_type == "novel",
                expand_series=False, enabled=True, interval_seconds=interval,
                next_run_at=now_utc() if run_now else now_utc() + timedelta(seconds=interval))
            self.session.add(subscription)
            await self.session.flush()
        return subscription, is_new

    @staticmethod
    def _merge_totals(total: dict, result: dict) -> None:
        for key in ("discovered", "archive_jobs", "pages", "skipped_fresh"):
            total[key] += result.get(key, 0)
        total["stopped_at_frontier"] = total["stopped_at_frontier"] or result["stopped_at_frontier"]
