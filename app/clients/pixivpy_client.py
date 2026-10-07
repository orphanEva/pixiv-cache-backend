from __future__ import annotations

import asyncio
import time
from typing import Any, NoReturn

from pixivpy3 import AppPixivAPI

from app.auth.pixiv_credentials import AutoAuthError, PixivCredentialManager
from app.clients.base import PixivClient
from app.clients.pixiv_mapper import (
    apply_ugoira_metadata,
    build_illust_snapshot,
    build_novel_snapshot,
    to_dict,
)
from app.clients.pixiv_web_client import PixivWebClient
from app.core.config import get_settings
from app.core.errors import (
    PixivAuthError,
    PixivNotFoundError,
    PixivRemoteError,
    PixivRestrictedError,
    PixivUnavailableError,
)
from app.models.work import WorkType
from app.schemas.pixiv import DiscoveredWork, DiscoveryPage, RemoteSnapshot


class PixivPyClient(PixivClient):
    """Serialized wrapper around one PixivPy AppPixivAPI instance.

    PixivPy does not document AppPixivAPI as thread-safe. All calls touching the
    shared SDK instance, including auth(), therefore pass through _api_lock.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.api = AppPixivAPI()
        self.credentials = PixivCredentialManager()
        self.refresh_token = self.credentials.current_refresh_token()
        self.web = PixivWebClient(cookie=self.credentials.current_cookie())
        self._auth_lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()
        self._next_auth_at = 0.0

    async def _invoke_api(self, func, *args, **kwargs):
        async with self._api_lock:
            return await asyncio.to_thread(func, *args, **kwargs)

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
                    raise PixivAuthError(
                        f"Pixiv App authentication unavailable: {exc.status}"
                    ) from None

            try:
                await self._invoke_api(self.api.auth, refresh_token=self.refresh_token)
            except Exception:
                if not self.settings.pixiv_auto_auth:
                    raise PixivAuthError("Pixiv App authentication failed") from None
                try:
                    self.refresh_token = await self.credentials.recover_refresh_token()
                    await self._invoke_api(
                        self.api.auth, refresh_token=self.refresh_token
                    )
                except Exception as exc:
                    status = (
                        exc.status
                        if isinstance(exc, AutoAuthError)
                        else "authentication_failed"
                    )
                    raise PixivAuthError(
                        f"Pixiv App automatic authentication failed: {status}"
                    ) from None

            self._next_auth_at = time.monotonic() + 45 * 60

    async def _call(self, func, *args, **kwargs):
        await self._ensure_auth()
        try:
            result = await self._invoke_api(func, *args, **kwargs)
            self._raise_for_pixiv_error(result)
            return result
        except PixivAuthError:
            self._next_auth_at = 0.0
            await self._ensure_auth(force=True)
            try:
                result = await self._invoke_api(func, *args, **kwargs)
                self._raise_for_pixiv_error(result)
                return result
            except PixivRemoteError:
                raise
            except Exception as retry_exc:
                self._raise_mapped_exception(retry_exc)
        except PixivRemoteError:
            raise
        except Exception as exc:
            if self._exception_is_auth(exc):
                self._next_auth_at = 0.0
                await self._ensure_auth(force=True)
                try:
                    result = await self._invoke_api(func, *args, **kwargs)
                    self._raise_for_pixiv_error(result)
                    return result
                except PixivRemoteError:
                    raise
                except Exception as retry_exc:
                    self._raise_mapped_exception(retry_exc)
            self._raise_mapped_exception(exc)

    @staticmethod
    def _error_text(payload: Any) -> str:
        data = to_dict(payload)
        if not isinstance(data, dict):
            return str(data or "")
        error = data.get("error")
        if not error:
            return ""
        if isinstance(error, dict):
            pieces = [
                error.get("message"),
                error.get("user_message"),
                error.get("reason"),
                error.get("details"),
            ]
            return " ".join(str(item) for item in pieces if item).strip()
        return str(error).strip()

    def _raise_for_pixiv_error(self, result: Any) -> None:
        message = self._error_text(result)
        if message:
            self._raise_mapped_message(message)

    @staticmethod
    def _exception_is_auth(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("oauth", "invalid_grant", "token", "401", "unauthorized")
        )

    def _raise_mapped_exception(self, exc: Exception) -> NoReturn:
        self._raise_mapped_message(str(exc), cause=exc)

    @staticmethod
    def _raise_mapped_message(
        message: str, cause: Exception | None = None
    ) -> NoReturn:
        text = message.lower()
        if any(
            marker in text
            for marker in (
                "oauth",
                "invalid_grant",
                "access token",
                "refresh token",
                "401",
                "unauthorized",
                "authentication required",
            )
        ):
            raise PixivAuthError(message or "Pixiv authentication failed") from cause
        if any(
            marker in text
            for marker in (
                "404",
                "not found",
                "does not exist",
                "deleted",
                "work not found",
            )
        ):
            raise PixivNotFoundError(message or "Pixiv work not found") from cause
        if any(
            marker in text
            for marker in (
                "403",
                "forbidden",
                "restricted",
                "private",
                "not public",
                "閲覧できません",
            )
        ):
            raise PixivRestrictedError(message or "Pixiv work is restricted") from cause
        if any(
            marker in text
            for marker in (
                "429",
                "rate limit",
                "too many requests",
                "timeout",
                "timed out",
                "temporarily",
                "service unavailable",
                "503",
            )
        ):
            raise PixivUnavailableError(message or "Pixiv is temporarily unavailable") from cause
        raise PixivRemoteError(message or "Pixiv returned an unknown error") from cause

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
        # gather() is kept so callers do not depend on ordering, while _api_lock
        # guarantees the shared PixivPy instance itself is never used concurrently.
        detail_result, text_result = await asyncio.gather(
            self._call(self.api.novel_detail, pixiv_id),
            self._call(self.api.novel_text, pixiv_id),
        )
        return build_novel_snapshot(
            getattr(detail_result, "novel", None), text_result, pixiv_id
        )

    async def get_authenticated_user_id(self) -> int:
        await self._ensure_auth()
        async with self._api_lock:
            value = getattr(self.api, "user_id", None)
        if not value:
            raise PixivAuthError("Pixiv authenticated user id is unavailable")
        return int(value)

    async def discover_author_illusts(
        self, user_id: int, next_params: dict | None = None
    ) -> DiscoveryPage:
        if next_params:
            result = await self._call(self.api.user_illusts, **next_params)
        else:
            # Omitting the API type filter lets Pixiv return the user's visual
            # works and preserves manga/ugoira types in each item.
            result = await self._call(self.api.user_illusts, user_id, type=None)
        return await self._discovery_page(result, "illusts", WorkType.ILLUST)

    async def discover_author_novels(
        self, user_id: int, next_params: dict | None = None
    ) -> DiscoveryPage:
        result = (
            await self._call(self.api.user_novels, **next_params)
            if next_params
            else await self._call(self.api.user_novels, user_id)
        )
        return await self._discovery_page(result, "novels", WorkType.NOVEL)

    async def discover_bookmark_illusts(
        self, user_id: int, restrict: str, next_params: dict | None = None
    ) -> DiscoveryPage:
        result = (
            await self._call(self.api.user_bookmarks_illust, **next_params)
            if next_params
            else await self._call(
                self.api.user_bookmarks_illust, user_id, restrict=restrict
            )
        )
        return await self._discovery_page(result, "illusts", WorkType.ILLUST)

    async def discover_bookmark_novels(
        self, user_id: int, restrict: str, next_params: dict | None = None
    ) -> DiscoveryPage:
        result = (
            await self._call(self.api.user_bookmarks_novel, **next_params)
            if next_params
            else await self._call(
                self.api.user_bookmarks_novel, user_id, restrict=restrict
            )
        )
        return await self._discovery_page(result, "novels", WorkType.NOVEL)

    async def _discovery_page(
        self,
        result: Any,
        collection_name: str,
        default_type: WorkType,
    ) -> DiscoveryPage:
        data = to_dict(result)
        if not isinstance(data, dict):
            raise PixivRemoteError("Pixiv discovery response is not an object")
        raw_items = data.get(collection_name) or []
        items: list[DiscoveredWork] = []
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            work_type = default_type
            if default_type != WorkType.NOVEL:
                raw_type = str(raw.get("type") or "illust")
                if raw_type in ("illust", "manga", "ugoira"):
                    work_type = WorkType(raw_type)
            items.append(
                DiscoveredWork(
                    pixiv_id=int(raw["id"]),
                    work_type=work_type,
                )
            )
        next_url = data.get("next_url")
        next_params = None
        if next_url:
            next_params = await self._invoke_api(self.api.parse_qs, next_url)
        return DiscoveryPage(items=items, next_params=next_params)
