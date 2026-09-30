# Pixiv authentication

The backend intentionally uses two independent Pixiv authentication methods.

## App API: refresh token

`PIXIV_REFRESH_TOKEN` is used only by PixivPy/App API calls. The backend
exchanges it for the normal App API access token through PixivPy. Authentication
is cached locally for 45 minutes; a 401/token error forces one refresh and one
retry.

The refresh token is not stored in MySQL and is never returned by the API.

## Web AJAX: browser cookie

`PIXIV_COOKIE` is optional. Paste the raw Cookie header from a browser session
that is already logged into **your own** Pixiv account, for example:

```text
PHPSESSID=...; device_token=...
```

The backend forwards the cookie only to `www.pixiv.net` Web AJAX GET requests.
Current Web usage is read-only, so no CSRF token is needed. If Web POST support
is added later it must implement Pixiv's CSRF flow separately.

The Cookie is useful for login-restricted Web metadata and for trying to obtain
an Ugoira `originalSrc`. If the cookie is absent or expired, public Ugoira
metadata is retried anonymously, then the App API remains the final fallback.

## Status endpoint

All admin routes are protected by the backend's own `X-API-Key`.

```http
GET /api/admin/pixiv/auth/status
X-API-Key: <your backend api key>
```

Response contains only:
- whether each credential is configured,
- whether a live probe succeeded,
- a small status code such as `ok`, `not_configured`, `http_403`.

It never returns the cookie, PHPSESSID, refresh token or App access token.

Web-cookie validity is probed using Pixiv's login-required
`/ajax/user/extra` endpoint.


## Automatic mode

Set `PIXIV_AUTO_AUTH=true` together with `PIXIV_USERNAME` and
`PIXIV_PASSWORD`. If the account uses TOTP two-factor authentication, also
set `PIXIV_TOTP_SECRET` to the base32 secret or the original `otpauth://`
URI.

App API credential recovery uses gppt 5.x: a cached refresh token is tried
first, then `gppt.refresh()`, then a fresh headless `gppt.login()` only when
necessary. gppt 5.1.0 currently documents Python-level `login`, `refresh`
and TOTP support for unattended authentication.

Web Cookie recovery uses a separate Playwright Chromium session. The browser
fills Pixiv's username/password form, handles a standard visible TOTP field
when a TOTP secret is configured, then exports only Pixiv-domain cookies into
the private cache. Browser localStorage is not persisted.

Derived credentials live under `PIXIV_AUTH_CACHE_DIR` (default
`/data/pixiv/auth`). Username, password and TOTP secret stay in the process
configuration and are never written into those cache files.

### Human verification

CAPTCHA, challenge pages, unsupported passkey-only flows, or a changed login
form are not bypassed. Automatic login stops with a status such as
`interactive_required`, `totp_required`, or `login_form_changed`.
Failed browser/App logins are subject to
`PIXIV_AUTO_LOGIN_COOLDOWN_SECONDS` (default 900 seconds) to avoid tight retry
loops and account lockouts.

### Manual mode priority

If `PIXIV_REFRESH_TOKEN` or `PIXIV_COOKIE` is explicitly set, that value is
preferred over the derived cache. Automatic recovery is used only when
`PIXIV_AUTO_AUTH=true`.

No database schema change is required; authentication data is intentionally
kept out of MySQL.
