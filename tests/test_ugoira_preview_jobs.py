from __future__ import annotations

import asyncio
import hashlib
import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from sqlalchemy import BigInteger, Integer, MetaData, Text, select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.work import CurrentFile, PixivAsset, PixivVersion, PixivWork, Series, WorkType
from app.services.archive_jobs import ArchiveJobQueue
from app.services.media_jobs import MediaJobQueue
from app.services.ugoira_preview import UgoiraPreviewService
from app.storage.local_storage import LocalStorage


def sources(storage, pixiv_id=77, version_no=1):
    target = storage.version_dir(WorkType.UGOIRA, pixiv_id, version_no)
    target.mkdir(parents=True)
    zip_path = target / "original.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("000000.jpg", b"frame-zero")
        archive.writestr("000001.jpg", b"frame-one")
    sha, size = storage._hash_file(zip_path)
    meta = target / "ugoira_meta.json"
    meta.write_text(json.dumps({"pixiv_id": pixiv_id, "zip_sha256": sha,
                               "frames": [{"file": "000000.jpg", "delay": 80},
                                          {"file": "000001.jpg", "delay": 120}]}), encoding="utf-8")
    meta_sha, meta_size = storage._hash_file(meta)
    return {"pixiv_id": pixiv_id, "version_no": version_no, "zip_path": zip_path,
            "metadata_path": meta, "zip_sha256": sha, "zip_size_bytes": size,
            "metadata_sha256": meta_sha, "metadata_size_bytes": meta_size}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)
    monkeypatch.setattr(get_settings(), "storage_min_free_bytes", 0)
    value = LocalStorage()

    def no_network():
        pytest.fail("Local preview generation attempted a Pixiv request")

    monkeypatch.setattr(value, "_http_client", no_network)
    return value


