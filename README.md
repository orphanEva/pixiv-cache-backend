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

Seven archive tables plus one metadata table:

- `schema_version`: manually applied database schema version (metadata only)
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
- `POST /api/admin/cache/{kind}/{id}/refresh` (queues a worker job)
- `POST /api/jobs/archive/{kind}/{id}`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/retry`

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
succeeds. Extracted frames are temporary by default; set
`UGOIRA_KEEP_EXTRACTED_FRAMES=true` only if you explicitly want to retain them.
`UGOIRA_MAX_FRAMES` and `UGOIRA_MAX_UNCOMPRESSED_BYTES` protect against malformed
or unexpectedly huge ZIP archives.


## Pixiv dual authentication

The App API uses `PIXIV_REFRESH_TOKEN`. Pixiv Web AJAX uses the optional
raw browser `PIXIV_COOKIE` (typically including `PHPSESSID`). These are
independent credentials: App API requests keep working if the Web cookie
expires, and public Web Ugoira metadata falls back to an anonymous request.

Check both without exposing credentials:

```sh
curl -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:18081/api/admin/pixiv/auth/status
```

Example shape:

```json
{
  "app_api": {"configured": true, "authenticated": true, "status": "ok"},
  "web_cookie": {"configured": true, "authenticated": true, "status": "ok"}
}
```

The response never contains the refresh token, raw cookie, PHPSESSID or access
token. Keep `.env` private. If future Pixiv Web POST operations are added,
they must also implement Pixiv's CSRF-token flow; current Web usage is GET-only.


## Automatic Pixiv authentication

Manual `PIXIV_REFRESH_TOKEN` and `PIXIV_COOKIE` remain supported. To let
the service maintain them automatically instead:

```env
PIXIV_AUTO_AUTH=true
PIXIV_USERNAME=your_pixiv_login
PIXIV_PASSWORD=your_pixiv_password
# Optional, for unattended 2FA:
PIXIV_TOTP_SECRET=BASE32_OR_OTPAUTH_URI
```

Derived credentials are cached under `/data/pixiv/auth` and survive container
restarts through the existing `./data:/data/pixiv` volume. The cache contains
only the derived refresh token and Cookie, not the username/password/TOTP
secret. Cache files are written with restrictive permissions.

Recovery flow:

```text
App API:
cached/manual refresh token
  -> Pixiv rejects it
  -> gppt.refresh()
  -> if refresh fails: gppt.login(username,password,TOTP)
  -> save new refresh token

Web:
cached/manual Cookie
  -> Pixiv Web rejects it / protected metadata unavailable
  -> headless Chromium login
  -> optional TOTP
  -> save new Pixiv Cookie
```

Automatic browser login is deliberately rate-limited after failures. If Pixiv
requires CAPTCHA or another human verification step the backend returns
`interactive_required` instead of repeatedly attempting login.

Check without triggering browser login:

```sh
curl -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:18081/api/admin/pixiv/auth/status
```

Explicitly ask the backend to recover both credentials:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:18081/api/admin/pixiv/auth/refresh
```


## v1.1 stability controls

The backend now renews Redis cache locks while long downloads/transcodes are
running. If lock ownership is lost, the database transaction is refused before
commit.

Set `PIXIV_DEEP_IMAGE_CHECK=true` to re-download still images when metadata
appears unchanged and compare SHA-256 against the current archive. This catches
the rare case where Pixiv replaces bytes behind the same URL/metadata at the cost
of additional bandwidth.

Run a read-only storage consistency audit with:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/admin/cache/storage/reconcile?verify_hash=false"
```

Use `verify_hash=true` for a full SHA-256 audit. The endpoint reports missing
registered files, hash mismatches, orphan version directories, stale `.part`
files, and paths outside the configured storage root. It never deletes files.

### Manual schema versions

New databases should always use the current `sql/pixiv_archive.sql`. Existing
v1 archive databases must manually apply:

```text
sql/upgrades/v1_to_v2.sql
```

before starting v1.1. The application verifies `schema_version` on startup but
does not execute schema changes itself.


## v1.2 archive worker

Long downloads and FFmpeg transcodes can now run outside the HTTP process.
Docker Compose starts a separate `worker` container using a durable Redis
Stream. The API and worker share `/data/pixiv`, while MySQL remains external.

Submit an archive/refresh job:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/jobs/archive/illust/123456?force_refresh=true"
```

The API returns HTTP 202 with a `job_id`. Poll it:

```sh
curl -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/jobs/JOB_ID"
```

Jobs move through `queued -> running -> succeeded|failed`. Requests for the
same work are deduplicated while a job is queued/running. Failed/completed jobs
can be explicitly resubmitted via `POST /api/jobs/{job_id}/retry`.

Redis Streams are used instead of an in-memory queue. A worker crash leaves an
unacknowledged pending message; another worker can reclaim it after the idle
lease expires. While processing a long job, the worker periodically refreshes
that pending lease so healthy work is not stolen.

The old GET endpoints remain backward-compatible and can still refresh
synchronously. For large Ugoira or bulk archival, prefer the job API.

### Storage capacity guard

The backend keeps at least `STORAGE_MIN_FREE_BYTES` free (default 2 GiB).
Known Content-Length values and Ugoira uncompressed size are included in the
preflight calculation. A full disk condition fails with HTTP 507 instead of
writing until the filesystem is exhausted.

### Safe reconcile repair

Audit only:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/admin/cache/storage/reconcile"
```

Conservative repair:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/admin/cache/storage/reconcile?repair_safe=true"
```

Safe repair only removes sufficiently old `.part` files and moves sufficiently
old orphan version directories under `.quarantine`. It never auto-modifies
missing registered files, hash mismatches, database rows, or paths outside the
storage root.

This v1.2 worker/storage change does **not** require a new MySQL schema version.
Schema version remains v2.
