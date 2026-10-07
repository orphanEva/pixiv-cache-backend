"""Integration tests run ONLY against disposable CI MySQL+Redis after manual DDL."""
from pathlib import Path
import uuid
import pytest
from sqlalchemy import delete, func, select, text

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.redis_client import close_redis, get_redis
from app.db.session import SessionLocal
from app.models.work import CurrentFile, PixivVersion, PixivWork, Series, SyncRestrict, SyncSource, SyncSourceType, Tag, WorkTagRelation, WorkType
from app.schemas.pixiv import RemoteSnapshot
from app.services.cache_service import CacheService


class FakePixiv(PixivClient):
    def __init__(self):
        self.version = 1
        self.calls = 0

    async def get_novel_snapshot(self, pixiv_id):
        self.calls += 1
        return RemoteSnapshot(
            pixiv_id=pixiv_id, work_type=WorkType.NOVEL,
            title=f"title-v{self.version}", text_content=f"novel-v{self.version}",
            version_token=f"{self.version:064x}",
            metadata={"title": f"title-v{self.version}"},
        )

    async def get_illust_snapshot(self, pixiv_id):
        raise AssertionError("unexpected illust call")


@pytest.mark.asyncio(loop_scope="module")
async def test_mysql_redis_cache_lifecycle(tmp_path: Path):
    settings = get_settings()
    old_root, old_ttl = settings.storage_root, settings.remote_check_ttl_seconds
    settings.storage_root, settings.remote_check_ttl_seconds = tmp_path, 0
    pixiv = FakePixiv()
    work_id = 987651234
    try:
        async with SessionLocal() as session:
            await session.execute(delete(PixivWork).where(PixivWork.id == str(work_id)))
            await session.commit()
            service = CacheService(session, pixiv, redis_client=get_redis())
            v1 = await service.get_novel(work_id)
            assert v1.version == 1 and v1.source == "remote-created"
            assert v1.assets[0].local_path.endswith("/novel.txt")
            local = await service.get_novel(work_id, refresh=False)
            assert local.version == 1 and pixiv.calls == 1
            unchanged = await service.get_novel(work_id)
            assert unchanged.version == 1 and unchanged.source == "local-validated"
            pixiv.version = 2
            v2 = await service.get_novel(work_id)
            assert v2.version == 2 and v2.title == "title-v2"
            history = await service.get_history(work_id, WorkType.NOVEL)
            assert [v.version for v in history.versions] == [2, 1]
            old = await service.get_version(work_id, WorkType.NOVEL, 1)
            assert old.title == "title-v1"
            assert (tmp_path / f"novel/{work_id}/versions/1/novel.txt").read_text() == "novel-v1"
            assert (tmp_path / f"novel/{work_id}/versions/2/novel.txt").read_text() == "novel-v2"
            files = (await session.execute(select(CurrentFile).where(CurrentFile.work_id == str(work_id)))).scalars().all()
            assert len(files) == 1 and files[0].local_path.endswith("/versions/2/novel.txt")
            versions = (await session.execute(select(func.count(PixivVersion.id)).where(PixivVersion.work_id == str(work_id)))).scalar_one()
            assert versions == 2
    finally:
        settings.storage_root, settings.remote_check_ttl_seconds = old_root, old_ttl
        await close_redis()


