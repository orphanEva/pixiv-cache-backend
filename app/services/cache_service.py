from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil

import redis.asyncio as redis
from redis.exceptions import LockNotOwnedError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivNotFoundError, PixivRemoteError, PixivRestrictedError, PixivUnavailableError
from app.models.work import PixivAsset, PixivVersion, PixivWork, WorkStatus, WorkType
from app.schemas.pixiv import AssetResponse, HistoryResponse, RemoteSnapshot, VersionSummary, WorkResponse
from app.storage.local_storage import LocalStorage


class CacheService:
    def __init__(self, session: AsyncSession, client: PixivClient):
        self.session = session
        self.client = client
        self.storage = LocalStorage()
        self.settings = get_settings()
        self.redis = redis.from_url(self.settings.redis_url, decode_responses=True)

    async def get_illust(self, pixiv_id: int, refresh: bool = True) -> WorkResponse:
        return await self._get(pixiv_id, WorkType.ILLUST, refresh)

    async def get_novel(self, pixiv_id: int, refresh: bool = True) -> WorkResponse:
        return await self._get(pixiv_id, WorkType.NOVEL, refresh)

    async def get_history(self, pixiv_id: int, work_type: WorkType) -> HistoryResponse:
        local = await self._load(pixiv_id, work_type)
        if local is None:
            raise PixivNotFoundError(f"No local {work_type.value} cache for {pixiv_id}")
        versions = [self._version_summary(v) for v in sorted(local.versions, key=lambda x: x.version_no, reverse=True)]
        return HistoryResponse(
            pixiv_id=pixiv_id,
            type=work_type,
            current_version=local.current_version_no,
            versions=versions,
        )

    async def get_version(self, pixiv_id: int, work_type: WorkType, version_no: int) -> WorkResponse:
        local = await self._load(pixiv_id, work_type)
        if local is None:
            raise PixivNotFoundError(f"No local {work_type.value} cache for {pixiv_id}")
        version = next((v for v in local.versions if v.version_no == version_no), None)
        if version is None:
            raise PixivNotFoundError(f"Version {version_no} not found for {work_type.value} {pixiv_id}")
        return self._to_response(local, "local-history", selected_version=version)

    async def _get(self, pixiv_id: int, work_type: WorkType, refresh: bool) -> WorkResponse:
        local = await self._load(pixiv_id, work_type)
        if local and not refresh:
            return self._to_response(local, "local")

        if local and self.settings.remote_check_ttl_seconds > 0:
            age = (datetime.now(timezone.utc) - self._as_utc(local.last_checked_at)).total_seconds()
            if age < self.settings.remote_check_ttl_seconds:
                return self._to_response(local, "local-recently-validated")

        lock_name = f"pixiv-cache:{work_type.value}:{pixiv_id}"
        lock = self.redis.lock(
            lock_name,
            timeout=self.settings.lock_ttl_seconds,
            blocking_timeout=self.settings.lock_wait_seconds,
        )
        acquired = await lock.acquire()
        if not acquired:
            local = await self._load(pixiv_id, work_type)
            if local:
                return self._to_response(local, "local-lock-timeout")
            raise PixivUnavailableError(f"Could not acquire cache lock for {lock_name}")

        try:
            local = await self._load(pixiv_id, work_type)
            if local and not refresh:
                return self._to_response(local, "local")

            try:
                snapshot = await self._remote_snapshot(pixiv_id, work_type)
            except PixivNotFoundError as exc:
                await self._mark_remote_status(local, WorkStatus.DELETED, str(exc))
                raise
            except PixivRestrictedError as exc:
                await self._mark_remote_status(local, WorkStatus.RESTRICTED, str(exc))
                raise
            except PixivAuthError as exc:
                await self._mark_remote_status(local, WorkStatus.AUTH_REQUIRED, str(exc))
                raise
            except PixivUnavailableError as exc:
                await self._mark_remote_status(local, WorkStatus.UNAVAILABLE, str(exc))
                if local and self.settings.serve_stale_on_remote_unavailable:
                    return self._to_response(local, "local-stale-remote-unavailable")
                raise
            except PixivRemoteError as exc:
                await self._mark_remote_status(local, WorkStatus.ERROR, str(exc))
                if local and self.settings.serve_stale_on_remote_unavailable:
                    return self._to_response(local, "local-stale-remote-error")
                raise

            now = datetime.now(timezone.utc)
            if local and local.version_token == snapshot.version_token:
                local.status = WorkStatus.ACTIVE
                local.status_reason = None
                local.last_checked_at = now
                await self.session.commit()
                local = await self._load(pixiv_id, work_type)
                return self._to_response(local, "local-validated")

            new_version_no = 1 if local is None else local.current_version_no + 1
            storage_path = ""
            try:
                storage_path, assets = await self.storage.materialize(snapshot, new_version_no)
                if local is None:
                    local = PixivWork(
                        pixiv_id=pixiv_id,
                        work_type=work_type,
                        status=WorkStatus.ACTIVE,
                        status_reason=None,
                        title=snapshot.title,
                        author_id=snapshot.author_id,
                        author_name=snapshot.author_name,
                        remote_created_at=snapshot.created_at,
                        remote_updated_at=snapshot.updated_at,
                        version_token=snapshot.version_token,
                        current_version_no=new_version_no,
                        cached_at=now,
                        last_checked_at=now,
                    )
                    self.session.add(local)
                    await self.session.flush()
                else:
                    local.status = WorkStatus.ACTIVE
                    local.status_reason = None
                    local.title = snapshot.title
                    local.author_id = snapshot.author_id
                    local.author_name = snapshot.author_name
                    local.remote_created_at = snapshot.created_at
                    local.remote_updated_at = snapshot.updated_at
                    local.version_token = snapshot.version_token
                    local.current_version_no = new_version_no
                    local.cached_at = now
                    local.last_checked_at = now

                version = PixivVersion(
                    work_id=local.id,
                    version_no=new_version_no,
                    version_token=snapshot.version_token,
                    remote_updated_at=snapshot.updated_at,
                    storage_path=storage_path,
                    metadata_json=snapshot.metadata,
                    created_at=now,
                )
                self.session.add(version)
                await self.session.flush()
                for item in assets:
                    self.session.add(PixivAsset(version_id=version.id, **item))
                await self.session.commit()
            except Exception:
                await self.session.rollback()
                if storage_path:
                    shutil.rmtree(Path(storage_path), ignore_errors=True)
                raise

            local = await self._load(pixiv_id, work_type)
            return self._to_response(local, "remote-updated" if new_version_no > 1 else "remote-created")
        finally:
            try:
                await lock.release()
            except LockNotOwnedError:
                pass

    async def _mark_remote_status(self, local: PixivWork | None, status: WorkStatus, reason: str) -> None:
        if local is None:
            return
        local.status = status
        local.status_reason = reason[:1000]
        local.last_checked_at = datetime.now(timezone.utc)
        await self.session.commit()

    async def _remote_snapshot(self, pixiv_id: int, work_type: WorkType) -> RemoteSnapshot:
        if work_type == WorkType.ILLUST:
            return await self.client.get_illust_snapshot(pixiv_id)
        return await self.client.get_novel_snapshot(pixiv_id)

    async def _load(self, pixiv_id: int, work_type: WorkType) -> PixivWork | None:
        stmt = (
            select(PixivWork)
            .where(PixivWork.pixiv_id == pixiv_id, PixivWork.work_type == work_type)
            .options(selectinload(PixivWork.versions).selectinload(PixivVersion.assets))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    def _to_response(
        self,
        work: PixivWork,
        source: str,
        selected_version: PixivVersion | None = None,
    ) -> WorkResponse:
        version = selected_version or next(v for v in work.versions if v.version_no == work.current_version_no)
        assets = sorted(version.assets, key=lambda a: a.page_index)
        public_assets = [
            AssetResponse(
                page_index=a.page_index,
                local_path=self._public_path(a.local_path),
                sha256=a.sha256,
                size_bytes=a.size_bytes,
            )
            for a in assets
        ]
        return WorkResponse(
            pixiv_id=work.pixiv_id,
            type=work.work_type,
            status=work.status,
            status_reason=work.status_reason,
            title=work.title,
            author_id=work.author_id,
            author_name=work.author_name,
            remote_created_at=work.remote_created_at,
            remote_updated_at=version.remote_updated_at,
            version=version.version_no,
            version_token=version.version_token,
            cached_at=work.cached_at,
            last_checked_at=work.last_checked_at,
            source=source,
            assets=public_assets,
            novel_text_path=public_assets[0].local_path if work.work_type == WorkType.NOVEL and public_assets else None,
        )

    def _version_summary(self, version: PixivVersion) -> VersionSummary:
        return VersionSummary(
            version=version.version_no,
            version_token=version.version_token,
            remote_updated_at=version.remote_updated_at,
            created_at=version.created_at,
            assets=[
                AssetResponse(
                    page_index=a.page_index,
                    local_path=self._public_path(a.local_path),
                    sha256=a.sha256,
                    size_bytes=a.size_bytes,
                )
                for a in sorted(version.assets, key=lambda x: x.page_index)
            ],
        )

    def _public_path(self, path: str) -> str:
        relative = Path(path).resolve().relative_to(self.settings.storage_root.resolve())
        return f"/media/{relative.as_posix()}"

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
