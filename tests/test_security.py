from app.core.security import authorized, cache_read_allowed


def test_authorized_fail_closed():
    secret = "x" * 32
    assert not authorized("", "")
    assert not authorized("secret", "secret")
    assert not authorized(secret, None)
    assert not authorized(secret, "wrong")
    assert authorized(secret, secret)


def test_revoked_remote_work_never_serves_cached_files():
    assert cache_read_allowed("active")
    assert cache_read_allowed("unavailable")
    for state in ("deleted", "restricted", "auth_required"):
        assert not cache_read_allowed(state)
