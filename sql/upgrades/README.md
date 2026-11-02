# Historical schema upgrades (v1–v5)

These scripts remain here for historical reference only.

**The currently supported backend uses schema v6, and these scripts DO NOT produce the v6 structure.** The project was not yet deployed when v6 was designed, so there is deliberately no automatic upgrade from v5.

For a fresh installation, execute only `sql/pixiv_archive.sql` in an empty MySQL database. Never merely change `schema_version` from an older value to 6: the foreign keys and columns are different.

If importing a real database with data is needed in the future, create a separately reviewed migration with backup, content/path reconciliation, and rollback.

## v6 → v7（保留现有数据）

本次新增 `sql/upgrades/v6_to_v7.sql`，专门为 **已有 v6 数据库** 增加
`sync_sources.type='watchlist'`；不删除作品、缓存、文件或同步来源。
执行前备份、停止所有应用和 worker、确认版本为 6；以 MySQL 管理员手工执行该脚本后确认版本为 7。
历史 v1-v5 迁移脚本依然不适用于 v6/v7。
