from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot
from app.storage.local_storage import LocalStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)
    monkeypatch.setattr(get_settings(), "storage_min_free_bytes", 0)
    return LocalStorage()


@pytest.mark.asyncio
async def test_cancelled_download_cleans_created_version(storage, monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(storage, "_write_images", cancelled)
    snapshot = RemoteSnapshot(pixiv_id=42, work_type=WorkType.ILLUST, version_token="a" * 64)
    with pytest.raises(asyncio.CancelledError):
        await storage.materialize(snapshot, 1)
    assert not storage.version_dir(WorkType.ILLUST, 42, 1).exists()


@pytest.mark.asyncio
async def test_quarantined_crash_remnant_allows_same_version_retry(storage):
    target = storage.version_dir(WorkType.NOVEL, 42, 1)
    target.mkdir(parents=True)
    (target / "novel.txt").write_text("incomplete", encoding="utf-8")
    moved = storage.quarantine_uncommitted_version(WorkType.NOVEL, 42, 1)
    assert moved is not None
    assert (moved / "novel.txt").read_text(encoding="utf-8") == "incomplete"
    assert not target.exists()
    snapshot = RemoteSnapshot(
        pixiv_id=42, work_type=WorkType.NOVEL, text_content="complete", version_token="a" * 64,
    )
    path, _ = await storage.materialize(snapshot, 1)
    assert (Path(path) / "novel.txt").read_text(encoding="utf-8") == "complete"


def test_quarantine_missing_version_is_noop(storage):
    assert storage.quarantine_uncommitted_version(WorkType.NOVEL, 42, 1) is None
