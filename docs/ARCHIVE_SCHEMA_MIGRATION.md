# Archive schema migration notes

The original seven-table export is the design basis, not an executable
drop-in replacement for the revised application.

Changes from that export:
- Works: nullable remote_update_at, version_token, current_version_no,
  status_reason and temporary upstream status values.
- History: every version is retained, *including the current version*;
  immutable storage_path, version_token and original JSON metadata added.
- History files: hash, type and remote URL stored alongside their immutable
  path; work_files reflects the current version only.
- File path capacity raised to 1000 chars.
- Existing original series and normalized tag tables are preserved.
- The old three-table pixiv_cache design is retired.

Important: the new DDL never DROPs existing tables, but CREATE TABLE still
fails if the original named tables already exist. Do NOT interpret failure
as a reason to delete existing tables. A populated database additionally
requires migrating records and rewriting media references; these steps
are *not* handled by the new-install SQL. Make a full SQL dump and file
backup before a future migration.

Current code implements still illustration/novel API endpoints. Manga and
ugoira names are supported in the schema/mapper, but animated archive/ZIP
download and video support are not completed.
