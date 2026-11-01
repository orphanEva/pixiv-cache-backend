from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import get_settings

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

def redact(message: str) -> str:
    """避免在日志中记录后端密钥、Pixiv Token/Cookie。"""
    settings = get_settings()
    for value in (settings.api_key, settings.pixiv_refresh_token, settings.pixiv_cookie):
        if value and len(value) >= 8:
            message = message.replace(value, "[REDACTED]")
    return message

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "request_id": request_id_ctx.get(),
        }
        for key in (
            "pixiv_id", "work_type", "source", "version", "job_id",
            "series_id", "series_type", "kind", "queued", "count",
            "deduplicated", "error_type", "attempts", "status_code",
            "duration_ms", "method", "path", "consumer",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)

def configure_logging(level: int | None = None) -> None:
    settings = get_settings()
    if level is None:
        level = getattr(logging, settings.log_level.upper(), logging.INFO)
    fmt = JsonFormatter()
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(fmt)
    handlers: list[logging.Handler] = [stdout]
    if settings.log_file:
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Multi-process deployments must use distinct log-file paths per process.
        file_handler = RotatingFileHandler(
            path, maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count, encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
