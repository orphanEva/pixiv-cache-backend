from __future__ import annotations

import hashlib
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.work import PixivAsset, PixivVersion


class StorageIntegrityService:
    """DB/filesystem consistency audit with conservative repair mode."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()
        self.root = self.settings.storage_root.resolve()

    async def audit(
        self,
        *,
        verify_hash: bool = False,
        repair_safe: bool = False,
    ) -> dict:
        collected = await self._collect(verify_hash=verify_hash)
        report = self._report(collected, verify_hash=verify_hash)
        if not repair_safe:
            return report

        repairs = self._repair_safe(collected)
        after = self._report(
            await self._collect(verify_hash=verify_hash),
            verify_hash=verify_hash,
        )
        return {
            "repair_safe": True,
            "before": report,
            "repairs": repairs,
            "after": after,
        }

    async def _collect(self, *, verify_hash: bool) -> dict:
        rows = (await self.session.execute(
            select(
                PixivAsset.local_path,
                PixivAsset.sha256,
                PixivVersion.storage_path,
                PixivVersion.work_id,
                PixivVersion.version_no,
            ).join(PixivVersion, PixivAsset.version_id == PixivVersion.id)
        )).all()

        db_files: set[Path] = set()
        db_version_dirs: set[Path] = set()
        missing_files: list[Path] = []
        hash_mismatches: list[Path] = []
        outside_root: list[Path] = []

        for local_path, expected_sha, storage_path, work_id, version_no in rows:
            path = Path(local_path).resolve()
            version_dir = Path(storage_path).resolve()
            if not self._under_root(path) or not self._under_root(version_dir):
                outside_root.append(path)
                continue
            db_files.add(path)
            db_version_dirs.add(version_dir)
            if not path.is_file():
                missing_files.append(path)
                continue
            if verify_hash and expected_sha:
                actual = self._sha256(path)
                if actual.lower() != expected_sha.lower():
                    hash_mismatches.append(path)

        orphan_version_dirs: list[Path] = []
        if self.root.exists():
            for path in self.root.glob("*/*/versions/*"):
                resolved = path.resolve()
                if path.is_dir() and resolved not in db_version_dirs:
                    orphan_version_dirs.append(resolved)

        part_files = (
            [path.resolve() for path in self.root.rglob("*.part") if path.is_file()]
            if self.root.exists()
            else []
        )

        return {
            "db_files": db_files,
            "db_version_dirs": db_version_dirs,
            "missing_files": missing_files,
            "hash_mismatches": hash_mismatches,
            "orphan_version_dirs": orphan_version_dirs,
            "part_files": part_files,
            "outside_root": outside_root,
        }

    def _report(self, data: dict, *, verify_hash: bool) -> dict:
        groups = (
            data["missing_files"],
            data["hash_mismatches"],
            data["orphan_version_dirs"],
            data["part_files"],
            data["outside_root"],
        )
        return {
            "ok": not any(groups),
            "verify_hash": verify_hash,
            "database_file_count": len(data["db_files"]),
            "database_version_dir_count": len(data["db_version_dirs"]),
            "missing_files": self._limit(data["missing_files"]),
            "hash_mismatches": self._limit(data["hash_mismatches"]),
            "orphan_version_dirs": self._limit(data["orphan_version_dirs"]),
            "part_files": self._limit(data["part_files"]),
            "outside_storage_root": self._limit(data["outside_root"]),
            "truncated": any(
                len(items) > self.settings.storage_audit_max_findings
                for items in groups
            ),
        }

    def _repair_safe(self, data: dict) -> dict:
        now = time.time()
        deleted_parts: list[str] = []
        quarantined: list[str] = []
        skipped_recent: list[str] = []

        for path in data["part_files"]:
            if not path.exists() or not self._under_root(path):
                continue
            age = now - path.stat().st_mtime
            if age < self.settings.storage_part_stale_seconds:
                skipped_recent.append(str(path))
                continue
            try:
                path.unlink()
                deleted_parts.append(str(path))
            except FileNotFoundError:
                pass

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine_root = self.root / ".quarantine" / stamp
        for path in data["orphan_version_dirs"]:
            if not path.exists() or not self._under_root(path):
                continue
            if self._tree_has_recent_activity(
                path,
                self.settings.storage_orphan_grace_seconds,
                now,
            ):
                skipped_recent.append(str(path))
                continue
            relative = path.relative_to(self.root)
            destination = quarantine_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            quarantined.append(str(destination))

        return {
            "deleted_stale_part_files": self._limit_strings(deleted_parts),
            "quarantined_orphan_version_dirs": self._limit_strings(quarantined),
            "skipped_recent": self._limit_strings(skipped_recent),
            "note": (
                "Missing DB files, hash mismatches and paths outside the storage "
                "root are never automatically changed."
            ),
        }

    def _tree_has_recent_activity(
        self,
        path: Path,
        grace_seconds: int,
        now: float,
    ) -> bool:
        cutoff = now - max(0, int(grace_seconds))
        try:
            if path.stat().st_mtime >= cutoff:
                return True
        except FileNotFoundError:
            return False
        try:
            for child in path.rglob("*"):
                try:
                    if child.stat().st_mtime >= cutoff:
                        return True
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            return False
        return False

    def _under_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _limit(self, items: list[Path]) -> list[str]:
        return self._limit_strings([str(item) for item in items])

    def _limit_strings(self, items: list[str]) -> list[str]:
        return items[: self.settings.storage_audit_max_findings]

    @staticmethod
    def _sha256(path: Path) -> str:
        sha = hashlib.sha256()
        with path.open("rb") as fp:
            while chunk := fp.read(1024 * 1024):
                sha.update(chunk)
        return sha.hexdigest()
