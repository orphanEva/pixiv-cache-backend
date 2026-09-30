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
    """Minimal Pixiv Web AJAX client using a browser session cookie.

    The cookie is only kept in memory and is never returned by public methods.
    """

    def __init__(
        self,
        cookie: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = get_settings()
        self.cookie = (cookie if cookie is not None else self.settings.pixiv_cookie).strip()
        if "\r" in self.cookie or "\n" in self.cookie:
            raise ValueError("PIXIV_COOKIE contains invalid newline characters")
        self._transport = transport

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
            return AuthProbe(configured=False, authenticated=False, status="not_configured")
        try:
            payload = await self._get_json("/ajax/user/extra")
            if payload.get("error") is True:
                return AuthProbe(True, False, "pixiv_rejected")
            if payload.get("body") is None:
                return AuthProbe(True, False, "unexpected_response")
            return AuthProbe(True, True, "ok")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                return AuthProbe(True, False, f"http_{code}")
            return AuthProbe(True, False, "http_error")
        except (httpx.HTTPError, ValueError):
            return AuthProbe(True, False, "network_or_response_error")

    async def get_ugoira_metadata(self, pixiv_id: int) -> dict[str, Any] | None:
        """Best-effort Web metadata.

        If a configured cookie has expired, retry anonymously so public works
        can still use originalSrc when Pixiv exposes it.
        """
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
