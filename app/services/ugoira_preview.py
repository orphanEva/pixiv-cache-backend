"""Generate a missing MP4 from a specific local history version only."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import PixivUnavailableError
from app.core.maintenance import MaintenanceWriter, require_writer_alive
from app.core.redis_lock import RenewingRedisLock
from app.models.work import CurrentFile, PixivAsset, PixivVersion, PixivWork, WorkType, work_identity
from app.storage.local_storage import LocalStorage


class UgoiraPreviewService:
    def __init__(self, session, redis_client, storage=None):
        self.session = session
        self.redis = redis_client
        self.storage = storage or LocalStorage()
        self.settings = get_settings()

    async def resolve_version(self, pixiv_id: int, version_no: int | None = None):
        if pixiv_id <= 0 or (version_no is not None and version_no <= 0):
            raise ValueError("Work ID and version must be positive")
        work = await self.session.get(PixivWork, work_identity(WorkType.ILLUST, pixiv_id))
        if work is None:
            raise LookupError("Archived illustration not found")
        version_no = version_no or work.current_version_no
        version = (await self.session.execute(select(PixivVersion).where(
            PixivVersion.work_kind == "illust", PixivVersion.work_id == str(pixiv_id),
            PixivVersion.version_no == version_no,
        ))).scalar_one_or_none()
        if version is None:
            raise LookupError("Archived illustration version not found")
        assets = (await self.session.execute(select(PixivAsset).where(
            PixivAsset.version_id == version.id,
            PixivAsset.file_type.in_(("ugoira_zip", "ugoira_meta", "ugoira_mp4")),
        ))).scalars().all()
        by_type = {}
        for asset in assets:
            if asset.file_type in by_type:
                raise ValueError("Ambiguous Ugoira source asset records")
            by_type[asset.file_type] = asset
        if not {"ugoira_zip", "ugoira_meta"}.issubset(by_type):
            raise ValueError("This version has no archived Ugoira ZIP and frame metadata")
        return work, version, by_type

    async def generate(self, pixiv_id: int, version_no: int, *, progress=None) -> dict:
        async with MaintenanceWriter(self.redis):
            lock = RenewingRedisLock(
                self.redis, f"{self.settings.cache_lock_prefix}illust:{pixiv_id}",
                ttl_seconds=self.settings.lock_ttl_seconds,
                wait_seconds=self.settings.lock_wait_seconds,
            )
            if not await lock.acquire():
                raise PixivUnavailableError("The archived work is busy; retry preview generation")
            try:
                await self.session.rollback()
                if progress:
                    await progress("validating")
                work, version, assets = await self.resolve_version(pixiv_id, version_no)
                expected_dir = self.storage.root.resolve() / "ugoira" / str(pixiv_id) / "versions" / str(version_no)
                if Path(version.storage_path).resolve() != expected_dir:
                    raise ValueError("Version storage directory does not match this Ugoira")
                mp4 = assets.get("ugoira_mp4")
                existing = bool(mp4 and Path(mp4.local_path).resolve() == expected_dir / "preview.mp4"
                                and await self.storage._local_thread(
                                    self.storage.validate_local_asset, Path(mp4.local_path), mp4.sha256, mp4.size_bytes,
                                ))
                # Do not retain a database read transaction during FFmpeg.
                await self.session.commit()

                async def before_publish():
                    await require_writer_alive()
                    lock.ensure_alive()

                if existing:
                    asset_data = self.storage._asset(
                        mp4.page_index, "ugoira_mp4", Path(mp4.local_path), mp4.sha256, mp4.size_bytes, None,
                    )
                else:
                    source_zip, source_meta = assets["ugoira_zip"], assets["ugoira_meta"]
                    asset_data = await self.storage.rebuild_ugoira_preview(
                        pixiv_id=pixiv_id, version_no=version_no,
                        zip_path=Path(source_zip.local_path), metadata_path=Path(source_meta.local_path),
                        zip_sha256=source_zip.sha256, zip_size_bytes=source_zip.size_bytes,
                        metadata_sha256=source_meta.sha256, metadata_size_bytes=source_meta.size_bytes,
                        before_publish=before_publish, progress=progress,
                    )
                if progress:
                    await progress("registering")
                if mp4 is None:
                    mp4 = PixivAsset(version_id=version.id, **asset_data)
                    self.session.add(mp4)
                else:
                    for key, value in asset_data.items():
                        setattr(mp4, key, value)
                is_current = work.current_version_no == version_no
                if is_current:
                    current = (await self.session.execute(select(CurrentFile).where(
                        CurrentFile.work_kind == "illust", CurrentFile.work_id == str(pixiv_id),
                        CurrentFile.file_type == "ugoira_mp4",
                    ))).scalar_one_or_none()
                    current_data = {key: value for key, value in asset_data.items() if key != "remote_url"}
                    if current is None:
                        self.session.add(CurrentFile(work_kind="illust", work_id=str(pixiv_id), **current_data))
                    else:
                        for key, value in current_data.items():
                            setattr(current, key, value)
                await before_publish()
                await self.session.commit()
                if progress:
                    await progress("complete")
                return {
                    "pixiv_id": pixiv_id, "work_kind": "illust", "version_no": version_no,
                    "history_id": version.id, "current": is_current,
                    "source": "local-existing" if existing else "local-generated",
                    "file_type": "ugoira_mp4", "size_bytes": asset_data["size_bytes"],
                    "sha256": asset_data["sha256"],
                    "url": f"/media/ugoira/{pixiv_id}/versions/{version_no}/preview.mp4",
                }
            except BaseException:
                await self.session.rollback()
                raise
            finally:
                await lock.release()
