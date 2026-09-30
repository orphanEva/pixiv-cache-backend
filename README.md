# Pixiv Cache Backend

FastAPI + externally managed **MySQL 8** + Redis. Versioned, local Pixiv
illustration and novel cache built around the seven-table `pixiv_archive` schema.

## Manual SQL initialization — no automatic DDL

The API container does **not** create a database or run migrations. Docker
Compose starts the API and Redis only. MySQL must already exist.

**For a brand-new empty database:** manually run
[sql/pixiv_archive.sql](sql/pixiv_archive.sql) as a MySQL administrator, then
create a separate low-privilege account with SELECT/INSERT/UPDATE/DELETE.
Follow [the complete setup guide](docs/MANUAL_DATABASE_SETUP.md).

**For an existing database with data:** DO NOT run this new-install DDL or
your old dump. Back up the original schema and media first. The old
three-table `pixiv_cache` layout and the originally supplied seven-table
dump are *different schemas* and need an explicit, reviewed data migration.

Seven tables:

- `works`: current work metadata, fingerprint, accessibility, current version
- `series`: series metadata
- `tags` and `work_tag_relation`: searchable current tags
- `work_files`: pointers to files in the current version
- `work_history`: **every** version, including the current one
- `work_history_files`: immutable files attached to each version

Files are saved to immutable `{kind}/{id}/versions/{number}/` directories.
A new version changes `works.current_version_no` and re-points
`work_files`; old files are *not moved or overwritten*. The remote
`remote_update_at` field is optional. Version comparison uses a SHA-256
fingerprint including novel text or illustration URL/metadata where available;
a remote image replaced in place behind an unchanged URL can still be missed.

Ugoira archival is implemented through the existing `/api/illust/{id}` route.
For an Ugoira work, the backend fetches Pixiv's Ugoira metadata, downloads the
original ZIP, validates every declared frame, preserves exact per-frame delay
metadata, extracts the frame sequence, and optionally creates a browser-friendly
MP4 with FFmpeg. The ZIP + frame timing JSON are the archival source of truth;
the MP4 is a derived preview and may be regenerated later.

## Start

```sh
cp .env.example .env
# Edit DATABASE_URL for the manually initialized pixiv_archive database.
# Set PIXIV_REFRESH_TOKEN and a strong, random API_KEY (>= 32 chars).
docker compose config --quiet
docker compose up -d --build
curl -sS http://127.0.0.1:18081/health/ready
```

MySQL must allow connections from the API container and should be protected
by a firewall. Avoid exposing API/DB ports directly on the public Internet.

## APIs

All non-health routes require an `X-API-Key` header.

- `GET /api/illust/{pixiv_id}`
- `GET /api/novel/{pixiv_id}`
- `GET /api/illust/{pixiv_id}/history`
- `GET /api/novel/{pixiv_id}/history`
- `GET /api/illust/{pixiv_id}/versions/{number}`
- `GET /api/novel/{pixiv_id}/versions/{number}`
- `GET /api/admin/cache/status`
- `GET /api/admin/cache/{kind}/{id}`
- `POST /api/admin/cache/{kind}/{id}/refresh`

`refresh=true` checks upstream according to
`REMOTE_CHECK_TTL_SECONDS` (0 checks each request). `refresh=false`
returns locally authorized cached content. When Pixiv reports a work
deleted/private, its regular data and protected media are blocked.
On transient upstream unavailability, optionally serve an already
authorized stale cache.

## Testing

```sh
pip install -e '.[dev]'
pytest -q --ignore=tests/test_mysql_integration.py
```

GitHub Actions separately provisions a disposable MySQL/Redis environment,
**explicitly** executes the manually maintained SQL there, and verifies V1
creation, validation, V2 update and history persistence. No production runtime
initialization is introduced by the CI helper.

[Real Pixiv validation](docs/LIVE_VALIDATION.md) is separate; never commit or
share your Pixiv refresh token.


## Ugoira archive layout

For work `123`, version `2`:

```text
data/ugoira/123/versions/2/
├── original.zip
├── ugoira_meta.json
├── preview.mp4
├── cover.jpg              # when Pixiv exposes a cover
└── frames/
    ├── 000000.jpg
    ├── 000001.jpg
    └── ...
```

Database file types:
- `ugoira_zip`: original Pixiv ZIP
- `ugoira_meta`: exact frame names and millisecond delays
- `ugoira_mp4`: derived FFmpeg preview
- `cover`: still preview image

`UGOIRA_GENERATE_MP4=false` disables only the derived MP4. Raw archival still
succeeds. `UGOIRA_MAX_FRAMES` and `UGOIRA_MAX_UNCOMPRESSED_BYTES` protect
against malformed or unexpectedly huge ZIP archives.
