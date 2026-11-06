# MCP 接入

## 默认安全边界：仅查询本地归档

`PIXIV_MCP_LIBRARY_ONLY=true` 现在默认开启，只注册 library 查询工具，HTTP 客户端也只放行明确列出的 `/api/library/` GET 路由。启动参数 `--library-only` 可强制此模式，覆盖环境变量。缺失内容不会自动下载，不注册归档、同步、账号验证或任务操作。启动适配器不会启动后端/worker，也不会恢复已暂停的 Pixiv 同步。此模式不能停止在其他进程中已经运行的 worker。

这是刻意比旧 `--read-only` 更严格的模式：旧只读模式仍允许 `account_identity` 访问 Pixiv，`list_jobs` 后端还可能清理 Redis 索引。因此仅 `readOnlyHint` 或仅禁止 POST 不能保证离线无副作用。需要外部请求或写操作时，必须明确授权后设置 `PIXIV_MCP_LIBRARY_ONLY=false`；若同时设置 `PIXIV_MCP_READ_ONLY=true`，仍仅隐藏写工具，但不保证不访问上游。不要在 Pixiv 自动同步暂停期间启用这些操作。

以下完整工具目录及写入工作流仅在显式关闭本地库模式后适用。默认模式可直接查询已有后端，而非导出快照；要求后端 API 地址和已有 API Key，但不需要机器人访问数据库、Redis 或 Pixiv 凭据。


