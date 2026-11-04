> 当前程序为 v1.10 / schema v9，升级步骤以 [v1.10 说明](V1_10_RELIABILITY.md)为准。下文保留 v1.9 功能说明。

# 私密收藏、合集追更与请求限速（v1.9 / schema v8）

## 升级

程序运行时仍不创建表或执行迁移。

- 全新数据库：执行 `sql/pixiv_archive.sql`，得到 schema v8。
- 已有 schema v7：备份后停止 api、worker、sync、maintenance，选择正确数据库，手工执行
  `sql/upgrades/v7_to_v8.sql`，确认 `SELECT version FROM schema_version WHERE id=1` 返回 8 后启动新版。
- 已有 schema v6：先执行 `v6_to_v7.sql`，再执行 `v7_to_v8.sql`。
- v1–v5 不可直接使用这个升级脚本，也不要只修改版本号。

v8 新增同步源的账号绑定字段和三张收藏迁移进度表；保留现有作品和历史版本。
实际 `.env` 中已有的数值会覆盖新默认值，请按下面的配置说明调整。升级脚本未由开发工具在用户数据库执行。

## 获取未保存作品时先收藏

下列人工请求默认开启 `bookmark_if_missing=true`：

```text
GET  /api/novel/{作品ID}
GET  /api/illust/{作品ID}
POST /api/jobs/archive/{novel|illust|manga|ugoira}/{作品ID}
POST /api/jobs/archive/series/{novel|manga}/{合集ID}
```

漫画、普通插画和壁纸都使用插画接口；没有独立的 wallpaper 作品类型。

作品锁内确认本地尚无归档后，先读取 Pixiv 收藏状态：

1. 已收藏：保留原公开性与收藏标签，不重复写入。
2. 未收藏：默认添加为**私密收藏**，成功后才获取小说正文或下载媒体。
3. 收藏状态无法确认、收藏写入失败或被限流：本次下载失败/等待重试，不伪报成功。
4. 已有本地归档：不新增收藏；是否检查内容更新仍由 refresh 和 TTL 决定。

如果收藏已成功、后续文件下载失败，Pixiv 收藏会保留。重试会识别已有收藏，不覆盖其设置。
任务完成后的本地缓存命中不会再次收藏。

自动作者同步、收藏同步和合集订阅**不会**把扫描到的所有作品自动加入 Pixiv 收藏。
人工整套下载会对尚未保存的每个成员执行同样的收藏规则。

所有接口仍要求 `X-API-Key`。上述 GET 可能触发远端收藏写操作；只读取本地内容可用
`/api/library/works/{ID}`，或已缓存作品的 `refresh=false`。`refresh=false` 在缓存缺失时仍会归档。
可对单次人工请求传 `bookmark_if_missing=false`，或使用全局开关关闭自动收藏。

```env
PIXIV_AUTO_BOOKMARK_ON_API_DOWNLOAD=true
PIXIV_AUTO_BOOKMARK_RESTRICT=private
```

App 收藏写入使用 `PIXIV_REFRESH_TOKEN` 所属账号，不能通过 API 参数指定其他账号。

## 收藏作品并展开合集

```text
POST /api/sync/sources/bookmarks/self?restrict=public&expand_series=true&interval_seconds=21600
POST /api/sync/sources/bookmarks/self?restrict=private&expand_series=true&interval_seconds=21600
```

公开和私密收藏是两个同步源。`expand_series` 默认 false，避免已有收藏同步突然下载整套作品。
开启后，从新发现的小说/漫画成员识别合集，归档合集并建立持续合集订阅。
修改该开关或内容类型会安排全量重扫，重新发现既有收藏中的合集，即使 run_now=false；
若已有任务运行则在它结束后重扫。停用期间不启动重扫，重新启用时安排。
普通插画不会被当作合集展开。手动运行停用的源会返回 409，请先启用。

## 订阅指定合集

```text
POST /api/sync/sources/series/novel/{合集ID}?interval_seconds=21600&run_now=true
POST /api/sync/sources/series/manga/{合集ID}?interval_seconds=21600&run_now=true
```

这是持续订阅；`/api/jobs/archive/series/...` 仍是人工单次归档。
首次和手动全量扫描对所有成员检查远端更新；后续默认每六小时发现新增成员，
新增或超过复查 TTL 的成员入队，近期验证过的本地作品跳过。

## 导入 Pixiv 追更合集清单

```text
POST /api/sync/sources/followed-series?include_novel=true&include_manga=true&run_now=true
```

