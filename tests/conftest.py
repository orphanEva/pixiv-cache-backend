"""Unit tests must not need the production Redis request limiter."""
import os

os.environ.setdefault('PIXIV_REQUEST_INTERVAL_SECONDS', '0')

import fakeredis.aioredis
import pytest


@pytest.fixture
async def maintenance_redis():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield redis
    finally:
        await redis.aclose()
