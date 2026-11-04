from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tarfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from app.core.errors import ArchiveMaintenanceError
from app.core.maintenance import recovery_key
from app.core.schema_version import EXPECTED_SCHEMA_VERSION
from app.maintenance import backup as module
from app.maintenance.backup import BackupError, FORMAT_VERSION, sha256_file
from test_backup import backup_manager


class Freeze:
    def __init__(self):
        self.released = False
        self.lost = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.released = True

    async def ensure_alive(self):
        if self.lost:
            raise ArchiveMaintenanceError("freeze lost")


def configure_database(manager, monkeypatch, database="target"):
    monkeypatch.setattr(manager.settings, "database_url", f"mysql+asyncmy://app:secret@db:3306/{database}")
    monkeypatch.setattr(manager.settings, "maintenance_database_url", f"mysql://admin:secret@db/{database}")


def test_dump_omits_database_selection_and_restore_selects_explicit_target(backup_manager, monkeypatch):
    configure_database(backup_manager, monkeypatch)
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(backup_manager, "_supports_option", lambda *args: False)

    def run(command, **kwargs):
        calls.append(command)
        if hasattr(kwargs.get("stdout"), "write"):
            kwargs["stdout"].write(b"-- isolated fixture dump\n")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", run)
    dump = backup_manager.backup_root / "database.sql"
    backup_manager._dump_database(dump)
    backup_manager._restore_database(dump)
    assert "--databases" not in calls[0]
    assert "--no-create-db" in calls[0]
    assert calls[0][-1] == "target"
    assert calls[1][-2:] == ["--database", "target"]


@pytest.mark.parametrize("maintenance", ["mysql://admin@other/target", "mysql://admin@db/other", "mysql://admin@db:3307/target"])
def test_maintenance_destination_must_match_runtime(backup_manager, monkeypatch, maintenance):
    configure_database(backup_manager, monkeypatch)
    monkeypatch.setattr(backup_manager.settings, "maintenance_database_url", maintenance)
    with pytest.raises(BackupError, match="same host, port and database"):
        backup_manager._database_url()


def test_legacy_dump_is_same_database_only_and_new_format_is_database_neutral(backup_manager, monkeypatch):
    configure_database(backup_manager, monkeypatch)
    dump = backup_manager.backup_root / "legacy.sql"
    dump.write_text("CREATE DATABASE /*!32312 IF NOT EXISTS*/ `target`;\nUSE `target`;\n", encoding="utf-8")
    backup_manager._validate_restore_binding({"format_version": 1, "database_name": "target"}, dump)
    with pytest.raises(BackupError, match="original database"):
        backup_manager._validate_restore_binding({"format_version": 1, "database_name": "source"}, dump)
    dump.write_text("USE `another_database`;\n", encoding="utf-8")
    with pytest.raises(BackupError, match="other than"):
        backup_manager._validate_restore_binding({"format_version": 1, "database_name": "target"}, dump)
    manifest = {"format_version": FORMAT_VERSION, "database_binding": "selected-database", "database_name": "source"}
    with pytest.raises(BackupError, match="unexpected database selection"):
        backup_manager._validate_restore_binding(manifest, dump)
    dump.write_text("CREATE TABLE example (id INT);\n", encoding="utf-8")
    backup_manager._validate_restore_binding(manifest, dump)


@pytest.mark.parametrize("name", ["../outside.sql", "sub/database.sql", "C:\\outside.sql", "/outside.sql", "database.sql:other"])
def test_manifest_payload_cannot_leave_backup_directory(backup_manager, name):
    with pytest.raises(BackupError, match="plain filename"):
        backup_manager._payload_path(backup_manager.backup_root, name)


