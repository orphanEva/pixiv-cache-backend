# Pixiv Cache Backend

FastAPI + externally managed **MySQL 8** + Redis. Versioned, local Pixiv
illustration and novel cache built around the manually versioned `pixiv_archive` schema.

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

Versioned archive and series/synchronization tables:

- `schema_version`: manually applied database schema version (metadata only)
- `sync_sources`: persistent author/bookmark/novel-watchlist synchronization subscriptions
- `work_sources`: many-to-many attribution from archived works to sync sources
- `works`: current work metadata, fingerprint, accessibility, current version
- `series`: typed series identity (composite primary key `type,id`) and metadata
- `series_members`: composite series/work identity with discovery/queue/archive/failure status
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
- `POST /api/jobs/archive/{kind}/{id}?expand_series=true` (default: auto-discover series)
- `POST /api/jobs/archive/series/{novel|manga}/{series_id}` (entire series)
- `GET /api/jobs/series/{series_type}/{series_id}` (series member progress)
- `GET /api/jobs/{job_id}`
- `GET /api/jobs?status=dead`
- `POST /api/jobs/{job_id}/retry`
- `POST /api/jobs/{job_id}/replay`
- `GET /api/ops/status`
- `GET /api/ops/jobs?status=dead`
- `POST /api/ops/jobs/{job_id}/replay`
- `POST /api/sync/sources/author/{user_id}`
- `POST /api/sync/sources/bookmarks/{user_id}`
- `GET /api/sync/sources`
- `POST /api/sync/sources/{id}/run`
- `GET /api/sync/jobs/{job_id}`
- `GET /api/library/works`
- `GET /api/library/works/{pixiv_id}`
- `GET /api/library/works/{pixiv_id}/history`
- `GET /api/library/works/{pixiv_id}/versions/{version}`
- `POST /api/library/works/{pixiv_id}/refresh`
- `GET /api/library/works/{pixiv_id}/integrity`
- `GET /api/library/authors`
- `GET /api/library/authors/{author_id}`
- `GET /api/library/tags`
- `GET /api/library/series`
- `GET /api/library/series/{series_type}/{series_id}`
- `GET /api/library/stats`

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


## Pixiv token-only authentication

Account/password/TOTP login, GPPT and headless Chromium have been removed.
Configure `PIXIV_REFRESH_TOKEN` for PixivPy; optionally configure `PIXIV_COOKIE`
for Web AJAX, especially manga series. Both are supplied manually in `.env`.
The server does not generate new credentials. See [auth guide](docs/PIXIV_AUTH.md).

```sh
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:18081/api/admin/pixiv/auth/status
```

## Series archive and logs

Discover a series automatically when downloading a single novel or manga:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/jobs/archive/novel/123456?expand_series=true"
```

Or pass a series ID directly (works if automatic detection is unavailable):

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/jobs/archive/series/novel/12345"
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/jobs/archive/series/manga/12345"
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:18081/api/jobs/series/novel/12345
```

The series and discovered members are persisted in MySQL first, and each member
uses the existing Redis download queue. The worker marks members archived or failed.
Web manga discovery relies on an undocumented Pixiv AJAX endpoint; if Pixiv changes its
response, a series request fails instead of claiming a partial collection is complete.
Single-item download remains available with `expand_series=false`.

JSON logs include request IDs, durations, archive and series events; view them with
`docker compose logs -f api worker`. Optional per-process rotating file logs can
be enabled with `LOG_FILE` (do not share one log file between multiple processes).

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

### 数据库版本：v7 全新初始化

因为项目还没有投入使用，这次直接重构了系列表的主键和外键。
**全新空库执行 `sql/pixiv_archive.sql` (v7)；已运行 v6 数据库请备份后执行 `sql/upgrades/v6_to_v7.sql`，不要重新建库。**
运行时程序从不自动建表或迁移。详见
[全新建库指南](docs/MANUAL_DATABASE_SETUP.md)。

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


## v1.3 maintenance and disaster recovery

Compose now runs a separate `maintenance` container. By default it performs a
read-only storage/DB integrity audit every six hours. It does not hash every
large asset and does not repair anything unless explicitly configured:

```env
INTEGRITY_AUDIT_INTERVAL_SECONDS=21600
INTEGRITY_AUDIT_VERIFY_HASH=false
INTEGRITY_AUDIT_REPAIR_SAFE=false
```

