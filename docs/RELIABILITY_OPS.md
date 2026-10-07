# Archive retry, DLQ and operations

## 目标

同步源的 frontier 一旦前进，已发现作品就不能因为一次临时网络错误而静默
丢失。v1.6 因此把 archive job 从“一次失败即结束”改为持久的重试状态机。

## 自动重试

默认最多执行 5 次（包含第一次执行）：

```text
第 1 次失败 -> 30 秒
第 2 次失败 -> 60 秒
第 3 次失败 -> 120 秒
第 4 次失败 -> 240 秒
第 5 次仍失败 -> dead
```

实际等待时间由以下环境变量控制：

```env
ARCHIVE_JOB_MAX_ATTEMPTS=5
ARCHIVE_JOB_RETRY_BASE_SECONDS=30
ARCHIVE_JOB_RETRY_MAX_SECONDS=1800
```

退避任务存放在 Redis Sorted Set 中。到期后 archive worker 使用原子 Lua
操作重新投递到主 Stream，多 worker 不会重复提升同一个 retry job。

## 哪些错误会自动重试

会重试：

- Pixiv 临时不可用、限流或上游未知 502 类错误
- HTTP 408 / 425 / 429 / 5xx
- HTTP 连接、读取、超时错误
- 维护冻结刚好与写操作竞争
- MySQL 临时断连 / OperationalError
- Redis 作品锁续租丢失后抛出的临时不可用错误

不会自动重试：

- Pixiv 作品已删除 / 404
- 私密、限制访问 / 403
- 自动认证恢复后仍然认证失败
- Ugoira ZIP / 元数据格式错误
- 本地磁盘空间不足
- 文件权限错误
- 明确的参数或数据校验错误

这样可以避免把永久错误反复请求 Pixiv 五轮。

## Dead Letter Queue

不可重试错误或达到最大尝试次数后：

```text
running -> dead
```

任务 Hash 保留最终错误、尝试次数、来源 ID 和 `dead_reason`，同时向有上限
的 Redis DLQ Stream 写入一条事件。DLQ 用于运维追踪，不作为第二套执行队列。

查询：

```http
GET /api/jobs?status=dead
GET /api/ops/jobs?status=dead
```

手工重放：

```http
POST /api/jobs/{job_id}/replay
POST /api/ops/jobs/{job_id}/replay
```

重放会创建新的 job_id，旧 dead job 不会被篡改。

## 运维状态

```http
GET /api/ops/status
```

返回：

- MySQL / Redis / schema 状态
- archive / sync / maintenance heartbeat 和 TTL
- archive Stream 长度与 pending 数
- retry_wait 数量
- dead job / DLQ 事件统计
- sync Stream 长度与 pending 数
- maintenance write freeze
- storage 总量、已用、剩余与最低安全余量

所有 `/api/ops/*` 仍受全局 `X-API-Key` 保护。
