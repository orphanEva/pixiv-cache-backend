# Source synchronization

v1.4 adds persistent scheduled discovery for Pixiv authors and bookmarks.

## Separation of responsibilities

The sync worker only discovers Pixiv IDs. It does not download media. Each
discovered ID is submitted to the existing durable archive queue, so download,
versioning, Ugoira processing, disk checks and per-work locking remain in one
place.

## Sources

`sync_sources` stores:

- source type: `author` or `bookmarks`
- remote user id, or `self`
- public/private bookmark restriction
- whether illustrations and/or novels are included
- schedule interval
- incremental frontier
- last run/success/error and next scheduled run

## Incremental frontier

Remote Pixiv lists are ordered newest-first. On a successful run, the sync
worker stores the first `SYNC_FRONTIER_SIZE` IDs as the new frontier. On the
next incremental run it keeps paging until it encounters any ID from the old
frontier, then stops.

This avoids rescanning a large author library every few hours while still
handling more than one page of newly posted works.

A manual `full=true` run ignores the old frontier and traverses the source
until Pixiv has no next page or `SYNC_MAX_PAGES_PER_RUN` is reached.

## Bookmarks

Use `remote_user_id=self` for the authenticated Pixiv account. Public and
private bookmarks are separate sources because Pixiv exposes them separately.

Archival is append-oriented: if a work is later removed from Pixiv bookmarks,
the already archived local work is not deleted. Bookmark sync means "archive
things that appear in this collection", not "mirror deletions".

## Scheduling

The sync worker periodically reserves due rows from MySQL and submits durable
Redis Stream sync jobs. Job deduplication prevents one source from running twice
concurrently. Failed source runs store the error and use a shorter retry time.

During maintenance/backup freeze, the sync worker stops claiming/scheduling
work.

## Pixiv API pagination

The implementation uses PixivPy's current App API methods:
`user_illusts`, `user_novels`, `user_bookmarks_illust`,
`user_bookmarks_novel`, and `parse_qs(next_url)`.
