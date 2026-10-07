from __future__ import annotations

import pytest

from app.models.work import WorkType
from app.schemas.pixiv import DiscoveredWork, DiscoveryPage
from app.services.source_sync import SourceSyncService


class FakeArchiveQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue(
        self,
        kind,
        pixiv_id,
        *,
        force_refresh=True,
        source_id=None,
        source_ids=None,
    ):
        self.jobs.append((kind, pixiv_id, force_refresh, source_id))
        return {"job_id": str(pixiv_id)}


@pytest.mark.asyncio
async def test_incremental_scan_stops_at_previous_frontier():
    service = SourceSyncService(None, None, None)
    queue = FakeArchiveQueue()
    service.archive_queue = queue

    pages = {
        None: DiscoveryPage(
            items=[
                DiscoveredWork(pixiv_id=5, work_type=WorkType.ILLUST),
                DiscoveredWork(pixiv_id=4, work_type=WorkType.MANGA),
            ],
            next_params={"offset": 30},
        ),
        30: DiscoveryPage(
            items=[
                DiscoveredWork(pixiv_id=3, work_type=WorkType.UGOIRA),
                DiscoveredWork(pixiv_id=2, work_type=WorkType.ILLUST),
            ],
            next_params=None,
        ),
    }

    async def fetch(params):
        return pages[None if params is None else params["offset"]]

    result = await service._scan(fetch, {"3"}, full=False, source_id=77)
    assert result["frontier"] == ["5", "4"]
    assert result["discovered"] == 2
    assert result["pages"] == 2
    assert result["stopped_at_frontier"] is True
    assert queue.jobs == [
        (WorkType.ILLUST, 5, True, 77),
        (WorkType.ILLUST, 4, True, 77),
    ]


@pytest.mark.asyncio
async def test_full_scan_ignores_frontier_and_archives_all_pages():
    service = SourceSyncService(None, None, None)
    queue = FakeArchiveQueue()
    service.archive_queue = queue

    pages = {
        None: DiscoveryPage(
            items=[
                DiscoveredWork(pixiv_id=5, work_type=WorkType.ILLUST),
                DiscoveredWork(pixiv_id=4, work_type=WorkType.MANGA),
            ],
            next_params={"offset": 30},
        ),
        30: DiscoveryPage(
            items=[
                DiscoveredWork(pixiv_id=3, work_type=WorkType.UGOIRA),
                DiscoveredWork(pixiv_id=2, work_type=WorkType.ILLUST),
            ],
            next_params=None,
        ),
    }

    async def fetch(params):
        return pages[None if params is None else params["offset"]]

    result = await service._scan(fetch, {"3"}, full=True, source_id=88)
    assert result["discovered"] == 4
    assert result["stopped_at_frontier"] is False
    assert [job[1] for job in queue.jobs] == [5, 4, 3, 2]
    assert all(job[0] == WorkType.ILLUST for job in queue.jobs)
    assert all(job[3] == 88 for job in queue.jobs)


@pytest.mark.asyncio
async def test_novel_discovery_keeps_novel_archive_kind():
    service = SourceSyncService(None, None, None)
    queue = FakeArchiveQueue()
    service.archive_queue = queue

    async def fetch(params):
        return DiscoveryPage(
            items=[
                DiscoveredWork(pixiv_id=101, work_type=WorkType.NOVEL),
                DiscoveredWork(pixiv_id=100, work_type=WorkType.NOVEL),
            ],
            next_params=None,
        )

    result = await service._scan(fetch, set(), full=False, source_id=99)
    assert result["frontier"] == ["101", "100"]
    assert queue.jobs == [
        (WorkType.NOVEL, 101, True, 99),
        (WorkType.NOVEL, 100, True, 99),
    ]
