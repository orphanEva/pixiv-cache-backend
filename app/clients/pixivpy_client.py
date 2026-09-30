from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from typing import Any
from pixivpy3 import AppPixivAPI

from app.clients.base import PixivClient
from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivNotFoundError, PixivRemoteError, PixivRestrictedError, PixivUnavailableError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteAsset, RemoteSnapshot


def _obj_to_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _obj_to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_obj_to_dict(v) for v in value]
    if hasattr(value, "items"):
        return {k: _obj_to_dict(v) for k, v in value.items()}
    return value


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _token(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PixivPyClient(PixivClient):
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.pixiv_refresh_token:
            raise RuntimeError("PIXIV_REFRESH_TOKEN is required")
        self.api = AppPixivAPI()
        self.refresh_token = settings.pixiv_refresh_token
        self._auth_lock = asyncio.Lock()
        self._next_auth_at = 0.0

    async def _ensure_auth(self, force: bool = False) -> None:
        if not force and time.monotonic() < self._next_auth_at:
            return
        async with self._auth_lock:
            if not force and time.monotonic() < self._next_auth_at:
                return
            try:
                await asyncio.to_thread(self.api.auth, refresh_token=self.refresh_token)
            except Exception as exc:
                raise PixivAuthError(f"Pixiv authentication failed: {exc}") from exc
            self._next_auth_at = time.monotonic() + 45 * 60

    async def _call(self, func, *args):
        await self._ensure_auth()
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            message = str(exc).lower()
            if any(x in message for x in ("oauth", "token", "401", "unauthorized")):
                await self._ensure_auth(force=True)
                try:
                    return await asyncio.to_thread(func, *args)
                except Exception as retry_exc:
                    raise PixivAuthError(f"Pixiv authentication failed after refresh: {retry_exc}") from retry_exc
            if any(x in message for x in ("404", "not found", "does not exist")):
                raise PixivNotFoundError(str(exc)) from exc
            if any(x in message for x in ("403", "forbidden", "restricted", "private")):
                raise PixivRestrictedError(str(exc)) from exc
            if any(x in message for x in ("429", "rate limit", "too many requests", "timeout", "temporarily")):
                raise PixivUnavailableError(str(exc)) from exc
            raise PixivRemoteError(str(exc)) from exc

    async def get_illust_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        result = await self._call(self.api.illust_detail, pixiv_id)
        illust = _obj_to_dict(getattr(result, "illust", None))
        if not illust or illust.get("id") is None:
            raise PixivNotFoundError(f"Pixiv illust {pixiv_id} not found")

        assets: list[RemoteAsset] = []
        meta_pages = illust.get("meta_pages") or []
        if meta_pages:
            for idx, page in enumerate(meta_pages):
                urls = page.get("image_urls") or {}
                url = urls.get("original") or urls.get("large")
                if url:
                    assets.append(RemoteAsset(page_index=idx, url=url, filename=f"{idx}{self._suffix(url)}"))
        else:
            urls = illust.get("meta_single_page") or {}
            url = urls.get("original_image_url") or (illust.get("image_urls") or {}).get("large")
            if url:
                assets.append(RemoteAsset(page_index=0, url=url, filename=f"0{self._suffix(url)}"))

        if not assets:
            raise PixivRestrictedError(f"Pixiv illust {pixiv_id} has no downloadable image URL")

        user = illust.get("user") or {}
        update_dt = _parse_dt(illust.get("updated_at") or illust.get("update_date"))
        fingerprint = {
            "id": illust.get("id"),
            "title": illust.get("title"),
            "caption": illust.get("caption"),
            "tags": illust.get("tags"),
            "page_count": illust.get("page_count"),
            "type": illust.get("type"),
            "assets": [a.model_dump() for a in assets],
            "updated_at": update_dt,
        }
        return RemoteSnapshot(
            pixiv_id=pixiv_id,
            work_type=WorkType.ILLUST,
            title=illust.get("title") or "",
            author_id=user.get("id"),
            author_name=user.get("name"),
            created_at=_parse_dt(illust.get("create_date")),
            updated_at=update_dt,
            metadata=illust,
            assets=assets,
            version_token=_token(fingerprint),
        )

    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        detail_result, text_result = await asyncio.gather(
            self._call(self.api.novel_detail, pixiv_id),
            self._call(self.api.novel_text, pixiv_id),
        )
        novel = _obj_to_dict(getattr(detail_result, "novel", None))
        text_payload = _obj_to_dict(text_result)
        if not novel or novel.get("id") is None:
            raise PixivNotFoundError(f"Pixiv novel {pixiv_id} not found")

        text = text_payload.get("novel_text") or text_payload.get("text") or ""
        user = novel.get("user") or {}
        update_dt = _parse_dt(novel.get("updated_at") or novel.get("update_date"))
        fingerprint = {
            "id": novel.get("id"),
            "title": novel.get("title"),
            "caption": novel.get("caption"),
            "tags": novel.get("tags"),
            "series": novel.get("series"),
            "text": text,
            "updated_at": update_dt,
        }
        return RemoteSnapshot(
            pixiv_id=pixiv_id,
            work_type=WorkType.NOVEL,
            title=novel.get("title") or "",
            author_id=user.get("id"),
            author_name=user.get("name"),
            created_at=_parse_dt(novel.get("create_date")),
            updated_at=update_dt,
            metadata=novel,
            text_content=text,
            version_token=_token(fingerprint),
        )

    @staticmethod
    def _suffix(url: str) -> str:
        path = url.split("?", 1)[0]
        suffix = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ".jpg"
        return suffix if len(suffix) <= 6 else ".jpg"
