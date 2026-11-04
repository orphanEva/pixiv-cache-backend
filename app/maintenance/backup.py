from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL, make_url

from app.core.config import get_settings
from app.core.maintenance import MaintenanceFreeze, recovery_key
from app.core.redis_client import close_redis, get_redis
from app.core.schema_version import EXPECTED_SCHEMA_VERSION, ensure_schema_version
from app.db.session import engine

FORMAT_VERSION = 2
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
        self._database_url()  # Validate the maintenance and runtime destinations first.
        await ensure_schema_version(engine)
        backup_name = self._safe_name(name) if name else utc_stamp()
        target = self.backup_root / backup_name
        if target.exists():
            raise BackupError(f"Backup target already exists: {target}")
        target.mkdir(parents=True, exist_ok=False)

        try:
            async with MaintenanceFreeze(self.redis) as freeze:
                source_bytes = await self._run_frozen(freeze, self._storage_source_bytes)
                self._ensure_backup_capacity(source_bytes)

                database_path = target / "database.sql"
                storage_path = target / "storage.tar.gz"
                await self._run_frozen(freeze, self._dump_database, database_path)
                await self._run_frozen(freeze, self._archive_storage, storage_path)
                database_hash = await self._run_frozen(freeze, sha256_file, database_path)
                storage_hash = await self._run_frozen(freeze, sha256_file, storage_path)

                manifest = {
                "format_version": FORMAT_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "app_version": application_version(),
                "schema_version": EXPECTED_SCHEMA_VERSION,
                "database_name": self._database_url().database,
                "database_binding": "selected-database",
                "storage_dirs": list(ARCHIVE_DIRS),
                "source_storage_bytes": source_bytes,
                "database_dump": database_path.name,
                "database_sha256": database_hash,
                "storage_archive": storage_path.name,
                "storage_sha256": storage_hash,
                "credentials_included": False,
                "quarantine_included": False,
                    "redis_jobs_included": False,
                }
                await freeze.ensure_alive()
                self._write_json(target / "manifest.json", manifest)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return {"backup_path": str(target), "manifest": manifest}

    def verify(self, backup_dir: Path) -> dict[str, Any]:
        backup_dir = backup_dir.resolve()
        manifest_path = backup_dir / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise BackupError("manifest.json is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise BackupError("Backup manifest must be an object")
        if manifest.get("format_version") not in (1, FORMAT_VERSION):
            raise BackupError(
                f"Unsupported backup format: {manifest.get('format_version')}"
            )
        database_path = self._payload_path(backup_dir, manifest.get("database_dump"))
        storage_path = self._payload_path(backup_dir, manifest.get("storage_archive"))
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
        database_path = self._payload_path(backup_dir, manifest["database_dump"])
        storage_path = self._payload_path(backup_dir, manifest["storage_archive"])
        self._validate_restore_binding(manifest, database_path)

        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)

        async with MaintenanceFreeze(self.redis) as freeze:
            staging = Path(
                tempfile.mkdtemp(
                    prefix=".restore-staging-",
                    dir=str(self.storage_root),
                )
            )
            operation_id = f"{utc_stamp()}-{uuid.uuid4().hex[:12]}"
            rollback_root = self.storage_root / f".restore-rollback-{operation_id}"
            safety_dir = self.backup_root / f"pre-restore-db-{operation_id}"
            safety_dir.mkdir(parents=True, exist_ok=False)
            safety_db = safety_dir / "database.sql"
            state_path = safety_dir / "restore-state.json"
            state = {
                "operation_id": operation_id, "backup_path": str(backup_dir),
                "rollback_storage": str(rollback_root), "staging_storage": str(staging),
                "pre_restore_database_dump": str(safety_db), "state_path": str(state_path),
                "phase": "preparing", "database_restore_started": False,
            }
            marker = json.dumps({"operation_id": operation_id, "state_path": str(state_path)})
            marker_owned = False
            self._write_json(state_path, state)
            try:
                await self._run_frozen(freeze, self._extract_storage, storage_path, staging)
                await self._run_frozen(freeze, self._dump_database, safety_db)
                await freeze.ensure_alive()
                marker_owned = bool(await self.redis.set(recovery_key(), marker, nx=True))
                if not marker_owned:
                    raise BackupError("A previous restore recovery is still pending")
                state["phase"] = "swapping_storage"
                self._write_json(state_path, state)
                self._swap_storage_in(staging, rollback_root)
                state.update(phase="restoring_database", database_restore_started=True)
                self._write_json(state_path, state)
                await self._run_frozen(freeze, self._restore_database, database_path)
                await engine.dispose()
                await ensure_schema_version(engine)
                await freeze.ensure_alive()
                state["phase"] = "restored"
                self._write_json(state_path, state)
                await self._clear_recovery_marker(marker)
                marker_owned = False
                shutil.rmtree(rollback_root, ignore_errors=True)
            except BaseException as exc:
                if marker_owned:
                    if state["phase"] == "restored":
                        # The replacement was fully verified. An uncertain
                        # response while releasing the interlock must not start
                        # a second destructive DB restore in the other direction.
                        state.update(phase="release_uncertain", error_type=type(exc).__name__)
                        self._write_json(state_path, state)
                        raise BackupError(
                            f"Restored DB/storage were verified, but maintenance release is uncertain. "
                            f"Inspect {state_path}; rollback storage and pre-restore DB are preserved."
                        ) from exc
                    # Blocking subprocess/thread work has been drained before
                    # reaching here. Never race a still-running mysql restore.
                    rollback_errors = []
                    try:
                        await freeze.ensure_alive()
                    except BaseException as ownership_error:
                        rollback_errors.append(f"freeze:{type(ownership_error).__name__}")
                    if not rollback_errors:
                        if state["database_restore_started"]:
                            try:
                                await self._run_frozen(freeze, self._restore_database, safety_db)
                                await engine.dispose()
                                await ensure_schema_version(engine)
                                state["database_rollback"] = "succeeded"
                            except BaseException as rollback_error:
                                rollback_errors.append(f"database:{type(rollback_error).__name__}")
                                state["database_rollback"] = "failed"
                        try:
                            if rollback_root.exists():
                                await self._run_frozen(freeze, self._rollback_storage, rollback_root)
                            state["storage_rollback"] = "succeeded"
                        except BaseException as rollback_error:
                            rollback_errors.append(f"storage:{type(rollback_error).__name__}")
                            state["storage_rollback"] = "failed"
                    state.update(phase="recovery_required" if rollback_errors else "rolled_back",
                                 error_type=type(exc).__name__, rollback_errors=rollback_errors)
                    self._write_json(state_path, state)
                    if not rollback_errors:
                        try:
                            await freeze.ensure_alive()
                            await self._clear_recovery_marker(marker)
                        except BaseException as clear_error:
                            state.update(phase="recovery_required", rollback_errors=[f"release:{type(clear_error).__name__}"])
                            self._write_json(state_path, state)
                    raise BackupError(
                        f"Restore failed ({type(exc).__name__}); {state['phase']}. "
                        f"Recovery details: {state_path}; preserved storage: {rollback_root}; "
                        f"pre-restore database: {safety_db}; rollback errors: {state['rollback_errors']}"
                    ) from exc
                raise
            else:
                shutil.rmtree(staging, ignore_errors=True)

        return {
            "restored": True,
            "backup_path": str(backup_dir),
            "pre_restore_database_dump": str(safety_db),
            "schema_version": EXPECTED_SCHEMA_VERSION,
            "restore_state": str(state_path),
        }

    async def _run_frozen(self, freeze, function, *args):
        """Keep the freeze until blocking work actually stops, even on cancel.

        Canceling to_thread only cancels its waiter. Shielding and draining the
        underlying task prevents releasing the gate while mysql/tar still write.
        A restore additionally has a persistent recovery interlock if its
        renewable freeze is lost during this wait.
        """
        await freeze.ensure_alive()
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        pending_error = None
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            except asyncio.TimeoutError:
                if pending_error is None:
                    try:
                        await freeze.ensure_alive()
                    except BaseException as exc:
                        pending_error = exc
            except asyncio.CancelledError as exc:
                pending_error = pending_error or exc
            except Exception:
                break
        try:
            result = task.result()
        except BaseException:
            if pending_error is not None:
                raise pending_error
            raise
        if pending_error is not None:
            raise pending_error
        await freeze.ensure_alive()
        return result

    async def _clear_recovery_marker(self, marker: str) -> None:
        script = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
        if not await self.redis.eval(script, 1, recovery_key(), marker):
            raise BackupError("Restore recovery marker ownership changed")

    async def release_recovery(self, *, confirm_recovery_complete: bool) -> dict:
        if not confirm_recovery_complete:
            raise BackupError("Pass --confirm-recovery-complete only after restoring DB/storage consistency")
        self._database_url()
        async with MaintenanceFreeze(self.redis, allow_recovery=True) as freeze:
            marker = await self.redis.get(recovery_key())
            if not marker:
                return {"released": False, "reason": "no_recovery_pending"}
            await ensure_schema_version(engine)
            await freeze.ensure_alive()
            await self._clear_recovery_marker(marker)
            return {"released": True, "recovery": json.loads(marker)}

    @staticmethod
    def _payload_path(backup_dir: Path, value: Any) -> Path:
        if not isinstance(value, str) or not value or value in {".", ".."} or any(char in value for char in ("/", "\\", ":")):
            raise BackupError("Backup payload must be a plain filename inside the backup directory")
        path = backup_dir / value
        if path.is_symlink() or path.resolve().parent != backup_dir.resolve() or not path.is_file():
            raise BackupError("Backup payload is missing or outside the backup directory")
        return path

    def _validate_restore_binding(self, manifest: dict, source: Path) -> None:
        target = str(self._database_url().database)
        legacy = manifest["format_version"] == 1
        if legacy and manifest.get("database_name") != target:
            raise BackupError("Legacy format-1 dumps can only be restored to their original database name")
        if not legacy and manifest.get("database_binding") != "selected-database":
            raise BackupError("Format-2 backup is missing its selected-database binding")
        identifier = r"(`(?:``|[^`])+`|[A-Za-z0-9_$]+)"
        selector = re.compile(r"^\s*(?:USE\s+|(?:CREATE|DROP)\s+DATABASE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?)" + identifier, re.I)
        prefix = re.compile(r"^\s*(?:USE\b|(?:CREATE|DROP)\s+DATABASE\b)", re.I)
        with source.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                expanded = re.sub(r"/\*!\d{5,6}\s*(.*?)\*/", r"\1", line)
                if not prefix.match(expanded):
                    continue
                match = selector.match(expanded)
                if not legacy or match is None:
                    raise BackupError("Backup SQL contains an unexpected database selection statement")
                selected = match.group(1)
                selected = selected[1:-1].replace("``", "`") if selected.startswith("`") else selected
                if selected != target:
                    raise BackupError("Legacy SQL selects a database other than its configured restore target")

    def _database_url(self) -> URL:
        raw = (
            self.settings.maintenance_database_url.strip()
            or self.settings.database_url
        )
        url = make_url(raw)
        if not url.database:
            raise BackupError("Database URL has no database name")
        app_url = make_url(self.settings.database_url)
        identity = lambda value: ((value.host or "localhost").lower(), value.port or 3306,
                                  value.database, value.query.get("unix_socket"))
        if identity(url) != identity(app_url):
            raise BackupError("MAINTENANCE_DATABASE_URL must use the same host, port and database as DATABASE_URL")
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
            "--default-character-set=utf8mb4",
            "--no-create-db",
            str(url.database),
        ]
        if self._supports_option(binary, "no-tablespaces"):
            cmd.insert(-1, "--no-tablespaces")
        if self._supports_option(binary, "set-gtid-purged"):
            cmd.insert(-1, "--set-gtid-purged=OFF")
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
            "--database", str(url.database),
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
    def _supports_option(binary: str, option: str) -> bool:
        result = subprocess.run(
            [binary, "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return option.lower() in result.stdout.decode(
            "utf-8", errors="replace"
        ).lower()

    @staticmethod
    def _mysql_connection_args(binary: str, url: URL) -> list[str]:
        cmd = [binary]
        if url.host:
            cmd += ["--host", url.host]
        if url.port:
            cmd += ["--port", str(url.port)]
        if url.username:
            cmd += ["--user", url.username]
        if url.query.get("unix_socket"):
            cmd += ["--socket", str(url.query["unix_socket"])]
        return cmd

    def _archive_storage(self, output: Path) -> None:
        with tarfile.open(output, "w:gz") as archive:
            for name in ARCHIVE_DIRS:
                source = self.storage_root / name
                if not source.exists():
                    continue
                if source.is_symlink() or source.resolve() != self.storage_root / name:
                    raise BackupError(f"Refusing to archive redirected storage directory: {source}")
                for path in source.rglob("*"):
                    if path.is_symlink():
                        raise BackupError(
                            f"Refusing to archive symlink inside storage: {path}"
                        )
                archive.add(source, arcname=name, recursive=True)

    def _extract_storage(self, archive_path: Path, staging: Path) -> None:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                self._validate_tar_member(member, staging)
            archive.extractall(staging, filter="data")

    def _validate_tar(self, archive_path: Path) -> None:
        with tarfile.open(archive_path, "r:gz") as archive:
            fake_root = Path("/archive-validation-root")
            for member in archive.getmembers():
                self._validate_tar_member(member, fake_root)

    @staticmethod
    def _validate_tar_member(member: tarfile.TarInfo, root: Path) -> None:
        parts = Path(member.name).parts
        top = parts[0] if parts else ""
        if top not in ARCHIVE_DIRS:
            raise BackupError(
                f"Unexpected top-level path in storage archive: {member.name}"
            )
        if member.issym() or member.islnk():
            raise BackupError(
                f"Links are not allowed in storage archive: {member.name}"
            )
        if not member.isfile() and not member.isdir():
            raise BackupError(f"Special files are not allowed in storage archive: {member.name}")
        destination = (root / member.name).resolve()
        try:
            destination.relative_to(root.resolve())
        except ValueError:
            raise BackupError(
                f"Unsafe path in storage archive: {member.name}"
            ) from None

    def _swap_storage_in(self, staging: Path, rollback_root: Path) -> None:
        rollback_root.mkdir(parents=True, exist_ok=False)
        original = [name for name in ARCHIVE_DIRS if (self.storage_root / name).exists()]
        self._write_json(rollback_root / "layout.json", {"original_dirs": original})
        for name in original:
            current = self.storage_root / name
            if current.is_symlink() or current.resolve() != self.storage_root / name:
                raise BackupError(f"Refusing to swap redirected storage directory: {current}")
            os.replace(current, rollback_root / name)
        for name in ARCHIVE_DIRS:
            incoming = staging / name
            if incoming.exists():
                os.replace(incoming, self.storage_root / name)

    def _rollback_storage(self, rollback_root: Path) -> None:
        layout = json.loads((rollback_root / "layout.json").read_text(encoding="utf-8"))
        original = set(layout["original_dirs"])
        failed_root = rollback_root / f"failed-current-{uuid.uuid4().hex[:12]}"
        for name in ARCHIVE_DIRS:
            current = self.storage_root / name
            old = rollback_root / name
            # A failed partial swap may not have moved this original directory.
            if name in original and not old.exists():
                continue
            if current.exists():
                failed_root.mkdir(parents=True, exist_ok=True)
                os.replace(current, failed_root / name)
            if old.exists():
                # Retain rollback originals even after a successful automatic
                # rollback, so a failed restore always leaves recovery evidence.
                shutil.copytree(old, current, symlinks=True)

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
        temporary = path.with_name(path.name + ".part")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)


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
    if args.command == "release-recovery":
        return await manager.release_recovery(
            confirm_recovery_complete=args.confirm_recovery_complete,
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
    release = sub.add_parser("release-recovery")
    release.add_argument(
        "--confirm-recovery-complete", action="store_true",
        help="Required: confirm DB and archive files have been recovered consistently.",
    )
    return parser


async def _run_and_close(args) -> dict[str, Any]:
    try:
        return await async_main(args)
    finally:
        await close_redis()
        await engine.dispose()


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(_run_and_close(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
