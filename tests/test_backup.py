from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.schema_version import EXPECTED_SCHEMA_VERSION
from app.maintenance.backup import (
    ArchiveBackupManager,
    BackupError,
    sha256_file,
)


@pytest.fixture
def backup_manager(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    old_backup = settings.backup_root
    old_min = settings.storage_min_free_bytes
    settings.storage_root = tmp_path / "storage"
    settings.backup_root = tmp_path / "backups"
    settings.storage_min_free_bytes = 0
    settings.storage_root.mkdir(parents=True)
    settings.backup_root.mkdir(parents=True)
    try:
        yield ArchiveBackupManager()
    finally:
        settings.storage_root = old_root
        settings.backup_root = old_backup
        settings.storage_min_free_bytes = old_min


def test_storage_backup_excludes_credentials_and_quarantine(
    backup_manager: ArchiveBackupManager,
):
    root = backup_manager.storage_root
    archive_file = backup_manager.backup_root / "storage.tar.gz"

    novel = root / "novel/1/versions/1/novel.txt"
    novel.parent.mkdir(parents=True)
    novel.write_text("hello", encoding="utf-8")

    auth = root / "auth/web_cookie.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("secret", encoding="utf-8")

    quarantine = root / ".quarantine/old/orphan.bin"
    quarantine.parent.mkdir(parents=True)
    quarantine.write_bytes(b"orphan")

    backup_manager._archive_storage(archive_file)

    with tarfile.open(archive_file, "r:gz") as archive:
        names = set(archive.getnames())
    assert "novel/1/versions/1/novel.txt" in names
    assert not any(name.startswith("auth/") for name in names)
    assert not any(name.startswith(".quarantine/") for name in names)


def test_verify_checks_manifest_and_payload_hashes(
    backup_manager: ArchiveBackupManager,
):
    target = backup_manager.backup_root / "test"
    target.mkdir()
    database = target / "database.sql"
    database.write_text("-- fake dump", encoding="utf-8")
    storage = target / "storage.tar.gz"

    novel = backup_manager.storage_root / "novel/1/versions/1/novel.txt"
    novel.parent.mkdir(parents=True)
    novel.write_text("hello", encoding="utf-8")
    backup_manager._archive_storage(storage)

    manifest = {
        "format_version": 1,
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "database_dump": database.name,
        "database_sha256": sha256_file(database),
        "storage_archive": storage.name,
        "storage_sha256": sha256_file(storage),
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert backup_manager.verify(target)["ok"] is True
    database.write_text("-- corrupted", encoding="utf-8")
    with pytest.raises(BackupError, match="checksum mismatch"):
        backup_manager.verify(target)


def test_validate_tar_rejects_path_traversal(
    backup_manager: ArchiveBackupManager,
):
    archive_path = backup_manager.backup_root / "bad.tar.gz"
    payload = b"evil"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("../evil")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(BackupError):
        backup_manager._validate_tar(archive_path)


def test_safe_backup_name():
    assert ArchiveBackupManager._safe_name("daily-2026_10.07") == "daily-2026_10.07"
    with pytest.raises(BackupError):
        ArchiveBackupManager._safe_name("../")


@pytest.mark.asyncio
async def test_restore_requires_explicit_destructive_confirmation(
    backup_manager: ArchiveBackupManager,
):
    with pytest.raises(BackupError, match="confirm-destructive-restore"):
        await backup_manager.restore(
            backup_manager.backup_root / "anything",
            confirm_destructive_restore=False,
        )
