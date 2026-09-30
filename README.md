# Pixiv Cache Backend

FastAPI + external MySQL 8 + Redis: a versioned caching backend for
Pixiv illustrations and novels.

## Manual database initialization (required)

**No automatic database or table creation.** This project's Docker Compose
starts only the API and Redis, **not MySQL**. Point `DATABASE_URL` to your
existing MySQL instance.

1. Use the complete [DDL file](sql/mysql-schema.sql) to **manually** create
   `pixiv_cache` and its three tables in MySQL.
2. Set up an application user with CRUD permissions (not DDL permissions).
3. Follow the [manual setup guide](docs/MANUAL_DATABASE_SETUP.md).
4. Copy `.env.example` to `.env` and configure `DATABASE_URL`,
   `PIXIV_REFRESH_TOKEN` and a random `API_KEY` of at least 32 characters.
5. Run `docker compose up -d --build` then check
   `GET http://127.0.0.1:18081/health/ready`.

Do **not** run `alembic upgrade head` on production. CI uses a separate
ephemeral test database to exercise migration code; deployed systems do not
perform any DDL. Database schema upgrades must be reviewed/applied manually.

## Business APIs

All non-health endpoints require `X-API-Key`:

- `GET /api/illust/{pixiv_id}`
- `GET /api/novel/{pixiv_id}`
- `GET /api/illust/{pixiv_id}/history`
- `GET /api/novel/{pixiv_id}/history`
- `GET /api/illust/{pixiv_id}/versions/{version_no}`
- `GET /api/novel/{pixiv_id}/versions/{version_no}`

Administrative endpoints:
- `GET /api/admin/cache/status`
- `GET /api/admin/cache/{kind}/{pixiv_id}`
- `POST /api/admin/cache/{kind}/{pixiv_id}/refresh`

When `refresh=true`, the backend fetches Pixiv metadata and checks the
`version_token`; if changed, it writes a new immutable version directory
and updates the database's current version pointer. Prior versions remain on
disk. With `refresh=false`, eligible cached data is returned directly.
`REMOTE_CHECK_TTL_SECONDS=0` validates each refreshed request.

Pixiv does not reliably expose a modification timestamp for every work.
The fallback fingerprint uses metadata and for novels the text body. For
illustrations, a replaced image that retains the same metadata and URL
might not be detected without a content check.

Media URLs are restricted to database-registered files, and access to
deleted/restricted works is denied. Remote temporary outages can serve a
previously authorized stale version when configured.

For tests run `pip install -e '.[dev]'` and `pytest -q
--ignore=tests/test_mysql_integration.py`. The separate integration test
needs its own ephemeral MySQL+Redis environment.

For authorized real Pixiv integration testing see
[the live validation guide](docs/LIVE_VALIDATION.md).
