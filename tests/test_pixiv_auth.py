from __future__ import annotations

import json

import httpx
import pytest

from app.clients.pixiv_web_client import PixivWebClient
from app.clients.pixivpy_client import PixivPyClient
from app.core.errors import PixivAuthError


@pytest.mark.asyncio
async def test_web_auth_probe_sends_cookie_but_never_returns_it():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        assert request.url.path == "/ajax/user/extra"
        return httpx.Response(200, json={"error": False, "body": {"userId": "123"}})

    client = PixivWebClient(
        "PHPSESSID=super-secret; device_token=abc",
        transport=httpx.MockTransport(handler),
    )
    status = await client.check_auth()
    assert seen["cookie"] == "PHPSESSID=super-secret; device_token=abc"
    assert status.configured is True
    assert status.authenticated is True
    assert status.status == "ok"
    assert "secret" not in repr(status)


@pytest.mark.asyncio
async def test_web_auth_probe_detects_expired_cookie():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": True, "message": "login required", "body": None})

    client = PixivWebClient("PHPSESSID=expired", transport=httpx.MockTransport(handler))
    status = await client.check_auth()
    assert status.configured is True
    assert status.authenticated is False
    assert status.status == "pixiv_rejected"


@pytest.mark.asyncio
async def test_ugoira_web_metadata_retries_anonymously_after_cookie_failure():
    cookie_headers = []

    async def handler(request: httpx.Request) -> httpx.Response:
        cookie_headers.append(request.headers.get("cookie"))
        if request.headers.get("cookie"):
            return httpx.Response(403, json={"error": True})
        return httpx.Response(200, json={
            "error": False,
            "body": {
                "originalSrc": "https://i.pximg.net/original.zip",
                "src": "https://i.pximg.net/medium.zip",
                "frames": [{"file": "000000.jpg", "delay": 100}],
            },
        })

    client = PixivWebClient("PHPSESSID=expired", transport=httpx.MockTransport(handler))
    result = await client.get_ugoira_metadata(123)
    assert cookie_headers == ["PHPSESSID=expired", None]
    assert result["ugoira_metadata"]["zip_urls"]["original"].endswith("original.zip")


def test_web_cookie_rejects_header_injection():
    with pytest.raises(ValueError):
        PixivWebClient("PHPSESSID=x\r\nX-Evil: injected")


@pytest.mark.asyncio
async def test_dual_auth_status_contains_no_credentials(monkeypatch):
    client = PixivPyClient()
    client.refresh_token = "refresh-secret-value"

    async def fake_auth(force=False):
        return None

    class FakeProbe:
        configured = True
        authenticated = True
        status = "ok"

    async def fake_web_auth():
        return FakeProbe()

    monkeypatch.setattr(client, "_ensure_auth", fake_auth)
    monkeypatch.setattr(client.web, "check_auth", fake_web_auth)

    result = await client.auth_status()
    encoded = json.dumps(result)
    assert result == {
        "app_api": {"configured": True, "authenticated": True, "status": "ok"},
        "web_cookie": {"configured": True, "authenticated": True, "status": "ok"},
    }
    assert "refresh-secret-value" not in encoded
    assert "PHPSESSID" not in encoded


@pytest.mark.asyncio
async def test_dual_auth_status_reports_missing_app_token(monkeypatch):
    client = PixivPyClient()
    client.refresh_token = ""

    class FakeProbe:
        configured = False
        authenticated = False
        status = "not_configured"

    async def fake_web_auth():
        return FakeProbe()

    monkeypatch.setattr(client.web, "check_auth", fake_web_auth)
    result = await client.auth_status()
    assert result["app_api"] == {
        "configured": False,
        "authenticated": False,
        "status": "not_configured",
    }


@pytest.mark.asyncio
async def test_app_api_requires_refresh_token_only_when_called():
    client = PixivPyClient()
    client.refresh_token = ""
    with pytest.raises(PixivAuthError, match="not configured"):
        await client._ensure_auth()
