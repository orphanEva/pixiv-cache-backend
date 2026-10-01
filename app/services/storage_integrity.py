from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.work import PixivAsset, PixivVersion


class StorageIntegrityService:
    """Read-only DB/filesystem consistency audit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()
        self.root = self.settings.storage_root.resolve()

    async def audit(self, *, verify_hash: bool = False) -> dict:
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
        missing_files: list[str] = []
        hash_mismatches: list[str] = []
        outside_root: list[str] = []

        for local_path, expected_sha, storage_path, work_id, version_no in rows:
            path = Path(local_path).resolve()
            version_dir = Path(storage_path).resolve()
            if not self._under_root(path) or not self._under_root(version_dir):
                outside_root.append(str(path))
                continue
            db_files.add(path)
            db_version_dirs.add(version_dir)
            if not path.is_file():
                missing_files.append(str(path))
                continue
            if verify_hash and expected_sha:
                actual = self._sha256(path)
                if actual.lower() != expected_sha.lower():
                    hash_mismatches.append(str(path))

        orphan_version_dirs: list[str] = []
        if self.root.exists():
            for path in self.root.glob("*/*/versions/*"):
                if path.is_dir() and path.resolve() not in db_version_dirs:
                    orphan_version_dirs.append(str(path.resolve()))

        part_files = [
            str(path.resolve())
            for path in self.root.rglob("*.part")
            if path.is_file()
        ] if self.root.exists() else []

        return {
            "ok": not (
                missing_files
                or hash_mismatches
                or orphan_version_dirs
                or part_files
                or outside_root
            ),
            "verify_hash": verify_hash,
            "database_file_count": len(db_files),
            "database_version_dir_count": len(db_version_dirs),
            "missing_files": self._limit(missing_files),
            "hash_mismatches": self._limit(hash_mismatches),
            "orphan_version_dirs": self._limit(orphan_version_dirs),
            "part_files": self._limit(part_files),
            "outside_storage_root": self._limit(outside_root),
            "truncated": any(
                len(items) > self.settings.storage_audit_max_findings
                for items in (
                    missing_files,
                    hash_mismatches,
                    orphan_version_dirs,
                    part_files,
                    outside_root,
                )
            ),
        }

    def _under_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _limit(self, items: list[str]) -> list[str]:
        return items[: self.settings.storage_audit_max_findings]

    @staticmethod
    def _sha256(path: Path) -> str:
        sha = hashlib.sha256()
        with path.open("rb") as fp:
            while chunk := fp.read(1024 * 1024):
                sha.update(chunk)
        return sha.hexdigest()
