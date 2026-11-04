from __future__ import annotations

import asyncio
import hashlib
import threading
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot, UgoiraFrame
from app.storage import local_storage as module


@pytest.fixture
def initial_storage(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "storage_min_free_bytes", 0)
    monkeypatch.setattr(settings, "ugoira_generate_mp4", True)
    monkeypatch.setattr(settings, "ugoira_keep_extracted_frames", False)
    storage = module.LocalStorage()
    fixture = tmp_path / "fixture.zip"
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("000000.jpg", b"frame")

    async def download(client, url, path, **kwargs):
        data = fixture.read_bytes()
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)

    monkeypatch.setattr(storage, "_download_stream", download)
    snapshot = RemoteSnapshot(pixiv_id=77, work_type=WorkType.UGOIRA, title="animation",
        version_token="a" * 64, ugoira_zip_url="https://example.invalid/never-requested.zip",
        ugoira_frames=[UgoiraFrame(file="000000.jpg", delay=100)])
    return storage, snapshot


@pytest.mark.asyncio
async def test_initial_ugoira_extraction_and_hash_leave_event_loop_available(initial_storage, monkeypatch):
    storage, snapshot = initial_storage
    main_thread = threading.get_ident()
    extract = storage._extract_ugoira_frames
    hash_file = storage._hash_file
    threaded = []

    def checked_extract(*args):
        assert threading.get_ident() != main_thread
        threaded.append("extract")
        return extract(*args)

    def checked_hash(*args):
        assert threading.get_ident() != main_thread
        threaded.append("hash")
        return hash_file(*args)

    async def convert(paths, frames, target, output):
        assert output.name.startswith(".preview-")
        assert not (target / "preview.mp4").exists()
        output.write_bytes(b"complete")

    monkeypatch.setattr(storage, "_extract_ugoira_frames", checked_extract)
    monkeypatch.setattr(storage, "_hash_file", checked_hash)
    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", convert)
    target, assets = await storage.materialize(snapshot, 1)
    assert threaded == ["extract", "hash"]
    assert (Path(target) / "preview.mp4").read_bytes() == b"complete"
    assert "ugoira_mp4" in {asset["file_type"] for asset in assets}
    assert not list(Path(target).glob(".preview-*.mp4"))


@pytest.mark.asyncio
async def test_initial_preview_cannot_publish_after_writer_lease_loss(initial_storage, monkeypatch):
    storage, snapshot = initial_storage
    lost = False

    async def alive():
        if lost:
            raise ArchiveMaintenanceError("writer lease lost")

    async def convert(paths, frames, target, output):
        nonlocal lost
        output.write_bytes(b"complete")
        lost = True

    monkeypatch.setattr(module, "require_writer_alive", alive)
    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", convert)
    with pytest.raises(ArchiveMaintenanceError):
        await storage.materialize(snapshot, 1)
    assert not storage.version_dir(WorkType.UGOIRA, 77, 1).exists()


@pytest.mark.asyncio
async def test_initial_conversion_timeout_preserves_raw_archive(initial_storage, monkeypatch):
    storage, snapshot = initial_storage

    async def timeout(paths, frames, target, output):
        output.write_bytes(b"partial")
        raise TimeoutError("conversion timeout")

    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", timeout)
    target, assets = await storage.materialize(snapshot, 1)
    assert {asset["file_type"] for asset in assets} == {"ugoira_zip", "ugoira_meta"}
    assert (Path(target) / "original.zip").is_file()
    assert (Path(target) / "ugoira_meta.json").is_file()
    assert not (Path(target) / "preview.mp4").exists()
    assert not list(Path(target).glob(".preview-*.mp4"))


@pytest.mark.asyncio
async def test_conversion_timeout_kills_and_reaps_ffmpeg(initial_storage, monkeypatch, tmp_path):
    storage, _ = initial_storage
    monkeypatch.setattr(get_settings(), "ugoira_conversion_timeout_seconds", 0.01)
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")

    class Process:
        returncode = None
        calls = 0
        killed = False

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            self.returncode = -9
            return b"", b""

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        await storage._convert_ugoira_to_mp4([frame], [UgoiraFrame(file=frame.name, delay=100)], tmp_path, tmp_path / "out.mp4")
    assert process.killed and process.calls == 2 and process.returncode == -9
    assert not (tmp_path / "ffmpeg_frames.txt").exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_filesystem_thread_before_cleanup(tmp_path):
    started, release = threading.Event(), threading.Event()
    written = tmp_path / "complete"

    def filesystem_write():
        started.set()
        assert release.wait(5)
        written.write_bytes(b"finished")

    task = asyncio.create_task(module.LocalStorage._local_thread(filesystem_write))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written.read_bytes() == b"finished"
