from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import get_settings
from app.models.work import PixivWork, Series, SeriesMember, WorkSource, WorkStatus, WorkType
from app.services.cache_service import now_utc
from app.services.series_archive import SeriesArchiveService


class Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


@pytest.mark.asyncio
@pytest.mark.parametrize("only_missing,expected_ids", [(True, [3]), (False, [1, 2, 3])])
async def test_legacy_only_missing_is_explicit_and_keeps_source_links(monkeypatch, only_missing, expected_ids):
    archived = [PixivWork(id=str(i), work_kind="novel", work_type=WorkType.NOVEL,
                         status=WorkStatus.ACTIVE, series_type="novel", series_id="10") for i in (1, 2)]
    added = []
    session = SimpleNamespace(
        get=AsyncMock(return_value=Series(id="10", type="novel", title="", author_id="1", author_name="")),
        add=added.append, flush=AsyncMock(), commit=AsyncMock(),
        execute=AsyncMock(side_effect=[Rows([]), Rows(archived)]),
    )
    queue = SimpleNamespace(redis=SimpleNamespace(exists=AsyncMock(return_value=False)),
                            enqueue=AsyncMock(return_value={"deduplicated": False}))
    attribution = AsyncMock()
    monkeypatch.setattr("app.services.series_archive.upsert_work_sources", attribution)
    result = await SeriesArchiveService(session, object(), queue).enqueue(
        "novel", 10, force_refresh=True, only_missing=only_missing, source_id=7,
        discovery={"title": "series", "works": [{"id": 1}, {"id": 2}, {"id": 3}]},
    )
    assert [call.args[1] for call in queue.enqueue.await_args_list] == expected_ids
    assert result["queued"] == len(expected_ids)
    if only_missing:
        attribution.assert_awaited_once_with(session, {"1", "2"}, [7], work_kind="novel")
    else:
        attribution.assert_not_awaited()


@pytest.mark.asyncio
async def test_series_sync_skips_recent_members_but_keeps_source_attribution(monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_work_recheck_ttl_seconds", 3600)
    now = now_utc()
    recent = PixivWork(id="1", work_type=WorkType.NOVEL, status=WorkStatus.ACTIVE,
                       last_checked_at=now, series_type="novel", series_id="10")
    stale = PixivWork(id="2", work_type=WorkType.NOVEL, status=WorkStatus.ACTIVE,
                      last_checked_at=now - timedelta(hours=2), series_type="novel", series_id="10")
    series = Series(id="10", type="novel", title="old", author_id="1", author_name="author")
    added = []
    session = SimpleNamespace(
        get=AsyncMock(return_value=series), add=added.append,
        flush=AsyncMock(), commit=AsyncMock(),
        execute=AsyncMock(side_effect=[Rows([]), Rows([recent, stale]), Rows([7, 8]), Rows([])]),
    )
    queue = SimpleNamespace(
        redis=SimpleNamespace(exists=AsyncMock(return_value=False)),
        enqueue=AsyncMock(return_value={"deduplicated": False}),
    )
    client = SimpleNamespace(discover_novel_series=AsyncMock())
    result = await SeriesArchiveService(session, client, queue).enqueue(
        "novel", 10, force_refresh=False, source_id=7, source_ids=[8],
        discovery={"title": "series", "works": [{"id": 1}, {"id": 2}, {"id": 3}], "published_total": 3},
    )
    client.discover_novel_series.assert_not_awaited()
    assert result["skipped_recent"] == 1
    assert result["queued"] == 2
    assert [call.args[1] for call in queue.enqueue.await_args_list] == [2, 3]
    assert all(call.kwargs["bookmark_if_missing"] is False for call in queue.enqueue.await_args_list)
    assert all(call.kwargs["source_ids"] == [7, 8] for call in queue.enqueue.await_args_list)
    assert {(row.work_id, row.source_id) for row in added if isinstance(row, WorkSource)} == {("1", 7), ("1", 8)}
    members = {row.work_id: row.status for row in added if isinstance(row, SeriesMember)}
    assert members == {"1": "archived", "2": "queued", "3": "queued"}


@pytest.mark.asyncio
async def test_manual_series_propagates_bookmark_intent_to_members():
    added = []
    series = Series(id="10", type="manga", title="old", author_id="1", author_name="author")
    session = SimpleNamespace(
        get=AsyncMock(return_value=series), add=added.append,
        flush=AsyncMock(), commit=AsyncMock(),
        execute=AsyncMock(side_effect=[Rows([]), Rows([])]),
    )
    queue = SimpleNamespace(
        redis=SimpleNamespace(exists=AsyncMock(return_value=False)),
        enqueue=AsyncMock(return_value={"deduplicated": False}),
    )
    result = await SeriesArchiveService(session, object(), queue).enqueue(
        "manga", 10, bookmark_if_missing=True,
        discovery={"title": "series", "works": [{"id": 1}, {"id": 2}]},
    )
    assert result["queued"] == 2
    assert all(call.kwargs["bookmark_if_missing"] is True for call in queue.enqueue.await_args_list)
    assert all(call.args[0] == WorkType.MANGA for call in queue.enqueue.await_args_list)


@pytest.mark.asyncio
async def test_cancelled_series_submit_restores_unpublished_members():
    added = []
    session = SimpleNamespace(
        get=AsyncMock(return_value=Series(id="10", type="novel", title="", author_id="1", author_name="")),
        add=added.append, flush=AsyncMock(), commit=AsyncMock(),
        execute=AsyncMock(side_effect=[Rows([]), Rows([])]),
    )
    queue = SimpleNamespace(
        redis=SimpleNamespace(exists=AsyncMock(return_value=False)),
        enqueue=AsyncMock(side_effect=[{"deduplicated": False}, asyncio.CancelledError()]),
    )
    with pytest.raises(asyncio.CancelledError):
        await SeriesArchiveService(session, object(), queue).enqueue(
            "novel", 10,
            discovery={"title": "series", "works": [{"id": 1}, {"id": 2}, {"id": 3}]},
        )
    members = {row.work_id: row.status for row in added if isinstance(row, SeriesMember)}
    assert members == {"1": "queued", "2": "discovered", "3": "discovered"}