@pytest.mark.asyncio(loop_scope="module")
async def test_series_and_tag_relationships_follow_updates(tmp_path: Path):
    """A real MySQL FK check: series must exist before a new work references it."""
    settings = get_settings()
    old_root, old_ttl = settings.storage_root, settings.remote_check_ttl_seconds
    settings.storage_root, settings.remote_check_ttl_seconds = tmp_path, 0
    pixiv_id = 987651235

    class SeriesPixiv(FakePixiv):
        async def get_novel_snapshot(self, pixiv_id):
            self.calls += 1
            tag = "初音ミク" if self.version == 1 else "原神"
            return RemoteSnapshot(
                pixiv_id=pixiv_id, work_type=WorkType.NOVEL,
                title="series novel", author_id=88, author_name="author",
                series_id="series-test-135", series_order=1,
                tags=[{"name": tag}, {"name": tag}],
                text_content=f"chapter-{self.version}",
                version_token=f"{self.version:064x}",
                metadata={"series": {"id": "series-test-135", "title": "My series"}},
            )

    client = SeriesPixiv()
    try:
        async with SessionLocal() as session:
            await session.execute(delete(PixivWork).where(PixivWork.id == str(pixiv_id)))
            await session.execute(delete(Series).where(Series.id == "series-test-135"))
            await session.commit()
            service = CacheService(session, client, redis_client=get_redis())
            await service.get_novel(pixiv_id)
            assert await session.get(Series, "series-test-135") is not None
            assert (await session.execute(
                select(func.count()).select_from(WorkTagRelation)
                .where(WorkTagRelation.work_id == str(pixiv_id))
            )).scalar_one() == 1

            client.version = 2
            await service.get_novel(pixiv_id)
            names = (await session.execute(
                select(Tag.name).join(WorkTagRelation, WorkTagRelation.tag_id == Tag.id)
                .where(WorkTagRelation.work_id == str(pixiv_id))
            )).scalars().all()
            assert names == ["原神"]
            history = await service.get_history(pixiv_id, WorkType.NOVEL)
            assert [v.version for v in history.versions] == [2, 1]
    finally:
        settings.storage_root, settings.remote_check_ttl_seconds = old_root, old_ttl
        await close_redis()


@pytest.mark.asyncio(loop_scope="module")
async def test_mysql_accepts_ugoira_archive_file_types():
    """The checked-in manual DDL must accept every Ugoira file type used by code."""
    from app.models.work import PixivAsset
    from app.services.cache_service import now_utc

    work_id = "987651236"
    async with SessionLocal() as session:
        await session.execute(delete(PixivWork).where(PixivWork.id == work_id))
        await session.commit()

        work = PixivWork(
            id=work_id,
            work_type=WorkType.UGOIRA,
            status="active",
            title="ugoira",
            author_id="1",
            author_name="artist",
            page_count=2,
            metadata_json={},
            version_token="f" * 64,
            current_version_no=1,
            last_checked_at=now_utc(),
            cached_at=now_utc(),
        )
        session.add(work)
        await session.flush()

        version = PixivVersion(
            work_id=work_id,
            version_no=1,
            version_token="f" * 64,
            title="ugoira",
            tags_snapshot=[],
            metadata_json={},
            storage_path="/tmp/ugoira",
            created_at=now_utc(),
        )
        session.add(version)
        await session.flush()

        for index, file_type in enumerate(("ugoira_zip", "ugoira_meta", "ugoira_mp4")):
            path = f"/tmp/ugoira/{file_type}"
            session.add(PixivAsset(
                version_id=version.id,
                page_index=index,
                file_type=file_type,
                local_path=path,
                sha256="0" * 64,
                size_bytes=1,
            ))
            session.add(CurrentFile(
                work_id=work_id,
                page_index=index,
                file_type=file_type,
                local_path=path,
                sha256="0" * 64,
                size_bytes=1,
            ))
        await session.commit()

        history_types = (await session.execute(
            select(PixivAsset.file_type).where(PixivAsset.version_id == version.id)
        )).scalars().all()
        current_types = (await session.execute(
            select(CurrentFile.file_type).where(CurrentFile.work_id == work_id)
        )).scalars().all()
        assert set(history_types) == {"ugoira_zip", "ugoira_meta", "ugoira_mp4"}
        assert set(current_types) == {"ugoira_zip", "ugoira_meta", "ugoira_mp4"}


@pytest.mark.asyncio(loop_scope="module")
async def test_manual_ddl_records_expected_schema_version():
    from app.core.schema_version import EXPECTED_SCHEMA_VERSION
    async with SessionLocal() as session:
        actual = (await session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        )).scalar_one()
        assert actual == EXPECTED_SCHEMA_VERSION


