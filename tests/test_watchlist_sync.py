"""小说追更列表同步单元测试：用 Pixiv MockTransport，绝不调用真实账号。"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.clients.pixiv_web_client import PixivWebClient
from app.core.errors import PixivAuthError
from app.models.work import SyncSourceType
from app.services.source_sync import SourceSyncService


@pytest.mark.asyncio
async def test_watchlist_pages_use_cookie_and_extract_series_ids():
    requested = []
    async def handler(request):
        assert request.headers["cookie"] == "PHPSESSID=example"
        requested.append(request.url.params["p"])
        if request.url.params["p"] == "1":
            return httpx.Response(200, json={"error": False, "body": {
                "page": {"series": [{"seriesId": "101"}, {"seriesId": "102"}], "hasNext": True}
            }})
        return httpx.Response(200, json={"error": False, "body": {
            "page": {"series": [{"seriesId": "103"}], "hasNext": False}
        }})
    web = PixivWebClient("PHPSESSID=example", transport=httpx.MockTransport(handler))
    first = await web.get_watched_novel_series_page(1)
    second = await web.get_watched_novel_series_page(2)
    assert first == {"series_ids": [101, 102], "has_next": True}
    assert second == {"series_ids": [103], "has_next": False}
    assert requested == ["1", "2"]


@pytest.mark.asyncio
async def test_watchlist_rejects_unauthenticated_and_unknown_format():
    no_cookie = PixivWebClient("")
    with pytest.raises(PixivAuthError, match="PIXIV_COOKIE"):
        await no_cookie.get_watched_novel_series_page(1)

    async def handler(request):
        # 作品列表不是系列列表，不能把 novel.id 当 series.id。
        return httpx.Response(200, json={"error": False, "body": {
            "thumbnails": {"novel": [{"id": "999"}]}
        }})
    web = PixivWebClient("PHPSESSID=example", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="series list"):
        await web.get_watched_novel_series_page(1)


@pytest.mark.asyncio
async def test_watchlist_rejects_expired_cookie():
    async def handler(request):
        return httpx.Response(403, json={"error": True})
    web = PixivWebClient("PHPSESSID=expired", transport=httpx.MockTransport(handler))
    with pytest.raises(PixivAuthError):
        await web.get_watched_novel_series_page(1)


@pytest.mark.asyncio
async def test_watchlist_sync_rescans_existing_series_instead_of_stopping_at_frontier(monkeypatch):
    calls = []

    class FakeSession:
        async def commit(self):
            pass

    class FakeWeb:
        async def get_watched_novel_series_page(self, page):
            if page == 1:
                return {"series_ids": [101, 102], "has_next": False}
            raise AssertionError("unexpected extra page")

    async def fake_series_enqueue(self, kind, sid, **options):
        calls.append((kind, sid, options))
        return {"discovered": 4, "queued": 1, "already_archived": 3}

    monkeypatch.setattr(
        "app.services.series_archive.SeriesArchiveService.enqueue",
        fake_series_enqueue,
    )
    service = SourceSyncService(FakeSession(), SimpleNamespace(web=FakeWeb()), None)
    source = SimpleNamespace(
        id=7, source_type=SyncSourceType.WATCHLIST,
        remote_user_id="novel", include_novel=True,
        interval_seconds=3600,
        frontier_json={"novel_series": ["101", "102"]},
        last_run_at=None, last_error=None,
        last_success_at=None, next_run_at=None,
    )
    for _ in range(2):
        outcome = await service.run_source(source)
        assert outcome["series"] == 2
        assert outcome["archive_jobs"] == 2
        assert source.frontier_json == {"novel_series": ["101", "102"]}
    assert len(calls) == 4
    assert all(x[2] == {
        "force_refresh": False, "only_missing": True, "source_id": 7
    } for x in calls)


@pytest.mark.asyncio
async def test_inaccessible_watched_series_does_not_block_other_series(monkeypatch):
    from app.core.errors import PixivNotFoundError
    calls = []

    class Session:
        async def commit(self): pass
        async def rollback(self): pass
        async def get(self, cls, source_id): return source

    class Web:
        async def get_watched_novel_series_page(self, page):
            return {"series_ids": [101, 102], "has_next": False}

    async def enqueue(self, kind, sid, **kwargs):
        calls.append(sid)
        if sid == 101:
            raise PixivNotFoundError("deleted")
        return {"discovered": 2, "queued": 2, "already_archived": 0}

    monkeypatch.setattr("app.services.series_archive.SeriesArchiveService.enqueue", enqueue)
    source = SimpleNamespace(
        id=9, source_type=SyncSourceType.WATCHLIST, remote_user_id="novel",
        include_novel=True, interval_seconds=3600, frontier_json={},
        last_run_at=None, last_error=None, last_success_at=None, next_run_at=None,
    )
    service = SourceSyncService(Session(), SimpleNamespace(web=Web()), None)
    with pytest.raises(ValueError, match="Unable to archive 1 watched novel series"):
        await service.run_source(source)
    assert calls == [101, 102]
    assert source.last_error and "101" in source.last_error
