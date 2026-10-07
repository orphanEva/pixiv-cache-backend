from __future__ import annotations

import httpx
from sqlalchemy.exc import DisconnectionError, OperationalError


class PixivRemoteError(Exception):
    """Pixiv 远端请求的基础异常。"""

    status_code = 502


class PixivNotFoundError(PixivRemoteError):
    """Pixiv 作品不存在或已删除。"""

    status_code = 404


class PixivRestrictedError(PixivRemoteError):
    """Pixiv 作品受限、私密或当前账号无权访问。"""

    status_code = 403


class PixivAuthError(PixivRemoteError):
    """Pixiv 认证失败，自动恢复后仍无法继续。"""

    status_code = 401


class PixivUnavailableError(PixivRemoteError):
    """Pixiv 暂时不可用，例如限流、超时或临时服务故障。"""

    status_code = 503


class ArchiveStorageError(PixivRemoteError):
    """本地归档存储不可写或剩余空间不足。"""

    status_code = 507


class ArchiveMaintenanceError(PixivRemoteError):
    """归档处于维护冻结状态，当前写操作需要稍后重试。"""

    status_code = 503


def is_retryable_archive_error(exc: Exception) -> bool:
    """判断归档任务失败是否适合自动重试。

    明确的不存在、权限限制、认证失败、磁盘空间不足和参数/数据格式问题
    不自动重试；网络、Pixiv 临时不可用、维护竞态和未知上游 502 类错误
    可以进入指数退避重试。
    """
    if isinstance(
        exc,
        (
            PixivNotFoundError,
            PixivRestrictedError,
            PixivAuthError,
            ArchiveStorageError,
            ValueError,
            PermissionError,
        ),
    ):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in {408, 425, 429} or status >= 500
    if isinstance(
        exc,
        (
            PixivUnavailableError,
            ArchiveMaintenanceError,
            httpx.RequestError,
            TimeoutError,
            ConnectionError,
            OperationalError,
            DisconnectionError,
        ),
    ):
        return True
    return type(exc) is PixivRemoteError
