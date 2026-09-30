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
