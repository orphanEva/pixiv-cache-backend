from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
import httpx

from app.core.config import get_settings
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot


class LocalStorage:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.storage_root

    def version_dir(self, work_type: WorkType, pixiv_id: int, version_no: int) -> Path:
        return self.root / work_type.value / str(pixiv_id) / "versions" / str(version_no)

    async def materialize(self, snapshot: RemoteSnapshot, version_no: int) -> tuple[str, list[dict]]:
        target = self.version_dir(snapshot.work_type, snapshot.pixiv_id, version_no)
        target.mkdir(parents=True, exist_ok=False)
        assets: list[dict] = []
        try:
            if snapshot.work_type == WorkType.NOVEL:
                data = (snapshot.text_content or "").encode("utf-8")
                path = target / "novel.txt"
                path.write_bytes(data)
                assets.append({
                    "page_index": 0,
                    "remote_url": None,
                    "local_path": str(path),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                })
                return str(target), assets

            headers = {"Referer": "https://www.pixiv.net/", "User-Agent": "Mozilla/5.0"}
            timeout = httpx.Timeout(self.settings.download_timeout_seconds)
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
                for asset in snapshot.assets:
                    path = target / asset.filename
                    part = path.with_suffix(path.suffix + ".part")
                    sha = hashlib.sha256()
                    size = 0
                    async with client.stream("GET", asset.url) as response:
                        response.raise_for_status()
                        content_type = (response.headers.get("content-type") or "").lower()
                        if content_type and not content_type.startswith("image/"):
                            raise ValueError(f"Unexpected Pixiv asset content-type: {content_type}")
                        declared = response.headers.get("content-length")
                        if declared and int(declared) > self.settings.max_asset_bytes:
                            raise ValueError(f"Pixiv asset exceeds max size: {declared} bytes")
                        with part.open("wb") as fp:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                size += len(chunk)
                                if size > self.settings.max_asset_bytes:
                                    raise ValueError(f"Pixiv asset exceeds max size: {size} bytes")
                                sha.update(chunk)
                                fp.write(chunk)
                    os.replace(part, path)
                    assets.append({
                        "page_index": asset.page_index,
                        "remote_url": asset.url,
                        "local_path": str(path),
                        "sha256": sha.hexdigest(),
                        "size_bytes": size,
                    })
            return str(target), assets
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
