from __future__ import annotations

import json

import httpx
import pytest

from app.clients.pixiv_web_client import PixivWebClient
from app.clients.pixivpy_client import PixivPyClient
from app.core.errors import PixivAuthError


@pytest.mark.asyncio
async def test_web_auth_probe_sends_cookie_without_echoing_it():
    async def handler(request):
        assert request.headers["cookie"] == "PHPSESSID=secret"
        return httpx.Response(200, json={"error": False, "body": {"userId": "123"}})
    client = PixivWebClient("PHPSESSID=secret", transport=httpx.MockTransport(handler))
    result = await client.check_auth()
    assert result.authenticated is True
    assert "secret" not in repr(result)


@pytest.mark.asyncio
async def test_ugoira_retries_anonymously_after_cookie_failure():
    cookies = []
    async def handler(request):
        cookies.append(request.headers.get("cookie"))
        if request.headers.get("cookie"):
            return httpx.Response(403, json={"error": True})
        return httpx.Response(200, json={"error": False, "body": {
            "originalSrc": "https://i.pximg.net/test.zip",
            "frames": [{"file": "000000.jpg", "delay": 100}],
        }})
    client = PixivWebClient("PHPSESSID=expired", transport=httpx.MockTransport(handler))
    assert await client.get_ugoira_metadata(10) is not None
    assert cookies == ["PHPSESSID=expired", None]


def test_cookie_header_injection_is_rejected():
    with pytest.raises(ValueError):
        PixivWebClient("PHPSESSID=x\r\nInjected: yes")


@pytest.mark.asyncio
async def test_auth_status_contains_no_credentials(monkeypatch):
    client = PixivPyClient()
    client.refresh_token = "super-secret-refresh-token"
    async def fake_auth(force=False):
        return None
    class Probe:
        configured = True
        authenticated = True
        status = "ok"
    async def fake_probe():
        return Probe()
    monkeypatch.setattr(client, "_ensure_auth", fake_auth)
    monkeypatch.setattr(client.web, "check_auth", fake_probe)
    data = await client.auth_status()
    assert data["app_api"]["authenticated"] is True
    assert data["web_cookie"]["authenticated"] is True
    assert "auto_auth" not in data
    assert "super-secret" not in json.dumps(data)


@pytest.mark.asyncio
async def test_without_token_no_password_login_attempt():
    client = PixivPyClient()
    client.refresh_token = ""
    client._next_auth_at = 0
    with pytest.raises(PixivAuthError, match="PIXIV_REFRESH_TOKEN"):
        await client._ensure_auth()