A complete backup contains the MySQL logical dump plus the canonical archive
directories (`illust/manga/ugoira/novel`). Pixiv auth caches, quarantine data
and Redis job state are deliberately excluded.

Create and verify a backup while the service is running:

```sh
docker compose exec api python -m app.maintenance.backup create --name daily
docker compose exec api python -m app.maintenance.backup verify /data/pixiv/.backups/daily
```

Backup creation acquires a global maintenance freeze, pauses new archive writes,
waits for current per-work writers to finish, then snapshots DB + files.

Restore is deliberately destructive and requires an explicit flag. For a real
restore, stop request/worker containers first and provide a privileged
`MAINTENANCE_DATABASE_URL` capable of recreating archive tables:

```sh
docker compose stop api worker sync maintenance
docker compose run --rm api python -m app.maintenance.backup restore \
  /data/pixiv/.backups/daily --confirm-destructive-restore
docker compose up -d
```

Before applying the requested restore, the CLI automatically takes a
pre-restore database dump. If the target DB restore fails, it attempts to put
that DB dump back and rolls storage directories back to their previous state.

v1.3 itself used schema v2. v1.4 adds persistent sync sources and requires schema v3.


## v1.4 author and bookmark synchronization

A separate `sync` container discovers remote works and submits ordinary
archive jobs. It never downloads media itself:

```text
Pixiv author/bookmarks
        ↓
    sync worker
        ↓
 Redis archive jobs
        ↓
  archive worker
        ↓
 MySQL + local media
```

### Archive an author's works

Create/update an author source and run it immediately:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/sync/sources/author/123456?include_illust=true&include_novel=true&interval_seconds=21600&run_now=true"
```

The first run walks all Pixiv pages (up to `SYNC_MAX_PAGES_PER_RUN`). Later
runs remember the newest IDs from the previous run and stop as soon as that old
frontier is reached.

### Synchronize bookmarks

For the authenticated Pixiv account:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/sync/sources/bookmarks/self?restrict=public&run_now=true"
```

Private bookmarks:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/sync/sources/bookmarks/self?restrict=private&run_now=true"
```

`self` is resolved from the authenticated Pixiv App API session. A numeric
Pixiv user id can be used for public sources.

List sources:

```sh
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:18081/api/sync/sources
```

Force a complete rescan:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:18081/api/sync/sources/SOURCE_ID/run?full=true"
```

Incremental discovery is durable: source schedule/frontier state lives in
MySQL, sync jobs live in a Redis Stream, and actual media archive jobs use the
existing archive Redis Stream.

### 历史版本结构说明

本节介绍旧版的同步源功能；当前运行版本使用 v7 新建表，运行中的 v6 数据库可手动升级，不应套用历史迁移脚本。

## v1.5 API-first library

The library layer is designed to be useful directly from scripts and other
services; a browser frontend is optional.

Typical filtered query:

```sh
curl -G -H "X-API-Key: $API_KEY" \
  --data-urlencode "q=纳西妲" \
  --data-urlencode "type=illust" \
  --data-urlencode "type=ugoira" \
  --data-urlencode "tag=原神" \
  --data-urlencode "page=1" \
  --data-urlencode "page_size=50" \
  http://127.0.0.1:18081/api/library/works
```

Every list response uses:

```json
{
  "items": [],
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total": 0,
    "pages": 0
  }
}
```

Work summaries contain tags, source IDs, current file count/size and archive
status. Work detail can optionally include novel content, raw Pixiv metadata,
or server-local paths:

```text
GET /api/library/works/123?include_content=true&include_raw_meta=true
```

By default local filesystem paths are omitted. Files expose a relative
`download_url`; use the same `X-API-Key` header when downloading it.

Management actions are intentionally conservative:

- queue a refresh: `POST /api/library/works/{id}/refresh`
- verify current files: `GET /api/library/works/{id}/integrity?verify_hash=true`

v1.5 does not expose a destructive delete endpoint.

### Work source attribution

One work can belong to multiple sync sources. For example the same Pixiv work
may be discovered through both an author subscription and your public
bookmarks. Redis archive-job deduplication merges those source IDs and
`work_sources` records all of them after archival succeeds.

