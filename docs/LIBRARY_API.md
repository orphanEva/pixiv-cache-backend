# Library API

The `/api/library` namespace is the stable browsing/management API for local
archive consumers. It is intentionally API-first; no frontend is required.

All requests require:

```http
X-API-Key: <your API key>
```

## Work list and search

```http
GET /api/library/works
```

Common parameters:

- `page` (default 1)
- `page_size` (default 50, max 200)
- `q`: title / author name / caption substring
- repeatable `type`: `illust`, `manga`, `ugoira`, `novel`
- repeatable `status`
- `author_id`
- `series_id`
- repeatable `tag`
- `tag_mode=all|any`
- `source_id`
- `is_ai=true|false`
- `x_restrict`
- `cached_from`, `cached_to`
- `remote_created_from`, `remote_created_to`
- `search_novel_text=true` to include novel bodies in `q` matching
- `sort=cached_at|updated_at|remote_created_at|remote_updated_at|title`
- `order=asc|desc`

Example with repeated query values:

```sh
curl -G -H "X-API-Key: $API_KEY" \
  --data-urlencode "type=illust" \
  --data-urlencode "type=ugoira" \
  --data-urlencode "tag=原神" \
  --data-urlencode "tag=纳西妲" \
  --data-urlencode "tag_mode=all" \
  http://127.0.0.1:18081/api/library/works
```

`search_novel_text=true` can be expensive on a large database because it
performs substring matching against novel content. Leave it off unless needed.

## Work detail

```http
GET /api/library/works/{pixiv_id}
```

Optional expansions:

- `include_content=true`: include current novel text when locally accessible
- `include_raw_meta=true`: include the stored raw Pixiv metadata
- `include_paths=true`: expose server-local file paths

The default response does not reveal local paths. Each current file includes a
relative `download_url`. Send the same API key to that URL.

If a work is in a blocked archive status (for example deleted/private under the
current access policy), its metadata can still be inspected for management, but
protected content/download URLs are not exposed.

## Versions

```http
GET /api/library/works/{pixiv_id}/history
GET /api/library/works/{pixiv_id}/versions/{version_no}
```

The same `include_content`, `include_raw_meta` and `include_paths` controls
apply to version payloads.

## Integrity

Fast existence/path check:

```http
GET /api/library/works/{pixiv_id}/integrity
```

Full SHA-256 verification of the current registered files:

```http
GET /api/library/works/{pixiv_id}/integrity?verify_hash=true
```

This endpoint is read-only.

## Refresh

Queue an upstream re-check through the archive worker:

```http
POST /api/library/works/{pixiv_id}/refresh
```

It returns an ordinary archive job rather than holding the HTTP connection open.

## Authors

```http
GET /api/library/authors
GET /api/library/authors/{author_id}
```

Author summaries include total works, counts by work type, and latest remote /
cached timestamps. To retrieve the author's actual works, use:

```text
GET /api/library/works?author_id=123456
```

## Tags

```http
GET /api/library/tags
```

Tag summaries include the number of archived works. Retrieve works for a tag
through `/works?tag=...`.

## Series

```http
GET /api/library/series
GET /api/library/series/{series_id}
```

Retrieve series works through `/works?series_id=...`.

## Statistics

```http
GET /api/library/stats
```

The response includes:

- total works and counts by type/status
- author/tag/series/version counts
- current file count and current-version bytes
- all archived history-file count and bytes
- sync source count
- work/source attribution count

`archive_storage_bytes` is the better database-level approximation of archive
media usage. It intentionally does not include backup files, quarantine data,
authentication cache, or filesystem allocation overhead.

## Source attribution

`work_sources` is many-to-many. A single work can report several sources, for
example an author subscription plus a bookmark source.

Work summaries contain `source_ids`; work detail expands them with source type,
remote user ID, restriction mode, first-seen and last-seen times.

Filter:

```text
GET /api/library/works?source_id=12
```

## Pagination contract

All collection endpoints return:

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

The maximum page size is intentionally bounded so API clients cannot
accidentally request the entire archive in one response.
