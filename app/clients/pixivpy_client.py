from __future__ import annotations

import asyncio
import re
import time
from typing import Any, NoReturn

from pixivpy3 import AppPixivAPI
from requests.exceptions import RequestException

from app.clients.base import PixivClient
from app.clients.pixiv_mapper import (
    apply_ugoira_metadata,
    build_illust_snapshot,
    build_novel_snapshot,
    to_dict,
)
from app.clients.pixiv_web_client import PixivWebClient
from app.core.config import get_settings
from app.core.pixiv_rate_limit import PixivRateLimiter, rate_limit_payload, response_is_rate_limit
from app.core.errors import (
    PixivAuthError,
    PixivNotFoundError,
    PixivRemoteError,
    PixivRestrictedError,
    PixivUnavailableError,
)
from app.models.work import WorkType
from app.schemas.pixiv import DiscoveredWork, DiscoveryPage, RemoteSnapshot


class _PixivHTTPError(Exception):
    def __init__(self, response):
        self.response = response
        super().__init__(f"Pixiv HTTP {response.status_code}")


class _AccountMismatchError(PixivAuthError):
    """A target-account guard must never trigger automatic credential recovery."""


class _StatusAwareAppAPI(AppPixivAPI):
    """Keep response status/headers before PixivPy discards them in JSON parsing."""
    def requests_call(self, *args, **kwargs):
        response = super().requests_call(*args, **kwargs)
        if response.status_code >= 400:
            raise _PixivHTTPError(response)
        return response

    def novel_bookmark_add(self, novel_id: int, restrict: str = 'private'):
        # PixivPy 3.7.5 has only the illustration helper. The corresponding App
        # endpoint/form is also implemented by tsubasa/pixiv.py (api.py).
        response = self.no_auth_requests_call(
            'POST', f'{self.hosts}/v2/novel/bookmark/add',
            data={'novel_id': novel_id, 'restrict': restrict}, req_auth=True,
        )
        return self.parse_result(response)


def _exception_response(exc: Exception):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        response = getattr(exc, 'response', None)
        if response is not None and isinstance(getattr(response, 'status_code', None), int):
            return response
        exc = exc.__cause__ or exc.__context__
    return None


