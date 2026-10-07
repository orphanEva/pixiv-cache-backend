import os
import sys

import redis


def main() -> int:
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    key = os.getenv(
        "ARCHIVE_WORKER_HEARTBEAT_KEY",
        "pixiv:archive:worker:healthy",
    )
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        return 0 if client.exists(key) else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
