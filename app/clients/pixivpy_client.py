from __future__ import annotations

import asyncio
import time
from pixivpy3 import AppPixivAPI

from app.auth.pixiv_credentials import AutoAuthError, PixivCredentialManager
from app.clients.base import PixivClient
from app.clients.pixiv_mapper import apply_ugoira_metadata, build_illust_snapshot, build_novel_snapshot
from app.clients.pixiv_web_client import PixivWebClient
from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivNotFoundError, PixivRemoteError, PixivRestrictedError, PixivUnavailableError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot


class PixivPyClient(PixivClient):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.api = AppPixivAPI()
        self.credentials = PixivCredentialManager()
        self.refresh_token = self.credentials.current_refresh_token()
        self.web = PixivWebClient(cookie=self.credentials.current_cookie())
        self._auth_lock = asyncio.Lock()
        self._next_auth_at = 0.0

    async def _ensure_auth(self, force: bool = False) -> None:
        if not force and time.monotonic() < self._next_auth_at:
            return

        async with self._auth_lock:
            if not force and time.monotonic() < self._next_auth_at:
                return

            if not self.refresh_token:
                try:
                    self.refresh_token = await self.credentials.ensure_refresh_token()
                except AutoAuthError as exc:
                    raise PixivAuthError(f"Pixiv App authentication unavailable: {exc.status}") from None

            try:
                await asyncio.to_thread(self.api.auth, refresh_token=self.refresh_token)
            except Exception:
                if not self.settings.pixiv_auto_auth:
                    raise PixivAuthError("Pixiv App authentication failed") from None
                try:
                    self.refresh_token = await self.credentials.recover_refresh_token()
                    await asyncio.to_thread(self.api.auth, refresh_token=self.refresh_token)
                except Exception as exc:
                    status = exc.status if isinstance(exc, AutoAuthError) else "authentication_failed"
                    raise PixivAuthError(f"Pixiv App automatic authentication failed: {status}") from None

            self._next_auth_at = time.monotonic() + 45 * 60

    async def _call(self, func, *args):
        await self._ensure_auth()
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            message = str(exc).lower()
            if any(x in message for x in ("oauth", "token", "401", "unauthorized")):
                self._next_auth_at = 0.0
                await self._ensure_auth(force=True)
                try:
                    return await asyncio.to_thread(func, *args)
                except Exception:
                    raise PixivAuthError("Pixiv authentication failed after credential recovery") from None
            if any(x in message for x in ("404", "not found", "does not exist")):
                raise PixivNotFoundError(str(exc)) from exc
            if any(x in message for x in ("403", "forbidden", "restricted", "private")):
                raise PixivRestrictedError(str(exc)) from exc
            if any(x in message for x in ("429", "rate limit", "too many requests", "timeout", "temporarily")):
                raise PixivUnavailableError(str(exc)) from exc
            raise PixivRemoteError(str(exc)) from exc

    async def get_illust_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        result = await self._call(self.api.illust_detail, pixiv_id)
        snapshot = build_illust_snapshot(getattr(result, "illust", None), pixiv_id)
        if snapshot.work_type == WorkType.UGOIRA:
            metadata = await self._call(self.api.ugoira_metadata, pixiv_id)
            if self.settings.ugoira_prefer_original:
                web_metadata = await self._try_web_ugoira_metadata(pixiv_id)
                if web_metadata is not None:
                    metadata = web_metadata
            snapshot = apply_ugoira_metadata(snapshot, metadata)
        return snapshot

    async def _try_web_ugoira_metadata(self, pixiv_id: int) -> dict | None:
        result = await self.web.get_ugoira_metadata(pixiv_id)
        if result is not None or not self.settings.pixiv_auto_auth:
            return result
        try:
            cookie = await self.credentials.recover_cookie()
            self.web.set_cookie(cookie)
        except AutoAuthError:
            return None
        return await self.web.get_ugoira_metadata(pixiv_id)

    async def auth_status(self) -> dict:
        state = self.credentials.state()

        app_configured = bool(self.refresh_token)
        app_authenticated = False
        app_status = "not_configured"
        if app_configured:
            try:
                await self._ensure_auth(force=True)
                app_authenticated = True
                app_status = "ok"
            except PixivAuthError:
                app_status = "authentication_failed"

        # Refresh the Web client from explicit/cache state, but status itself does
        # not trigger a browser login.
        self.web.set_cookie(self.credentials.current_cookie())
        web = await self.web.check_auth()

        return {
            "auto_auth": {
                "enabled": state.auto_enabled,
                "username_configured": state.username_configured,
                "password_configured": state.password_configured,
                "totp_configured": state.totp_configured,
            },
            "app_api": {
                "configured": app_configured,
                "authenticated": app_authenticated,
                "status": app_status,
                "cached_credential_available": state.refresh_token_available,
            },
            "web_cookie": {
                "configured": web.configured,
                "authenticated": web.authenticated,
                "status": web.status,
                "cached_credential_available": state.cookie_available,
            },
        }

    async def recover_auth(self) -> dict:
        app_status = "unchanged"
        web_status = "unchanged"

        try:
            self.refresh_token = await self.credentials.recover_refresh_token()
            self._next_auth_at = 0.0
            await self._ensure_auth(force=True)
            app_status = "ok"
        except AutoAuthError as exc:
            app_status = exc.status
        except PixivAuthError:
            app_status = "authentication_failed"

        probe = await self.web.check_auth()
        if probe.authenticated:
            web_status = "ok"
        else:
            try:
                cookie = await self.credentials.recover_cookie()
                self.web.set_cookie(cookie)
                probe = await self.web.check_auth()
                web_status = "ok" if probe.authenticated else probe.status
            except AutoAuthError as exc:
                web_status = exc.status

        return {
            "app_api": {"status": app_status},
            "web_cookie": {"status": web_status},
        }

    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        detail_result, text_result = await asyncio.gather(
            self._call(self.api.novel_detail, pixiv_id),
            self._call(self.api.novel_text, pixiv_id),
        )
        return build_novel_snapshot(getattr(detail_result, "novel", None), text_result, pixiv_id)
