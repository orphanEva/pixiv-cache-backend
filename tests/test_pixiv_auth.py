from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.auth.pixiv_credentials import AutoAuthError, PixivCredentialManager
from app.clients.pixiv_web_client import PixivWebClient
from app.clients.pixivpy_client import PixivPyClient
from app.core.config import get_settings
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
    assert status.authenticated is True
    assert "secret" not in repr(status)


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


@pytest.fixture
def auto_auth_settings(tmp_path: Path):
    settings = get_settings()
    old = {
        "pixiv_auto_auth": settings.pixiv_auto_auth,
        "pixiv_username": settings.pixiv_username,
        "pixiv_password": settings.pixiv_password,
        "pixiv_totp_secret": settings.pixiv_totp_secret,
        "pixiv_refresh_token": settings.pixiv_refresh_token,
        "pixiv_cookie": settings.pixiv_cookie,
        "pixiv_auth_cache_dir": settings.pixiv_auth_cache_dir,
        "pixiv_auto_login_cooldown_seconds": settings.pixiv_auto_login_cooldown_seconds,
    }
    settings.pixiv_auto_auth = True
    settings.pixiv_username = "user@example.com"
    settings.pixiv_password = "password-only-in-test-memory"
    settings.pixiv_totp_secret = ""
    settings.pixiv_refresh_token = ""
    settings.pixiv_cookie = ""
    settings.pixiv_auth_cache_dir = tmp_path / "auth"
    settings.pixiv_auto_login_cooldown_seconds = 900
    try:
        yield settings
    finally:
        for key, value in old.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_gppt_login_is_cached_privately(monkeypatch, auto_auth_settings):
    manager = PixivCredentialManager()

    def fake_login(username, password, totp_secret, *, headless):
        assert username == "user@example.com"
        assert password == "password-only-in-test-memory"
        assert headless is True
        return SimpleNamespace(refresh_token="refresh-from-gppt")

    monkeypatch.setattr("app.auth.pixiv_credentials.gppt.login", fake_login)
    token = await manager.recover_refresh_token(force_login=True)
    assert token == "refresh-from-gppt"
    assert manager.current_refresh_token() == "refresh-from-gppt"

    cached = manager.token_file.read_text()
    assert "password-only-in-test-memory" not in cached
    assert "user@example.com" not in cached
    if os.name != "nt":
        assert manager.token_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_gppt_refresh_precedes_relogin(monkeypatch, auto_auth_settings):
    manager = PixivCredentialManager()
    manager._save_refresh_token("old-refresh")
    calls = []

    def fake_refresh(value):
        calls.append(("refresh", value))
        return SimpleNamespace(refresh_token="rotated-refresh")

    def forbidden_login(*args, **kwargs):
        raise AssertionError("login should not be called while refresh works")

    monkeypatch.setattr("app.auth.pixiv_credentials.gppt.refresh", fake_refresh)
    monkeypatch.setattr("app.auth.pixiv_credentials.gppt.login", forbidden_login)

    result = await manager.recover_refresh_token()
    assert result == "rotated-refresh"
    assert calls == [("refresh", "old-refresh")]


@pytest.mark.asyncio
async def test_browser_cookie_is_cached_without_account_password(monkeypatch, auto_auth_settings):
    manager = PixivCredentialManager()

    async def fake_browser(username, password, totp):
        assert username == "user@example.com"
        assert password == "password-only-in-test-memory"
        return "PHPSESSID=session123; device_token=device456"

    monkeypatch.setattr(manager, "_browser_login_cookie", fake_browser)
    cookie = await manager.recover_cookie()
    assert cookie.startswith("PHPSESSID=")
    cached = manager.cookie_file.read_text()
    assert "password-only-in-test-memory" not in cached
    assert "user@example.com" not in cached


def test_totp_supports_base32_and_otpauth_uri():
    code1 = PixivCredentialManager._totp_code("JBSWY3DPEHPK3PXP")
    code2 = PixivCredentialManager._totp_code(
        "otpauth://totp/Pixiv:test?secret=JBSWY3DPEHPK3PXP&issuer=Pixiv"
    )
    assert len(code1) == 6 and code1.isdigit()
    assert len(code2) == 6 and code2.isdigit()


@pytest.mark.asyncio
async def test_dual_auth_status_contains_no_credentials(monkeypatch, auto_auth_settings):
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
    monkeypatch.setattr(client.credentials, "state", lambda: SimpleNamespace(
        auto_enabled=True,
        username_configured=True,
        password_configured=True,
        totp_configured=False,
        refresh_token_available=True,
        cookie_available=True,
    ))
    monkeypatch.setattr(client.credentials, "current_cookie", lambda: "PHPSESSID=web-secret")

    result = await client.auth_status()
    encoded = json.dumps(result)
    assert result["auto_auth"]["enabled"] is True
    assert result["app_api"]["authenticated"] is True
    assert result["web_cookie"]["authenticated"] is True
    for secret in ("refresh-secret-value", "web-secret", "PHPSESSID", "password-only-in-test-memory"):
        assert secret not in encoded


@pytest.mark.asyncio
async def test_app_api_manual_mode_still_fails_cleanly_without_token(monkeypatch):
    settings = get_settings()
    old_auto, old_token = settings.pixiv_auto_auth, settings.pixiv_refresh_token
    settings.pixiv_auto_auth = False
    settings.pixiv_refresh_token = ""
    try:
        client = PixivPyClient()
        client.refresh_token = ""
        with pytest.raises(PixivAuthError, match="unavailable"):
            await client._ensure_auth()
    finally:
        settings.pixiv_auto_auth, settings.pixiv_refresh_token = old_auto, old_token


@pytest.mark.asyncio
async def test_login_cooldown_prevents_tight_retry_loop(auto_auth_settings):
    manager = PixivCredentialManager()
    manager._last_web_login_failure = __import__("time").monotonic()
    with pytest.raises(AutoAuthError, match="login_cooldown"):
        await manager.recover_cookie()
