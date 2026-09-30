from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gppt
import pyotp
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.core.config import get_settings


class AutoAuthError(RuntimeError):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


@dataclass(slots=True)
class CredentialState:
    auto_enabled: bool
    username_configured: bool
    password_configured: bool
    totp_configured: bool
    refresh_token_available: bool
    cookie_available: bool


class PixivCredentialManager:
    """Owns Pixiv credential recovery and private on-disk cache.

    Password/TOTP are read from environment-backed Settings only. Cache files
    contain only derived credentials and are written mode 0600.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.pixiv_auth_cache_dir
        self.token_file = self.root / "app_refresh_token.json"
        self.cookie_file = self.root / "web_cookie.json"
        self._app_lock = asyncio.Lock()
        self._web_lock = asyncio.Lock()
        self._last_app_login_failure = 0.0
        self._last_web_login_failure = 0.0

    def state(self) -> CredentialState:
        return CredentialState(
            auto_enabled=self.settings.pixiv_auto_auth,
            username_configured=bool(self.settings.pixiv_username),
            password_configured=bool(self.settings.pixiv_password),
            totp_configured=bool(self.settings.pixiv_totp_secret),
            refresh_token_available=bool(self.current_refresh_token()),
            cookie_available=bool(self.current_cookie()),
        )

    def current_refresh_token(self) -> str:
        explicit = self.settings.pixiv_refresh_token.strip()
        if explicit:
            return explicit
        payload = self._read_private_json(self.token_file)
        return str(payload.get("refresh_token") or "").strip()

    def current_cookie(self) -> str:
        explicit = self.settings.pixiv_cookie.strip()
        if explicit:
            return explicit
        payload = self._read_private_json(self.cookie_file)
        return str(payload.get("cookie") or "").strip()

    async def ensure_refresh_token(self) -> str:
        token = self.current_refresh_token()
        if token:
            return token
        if not self.settings.pixiv_auto_auth:
            raise AutoAuthError("refresh_token_not_configured")
        return await self.recover_refresh_token(force_login=True)

    async def recover_refresh_token(self, *, force_login: bool = False) -> str:
        if not self.settings.pixiv_auto_auth:
            token = self.current_refresh_token()
            if token:
                return token
            raise AutoAuthError("auto_auth_disabled")

        async with self._app_lock:
            candidate = self.current_refresh_token()
            if candidate and not force_login:
                try:
                    token = await asyncio.to_thread(gppt.refresh, candidate)
                    refresh = str(token.refresh_token or "").strip()
                    if refresh:
                        self._save_refresh_token(refresh)
                        return refresh
                except Exception:
                    pass

            self._check_login_cooldown(self._last_app_login_failure)
            username, password = self._account_credentials()
            try:
                token = await asyncio.to_thread(
                    gppt.login,
                    username,
                    password,
                    self.settings.pixiv_totp_secret,
                    headless=True,
                )
                refresh = str(token.refresh_token or "").strip()
                if not refresh:
                    raise AutoAuthError("gppt_returned_no_refresh_token")
                self._save_refresh_token(refresh)
                self._last_app_login_failure = 0.0
                return refresh
            except AutoAuthError:
                self._last_app_login_failure = time.monotonic()
                raise
            except Exception as exc:
                self._last_app_login_failure = time.monotonic()
                name = exc.__class__.__name__.lower()
                if "captcha" in name or "challenge" in name:
                    raise AutoAuthError("interactive_required") from None
                raise AutoAuthError("gppt_login_failed") from None

    async def recover_cookie(self) -> str:
        if not self.settings.pixiv_auto_auth:
            cookie = self.current_cookie()
            if cookie:
                return cookie
            raise AutoAuthError("auto_auth_disabled")

        async with self._web_lock:
            self._check_login_cooldown(self._last_web_login_failure)
            username, password = self._account_credentials()
            try:
                cookie = await self._browser_login_cookie(
                    username, password, self.settings.pixiv_totp_secret
                )
                if not cookie:
                    raise AutoAuthError("browser_returned_no_cookie")
                self._save_cookie(cookie)
                self._last_web_login_failure = 0.0
                return cookie
            except AutoAuthError:
                self._last_web_login_failure = time.monotonic()
                raise
            except Exception:
                self._last_web_login_failure = time.monotonic()
                raise AutoAuthError("browser_login_failed") from None

    async def _browser_login_cookie(
        self, username: str, password: str, totp_secret: str
    ) -> str:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    user_agent=self.settings.pixiv_web_user_agent,
                    locale=self.settings.pixiv_web_locale,
                )
                page = await context.new_page()
                await page.goto(
                    "https://accounts.pixiv.net/login?lang=en",
                    wait_until="domcontentloaded",
                    timeout=self.settings.pixiv_browser_timeout_seconds * 1000,
                )

                if await self._challenge_detected(page):
                    raise AutoAuthError("interactive_required")

                username_input = page.locator('input[autocomplete="username"]').first
                password_input = page.locator('input[type="password"]').first
                submit = page.locator("form:has(fieldset) button[type='submit']").first
                if await username_input.count() == 0 or await password_input.count() == 0:
                    raise AutoAuthError("login_form_changed")

                await username_input.fill(username)
                await password_input.fill(password)
                if await submit.count() == 0:
                    submit = page.locator('button[type="submit"]').first
                if await submit.count() == 0:
                    raise AutoAuthError("login_form_changed")
                await submit.click()

                try:
                    otp = page.locator('input[autocomplete="one-time-code"]').first
                    await otp.wait_for(
                        state="visible",
                        timeout=min(7000, self.settings.pixiv_browser_timeout_seconds * 1000),
                    )
                    if not totp_secret:
                        raise AutoAuthError("totp_required")
                    await otp.fill(self._totp_code(totp_secret))
                    submit = page.locator("form:has(fieldset) button[type='submit']").first
                    if await submit.count() == 0:
                        submit = page.locator('button[type="submit"]').first
                    await submit.click()
                except PlaywrightTimeoutError:
                    pass

                if await self._challenge_detected(page):
                    raise AutoAuthError("interactive_required")

                # Authentication cookies can be set before the final redirect.
                # Visiting pixiv.net also finishes ordinary login redirects.
                try:
                    await page.goto(
                        "https://www.pixiv.net/",
                        wait_until="domcontentloaded",
                        timeout=self.settings.pixiv_browser_timeout_seconds * 1000,
                    )
                except PlaywrightTimeoutError:
                    pass

                cookies = await context.cookies()
                pixiv_cookies = [
                    item for item in cookies
                    if str(item.get("domain") or "").lstrip(".").endswith("pixiv.net")
                ]
                if not any(item.get("name") == "PHPSESSID" for item in pixiv_cookies):
                    if await self._challenge_detected(page):
                        raise AutoAuthError("interactive_required")
                    raise AutoAuthError("login_failed")

                # Cookie header format only; never persist browser localStorage.
                return "; ".join(
                    f"{item['name']}={item['value']}"
                    for item in sorted(pixiv_cookies, key=lambda x: x["name"])
                    if item.get("name") and item.get("value")
                )
            finally:
                await browser.close()

    async def _challenge_detected(self, page: Any) -> bool:
        try:
            content = (await page.content()).lower()
        except Exception:
            content = ""
        markers = ("recaptcha", "hcaptcha", "captcha", "cloudflare challenge")
        if any(marker in content for marker in markers):
            return True
        try:
            frames = [frame.url.lower() for frame in page.frames]
            return any("captcha" in url or "challenge" in url for url in frames)
        except Exception:
            return False

    @staticmethod
    def _totp_code(secret: str) -> str:
        value = secret.strip()
        if value.startswith("otpauth://"):
            return pyotp.parse_uri(value).now()
        return pyotp.TOTP(value).now()

    def _account_credentials(self) -> tuple[str, str]:
        username = self.settings.pixiv_username.strip()
        password = self.settings.pixiv_password
        if not username or not password:
            raise AutoAuthError("account_credentials_not_configured")
        return username, password

    def _check_login_cooldown(self, failed_at: float) -> None:
        if not failed_at:
            return
        elapsed = time.monotonic() - failed_at
        if elapsed < self.settings.pixiv_auto_login_cooldown_seconds:
            raise AutoAuthError("login_cooldown")

    def _save_refresh_token(self, refresh_token: str) -> None:
        self._write_private_json(
            self.token_file,
            {"refresh_token": refresh_token},
        )

    def _save_cookie(self, cookie: str) -> None:
        self._write_private_json(
            self.cookie_file,
            {"cookie": cookie},
        )

    def clear_derived_credentials(self) -> None:
        for path in (self.token_file, self.cookie_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _read_private_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write_private_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
