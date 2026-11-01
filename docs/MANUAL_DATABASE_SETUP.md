# pixiv_archive 数据库初始化（全新项目 / Schema v6）

> 当前项目尚未投入使用，因此采用重新设计后的 **v6 全新建库结构**。
> 原有 v1-v5 升级脚本不适用于 v6，**不要按旧脚本逐级升级**。

## 1. 手动初始化（MySQL 8.0+）

确认 `pixiv_archive` 数据库中没有需要保留的数据。使用 MySQL 管理员执行：

```bash
mysql -h MYSQL_HOST -u root -p < sql/pixiv_archive.sql
```

DDL 不包含 DROP TABLE，适合空库。非空库请先单独备份，不要直接运行此脚本覆盖。
程序容器不建表或自动升级（禁止运行时 DDL），仅检查 `schema_version=6`。

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
| `works` | 当前作品数据及 `(series_type,series_id)` 联合外键 |
| `work_history` | 不可变作品版本 |
| `work_files` | 当前版本文件索引（作品/页码/类型唯一） |
| `work_history_files` | 历史文件索引（版本/页码/类型唯一） |
| `tags` / `work_tag_relation` | 标签与作品关联 |
| `sync_sources` / `work_sources` | 作者/收藏同步来源及归因 |

`series_members` **故意不为 work_id 设置作品外键**：发现作品可以先写入，
待实际下载归档后才会创建 `works` 记录。

`works` 的系列类型必须和作品类型对应：
`novel` 作品只能挂在小说系列下，`manga` 作品只能挂在漫画系列下。
系列 ID 和系列类型要么同时为空，要么同时有值。
同一个远端系列数字 ID 可以分别出现在小说系列和漫画系列中。

## 4. 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -sS http://127.0.0.1:18081/health/ready
docker compose logs -f api worker
```

若提示 schema version 不匹配，说明不是 v6 空库，必须人工确认数据状态，
不能修改 `schema_version` 数字来跳过检查。

## 5. 旧版本数据库

本次未提供自动 v5→v6 数据迁移：因为尚未投入使用。如果今后发现
确实存在旧版需要保留的数据，应单独编写有备份、回滚、校验的迁移。
旧版升级脚本仅属历史参考，不能组合出 v6 结构。
