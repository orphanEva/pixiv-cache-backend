from __future__ import annotations

import asyncio
import time
from typing import Any, NoReturn

from pixivpy3 import AppPixivAPI

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
        self.refresh_token = self.settings.pixiv_refresh_token.strip()
        self.web = PixivWebClient(cookie=self.settings.pixiv_cookie)
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
                raise PixivAuthError("Pixiv App authentication unavailable: configure PIXIV_REFRESH_TOKEN")
            try:
                await self._invoke_api(self.api.auth, refresh_token=self.refresh_token)
            except Exception:
                # Never expose upstream auth errors, which can contain secrets.
                raise PixivAuthError("Pixiv App authentication failed; update PIXIV_REFRESH_TOKEN") from None

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
        data = to_dict(result)
        illust = data.get("illust") if isinstance(data, dict) and "illust" in data else data
        snapshot = build_illust_snapshot(illust, pixiv_id)
        if snapshot.work_type == WorkType.MANGA:
            try:
                series = await self.web.get_illust_series(pixiv_id)
                if series:
                    snapshot.series_id = series["id"]
                    snapshot.series_order = series.get("order")
                    snapshot.metadata["series"] = series
            except Exception:
                # Series detection must not prevent an otherwise valid single-work archive.
                pass
        if snapshot.work_type == WorkType.UGOIRA:
            metadata = await self._call(self.api.ugoira_metadata, pixiv_id)
            if self.settings.ugoira_prefer_original:
                web_metadata = await self._try_web_ugoira_metadata(pixiv_id)
                if web_metadata is not None:
                    metadata = web_metadata
            snapshot = apply_ugoira_metadata(snapshot, metadata)
        return snapshot

    async def _try_web_ugoira_metadata(self, pixiv_id: int) -> dict | None:
        return await self.web.get_ugoira_metadata(pixiv_id)

    async def auth_status(self) -> dict:
        configured = bool(self.refresh_token)
        authenticated = False
        state = "not_configured"
        if configured:
            try:
                await self._ensure_auth(force=True)
                authenticated = True
                state = "ok"
            except PixivAuthError:
                state = "authentication_failed"
        web = await self.web.check_auth()
        return {
            "app_api": {"configured": configured, "authenticated": authenticated, "status": state},
            "web_cookie": {"configured": web.configured, "authenticated": web.authenticated, "status": web.status},
        }

    async def recover_auth(self) -> dict:
        """Revalidate manually configured credentials, never attempt account login."""
        self._next_auth_at = 0.0
        return await self.auth_status()

    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        # gather() is kept so callers do not depend on ordering, while _api_lock
        # guarantees the shared PixivPy instance itself is never used concurrently.
        detail_result, text_result = await asyncio.gather(
            self._call(self.api.novel_detail, pixiv_id),
            self._call(self.api.novel_text, pixiv_id),
        )
        detail = to_dict(detail_result)
        novel = detail.get("novel") if isinstance(detail, dict) and "novel" in detail else detail
        return build_novel_snapshot(novel, text_result, pixiv_id)

    async def detect_series(self, kind: WorkType, pixiv_id: int) -> tuple[str, int] | None:
        """按作品 ID 检测所属系列，无法检测时返回 None。"""
        if kind == WorkType.NOVEL:
            payload = to_dict(await self._call(self.api.novel_detail, pixiv_id))
            detail = payload.get("novel", payload) if isinstance(payload, dict) else {}
            series = detail.get("series") or {}
            sid = series.get("id") if isinstance(series, dict) else None
            return ("novel", int(sid)) if sid else None
        if kind in (WorkType.MANGA, WorkType.ILLUST):
            info = await self.web.get_illust_series(pixiv_id)
            if info:
                return ("manga", int(info["id"]))
        return None

    async def discover_novel_series(self, series_id: int) -> dict:
        """遍历 PixivPy novel_series 的 last_order 游标，禁止静默截断。"""
        from app.core.config import get_settings
        settings = get_settings()
        seen_ids: set[int] = set()
        seen_cursor: set[str] = set()
        works: list[dict] = []
        detail: dict = {}
        cursor: str | None = None
        for page_no in range(settings.series_max_pages):
            response = await self._call(self.api.novel_series, series_id, last_order=cursor)
            data = to_dict(response)
            if not isinstance(data, dict):
                raise ValueError("Invalid novel series payload")
            if not detail:
                detail = data.get("novel_series_detail") or {}
                if not isinstance(detail, dict):
                    detail = {}
            batch = data.get("novels") or []
            if not isinstance(batch, list):
                raise ValueError("Invalid novel series works list")
            for value in batch:
                item = to_dict(value)
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                wid = int(item["id"])
                if wid not in seen_ids:
                    seen_ids.add(wid)
                    info = item.get("series") or {}
                    works.append({"id": wid, "kind": "novel", "order": (
                        info.get("order") if isinstance(info, dict) else None
                    ) or len(works) + 1})
                    if len(works) > settings.series_max_works:
                        raise ValueError("Novel series exceeds SERIES_MAX_WORKS")
            next_url = data.get("next_url")
            if not next_url:
                break
            params = await self._invoke_api(self.api.parse_qs, next_url)
            next_cursor = str((params or {}).get("last_order") or "")
            if not next_cursor or next_cursor in seen_cursor:
                raise ValueError("Novel series pagination cursor did not advance")
            seen_cursor.add(next_cursor)
            cursor = next_cursor
        else:
            raise ValueError("Novel series exceeds SERIES_MAX_PAGES")
        user = to_dict(detail.get("user") or {})
        if not isinstance(user, dict):
            user = {}
        return {
            "series_id": series_id, "series_type": "novel",
            "title": detail.get("title") or f"Novel series {series_id}",
            "caption": detail.get("caption"),
            "author_id": str(user.get("id") or detail.get("user_id") or "0"),
            "author_name": user.get("name") or detail.get("user_name") or "",
            "published_total": detail.get("content_count") or detail.get("total") or len(works),
            "is_completed": bool(detail.get("is_completed", False)),
            "works": works,
        }

    async def discover_manga_series(self, series_id: int) -> dict:
        """遍历漫画 Web AJAX；页数据不可信时不返回不完整结果。"""
        settings = get_settings()
        seen: set[int] = set()
        works: list[dict] = []
        meta: dict = {}
        for page_no in range(1, settings.series_max_pages + 1):
            body = await self.web.get_manga_series_page(series_id, page_no)
            if page_no == 1:
                meta = body.get("series") or body.get("seriesInfo") or {}
                if not isinstance(meta, dict):
                    meta = {}
            thumbnails = body.get("thumbnails") or {}
            batch = thumbnails.get("illust") if isinstance(thumbnails, dict) else None
            if batch is None:
                batch = body.get("illusts") or body.get("works")
            if not isinstance(batch, (list, dict)):
                raise ValueError("Unsupported manga series response structure")
            if isinstance(batch, dict):
                batch = list(batch.values())
            new_count = 0
            for value in batch:
                item = to_dict(value)
                if not isinstance(item, dict) or not (item.get("id") or item.get("illustId")):
                    continue
                wid = int(item.get("id") or item["illustId"])
                if wid in seen:
                    continue
                seen.add(wid)
                new_count += 1
                works.append({"id": wid, "kind": "manga", "order": len(works) + 1})
                if len(works) > settings.series_max_works:
                    raise ValueError("Manga series exceeds SERIES_MAX_WORKS")
            page = body.get("page") or {}
            if not new_count or (isinstance(page, dict) and page.get("isLastPage") is True):
                break
        else:
            raise ValueError("Manga series exceeds SERIES_MAX_PAGES")
        return {
            "series_id": series_id, "series_type": "manga",
            "title": meta.get("title") or body.get("title") or f"Manga series {series_id}",
            "caption": meta.get("caption"),
            "author_id": str(meta.get("userId") or meta.get("user_id") or "0"),
            "author_name": meta.get("userName") or meta.get("user_name") or "",
            "published_total": meta.get("total") or len(works),
            "is_completed": bool(meta.get("isCompleted", False)),
            "works": works,
        }

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
