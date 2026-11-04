from __future__ import annotations

from datetime import timedelta

import fakeredis.aioredis
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.errors import PixivAuthError, PixivRestrictedError, PixivUnavailableError
from app.models.work import (
    AccountMigrationItem, AccountMigrationRun, AccountMigrationState,
    Base, PixivWork, Series, SyncSource, SyncSourceType, WorkType,
)
from app.services.account_migration import AccountMigrationService
from app.services.cache_service import now_utc


class FakeClient:
    """An isolated remote model; no SDK or network calls are made."""

    def __init__(self, account="100"):
        self.account = account
        self.identity_calls = 0
        self.identity_error = None
        self.bookmarks = {}
        self.followed = set()
        self.attempts = []
        self.writes = []
        self.failures = {}
        self.switch_after_first_write = None

    async def get_account_identity(self, *, force=True):
        self.identity_calls += 1
        if self.identity_error:
            raise self.identity_error
        return {"user_id": self.account, "app_user_id": self.account,
                "web_user_id": self.account, "same_account": True}

    def _before_write(self, kind, target_id, expected_user_id):
        if str(expected_user_id) != self.account:
            raise PixivAuthError("wrong account")
        key = (kind, str(target_id))
        self.attempts.append((self.account, *key))
        errors = self.failures.get(key, [])
        if errors:
            raise errors.pop(0)
        return (self.account, *key)

    async def ensure_bookmarked(self, target_id, kind, *, restrict, expected_user_id):
        assert restrict == "private"
        key = self._before_write(kind.value, target_id, expected_user_id)
        if key in self.bookmarks:
            return False
        self.bookmarks[key] = {"restrict": restrict, "tags": []}
        self.writes.append(key)
        if self.switch_after_first_write:
            self.account = self.switch_after_first_write
            self.switch_after_first_write = None
        return True

    async def ensure_series_followed(self, kind, target_id, *, expected_user_id):
        key = self._before_write(f"series_{kind}", target_id, expected_user_id)
        if key in self.followed:
            return False
        self.followed.add(key)
        self.writes.append(key)
        return True


