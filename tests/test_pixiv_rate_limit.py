from datetime import datetime, timezone
import asyncio
import time

import fakeredis.aioredis
import pytest

from app.core.errors import PixivUnavailableError
from app.core.pixiv_rate_limit import PixivRateLimiter, retry_after_seconds


def test_retry_after_accepts_seconds_and_http_date():
    assert retry_after_seconds('90') == 90
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    assert retry_after_seconds('Wed, 04 Nov 2026 00:01:00 GMT', now=now) == 60
    assert retry_after_seconds('NaN') is None
    assert retry_after_seconds('bad') is None


@pytest.mark.asyncio
async def test_independent_clients_share_atomic_request_slots():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first = PixivRateLimiter(redis, credential='same-account', interval_seconds=0.05)
    second = PixivRateLimiter(redis, credential='same-account', interval_seconds=0.05)
    times = []

    async def acquire(limiter):
        await limiter.wait()
        times.append(time.monotonic())

    try:
        await asyncio.gather(acquire(first), acquire(second), acquire(first))
        times.sort()
        assert times[1] - times[0] >= 0.04
        assert times[2] - times[1] >= 0.04
        assert first.key == second.key
        assert 'same-account' not in first.key
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_shared_cooldown_is_observed_and_never_shortened():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first = PixivRateLimiter(redis, credential='same', interval_seconds=0.01, cooldown_seconds=0.03)
    second = PixivRateLimiter(redis, credential='same', interval_seconds=0.01, cooldown_seconds=0.03)
    try:
        await first.penalize('0.12')
        previous = int(await redis.get(first.key + ':cooldown'))
        await second.penalize('0.01')
        assert int(await redis.get(first.key + ':cooldown')) >= previous
        started = time.monotonic()
        await second.wait()
        assert time.monotonic() - started >= 0.09
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_limiter_fails_closed_when_redis_is_unavailable():
    class BrokenRedis:
        async def eval(self, *args):
            raise ConnectionError('offline')
    with pytest.raises(PixivUnavailableError, match='limiter'):
        await PixivRateLimiter(BrokenRedis(), interval_seconds=1).wait()
