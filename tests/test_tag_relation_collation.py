"""Real SQLite NOCASE regression for DB tag identity and work namespaces."""
from __future__ import annotations

import pytest
from sqlalchemy import MetaData, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.work import PixivWork, Series, Tag, WorkTagRelation, WorkType
from app.services.cache_service import CacheService


@pytest.fixture
async def tag_service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model in (Series, PixivWork, Tag, WorkTagRelation):
        model.__table__.to_metadata(metadata)
    metadata.tables["tags"].c.name.type = String(128, collation="NOCASE")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add_all([
                PixivWork(work_kind="illust", id="77", work_type=WorkType.ILLUST,
                          title="illustration", version_token="a" * 64),
                PixivWork(work_kind="novel", id="77", work_type=WorkType.NOVEL,
                          title="novel", version_token="b" * 64),
            ])
            await session.commit()
            service = CacheService.__new__(CacheService)
            service.session = session
            yield service, session
    finally:
        await engine.dispose()


async def relation_ids(session, kind):
    return (await session.execute(select(WorkTagRelation.tag_id).where(
        WorkTagRelation.work_kind == kind, WorkTagRelation.work_id == "77",
    ).order_by(WorkTagRelation.tag_id))).scalars().all()


@pytest.mark.asyncio
async def test_collation_equivalent_names_share_tag_id_without_duplicate_relations(tag_service):
    service, session = tag_service
    tags = [{"name": "CaseFoldTag"}, {"name": "casefoldtag"},
            {"name": " CASEFOLDTAG "}, {"name": "CaseFoldTag"}]
    assert tags[0]["name"] != tags[1]["name"]
    await service._sync_tags("77", tags, work_kind="novel")
    await session.commit()
    ids = [(await session.execute(select(Tag.id).where(Tag.name == spelling))).scalar_one()
           for spelling in ("CaseFoldTag", "casefoldtag", "CASEFOLDTAG")]
    assert len(set(ids)) == 1
    assert await relation_ids(session, "novel") == [ids[0]]

    # A second snapshot and a later autoflush must remain idempotent.
    await service._sync_tags("77", tags, work_kind="novel")
    await session.flush()
    await session.commit()
    assert await relation_ids(session, "novel") == [ids[0]]


@pytest.mark.asyncio
async def test_same_numeric_work_id_keeps_illustration_and_novel_tags_independent(tag_service):
    service, session = tag_service
    shared = [{"name": "Shared"}, {"name": "shared"}]
    await service._sync_tags("77", shared, work_kind="illust")
    await service._sync_tags("77", shared, work_kind="novel")
    await session.commit()
    original = await relation_ids(session, "illust")
    assert len(original) == 1 and await relation_ids(session, "novel") == original

    replacement = [{"name": "NovelOnly"}, {"name": "novelonly"}]
    await service._sync_tags("77", replacement, work_kind="novel")
    await session.commit()
    novel = await relation_ids(session, "novel")
    assert len(novel) == 1 and novel != original
    assert await relation_ids(session, "illust") == original

    await service._sync_tags("77", replacement, work_kind="novel")
    await session.commit()
    assert await relation_ids(session, "novel") == novel
    assert await relation_ids(session, "illust") == original

    await service._sync_tags("77", [], work_kind="novel")
    await session.commit()
    assert await relation_ids(session, "novel") == []
    assert await relation_ids(session, "illust") == original