需要 `PIXIV_COOKIE`。使用 **Cookie 所属账号**的追更清单；请让 Cookie 和 App refresh token
对应同一个账号，否则清单发现与内容访问权限可能不同。缺少/过期 Cookie 返回错误。

扫描小说与漫画追更清单，为发现的合集建立独立的合集订阅；已有手动停用的订阅不会自动重新启用。
独立合集订阅可在 `/api/sync/sources` 中查看，使用 PATCH 停用或 DELETE 删除。
停用清单源只停止发现新的追更合集；已建立的独立合集订阅需分别停用。
取消 Pixiv 追更不会自动删除本地文件或独立订阅。

该功能使用非官方 Web AJAX `/ajax/watch_list/{novel|manga}?p=N&new=1`，校验页数、总量和重复 ID；
未知或不完整响应会失败，不会把截断数据当作完整清单。已用模拟响应验证，尚未用真实 Cookie 实测。
实现参考以下维护项目，参数可能随 Pixiv 调整：

- [FollowApi.kt](https://github.com/sunbeams001/ProjectU/blob/master/shared/src/commonMain/kotlin/com/projectu/shared/data/remote/api/FollowApi.kt)
- [WatchListDto.kt](https://github.com/sunbeams001/ProjectU/blob/master/shared/src/commonMain/kotlin/com/projectu/shared/data/remote/dto/follow/WatchListDto.kt)

小说收藏写入端点参考 [pixiv.py](https://github.com/tsubasa/pixiv.py/blob/master/pixiv/api.py)，
封装 `/v2/novel/bookmark/add`，兼容 PixivPy 3.7.5 未提供该便利方法的情况。

## 请求限速与断点续传

```env
PIXIV_REQUEST_INTERVAL_SECONDS=2
PIXIV_RATE_LIMIT_COOLDOWN_SECONDS=300
PIXIV_RATE_LIMIT_PREFIX=pixiv:rate
REMOTE_CHECK_TTL_SECONDS=21600
SYNC_MAX_PAGES_PER_RUN=20
SYNC_PAGE_DELAY_SECONDS=2
SYNC_RESUME_DELAY_SECONDS=30
SYNC_WORK_RECHECK_TTL_SECONDS=86400
```

- API、sync、archive 进程按同一凭据摘要共用 Redis 时间槽；App、Web 与媒体请求均经过节流。
- 默认两次请求起始时间至少间隔两秒。这是本项目保守默认值，不是 Pixiv 官方安全额度。
  长请求可能重叠，节流不是全局只允许一个在途请求。
- HTTP 429 或可识别的 App 限流错误设置共享冷却；采用默认冷却与 Retry-After 中的较大值。
  Redis 不可用时拒绝继续不受控地请求。设置请求间隔为 0 会显式关闭节流，只用于离线测试。
- 作者/收藏扫描每轮共处理最多 20 页，逐页保存游标；预算用尽后默认 30 秒续跑。
  只有所有类型扫描完成才更新正式增量边界。中断后恢复，可能重读最后未提交的一页，队列去重处理重复项。
- 首次扫描及 `full=true` 会对所有发现的作品强制检查远端，恢复分页时保留这个模式。
  已有作品按内容指纹比较；未变就复用文件并更新元数据，有变化保存新版本，缺失则归档。
  后续增量扫描仍使用 TTL。强制检查遇到临时错误，即使旧缓存可读，也会重试而不误报成功。
  运行中的增量任务收到全量要求后，会在结束或失败后追加全量任务。
- 合集和追更清单发现仍使用 `SERIES_MAX_PAGES` / `SERIES_MAX_WORKS` 完整性上限，
  不使用作者/收藏分页 checkpoint；超过上限会报错。两类请求同样经过统一限速。

普通阅读优先使用 `/api/library`。`REMOTE_CHECK_TTL_SECONDS` 约束普通缓存读取；
明确指定 force_refresh 的人工任务仍可绕过该 TTL。

## 进度

```text
GET /api/sync/sources
GET /api/sync/sources/{source_id}
GET /api/sync/jobs/{job_id}
GET /api/jobs?status=retry_wait
GET /api/ops/status
```

同步发现完成不等于媒体全部下载完成。分页未结束时结果 `complete=false`，源中的 `scan_state`
保留下一页游标；作品归档进度在 archive jobs 和本地 library 中查看。

新队列支持崩溃后恢复 finalizing；取消或状态持久化失败不会提前 ACK。
未入库的遗留版本目录先在作品锁内核对数据库，再移至 `.quarantine`，不会直接覆盖旧目录。

## 切换账号后补收藏与追更

默认 `ACCOUNT_MIGRATION_ENABLED=true`。`sync` 进程首次启动时验证当前账号，
为该账号建立持久化迁移任务：本地插画、壁纸、漫画、Ugoira 和小说添加为**私密收藏**；
本地小说/漫画合集以及已配置的合集订阅添加到 Pixiv **追更**。
已收藏、已追更的项目不重复写入，保留原收藏可见性和标签。
这是提交作品/合集 ID 的收藏关系，不会上传本地文件，也不会迁移作者关注关系。

切换步骤：

1. 停止所有 api/worker/sync/maintenance 服务，保留数据库、Redis 和 data 文件。
2. 修改 `.env` 中新账号的 `PIXIV_REFRESH_TOKEN` 和同账号的 `PIXIV_COOKIE`。
3. 重新创建服务以加载环境：`docker compose up -d --build --force-recreate api worker sync maintenance media`。
   仅 `docker compose restart` 不保证重新加载 env_file；认证 refresh 接口也不是跨进程账号切换接口。
4. `GET /api/account/identity` 检查身份，再查看迁移进度。请求都需要 `X-API-Key`。

App Token 用于作品收藏；合集追更还需要 Cookie 和网页 CSRF。
两种凭据必须属于同一账号；缺失/过期或不匹配时相应迁移暂停，修正凭据并重新创建服务后继续。
旧账号的任务保留但不在新账号下执行。`self` 收藏/追更扫描也绑定账号，换号后重置发现游标，
保留本地作品、历史版本及订阅。显式指定用户 ID 的同步源仍指向该用户。

迁移按本地清单分页，每批默认处理 10 个项目，逐项提交成功状态；全部完成后停止远端轮询。
进程中断后，已完成项目跳过；上次远端成功但数据库尚未确认的项目会先查询是否已收藏，再决定是否写入。
重新激活同一账号会补扫本地新增项目并保留旧成功记录。手动创建新任务可重新核对整库。
扫描期间新增且排序落在游标之前的作品可能不进入本次清单，可通过手动新任务补扫。

临时错误指数退避重试，默认最多 5 次；已删除/无权限的作品单独记为 skipped，继续其他项目。
completed 表示任务没有待处理项目，**不代表全部成功**，请同时查看 failed/skipped 数量。
有限次重试用尽后进入 failed，凭据问题进入 paused；成功项目不会因为其他项目失败而回滚。

```text
GET  /api/account/identity
GET  /api/account/migrations
GET  /api/account/migrations/{run_id}
GET  /api/account/migrations/{run_id}/items?status=failed
GET  /api/account/migrations/{run_id}/items?status=skipped
POST /api/account/migrations
POST /api/account/migrations/{run_id}/retry
```

POST 创建任务不接受任意目标账号，只使用服务端当前已验证账号。retry 重试该账号任务中的 failed 项，
不重新提交成功项；如需重新检查 skipped 项，创建新的迁移任务。需运行 sync worker，且启用迁移开关。

```env
ACCOUNT_MIGRATION_ENABLED=true
ACCOUNT_MIGRATION_BATCH_SIZE=10
ACCOUNT_MIGRATION_SCAN_PAGE_SIZE=200
ACCOUNT_MIGRATION_TICK_SECONDS=30
ACCOUNT_MIGRATION_MAX_ATTEMPTS=5
ACCOUNT_MIGRATION_RETRY_BASE_SECONDS=60
ACCOUNT_MIGRATION_RETRY_MAX_SECONDS=3600
ACCOUNT_MIGRATION_LOCK_TTL_SECONDS=180
```

每个远端查询及写入沿用前述 Redis 共享限速和冷却，设置批大小不会绕过请求间隔。
这些措施降低突发请求，不能保证 Pixiv 永不限流；遇到限流应保留冷却并降低频率。
未使用真实账号批量收藏或真实 MySQL 执行迁移，相关回归用模拟远端和 SQLite 验证。
网页接口为非官方接口，Pixiv 改版时可能需要调整；实现参考
[NovelSeriesApi](https://github.com/sunbeams001/ProjectU/blob/master/shared/src/commonMain/kotlin/com/projectu/shared/data/remote/api/NovelSeriesApi.kt)、
[IllustSeriesApi](https://github.com/sunbeams001/ProjectU/blob/master/shared/src/commonMain/kotlin/com/projectu/shared/data/remote/api/IllustSeriesApi.kt)。
