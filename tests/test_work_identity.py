from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, Integer, Text, delete, event, select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.work import (
    Base, CurrentFile, PixivAsset, PixivVersion, PixivWork, Series,
    SeriesMember, SyncSource, SyncSourceType, Tag, WorkSource,
    WorkTagRelation, WorkType, normalize_work_kind, work_identity,
)
from app.repositories.work_repository import WorkRepository
from app.schemas.pixiv import RemoteSnapshot
from app.services.cache_service import CacheService
from app.services.series_archive import SeriesArchiveService
from app.services.work_sources import record_work_sources, upsert_work_sources
from app.storage.local_storage import LocalStorage


class SnapshotClient:
    def __init__(self):
        self.revisions = {"illust": 1, "novel": 1}
        self.illustration_type = WorkType.MANGA

    def snapshot(self, pixiv_id, kind):
        namespace = normalize_work_kind(kind)
        revision = self.revisions[namespace]
        payload = f"{namespace}-{revision}"
        return RemoteSnapshot(
            pixiv_id=pixiv_id, work_type=kind, title=payload,
            version_token=hashlib.sha256(payload.encode()).hexdigest(),
            tags=[{"name": payload}], text_content=payload,
            series_id="10" if kind in (WorkType.NOVEL, WorkType.MANGA) else None,
            metadata={"payload": payload, "series": {"title": f"{namespace} series"}},
        )

    async def get_novel_snapshot(self, pixiv_id):
        return self.snapshot(pixiv_id, WorkType.NOVEL)

    async def get_illust_snapshot(self, pixiv_id):
        return self.snapshot(pixiv_id, self.illustration_type)


