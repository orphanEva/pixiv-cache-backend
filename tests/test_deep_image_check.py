from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.core.config import get_settings
from app.models.work import WorkType
from app.schemas.pixiv import RemoteAsset, RemoteSnapshot
from app.storage.local_storage import LocalStorage


@pytest.mark.asyncio
async def test_deep_image_check_matches_remote_binary(monkeypatch):
    settings = get_settings()
    storage = LocalStorage()
    payload = b"same-image-bytes"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=payload,
        )

    monkeypatch.setattr(
        storage,
        "_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    import hashlib
    sha = hashlib.sha256(payload).hexdigest()
    snapshot = RemoteSnapshot(
        pixiv_id=10,
        work_type=WorkType.ILLUST,
        title="x",
        version_token="a" * 64,
        assets=[RemoteAsset(page_index=0, url="https://i.pximg.net/a.jpg", filename="0.jpg")],
    )
    existing = [SimpleNamespace(page_index=0, sha256=sha, file_type="image")]
    assert await storage.remote_images_match(snapshot, existing) is True


@pytest.mark.asyncio
async def test_deep_image_check_detects_replaced_bytes(monkeypatch):
    storage = LocalStorage()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"new-bytes",
        )

    monkeypatch.setattr(
        storage,
        "_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    snapshot = RemoteSnapshot(
        pixiv_id=11,
        work_type=WorkType.ILLUST,
        title="x",
        version_token="a" * 64,
        assets=[RemoteAsset(page_index=0, url="https://i.pximg.net/a.jpg", filename="0.jpg")],
    )
    existing = [SimpleNamespace(page_index=0, sha256="0" * 64, file_type="image")]
    assert await storage.remote_images_match(snapshot, existing) is False
