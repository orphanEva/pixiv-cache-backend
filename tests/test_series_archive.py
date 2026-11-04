from __future__ import annotations

import httpx
import pytest

from app.clients.pixivpy_client import PixivPyClient
from app.clients.pixiv_web_client import PixivWebClient
from app.core.logging import redact


@pytest.mark.asyncio
async def test_novel_series_paginates_and_deduplicates(monkeypatch):
    client = PixivPyClient()
    calls = []
    async def mock_call(fn, series_id, *, last_order=None):
        calls.append(last_order)
        if last_order is None:
            return {
                "novel_series_detail": {"title": "第一部", "content_count": 2},
                "novels": [{"id": 11, "series": {"order": 1}}],
                "next_url": "https://app-api.pixiv.net/v2/novel/series?last_order=1",
            }
        return {"novels": [{"id": 11}, {"id": 22, "series": {"order": 2}}], "next_url": None}
    async def mock_invoke(fn, url):
        return {"last_order": "1"}
    monkeypatch.setattr(client, "_call", mock_call)
    monkeypatch.setattr(client, "_invoke_api", mock_invoke)
    result = await client.discover_novel_series(55)
    assert calls == [None, "1"]
    assert result["title"] == "第一部"
    assert [x["id"] for x in result["works"]] == [11, 22]


@pytest.mark.asyncio
async def test_novel_series_rejects_nonadvancing_cursor(monkeypatch):
    client = PixivPyClient()
    async def mock_call(fn, series_id, *, last_order=None):
        return {"novels": [{"id": 1}], "next_url": "https://test/?last_order=1"}
    async def mock_invoke(fn, url):
        return {"last_order": "1"}
    monkeypatch.setattr(client, "_call", mock_call)
    monkeypatch.setattr(client, "_invoke_api", mock_invoke)
    with pytest.raises(ValueError, match="cursor"):
        await client.discover_novel_series(55)


@pytest.mark.asyncio
async def test_manga_series_and_membership_detected_from_web(monkeypatch):
    async def handler(request):
        if request.url.path == "/ajax/illust/100":
            return httpx.Response(200, json={"error": False, "body": {
                "illustType": 1, "seriesNavData": {"seriesId": 99, "title": "画集", "order": 3}
            }})
        assert request.url.path == "/ajax/series/99"
        assert request.url.params["p"] == "1"
        return httpx.Response(200, json={"error": False, "body": {
            "series": {"title": "画集"},
            "thumbnails": {"illust": [{"id": 100}, {"id": 101}]},
            "page": {"isLastPage": True},
        }})
    client = PixivPyClient()
    client.web = PixivWebClient(transport=httpx.MockTransport(handler))
    from app.models.work import WorkType
    assert await client.detect_series(WorkType.MANGA, 100) == ("manga", 99)
    result = await client.discover_manga_series(99)
    assert [x["id"] for x in result["works"]] == [100, 101]


def test_redact_configured_token(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "pixiv_refresh_token", "long-secret-token-for-test")
    assert "long-secret-token-for-test" not in redact("invalid long-secret-token-for-test")


@pytest.mark.asyncio
async def test_direct_novel_model_snapshot_and_series_detection(monkeypatch):
    """PixivPy >=3.7 novel_detail returns NovelInfo directly, not {'novel': ...}."""
    from types import SimpleNamespace
    from app.models.work import WorkType
    client = PixivPyClient()
    async def fake_call(fn, *args, **kwargs):
        if fn == client.api.novel_detail:
            return SimpleNamespace(
                id=2345, title="第一卷", caption="", tags=[], user={"id": 1, "name": "A"},
                series={"id": 991, "order": 3},
            )
        return {"novel_text": "正文"}
    monkeypatch.setattr(client, "_call", fake_call)
    assert await client.detect_series(WorkType.NOVEL, 2345) == ("novel", 991)
    snapshot = await client.get_novel_snapshot(2345)
    assert snapshot.series_id == "991"
    assert snapshot.text_content == "正文"


@pytest.mark.asyncio
async def test_illust_json_detail_is_read_correctly(monkeypatch):
    client = PixivPyClient()
    async def fake_call(fn, *args, **kwargs):
        return {"illust": {
            "id": 10, "title": "漫画", "type": "illust", "tags": [],
            "meta_single_page": {"original_image_url": "https://i.pximg.net/10_p0.jpg"},
            "user": {"id": 22, "name": "Author"}
        }}
    monkeypatch.setattr(client, "_call", fake_call)
    snap = await client.get_illust_snapshot(10)
    assert snap.pixiv_id == 10 and snap.title == "漫画"



@pytest.mark.asyncio
@pytest.mark.parametrize("error_class,expected_status", [
    ("PixivNotFoundError", 404),
    ("PixivRestrictedError", 403),
    ("PixivAuthError", 401),
    ("PixivUnavailableError", 503),
])
async def test_explicit_series_archive_maps_remote_errors_to_http_status(
    monkeypatch, error_class, expected_status
):
    """合集发现异常不能变成 FastAPI 未处理的 HTTP 500。"""
    from fastapi import HTTPException
    from app.api.jobs import enqueue_series_archive
    from app.core import errors
    from app.services.series_archive import SeriesArchiveService

    async def failed_discovery(self, kind, series_id, *, force_refresh=True, bookmark_if_missing=False):
        raise getattr(errors, error_class)("Pixiv series request failed")

    monkeypatch.setattr(SeriesArchiveService, "enqueue", failed_discovery)
    monkeypatch.setattr("app.api.jobs.queue", lambda: object())
    with pytest.raises(HTTPException) as caught:
        await enqueue_series_archive(
            series_type="novel",
            series_id=28356186,
            force_refresh=True,
            session=object(),
            client=object(),
        )
    assert caught.value.status_code == expected_status


@pytest.mark.asyncio
async def test_auto_expand_series_maps_not_found_to_http_404(monkeypatch):
    """单篇小说识别出合集后，合集失败也应该返回明确的 404。"""
    from fastapi import HTTPException
    from app.api.jobs import enqueue_archive_job
    from app.core.errors import PixivNotFoundError
    from app.models.work import WorkType
    from app.services.series_archive import SeriesArchiveService

    class FakePixiv:
        async def detect_series(self, kind, pixiv_id):
            assert kind == WorkType.NOVEL
            return ("novel", 28356186)

    async def failed_discovery(self, kind, series_id, *, force_refresh=True, bookmark_if_missing=False):
        raise PixivNotFoundError("This series no longer exists")

    monkeypatch.setattr(SeriesArchiveService, "enqueue", failed_discovery)
    monkeypatch.setattr("app.api.jobs.queue", lambda: object())
    with pytest.raises(HTTPException) as caught:
        await enqueue_archive_job(
            kind=WorkType.NOVEL,
            pixiv_id=123456,
            force_refresh=True,
            expand_series=True,
            session=object(),
            client=FakePixiv(),
        )
    assert caught.value.status_code == 404
    assert "no longer exists" in caught.value.detail
