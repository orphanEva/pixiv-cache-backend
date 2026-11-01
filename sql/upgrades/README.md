# Historical schema upgrades (v1–v5)

These scripts remain here for historical reference only.

**The currently supported backend uses schema v6, and these scripts DO NOT produce the v6 structure.** The project was not yet deployed when v6 was designed, so there is deliberately no automatic upgrade from v5.

For a fresh installation, execute only `sql/pixiv_archive.sql` in an empty MySQL database. Never merely change `schema_version` from an older value to 6: the foreign keys and columns are different.

If importing a real database with data is needed in the future, create a separately reviewed migration with backup, content/path reconciliation, and rollback.
