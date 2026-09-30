from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app import main
from app.api.media import get_media
from app.core.config import get_settings
from app.models.work import WorkStatus, WorkType


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, query):
        return Result(self.rows)


def test_key_blocks_all_non_health_routes(monkeypatch):
    monkeypatch.setattr(main.settings, "api_key", "S" * 32)
    client = TestClient(main.app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/admin/cache/status").status_code == 401
    assert client.get("/media/illust/1/versions/1/0.jpg").status_code == 401
    assert client.get("/api/admin/cache/status", headers={"X-API-Key": "wrong"}).status_code == 401


def test_no_key_fails_closed(monkeypatch):
    monkeypatch.setattr(main.settings, "api_key", "")
    client = TestClient(main.app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/admin/cache/status").status_code == 503


@pytest.mark.asyncio
async def test_media_denies_revoked_and_allows_registered_file(tmp_path: Path):
    settings = get_settings()
    old = settings.storage_root
    settings.storage_root = tmp_path
    path = tmp_path / "illust/1/versions/1/0.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake-image")
    try:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await get_media(WorkType.ILLUST, 1, 1, "0.jpg", Session([(str(path), WorkStatus.RESTRICTED)]))
        assert exc.value.status_code == 403
        response = await get_media(WorkType.ILLUST, 1, 1, "0.jpg", Session([(str(path), WorkStatus.ACTIVE)]))
        assert response.path == str(path)
        with pytest.raises(HTTPException) as exc:
            await get_media(WorkType.ILLUST, 1, 1, "other.jpg", Session([(str(path), WorkStatus.ACTIVE)]))
        assert exc.value.status_code == 404
    finally:
        settings.storage_root = old