@pytest.mark.asyncio(loop_scope="module")
async def test_redis_stream_archive_job_lifecycle():
    from app.services.archive_jobs import ArchiveJobQueue

    settings = get_settings()
    suffix = uuid.uuid4().hex
    old = {
        "archive_job_stream": settings.archive_job_stream,
        "archive_job_group": settings.archive_job_group,
        "archive_job_status_prefix": settings.archive_job_status_prefix,
        "archive_job_dedupe_prefix": settings.archive_job_dedupe_prefix,
        "archive_job_claim_idle_ms": settings.archive_job_claim_idle_ms,
    }
    settings.archive_job_stream = f"test:archive:jobs:{suffix}"
    settings.archive_job_group = f"test-workers-{suffix}"
    settings.archive_job_status_prefix = f"test:archive:job:{suffix}:"
    settings.archive_job_dedupe_prefix = f"test:archive:dedupe:{suffix}:"
    settings.archive_job_claim_idle_ms = 1000

    redis_client = get_redis()
    queue = ArchiveJobQueue(redis_client)
    try:
        first = await queue.enqueue(WorkType.UGOIRA, 778899, force_refresh=True)
        assert first["status"] == "queued"
        assert first["kind"] == "illust"

        duplicate = await queue.enqueue(WorkType.ILLUST, 778899, force_refresh=True)
        assert duplicate["job_id"] == first["job_id"]
        assert duplicate["deduplicated"] is True

        item = await queue.read_one("test-consumer", block_ms=100)
        assert item is not None
        stream_id, job_id = item
        assert job_id == first["job_id"]

        running = await queue.mark_running(job_id, "test-consumer")
        assert running["status"] == "running"
        assert running["attempts"] == 1

        await queue.touch(stream_id, "test-consumer")
        await queue.mark_succeeded(job_id, {"version": 1})
        await queue.ack(stream_id)

        final = await queue.get(job_id)
        assert final["status"] == "succeeded"
        assert final["result"] == {"version": 1}

        retried = await queue.retry(job_id)
        assert retried["job_id"] != job_id
        assert retried["status"] == "queued"

        retry_item = await queue.read_one("test-consumer", block_ms=100)
        assert retry_item is not None
        await queue.mark_failed(retry_item[1], RuntimeError("test failure"))
        await queue.ack(retry_item[0])
        retry_final = await queue.get(retry_item[1])
        assert retry_final["status"] == "failed"
        assert retry_final["error_type"] == "RuntimeError"
    finally:
        await redis_client.delete(settings.archive_job_stream)
        for key in await redis_client.keys(f"test:archive:*:{suffix}*"):
            await redis_client.delete(key)
        for key, value in old.items():
            setattr(settings, key, value)


@pytest.mark.asyncio(loop_scope="module")
async def test_backup_restore_roundtrip_against_mysql8(tmp_path: Path):
    from app.maintenance.backup import ArchiveBackupManager

    settings = get_settings()
    old = {
        "storage_root": settings.storage_root,
        "backup_root": settings.backup_root,
        "storage_min_free_bytes": settings.storage_min_free_bytes,
        "maintenance_database_url": settings.maintenance_database_url,
    }
    settings.storage_root = tmp_path / "storage"
    settings.backup_root = tmp_path / "backups"
    settings.storage_min_free_bytes = 0
    settings.maintenance_database_url = ""
    settings.storage_root.mkdir(parents=True)
    settings.backup_root.mkdir(parents=True)

    probe_name = f"restore-probe-{uuid.uuid4().hex}"
    file_path = settings.storage_root / "novel/backup-probe/versions/1/novel.txt"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("before-backup", encoding="utf-8")

    manager = ArchiveBackupManager()
    try:
        created = await manager.create("ci-roundtrip")
        backup_path = Path(created["backup_path"])
        assert manager.verify(backup_path)["ok"] is True

        file_path.write_text("mutated-after-backup", encoding="utf-8")
        async with SessionLocal() as session:
            session.add(Tag(name=probe_name))
            await session.commit()

        restored = await manager.restore(
            backup_path,
            confirm_destructive_restore=True,
        )
        assert restored["restored"] is True
        assert file_path.read_text(encoding="utf-8") == "before-backup"

        async with SessionLocal() as session:
            probe = (await session.execute(
                select(Tag.id).where(Tag.name == probe_name)
            )).scalar_one_or_none()
            assert probe is None
    finally:
        for key, value in old.items():
            setattr(settings, key, value)



