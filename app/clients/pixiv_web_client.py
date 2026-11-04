from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import time
from html.parser import HTMLParser
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import (PixivAuthError, PixivNotFoundError, PixivRemoteError,
                             PixivRestrictedError, PixivUnavailableError)
from app.core.pixiv_rate_limit import PixivRateLimiter, response_is_rate_limit


@dataclass(slots=True)
class AuthProbe:
    configured: bool
    authenticated: bool
    status: str


class _IdentityHTMLParser(HTMLParser):
    """Extract only documented self-account bootstrap containers, not page authors."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources: list[dict] = []
        self._next = False
        self._script: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'meta' and (values.get('id') in {'meta-global-data', 'init-config'} or
                              values.get('name') in {'global-data', 'init-config'}):
            self._add(values.get('content') or '')
        if tag == 'script' and values.get('id') == '__NEXT_DATA__':
            self._next, self._script = True, []

    def handle_data(self, data):
        if self._next:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._next:
            self._next = False
            try:
                data = json.loads(''.join(self._script))
                state = data['props']['pageProps']['serverSerializedPreloadedState']
                self._add(state)
            except (KeyError, TypeError, ValueError):
                pass

    def _add(self, value):
        try:
            data = json.loads(value) if isinstance(value, str) else value
            if isinstance(data, dict):
                self.sources.append(data)
        except (TypeError, ValueError):
            pass


def _web_identity_from_html(html: str) -> tuple[str, str]:
    """Pixiv legacy/Vue/Next bootstrap formats, verified in Bobby's Pixiv Utils.

    Both identity and CSRF must come from authenticated server HTML. Never infer
    the logged-in account from a PHPSESSID prefix or a viewed author's profile.
    """
    parser = _IdentityHTMLParser()
    parser.feed(html)
    identities: set[str] = set()
    tokens: list[str] = []
    for data in parser.sources:
        api = data.get('api') if isinstance(data.get('api'), dict) else {}
        user = data.get('userData') if isinstance(data.get('userData'), dict) else {}
        api_user = api.get('userData') if isinstance(api.get('userData'), dict) else {}
        self_user = user.get('self') if isinstance(user.get('self'), dict) else {}
        api_self = api_user.get('self') if isinstance(api_user.get('self'), dict) else {}
        candidates = [data.get('pixiv.user.id'), self_user.get('id'), self_user.get('userId'),
                      api_self.get('id'), api_self.get('userId'), user.get('id'), user.get('userId')]
        identities.update(str(value) for value in candidates
                          if str(value).isascii() and str(value).isdigit() and int(value) > 0)
        token = data.get('pixiv.context.postKey') or api.get('token') or data.get('token')
        if isinstance(token, str) and token and '\r' not in token and '\n' not in token:
            tokens.append(token)
    if len(identities) != 1 or not tokens:
        raise PixivAuthError('Cannot verify Pixiv Web account and CSRF token; update PIXIV_COOKIE')
    return identities.pop(), tokens[0]


class PixivWebClient:
    """Minimal Pixiv Web AJAX client using a browser session cookie."""

    def __init__(
        self,
        cookie: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: PixivRateLimiter | None = None,
    ) -> None:
        self.settings = get_settings()
        self._session_lock = asyncio.Lock()
        self._cookie_generation = 0
        self._session_user_id: str | None = None
        self._csrf_token: str | None = None
        self._session_checked_at = 0.0
        self.cookie = ""
        self.set_cookie(cookie if cookie is not None else self.settings.pixiv_cookie)
        self._transport = transport
        self.rate_limiter = rate_limiter or PixivRateLimiter()

    def set_cookie(self, cookie: str | None) -> None:
        value = (cookie or "").strip()
        if "\r" in value or "\n" in value:
            raise ValueError("PIXIV_COOKIE contains invalid newline characters")
        if value != self.cookie:
            self._cookie_generation += 1
            self._session_user_id = None
            self._csrf_token = None
            self._session_checked_at = 0.0
        self.cookie = value

    async def get_session_identity(self, *, force: bool = False) -> dict[str, str]:
        async with self._session_lock:
            user_id, _ = await self._session(force=force)
            return {'user_id': user_id}

    async def _authenticated_request(self, method: str, path: str, *,
                                     generation: int, csrf: str | None = None,
                                     payload: dict | None = None) -> httpx.Response:
        if generation != self._cookie_generation or not self.configured:
            raise PixivAuthError('Pixiv Web credentials changed; restart this account operation')
        headers = self._headers()
        if csrf is not None:
            headers['x-csrf-token'] = csrf
        if path in {'/settings/account', '/'}:
            headers['Accept'] = 'text/html,application/xhtml+xml'
        await self.rate_limiter.wait()
        if generation != self._cookie_generation:
            raise PixivAuthError('Pixiv Web credentials changed; restart this account operation')
        try:
            async with httpx.AsyncClient(base_url='https://www.pixiv.net', headers=headers,
                    follow_redirects=False, transport=self._transport,
                    timeout=httpx.Timeout(self.settings.download_timeout_seconds)) as client:
                response = await client.request(method, path, json=payload)
        except httpx.RequestError:
            raise PixivUnavailableError('Pixiv Web request failed') from None
        if response_is_rate_limit(response):
            await self.rate_limiter.penalize(response.headers.get('Retry-After'))
            raise PixivUnavailableError('Pixiv Web rate limit reached')
        if response.status_code in {408, 425} or response.status_code >= 500:
            raise PixivUnavailableError('Pixiv Web is temporarily unavailable')
        if response.status_code == 401 or 300 <= response.status_code < 400:
            self._session_checked_at = 0.0
            raise PixivAuthError('Pixiv Web session expired; update PIXIV_COOKIE')
        if generation != self._cookie_generation:
            raise PixivAuthError('Pixiv Web credentials changed during account operation')
        return response

    async def _session(self, *, force: bool = False) -> tuple[str, str]:
        if not self.configured:
            raise PixivAuthError('PIXIV_COOKIE is required for Pixiv Web account operations')
        if (not force and self._session_user_id and self._csrf_token and
                time.monotonic() - self._session_checked_at < 300):
            return self._session_user_id, self._csrf_token
        self._session_user_id = self._csrf_token = None
        self._session_checked_at = 0.0
        generation = self._cookie_generation
        # Settings uses Next.js; the root page also supports the legacy/mobile
        # bootstrap containers. A login redirect is always a hard auth failure.
        for path in ('/settings/account', '/'):
            response = await self._authenticated_request('GET', path, generation=generation)
            if response.status_code >= 400:
                raise PixivAuthError('Pixiv Web account verification failed; update PIXIV_COOKIE')
            try:
                user_id, token = _web_identity_from_html(response.text)
            except PixivAuthError:
                continue
            self._session_user_id, self._csrf_token = user_id, token
            self._session_checked_at = time.monotonic()
            return user_id, token
        raise PixivAuthError('Pixiv Web did not return a verified account and CSRF token')

    async def ensure_series_followed(self, series_type: str, series_id: int, *,
                                     expected_user_id: str | int) -> bool:
        """Follow only an unwatched series using a verified same-account session."""
        if series_type not in {'novel', 'manga'} or series_id <= 0:
            raise ValueError('Invalid series type/id')
        async with self._session_lock:
            user_id, csrf = await self._session()
            if user_id != str(expected_user_id):
                raise PixivAuthError('Pixiv App and Web account mismatch; update credentials')
            generation = self._cookie_generation
            detail_path = (f'/ajax/novel/series/{series_id}' if series_type == 'novel'
                           else f'/ajax/series/{series_id}?p=1')
            response = await self._authenticated_request('GET', detail_path, generation=generation)
            body = self._series_body(response)
            if series_type == 'novel':
                state = body.get('isWatched') if str(body.get('id')) == str(series_id) else None
            else:
                page = body.get('page') or {}
                state = page.get('isWatched') if isinstance(page, dict) and str(page.get('seriesId')) == str(series_id) else None
                if not isinstance(state, bool):
                    state = next((item.get('isWatched') for item in body.get('illustSeries', [])
                                  if isinstance(item, dict) and str(item.get('id')) == str(series_id)), None)
            if state is True:
                return False
            if state is not False:
                raise PixivRemoteError('Pixiv did not return a verified series watch state')
            endpoint_kind = 'novel' if series_type == 'novel' else 'illust'
            for attempt in range(2):
                if user_id != str(expected_user_id) or generation != self._cookie_generation:
                    raise PixivAuthError('Pixiv Web account changed before series write')
                response = await self._authenticated_request('POST',
                    f'/ajax/{endpoint_kind}/series/{series_id}/watch',
                    generation=generation, csrf=csrf, payload={})
                if response.status_code == 403 and attempt == 0:
                    user_id, csrf = await self._session(force=True)
                    continue
                self._series_body(response, writing=True)
                return True
        raise PixivAuthError('Pixiv Web series write failed')

    @staticmethod
    def _series_body(response: httpx.Response, *, writing: bool = False):
        if response.status_code == 403:
            error = PixivAuthError if writing else PixivRestrictedError
            raise error('Pixiv denied this series request')
        if response.status_code == 404:
            raise PixivNotFoundError('Pixiv series not found')
        if response.status_code >= 400:
            raise PixivRemoteError('Pixiv series request failed')
        try:
            data = response.json()
        except ValueError:
            raise PixivAuthError('Pixiv Web did not return an authenticated response') from None
        if not isinstance(data, dict) or data.get('error') is not False:
            raise PixivRemoteError('Pixiv rejected the series request')
        body = data.get('body')
        if not writing and not isinstance(body, dict):
            raise PixivRemoteError('Invalid Pixiv series response')
        return body

    @property
    def configured(self) -> bool:
        return bool(self.cookie)

    def _headers(self, referer: str = "https://www.pixiv.net/") -> dict[str, str]:
        headers = {
            "User-Agent": self.settings.pixiv_web_user_agent,
            "Referer": referer,
            "Origin": "https://www.pixiv.net",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.settings.pixiv_web_accept_language,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _get_json(
        self,
        path: str,
        *,
        referer: str = "https://www.pixiv.net/",
        include_cookie: bool = True,
    ) -> dict[str, Any]:
        headers = self._headers(referer)
        if not include_cookie:
            headers.pop("Cookie", None)
        await self.rate_limiter.wait()
        async with httpx.AsyncClient(
            base_url="https://www.pixiv.net",
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(self.settings.download_timeout_seconds),
            transport=self._transport,
        ) as client:
            response = await client.get(path)
            if response_is_rate_limit(response):
                await self.rate_limiter.penalize(response.headers.get("Retry-After"))
                raise PixivUnavailableError("Pixiv Web rate limit reached")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Pixiv Web API returned a non-object JSON response")
        return payload

    async def check_auth(self) -> AuthProbe:
        if not self.configured:
            return AuthProbe(False, False, "not_configured")
        try:
            payload = await self._get_json("/ajax/user/extra")
            if payload.get("error") is True:
                return AuthProbe(True, False, "pixiv_rejected")
            if payload.get("body") is None:
                return AuthProbe(True, False, "unexpected_response")
            return AuthProbe(True, True, "ok")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return AuthProbe(
                True, False,
                f"http_{code}" if code in (401, 403) else "http_error",
            )
        except (httpx.HTTPError, ValueError, PixivUnavailableError):
            return AuthProbe(True, False, "network_or_response_error")

    async def get_ugoira_metadata(self, pixiv_id: int) -> dict[str, Any] | None:
        referer = f"https://www.pixiv.net/artworks/{pixiv_id}"
        attempts = (True, False) if self.configured else (False,)
        for include_cookie in attempts:
            try:
                payload = await self._get_json(
                    f"/ajax/illust/{pixiv_id}/ugoira_meta",
                    referer=referer,
                    include_cookie=include_cookie,
                )
                if payload.get("error") is True:
                    continue
                body = payload.get("body")
                if not isinstance(body, dict) or not body.get("frames"):
                    continue
                original = body.get("originalSrc")
                medium = body.get("src")
                if not original and not medium:
                    continue
                zip_urls: dict[str, str] = {}
                if original:
                    zip_urls["original"] = str(original)
                if medium:
                    zip_urls["medium"] = str(medium)
                return {
                    "ugoira_metadata": {
                        "zip_urls": zip_urls,
                        "frames": body["frames"],
                    }
                }
            except (httpx.HTTPError, ValueError):
                continue
        return None

    async def get_illust_series(self, pixiv_id: int) -> dict[str, Any] | None:
        """从作品详情提取漫画系列信息；不能识别时返回 None。"""
        payload = await self._get_json(
            f"/ajax/illust/{pixiv_id}",
            referer=f"https://www.pixiv.net/artworks/{pixiv_id}",
        )
        if payload.get("error"):
            return None
        body = payload.get("body") or {}
        if not isinstance(body, dict) or int(body.get("illustType") or 0) != 1:
            return None
        nav = body.get("seriesNavData") or {}
        if not isinstance(nav, dict):
            nav = {}
        sid = nav.get("seriesId") or body.get("seriesId")
        if not sid:
            return None
        return {
            "id": str(sid),
            "title": nav.get("title") or body.get("seriesTitle") or "",
            "order": nav.get("order"),
            "author_id": str(body.get("userId") or "0"),
            "author_name": body.get("userName") or "",
        }

    async def get_manga_series_page(self, series_id: int, page: int) -> dict[str, Any]:
        """Pixiv Web 漫画系列分页，API 可能调整，未知结构必须显式报错。"""
        payload = await self._get_json(
            f"/ajax/series/{series_id}?p={page}",
            referer=f"https://www.pixiv.net/user/0/series/{series_id}",
        )
        if payload.get("error"):
            raise ValueError("Pixiv manga series AJAX returned an error")
        body = payload.get("body")
        if not isinstance(body, dict):
            raise ValueError("Pixiv manga series response body is missing")
        return body

    async def get_followed_series_page(self, series_type: str, page: int) -> dict[str, Any]:
        """Read the signed-in account's watchlist; never treat auth failure as empty.

        Shape verified against ProjectU FollowApi/WatchListDto (2026-11-04).
        This is an undocumented Pixiv Web endpoint, not the App follow feed.
        """
        body = await self._watchlist_body(series_type, page)
        if not isinstance(body.get("page"), dict):
            raise PixivRemoteError("Unsupported Pixiv watchlist response")
        return body

    async def _watchlist_body(self, series_type: str, page: int) -> dict[str, Any]:
        """Shared authenticated request for current and legacy watchlist callers."""
        if series_type not in {"novel", "manga"} or page < 1:
            raise ValueError("Invalid watchlist type/page")
        if not self.configured:
            raise PixivAuthError("PIXIV_COOKIE is required to read followed series")
        try:
            payload = await self._get_json(
                f"/ajax/watch_list/{series_type}?p={page}&new=1",
                referer=f"https://www.pixiv.net/following/watchlist/{'novels' if series_type == 'novel' else 'manga'}",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise PixivAuthError("Pixiv watchlist authentication failed; update PIXIV_COOKIE") from None
            raise PixivUnavailableError("Pixiv watchlist API returned an HTTP error") from None
        except httpx.RequestError:
            raise PixivUnavailableError("Pixiv watchlist API is unreachable") from None
        except ValueError:
            # Login redirects return HTML instead of a JSON body.
            raise PixivAuthError("Pixiv watchlist session is invalid; update PIXIV_COOKIE") from None
        if payload.get("error"):
            raise PixivAuthError("Pixiv rejected the watchlist request; check PIXIV_COOKIE")
        body = payload.get("body")
        if not isinstance(body, dict):
            raise PixivRemoteError("Unsupported Pixiv watchlist response")
        return body

    async def get_watched_novel_series_page(self, page: int) -> dict[str, Any]:
        """Compatibility API for the legacy novel-only watchlist subscription.

        Accept the documented pagination/thumbnails shape and explicit legacy
        series lists; never mistake a novel work thumbnail for a series ID.
        Both paths use the shared authenticated request and global rate limiter.
        """
        body = await self._watchlist_body("novel", page)
        containers = [body]
        for key in ("page", "data", "thumbnails"):
            if isinstance(body.get(key), dict):
                containers.append(body[key])
        entries = None
        for container in containers:
            for key in ("novelSeries", "novel_series", "series", "seriesList", "watchList", "watchlist"):
                if isinstance(container.get(key), list):
                    entries = container[key]
                    break
            if entries is not None:
                break
        info = body.get("page") or {}
        metadata = {}
        if isinstance(info, dict) and ("total" in info or "maxPage" in info):
            try:
                if isinstance(info.get("total"), bool) or isinstance(info.get("maxPage"), bool):
                    raise ValueError
                total, max_page = int(info["total"]), int(info["maxPage"])
            except (KeyError, TypeError, ValueError):
                raise PixivRemoteError("Pixiv watchlist pagination is missing") from None
            watched = info.get("watchedSeriesIds")
            if not isinstance(watched, list) or total < 0 or max_page < 0:
                raise PixivRemoteError("Invalid Pixiv watchlist pagination")
            if total == 0:
                if watched or entries or max_page > 1:
                    raise PixivRemoteError("Inconsistent empty Pixiv watchlist")
                entries = []
            elif max_page < page or not entries:
                raise PixivRemoteError("Pixiv watchlist page is incomplete")
            metadata = {"total": total, "max_page": max_page}
        if entries is None:
            raise ValueError("Unknown novel watchlist response: no series list")
        result, seen = [], set()
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Unknown novel watchlist entry")
            nested = item.get("series")
            sid = item.get("seriesId") or item.get("novelSeriesId") or item.get("series_id")
            if not sid and isinstance(nested, dict):
                sid = nested.get("id") or nested.get("seriesId")
            if not sid:
                sid = item.get("id")
            if isinstance(sid, bool) or not str(sid or "").isascii() or not str(sid or "").isdigit() or int(sid) <= 0:
                raise ValueError("Unknown novel watchlist entry: missing series ID")
            sid = int(sid)
            if sid in seen:
                raise PixivRemoteError("Pixiv watchlist repeated a series in one page")
            seen.add(sid)
            result.append(sid)
        has_next = None
        if metadata:
            has_next = page < metadata["max_page"]
        else:
            for container in containers:
                for key in ("hasNextPage", "hasNext", "has_next"):
                    if isinstance(container.get(key), bool):
                        has_next = container[key]
                        break
                if has_next is not None:
                    break
                last_page = container.get("lastPage") or container.get("last_page")
                if isinstance(last_page, int) and not isinstance(last_page, bool) and last_page >= page:
                    has_next = page < last_page
                    break
        return {"series_ids": result, "has_next": has_next, **metadata}
