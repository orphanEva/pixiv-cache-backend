from pathlib import Path

import pytest

from app.core.config import get_settings
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot
from app.storage.local_storage import LocalStorage


@pytest.mark.asyncio
async def test_novel_materialize(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    settings.storage_root = tmp_path
    try:
        storage = LocalStorage()
        snapshot = RemoteSnapshot(
            pixiv_id=123,
            work_type=WorkType.NOVEL,
            title="demo",
            text_content="hello pixiv",
            version_token="x" * 64,
        )
        root, assets = await storage.materialize(snapshot, 1)
        assert Path(root).exists()
        assert Path(assets[0]["local_path"]).read_text() == "hello pixiv"
        assert assets[0]["size_bytes"] == len("hello pixiv".encode())
        assert len(assets[0]["sha256"]) == 64
    finally:
        settings.storage_root = old_root
