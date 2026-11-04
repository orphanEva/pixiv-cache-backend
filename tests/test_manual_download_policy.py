import pytest
from fastapi.testclient import TestClient

from app import main
from app.api import jobs
from app.db.session import get_session


@pytest.mark.parametrize("bookmark,expected", [(None, True), ("false", False)])
def test_manual_job_carries_bookmark_intent(monkeypatch, bookmark, expected):
    monkeypatch.setattr(main.settings, "api_key", "test-key-" * 4)
    monkeypatch.setattr(main.settings, "pixiv_auto_bookmark_on_api_download", True)
    captured = []
    class Queue:
        async def enqueue(self, kind, pixiv_id, **kwargs):
            captured.append((kind, pixiv_id, kwargs))
            return {"job_id": "offline-job", "status": "queued"}
    async def session(): yield object()
    monkeypatch.setattr(jobs, "queue", lambda: Queue())
    main.app.dependency_overrides[get_session] = session
    try:
        query = "?expand_series=false" + (f"&bookmark_if_missing={bookmark}" if bookmark else "")
        response = TestClient(main.app).post(
            "/api/jobs/archive/illust/123" + query,
            headers={"X-API-Key": main.settings.api_key},
        )
        assert response.status_code == 202
        assert captured[0][2]["bookmark_if_missing"] is expected
    finally:
        main.app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_source_upsert_preserves_first_seen_and_avoids_duplicates():
    from datetime import timedelta
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.work import PixivWork, SyncSource, SyncSourceType, SyncRestrict, WorkType, WorkSource
    from app.services.cache_service import now_utc
    from app.services.work_sources import upsert_work_sources
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (PixivWork.__table__, SyncSource.__table__, WorkSource.__table__):
            await connection.run_sync(table.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    old = now_utc() - timedelta(days=1)
    try:
        async with sessions() as session:
            session.add(PixivWork(id="123", work_type=WorkType.NOVEL, version_token="a" * 64))
            session.add(SyncSource(id=1, source_type=SyncSourceType.BOOKMARKS,
                remote_user_id="self", restrict_mode=SyncRestrict.PRIVATE,
                include_illust=False, include_novel=True, interval_seconds=3600))
            await session.flush()
            session.add(WorkSource(work_kind="novel", work_id="123", source_id=1, first_seen_at=old, last_seen_at=old))
            await session.commit()
            await upsert_work_sources(session, ["123"], [1], work_kind="novel")
            await upsert_work_sources(session, ["123", "123"], [1, 1], work_kind="novel")
            await session.commit()
        async with sessions() as session:
            relations = (await session.execute(select(WorkSource))).scalars().all()
            assert len(relations) == 1
            assert relations[0].first_seen_at == old
            assert relations[0].last_seen_at > old
    finally:
        await engine.dispose()
