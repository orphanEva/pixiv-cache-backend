"""小说追更列表同步单元测试：用 Pixiv MockTransport，绝不调用真实账号。"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.clients.pixiv_web_client import PixivWebClient
from app.core.config import get_settings
from app.core.errors import PixivAuthError
from app.models.work import SyncSourceType
from app.services.cache_service import now_utc
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
    monkeypatch.setattr(get_settings(), "account_migration_enabled", False)
    calls = []

    class FakeSession:
        async def commit(self):
            pass

    class FakeWeb:
        async def get_watched_novel_series_page(self, page):
            if page == 1:
                return {"series_ids": [101, 102], "has_next": False}
            raise AssertionError("unexpected extra page")

    class Client:
        web = FakeWeb()
        async def get_authenticated_web_user_id(self, **kwargs):
            return 99

    async def fake_series_enqueue(self, kind, sid, **options):
        calls.append((kind, sid, options))
        return {"discovered": 4, "queued": 1, "already_archived": 3}

    monkeypatch.setattr(
        "app.services.series_archive.SeriesArchiveService.enqueue",
        fake_series_enqueue,
    )
    service = SourceSyncService(FakeSession(), Client(), None)
    source = SimpleNamespace(
        id=7, source_type=SyncSourceType.WATCHLIST,
        remote_user_id="novel", include_novel=True, include_illust=False,
        account_id="99", scan_state_json=None,
        interval_seconds=3600,
        frontier_json={"novel_series": ["101", "102"]},
        last_run_at=None, last_error=None,
        last_success_at=now_utc(), next_run_at=None,
    )
    for _ in range(2):
        outcome = await service.run_source(source)
        assert outcome["series"] == 2
        assert outcome["archive_jobs"] == 2
        assert source.frontier_json == {"novel_series": ["101", "102"]}
    assert len(calls) == 4
    assert all(x[2] == {
        "force_refresh": False, "only_missing": True, "source_id": 7, "bookmark_if_missing": False,
    } for x in calls)


@pytest.mark.asyncio
async def test_inaccessible_watched_series_does_not_block_other_series(monkeypatch):
    monkeypatch.setattr(get_settings(), "account_migration_enabled", False)
    from app.core.errors import PixivNotFoundError
    calls = []

    class Session:
        async def commit(self): pass
        async def rollback(self): pass
        async def get(self, cls, source_id): return source

    class Web:
        async def get_watched_novel_series_page(self, page):
            return {"series_ids": [101, 102], "has_next": False}

    class Client:
        web = Web()
        async def get_authenticated_web_user_id(self, **kwargs):
            return 99

    async def enqueue(self, kind, sid, **kwargs):
        calls.append(sid)
        if sid == 101:
            raise PixivNotFoundError("deleted")
        return {"discovered": 2, "queued": 2, "already_archived": 0}

    monkeypatch.setattr("app.services.series_archive.SeriesArchiveService.enqueue", enqueue)
    source = SimpleNamespace(
        id=9, source_type=SyncSourceType.WATCHLIST, remote_user_id="novel",
        include_novel=True, include_illust=False, interval_seconds=3600, frontier_json={},
        account_id="99", scan_state_json=None,
        last_run_at=None, last_error=None, last_success_at=None, next_run_at=None,
    )
    service = SourceSyncService(Session(), Client(), None)
    with pytest.raises(ValueError, match="Unable to archive 1 watched novel series"):
        await service.run_source(source)
    assert calls == [101, 102]
    assert source.last_error and "101" in source.last_error
    assert source.scan_state_json["processed"] == [102]


@pytest.mark.asyncio
async def test_legacy_watchlist_accepts_current_shape_and_uses_global_limiter():
    class Limiter:
        calls = 0
        async def wait(self): self.calls += 1
        async def penalize(self, value=None): raise AssertionError("unexpected cooldown")
    limiter = Limiter()
    def handler(request):
        return httpx.Response(200, json={"error": False, "body": {
            "page": {"total": "1", "maxPage": 1, "watchedSeriesIds": ["101"]},
            "thumbnails": {"novelSeries": [{"id": "101", "title": "series"}]},
        }})
    web = PixivWebClient("PHPSESSID=example", transport=httpx.MockTransport(handler), rate_limiter=limiter)
    assert await web.get_watched_novel_series_page(1) == {
        "series_ids": [101], "has_next": False, "total": 1, "max_page": 1,
    }
    assert limiter.calls == 1


@pytest.mark.asyncio
async def test_legacy_watchlist_budget_resume_preserves_strict_first_scan(monkeypatch):
    monkeypatch.setattr(get_settings(), "account_migration_enabled", False)
    monkeypatch.setattr(get_settings(), "sync_max_pages_per_run", 1)
    monkeypatch.setattr(get_settings(), "sync_page_delay_seconds", 0)
    pages, archives = [], []
    class Session:
        async def commit(self): pass
    class Web:
        async def get_watched_novel_series_page(self, page):
            pages.append(page)
            return {"series_ids": [100 + page], "has_next": page < 2, "total": 2, "max_page": 2}
    class Client:
        web = Web()
        async def get_authenticated_web_user_id(self, **kwargs): return 99
    async def enqueue(self, kind, series_id, **kwargs):
        archives.append((series_id, kwargs))
        return {"discovered": 1, "queued": 1, "already_archived": 1}
    monkeypatch.setattr("app.services.series_archive.SeriesArchiveService.enqueue", enqueue)
    source = SimpleNamespace(
        id=7, source_type=SyncSourceType.WATCHLIST, remote_user_id="novel",
        include_novel=True, include_illust=False, account_id="99", interval_seconds=3600,
        frontier_json=None, scan_state_json=None, last_success_at=None, last_error=None,
    )
    first = await SourceSyncService(Session(), Client(), None).run_source(source)
    assert not first["complete"] and source.frontier_json is None and not archives
    assert source.scan_state_json["page"] == 2
    second = await SourceSyncService(Session(), Client(), None).run_source(source)
    assert not second["complete"] and source.frontier_json is None
    assert source.scan_state_json["processed"] == [101]
    third = await SourceSyncService(Session(), Client(), None).run_source(source)
    assert third["complete"] and third["strict_remote_check"]
    assert pages == [1, 2]
    assert [sid for sid, _ in archives] == [101, 102]
    assert all(options["force_refresh"] and not options["only_missing"] for _, options in archives)
    assert source.frontier_json == {"novel_series": ["101", "102"]}
    assert source.scan_state_json is None


@pytest.mark.asyncio
async def test_legacy_watchlist_new_account_discards_old_checkpoint(monkeypatch):
    monkeypatch.setattr(get_settings(), "account_migration_enabled", False)
    class Session:
        async def commit(self): pass
    class Client:
        async def get_authenticated_web_user_id(self, **kwargs): return 100
    source = SimpleNamespace(source_type=SyncSourceType.WATCHLIST, remote_user_id="novel",
        account_id="99", frontier_json={"novel_series": ["101"]},
        scan_state_json={"mode": "novel_watchlist", "page": 3}, last_success_at=now_utc())
    assert await SourceSyncService(Session(), Client(), None)._bind_self_account(source) == "100"
    assert source.account_id == "100"
    assert source.frontier_json is None and source.scan_state_json is None and source.last_success_at is None
