# Real Pixiv integration checklist

Use only your own authorized Pixiv account and your own server. Never paste
`PIXIV_REFRESH_TOKEN`, `API_KEY`, a complete `.env`, or authorization
headers into chat or GitHub.

1. Complete [manual database initialization](MANUAL_DATABASE_SETUP.md)
   first. The app will not create MySQL databases or tables.
2. Clone this repository, copy `.env.example` to `.env`, and set
   `DATABASE_URL` for your external MySQL and your own `API_KEY`.
3. Set your Pixiv refresh token privately as `PIXIV_REFRESH_TOKEN`. Optionally set `PIXIV_COOKIE` to the raw Cookie header from your own logged-in browser session.
4. Run `docker compose config --quiet` and
   `docker compose up -d --build`.
5. `curl -sS http://127.0.0.1:18081/health/ready` should report
   `{"status":"ok","checks":{"mysql":true,"schema":true,"redis":true}}`.
6. Test one public image ID and one public novel ID using an authenticated
   `X-API-Key` header on `/api/illust/ID` and `/api/novel/ID`.
7. Repeat requests and verify `source` and `version`. Use
   `?refresh=false` to skip Pixiv validation.
8. If something fails, only share redacted status codes and sanitized
   `docker compose logs --tail=100 api` output.

CI unit and throwaway-DB integration tests cannot replace real Pixiv auth
and download validation. The metadata fingerprint cannot guarantee
detection of replaced images when URLs and metadata remain unchanged.


9. Verify both Pixiv credentials without revealing them:

```sh
curl -sS -H "X-API-Key: YOUR_LOCAL_KEY" \
  http://127.0.0.1:18081/api/admin/pixiv/auth/status
```

Only share the boolean/status result if troubleshooting. Never share
`PIXIV_COOKIE`, `PHPSESSID` or `PIXIV_REFRESH_TOKEN`.


10. Optional integrity checks:

```sh
curl -X POST -H "X-API-Key: YOUR_LOCAL_KEY" \
  "http://127.0.0.1:18081/api/admin/cache/storage/reconcile?verify_hash=false"
```

Run once with `verify_hash=true` after initial validation if you want a full
local SHA-256 audit.
