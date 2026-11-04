"""Redis-backed request pacing shared by API, archive and sync processes."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.core.config import get_settings
from app.core.errors import PixivUnavailableError


def rate_limit_payload(payload) -> bool:
    """Recognize explicit API error fields, never arbitrary response/page text."""
    if not isinstance(payload, dict) or not payload.get('error'):
        return False
    error = payload['error']
    fields = [error] if isinstance(error, str) else []
    if isinstance(error, dict):
        for name in ('status_code', 'http_status', 'status', 'code'):
            if str(error.get(name)) == '429':
                return True
        fields.extend(error.get(name) for name in ('code', 'message', 'user_message', 'reason', 'details'))
    elif error is True:
        fields.extend(payload.get(name) for name in ('code', 'message', 'reason'))
    return any(
        isinstance(value, str) and bool(re.search(
            r'\brate[\s_-]*limit(?:ed|ing)?\b|\btoo many requests\b', value, re.IGNORECASE
        ))
        for value in fields
    )


def response_is_rate_limit(response) -> bool:
    """Some Pixiv endpoints report rate_limit inside HTTP 400/403 or 200."""
    if response.status_code == 429:
        return True
    try:
        return rate_limit_payload(response.json())
    except (TypeError, ValueError):
        return False


# Redis TIME avoids clock skew between containers. A request only claims the
# current slot, rather than reserving future slots that could bypass a new 429.
_ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local next_at = tonumber(redis.call('GET', KEYS[1]) or '0')
local cool_at = tonumber(redis.call('GET', KEYS[2]) or '0')
local ready = math.max(next_at, cool_at)
if ready > now then return ready - now end
redis.call('SET', KEYS[1], now + tonumber(ARGV[1]), 'PX', math.max(1000, tonumber(ARGV[1]) * 2))
return 0
"""

_COOLDOWN = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local until_at = math.max(current, now + tonumber(ARGV[1]))
redis.call('SET', KEYS[1], until_at, 'PX', math.max(1000, until_at - now))
return until_at - now
"""


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse both RFC Retry-After forms without accepting NaN/infinity."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class PixivRateLimiter:
    def __init__(self, redis_client=None, *, credential: str | None = None,
                 interval_seconds: float | None = None,
                 cooldown_seconds: float | None = None) -> None:
        settings = get_settings()
        self.redis = redis_client
        self.interval = max(0.0, float(interval_seconds if interval_seconds is not None
                                       else getattr(settings, 'pixiv_request_interval_seconds', 2.0)))
        self.cooldown = max(0.0, float(cooldown_seconds if cooldown_seconds is not None
                                       else getattr(settings, 'pixiv_rate_limit_cooldown_seconds', 300.0)))
        identity = credential or settings.pixiv_refresh_token or settings.pixiv_cookie or 'anonymous'
        digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        prefix = getattr(settings, 'pixiv_rate_limit_prefix', 'pixiv:rate')
        # Hash tag also keeps the two keys in one Redis Cluster slot.
        self.key = f'{prefix}:{{{digest}}}'

    def _redis(self):
        if self.redis is None:
            from app.core.redis_client import get_redis
            self.redis = get_redis()
        return self.redis

    async def wait(self) -> None:
        # Explicitly setting zero disables pacing (useful for offline tests).
        if self.interval <= 0:
            return
        while True:
            try:
                delay_ms = int(await self._redis().eval(
                    _ACQUIRE, 2, self.key + ':next', self.key + ':cooldown',
                    max(1, math.ceil(self.interval * 1000)),
                ))
            except Exception:
                # Do not silently send an unthrottled burst when Redis is down.
                raise PixivUnavailableError('Pixiv request limiter is unavailable') from None
            if delay_ms <= 0:
                return
            await asyncio.sleep(min(delay_ms / 1000, 1.0))

    async def penalize(self, retry_after: str | None = None) -> None:
        if self.interval <= 0:
            return
        delay = max(self.cooldown, retry_after_seconds(retry_after) or 0.0)
        try:
            await self._redis().eval(_COOLDOWN, 1, self.key + ':cooldown',
                                     max(1, math.ceil(delay * 1000)))
        except Exception:
            raise PixivUnavailableError('Pixiv request limiter is unavailable') from None