@pytest.mark.asyncio(loop_scope="module")
async def test_mysql_sync_source_schema_and_persistence():
    remote_user_id = "9988776655"
    async with SessionLocal() as session:
        await session.execute(
            delete(SyncSource).where(
                SyncSource.source_type == SyncSourceType.AUTHOR,
                SyncSource.remote_user_id == remote_user_id,
            )
        )
        await session.commit()

        source = SyncSource(
            source_type=SyncSourceType.AUTHOR,
            remote_user_id=remote_user_id,
            restrict_mode=SyncRestrict.PUBLIC,
            include_illust=True,
            include_novel=True,
            enabled=True,
            interval_seconds=3600,
            frontier_json={"illust": ["100"], "novel": ["200"]},
        )
        session.add(source)
        await session.commit()
        await session.refresh(source)

        loaded = await session.get(SyncSource, source.id)
        assert loaded is not None
        assert loaded.source_type == SyncSourceType.AUTHOR
        assert loaded.frontier_json == {
            "illust": ["100"],
            "novel": ["200"],
        }

        await session.delete(loaded)
        await session.commit()


@pytest.mark.asyncio(loop_scope="module")
async def test_redis_stream_sync_job_lifecycle():
    from app.services.sync_jobs import SyncJobQueue

    settings = get_settings()
    suffix = uuid.uuid4().hex
    old = {
        "sync_job_stream": settings.sync_job_stream,
        "sync_job_group": settings.sync_job_group,
        "sync_job_status_prefix": settings.sync_job_status_prefix,
        "sync_job_dedupe_prefix": settings.sync_job_dedupe_prefix,
        "sync_job_claim_idle_ms": settings.sync_job_claim_idle_ms,
    }
    settings.sync_job_stream = f"test:sync:jobs:{suffix}"
    settings.sync_job_group = f"test-sync-workers-{suffix}"
    settings.sync_job_status_prefix = f"test:sync:job:{suffix}:"
    settings.sync_job_dedupe_prefix = f"test:sync:dedupe:{suffix}:"
    settings.sync_job_claim_idle_ms = 1000

    redis_client = get_redis()
    queue = SyncJobQueue(redis_client)
    try:
        first = await queue.enqueue(12345, full=False)
        assert first["status"] == "queued"
        assert first["source_id"] == 12345

        duplicate = await queue.enqueue(12345, full=True)
        assert duplicate["job_id"] == first["job_id"]
        assert duplicate["deduplicated"] is True
        assert duplicate["full"] is True

        item = await queue.read_one("sync-test-consumer", block_ms=100)
        assert item is not None
        stream_id, job_id = item

        running = await queue.mark_running(job_id, "sync-test-consumer")
        assert running["status"] == "running"
        assert running["attempts"] == 1
        assert running["full"] is True

        await queue.touch(stream_id, "sync-test-consumer")
        await queue.mark_succeeded(job_id, {"discovered": 4})
        await queue.ack(stream_id)

        final = await queue.get(job_id)
        assert final["status"] == "succeeded"
        assert final["result"] == {"discovered": 4}

        retried = await queue.retry(job_id)
        assert retried["job_id"] != job_id
        assert retried["full"] is True

        retry_item = await queue.read_one("sync-test-consumer", block_ms=100)
        assert retry_item is not None
        await queue.mark_failed(retry_item[1], RuntimeError("sync failed"))
        await queue.ack(retry_item[0])
        failed = await queue.get(retry_item[1])
        assert failed["status"] == "failed"
        assert failed["error_type"] == "RuntimeError"
    finally:
        await redis_client.delete(settings.sync_job_stream)
        for key in await redis_client.keys(f"test:sync:*:{suffix}*"):
            await redis_client.delete(key)
        for key, value in old.items():
            setattr(settings, key, value)
