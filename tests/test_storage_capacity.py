from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.errors import ArchiveStorageError
from app.storage.local_storage import LocalStorage


def test_storage_space_guard_rejects_low_disk(monkeypatch, tmp_path):
    settings = get_settings()
    old_root = settings.storage_root
    old_min = settings.storage_min_free_bytes
    settings.storage_root = tmp_path
    settings.storage_min_free_bytes = 1024
    try:
        storage = LocalStorage()
        storage.root = tmp_path
        monkeypatch.setattr(
            "app.storage.local_storage.shutil.disk_usage",
            lambda path: SimpleNamespace(free=100),
        )
        with pytest.raises(ArchiveStorageError):
            storage._ensure_disk_space()
    finally:
        settings.storage_root = old_root
        settings.storage_min_free_bytes = old_min


def test_storage_space_guard_accounts_for_required_extra(monkeypatch, tmp_path):
    settings = get_settings()
    old_root = settings.storage_root
    old_min = settings.storage_min_free_bytes
    settings.storage_root = tmp_path
    settings.storage_min_free_bytes = 100
    try:
        storage = LocalStorage()
        storage.root = tmp_path
        monkeypatch.setattr(
            "app.storage.local_storage.shutil.disk_usage",
            lambda path: SimpleNamespace(free=150),
        )
        with pytest.raises(ArchiveStorageError):
            storage._ensure_disk_space(100)
    finally:
        settings.storage_root = old_root
        settings.storage_min_free_bytes = old_min
