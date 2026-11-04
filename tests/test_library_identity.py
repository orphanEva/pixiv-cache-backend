from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import main
from app.core.errors import AmbiguousWorkError
from app.db.session import get_session
from app.models.work import (
    PixivWork, SyncSource, SyncSourceType, WorkSource, WorkStatus, WorkType, work_identity,
)
from app.schemas.pixiv import DiscoveredWork
from app.services.cache_service import CacheService, now_utc
from app.services.library_service import LibraryService
from app.services.source_sync import SourceSyncService
from app.services.work_sources import record_work_sources

from test_work_identity import archive  # Shared real SQLite/filesystem/fake Redis fixture.


async def seed_same_number(archive):
    async with archive.sessions() as session:
        cache = CacheService(session, archive.client, archive.redis)
        await cache.get_novel(700)
        await cache.get_illust(700)
        archive.client.revisions["novel"] = 2
        await cache.get_novel(700)
        session.add_all([
            SyncSource(id=1, source_type=SyncSourceType.AUTHOR, remote_user_id="11"),
            SyncSource(id=2, source_type=SyncSourceType.AUTHOR, remote_user_id="22"),
        ])
        await session.commit()
        await record_work_sources(session, "700", [1], work_kind="novel")
        await record_work_sources(session, "700", [2], work_kind="manga")


@asynccontextmanager
async def library_http(archive, monkeypatch):
    monkeypatch.setattr(main.settings, "api_key", "identity-test-key-" * 3)

    async def session_dependency():
        async with archive.sessions() as session:
            yield session

    main.app.dependency_overrides[get_session] = session_dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test",
            headers={"X-API-Key": main.settings.api_key},
        ) as client:
            yield client
    finally:
        main.app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_library_same_number_lists_filters_and_details_keep_attribution(archive):
    await seed_same_number(archive)
    async with archive.sessions() as session:
        service = LibraryService(session)
        result = await service.list_works(page=1, page_size=10)
        assert result.pagination.total == 2
        items = {item.type: item for item in result.items}
        assert set(items) == {WorkType.NOVEL, WorkType.MANGA}
        assert {tag.name for tag in items[WorkType.NOVEL].tags} == {"novel-2"}
        assert items[WorkType.NOVEL].source_ids == [1]
        assert {tag.name for tag in items[WorkType.MANGA].tags} == {"illust-1"}
        assert items[WorkType.MANGA].source_ids == [2]
        assert items[WorkType.NOVEL].current_version == 2
        assert items[WorkType.MANGA].current_version == 1

        filters = [
            ({"source_id": 1}, WorkType.NOVEL),
            ({"source_id": 2}, WorkType.MANGA),
            ({"tags": ["novel-2"], "tag_mode": "all"}, WorkType.NOVEL),
            ({"tags": ["illust-1"], "tag_mode": "any"}, WorkType.MANGA),
        ]
        for options, expected in filters:
            filtered = await service.list_works(page=1, page_size=10, **options)
            assert filtered.pagination.total == 1
            assert [(item.pixiv_id, item.type) for item in filtered.items] == [(700, expected)]
        incompatible = await service.list_works(
            page=1, page_size=10, source_id=2, tags=["novel-2"],
        )
        assert incompatible.items == []

        for kind, source_id, expected_versions in (("novel", 1, [2, 1]), ("illust", 2, [1])):
            detail = await service.get_work(700, kind=kind, include_content=True)
            assert [source.source_id for source in detail.sources] == [source_id]
            assert [source.remote_user_id for source in detail.sources] == [str(source_id * 11)]
            assert detail.novel_content == ("novel-2" if kind == "novel" else None)
            history = await service.get_history(700, kind=kind, include_content=True)
            assert [item.version for item in history.versions] == expected_versions
            old = await service.get_version(700, 1, kind=kind, include_content=True)
            assert old.title == f"{kind}-1"
            assert old.tags == [{"name": f"{kind}-1"}]
            assert old.novel_content == ("novel-1" if kind == "novel" else None)
        assert await service.get_version(700, 2, kind="illust") is None


@pytest.mark.asyncio
async def test_ambiguous_id_requires_kind_but_unique_numeric_id_remains_compatible(archive):
    await seed_same_number(archive)
    async with archive.sessions() as session:
        await CacheService(session, archive.client, archive.redis).get_novel(701)
        service = LibraryService(session)
        for method, arguments in (
            (service.resolve_work, (700,)), (service.get_work, (700,)),
            (service.get_history, (700,)), (service.get_version, (700, 1)),
            (service.verify_work, (700,)),
        ):
            with pytest.raises(AmbiguousWorkError):
                await method(*arguments)
        assert (await service.get_work(701)).type == WorkType.NOVEL
        assert (await service.get_history(701)).current_version == 1
        assert (await service.get_version(701, 1)).title == "novel-2"
        assert (await service.verify_work(701))["ok"] is True
        assert await service.get_work(701, kind="illust") is None


