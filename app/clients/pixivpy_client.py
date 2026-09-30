from __future__ import annotations

import asyncio
import time
from pixivpy3 import AppPixivAPI

from app.clients.base import PixivClient
from app.clients.pixiv_mapper import build_illust_snapshot, build_novel_snapshot
from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivNotFoundError, PixivRemoteError, PixivRestrictedError, PixivUnavailableError
from app.schemas.pixiv import RemoteSnapshot


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
        return build_illust_snapshot(getattr(result, "illust", None), pixiv_id)

    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        detail_result, text_result = await asyncio.gather(
            self._call(self.api.novel_detail, pixiv_id),
            self._call(self.api.novel_text, pixiv_id),
        )
        return build_novel_snapshot(getattr(detail_result, "novel", None), text_result, pixiv_id)
