from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings


@dataclass(slots=True)
class AuthProbe:
    configured: bool
    authenticated: bool
    status: str


class PixivWebClient:
    """Minimal Pixiv Web AJAX client using a browser session cookie."""

    def __init__(
        self,
        cookie: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = get_settings()
        self.cookie = ""
        self.set_cookie(cookie if cookie is not None else self.settings.pixiv_cookie)
        self._transport = transport

    def set_cookie(self, cookie: str | None) -> None:
        value = (cookie or "").strip()
        if "\r" in value or "\n" in value:
            raise ValueError("PIXIV_COOKIE contains invalid newline characters")
        self.cookie = value

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
        async with httpx.AsyncClient(
            base_url="https://www.pixiv.net",
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(self.settings.download_timeout_seconds),
            transport=self._transport,
        ) as client:
            response = await client.get(path)
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
        except (httpx.HTTPError, ValueError):
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


    async def get_watched_novel_series_page(self, page: int) -> dict[str, Any]:
        """读取当前账号追更的小说合集。Pixiv Web AJAX 非公开接口；未知结构明确失败。

        不把小说作品 ID 当合集 ID，也不把 Cookie 失效时的空响应当空列表。
        """
        from app.core.errors import PixivAuthError, PixivUnavailableError

        if not self.configured:
            raise PixivAuthError("PIXIV_COOKIE is required to sync the novel watchlist")
        if page < 1:
            raise ValueError("Watchlist page must be positive")
        try:
            payload = await self._get_json(
                f"/ajax/watch_list/novel?p={page}&new=1&lang=zh",
                referer="https://www.pixiv.net/following/watchlist/novels",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise PixivAuthError("Pixiv Web cookie was rejected when reading the watchlist") from None
            raise PixivUnavailableError("Pixiv watchlist API returned an HTTP error") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PixivUnavailableError("Pixiv watchlist API is unreachable or returned invalid JSON") from exc

        if payload.get("error") is True:
            raise PixivAuthError("Pixiv rejected the novel watchlist request; verify PIXIV_COOKIE")
        body = payload.get("body")
        if not isinstance(body, dict):
            raise ValueError("Unknown novel watchlist response: missing body")

        # 数据格式可能随 Pixiv Web 前端调整；只认可明确标记为系列的数组。
        containers = [body]
        for key in ("page", "data"):
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
        if entries is None:
            raise ValueError("Unknown novel watchlist response: no series list")

        result: list[int] = []
        seen: set[int] = set()
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Unknown novel watchlist entry")
            nested = item.get("series")
            sid = item.get("seriesId") or item.get("novelSeriesId") or item.get("series_id")
            if not sid and isinstance(nested, dict):
                sid = nested.get("id") or nested.get("seriesId")
            if not sid and "id" in item:
                sid = item["id"]  # 仅当所在列表明确是系列列表时接受
            if isinstance(sid, bool) or not str(sid or "").isdigit() or int(sid) <= 0:
                raise ValueError("Unknown novel watchlist entry: missing series ID")
            sid = int(sid)
            if sid not in seen:
                seen.add(sid)
                result.append(sid)

        has_next = None
        for container in containers:
            for key in ("hasNextPage", "hasNext", "has_next"):
                if isinstance(container.get(key), bool):
                    has_next = container[key]
                    break
            if has_next is not None:
                break
            last_page = container.get("lastPage") or container.get("last_page")
            if isinstance(last_page, int) and not isinstance(last_page, bool):
                has_next = page < last_page
                break
        return {"series_ids": result, "has_next": has_next}
