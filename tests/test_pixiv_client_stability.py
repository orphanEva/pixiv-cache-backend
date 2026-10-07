from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.clients.pixivpy_client import PixivPyClient
from app.core.errors import (
    PixivAuthError,
    PixivNotFoundError,
    PixivRestrictedError,
    PixivUnavailableError,
)


@pytest.mark.asyncio
async def test_shared_pixivpy_instance_is_serialized(monkeypatch):
    client = PixivPyClient()
    client.refresh_token = "token"
    client._next_auth_at = 10**12

    active = 0
    max_active = 0

    def blocking_call(value):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        import time
        time.sleep(0.03)
        active -= 1
        return {"value": value}

    results = await asyncio.gather(
        client._call(blocking_call, 1),
        client._call(blocking_call, 2),
        client._call(blocking_call, 3),
    )
    assert results == [{"value": 1}, {"value": 2}, {"value": 3}]
    assert max_active == 1


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"error": {"message": "OAuth access token invalid"}}, PixivAuthError),
        ({"error": {"message": "404 work not found"}}, PixivNotFoundError),
        ({"error": {"user_message": "This work is private"}}, PixivRestrictedError),
        ({"error": {"message": "429 too many requests"}}, PixivUnavailableError),
    ],
)
def test_pixiv_error_payloads_are_mapped(payload, error_type):
    client = PixivPyClient()
    with pytest.raises(error_type):
        client._raise_for_pixiv_error(payload)


def test_pixiv_success_payload_is_not_rejected():
    client = PixivPyClient()
    client._raise_for_pixiv_error({"error": False, "illust": {"id": 1}})


@pytest.mark.asyncio
async def test_error_payload_from_sdk_is_checked(monkeypatch):
    client = PixivPyClient()
    client.refresh_token = "token"
    client._next_auth_at = 10**12

    def returns_business_error():
        return SimpleNamespace(error={"message": "404 not found"})

    with pytest.raises(PixivNotFoundError):
        await client._call(returns_business_error)


@pytest.mark.asyncio
async def test_business_auth_error_payload_retries_after_forced_auth(monkeypatch):
    client = PixivPyClient()
    client.refresh_token = "token"
    client._next_auth_at = 10**12
    calls = {"api": 0, "auth": 0}

    async def fake_ensure_auth(force=False):
        if force:
            calls["auth"] += 1

    def payload_call():
        calls["api"] += 1
        if calls["api"] == 1:
            return {"error": {"message": "401 unauthorized access token"}}
        return {"error": False, "ok": True}

    monkeypatch.setattr(client, "_ensure_auth", fake_ensure_auth)
    result = await client._call(payload_call)
    assert result["ok"] is True
    assert calls == {"api": 2, "auth": 1}



@pytest.mark.asyncio
async def test_discovery_page_maps_remote_work_types_and_pagination(monkeypatch):
    from app.models.work import WorkType

    client = PixivPyClient()

    async def fake_invoke(func, *args, **kwargs):
        return {"offset": "30"}

    monkeypatch.setattr(client, "_invoke_api", fake_invoke)
    page = await client._discovery_page(
        {
            "illusts": [
                {"id": 1, "type": "illust"},
                {"id": 2, "type": "manga"},
                {"id": 3, "type": "ugoira"},
            ],
            "next_url": "https://app-api.pixiv.net/v1/user/illusts?offset=30",
        },
        "illusts",
        WorkType.ILLUST,
    )

    assert [(item.pixiv_id, item.work_type) for item in page.items] == [
        (1, WorkType.ILLUST),
        (2, WorkType.MANGA),
        (3, WorkType.UGOIRA),
    ]
    assert page.next_params == {"offset": "30"}
