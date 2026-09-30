"""Versioned Pixiv caching against the manually managed seven-table archive schema."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import logging
import shutil

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.security import cache_read_allowed
from app.core.errors import (
    PixivAuthError, PixivNotFoundError, PixivRemoteError,
    PixivRestrictedError, PixivUnavailableError,
)
from app.models.work import (
    CurrentFile, PixivAsset, PixivVersion, PixivWork, Series,
    Tag, WorkStatus, WorkTagRelation, WorkType,
)
from app.schemas.pixiv import (
    AssetResponse, HistoryResponse, RemoteSnapshot, VersionSummary, WorkResponse,
)
from app.storage.local_storage import LocalStorage

logger = logging.getLogger(__name__)
ILLUSTRATION_TYPES = (WorkType.ILLUST, WorkType.MANGA, WorkType.UGOIRA)


def utc_naive(dt: datetime | None) -> datetime | None:
    """MySQL DATETIME stores wall time without TZ; all values are UTC by convention."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def asset_file_type(work_type: WorkType) -> str:
    return "novel_txt" if work_type == WorkType.NOVEL else "image"


class CacheService:
    def __init__(self, session: AsyncSession, client: PixivClient, redis_client=None):
        self.session = session
        self.client = client
        self.storage = LocalStorage()
        self.settings = get_settings()
        if redis_client is None:
            from app.core.redis_client import get_redis
            redis_client = get_redis()
        self.redis = redis_client

    async def get_illust(self, pixiv_id: int, refresh: bool = True) -> WorkResponse:
        return await self._get(pixiv_id, WorkType.ILLUST, refresh)

    async def get_novel(self, pixiv_id: int, refresh: bool = True) -> WorkResponse:
        return await self._get(pixiv_id, WorkType.NOVEL, refresh)

    async def get_history(self, pixiv_id: int, work_type: WorkType) -> HistoryResponse:
        work = await self._load(pixiv_id, work_type)
        if work is None:
            raise PixivNotFoundError("No local archive for this work")
        self._require_local_access(work)
        return HistoryResponse(
            pixiv_id=pixiv_id,
            type=work.work_type,
            current_version=work.current_version_no,
            versions=[self._version_summary(v) for v in sorted(work.versions, key=lambda v: v.version_no, reverse=True)],
        )

    async def get_version(self, pixiv_id: int, work_type: WorkType, version_no: int) -> WorkResponse:
        work = await self._load(pixiv_id, work_type)
        if work is None:
            raise PixivNotFoundError("No local archive for this work")
        self._require_local_access(work)
        version = next((v for v in work.versions if v.version_no == version_no), None)
        if version is None:
            raise PixivNotFoundError("Archived version does not exist")
        return self._to_response(work, "local-history", version)

    async def _get(self, pixiv_id: int, work_type: WorkType, refresh: bool, bypass_ttl: bool = False) -> WorkResponse:
        work = await self._load(pixiv_id, work_type)
        if work and not refresh:
            self._require_local_access(work)
            return self._to_response(work, "local")
        if (work and not bypass_ttl and cache_read_allowed(work.status.value)
                and work.last_checked_at is not None and self.settings.remote_check_ttl_seconds > 0):
            if (now_utc() - utc_naive(work.last_checked_at)).total_seconds() < self.settings.remote_check_ttl_seconds:
                return self._to_response(work, "local-recently-validated")

        lock = self.redis.lock(
            f"pixiv-cache:{work_type.value}:{pixiv_id}",
            timeout=self.settings.lock_ttl_seconds,
            blocking_timeout=self.settings.lock_wait_seconds,
        )
        if not await lock.acquire():
            work = await self._load(pixiv_id, work_type)
            if work:
                self._require_local_access(work)
                return self._to_response(work, "local-lock-timeout")
            raise PixivUnavailableError("Cache lock could not be acquired")

        try:
            work = await self._load(pixiv_id, work_type)
            if work and not refresh:
                self._require_local_access(work)
                return self._to_response(work, "local")
            try:
                snapshot = await self._remote_snapshot(pixiv_id, work_type)
            except PixivNotFoundError as exc:
                await self._mark_remote_status(work, WorkStatus.DELETED, str(exc))
                raise
            except PixivRestrictedError as exc:
                await self._mark_remote_status(work, WorkStatus.RESTRICTED, str(exc))
                raise
            except PixivAuthError as exc:
                await self._mark_remote_status(work, WorkStatus.AUTH_REQUIRED, str(exc))
                raise
            except PixivUnavailableError as exc:
                await self._mark_remote_status(work, WorkStatus.UNAVAILABLE, str(exc))
                if work and self.settings.serve_stale_on_remote_unavailable:
                    return self._to_response(work, "local-stale-remote-unavailable")
                raise
            except PixivRemoteError as exc:
                await self._mark_remote_status(work, WorkStatus.ERROR, str(exc))
                if work and self.settings.serve_stale_on_remote_unavailable:
                    return self._to_response(work, "local-stale-remote-error")
                raise

            if work and work.version_token == snapshot.version_token:
                work.status = WorkStatus.ACTIVE
                work.status_reason = None
                work.last_checked_at = now_utc()
                await self.session.commit()
                work = await self._load(pixiv_id, work_type)
                return self._to_response(work, "local-validated")

            new_no = 1 if work is None else work.current_version_no + 1
            storage_path = ""
            committed = False
            try:
                # Store files in immutable per-version directories. work_files
                # is merely a DB pointer to current paths, never a file move.
                storage_path, assets = await self.storage.materialize(snapshot, new_no)
                now = now_utc()
                meta = snapshot.metadata
                if work is None:
                    work = PixivWork(id=str(pixiv_id), work_type=snapshot.work_type)
                    self.session.add(work)
                await self._sync_series(snapshot)
                work.status = WorkStatus.ACTIVE
                work.status_reason = None
                work.work_type = snapshot.work_type
                work.title = snapshot.title[:255]
                work.caption = snapshot.caption
                work.author_id = str(snapshot.author_id or 0)
                work.author_name = (snapshot.author_name or "")[:128]
                work.page_count = max(1, len(snapshot.assets)) if snapshot.work_type != WorkType.NOVEL else 1
                work.x_restrict = snapshot.x_restrict
                work.is_ai = snapshot.is_ai
                work.series_id = snapshot.series_id
                work.series_order = snapshot.series_order
                work.remote_created_at = utc_naive(snapshot.created_at)
                work.remote_updated_at = utc_naive(snapshot.updated_at)
                work.novel_content = snapshot.text_content if snapshot.work_type == WorkType.NOVEL else None
                work.metadata_json = meta
                work.version_token = snapshot.version_token
                work.current_version_no = new_no
                work.last_checked_at = now
                if new_no == 1:
                    work.cached_at = now
                await self.session.flush()

                version = PixivVersion(
                    work_id=str(pixiv_id), version_no=new_no,
                    version_token=snapshot.version_token, title=snapshot.title[:255],
                    caption=snapshot.caption, tags_snapshot=snapshot.tags,
                    remote_updated_at=utc_naive(snapshot.updated_at),
                    novel_content=snapshot.text_content if snapshot.work_type == WorkType.NOVEL else None,
                    metadata_json=meta, storage_path=storage_path, created_at=now,
                )
                self.session.add(version)
                await self.session.flush()
                for item in assets:
                    self.session.add(PixivAsset(
                        version_id=version.id, page_index=item["page_index"],
                        file_type=asset_file_type(snapshot.work_type),
                        local_path=item["local_path"], sha256=item["sha256"],
                        size_bytes=item["size_bytes"], remote_url=item["remote_url"],
                    ))

                await self.session.execute(delete(CurrentFile).where(CurrentFile.work_id == str(pixiv_id)))
                for item in assets:
                    self.session.add(CurrentFile(
                        work_id=str(pixiv_id), page_index=item["page_index"],
                        file_type=asset_file_type(snapshot.work_type),
                        local_path=item["local_path"], sha256=item["sha256"],
                        size_bytes=item["size_bytes"],
                    ))
                await self._sync_tags(str(pixiv_id), snapshot.tags)
                await self.session.commit()
                committed = True
            except Exception:
                if not committed:
                    await self.session.rollback()
                    if storage_path:
                        shutil.rmtree(Path(storage_path), ignore_errors=True)
                raise
            work = await self._load(pixiv_id, work_type)
            source = "remote-created" if new_no == 1 else "remote-updated"
            logger.info("cache_materialized", extra={"pixiv_id": pixiv_id, "work_type": snapshot.work_type.value, "version": new_no, "source": source})
            return self._to_response(work, source)
        finally:
            try:
                await lock.release()
            except Exception:
                logger.warning("cache_lock_release_failed", exc_info=True)

    async def _sync_series(self, snapshot: RemoteSnapshot) -> None:
        if not snapshot.series_id:
            return
        series_info = snapshot.metadata.get("series") or {}
        if not isinstance(series_info, dict):
            series_info = {}
        series_type = "novel" if snapshot.work_type == WorkType.NOVEL else "manga"
        series = await self.session.get(Series, snapshot.series_id)
        if series is None:
            self.session.add(Series(
                id=snapshot.series_id, type=series_type,
                title=(series_info.get("title") or snapshot.title)[:255],
                author_id=str(snapshot.author_id or 0),
                author_name=(snapshot.author_name or "")[:128],
            ))

    async def _sync_tags(self, work_id: str, tags: list[dict]) -> None:
        if not tags:
            return
        await self.session.execute(delete(WorkTagRelation).where(WorkTagRelation.work_id == work_id))
        # A separately committed tag dictionary survives individual work updates;
        # relations are rebuilt transactionally against the current snapshot.
        for tag in tags:
            name = str(tag.get("name") or "").strip()[:128]
            if not name:
                continue
            tag_id = (await self.session.execute(select(Tag.id).where(Tag.name == name))).scalar_one_or_none()
            if tag_id is None:
                record = Tag(name=name, translated_name=(tag.get("translated_name") or None))
                self.session.add(record)
                await self.session.flush()
                tag_id = record.id
            self.session.add(WorkTagRelation(work_id=work_id, tag_id=tag_id))

    async def _mark_remote_status(self, work: PixivWork | None, status: WorkStatus, reason: str) -> None:
        if work is None:
            return
        work.status = status
        work.status_reason = reason[:1000]
        work.last_checked_at = now_utc()
        await self.session.commit()

    async def _remote_snapshot(self, pixiv_id: int, work_type: WorkType) -> RemoteSnapshot:
        if work_type == WorkType.NOVEL:
            return await self.client.get_novel_snapshot(pixiv_id)
        return await self.client.get_illust_snapshot(pixiv_id)

    async def _load(self, pixiv_id: int, work_type: WorkType) -> PixivWork | None:
        kinds = ILLUSTRATION_TYPES if work_type == WorkType.ILLUST else (work_type,)
        stmt = (select(PixivWork)
                .where(PixivWork.id == str(pixiv_id), PixivWork.work_type.in_(kinds))
                .options(selectinload(PixivWork.versions).selectinload(PixivVersion.assets))
                .execution_options(populate_existing=True))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _to_response(self, work: PixivWork, source: str, selected_version: PixivVersion | None = None) -> WorkResponse:
        version = selected_version or next(v for v in work.versions if v.version_no == work.current_version_no)
        assets = [
            AssetResponse(page_index=a.page_index, local_path=self._public_path(a.local_path),
                          sha256=a.sha256 or "", size_bytes=a.size_bytes)
            for a in sorted(version.assets, key=lambda a: a.page_index)
        ]
        return WorkResponse(
            pixiv_id=work.pixiv_id, type=work.work_type, status=work.status,
            status_reason=work.status_reason, title=version.title,
            author_id=int(work.author_id) if work.author_id.isdigit() else None,
            author_name=work.author_name, remote_created_at=work.remote_created_at,
            remote_updated_at=version.remote_updated_at, version=version.version_no,
            version_token=version.version_token, cached_at=work.cached_at,
            last_checked_at=work.last_checked_at or work.cached_at,
            source=source, assets=assets,
            novel_text_path=(assets[0].local_path if work.work_type == WorkType.NOVEL and assets else None),
        )

    def _version_summary(self, version: PixivVersion) -> VersionSummary:
        return VersionSummary(
            version=version.version_no, version_token=version.version_token,
            remote_updated_at=version.remote_updated_at, created_at=version.created_at,
            assets=[
                AssetResponse(page_index=a.page_index, local_path=self._public_path(a.local_path),
                              sha256=a.sha256 or "", size_bytes=a.size_bytes)
                for a in sorted(version.assets, key=lambda a: a.page_index)
            ],
        )

    def _public_path(self, path: str) -> str:
        relative = Path(path).resolve().relative_to(self.settings.storage_root.resolve())
        return f"/media/{relative.as_posix()}"

    @staticmethod
    def _require_local_access(work: PixivWork) -> None:
        if not cache_read_allowed(work.status.value):
            if work.status == WorkStatus.DELETED:
                raise PixivNotFoundError("Remote work is no longer accessible")
            if work.status == WorkStatus.AUTH_REQUIRED:
                raise PixivAuthError("Remote authorization requires renewal")
            raise PixivRestrictedError("Remote work is restricted")