@pytest.mark.asyncio
async def test_local_rebuild_checks_sources_preserves_timing_and_publishes_atomically(storage, monkeypatch):
    args = sources(storage)
    target = args["zip_path"].parent
    previous = target / "preview.mp4"
    previous.write_bytes(b"old")
    phases = []

    async def convert(paths, frames, scratch, output):
        assert [frame.delay for frame in frames] == [80, 120]
        assert [path.read_bytes() for path in paths] == [b"frame-zero", b"frame-one"]
        assert scratch != target and scratch.parent == target
        output.write_bytes(b"new-complete-mp4")
        assert previous.read_bytes() == b"old"

    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", convert)
    before = AsyncMock()

    async def progress(phase):
        phases.append(phase)

    asset = await storage.rebuild_ugoira_preview(**args, before_publish=before, progress=progress)
    before.assert_awaited_once()
    assert previous.read_bytes() == b"new-complete-mp4"
    assert asset["sha256"] == hashlib.sha256(previous.read_bytes()).hexdigest()
    assert phases == ["converting"]
    assert not list(target.glob(".mp4-*"))
    assert storage._hash_file(args["zip_path"])[0] == args["zip_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["zip_path", "metadata_path"])
async def test_corrupt_registered_sources_do_not_replace_preview(storage, monkeypatch, part):
    args = sources(storage)
    target = args["zip_path"].parent
    (target / "preview.mp4").write_bytes(b"old")
    args[part].write_bytes(b"tampered")
    convert = AsyncMock()
    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", convert)
    with pytest.raises(ValueError, match="hash or size"):
        await storage.rebuild_ugoira_preview(**args)
    convert.assert_not_awaited()
    assert (target / "preview.mp4").read_bytes() == b"old"
    assert not list(target.glob(".mp4-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("ffmpeg failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_conversion_preserves_archive(storage, monkeypatch, error):
    args = sources(storage)
    target = args["zip_path"].parent
    (target / "preview.mp4").write_bytes(b"old")

    async def convert(paths, frames, scratch, output):
        output.write_bytes(b"partial")
        raise error

    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", convert)
    with pytest.raises(type(error)):
        await storage.rebuild_ugoira_preview(**args)
    assert (target / "preview.mp4").read_bytes() == b"old"
    assert args["zip_path"].is_file() and args["metadata_path"].is_file()
    assert not list(target.glob(".mp4-*"))


@pytest.mark.asyncio
async def test_guard_loss_prevents_publish(storage, monkeypatch):
    args = sources(storage)
    target = args["zip_path"].parent

    async def convert(paths, frames, scratch, output):
        output.write_bytes(b"complete")

    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", convert)
    with pytest.raises(RuntimeError, match="lease lost"):
        await storage.rebuild_ugoira_preview(**args, before_publish=AsyncMock(side_effect=RuntimeError("lease lost")))
    assert not (target / "preview.mp4").exists()
    assert not list(target.glob(".mp4-*"))


def test_zip_budget_is_checked_before_decompression(storage, monkeypatch):
    args = sources(storage)
    monkeypatch.setattr(get_settings(), "ugoira_max_uncompressed_bytes", 1)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: pytest.fail("decompressed over-budget ZIP"))
    with pytest.raises(ValueError, match="exceeds limit"):
        storage._extract_ugoira_frames(args["zip_path"], [], args["zip_path"].parent / "frames")


@pytest.fixture
async def media_queue():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield MediaJobQueue(redis)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_media_queue_deduplicates_same_version_and_cannot_consume_archive_jobs(media_queue):
    queue = media_queue
    jobs = await asyncio.gather(*(queue.enqueue_preview(77, 1) for _ in range(12)))
    assert len({job["job_id"] for job in jobs}) == 1
    second = await queue.enqueue_preview(77, 2)
    assert second["job_id"] != jobs[0]["job_id"]
    archive = ArchiveJobQueue(queue.redis)
    upstream = await archive.enqueue(WorkType.ILLUST, 77)
    assert queue.stream != archive.stream
    assert await queue.redis.xlen(queue.stream) == 2
    assert await queue.get(upstream["job_id"]) is None
    message = await queue.read_one("media", block_ms=1)
    assert message[1] in {jobs[0]["job_id"], second["job_id"]}
    assert (await queue.get(message[1]))["job_type"] == "ugoira_mp4"


@pytest.mark.asyncio
async def test_media_queue_retry_progress_and_terminal_replay_keep_exact_version(media_queue):
    queue = media_queue
    job = await queue.enqueue_preview(77, 4)
    stream, job_id = await queue.read_one("media", block_ms=1)
    running = await queue.mark_running(job_id, "media")
    assert running["attempts"] == 1
    await queue.set_phase(job_id, "converting")
    await queue.schedule_retry(stream, job_id, RuntimeError("temporary"), delay_seconds=1)
    assert (await queue.get(job_id))["status"] == "retry_wait"
    assert (await queue.enqueue_preview(77, 4))["job_id"] == job_id
    await queue.redis.zadd(queue.retry_zset, {job_id: time.time() - 1})
    assert await queue.promote_due_retries() == 1
    stream, _ = await queue.read_one("media", block_ms=1)
    assert (await queue.mark_running(job_id, "media"))["attempts"] == 2
    await queue.mark_dead(job_id, ValueError("bad source"), reason="non_retryable")
    await queue.ack(stream)
    retry = await queue.retry(job_id)
    assert retry["job_id"] != job_id and retry["version_no"] == 4
    await queue.mark_succeeded(retry["job_id"], {"version_no": 4})
    assert await queue.redis.get(queue.dedupe_key(WorkType.ILLUST, 77, 4)) is None


@pytest.fixture
async def preview_service(storage):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model in (Series, PixivWork, PixivVersion, PixivAsset, CurrentFile):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, BigInteger):
                column.type = Integer()
            elif isinstance(column.type, MEDIUMTEXT):
                column.type = Text()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([
            PixivWork(work_kind="illust", id="77", work_type=WorkType.UGOIRA, title="animation", version_token="a" * 64, current_version_no=2),
            PixivWork(work_kind="novel", id="77", work_type=WorkType.NOVEL, title="same ID", version_token="b" * 64),
            CurrentFile(work_kind="novel", work_id="77", page_index=0, file_type="novel_txt", local_path="novel.txt", size_bytes=9),
        ])
        await session.flush()
        for version_no in (1, 2):
            args = sources(storage, version_no=version_no)
            version = PixivVersion(work_kind="illust", work_id="77", version_no=version_no,
                version_token="a" * 64, title="animation", metadata_json={},
                storage_path=str(args["zip_path"].parent), created_at=datetime.now(timezone.utc))
            session.add(version)
            await session.flush()
            session.add_all([
                PixivAsset(version_id=version.id, page_index=0, file_type="ugoira_zip", local_path=str(args["zip_path"]), sha256=args["zip_sha256"], size_bytes=args["zip_size_bytes"]),
                PixivAsset(version_id=version.id, page_index=2, file_type="ugoira_meta", local_path=str(args["metadata_path"]), sha256=args["metadata_sha256"], size_bytes=args["metadata_size_bytes"]),
            ])
        await session.commit()
        yield UgoiraPreviewService(session, redis, storage), session, redis, factory
    await redis.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_history_and_current_assets_registered_without_touching_same_id_novel(preview_service, monkeypatch):
    service, session, _, _ = preview_service
    converted = []

    async def convert(paths, frames, scratch, output):
        converted.append(scratch)
        output.write_bytes(b"complete-video")

    monkeypatch.setattr(service.storage, "_convert_ugoira_to_mp4", convert)
    old = await service.generate(77, 1)
    assert old["current"] is False
    assert not (await session.execute(select(CurrentFile).where(CurrentFile.work_kind == "illust"))).scalars().all()
    new = await service.generate(77, 2)
    assert new["current"] is True
    repeated = await service.generate(77, 2)
    assert repeated["source"] == "local-existing" and len(converted) == 2
    history = (await session.execute(select(PixivAsset).where(PixivAsset.file_type == "ugoira_mp4"))).scalars().all()
    assert len(history) == 2
    current = (await session.execute(select(CurrentFile).where(CurrentFile.work_kind == "illust"))).scalar_one()
    assert current.file_type == "ugoira_mp4" and "/2/preview.mp4" in Path(current.local_path).as_posix()
    novel = (await session.execute(select(CurrentFile).where(CurrentFile.work_kind == "novel"))).scalar_one()
    assert novel.local_path == "novel.txt" and novel.file_type == "novel_txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_uncertain_db_commit_replay_keeps_one_registered_preview(preview_service, monkeypatch, committed):
    service, session, _, _ = preview_service
    calls = []

    async def convert(paths, frames, scratch, output):
        calls.append(output)
        output.write_bytes(b"complete-video")

    monkeypatch.setattr(service.storage, "_convert_ugoira_to_mp4", convert)
    original_commit = session.commit
    original_execute = session.execute
    injected = False
    writing_assets = False

    async def execute(statement, *args, **kwargs):
        nonlocal writing_assets
        # The CurrentFile query may autoflush the history asset, so track the
        # publishing transaction before that flush rather than session.new.
        if any(isinstance(row, PixivAsset) and row.file_type == "ugoira_mp4" for row in session.new):
            writing_assets = True
        return await original_execute(statement, *args, **kwargs)

    async def uncertain_commit():
        nonlocal injected
        if writing_assets and not injected:
            injected = True
            if committed:
                await original_commit()
            raise ConnectionError("commit response lost")
        return await original_commit()

    monkeypatch.setattr(session, "execute", execute)
    monkeypatch.setattr(session, "commit", uncertain_commit)
    with pytest.raises(ConnectionError):
        await service.generate(77, 2)
    result = await service.generate(77, 2)
    assert result["source"] == ("local-existing" if committed else "local-generated")
    assets = (await session.execute(select(PixivAsset).where(PixivAsset.file_type == "ugoira_mp4"))).scalars().all()
    assert len(assets) == 1
    assert len(calls) == (1 if committed else 2)


@pytest.mark.asyncio
async def test_media_worker_records_retry_without_remote_client(media_queue, monkeypatch):
    from app.workers import media_worker as module
    queue = media_queue
    worker = module.MediaWorker.__new__(module.MediaWorker)
    worker.settings, worker.redis, worker.queue, worker.consumer = get_settings(), queue.redis, queue, "media"

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    generate = AsyncMock(side_effect=TimeoutError("local encode timeout"))
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(module, "UgoiraPreviewService", lambda *args: type("Service", (), {"generate": generate})())
    job = await queue.enqueue_preview(77, 1)
    await worker._process(*(await queue.read_one("media", block_ms=1)))
    result = await queue.get(job["job_id"])
    assert result["status"] == "retry_wait" and result["attempts"] == 1


@pytest.mark.asyncio
async def test_enqueue_api_pins_current_version_then_worker_registers_local_result(preview_service, monkeypatch):
    from app.api import media_jobs as api
    from app.workers import media_worker as module

    service, session, redis, factory = preview_service
    monkeypatch.setattr(api, "get_redis", lambda: redis)
    job = await api.enqueue_ugoira_preview(77, None, session)
    assert job["version_no"] == 2 and job["job_type"] == "ugoira_mp4"
    assert (await api.enqueue_ugoira_preview(77, 2, session))["job_id"] == job["job_id"]

    async def convert(paths, frames, scratch, output):
        output.write_bytes(b"complete-video")

    monkeypatch.setattr(service.storage, "_convert_ugoira_to_mp4", convert)
    monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(module, "UgoiraPreviewService", lambda db, connection: UgoiraPreviewService(db, connection, service.storage))
    worker = module.MediaWorker.__new__(module.MediaWorker)
    worker.settings, worker.redis, worker.queue, worker.consumer = get_settings(), redis, MediaJobQueue(redis), "media"
    await worker._process(*(await worker.queue.read_one("media", block_ms=1)))
    finished = await api.get_media_job(job["job_id"])
    assert finished["status"] == "succeeded" and finished["phase"] == "complete"
    assert finished["result"]["source"] == "local-generated"
    assert finished["result"]["version_no"] == 2
    assert (await api.media_job_stats())["pending"] == 0
