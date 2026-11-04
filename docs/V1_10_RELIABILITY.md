# v1.10：作品身份隔离、备份恢复、旧作复查与动图补生成

本版要求 schema v9。源码和测试已更新，程序仍不会自动执行数据库 DDL。

## 升级已有数据库

1. 备份当前数据库和归档目录，停止 api、worker、sync、maintenance 及其他写入进程。
2. 查询 `SELECT version FROM schema_version WHERE id=1;`。
3. v8 执行 `sql/upgrades/v8_to_v9.sql`。v7 先执行 `v7_to_v8.sql`；v6 先升 v7，再逐步升 v9。
4. 确认版本为 9 后重新创建全部服务：

```bash
docker compose up -d --build --force-recreate api worker sync maintenance media
```

初始化 SQL `sql/pixiv_archive.sql` 仅用于新空库。MySQL DDL 隐式提交；升级必须遇错停止，
不能使用忽略错误选项或仅修改 schema_version。旧应用不能连接已经升级的数据库。
本次没有在用户数据库上运行迁移，也没有执行真实 Pixiv 写操作。

v8 的备份应由对应 v8 程序恢复到隔离环境，再执行 v8→v9 升级；新版拒绝直接恢复旧 schema 的备份。

## 同号小说与插画

作品身份改为 `(work_kind, id)`：novel 是小说，illust 包含插画、漫画和 Ugoira。
历史版本、当前文件、标签及来源都按同一身份关联。既有数字 ID、历史编号和磁盘路径保留。
同号小说与插画可以同时存在；同一插画从 illust 变为 manga 时保持同一作品身份。

原有 `/api/novel/{id}`、`/api/illust/{id}` 用法保持。
资料库以数字 ID 查询时，如果两个命名空间都有同号作品，返回 HTTP 409，需要指定 kind：

```text
GET  /api/library/works/123?kind=novel&include_content=true
GET  /api/library/works/123?kind=illust
GET  /api/library/works/123/history?kind=novel
GET  /api/library/works/123/versions/1?kind=novel
GET  /api/library/works/123/integrity?kind=illust
POST /api/library/works/123/refresh?kind=novel
```

只有一种类型的 ID 可继续省略 kind。新客户端建议始终携带 kind。
账号迁移也按两种命名空间扫描，不会遗漏同号作品。

## 旧作品、旧章节定期复查

作者/收藏增量扫描仍负责发现新增作品；新增复查调度器从本地作品库选择较久未检查的 active 作品，
因此不受远端收藏列表或作者列表的增量边界影响。由 sync 进程调度，archive worker 执行。

默认检查间隔为七天，每分钟最多投递 10 项；积压时实际完成时间取决于队列长度和限速。
任务强制核对远端内容，不新增收藏。内容不变复用文件，变化保存新版本，临时失败走现有退避重试。
deleted/private 等已不可访问的作品不会由此调度器反复请求，可显式提交刷新任务重查。

```env
REMOTE_RECHECK_ENABLED=true
REMOTE_RECHECK_INTERVAL_SECONDS=604800
REMOTE_RECHECK_POLL_SECONDS=60
REMOTE_RECHECK_BATCH_SIZE=10
REMOTE_RECHECK_RESERVATION_SECONDS=3600
```

数据库中的 next_recheck_at 用于投递预约；中断后预约到期可补投，Redis 队列合并重复活动任务。
同一作品的小说/插画身份分别调度。现有 Redis 全局请求间隔及限流冷却继续生效。

```text
GET /api/ops/remote-rechecks
GET /api/jobs?status=queued
GET /api/jobs?status=retry_wait
```

## 从本地动图补生成 MP4

原始 ZIP 和 ugoira_meta.json 是保留的来源材料。独立 media worker 从这两份本地文件重新生成
preview.mp4，核对数据库登记的大小、SHA-256、帧顺序和目录后才执行；全过程不访问 Pixiv。

```text
POST /api/media-jobs/ugoira/123
POST /api/media-jobs/ugoira/123?version_no=1
GET  /api/media-jobs
GET  /api/media-jobs/stats
GET  /api/media-jobs/{job_id}
POST /api/media-jobs/{job_id}/retry
```

省略 version_no 时，在提交任务时固定当前版本。重复活动任务按作品和版本合并。
正常且已登记的 MP4 会直接复用。生成阶段在临时目录进行，成功后原子发布并登记历史资产；
若补的是当前版本，也更新当前文件索引。旧版本补生成不会改变当前版本指针。
任务显示 validating/converting/registering/complete 阶段；临时失败自动重试，终态失败可手动 retry。
成功提交响应丢失后仍可幂等恢复，不重复插入文件记录。

```env
MEDIA_JOB_PREFIX=pixiv:media
MEDIA_JOB_MAX_ATTEMPTS=3
MEDIA_JOB_RETRY_BASE_SECONDS=30
MEDIA_JOB_RETRY_MAX_SECONDS=1800
UGOIRA_CONVERSION_TIMEOUT_SECONDS=600
```

Docker 镜像已包含 FFmpeg；直接运行 Python 时需安装 FFmpeg 并配置 FFMPEG_BINARY。
这版提供按作品/版本补生成入口，没有自动扫全库批量转码，也没有增加 GIF/WebP 导出或大文件字节续传。
已有全量扫描断点续跑仍可用。

## 备份与恢复

新备份格式为 v2。SQL dump 不写 CREATE DATABASE/USE，恢复时明确指定配置中的目标数据库。
MAINTENANCE_DATABASE_URL 可以使用具有备份权限的账号，但 host、port、database 必须和应用连接一致。
备份中的旧库名仅作来源记录，不会把新格式恢复重定向到旧数据库。
旧格式 v1 在 schema 相同的前提下可校验；恢复只允许同库，禁止跨库误恢复。

同步、归档、账号迁移、接口写入、完整性修复和 MP4 生成通过共享写入登记与维护冻结协调：
冻结先阻止新写入，再等待已有写入结束，随后生成数据库和文件备份。
锁丢失时停止发布有效备份；取消时等待正在运行的后台文件操作/数据库子进程结束，避免提前恢复写入。

```bash
python -m app.maintenance.backup create --name manual-v110
python -m app.maintenance.backup verify /path/to/backup
python -m app.maintenance.backup restore /path/to/backup --confirm-destructive-restore
```

恢复会替换目标数据库和归档目录。开始替换前保留数据库安全副本、旧目录和 restore-state.json。
恢复失败会尝试回滚；回滚失败或锁状态不确定时保留恢复资料，并保持写入阻断，错误信息会给出具体路径。
确认数据库和媒体已经恢复一致后，才执行：

```bash
python -m app.maintenance.backup release-recovery --confirm-recovery-complete
```

这个命令解除阻断并检查 schema，不能代替人工修复。不要直接删除 Redis 中的恢复阻断键。
备份不包含 Redis 队列和登录凭据；恢复、换机器时应按部署情况处理任务队列并重新配置凭据。

## 验证范围

本次回归使用 SQLite、模拟 Redis/远端、临时目录和模拟数据库子进程，验证身份隔离、事务失败恢复、
备份门禁、目标库选择、旧作任务投递及本地补生成链路。真实 MySQL 迁移和账号线上操作仍需在部署环境验证。
