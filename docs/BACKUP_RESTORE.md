# Backup, restore, and maintenance

## Backup contents

A backup directory contains:

```text
manifest.json
database.sql
storage.tar.gz
```

`database.sql` is produced with a consistent transactional MySQL dump.
`storage.tar.gz` contains only canonical archive directories:

- illust
- manga
- ugoira
- novel

It intentionally excludes:

- `auth/` (Pixiv refresh token / Cookie cache)
- `.quarantine/`
- `.backups/`
- Redis Stream job/status state

The manifest records SHA-256 for both payload files and the required schema
version.

## Consistency freeze

Backup creation takes the Redis maintenance freeze. New writes are rejected or
left queued, the archive and sync workers stop claiming new jobs, and backup waits until
all existing per-work cache locks disappear. Local read-only cache access is
still possible.

## Commands

Create:

```sh
docker compose exec api python -m app.maintenance.backup create --name daily
```

Verify:

```sh
docker compose exec api python -m app.maintenance.backup verify \
  /data/pixiv/.backups/daily
```

Restore:

```sh
docker compose stop api worker sync maintenance
docker compose run --rm api python -m app.maintenance.backup restore \
  /data/pixiv/.backups/daily --confirm-destructive-restore
docker compose up -d
```

Restore validates manifest/checksums/tar paths before touching live data. It
stages archive files, makes a pre-restore DB dump, swaps storage with a rollback
directory, restores MySQL, verifies schema_version, then removes the rollback
storage only after success.

### Database privileges

Normal runtime remains DML-only. Backup generally needs read access. Restore
needs privileges to execute the dump's DROP/CREATE/INSERT statements. Supply
that account through `MAINTENANCE_DATABASE_URL`; do not commit it.

## Capacity

When BACKUP_ROOT is on the same filesystem as the archive, creation requires
enough free space for approximately the complete archive plus
`STORAGE_MIN_FREE_BYTES`. For large libraries, mount BACKUP_ROOT on a separate
disk.

## Scheduled integrity patrol

The `maintenance` Compose service runs
`app.workers.integrity_worker`. A distributed Redis lock prevents duplicate
full scans when multiple maintenance workers exist. Default behavior is
read-only and hash verification is off to avoid repeatedly reading every large
file.

Recommended default:

```env
INTEGRITY_AUDIT_INTERVAL_SECONDS=21600
INTEGRITY_AUDIT_VERIFY_HASH=false
INTEGRITY_AUDIT_REPAIR_SAFE=false
```

Run manual SHA-256 audits when needed rather than enabling continuous hashing on
a large archive.
