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
