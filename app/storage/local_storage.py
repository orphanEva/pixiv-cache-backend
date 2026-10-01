from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.errors import PixivUnavailableError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot, UgoiraFrame

logger = logging.getLogger(__name__)


class LocalStorage:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.storage_root

    def version_dir(self, work_type: WorkType, pixiv_id: int, version_no: int) -> Path:
        return self.root / work_type.value / str(pixiv_id) / "versions" / str(version_no)

    async def materialize(self, snapshot: RemoteSnapshot, version_no: int) -> tuple[str, list[dict]]:
        target = self.version_dir(snapshot.work_type, snapshot.pixiv_id, version_no)
        target.mkdir(parents=True, exist_ok=False)
        try:
            if snapshot.work_type == WorkType.NOVEL:
                return str(target), self._write_novel(snapshot, target)
            if snapshot.work_type == WorkType.UGOIRA:
                return str(target), await self._write_ugoira(snapshot, target)
            return str(target), await self._write_images(snapshot, target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _write_novel(self, snapshot: RemoteSnapshot, target: Path) -> list[dict]:
        data = (snapshot.text_content or "").encode("utf-8")
        path = target / "novel.txt"
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

        frame_paths = self._extract_ugoira_frames(
            zip_path, snapshot.ugoira_frames, target / "frames"
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
        metadata_path.write_bytes(metadata_bytes)
        assets.append(self._asset(
            2, "ugoira_meta", metadata_path,
            hashlib.sha256(metadata_bytes).hexdigest(), len(metadata_bytes), None
        ))

        if self.settings.ugoira_generate_mp4:
            mp4_path = target / "preview.mp4"
            try:
                await self._convert_ugoira_to_mp4(
                    frame_paths, snapshot.ugoira_frames, target, mp4_path
                )
                mp4_sha, mp4_size = self._hash_file(mp4_path)
                assets.append(self._asset(
                    3, "ugoira_mp4", mp4_path, mp4_sha, mp4_size, None
                ))
            except Exception:
                # Raw ZIP + exact frame timing are the archival source of truth.
                # A derived MP4 failure must not discard a valid archive.
                logger.warning(
                    "ugoira_mp4_conversion_failed",
                    extra={"pixiv_id": snapshot.pixiv_id},
                    exc_info=True,
                )
                mp4_path.unlink(missing_ok=True)

        if not self.settings.ugoira_keep_extracted_frames:
            shutil.rmtree(target / "frames", ignore_errors=True)

        return assets

    def _extract_ugoira_frames(
        self, zip_path: Path, frames: list[UgoiraFrame], frame_dir: Path
    ) -> list[Path]:
        frame_dir.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(zip_path) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"Corrupt Ugoira ZIP member: {bad}")
            info_by_name = {info.filename: info for info in archive.infolist()}
            total_uncompressed = sum(info.file_size for info in info_by_name.values())
            if total_uncompressed > self.settings.ugoira_max_uncompressed_bytes:
                raise ValueError(
                    f"Ugoira uncompressed data exceeds limit: {total_uncompressed}"
                )

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
        _, stderr = await proc.communicate()
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
                with part.open("wb") as fp:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"Pixiv asset exceeds max size: {size} bytes")
                        sha.update(chunk)
                        fp.write(chunk)
            os.replace(part, path)
            return sha.hexdigest(), size
        finally:
            part.unlink(missing_ok=True)

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Referer": "https://www.pixiv.net/", "User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=httpx.Timeout(self.settings.download_timeout_seconds),
        )

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
