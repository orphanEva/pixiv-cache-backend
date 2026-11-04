"""One bounded, authenticated HTTP client shared by all MCP tools."""
from __future__ import annotations

import json as json_module
import re
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.config import MCPSettings


_SAFE_PATH = re.compile(r"/api/[A-Za-z0-9_/-]+\Z")
_SECRET_KEYS = {"apikey", "authorization", "cookie", "setcookie", "accesstoken",
                "refreshtoken", "pixivcookie", "pixivrefreshtoken", "password"}


class BackendClient:
    """Only domain tools select routes; callers cannot supply URLs or credentials."""

    def __init__(self, settings: MCPSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> BackendClient:
        if self._client is not None:
            raise RuntimeError("MCP backend client is already running")
        self._client = httpx.AsyncClient(
            base_url=self.settings.backend_url + "/",
            headers={"X-API-Key": self.settings.api_key.get_secret_value(),
                     "Accept": "application/json", "User-Agent": "pixiv-cache-mcp/1.11.0"},
            timeout=httpx.Timeout(self.settings.timeout_seconds, connect=min(10, self.settings.timeout_seconds)),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=False, trust_env=False, transport=self._transport,
        )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("[redacted]" if re.sub(r"[^a-z]", "", key.lower()) in _SECRET_KEYS
                          else self._redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return value.replace(self.settings.api_key.get_secret_value(), "[redacted]")
        return value

    @staticmethod
    def _status_error(response: httpx.Response) -> ToolError:
        status = response.status_code
        messages = {
            400: "请求参数不符合后端要求。请检查参数。",
            401: "后端 API Key 验证失败，请检查 PIXIV_MCP_API_KEY。",
            403: "后端拒绝访问，请检查后端账号权限。",
            404: "未找到记录；本地查询不会自动下载，请按需调用归档工具。",
            409: "操作与当前状态冲突，请查询任务、同步源或账号状态。",
            422: "请求参数或远端数据未通过后端校验。",
            429: "请求已限速，请等待后重试，避免连续调用。",
            503: "后端暂不可用，可能正在维护、等待依赖或远端冷却。请稍后查询运行状态。",
        }
        message = messages.get(status, "后端请求失败，请查询运行状态或后端日志。")
        if 300 <= status < 400:
            message = "后端返回重定向，已停止请求；请配置最终后端地址。"
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 6:
            message += f" 建议等待 {int(retry_after)} 秒。"
        if response.request.method != "GET" and status >= 500:
            message += " 操作可能已经入队，请先查询任务状态再决定是否重试。"
        return ToolError(f"HTTP {status}: {message}")

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None) -> dict[str, Any]:
        method = method.upper()
        if (method not in {"GET", "POST", "PATCH"} or not _SAFE_PATH.fullmatch(path)
                or "//" in path or any(part in {".", ".."} for part in path.split("/"))):
            raise ToolError("不支持的后端操作。")
        if self.settings.read_only and method != "GET":
            raise ToolError("当前 MCP 为只读模式，无法提交变更。")
        if self._client is None:
            raise ToolError("MCP 后端连接尚未启动。")
        query = {key: value for key, value in (params or {}).items() if value is not None}

        def response_error(message: str) -> ToolError:
            if method != "GET":
                message += " 后端可能已经接受操作，请先查询任务或同步源状态再决定是否重试。"
            return ToolError(message)

        try:
            async with self._client.stream(method, path.lstrip("/"), params=query, json=json) as response:
                if not 200 <= response.status_code < 300:
                    raise self._status_error(response)
                if response.status_code == 204:
                    return {"ok": True}
                chunks = bytearray()
                # aiter_bytes counts decoded bytes, including compressed responses.
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > self.settings.max_response_bytes:
                        raise response_error("后端响应超过 MCP 大小上限。请缩小查询范围，或调整 PIXIV_MCP_MAX_RESPONSE_BYTES。")
                    chunks.extend(chunk)
                try:
                    payload = json_module.loads(chunks)
                except (ValueError, UnicodeError, RecursionError):
                    raise response_error("后端未返回有效 JSON，请检查后端地址和版本。") from None
                if not isinstance(payload, dict):
                    raise response_error("后端响应结构不符合预期，请确认后端为 v1.10 或更新版本。")
                try:
                    return self._redact(payload)
                except RecursionError:
                    raise response_error("后端 JSON 嵌套过深。") from None
        except httpx.TimeoutException:
            message = "后端请求超时。"
            if method != "GET":
                message += " 操作可能已被接受，请先查询任务或同步源状态，避免重复提交。"
            raise ToolError(message) from None
        except httpx.RequestError:
            message = "后端连接异常，请检查 PIXIV_MCP_BACKEND_URL 和后端运行状态。"
            if method != "GET":
                message += " 操作结果尚未确认，请先查询任务或同步源状态再重试。"
            raise ToolError(message) from None
