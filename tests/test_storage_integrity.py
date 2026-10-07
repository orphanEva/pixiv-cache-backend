from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services.storage_integrity import StorageIntegrityService


class Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement):
        return Rows(self.rows)


@pytest.mark.asyncio
async def test_storage_audit_reports_missing_orphan_part_and_hash(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    settings.storage_root = tmp_path
    try:
        good_dir = tmp_path / "illust/1/versions/1"
        good_dir.mkdir(parents=True)
        good_file = good_dir / "0.jpg"
        good_file.write_bytes(b"changed")
        expected_sha = hashlib.sha256(b"original").hexdigest()

        missing_dir = tmp_path / "illust/2/versions/1"
        missing_file = missing_dir / "0.jpg"

        orphan = tmp_path / "illust/999/versions/1"
        orphan.mkdir(parents=True)
        part = orphan / "download.jpg.part"
        part.write_bytes(b"partial")

        service = StorageIntegrityService(Session([
            (str(good_file), expected_sha, str(good_dir), "1", 1),
            (str(missing_file), "0" * 64, str(missing_dir), "2", 1),
        ]))
        result = await service.audit(verify_hash=True)

        assert result["ok"] is False
        assert str(missing_file.resolve()) in result["missing_files"]
        assert str(good_file.resolve()) in result["hash_mismatches"]
        assert str(orphan.resolve()) in result["orphan_version_dirs"]
        assert str(part.resolve()) in result["part_files"]
    finally:
        settings.storage_root = old_root


@pytest.mark.asyncio
async def test_storage_audit_is_clean_for_registered_file(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    settings.storage_root = tmp_path
    try:
        version_dir = tmp_path / "novel/1/versions/1"
        version_dir.mkdir(parents=True)
        path = version_dir / "novel.txt"
        path.write_bytes(b"hello")
        sha = hashlib.sha256(b"hello").hexdigest()
        result = await StorageIntegrityService(Session([
            (str(path), sha, str(version_dir), "1", 1),
        ])).audit(verify_hash=True)
        assert result["ok"] is True
    finally:
        settings.storage_root = old_root


@pytest.mark.asyncio
async def test_safe_repair_deletes_stale_parts_and_quarantines_orphans(tmp_path: Path):
    import os
    import time

    settings = get_settings()
    old = {
        "storage_root": settings.storage_root,
        "storage_part_stale_seconds": settings.storage_part_stale_seconds,
        "storage_orphan_grace_seconds": settings.storage_orphan_grace_seconds,
    }
    settings.storage_root = tmp_path
    settings.storage_part_stale_seconds = 1
    settings.storage_orphan_grace_seconds = 1
    try:
        orphan = tmp_path / "illust/999/versions/7"
        orphan.mkdir(parents=True)
        payload = orphan / "orphan.jpg"
        payload.write_bytes(b"orphan")
        part = orphan / "download.part"
        part.write_bytes(b"partial")
        old_time = time.time() - 60
        os.utime(payload, (old_time, old_time))
        os.utime(part, (old_time, old_time))
        os.utime(orphan, (old_time, old_time))

        result = await StorageIntegrityService(Session([])).audit(
            repair_safe=True
        )
        assert result["repair_safe"] is True
        assert not part.exists()
        assert not orphan.exists()
        quarantined = result["repairs"]["quarantined_orphan_version_dirs"]
        assert len(quarantined) == 1
        assert Path(quarantined[0]).exists()
        assert result["after"]["orphan_version_dirs"] == []
        assert result["after"]["part_files"] == []
    finally:
        for key, value in old.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_safe_repair_skips_recent_orphan(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    old_grace = settings.storage_orphan_grace_seconds
    settings.storage_root = tmp_path
    settings.storage_orphan_grace_seconds = 3600
    try:
        orphan = tmp_path / "illust/888/versions/1"
        orphan.mkdir(parents=True)
        (orphan / "active.part").write_bytes(b"active")
        result = await StorageIntegrityService(Session([])).audit(
            repair_safe=True
        )
        assert orphan.exists()
        assert str(orphan.resolve()) in result["repairs"]["skipped_recent"]
    finally:
        settings.storage_root = old_root
        settings.storage_orphan_grace_seconds = old_grace
