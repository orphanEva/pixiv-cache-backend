# Manual schema upgrades

Current schema is **v9**. Back up the database and archive, then stop every application/worker before upgrading.

- v8: run `v8_to_v9.sql`.
- v7: run `v7_to_v8.sql`, then `v8_to_v9.sql`.
- v6: first run `v6_to_v7.sql`, then the two upgrades above.
- Existing v9 created with an earlier script: run `v7_v9_sync_sources_compat.sql` once before starting this merged release. It is safe to repeat and does not change the version number.
- v1–v5 scripts are historical references and cannot be chained into the current series schema.

Select the intended database explicitly. MySQL DDL commits implicitly: stop on the first error, do not use `--force`.
Never just edit schema_version. Fresh empty databases use `sql/pixiv_archive.sql` only.

The v9 upgrade adds the novel/illustration namespace to work identities and dependent foreign keys,
backfills existing rows, and adds the old-work recheck reservation column. Numeric IDs and disk paths are retained.

## Compatibility between the two v7 branches

The previous remote v7 supported `type='watchlist'` for the novel-only route
`POST /api/sync/sources/watchlist/novel`. The local v7 added persistent scan
state and `novel_series`, `manga_series`, and `followed_series` source types.
The merged model and SQL retain **all six** source values, including `author`
and `bookmarks`; existing `watchlist` rows keep their IDs and values.

Use the scripts from this release throughout the upgrade. `v7_to_v8.sql`
accepts either v7 layout: it adds `expand_series` and `scan_state_json` only
when missing, expands the enum, then adds account migration state. The current
`v6_to_v7.sql` and `v8_to_v9.sql` also preserve the legacy enum value.

If an older local upgrade has already completed, **do not rerun its versioned
migration or manually lower schema_version**. Apply the compatibility patch:

```sh
mysql -h MYSQL_HOST -u root -p --database=pixiv_archive < sql/upgrades/v7_v9_sync_sources_compat.sql
```

The patch accepts versions 7, 8 and 9, adds only missing scan columns, and
expands the source enum. It does not migrate any source to another type or
change schema_version, works, archive files, or source attribution. A v7/v8
database still needs the remaining numbered upgrades afterward. Existing v9
databases need only this patch. Fresh databases initialized with the current
`sql/pixiv_archive.sql` already contain the compatible layout.

Before and after upgrading, compare `SELECT type, COUNT(*) FROM sync_sources
GROUP BY type;`, check `SHOW COLUMNS FROM sync_sources;`, and confirm the final
schema_version is 9. Stop all application and worker processes while applying
the scripts. If a script fails partway through, stop and inspect the changes
before retrying; DDL cannot be rolled back as one transaction.