@pytest.mark.asyncio
async def test_library_api_disambiguates_and_media_checks_matching_namespace(archive, monkeypatch):
    await seed_same_number(archive)
    async with archive.sessions() as session:
        await CacheService(session, archive.client, archive.redis).get_novel(701)
    async with library_http(archive, monkeypatch) as client:
        for suffix in ("", "/history", "/versions/1", "/integrity"):
            ambiguous = await client.get(f"/api/library/works/700{suffix}")
            assert ambiguous.status_code == 409, ambiguous.text
            for kind in ("novel", "illust"):
                selected = await client.get(f"/api/library/works/700{suffix}", params={"kind": kind})
                assert selected.status_code == 200, selected.text
            unique = await client.get(f"/api/library/works/701{suffix}")
            assert unique.status_code == 200, unique.text
        novel = await client.get("/media/novel/700/versions/1/novel.txt")
        manga = await client.get("/media/manga/700/versions/1/0.jpg")
        assert novel.status_code == manga.status_code == 200
        assert novel.content == b"novel-1" and manga.content == b"illust-1"
        async with archive.sessions() as session:
            work = await session.get(PixivWork, ("novel", "700"))
            work.status = WorkStatus.RESTRICTED
            await session.commit()
        assert (await client.get("/media/novel/700/versions/1/novel.txt")).status_code == 403
        assert (await client.get("/media/manga/700/versions/1/0.jpg")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_kind,other_state", [
    (WorkType.NOVEL, "stale"), (WorkType.MANGA, "stale"),
    (WorkType.NOVEL, "missing"), (WorkType.MANGA, "missing"),
])
async def test_source_ttl_does_not_skip_same_number_in_other_namespace(archive, fresh_kind, other_state):
    other_kind = WorkType.MANGA if fresh_kind == WorkType.NOVEL else WorkType.NOVEL
    calls = []

    class Queue:
        async def enqueue(self, kind, pixiv_id, **kwargs):
            calls.append((kind, pixiv_id, kwargs))
            return {"job_id": "offline"}

    async with archive.sessions() as session:
        cache = CacheService(session, archive.client, archive.redis)
        await cache._get(700, fresh_kind, True)
        if other_state == "stale":
            await cache._get(700, other_kind, True)
            stale = await session.get(PixivWork, work_identity(other_kind, 700))
            stale.last_checked_at = now_utc() - timedelta(hours=2)
        session.add(SyncSource(id=1, source_type=SyncSourceType.AUTHOR, remote_user_id="11"))
        await session.commit()
        service = SourceSyncService(session, archive.client, archive.redis)
        service.archive_queue = Queue()
        items = [DiscoveredWork(pixiv_id=700, work_type=kind) for kind in (fresh_kind, other_kind)]
        assert await service._queue_works(items, 1) == (1, 1)
        await session.commit()
        expected_queue_kind = WorkType.NOVEL if other_kind == WorkType.NOVEL else WorkType.ILLUST
        assert calls == [(expected_queue_kind, 700, {"force_refresh": True, "source_id": 1})]
        relations = (await session.execute(select(WorkSource))).scalars().all()
        assert [(row.work_kind, row.work_id, row.source_id) for row in relations] == [
            (*work_identity(fresh_kind, 700), 1),
        ]
        calls.clear()
        assert await service._queue_works(items, 1, strict_remote_check=True) == (2, 0)
        assert {kind for kind, _, _ in calls} == {WorkType.NOVEL, WorkType.ILLUST}


@pytest.mark.asyncio
async def test_history_download_url_keeps_original_illustration_subtype(archive, monkeypatch):
    archive.client.illustration_type = WorkType.ILLUST
    async with archive.sessions() as session:
        cache = CacheService(session, archive.client, archive.redis)
        await cache.get_illust(700)
        archive.client.illustration_type = WorkType.MANGA
        archive.client.revisions["illust"] = 2
        await cache.get_illust(700)
        history = await LibraryService(session).get_history(700, kind="illust")
        old = next(item for item in history.versions if item.version == 1)
        url = old.files[0].download_url
        assert url == "/media/illust/700/versions/1/0.jpg"
    async with library_http(archive, monkeypatch) as client:
        response = await client.get(url)
        assert response.status_code == 200 and response.content == b"illust-1"