def make_backup(manager, tmp_path):
    target = manager.backup_root / "incoming"
    target.mkdir()
    database = target / "database.sql"
    database.write_text("-- new database\n", encoding="utf-8")
    media = tmp_path / "fixture_media" / "novel/1/versions/1/novel.txt"
    media.parent.mkdir(parents=True)
    media.write_text("new", encoding="utf-8")
    archive_path = target / "storage.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(tmp_path / "fixture_media" / "novel", arcname="novel")
    manifest = {
        "format_version": FORMAT_VERSION, "schema_version": EXPECTED_SCHEMA_VERSION,
        "database_name": "source", "database_binding": "selected-database",
        "database_dump": "database.sql", "database_sha256": sha256_file(database),
        "storage_archive": "storage.tar.gz", "storage_sha256": sha256_file(archive_path),
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_failed_restore_keeps_recovery_artifacts_and_reports_rollback_failure(
    backup_manager, monkeypatch, tmp_path, rollback_fails,
):
    configure_database(backup_manager, monkeypatch)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    backup_manager.redis = redis
    guard = Freeze()
    monkeypatch.setattr(module, "MaintenanceFreeze", lambda *args, **kwargs: guard)
    monkeypatch.setattr(module, "ensure_schema_version", AsyncMock())
    monkeypatch.setattr(module, "engine", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(backup_manager, "_dump_database", lambda path: path.write_text("-- old database", encoding="utf-8"))
    live = backup_manager.storage_root / "novel/1/versions/1/novel.txt"
    live.parent.mkdir(parents=True)
    live.write_text("old", encoding="utf-8")
    incoming = make_backup(backup_manager, tmp_path)
    calls = []

    def restore(path):
        calls.append(path.read_text(encoding="utf-8"))
        if len(calls) == 1 or rollback_fails:
            raise BackupError("isolated restore failure")

    monkeypatch.setattr(backup_manager, "_restore_database", restore)
    with pytest.raises(BackupError, match="Restore failed"):
        await backup_manager.restore(incoming, confirm_destructive_restore=True)
    assert calls == ["-- new database\n", "-- old database"]
    assert live.read_text(encoding="utf-8") == "old"
    rollbacks = list(backup_manager.storage_root.glob(".restore-rollback-*"))
    assert len(rollbacks) == 1
    assert (rollbacks[0] / "novel/1/versions/1/novel.txt").read_text(encoding="utf-8") == "old"
    states = list(backup_manager.backup_root.glob("pre-restore-db-*/restore-state.json"))
    state = json.loads(states[0].read_text(encoding="utf-8"))
    assert Path(state["pre_restore_database_dump"]).is_file()
    assert state["phase"] == ("recovery_required" if rollback_fails else "rolled_back")
    assert bool(await redis.exists(recovery_key())) is rollback_fails
    if rollback_fails:
        assert state["database_rollback"] == "failed"
        assert state["rollback_errors"] == ["database:BackupError"]
    await redis.aclose()


@pytest.mark.asyncio
async def test_cancelled_backup_waits_for_thread_before_releasing_freeze(backup_manager, monkeypatch):
    configure_database(backup_manager, monkeypatch)
    guard = Freeze()
    monkeypatch.setattr(module, "MaintenanceFreeze", lambda *args, **kwargs: guard)
    monkeypatch.setattr(module, "ensure_schema_version", AsyncMock())
    monkeypatch.setattr(backup_manager, "_dump_database", lambda path: path.write_bytes(b"dump"))
    started, finish = threading.Event(), threading.Event()

    def archive(path):
        started.set()
        assert finish.wait(5)
        path.write_bytes(b"archive")

    monkeypatch.setattr(backup_manager, "_archive_storage", archive)
    task = asyncio.create_task(backup_manager.create("cancelled"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not guard.released
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert guard.released
    assert not (backup_manager.backup_root / "cancelled").exists()


@pytest.mark.asyncio
async def test_lost_freeze_never_publishes_backup_manifest(backup_manager, monkeypatch):
    configure_database(backup_manager, monkeypatch)
    guard = Freeze()
    monkeypatch.setattr(module, "MaintenanceFreeze", lambda *args, **kwargs: guard)
    monkeypatch.setattr(module, "ensure_schema_version", AsyncMock())
    monkeypatch.setattr(backup_manager, "_dump_database", lambda path: path.write_bytes(b"dump"))

    def archive(path):
        path.write_bytes(b"archive")
        guard.lost = True

    monkeypatch.setattr(backup_manager, "_archive_storage", archive)
    with pytest.raises(ArchiveMaintenanceError):
        await backup_manager.create("lost")
    assert not (backup_manager.backup_root / "lost" / "manifest.json").exists()