class RecordingRedis:
    def __init__(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.lock_names = []

    def lock(self, name, **kwargs):
        self.lock_names.append(name)
        return self.redis.lock(name, **kwargs)

    def __getattr__(self, name):
        return getattr(self.redis, name)


@pytest_asyncio.fixture
async def archive(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "storage_min_free_bytes", 0)
    monkeypatch.setattr(settings, "remote_check_ttl_seconds", 0)
    monkeypatch.setattr(settings, "pixiv_deep_image_check", False)
    monkeypatch.setattr(settings, "sync_work_recheck_ttl_seconds", 3600)

    async def write_images(storage, snapshot, target):
        content = snapshot.metadata["payload"].encode()
        path = target / "0.jpg"
        path.write_bytes(content)
        return [storage._asset(
            0, "image", path, hashlib.sha256(content).hexdigest(), len(content), None,
        )]

    monkeypatch.setattr(LocalStorage, "_write_images", write_images)
    tables = [model.__table__ for model in (
        Series, SeriesMember, Tag, PixivWork, PixivVersion, PixivAsset,
        CurrentFile, WorkTagRelation, SyncSource, WorkSource,
    )]
    # Production uses MySQL. Preserve real keys/FKs and adapt only SQLite types.
    for table in tables:
        for column in table.columns:
            if isinstance(column.type, BigInteger) and column.primary_key:
                monkeypatch.setattr(column, "type", column.type.with_variant(Integer(), "sqlite"))
            elif isinstance(column.type, MEDIUMTEXT):
                monkeypatch.setattr(column, "type", column.type.with_variant(Text(), "sqlite"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    redis = RecordingRedis()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        yield SimpleNamespace(
            sessions=async_sessionmaker(engine, expire_on_commit=False),
            redis=redis, client=SnapshotClient(), root=tmp_path,
        )
    finally:
        await redis.aclose()
        await engine.dispose()


async def tags_for(session, kind, work_id="700"):
    return set((await session.execute(
        select(Tag.name).join(WorkTagRelation, WorkTagRelation.tag_id == Tag.id).where(
            WorkTagRelation.work_kind == kind, WorkTagRelation.work_id == work_id,
        )
    )).scalars().all())


@pytest.mark.asyncio
async def test_same_number_isolated_through_versions_files_tags_sources_and_delete(archive):
    async with archive.sessions() as session:
        service = CacheService(session, archive.client, archive.redis)
        assert (await service.get_novel(700)).source == "remote-created"
        assert (await service.get_illust(700)).source == "remote-created"
        session.add_all([
            SyncSource(id=1, source_type=SyncSourceType.AUTHOR, remote_user_id="11"),
            SyncSource(id=2, source_type=SyncSourceType.AUTHOR, remote_user_id="22"),
        ])
        await session.commit()
        await record_work_sources(session, "700", [1], work_kind="novel")
        await record_work_sources(session, "700", [1], work_kind="manga")
        await upsert_work_sources(session, ["700", "700"], [1, 2], work_kind="novel")
        await session.commit()

        archive.client.revisions["illust"] = 2
        assert (await service.get_illust(700)).version == 2
        assert await tags_for(session, "novel") == {"novel-1"}
        assert await tags_for(session, "illust") == {"illust-2"}
        novel_files = (await session.execute(select(CurrentFile).where(
            CurrentFile.work_kind == "novel", CurrentFile.work_id == "700",
        ))).scalars().all()
        assert len(novel_files) == 1
        assert Path(novel_files[0].local_path).read_text() == "novel-1"

        # A committed illustration v2 must not block recovery of a novel v2
        # directory left behind by a crashed, uncommitted download.
        orphan = archive.root / "novel" / "700" / "versions" / "2"
        orphan.mkdir(parents=True)
        (orphan / "incomplete.txt").write_text("crash remnant")
        archive.client.revisions["novel"] = 2
        assert (await service.get_novel(700)).version == 2
        remnants = list((archive.root / ".quarantine").rglob("incomplete.txt"))
        assert len(remnants) == 1 and remnants[0].read_text() == "crash remnant"

    # A new session verifies persisted composite relationships, not just the ORM
    # identity map used while archiving.
    async with archive.sessions() as session:
        service = CacheService(session, archive.client, archive.redis)
        repository = WorkRepository(session)
        for kind, payload in ((WorkType.NOVEL, "novel"), (WorkType.MANGA, "illust")):
            history = await service.get_history(700, kind)
            assert [version.version for version in history.versions] == [2, 1]
            current = await repository.get(700, kind)
            old = await repository.get_version(700, kind, 1)
            assert current.work_kind == payload and current.current_version_no == 2
            assert old.work.work_kind == payload and old.title == f"{payload}-1"
            assert len(old.assets) == 1
            assert Path(old.assets[0].local_path).read_text() == f"{payload}-1"
            assert await tags_for(session, payload) == {f"{payload}-2"}
        relations = (await session.execute(select(WorkSource))).scalars().all()
        assert {(row.work_kind, row.work_id, row.source_id) for row in relations} == {
            ("novel", "700", 1), ("novel", "700", 2), ("illust", "700", 1),
        }
        await session.execute(delete(PixivWork).where(
            PixivWork.work_kind == "novel", PixivWork.id == "700",
        ))
        await session.commit()

    async with archive.sessions() as session:
        for model in (PixivWork, PixivVersion, CurrentFile, WorkTagRelation, WorkSource):
            rows = (await session.execute(select(model))).scalars().all()
            assert rows and {row.work_kind for row in rows} == {"illust"}
        assert len((await session.execute(select(PixivAsset))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_illustration_subtypes_share_identity_and_lock(archive):
    archive.client.illustration_type = WorkType.ILLUST
    async with archive.sessions() as session:
        service = CacheService(session, archive.client, archive.redis)
        first = await service._get(700, WorkType.ILLUST, True, bypass_ttl=True)
        archive.client.illustration_type = WorkType.MANGA
        archive.client.revisions["illust"] = 2
        second = await service._get(700, WorkType.MANGA, True, bypass_ttl=True)
        third = await service._get(700, WorkType.UGOIRA, True, bypass_ttl=True)
        assert [first.version, second.version, third.version] == [1, 2, 2]
        assert third.source == "local-validated"
        assert third.type == WorkType.MANGA
        rows = (await session.execute(select(PixivWork))).scalars().all()
        assert len(rows) == 1 and rows[0].work_kind == "illust"
        old = await WorkRepository(session).get_version(700, WorkType.UGOIRA, 1)
        assert Path(old.assets[0].local_path).parts[-5:] == (
            "illust", "700", "versions", "1", "0.jpg",
        )
    assert archive.redis.lock_names == [
        f"{get_settings().cache_lock_prefix}illust:700",
    ] * 3


@pytest.mark.asyncio
async def test_snapshot_cannot_write_into_another_namespace(archive, monkeypatch):
    async def wrong_snapshot(pixiv_id):
        return archive.client.snapshot(pixiv_id, WorkType.NOVEL)

    monkeypatch.setattr(archive.client, "get_illust_snapshot", wrong_snapshot)
    async with archive.sessions() as session:
        service = CacheService(session, archive.client, archive.redis)
        with pytest.raises(ValueError, match="identity"):
            await service.get_illust(700)
        assert (await session.execute(select(PixivWork))).scalars().all() == []
        assert not (archive.root / "novel").exists()


@pytest.mark.asyncio
async def test_series_recent_skip_records_sources_in_matching_namespace(archive):
    class Queue:
        redis = archive.redis
        calls = []

        async def enqueue(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"deduplicated": False}

    queue = Queue()
    async with archive.sessions() as session:
        service = CacheService(session, archive.client, archive.redis)
        await service.get_novel(700)
        await service.get_illust(700)
        session.add_all([
            SyncSource(id=1, source_type=SyncSourceType.AUTHOR, remote_user_id="11"),
            SyncSource(id=2, source_type=SyncSourceType.AUTHOR, remote_user_id="22"),
        ])
        await session.commit()
        series_service = SeriesArchiveService(session, archive.client, queue)
        for kind, source_id in (("novel", 1), ("manga", 2)):
            result = await series_service.enqueue(
                kind, 10, force_refresh=False, source_id=source_id,
                discovery={"title": f"{kind} series", "works": [{"id": 700, "kind": kind}]},
            )
            assert result["skipped_recent"] == 1 and result["queued"] == 0
        relations = (await session.execute(select(WorkSource))).scalars().all()
        assert {(row.work_kind, row.source_id) for row in relations} == {
            ("novel", 1), ("illust", 2),
        }
        assert queue.calls == []


@pytest.mark.parametrize("kind,expected", [
    (WorkType.NOVEL, "novel"), (WorkType.ILLUST, "illust"),
    (WorkType.MANGA, "illust"), (WorkType.UGOIRA, "illust"),
])
def test_work_identity_keeps_two_remote_namespaces(kind, expected):
    assert work_identity(kind, 700) == (expected, "700")
