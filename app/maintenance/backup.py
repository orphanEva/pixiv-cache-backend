from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL, make_url

from app.core.config import get_settings
from app.core.maintenance import MaintenanceFreeze
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import EXPECTED_SCHEMA_VERSION, ensure_schema_version
from app.db.session import engine

FORMAT_VERSION = 1
ARCHIVE_DIRS = ("illust", "manga", "ugoira", "novel")


class BackupError(RuntimeError):
    pass


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def application_version() -> str:
    try:
        return package_version("pixiv-cache-backend")
    except PackageNotFoundError:
        return "unknown"


class ArchiveBackupManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = get_redis()
        self.storage_root = self.settings.storage_root.resolve()
        self.backup_root = self.settings.backup_root.resolve()

    async def create(self, name: str | None = None) -> dict[str, Any]:
        await ensure_schema_version(engine)
        backup_name = self._safe_name(name) if name else utc_stamp()
        target = self.backup_root / backup_name
        if target.exists():
            raise BackupError(f"Backup target already exists: {target}")
        target.mkdir(parents=True, exist_ok=False)

        async with MaintenanceFreeze(self.redis):
            source_bytes = self._storage_source_bytes()
            self._ensure_backup_capacity(source_bytes)

            database_path = target / "database.sql"
            storage_path = target / "storage.tar.gz"
            await asyncio.to_thread(self._dump_database, database_path)
            await asyncio.to_thread(self._archive_storage, storage_path)

            manifest = {
                "format_version": FORMAT_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "app_version": application_version(),
                "schema_version": EXPECTED_SCHEMA_VERSION,
                "database_name": self._database_url().database,
                "storage_dirs": list(ARCHIVE_DIRS),
                "source_storage_bytes": source_bytes,
                "database_dump": database_path.name,
                "database_sha256": sha256_file(database_path),
                "storage_archive": storage_path.name,
                "storage_sha256": sha256_file(storage_path),
                "credentials_included": False,
                "quarantine_included": False,
                "redis_jobs_included": False,
            }
            self._write_json(target / "manifest.json", manifest)
        return {"backup_path": str(target), "manifest": manifest}

    def verify(self, backup_dir: Path) -> dict[str, Any]:
        backup_dir = backup_dir.resolve()
        manifest_path = backup_dir / "manifest.json"
        if not manifest_path.is_file():
            raise BackupError("manifest.json is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format_version") != FORMAT_VERSION:
            raise BackupError(
                f"Unsupported backup format: {manifest.get('format_version')}"
            )
        database_path = backup_dir / str(manifest.get("database_dump") or "")
        storage_path = backup_dir / str(manifest.get("storage_archive") or "")
        if not database_path.is_file() or not storage_path.is_file():
            raise BackupError("Backup database/storage payload is incomplete")
        if sha256_file(database_path) != manifest.get("database_sha256"):
            raise BackupError("database.sql checksum mismatch")
        if sha256_file(storage_path) != manifest.get("storage_sha256"):
            raise BackupError("storage.tar.gz checksum mismatch")
        if int(manifest.get("schema_version") or -1) != EXPECTED_SCHEMA_VERSION:
            raise BackupError(
                f"Backup schema version {manifest.get('schema_version')} is not "
                f"compatible with required version {EXPECTED_SCHEMA_VERSION}"
            )
        self._validate_tar(storage_path)
        return {
            "ok": True,
            "backup_path": str(backup_dir),
            "manifest": manifest,
        }

    async def restore(
        self,
        backup_dir: Path,
        *,
        confirm_destructive_restore: bool,
    ) -> dict[str, Any]:
        if not confirm_destructive_restore:
            raise BackupError(
                "Restore is destructive; pass --confirm-destructive-restore"
            )
        verified = self.verify(backup_dir)
        manifest = verified["manifest"]
        backup_dir = backup_dir.resolve()
        database_path = backup_dir / manifest["database_dump"]
        storage_path = backup_dir / manifest["storage_archive"]

        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)

        async with MaintenanceFreeze(self.redis):
            staging = Path(
                tempfile.mkdtemp(
                    prefix=".restore-staging-",
                    dir=str(self.storage_root),
                )
            )
            rollback_root = self.storage_root / f".restore-rollback-{utc_stamp()}"
            safety_dir = self.backup_root / f"pre-restore-db-{utc_stamp()}"
            safety_dir.mkdir(parents=True, exist_ok=False)
            safety_db = safety_dir / "database.sql"

            try:
                await asyncio.to_thread(self._extract_storage, storage_path, staging)
                await asyncio.to_thread(self._dump_database, safety_db)
                self._swap_storage_in(staging, rollback_root)
                try:
                    await asyncio.to_thread(self._restore_database, database_path)
                    await engine.dispose()
                    await ensure_schema_version(engine)
                except Exception:
                    self._rollback_storage(rollback_root)
                    try:
                        await asyncio.to_thread(self._restore_database, safety_db)
                    except Exception:
                        pass
                    raise
                shutil.rmtree(rollback_root, ignore_errors=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        return {
            "restored": True,
            "backup_path": str(backup_dir),
            "pre_restore_database_dump": str(safety_db),
            "schema_version": EXPECTED_SCHEMA_VERSION,
        }

    def _database_url(self) -> URL:
        raw = (
            self.settings.maintenance_database_url.strip()
            or self.settings.database_url
        )
        url = make_url(raw)
        if not url.database:
            raise BackupError("Database URL has no database name")
        return url

    def _dump_database(self, output: Path) -> None:
        url = self._database_url()
        binary = shutil.which("mysqldump") or shutil.which("mariadb-dump")
        if not binary:
            raise BackupError("mysqldump/mariadb-dump is not installed")
        cmd = self._mysql_connection_args(binary, url) + [
            "--single-transaction",
            "--quick",
            "--hex-blob",
            "--skip-lock-tables",
            "--no-tablespaces",
            "--default-character-set=utf8mb4",
            "--databases",
            str(url.database),
        ]
        env = os.environ.copy()
        if url.password:
            env["MYSQL_PWD"] = url.password
        with output.open("wb") as fp:
            result = subprocess.run(
                cmd,
                stdout=fp,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )
        if result.returncode != 0:
            output.unlink(missing_ok=True)
            raise BackupError(
                "Database dump failed: "
                + result.stderr.decode("utf-8", errors="replace")[-2000:]
            )

    def _restore_database(self, source: Path) -> None:
        url = self._database_url()
        binary = shutil.which("mysql") or shutil.which("mariadb")
        if not binary:
            raise BackupError("mysql/mariadb client is not installed")
        cmd = self._mysql_connection_args(binary, url) + [
            "--default-character-set=utf8mb4",
        ]
        env = os.environ.copy()
        if url.password:
            env["MYSQL_PWD"] = url.password
        with source.open("rb") as fp:
            result = subprocess.run(
                cmd,
                stdin=fp,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )
        if result.returncode != 0:
            raise BackupError(
                "Database restore failed: "
                + result.stderr.decode("utf-8", errors="replace")[-2000:]
            )

    @staticmethod
    def _mysql_connection_args(binary: str, url: URL) -> list[str]:
        cmd = [binary]
        if url.host:
            cmd += ["--host", url.host]
        if url.port:
            cmd += ["--port", str(url.port)]
        if url.username:
            cmd += ["--user", url.username]
        return cmd

    def _archive_storage(self, output: Path) -> None:
        with tarfile.open(output, "w:gz") as archive:
            for name in ARCHIVE_DIRS:
                source = self.storage_root / name
                if source.exists():
                    archive.add(source, arcname=name, recursive=True)

    def _extract_storage(self, archive_path: Path, staging: Path) -> None:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                top = Path(member.name).parts[0] if Path(member.name).parts else ""
                if top not in ARCHIVE_DIRS:
                    raise BackupError(
                        f"Unexpected top-level path in storage archive: {member.name}"
                    )
                destination = (staging / member.name).resolve()
                try:
                    destination.relative_to(staging.resolve())
                except ValueError:
                    raise BackupError(
                        f"Unsafe path in storage archive: {member.name}"
                    ) from None
            archive.extractall(staging, filter="data")

    def _validate_tar(self, archive_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            self._extract_storage(archive_path, staging)

    def _swap_storage_in(self, staging: Path, rollback_root: Path) -> None:
        rollback_root.mkdir(parents=True, exist_ok=False)
        moved_old: list[str] = []
        moved_new: list[str] = []
        try:
            for name in ARCHIVE_DIRS:
                current = self.storage_root / name
                if current.exists():
                    shutil.move(str(current), str(rollback_root / name))
                    moved_old.append(name)
            for name in ARCHIVE_DIRS:
                incoming = staging / name
                if incoming.exists():
                    shutil.move(str(incoming), str(self.storage_root / name))
                    moved_new.append(name)
        except Exception:
            for name in moved_new:
                shutil.rmtree(self.storage_root / name, ignore_errors=True)
            for name in moved_old:
                old = rollback_root / name
                if old.exists():
                    shutil.move(str(old), str(self.storage_root / name))
            raise

    def _rollback_storage(self, rollback_root: Path) -> None:
        for name in ARCHIVE_DIRS:
            current = self.storage_root / name
            if current.exists():
                shutil.rmtree(current, ignore_errors=True)
            old = rollback_root / name
            if old.exists():
                shutil.move(str(old), str(current))
        shutil.rmtree(rollback_root, ignore_errors=True)

    def _storage_source_bytes(self) -> int:
        total = 0
        for name in ARCHIVE_DIRS:
            root = self.storage_root / name
            if not root.exists():
                continue
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    continue
        return total

    def _ensure_backup_capacity(self, source_bytes: int) -> None:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.backup_root).free
        required = self.settings.storage_min_free_bytes + source_bytes
        if free < required:
            raise BackupError(
                f"Insufficient backup space: free={free} required={required}. "
                "Mount BACKUP_ROOT on another filesystem or free space first."
            )

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = "".join(
            ch for ch in value.strip() if ch.isalnum() or ch in ("-", "_", ".")
        )
        if not cleaned or cleaned in (".", ".."):
            raise BackupError("Invalid backup name")
        return cleaned[:100]

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


async def async_main(args) -> dict[str, Any]:
    manager = ArchiveBackupManager()
    if args.command == "create":
        return await manager.create(args.name)
    if args.command == "verify":
        return manager.verify(Path(args.backup_dir))
    if args.command == "restore":
        return await manager.restore(
            Path(args.backup_dir),
            confirm_destructive_restore=args.confirm_destructive_restore,
        )
    raise BackupError("Unknown command")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pixiv archive backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--name")

    verify = sub.add_parser("verify")
    verify.add_argument("backup_dir")

    restore = sub.add_parser("restore")
    restore.add_argument("backup_dir")
    restore.add_argument(
        "--confirm-destructive-restore",
        action="store_true",
        help="Required: restore replaces archive tables/files.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = asyncio.run(async_main(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        try:
            asyncio.run(close_redis())
        except RuntimeError:
            pass


if __name__ == "__main__":
    main()