def _transport_failure(exc: Exception) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (RequestException, TimeoutError, ConnectionError)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class PixivPyClient(PixivClient):
    """Serialized wrapper around one PixivPy AppPixivAPI instance.

    PixivPy does not document AppPixivAPI as thread-safe. All calls touching the
    shared SDK instance, including auth(), therefore pass through _api_lock.
    """

    def __init__(self, *, rate_limiter: PixivRateLimiter | None = None) -> None:
        self.settings = get_settings()
        self.api = _StatusAwareAppAPI(timeout=self.settings.download_timeout_seconds)
        self.refresh_token = self.settings.pixiv_refresh_token.strip()
        self._injected_rate_limiter = rate_limiter
        self.rate_limiter = rate_limiter or PixivRateLimiter(credential=self.refresh_token)
        self.web = PixivWebClient(cookie=self.settings.pixiv_cookie, rate_limiter=self.rate_limiter)
        self._auth_lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()
        self._credentials_lock = asyncio.Lock()
        self._next_auth_at = 0.0

    async def _invoke_api(self, func, *args, _expected_user_id=None, **kwargs):
        async with self._api_lock:
            await self.rate_limiter.wait()
            if _expected_user_id is not None:
                current = getattr(self.api, 'user_id', None)
                if str(current) != str(_expected_user_id):
                    raise _AccountMismatchError('Pixiv account changed; restart this account operation')
            task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # to_thread keeps running after cancellation; retain SDK ownership
                # until it finishes so another request cannot use the same session.
                try:
                    await task
                except Exception:
                    pass
                raise
            except Exception as exc:
                response = _exception_response(exc)
                if response is not None and response_is_rate_limit(response):
                    await self.rate_limiter.penalize(response.headers.get('Retry-After'))
                    raise PixivUnavailableError('Pixiv rate limit reached') from None
                raise
            if rate_limit_payload(to_dict(result)) or self._payload_status(result) == 429 or self._message_is_rate_limit(self._error_text(result)):
                await self.rate_limiter.penalize()
            return result

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
            except Exception as exc:
                response = _exception_response(exc)
                if isinstance(exc, PixivUnavailableError) or _transport_failure(exc) or (response is not None and
                        (response.status_code == 429 or response.status_code >= 500)):
                    raise PixivUnavailableError("Pixiv authentication temporarily unavailable") from None
                # Never expose upstream auth errors, which can contain secrets.
                raise PixivAuthError("Pixiv App authentication failed; update PIXIV_REFRESH_TOKEN") from None

            self._next_auth_at = time.monotonic() + 45 * 60

    async def _call(self, func, *args, _expected_user_id=None, **kwargs):
        await self._ensure_auth()
        try:
            result = await self._invoke_api(func, *args, _expected_user_id=_expected_user_id, **kwargs)
            self._raise_for_pixiv_error(result)
            return result
        except PixivAuthError as exc:
            if isinstance(exc, _AccountMismatchError):
                raise
            self._next_auth_at = 0.0
            await self._ensure_auth(force=True)
            try:
                result = await self._invoke_api(func, *args, _expected_user_id=_expected_user_id, **kwargs)
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
                    result = await self._invoke_api(func, *args, _expected_user_id=_expected_user_id, **kwargs)
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
        if rate_limit_payload(to_dict(result)):
            raise PixivUnavailableError('Pixiv rate limit reached')
        code = self._payload_status(result)
        if code is not None:
            if code in {408, 425, 429} or code >= 500:
                raise PixivUnavailableError('Pixiv is temporarily unavailable')
            if code in {401, 403, 404}:
                self._raise_mapped_message(str(code))
            raise PixivRemoteError(f'Pixiv returned HTTP {code}')
        message = self._error_text(result)
        if message:
            self._raise_mapped_message(message)
        data = to_dict(result)
        if isinstance(data, dict) and data.get('error'):
            raise PixivRemoteError('Pixiv returned an unspecified error')

    @staticmethod
    def _payload_status(result: Any) -> int | None:
        data = to_dict(result)
        error = data.get('error') if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return None
        for field in ('status_code', 'http_status', 'status', 'code'):
            value = error.get(field)
            if str(value).isdigit() and 400 <= int(value) <= 599:
                return int(value)
        return None

    @staticmethod
    def _exception_is_auth(exc: Exception) -> bool:
        response = _exception_response(exc)
        return isinstance(exc, PixivAuthError) or (
            response is not None and response.status_code == 401
        )

    def _raise_mapped_exception(self, exc: Exception) -> NoReturn:
        response = _exception_response(exc)
        if response is not None:
            if response_is_rate_limit(response):
                raise PixivUnavailableError('Pixiv rate limit reached') from exc
            code = response.status_code
            if code == 401:
                raise PixivAuthError("Pixiv authentication failed") from exc
            if code == 403:
                raise PixivRestrictedError("Pixiv work is restricted") from exc
            if code == 404:
                raise PixivNotFoundError("Pixiv work not found") from exc
            if code in {408, 425, 429} or code >= 500:
                raise PixivUnavailableError("Pixiv is temporarily unavailable") from exc
            raise PixivRemoteError(f"Pixiv returned HTTP {code}") from exc
        body = getattr(exc, 'body', None)
        if isinstance(body, dict) and self._error_text(body):
            self._raise_mapped_message(self._error_text(body), cause=exc)
        if _transport_failure(exc):
            raise PixivUnavailableError("Pixiv network request failed") from exc
        # Exception text often contains a URL and work ID: never interpret an ID
        # containing 401/403/404 as an actual response status.
        raise PixivRemoteError("Pixiv request failed") from exc

    @staticmethod
    def _message_is_rate_limit(message: str) -> bool:
        text = message.lower()
        return bool(re.match(r'^\s*(?:http(?:/\d(?:\.\d)?)?\s+)?429\b', text)) or any(
            marker in text for marker in ('rate limit', 'rate_limit', 'too many requests')
        )

    @staticmethod
    def _raise_mapped_message(
        message: str, cause: Exception | None = None
    ) -> NoReturn:
        text = message.lower()
        status = re.match(r'^\s*(?:http(?:/\d(?:\.\d)?)?\s+)?(401|403|404|429|503)\b', text)
        code = int(status.group(1)) if status else None
        if PixivPyClient._message_is_rate_limit(message):
            raise PixivUnavailableError("Pixiv rate limit reached") from cause
        if any(
            marker in text
            for marker in (
                "oauth",
                "invalid_grant",
                "access token",
                "refresh token",
                "unauthorized",
                "authentication required",
            )
        ) or code == 401:
            raise PixivAuthError("Pixiv authentication failed") from cause
        if any(
            marker in text
            for marker in (
                "not found",
                "does not exist",
                "deleted",
                "work not found",
            )
        ) or code == 404:
            raise PixivNotFoundError("Pixiv work not found") from cause
        if any(
            marker in text
            for marker in (
                "forbidden",
                "restricted",
                "private",
                "not public",
                "閲覧できません",
            )
        ) or code == 403:
            raise PixivRestrictedError("Pixiv work is restricted") from cause
        if any(
            marker in text
            for marker in (
                "rate limit",
                "too many requests",
                "timeout",
                "timed out",
                "temporarily",
                "service unavailable",
            )
        ) or code == 503:
            raise PixivUnavailableError("Pixiv is temporarily unavailable") from cause
        raise PixivRemoteError("Pixiv returned an unknown error") from cause

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
            except PixivUnavailableError:
                state = "temporarily_unavailable"
        web = await self.web.check_auth()
        return {
            "app_api": {"configured": configured, "authenticated": authenticated, "status": state},
            "web_cookie": {"configured": web.configured, "authenticated": web.authenticated, "status": web.status},
        }

    async def recover_auth(self) -> dict:
        """Revalidate manually configured credentials, never attempt account login."""
        await self.reload_credentials()
        return await self.auth_status()

    async def reload_credentials(self) -> None:
        """Explicitly reload current settings when idle; never hot-switch a write.

        Settings may be refreshed by the application, or all processes can simply
        be restarted after editing .env. No request silently reloads credentials.
        """
        if self._credentials_lock.locked():
            raise PixivUnavailableError('Pixiv account operation is active; reload after it finishes')
        async with self._credentials_lock:
            if self._api_lock.locked() or self._auth_lock.locked():
                raise PixivUnavailableError('Pixiv client is busy; reload after requests finish')
            settings = get_settings()
            token = settings.pixiv_refresh_token.strip()
            limiter = self._injected_rate_limiter or PixivRateLimiter(credential=token)
            web = PixivWebClient(cookie=settings.pixiv_cookie, rate_limiter=limiter,
                                 transport=self.web._transport)
            api = _StatusAwareAppAPI(timeout=settings.download_timeout_seconds)
            self.api.requests.close()
            self.settings, self.api, self.web = settings, api, web
            self.refresh_token, self.rate_limiter = token, limiter
            self._next_auth_at = 0.0

    async def get_account_identity(self, *, force: bool = True) -> dict:
        async with self._credentials_lock:
            app_id = str(await self.get_authenticated_user_id())
            web_id = None
            if self.web.configured:
                web_id = (await self.web.get_session_identity(force=force))['user_id']
            return {'user_id': app_id, 'app_user_id': app_id,
                    'web_user_id': web_id,
                    'same_account': app_id == web_id if web_id is not None else None}

    async def get_authenticated_web_user_id(self, *, force: bool = False) -> int:
        async with self._credentials_lock:
            return int((await self.web.get_session_identity(force=force))['user_id'])

    async def ensure_series_followed(self, series_type: str, series_id: int, *,
                                     expected_user_id: str | int | None = None) -> bool:
        async with self._credentials_lock:
            app_id = str(await self.get_authenticated_user_id())
            if expected_user_id is not None and str(expected_user_id) != app_id:
                raise _AccountMismatchError('Pixiv account changed; restart this account operation')
            return await self.web.ensure_series_followed(series_type, series_id,
                                                         expected_user_id=app_id)

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

    async def ensure_bookmarked(
        self, pixiv_id: int, work_type: WorkType, *, restrict: str = 'private',
        snapshot: RemoteSnapshot | None = None,
        expected_user_id: str | int | None = None,
    ) -> bool:
        async with self._credentials_lock:
            return await self._ensure_bookmarked(pixiv_id, work_type, restrict=restrict,
                snapshot=snapshot, expected_user_id=expected_user_id)

    async def _ensure_bookmarked(
        self, pixiv_id: int, work_type: WorkType, *, restrict: str = 'private',
        snapshot: RemoteSnapshot | None = None,
        expected_user_id: str | int | None = None,
    ) -> bool:
        """Bookmark only an unbookmarked work; preserve existing visibility/tags.

        Returns True only if a bookmark was added. Checking novel metadata here
        never downloads its text, so callers can run this before archival.
        """
        if pixiv_id <= 0 or restrict not in {'public', 'private'}:
            raise ValueError('Invalid bookmark id/restrict')
        kind = WorkType(work_type)
        if snapshot is not None and snapshot.pixiv_id != pixiv_id:
            raise ValueError('Bookmark snapshot does not match requested work')
        expected = {}
        if expected_user_id is not None:
            if str(await self.get_authenticated_user_id()) != str(expected_user_id):
                raise _AccountMismatchError('Pixiv account changed; restart this account operation')
            expected['_expected_user_id'] = expected_user_id
        # A local snapshot could have been fetched under a different account.
        # Migration operations must query the destination's own bookmark state.
        detail = snapshot.metadata if snapshot is not None and expected_user_id is None else {}
        if not isinstance(detail.get('is_bookmarked'), bool):
            method = self.api.novel_detail if kind == WorkType.NOVEL else self.api.illust_detail
            data = to_dict(await self._call(method, pixiv_id, **expected))
            collection = 'novel' if kind == WorkType.NOVEL else 'illust'
            detail = data.get(collection, data) if isinstance(data, dict) else {}
        state = detail.get('is_bookmarked') if isinstance(detail, dict) else None
        if state is True:
            return False
        if state is not False:
            # Missing permission/bookmark metadata must not accidentally update
            # an existing bookmark's public/private setting.
            raise PixivRemoteError('Pixiv did not return a bookmark state')
        method = self.api.novel_bookmark_add if kind == WorkType.NOVEL else self.api.illust_bookmark_add
        await self._call(method, pixiv_id, restrict=restrict, **expected)
        if snapshot is not None:
            snapshot.metadata['is_bookmarked'] = True
        return True

    async def discover_followed_series(self, series_type: str = 'novel') -> list[dict]:
        """Enumerate all pages of the account's Web watchlist, fail on truncation."""
        if series_type not in {'novel', 'manga'}:
            raise ValueError('series_type must be novel/manga')
        found: dict[int, dict] = {}
        expected_total = None
        expected_pages = None
        for page_no in range(1, self.settings.series_max_pages + 1):
            body = await self.web.get_followed_series_page(series_type, page_no)
            page = body['page']
            try:
                total = int(page['total'])
                max_page = int(page['maxPage'])
            except (KeyError, TypeError, ValueError):
                raise PixivRemoteError('Pixiv watchlist pagination is missing') from None
            watched = page.get('watchedSeriesIds')
            if not isinstance(watched, list) or total < 0 or max_page < 0:
                raise PixivRemoteError('Invalid Pixiv watchlist pagination')
            if expected_total is None:
                expected_total, expected_pages = total, max_page
            elif (total, max_page) != (expected_total, expected_pages):
                raise PixivUnavailableError('Pixiv watchlist changed during pagination; retry')
            if max_page > self.settings.series_max_pages or total > self.settings.series_max_works:
                raise ValueError('Pixiv watchlist exceeds configured discovery limits')
            thumbnails = body.get('thumbnails') or {}
            batch = (thumbnails.get('novelSeries') if isinstance(thumbnails, dict) else None) if series_type == 'novel' else body.get('illustSeries')
            if total == 0:
                if watched or batch or max_page > 1:
                    raise PixivRemoteError('Inconsistent empty Pixiv watchlist')
                return []
            if not isinstance(batch, list) or not batch or max_page < 1:
                raise PixivRemoteError('Pixiv watchlist page is incomplete')
            for item in batch:
                if not isinstance(item, dict) or not str(item.get('id', '')).isdigit():
                    raise PixivRemoteError('Invalid Pixiv watchlist series')
                sid = int(item['id'])
                if sid <= 0 or sid in found:
                    raise PixivRemoteError('Pixiv watchlist pagination repeated a series')
                found[sid] = {
                    **item, 'series_id': sid, 'series_type': series_type,
                    'title': str(item.get('title') or ''),
                }
            if page_no >= max_page:
                if len(found) != total:
                    raise PixivRemoteError('Pixiv watchlist returned an incomplete collection')
                return list(found.values())
        raise ValueError('Pixiv watchlist exceeds SERIES_MAX_PAGES')

    async def discover_followed_novel_series(self) -> list[dict]:
        return await self.discover_followed_series('novel')

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
            params = self.api.parse_qs(next_url)
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
            series = raw.get('series') or {}
            series_id = str(series['id']) if isinstance(series, dict) and series.get('id') else None
            series_type = ('novel' if work_type == WorkType.NOVEL else 'manga') if series_id and work_type in {WorkType.NOVEL, WorkType.MANGA} else None
            items.append(
                DiscoveredWork(
                    pixiv_id=int(raw["id"]),
                    work_type=work_type,
                    series_id=series_id if series_type else None,
                    series_type=series_type,
                )
            )
        next_url = data.get("next_url")
        next_params = None
        if next_url:
            next_params = self.api.parse_qs(next_url)
        return DiscoveryPage(items=items, next_params=next_params)
