"""Constant-time credential checks shared by HTTP middleware and tests."""
import hmac


def authorized(configured_key: str, supplied_key: str | None) -> bool:
    if len(configured_key) < 32 or not supplied_key:
        return False
    return hmac.compare_digest(configured_key, supplied_key)


def cache_read_allowed(status: str) -> bool:
    """Remote access revocation must not be bypassed by local cache paths."""
    return status in {"active", "unavailable", "error"}
