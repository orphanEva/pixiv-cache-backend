# pixiv_archive 数据库初始化与升级（Schema v9）

> 新库使用当前 v9 初始化 SQL；v8 执行 `sql/upgrades/v8_to_v9.sql`，v6/v7 先逐级升到 v8。
> 已经执行过旧版 v8→v9 脚本的库，另执行 `sql/upgrades/v7_v9_sync_sources_compat.sql` 补齐旧小说追更枚举；版本号仍为 9。
> 原有 v1-v5 升级脚本不适用于当前结构，**不要按旧脚本逐级升级**。

## 1. 手动初始化（MySQL 8.0+）

确认 `pixiv_archive` 数据库中没有需要保留的数据。使用 MySQL 管理员执行：

```bash
mysql -h MYSQL_HOST -u root -p < sql/pixiv_archive.sql
```

DDL 不包含 DROP TABLE，适合空库。非空库请先单独备份，不要直接运行此脚本覆盖。
程序容器不建表或自动升级（禁止运行时 DDL），仅检查 `schema_version=9`。

## 2. 创建专用 DML 账号

```sql
CREATE USER 'pixiv'@'YOUR_API_HOST' IDENTIFIED BY 'YOUR_OWN_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE ON pixiv_archive.* TO 'pixiv'@'YOUR_API_HOST';
```

在 `.env` 中填入编码后的连接信息：
`DATABASE_URL=mysql+asyncmy://pixiv:PASSWORD@MYSQL_HOST:3306/pixiv_archive?charset=utf8mb4`。
不要提交 `.env`，也不要把 MySQL 直接暴露公网。

## 3. 结构说明

| 表 | 用途 |
|---|---|
| `schema_version` | 手动结构版本锁定 |
| `series` | 漫画与小说合集，联合主键 `(type,id)` |
| `series_members` | 合集成员与下载进度，联合主键 `(series_type,series_id,work_id)` |
| `works` | `(work_kind,id)` 区分小说与插画，保存当前作品及旧作复查预约 |
| `work_history` | 不可变作品版本 |
| `work_files` | 当前版本文件索引（作品/页码/类型唯一） |
| `work_history_files` | 历史文件索引（版本/页码/类型唯一） |
| `tags` / `work_tag_relation` | 标签与作品关联 |
| `sync_sources` / `work_sources` | 作者/收藏/合集/追更清单同步来源、账号绑定、分页进度及归因 |
| `account_migration_state` / `account_migration_runs` / `account_migration_items` | 当前账号协调、收藏迁移任务及逐项重试进度 |

`series_members` **故意不为 work_id 设置作品外键**：发现作品可以先写入，
待实际下载归档后才会创建 `works` 记录。

`works` 的系列类型必须和作品类型对应：
`novel` 作品只能挂在小说系列下，`manga` 作品只能挂在漫画系列下。
系列 ID 和系列类型要么同时为空，要么同时有值。
同一个远端系列数字 ID 可以分别出现在小说系列和漫画系列中。
已有作品引用的系列不能直接删除，必须先显式解除作品关联（数据库 RESTRICT）。

## 4. 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -sS http://127.0.0.1:18081/health/ready
docker compose logs -f api worker
```

若提示 schema version 不匹配，必须人工确认数据库版本和数据状态，
不能修改 `schema_version` 数字来跳过检查。

## 5. 旧版本数据库

先备份 MySQL 和归档目录，停止全部 API 及归档、同步、媒体、维护 worker。
使用具有 ALTER 和创建存储过程权限的管理员账号，明确选择目标数据库，并使用本次发布的整套脚本：

| 当前版本 | 执行顺序 |
|---|---|
| v6 | `v6_to_v7.sql` → `v7_to_v8.sql` → `v8_to_v9.sql` |
| v7（任一分支） | `v7_to_v8.sql` → `v8_to_v9.sql` |
| v8 | `v8_to_v9.sql` |
| 已用旧脚本升级的 v9 | `v7_v9_sync_sources_compat.sql`，版本号保持 9 |
| 新空库 | 当前 `sql/pixiv_archive.sql`，不再运行升级脚本 |

例如，当前库已经是 v9 时：

```bash
mysql -h MYSQL_HOST -u root -p --database=pixiv_archive < sql/upgrades/v7_v9_sync_sources_compat.sql
```

两条历史 v7 分支的区别：远端 v7 已有小说追更 `type='watchlist'`，本地 v7 已有
`novel_series`、`manga_series`、`followed_series` 及 `expand_series`、`scan_state_json`。
当前 `v7_to_v8.sql` 会先检查并补缺失列，再新增账号进度；不会重复添加已有列。
新建和升级 SQL 均保留全部来源类型，已有 `watchlist` 行的类型、ID 和作品归因不变。

独立兼容补丁可在 v7/v8/v9 重复执行，只补同步字段和枚举，不改变版本号；v7/v8 仍需后续版本升级。
脚本不删除作品或历史文件。MySQL DDL 会隐式提交，运行时禁止 `--force`；若中途失败，停止并核对已完成步骤，不能盲目重跑整个版本升级。

升级前后核对来源数量和类型，并确认最终版本为 9，再启动程序：

```sql
SELECT id, version FROM schema_version WHERE id=1;
SHOW COLUMNS FROM sync_sources;
SELECT type, COUNT(*) FROM sync_sources GROUP BY type;
```

不要只修改 `schema_version` 数字，也不要在有数据的库上执行全量初始化 SQL。

v1–v5 的真实数据需要单独设计迁移，历史脚本不能组合出当前结构。
新功能及配置见 [收藏、合集同步与限速](BOOKMARKS_SERIES_AND_LIMITS.md)。

详细升级说明见 [v1.10 可靠性更新](V1_10_RELIABILITY.md)。

## 6. 原小说追更来源的兼容接口

原有 `POST /api/sync/sources/watchlist/novel?run_now=true` 保留，依赖有效的 `PIXIV_COOKIE`。
它只处理小说追更，数据库保留 `type='watchlist'`、`remote_user_id='novel'`；已有来源不需要删除重建。
新的 `POST /api/sync/sources/followed-series` 则支持小说和漫画追更，使用独立的 `followed_series` 类型。
两者共享当前分页校验、账号绑定、归档队列和限速机制。
