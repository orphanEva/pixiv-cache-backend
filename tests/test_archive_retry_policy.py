from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.core.errors import (
    ArchiveStorageError,
    PixivAuthError,
    PixivNotFoundError,
    PixivRemoteError,
    PixivUnavailableError,
    is_retryable_archive_error,
)
from app.services.archive_jobs import ArchiveJobQueue


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"status {status_code}",
        request=request,
        response=response,
    )


def test_archive_retry_policy_only_retries_transient_failures():
    assert is_retryable_archive_error(PixivUnavailableError("temporary")) is True
    assert is_retryable_archive_error(PixivRemoteError("unknown upstream")) is True
    assert is_retryable_archive_error(_http_status_error(503)) is True
    assert is_retryable_archive_error(_http_status_error(429)) is True
    assert is_retryable_archive_error(_http_status_error(404)) is False
    assert is_retryable_archive_error(PixivNotFoundError("gone")) is False
    assert is_retryable_archive_error(PixivAuthError("bad credential")) is False
    assert is_retryable_archive_error(ArchiveStorageError("disk full")) is False
    assert is_retryable_archive_error(ValueError("bad archive")) is False


def test_archive_retry_delay_is_exponential_and_capped():
    settings = get_settings()
    old_base = settings.archive_job_retry_base_seconds
    old_max = settings.archive_job_retry_max_seconds
    settings.archive_job_retry_base_seconds = 30
    settings.archive_job_retry_max_seconds = 120
    try:
        queue = ArchiveJobQueue(redis_client=None)
        assert queue.retry_delay_seconds(1) == 30
        assert queue.retry_delay_seconds(2) == 60
        assert queue.retry_delay_seconds(3) == 120
        assert queue.retry_delay_seconds(10) == 120
    finally:
        settings.archive_job_retry_base_seconds = old_base
        settings.archive_job_retry_max_seconds = old_max
