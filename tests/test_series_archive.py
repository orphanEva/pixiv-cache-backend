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