Filter by source:

```text
GET /api/library/works?source_id=12
```

### Schema v6（新库）

`series` 使用联合主键 `(type, id)`，
`series_members` 使用 `(series_type, series_id, work_id)` 唯一键，
`works` 使用联合外键 `(series_type, series_id)`，
从数据库层隔离同号小说与漫画合集。
只需执行 `sql/pixiv_archive.sql`，无需升级脚本。

## v1.6 archive reliability and operations

Archive jobs now use automatic exponential backoff for transient failures.

Default retry policy:

```env
ARCHIVE_JOB_MAX_ATTEMPTS=5
ARCHIVE_JOB_RETRY_BASE_SECONDS=30
ARCHIVE_JOB_RETRY_MAX_SECONDS=1800
ARCHIVE_JOB_DEAD_MAXLEN=10000
```

State flow:

```text
queued
  -> running
      -> succeeded
      -> retry_wait -> queued -> running ...
      -> dead
```

Only transient failures are retried automatically. Pixiv temporary
unavailability, network timeouts, retryable HTTP responses (408/425/429/5xx),
maintenance races and transient database connection errors are retryable.
Explicit not-found/private/auth failures, invalid archive data and insufficient
local disk space go directly to `dead`.

After the configured attempts are exhausted, the job is stored as `dead` and
an event is appended to the bounded Redis DLQ stream. The original task status,
error, attempts and sync-source IDs remain queryable for the normal job-status
TTL.

Query dead jobs:

```sh
curl -G -H "X-API-Key: $API_KEY" \
  --data-urlencode "status=dead" \
  http://127.0.0.1:18081/api/jobs
```

Replay a dead job:

```sh
curl -X POST -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:18081/api/jobs/JOB_ID/replay
```

Replay creates a new archive job and preserves the original sync-source
attribution. The original dead job remains as history until its status TTL
expires.

Operational status:

```sh
curl -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:18081/api/ops/status
```

The response summarizes MySQL/Redis/schema readiness, archive/sync/maintenance
worker heartbeats, archive queue/pending/retry/DLQ counts, sync queue state and
filesystem capacity.

The equivalent task management endpoints under `/api/ops/jobs` are convenient
for monitoring clients:

```text
GET  /api/ops/jobs?status=retry_wait
GET  /api/ops/jobs?status=dead
POST /api/ops/jobs/{job_id}/replay
```

### DDL 中文注释

最新 `sql/pixiv_archive.sql` 已为每张表、每个字段提供中文 COMMENT，
适合直接在 DataGrip、Navicat 或 IDEA Database 查看。
当前程序要求 `schema_version=6`，且只支持重新初始化的空数据库。


## 自动备份小说追更列表（v1.8，Schema v7）

这是 Pixiv 小说合集页面的 **「加入追更列表」**，不是普通小说收藏夹。
依赖有效的 `PIXIV_COOKIE`（通常包含 PHPSESSID）和已启动的 `sync`、`worker` 服务。

启动同步源（默认每 6 小时重新扫描一次所有仍在追更的小说合集）：

```sh
curl -X POST -H "X-API-Key: $API_KEY" \\
  "http://127.0.0.1:18081/api/sync/sources/watchlist/novel?run_now=true&interval_seconds=21600"
```

可从 `GET /api/sync/sources` 取得同步源 ID，使用 `GET /api/sync/jobs/{job_id}` 查看扫描状态，或用 `POST /api/sync/sources/{id}/run?full=true` 手动触发。

- 首次从 Pixiv Web `/ajax/watch_list/novel` 读取追更列表；以后每次都重新扫描旧合集，不能用普通收藏的增量边界跳过旧合集。
- 通过现有合集发现服务遍历章节；已成功归档的章节不重复投递，新增或之前失败的章节进入 Redis 队列。
- 取消追更后，该合集不再被后续扫描；本地已经归档的文件不会删除。
- **这项功能需要现有数据库从 v6 升级至 v7。** 不要对有数据的库执行全量初始化 SQL。
- 接口为未公开的 Pixiv Web AJAX，真实账号的结构仍需现场验证；如 Cookie 失效或响应结构变化，任务明确失败，不会假称全集完成。
- 目前自动追更仅支持 **小说**；漫画追更列表和自动扫描所有关注作者尚未接入。
