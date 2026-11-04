from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.errors import ArchiveMaintenanceError, ArchiveStorageError, PixivUnavailableError
from app.core.maintenance import get_current_writer, require_writer_alive
from app.core.pixiv_rate_limit import PixivRateLimiter
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot, UgoiraFrame

logger = logging.getLogger(__name__)


class LocalStorage:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.storage_root
        self.rate_limiter = PixivRateLimiter()

    def version_dir(self, work_type: WorkType, pixiv_id: int, version_no: int) -> Path:
        return self.root / work_type.value / str(pixiv_id) / "versions" / str(version_no)

    def validate_local_asset(self, path: Path, sha256: str | None, size_bytes: int) -> bool:
        """Check registered bytes without trusting an arbitrary DB path."""
        root = self.root.resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            return False
        if not sha256 or len(sha256) != 64 or size_bytes <= 0 or path.stat().st_size != size_bytes:
            return False
        actual_sha, actual_size = self._hash_file(path)
        return actual_size == size_bytes and actual_sha.lower() == sha256.lower()

    async def rebuild_ugoira_preview(
        self, *, pixiv_id: int, version_no: int, zip_path: Path,
        metadata_path: Path, zip_sha256: str, zip_size_bytes: int,
        metadata_sha256: str, metadata_size_bytes: int, before_publish=None,
        progress=None,
    ) -> dict:
        """Build one derivative solely from verified, already archived sources.

        The caller holds the work/maintenance guards. It registers the returned
        asset in the same history version after this atomic filesystem publish.
        """
        target = self.version_dir(WorkType.UGOIRA, pixiv_id, version_no)
        expected = self.root.resolve() / "ugoira" / str(pixiv_id) / "versions" / str(version_no)
        if target.is_symlink() or target.resolve() != expected or not target.is_dir():
            raise ArchiveStorageError("Unsafe or missing archived Ugoira version directory")
        if any(path.parent.resolve() != expected for path in (zip_path, metadata_path)):
            raise ArchiveStorageError("Ugoira source assets do not belong to the requested version")
        await require_writer_alive()
        self._ensure_disk_space()
        scratch = Path(tempfile.mkdtemp(prefix=".mp4-", dir=target))
        try:
            def prepare():
                if zip_size_bytes > self.settings.max_asset_bytes:
                    raise ValueError("Archived Ugoira ZIP exceeds configured size limit")
                if metadata_size_bytes > min(self.settings.max_asset_bytes, 16 * 1024 * 1024):
                    raise ValueError("Ugoira metadata exceeds configured size limit")
                if not self.validate_local_asset(zip_path, zip_sha256, zip_size_bytes):
                    raise ValueError("Archived Ugoira ZIP hash or size does not match its record")
                if not self.validate_local_asset(metadata_path, metadata_sha256, metadata_size_bytes):
                    raise ValueError("Archived Ugoira metadata hash or size does not match its record")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict) or str(metadata.get("pixiv_id")) != str(pixiv_id):
                    raise ValueError("Ugoira metadata belongs to a different work")
                if str(metadata.get("zip_sha256", "")).lower() != zip_sha256.lower():
                    raise ValueError("Ugoira metadata references different ZIP bytes")
                raw_frames = metadata.get("frames")
                if not isinstance(raw_frames, list) or not 0 < len(raw_frames) <= self.settings.ugoira_max_frames:
                    raise ValueError("Invalid Ugoira frame count")
                frames = []
                names = set()
                for frame in raw_frames:
                    if not isinstance(frame, dict) or type(frame.get("delay")) is not int or frame["delay"] <= 0:
                        raise ValueError("Ugoira frame delays must be positive integer milliseconds")
                    name = frame.get("file")
                    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in names:
                        raise ValueError("Invalid or duplicate Ugoira frame filename")
                    names.add(name)
                    frames.append(UgoiraFrame(file=name, delay=frame["delay"]))
                return frames, self._extract_ugoira_frames(zip_path, frames, scratch / "frames")

            frames, frame_paths = await self._local_thread(prepare)
            if progress:
                await progress("converting")
            output = scratch / "preview.mp4"
            await self._convert_ugoira_to_mp4(frame_paths, frames, scratch, output)
            sha, size = await self._local_thread(self._hash_file, output)
            if size <= 0 or size > self.settings.max_asset_bytes:
                raise ArchiveStorageError("Generated MP4 is empty or exceeds configured size limit")
            if before_publish:
                await before_publish()
            destination = target / "preview.mp4"
            if destination.is_symlink():
                raise ArchiveStorageError("Refusing to replace a symbolic-link preview")
            os.replace(output, destination)
            return self._asset(3, "ugoira_mp4", destination, sha, size, None)
        finally:
            await self._local_thread(shutil.rmtree, scratch, True)

    @staticmethod
    async def _local_thread(function, *args):
        # Wait for filesystem work before cleaning its temporary directory on
        # cancellation; the Redis writer/lease tasks remain responsive meanwhile.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            result = task.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError
        return result

    @staticmethod
    def _check_local_writer() -> None:
        writer = get_current_writer()
        if writer is not None:
            writer.check_alive()

    def quarantine_uncommitted_version(
        self, work_type: WorkType, pixiv_id: int, version_no: int
    ) -> Path | None:
        """Move an abandoned version aside without deleting its contents.

        The caller MUST hold the work lock and verify that this version is not
        registered in the database. LocalStorage cannot establish DB ownership.
        """
        target = self.version_dir(work_type, pixiv_id, version_no)
        if not target.exists() and not target.is_symlink():
            return None
        canonical_root = self.root.resolve()
        expected = canonical_root / work_type.value / str(pixiv_id) / "versions" / str(version_no)
        if target.is_symlink() or target.resolve() != expected or not target.is_dir():
            raise ArchiveStorageError("Refusing to quarantine an unsafe version path")
        destination = canonical_root / ".quarantine" / f"uncommitted-{uuid.uuid4().hex}" / expected.relative_to(canonical_root)
        if not destination.resolve().is_relative_to(canonical_root):
            raise ArchiveStorageError("Quarantine path is outside archive storage")
        destination.parent.mkdir(parents=True, exist_ok=False)
        os.replace(target, destination)
        logger.warning("uncommitted_version_quarantined", extra={
            "pixiv_id": pixiv_id, "work_type": work_type.value, "version": version_no,
        })
        return destination

    async def materialize(self, snapshot: RemoteSnapshot, version_no: int) -> tuple[str, list[dict]]:
        await require_writer_alive()
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_disk_space()
        target = self.version_dir(snapshot.work_type, snapshot.pixiv_id, version_no)
        target.mkdir(parents=True, exist_ok=False)
        try:
            if snapshot.work_type == WorkType.NOVEL:
                return str(target), self._write_novel(snapshot, target)
            if snapshot.work_type == WorkType.UGOIRA:
                return str(target), await self._write_ugoira(snapshot, target)
            return str(target), await self._write_images(snapshot, target)
        except (Exception, asyncio.CancelledError):
            await self._local_thread(shutil.rmtree, target, True)
            raise

    def _write_novel(self, snapshot: RemoteSnapshot, target: Path) -> list[dict]:
        data = (snapshot.text_content or "").encode("utf-8")
        path = target / "novel.txt"
        self._check_local_writer()
        path.write_bytes(data)
        return [self._asset(
            0, "novel_txt", path, hashlib.sha256(data).hexdigest(), len(data), None
        )]

    async def remote_images_match(self, snapshot: RemoteSnapshot, existing_assets) -> bool:
        """Deep-check remote still-image bytes against the current archived hashes."""
        expected = {
            int(asset.page_index): str(asset.sha256 or "").lower()
            for asset in existing_assets
            if getattr(asset, "file_type", None) == "image"
        }
        if len(expected) != len(snapshot.assets):
            return False
        try:
            async with self._http_client() as client:
                for asset in snapshot.assets:
                    expected_sha = expected.get(asset.page_index)
                    if not expected_sha:
                        return False
                    actual_sha, _ = await self._hash_remote_image(client, asset.url)
                    if actual_sha.lower() != expected_sha:
                        return False
            return True
        except (httpx.HTTPError, ValueError) as exc:
            raise PixivUnavailableError(
                f"Deep image validation failed: {exc.__class__.__name__}"
            ) from exc

    async def _hash_remote_image(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, int]:
        sha = hashlib.sha256()
        size = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = (
                response.headers.get("content-type") or ""
            ).split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError(
                    f"Unexpected Pixiv image content-type: {content_type}"
                )
            declared = response.headers.get("content-length")
            if declared and int(declared) > self.settings.max_asset_bytes:
                raise ValueError("Pixiv image exceeds configured size limit")
            async for chunk in response.aiter_bytes(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_asset_bytes:
                    raise ValueError("Pixiv image exceeds configured size limit")
                sha.update(chunk)
        return sha.hexdigest(), size

    async def _write_images(self, snapshot: RemoteSnapshot, target: Path) -> list[dict]:
        assets: list[dict] = []
        async with self._http_client() as client:
            for asset in snapshot.assets:
                path = target / asset.filename
                sha, size = await self._download_stream(
                    client, asset.url, path, require_image=True,
                    max_bytes=self.settings.max_asset_bytes,
                )
                assets.append(self._asset(
                    asset.page_index, "image", path, sha, size, asset.url
                ))
        return assets

    async def _write_ugoira(self, snapshot: RemoteSnapshot, target: Path) -> list[dict]:
        if not snapshot.ugoira_zip_url or not snapshot.ugoira_frames:
            raise ValueError("Ugoira snapshot is missing ZIP URL or frame metadata")
        if len(snapshot.ugoira_frames) > self.settings.ugoira_max_frames:
            raise ValueError(
                f"Ugoira has too many frames: {len(snapshot.ugoira_frames)} > "
                f"{self.settings.ugoira_max_frames}"
            )

        assets: list[dict] = []
        async with self._http_client() as client:
            zip_path = target / "original.zip"
            zip_sha, zip_size = await self._download_stream(
                client, snapshot.ugoira_zip_url, zip_path,
                allowed_content_types=(
                    "application/zip",
                    "application/x-zip-compressed",
                    "application/octet-stream",
                    "binary/octet-stream",
                ),
                max_bytes=self.settings.max_asset_bytes,
            )
            assets.append(self._asset(
                0, "ugoira_zip", zip_path, zip_sha, zip_size, snapshot.ugoira_zip_url
            ))

            # Keep the cover separately when Pixiv supplies one.
            if snapshot.assets:
                cover = snapshot.assets[0]
                cover_suffix = Path(cover.filename).suffix or ".jpg"
                cover_path = target / f"cover{cover_suffix}"
                cover_sha, cover_size = await self._download_stream(
                    client, cover.url, cover_path, require_image=True,
                    max_bytes=self.settings.max_asset_bytes,
                )
                assets.append(self._asset(
                    1, "cover", cover_path, cover_sha, cover_size, cover.url
                ))

        await require_writer_alive()
        frame_paths = await self._local_thread(
            self._extract_ugoira_frames, zip_path, snapshot.ugoira_frames, target / "frames"
        )
        metadata_path = target / "ugoira_meta.json"
        metadata_bytes = json.dumps(
            {
                "pixiv_id": snapshot.pixiv_id,
                "zip_url": snapshot.ugoira_zip_url,
                "zip_sha256": zip_sha,
                "frames": [frame.model_dump() for frame in snapshot.ugoira_frames],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        await require_writer_alive()
        metadata_path.write_bytes(metadata_bytes)
        assets.append(self._asset(
            2, "ugoira_meta", metadata_path,
            hashlib.sha256(metadata_bytes).hexdigest(), len(metadata_bytes), None
        ))

        if self.settings.ugoira_generate_mp4:
            mp4_path = target / "preview.mp4"
            partial_mp4 = target / f".preview-{uuid.uuid4().hex}.mp4"
            try:
                await require_writer_alive()
                await self._convert_ugoira_to_mp4(
                    frame_paths, snapshot.ugoira_frames, target, partial_mp4
                )
                mp4_sha, mp4_size = await self._local_thread(self._hash_file, partial_mp4)
                await require_writer_alive()
                os.replace(partial_mp4, mp4_path)
                assets.append(self._asset(
                    3, "ugoira_mp4", mp4_path, mp4_sha, mp4_size, None
                ))
            except ArchiveMaintenanceError:
                raise
            except Exception:
                # Raw ZIP + exact frame timing are the archival source of truth.
                # A derived MP4 failure must not discard a valid archive.
                logger.warning(
                    "ugoira_mp4_conversion_failed",
                    extra={"pixiv_id": snapshot.pixiv_id},
                    exc_info=True,
                )
                mp4_path.unlink(missing_ok=True)
            finally:
                partial_mp4.unlink(missing_ok=True)

        if not self.settings.ugoira_keep_extracted_frames:
            await self._local_thread(shutil.rmtree, target / "frames", True)

        return assets

    def _extract_ugoira_frames(
        self, zip_path: Path, frames: list[UgoiraFrame], frame_dir: Path
    ) -> list[Path]:
        self._check_local_writer()
        frame_dir.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(zip_path) as archive:
            entries = archive.infolist()
            info_by_name = {info.filename: info for info in entries}
            if len(info_by_name) != len(entries):
                raise ValueError("Duplicate Ugoira ZIP members")
            total_uncompressed = sum(info.file_size for info in entries)
            if total_uncompressed > self.settings.ugoira_max_uncompressed_bytes:
                raise ValueError(
                    f"Ugoira uncompressed data exceeds limit: {total_uncompressed}"
                )
            self._ensure_disk_space(total_uncompressed)
            bad = archive.testzip()
            if bad:
                raise ValueError(f"Corrupt Ugoira ZIP member: {bad}")

            paths: list[Path] = []
            for index, frame in enumerate(frames):
                info = info_by_name.get(frame.file)
                if info is None:
                    raise ValueError(f"Ugoira frame missing from ZIP: {frame.file}")
                suffix = Path(frame.file).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    raise ValueError(f"Unsupported Ugoira frame type: {suffix}")
                data = archive.read(info)
                output = frame_dir / f"{index:06d}{suffix}"
                self._check_local_writer()
                output.write_bytes(data)
                paths.append(output)
            return paths

    async def _convert_ugoira_to_mp4(
        self,
        frame_paths: list[Path],
        frames: list[UgoiraFrame],
        target: Path,
        output: Path,
    ) -> None:
        if len(frame_paths) != len(frames) or not frame_paths:
            raise ValueError("Ugoira frame path/metadata count mismatch")

        await require_writer_alive()
        concat_path = target / "ffmpeg_frames.txt"
        lines: list[str] = []
        for path, frame in zip(frame_paths, frames, strict=True):
            relative = path.relative_to(target).as_posix()
            lines.append(f"file '{relative}'")
            lines.append(f"duration {frame.delay / 1000:.6f}")
        # concat demuxer needs the final frame repeated so its duration is honored.
        lines.append(f"file '{frame_paths[-1].relative_to(target).as_posix()}'")
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            self.settings.ffmpeg_binary,
            "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_path.name,
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-vsync", "vfr",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-y", output.name,
            cwd=str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.ugoira_conversion_timeout_seconds,
            )
        except (asyncio.CancelledError, TimeoutError):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.communicate()
            raise
        finally:
            concat_path.unlink(missing_ok=True)
        if proc.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg Ugoira conversion failed ({proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[-2000:]}"
            )

    async def _download_stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        path: Path,
        *,
        require_image: bool = False,
        allowed_content_types: tuple[str, ...] = (),
        max_bytes: int,
    ) -> tuple[str, int]:
        part = path.with_name(path.name + ".part")
        sha = hashlib.sha256()
        size = 0
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
                if require_image and content_type and not content_type.startswith("image/"):
                    raise ValueError(f"Unexpected Pixiv image content-type: {content_type}")
                if allowed_content_types and content_type and content_type not in allowed_content_types:
                    raise ValueError(f"Unexpected Pixiv asset content-type: {content_type}")
                declared = response.headers.get("content-length")
                if declared and int(declared) > max_bytes:
                    raise ValueError(f"Pixiv asset exceeds max size: {declared} bytes")
                if declared:
                    self._ensure_disk_space(int(declared))
                next_space_check = 64 * 1024 * 1024
                with part.open("wb") as fp:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"Pixiv asset exceeds max size: {size} bytes")
                        if size >= next_space_check:
                            self._ensure_disk_space()
                            next_space_check += 64 * 1024 * 1024
                        sha.update(chunk)
                        fp.write(chunk)
            await require_writer_alive()
            os.replace(part, path)
            return sha.hexdigest(), size
        finally:
            part.unlink(missing_ok=True)

    def _ensure_disk_space(self, required_extra: int = 0) -> None:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        required = self.settings.storage_min_free_bytes + max(0, int(required_extra))
        if free < required:
            raise ArchiveStorageError(
                f"Insufficient archive storage: free={free} required={required}"
            )

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Referer": "https://www.pixiv.net/", "User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=httpx.Timeout(self.settings.download_timeout_seconds),
            event_hooks={"request": [self._pace_download], "response": [self._download_response]},
        )

    async def _pace_download(self, request: httpx.Request) -> None:
        await self.rate_limiter.wait()

    async def _download_response(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            await self.rate_limiter.penalize(response.headers.get("Retry-After"))

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        sha = hashlib.sha256()
        size = 0
        with path.open("rb") as fp:
            while chunk := fp.read(1024 * 1024):
                sha.update(chunk)
                size += len(chunk)
        return sha.hexdigest(), size

    @staticmethod
    def _asset(
        page_index: int,
        file_type: str,
        path: Path,
        sha256: str,
        size_bytes: int,
        remote_url: str | None,
    ) -> dict:
        return {
            "page_index": page_index,
            "file_type": file_type,
            "remote_url": remote_url,
            "local_path": str(path),
            "sha256": sha256,
            "size_bytes": size_bytes,
        }