v1.11 提供基于[官方 Python MCP SDK 2.x](https://py.sdk.modelcontextprotocol.io/) 的 **stdio 适配器**，依赖范围为 `mcp>=2.3,<3`。MCP 客户端启动适配器进程，通过标准输入、输出调用工具；适配器使用 HTTP API 访问现有后端。它不直接连接 MySQL、Redis 或 Pixiv，也不启动 HTTP 监听端口。

后端需为 **v1.10 或更新版本，数据库 schema v9**。单独给已有后端增加 MCP 适配器不需要新表；但若已通过前次本地 v1.10 的旧脚本升级到 schema v9，本次合并远端小说追更功能后，须先手工执行 `sql/upgrades/v7_v9_sync_sources_compat.sql` 补齐兼容枚举，版本号保持 9。部署更旧后端时，先按[数据库指南](MANUAL_DATABASE_SETUP.md)完成原有升级。

## 本机安装与客户端配置

需要 Python 3.12 或更新版本。在项目根目录安装可选的 MCP 依赖和命令入口：

```powershell
Set-Location E:\Python\pixiv-cache-backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[mcp]'
```

安装后，将 [examples/mcp-client.json](../examples/mcp-client.json) 的内容合并到支持 stdio 的 MCP 客户端配置中。修改 `command` 为实际安装环境中的 `pixiv-cache-mcp` 可执行文件绝对路径，并把示例密钥替换为后端现有的 `API_KEY`。

示例使用 Windows 路径；Linux/macOS 虚拟环境的入口通常是 `/绝对路径/.venv/bin/pixiv-cache-mcp`。安装后的入口不依赖客户端的工作目录。

也可用 Python 模块入口：

```text
python -m app.mcp
```

直接从源码运行而未安装项目时，需要在仓库根目录执行，并在该 Python 环境安装 MCP 依赖。安装了项目后，可使用对应虚拟环境的 Python 从其他目录运行模块入口。

启动 stdio 服务后等待客户端输入属于正常行为；不要向它手动输入聊天文本，也不要把普通日志重定向到标准输出。客户端应接管 stdin/stdout，诊断日志使用 stderr。

入口还支持 `--version` 和 `--help`；`--read-only` 可在启动时强制开启只读模式。

## 环境变量

适配器**不会自动读取项目的 `.env`**。在 MCP 客户端的 `env` 字段或启动进程的环境中显式设置以下变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PIXIV_MCP_BACKEND_URL` | `http://127.0.0.1:18081` | 后端 API 地址；不是 Pixiv 地址 |
| `PIXIV_MCP_API_KEY` | 必填 | 至少 32 个字符，必须与后端 `API_KEY` 一致；通过 `X-API-Key` 发送 |
| `PIXIV_MCP_TIMEOUT_SECONDS` | `60` | 单次后端 HTTP 请求的超时秒数 |
| `PIXIV_MCP_LIBRARY_ONLY` | `true` | 默认仅本地 library 查询，禁止账号远端校验、任务及同步工具 |
| `PIXIV_MCP_READ_ONLY` | `false` | 设为 `true` 时隐藏写工具，同时阻止适配器发送非 GET 请求 |
| `PIXIV_MCP_MAX_RESPONSE_BYTES` | `8388608` | 单次后端响应大小上限，默认 8 MiB |

密钥只配置在本机客户端或进程环境中。不要把替换了真实密钥的客户端配置提交到版本库。MCP 进程不需要 `PIXIV_REFRESH_TOKEN`、Cookie、MySQL 或 Redis 凭据，这些仍由后端配置管理。

本地默认端口来自现有 Compose 配置：宿主机 `127.0.0.1:18081` 映射到 `api` 容器的 `8000`。如果后端运行在另一台机器，填写实际可访问的后端地址。

## 容器方式

[Dockerfile.mcp](../Dockerfile.mcp) 使用独立的 `requirements-mcp.txt`，仅安装 MCP、HTTP 客户端和配置依赖，不安装后端的数据库驱动、PixivPy 或 FFmpeg。专用的 `Dockerfile.mcp.dockerignore` 只允许适配器源码与依赖清单进入构建上下文，排除 `.env`、归档媒体和虚拟环境。适配器通过现有 Compose 项目网络访问 `http://api:8000`。

先确保后端及需要的 worker 已按正常方式部署。构建适配器镜像：

```powershell
Set-Location E:\Python\pixiv-cache-backend
docker build -f Dockerfile.mcp -t pixiv-cache-mcp:local .
```

将以下命令交给 MCP 客户端启动，并由客户端环境提供 `PIXIV_MCP_API_KEY`：

```text
docker compose -f E:\Python\pixiv-cache-backend\docker-compose.yml -f E:\Python\pixiv-cache-backend\docker-compose.mcp.yml run --rm -T --no-deps mcp
```

对应的通用客户端配置示例：

```json
{
  "mcpServers": {
    "pixiv-cache": {
      "command": "docker",
      "args": [
        "compose",
        "-f", "E:\\Python\\pixiv-cache-backend\\docker-compose.yml",
        "-f", "E:\\Python\\pixiv-cache-backend\\docker-compose.mcp.yml",
        "run", "--rm", "-T", "--no-deps", "mcp"
      ],
      "env": {
        "PIXIV_MCP_BACKEND_URL": "http://api:8000",
        "PIXIV_MCP_API_KEY": "REPLACE_WITH_YOUR_32_PLUS_CHARACTER_BACKEND_API_KEY",
        "PIXIV_MCP_LIBRARY_ONLY": "true",
        "PIXIV_MCP_READ_ONLY": "true"
      }
    }
  }
}
```

使用与现有后端相同的 Compose 项目名和 Compose 文件路径。若后端通过 `docker compose -p 自定义项目名 ...` 启动，在上述客户端 `args` 的 `compose` 后也加入 `"-p", "自定义项目名"`，才能加入同一个网络。`--no-deps` 不会启动或重启后端服务。

容器中的 `127.0.0.1` 指向适配器自己，因此本方案必须使用 `http://api:8000`。MCP 服务没有端口映射、后端数据目录挂载或 `env_file`。Compose 本身仍按原有规则处理后端的配置文件和变量；MCP 密钥通过上面的客户端环境显式提供。

`stdin_open: true` 和 `tty: false` 保留 MCP 协议管道；`-T` 禁用终端模拟。该服务使用可选 `mcp` profile，直接指定服务名的 `compose run` 会启动它。不要用 `compose up -d mcp` 代替客户端启动：stdio 会话必须连接客户端的输入、输出。

## 工具目录

工具名不带 `pixiv_` 前缀。客户端可读取静态资源 `pixiv://guide` 获取使用说明。

| 用途 | 工具 |
|---|---|
| 查询本地作品 | `search_library`、`get_library_work` |
| 版本与小说片段 | `get_library_history`、`get_library_version`、`read_novel_excerpt` |
| 文件完整性 | `check_library_integrity` |
| 作者、标签 | `list_library_authors`、`get_library_author`、`list_library_tags` |
| 本地合集与统计 | `list_library_series`、`get_library_series`、`get_library_stats` |
| 提交归档 | `archive_work`、`archive_series`、`series_archive_status` |
| 查询、重试任务 | `list_jobs`、`get_job`、`retry_job` |
| 同步源查询 | `list_sync_sources`、`get_sync_source` |
| 建立同步源 | `upsert_author_source`、`upsert_bookmark_source`、`upsert_series_source`、`upsert_followed_series_source` |
| 修改、执行同步 | `patch_sync_source`、`run_sync_source` |
| 当前账号 | `account_identity` |
| 账号收藏迁移 | `create_account_migration`、`list_account_migrations`、`get_account_migration`、`list_account_migration_items`、`retry_account_migration` |
| 从本地动图补 MP4 | `generate_ugoira_preview` |
| 运维状态 | `ops_status` |

小说和插画可以有相同的数字 ID。查询具体作品、历史、版本或完整性时，必须指定 `kind=novel` 或 `kind=illust`；`illust` 命名空间包含漫画和 Ugoira。系列则区分 `novel` 与 `manga`。

`get_job`、`retry_job` 的 `queue` 可选 `archive`、`sync`、`media`；`list_jobs` 仅支持 `archive` 和 `media`。`ops_status` 的 `view` 可选 `status`、`remote_rechecks`、`media_queue`。具体参数、枚举与分页限制以客户端列出的工具 schema 为准。

搜索列表每页最多 50 条，小说片段最多 12,000 个字符。历史列表、文件列表和小说正文目前由后端完整返回，再由适配器分页或截取；它们仍受完整 HTTP 响应大小上限约束。特别大的小说或历史记录需要调整 `PIXIV_MCP_MAX_RESPONSE_BYTES`，仅缩短片段不会减少这次后端传输量。

## 可以直接说的工作流

**查资料与读片段：**“查本地带原神标签的小说，先给我十条标题和作者；然后读取小说 123 的一小段正文。”客户端先调用 `search_library`，再用 `read_novel_excerpt`。需要旧版时先查 `get_library_history`，明确版本后读取；大列表应分页，正文应分段。

**下载指定作品或整套合集：**“把插画 123 加入归档，允许给尚未保存且未收藏的作品新增收藏，并展开它的系列；提交后告诉我任务进度。”使用 `archive_work`；已知合集 ID 时使用 `archive_series`。收藏与系列展开会影响处理范围，应明确自己的意图。现有收藏的公开性和标签由后端保留；新增收藏的默认方式受后端配置控制。

**持续同步：**“订阅作者 123，每六小时同步作品；不要把作者所有作品收藏到我的账号。”使用 `upsert_author_source`，需要立即执行时调用 `run_sync_source`。作者同步和手工下载的收藏行为不同：自动作者扫描不会隐式开启整库收藏。收藏同步、指定合集与账号追更清单分别使用对应的 `upsert_*_source` 工具。

**迁移收藏到新账号：**“确认当前账号后，把整个本地作品库的收藏和系列追更补到这个账号，给我迁移进度和失败项。”先用 `account_identity` 确认身份，再明确调用 `create_account_migration`。这会对当前 Pixiv 账号执行写操作；使用迁移查询工具查看进度，必要时 `retry_account_migration` 重试失败项。账号或认证发生变化时，后端会暂停迁移。

**补动图预览：**“为本地 Ugoira 123 的第 2 个历史版本生成 MP4，不重新从 Pixiv 下载。”调用 `generate_ugoira_preview`，再用 `get_job(queue="media", ...)` 查询。转换依赖已经归档的 ZIP 和帧时序，生成的是预览，原始 ZIP 仍保留。

**查积压与限流：**“查看 worker、队列和远端冷却状态，列出失败任务，先不要批量重试。”使用 `ops_status` 与 `list_jobs`；选定任务后再调用 `retry_job`。

## 后台服务与任务完成条件

| 操作 | 需要的后端进程 |
|---|---|
| 本地库、历史、统计、状态查询 | `api`，以及正常运行的数据库/Redis |
| 新归档、刷新、整系列下载 | `api` + `worker` |
| 作者、收藏、合集、追更清单同步 | `api` + `sync`；下载还需要 `worker` |
| 账号收藏迁移 | `api` + `sync` |
| 本地 Ugoira MP4 生成 | `api` + `media`，其镜像内需有 FFmpeg |
| 定时完整性检查 | `maintenance` |

任务接口返回 `queued` 或任务 ID，只表示已接受请求。应继续查询对应任务；归档与媒体任务成功状态为 `succeeded`。合集请求还需检查 `series_archive_status` 的成员进度，不能把提交数量当作全部下载成功。同步发现结束也不代表其派发的下载任务已经完成。账号迁移的 `completed` 应结合成功、跳过和失败计数判断结果。

MCP 的 HTTP 超时只限制提交或查询请求，不代表后台任务在该时限内完成。超时后先查询已有任务或同步状态，避免反复提交同一批操作。

后端仍执行全局请求间隔、限流冷却和任务退避。限速用于降低请求压力，**不保证无限请求、不被风控或不被封禁**；遇到冷却、认证失败或权限限制，应查看状态并处理原因。

## 排查连接问题

- **启动时配置错误：**检查客户端环境中的 API 密钥长度与变量名。适配器不会自动从后端 `.env` 补齐配置。
- **HTTP 401/403：**检查 MCP 使用的密钥是否与后端 `API_KEY` 相同，并区分后端 API 鉴权失败与 Pixiv 账号权限错误。
- **无法连接：**本机进程使用可访问的宿主地址；同一 Compose 网络的适配器使用 `http://api:8000`。检查后端是否已经运行。
- **有任务号但一直排队：**用 `ops_status` 查看队列和冷却状态；默认状态包含归档、同步、维护 worker 的心跳，媒体队列需使用 `view="media_queue"`，媒体 worker 健康检查见 Compose。stdio 适配器本身不执行下载或转码。
- **找不到写工具：**默认 `PIXIV_MCP_LIBRARY_ONLY=true` 有意隐藏全部非 library 工具。仅在明确允许远端请求/写入时才关闭它；`PIXIV_MCP_READ_ONLY=true` 还会隐藏写工具。更改配置后需要重启 MCP 会话。
- **响应过大：**搜索接口可缩小分页；完整历史、文件列表或小说正文超过 HTTP 响应上限时，需调整上限，或先查询更小的记录。
- **媒体链接无法直接在聊天界面打开：**受保护媒体同样需要后端鉴权。工具返回的文件地址不等于匿名公开下载链接，不能把 API 密钥拼进 URL。

后端完整部署及功能说明见 [README](../README.md)、[同步源说明](SOURCE_SYNC.md) 和 [v1.10 可靠性说明](V1_10_RELIABILITY.md)。