@pytest.fixture
async def migration(monkeypatch):
    settings = get_settings()
    for key, value in {
        "account_migration_enabled": True, "account_migration_batch_size": 2,
        "account_migration_scan_page_size": 2, "account_migration_lock_ttl_seconds": 180,
        "account_migration_max_attempts": 3, "account_migration_retry_base_seconds": 60,
        "account_migration_retry_max_seconds": 3600,
    }.items():
        monkeypatch.setattr(settings, key, value)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [Series.__table__, PixivWork.__table__, SyncSource.__table__,
              AccountMigrationState.__table__, AccountMigrationRun.__table__, AccountMigrationItem.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        client = FakeClient()
        yield AccountMigrationService(session, client, redis), session, client, redis
    await redis.aclose()
    await engine.dispose()


def work(wid, kind=WorkType.ILLUST):
    return PixivWork(id=str(wid), work_type=kind, title="fixture", version_token="a" * 64)


async def finish(service, *, max_ticks=30):
    for _ in range(max_ticks):
        result = await service.tick()
        if result["status"] == "completed":
            return result
    pytest.fail("migration did not complete within bounded fixture ticks")


@pytest.mark.asyncio
async def test_same_number_is_migrated_in_both_namespaces_with_small_pages(migration, monkeypatch):
    service, session, client, _ = migration
    monkeypatch.setattr(get_settings(), "account_migration_scan_page_size", 1)
    session.add_all([work(77, WorkType.MANGA), work(77, WorkType.NOVEL), work(78, WorkType.NOVEL)])
    await session.commit()
    await service.tick(allow_account_switch=True)
    result = await finish(service)
    assert result["counts"]["succeeded"] == 3
    assert set(client.bookmarks) == {
        ("100", "illust", "77"), ("100", "novel", "77"), ("100", "novel", "78"),
    }


@pytest.mark.asyncio
async def test_bounded_library_scan_covers_all_kinds_and_series_sources(migration):
    service, session, client, _ = migration
    session.add_all([work(1), work(2, WorkType.MANGA), work(3, WorkType.UGOIRA), work(4, WorkType.NOVEL)])
    session.add_all([
        Series(type="novel", id="10", title="novel series", author_id="1", author_name="A"),
        Series(type="manga", id="20", title="manga series", author_id="1", author_name="A"),
        SyncSource(id=1, source_type=SyncSourceType.NOVEL_SERIES, remote_user_id="10"),
        SyncSource(id=2, source_type=SyncSourceType.NOVEL_SERIES, remote_user_id="99"),
        SyncSource(id=3, source_type=SyncSourceType.AUTHOR, remote_user_id="999"),
    ])
    await session.commit()
    original = {"restrict": "public", "tags": ["keep"]}
    client.bookmarks[("100", "illust", "1")] = original.copy()
    first = await service.tick(allow_account_switch=True)
    assert first["discovered_this_tick"] == 2
    assert first["processed_this_tick"] == 2
    assert first["total"] == 2
    result = await finish(service)
    assert result["total"] == 7  # Duplicate Series + source identity is merged.
    assert result["counts"]["succeeded"] == 7
    assert client.bookmarks[("100", "illust", "1")] == original
    assert set(client.bookmarks) == {
        ("100", "illust", "1"), ("100", "illust", "2"),
        ("100", "illust", "3"), ("100", "novel", "4"),
    }
    assert client.followed == {
        ("100", "series_novel", "10"), ("100", "series_manga", "20"),
        ("100", "series_novel", "99"),
    }
    before = client.identity_calls
    assert (await service.tick())["status"] == "idle"
    assert client.identity_calls == before


@pytest.mark.asyncio
async def test_manual_rescan_adds_new_local_data_without_overwriting_remote_state(migration):
    service, session, client, _ = migration
    session.add(work(1))
    await session.commit()
    first = await service.tick(allow_account_switch=True)
    first_id = first["run_id"]
    await finish(service)
    session.add(work(2, WorkType.NOVEL))
    await session.commit()
    assert (await service.tick())["status"] == "idle"
    manual = await service.create_run("100")
    assert manual["run_id"] != first_id and manual["automatic"] is False
    result = await finish(service)
    assert result["run_id"] == manual["run_id"]
    assert result["total"] == 2
    assert client.writes == [("100", "illust", "1"), ("100", "novel", "2")]
    with pytest.raises(PixivAuthError):
        await service.create_run("999")


@pytest.mark.asyncio
async def test_account_switch_stops_old_run_and_stale_worker_cannot_reactivate_it(migration, monkeypatch):
    service, session, client, redis = migration
    monkeypatch.setattr(get_settings(), "account_migration_batch_size", 10)
    monkeypatch.setattr(get_settings(), "account_migration_scan_page_size", 10)
    session.add_all([work(1), work(2), work(3)])
    await session.commit()
    client.switch_after_first_write = "200"
    stopped = await service.tick(allow_account_switch=True)
    assert stopped["status"] == "paused"
    old_id = stopped["run_id"]
    assert client.writes == [("100", "illust", "1")]
    assert (await service.tick())["status"] == "account_changed"
    switched = await service.tick(allow_account_switch=True)
    assert switched["target_user_id"] == "200"
    assert switched["counts"]["succeeded"] == 3
    assert (await service.get_run(old_id))["status"] == "paused"
    old_client = FakeClient("100")
    stale = AccountMigrationService(session, old_client, redis)
    assert (await stale.tick())["status"] == "account_changed"
    assert old_client.writes == []
    state = await session.get(AccountMigrationState, 1)
    assert state.active_user_id == "200"


@pytest.mark.asyncio
async def test_unknown_old_credentials_do_not_pause_current_account(migration):
    service, session, client, redis = migration
    session.add_all([work(1), work(2), work(3)])
    await session.commit()
    first = await service.tick(allow_account_switch=True)
    old_client = FakeClient("999")
    old_client.identity_error = PixivAuthError("expired")
    stale = AccountMigrationService(session, old_client, redis)
    assert (await stale.tick())["status"] == "auth_required"
    assert (await service.get_run(first["run_id"]))["status"] == "running"


@pytest.mark.asyncio
async def test_permanent_item_failure_does_not_block_later_items_and_transient_errors_backoff(migration, monkeypatch):
    service, session, client, _ = migration
    monkeypatch.setattr(get_settings(), "account_migration_batch_size", 10)
    monkeypatch.setattr(get_settings(), "account_migration_scan_page_size", 10)
    session.add_all([work(1), work(2), work(3)])
    await session.commit()
    client.failures[("illust", "1")] = [PixivRestrictedError("private")]
    client.failures[("illust", "2")] = [PixivUnavailableError("limited")]
    first = await service.tick(allow_account_switch=True)
    assert first["counts"]["skipped"] == 1
    assert first["counts"]["failed"] == 1
    assert first["counts"]["succeeded"] == 1
    before = list(client.attempts)
    await service.tick()
    assert client.attempts == before
    await session.execute(update(AccountMigrationItem).where(
        AccountMigrationItem.run_id == first["run_id"], AccountMigrationItem.target_id == "2",
    ).values(next_attempt_at=now_utc() - timedelta(seconds=1)))
    await session.commit()
    result = await finish(service)
    assert result["counts"]["succeeded"] == 2
    assert result["counts"]["skipped"] == 1
    assert client.attempts.count(("100", "illust", "2")) == 2


@pytest.mark.asyncio
async def test_auth_failure_pauses_instead_of_spending_item_retries(migration):
    service, session, client, _ = migration
    session.add_all([work(1), work(2)])
    await session.commit()
    client.failures[("illust", "1")] = [PixivAuthError("credentials changed")]
    result = await service.tick(allow_account_switch=True)
    assert result["status"] == "paused"
    assert result["counts"]["failed"] == 0
    assert client.writes == []
    item = await session.get(AccountMigrationItem, (result["run_id"], "illust", "1"))
    assert item.status == "pending" and item.attempts == 0
    await finish(service)
    assert len(client.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_uncertain_progress_commit_replays_idempotently(migration, monkeypatch, committed):
    service, session, client, _ = migration
    session.add(work(1))
    await session.commit()
    original_commit = session.commit
    injected = False

    async def uncertain_commit():
        nonlocal injected
        if not injected and any(isinstance(row, AccountMigrationItem) and row.status == "succeeded" for row in session.dirty):
            injected = True
            if committed:
                await original_commit()
            raise OperationalError("COMMIT", {}, ConnectionError("response lost"))
        await original_commit()

    monkeypatch.setattr(session, "commit", uncertain_commit)
    with pytest.raises(OperationalError):
        await service.tick(allow_account_switch=True)
    await session.rollback()
    result = await finish(service)
    assert result["counts"]["succeeded"] == 1
    assert client.writes == [("100", "illust", "1")]


@pytest.mark.asyncio
async def test_restart_rescan_preserves_successes_and_discovers_new_data(migration):
    service, session, client, _ = migration
    session.add(work(1))
    await session.commit()
    first = await service.tick(allow_account_switch=True)
    await finish(service)
    session.add(work(2))
    await session.commit()
    restarted = await service.tick(allow_account_switch=True)
    assert restarted["run_id"] == first["run_id"]
    assert client.attempts.count(("100", "illust", "1")) == 1
    final = await finish(service)
    assert final["counts"]["succeeded"] == 2
    assert client.writes == [("100", "illust", "1"), ("100", "illust", "2")]


@pytest.mark.asyncio
async def test_activation_permission_is_consumed_before_later_batch_failure(migration, monkeypatch):
    service, session, _, _ = migration

    async def broken_discovery(run):
        raise RuntimeError("discovery failed after activation")

    monkeypatch.setattr(service, "_discover_page", broken_discovery)
    with pytest.raises(RuntimeError, match="discovery failed"):
        await service.tick(allow_account_switch=True)
    assert service.activation_attempted is True
    assert service.activation_target == "100"
    state = await session.get(AccountMigrationState, 1)
    assert state.active_user_id == "100"


@pytest.mark.asyncio
async def test_identity_failure_does_not_consume_startup_activation_permission(migration):
    service, _, client, _ = migration
    client.identity_error = PixivAuthError("not authenticated yet")
    result = await service.tick(allow_account_switch=True)
    assert result["status"] == "auth_required"
    assert service.activation_attempted is False
    assert service.activation_target is None


def migration_item(run_id, target, status):
    now = now_utc()
    return AccountMigrationItem(
        run_id=run_id, kind="illust", target_id=str(target), status=status,
        attempts=3, next_attempt_at=None, last_error_type="FixtureError",
        last_error="fixture", created_at=now, updated_at=now, finished_at=now,
    )


@pytest.mark.asyncio
async def test_retry_failed_only_resets_failed_items_of_requested_run(migration):
    service, session, _, _ = migration
    first = await service.create_run()
    second = await service.create_run()
    first_id, second_id = first["run_id"], second["run_id"]
    session.add_all([
        migration_item(first_id, 1, "failed"), migration_item(first_id, 2, "succeeded"),
        migration_item(first_id, 3, "skipped"), migration_item(second_id, 4, "failed"),
    ])
    run = await session.get(AccountMigrationRun, first_id)
    run.scan_cursor = {"id": "123"}
    await session.commit()
    result = await service.retry_failed(first_id)
    assert result["counts"] == {"pending": 1, "running": 0, "succeeded": 1, "failed": 0, "skipped": 1}
    items = (await session.execute(select(AccountMigrationItem).order_by(AccountMigrationItem.target_id))).scalars().all()
    assert [(row.status, row.attempts) for row in items] == [
        ("pending", 0), ("succeeded", 3), ("skipped", 3), ("failed", 3),
    ]
    run = await session.get(AccountMigrationRun, first_id)
    assert run.scan_cursor == {"id": "123"}


@pytest.mark.asyncio
async def test_retry_failed_rejects_other_account_without_pausing_current_run(migration):
    service, session, client, _ = migration
    old = await service.create_run()
    old_id = old["run_id"]
    session.add(migration_item(old_id, 1, "failed"))
    await session.commit()
    client.account = "200"
    current = await service.create_run()
    with pytest.raises(PixivAuthError):
        await service.retry_failed(old_id)
    old_item = await session.get(AccountMigrationItem, (old_id, "illust", "1"))
    assert old_item.status == "failed" and old_item.attempts == 3
    assert (await service.get_run(current["run_id"]))["status"] == "pending"
