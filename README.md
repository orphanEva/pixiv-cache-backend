# Pixiv Cache Backend

FastAPI + MySQL + Redis backend for local, versioned caching of Pixiv illustrations and novels.

## Main APIs

- `GET /api/illust/{pixiv_id}`
- `GET /api/novel/{pixiv_id}`
- `GET /api/illust/{pixiv_id}/history`
- `GET /api/novel/{pixiv_id}/history`
- `GET /api/illust/{pixiv_id}/versions/{version_no}`
- `GET /api/novel/{pixiv_id}/versions/{version_no}`

## Cache flow

1. Query MySQL for local metadata and immutable version history.
2. If absent, acquire a Redis lock, fetch from Pixiv, persist version 1, then return local media URLs.
3. If present and `refresh=true`, obtain a remote snapshot and calculate a `version_token`.
4. If unchanged, update `last_checked_at` and return the current local version.
5. If changed, write a new immutable version directory and DB version row, then move `current_version_no` forward.
6. Older versions are never overwritten, so rollback/history does not require copying the old files first.

`REMOTE_CHECK_TTL_SECONDS=0` means every `refresh=true` request checks Pixiv. Increase it to reduce remote requests.

## Version detection

Pixiv integrations do not reliably expose a modification timestamp for every work. The backend therefore hashes update-sensitive remote fields. For novels the body text is included. For illustrations the fingerprint includes title, caption, tags, page count and downloadable image URLs. A remote update timestamp is included when available.

## Remote failure behavior

- 404/not found -> local record is marked `DELETED`, API returns 404.
- 403/private/restricted -> marked `RESTRICTED`, API returns 403.
- auth failure -> marked `AUTH_REQUIRED`, API returns 401.
- rate-limit/temporary/network failure -> marked `UNAVAILABLE`; if a cache exists and `SERVE_STALE_ON_REMOTE_UNAVAILABLE=true`, the last local version is returned with a stale source marker.
- Historical files are kept on disk even when a work becomes unavailable; normal refreshed access does not silently treat restricted/deleted content as current.

## Run

```bash
cp .env.example .env
# set PIXIV_REFRESH_TOKEN in .env
docker compose up -d --build
```

The API container runs `alembic upgrade head` before starting Uvicorn.

Examples:

```bash
curl 'http://127.0.0.1:18081/api/illust/12345678'
curl 'http://127.0.0.1:18081/api/novel/12345678'
curl 'http://127.0.0.1:18081/api/illust/12345678/history'
curl 'http://127.0.0.1:18081/api/illust/12345678/versions/1'
```

Skip remote validation and return cache immediately:

```bash
curl 'http://127.0.0.1:18081/api/illust/12345678?refresh=false'
```

## Storage layout

```text
data/
  illust/{pixiv_id}/versions/1/0.jpg
  illust/{pixiv_id}/versions/2/0.jpg
  novel/{pixiv_id}/versions/1/novel.txt
  novel/{pixiv_id}/versions/2/novel.txt
```

## Notes

- Authentication uses a Pixiv refresh token, not username/password login.
- The Pixiv adapter is isolated behind `PixivClient`, so a future browser/cookie or alternate API implementation can replace PixivPy without changing the cache service.
- Access should respect Pixiv access controls and terms; do not use the cache layer to bypass content the authenticated account can no longer access.

## Health checks

- `GET /health/live` - process liveness
- `GET /health/ready` - verifies MySQL and Redis connectivity; returns 503 when a dependency is unavailable

## Download safety

Illustrations are streamed to `*.part`, validated as image content, bounded by `MAX_ASSET_BYTES`, hashed while streaming, and atomically renamed only after the download completes. A failed version is removed entirely.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```
