from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import threading
import time

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError
from app.core.maintenance import MaintenanceWriter, writers_key
from app.services.storage_integrity import StorageIntegrityService


class Session:
    def __init__(self, rows=()):
        self.rows = rows
        self.thread_id = None

    async def execute(self, statement):
        self.thread_id = threading.get_ident()
        return self

    def all(self):
        return self.rows


@pytest_asyncio.fixture
async def safety(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "maintenance_writer_ttl_seconds", 3)
    monkeypatch.setattr(settings, "storage_part_stale_seconds", 1)
    monkeypatch.setattr(settings, "storage_orphan_grace_seconds", 1)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield tmp_path, redis
    finally:
        await redis.aclose()


def stale(path: Path, content=b"partial"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    earlier = time.time() - 120
    os.utime(path, (earlier, earlier))
    os.utime(path.parent, (earlier, earlier))
    return path


def findings(*, parts=(), orphans=()):
    return {
        "db_files": set(), "db_version_dirs": set(), "missing_files": [],
        "hash_mismatches": [], "outside_root": [],
        "orphan_version_dirs": list(orphans), "part_files": list(parts),
    }


@pytest.mark.asyncio
async def test_slow_hash_scan_does_not_block_writer_renewal(safety, monkeypatch):
    root, redis = safety
    path = stale(root / "illust/1/versions/1/0.jpg", b"image")
    sha = hashlib.sha256(b"image").hexdigest()
    session = Session([(str(path), sha, str(path.parent), "1", 1)])
    service = StorageIntegrityService(session)
    started, release = threading.Event(), threading.Event()
    caller_thread = threading.get_ident()
    hash_threads = []

    def blocking_hash(target):
        hash_threads.append(threading.get_ident())
        started.set()
        assert release.wait(5), "Test did not release the hash worker"
        return sha

    monkeypatch.setattr(service, "_sha256", blocking_hash)
    async with MaintenanceWriter(redis) as writer:
        initial_deadline = await redis.zscore(writers_key(), writer.token)
        audit = asyncio.create_task(service.audit(verify_hash=True))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.sleep(1.15)  # A 3-second lease renews every second.
            assert await redis.zscore(writers_key(), writer.token) > initial_deadline
            await writer.ensure_alive()
            assert session.thread_id == caller_thread
            assert hash_threads and hash_threads[0] != caller_thread
        finally:
            release.set()
            result = await audit
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_lost_writer_is_checked_after_scan_before_repair(safety, monkeypatch):
    root, redis = safety
    part = stale(root / "pending.part")
    service = StorageIntegrityService(Session())
    async with MaintenanceWriter(redis) as writer:
        async def collect(**kwargs):
            writer._lost = True
            return findings(parts=[part])

        monkeypatch.setattr(service, "_collect", collect)
        with pytest.raises(ArchiveMaintenanceError):
            await service.audit(repair_safe=True)
    assert part.read_bytes() == b"partial"


@pytest.mark.asyncio
async def test_lost_writer_stops_next_unlink_and_quarantine(safety, monkeypatch):
    root, redis = safety
    first = stale(root / "first.part")
    second = stale(root / "second.part")
    orphan = root / "illust/3/versions/1"
    stale(orphan / "orphan.jpg")
    service = StorageIntegrityService(Session())
    original_unlink = Path.unlink
    async with MaintenanceWriter(redis) as writer:
        async def collect(**kwargs):
            return findings(parts=[first, second], orphans=[orphan])

        def lose_after_first(path, *args, **kwargs):
            result = original_unlink(path, *args, **kwargs)
            if path == first:
                writer._lost = True
            return result

        monkeypatch.setattr(service, "_collect", collect)
        monkeypatch.setattr(Path, "unlink", lose_after_first)
        with pytest.raises(ArchiveMaintenanceError):
            await service.audit(repair_safe=True)
    assert not first.exists()
    assert second.exists() and orphan.exists()
    assert not (root / ".quarantine").exists()


@pytest.mark.asyncio
async def test_lost_writer_between_quarantine_mkdir_and_move_keeps_source(safety, monkeypatch):
    root, redis = safety
    orphan = root / "illust/3/versions/1"
    payload = stale(orphan / "orphan.jpg")
    service = StorageIntegrityService(Session())
    original_mkdir = Path.mkdir
    async with MaintenanceWriter(redis) as writer:
        async def collect(**kwargs):
            return findings(orphans=[orphan])

        def lose_after_mkdir(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if ".quarantine" in path.parts:
                writer._lost = True
            return result

        monkeypatch.setattr(service, "_collect", collect)
        monkeypatch.setattr(Path, "mkdir", lose_after_mkdir)
        with pytest.raises(ArchiveMaintenanceError):
            await service.audit(repair_safe=True)
    assert payload.read_bytes() == b"partial"


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_repair_before_writer_unregisters(safety, monkeypatch):
    root, redis = safety
    part = stale(root / "pending.part")
    service = StorageIntegrityService(Session())
    started, release = threading.Event(), threading.Event()
    original_unlink = Path.unlink
    writers, write_after_close = [], []

    async def collect(**kwargs):
        return findings(parts=[part])

    def blocking_unlink(path, *args, **kwargs):
        if path == part:
            started.set()
            assert release.wait(5), "Test did not release the repair worker"
            write_after_close.append(writers[0]._closed)
        return original_unlink(path, *args, **kwargs)

    async def guarded_audit():
        async with MaintenanceWriter(redis) as writer:
            writers.append(writer)
            return await service.audit(repair_safe=True)

    monkeypatch.setattr(service, "_collect", collect)
    monkeypatch.setattr(Path, "unlink", blocking_unlink)
    task = asyncio.create_task(guarded_audit())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not writers[0]._closed
        assert await redis.zscore(writers_key(), writers[0].token) is not None
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert write_after_close == [False]
    assert writers[0]._closed and not part.exists()
    assert await redis.zcard(writers_key()) == 0
